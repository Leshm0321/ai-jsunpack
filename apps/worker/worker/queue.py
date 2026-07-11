from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from apps.api.app.models import ArtifactRecord, FailureClass, JobRecord, JobStatus, OpsAlert, OpsHeartbeatRequest
from apps.api.app.store import (
    DEFAULT_WORKER_LEASE_SECONDS,
    DEFAULT_WORKER_MAX_ATTEMPTS,
    create_store,
)
from packages.configuration import apply_application_config_to_environment
from packages.deployment import DeploymentConfigurationError, validate_current_environment

from .pipeline import PipelineEvent, WorkerPipeline


logger = logging.getLogger(__name__)
apply_application_config_to_environment("worker")
WORKER_ID_ENV = "AI_JSUNPACK_WORKER_ID"
WORKER_LEASE_SECONDS_ENV = "AI_JSUNPACK_WORKER_LEASE_SECONDS"
WORKER_POLL_SECONDS_ENV = "AI_JSUNPACK_WORKER_POLL_SECONDS"
WORKER_MAX_ATTEMPTS_ENV = "AI_JSUNPACK_WORKER_MAX_ATTEMPTS"
OPS_HEARTBEAT_TTL_SECONDS_ENV = "AI_JSUNPACK_OPS_HEARTBEAT_TTL_SECONDS"
DEFAULT_WORKER_POLL_SECONDS = 5.0


@dataclass
class QueueRunResult:
    job_id: str
    status: JobStatus
    message: str
    events: list[PipelineEvent] = field(default_factory=list)


class LeasedStoreProxy:
    def __init__(self, store, *, job_id: str, worker_id: str) -> None:
        self._store = store
        self._job_id = job_id
        self._worker_id = worker_id

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def update_status(self, job_id: str, status: JobStatus, *args, **kwargs):
        if job_id == self._job_id and "expected_worker_id" not in kwargs:
            kwargs["expected_worker_id"] = self._worker_id
        return self._store.update_status(job_id, status, *args, **kwargs)


class LeaseRenewer:
    def __init__(self, store, *, job_id: str, worker_id: str, lease_seconds: int, on_renew=None) -> None:
        self.store = store
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_seconds = max(1, lease_seconds)
        self.interval_seconds = max(1.0, self.lease_seconds / 2)
        self.on_renew = on_renew
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lease-renewer-{job_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 1)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            renewed = self.store.renew_lease(
                job_id=self.job_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            if renewed is None:
                self._stop.set()
                return
            if self.on_renew is not None:
                try:
                    self.on_renew()
                except Exception as error:
                    logger.debug("Lease renew callback failed for job %s: %s", self.job_id, error, exc_info=True)


class WorkerQueueRunner:
    def __init__(
        self,
        *,
        store=None,
        pipeline: WorkerPipeline | None = None,
        worker_id: str | None = None,
        lease_seconds: int | None = None,
        poll_seconds: float | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self.store = store or create_store()
        self.pipeline = pipeline or WorkerPipeline()
        self.worker_id = worker_id or os.getenv(WORKER_ID_ENV) or f"worker-{os.getpid()}"
        self.lease_seconds = lease_seconds or parse_int_env(WORKER_LEASE_SECONDS_ENV, DEFAULT_WORKER_LEASE_SECONDS)
        self.poll_seconds = poll_seconds if poll_seconds is not None else parse_float_env(WORKER_POLL_SECONDS_ENV, DEFAULT_WORKER_POLL_SECONDS)
        self.max_attempts = max_attempts or parse_int_env(WORKER_MAX_ATTEMPTS_ENV, DEFAULT_WORKER_MAX_ATTEMPTS)
        self.deployment_profile = validate_current_environment("worker", strict=False)

    def run_once(self) -> QueueRunResult | None:
        self.store.requeue_expired_leases(max_attempts=self.max_attempts)
        self._record_ops_heartbeat(
            status=self._heartbeat_status(),
            metrics=self._ops_metrics_snapshot(phase="poll", job_id=None, job_status=None, message="poll loop"),
        )
        job = self.store.lease_next_job(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
        )
        if job is None:
            self._record_ops_heartbeat(
                status=self._heartbeat_status(),
                metrics=self._ops_metrics_snapshot(phase="idle", job_id=None, job_status=None, message="no job available"),
            )
            return None
        return self.run_leased_job(job)

    def run_forever(self, *, max_jobs: int | None = None) -> None:
        completed_jobs = 0
        while max_jobs is None or completed_jobs < max_jobs:
            result = self.run_once()
            if result is None:
                time.sleep(max(0.1, self.poll_seconds))
                continue
            completed_jobs += 1

    def run_leased_job(self, job: JobRecord) -> QueueRunResult:
        try:
            source_artifact = self._source_artifact(job)
            with self._source_input_path(source_artifact) as input_path:
                proxy = LeasedStoreProxy(self.store, job_id=job.id, worker_id=self.worker_id)
                renewer = LeaseRenewer(
                    self.store,
                    job_id=job.id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                    on_renew=lambda: self._record_ops_heartbeat(
                        status=self._heartbeat_status(),
                        metrics=self._ops_metrics_snapshot(
                            phase="lease_renewed",
                            job_id=job.id,
                            job_status="leased",
                            message="lease renewed",
                        ),
                    ),
                )
                renewer.start()
                try:
                    self._record_ops_heartbeat(
                        status=self._heartbeat_status(),
                        metrics=self._ops_metrics_snapshot(
                            phase="running",
                            job_id=job.id,
                            job_status="leased",
                            message="job leased",
                        ),
                    )
                    pipeline_run = self.pipeline.run(job.id, input_path=input_path, store=proxy)
                finally:
                    renewer.stop()

            current = self.store.get_job(job.id)
            status = current.status if current is not None else "failed"
            message = pipeline_run.events[-1].message if pipeline_run.events else "Worker pipeline finished."
            self._record_ops_heartbeat(
                status=self._heartbeat_status(job_status=status),
                metrics=self._ops_metrics_snapshot(
                    phase="completed",
                    job_id=job.id,
                    job_status=status,
                    message=message,
                    event_count=len(pipeline_run.events),
                ),
            )
            return QueueRunResult(job_id=job.id, status=status, message=message, events=pipeline_run.events)
        except Exception as error:
            failure_class = classify_worker_error(error)
            updated = self.store.update_status(
                job.id,
                "failed",
                failure_reason=str(error),
                failure_class=failure_class,
                expected_worker_id=self.worker_id,
            )
            self._record_ops_heartbeat(
                status="degraded",
                alerts=[
                    OpsAlert(
                        code="worker_pipeline_failed",
                        severity="warning",
                        message="Worker pipeline failed for the current job.",
                        field="failureClass",
                        value=failure_class,
                        threshold="none",
                        service_role="worker",
                        instance_id=self.worker_id,
                    )
                ],
                metrics=self._ops_metrics_snapshot(
                    phase="failed",
                    job_id=job.id,
                    job_status="failed",
                    message=str(error),
                    failure_class=failure_class,
                ),
            )
            return QueueRunResult(job_id=job.id, status=updated.status, message=str(error))

    def _source_artifact(self, job: JobRecord) -> ArtifactRecord:
        if job.input_artifact_id is None:
            raise ValueError(f"Job {job.id} has no source input artifact.")
        source_artifact = self.store.get_artifact(job.id, job.input_artifact_id)
        if source_artifact is None:
            raise ValueError(f"Job {job.id} source input artifact is missing: {job.input_artifact_id}")
        return source_artifact

    @contextmanager
    def _source_input_path(self, artifact: ArtifactRecord) -> Iterator[Path]:
        local_path = self.store.artifact_local_path(artifact)
        if local_path is not None:
            yield local_path
            return

        with tempfile.TemporaryDirectory(prefix="ai-jsunpack-source-input-") as temp_dir:
            filename = self.store.artifact_filename(artifact) or "source-input.bin"
            materialized = Path(temp_dir) / filename
            materialized.write_bytes(self.store.read_artifact_record(artifact))
            yield materialized

    def _ops_metrics_snapshot(
        self,
        *,
        phase: str,
        job_id: str | None,
        job_status: str | None,
        message: str,
        failure_class: FailureClass | None = None,
        event_count: int | None = None,
    ) -> dict[str, object]:
        metrics: dict[str, object] = {
            "workerId": self.worker_id,
            "leaseSeconds": self.lease_seconds,
            "pollSeconds": self.poll_seconds,
            "maxAttempts": self.max_attempts,
            "phase": phase,
            "message": message,
            "deploymentProfile": self.deployment_profile.status,
        }
        if job_id is not None:
            metrics["jobId"] = job_id
        if job_status is not None:
            metrics["jobStatus"] = job_status
        if failure_class is not None:
            metrics["failureClass"] = failure_class
        if event_count is not None:
            metrics["eventCount"] = event_count
        return metrics

    def _heartbeat_status(self, *, job_status: str | None = None) -> str:
        if self.deployment_profile.status != "ok":
            return "degraded"
        if job_status in {"failed", "cancelled"}:
            return "degraded"
        return "ok"

    def _record_ops_heartbeat(
        self,
        *,
        status: str,
        metrics: dict[str, object],
        alerts: list[OpsAlert] | None = None,
    ) -> None:
        heartbeat_alerts = list(alerts or [])
        if self.deployment_profile.status != "ok":
            heartbeat_alerts.append(
                OpsAlert(
                    code="worker_deployment_profile_warning",
                    severity="warning",
                    message="Worker deployment profile is not fully ok.",
                    field="deploymentProfile",
                    value=self.deployment_profile.status,
                    threshold="ok",
                    service_role="worker",
                    instance_id=self.worker_id,
                )
            )
        try:
            self.store.record_ops_heartbeat(
                OpsHeartbeatRequest(
                    service_role="worker",
                    instance_id=self.worker_id,
                    status=status,
                    ttl_seconds=parse_int_env(OPS_HEARTBEAT_TTL_SECONDS_ENV, max(self.lease_seconds * 2, 90)),
                    metrics=metrics,
                    alerts=heartbeat_alerts,
                    metadata={
                        "deploymentProfile": self.deployment_profile.status,
                        "workerId": self.worker_id,
                    },
                )
            )
        except Exception as error:
            logger.debug("Worker ops heartbeat could not be recorded: %s", error, exc_info=True)
            return


def classify_worker_error(error: Exception) -> FailureClass:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, (FileNotFoundError, ValueError)):
        return "invalid_input"
    if isinstance(error, PermissionError):
        return "sandbox_denied"
    return "unknown"


def parse_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return max(1, parsed)


def parse_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        parsed = float(raw_value)
    except ValueError:
        return default
    return max(0.1, parsed)


def main() -> None:
    try:
        validate_current_environment("worker")
    except DeploymentConfigurationError as error:
        raise SystemExit(str(error)) from error
    WorkerQueueRunner().run_forever()


if __name__ == "__main__":
    main()
