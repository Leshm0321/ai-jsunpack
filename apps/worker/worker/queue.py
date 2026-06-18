from __future__ import annotations

import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from apps.api.app.models import ArtifactRecord, FailureClass, JobRecord, JobStatus
from apps.api.app.store import (
    DEFAULT_WORKER_LEASE_SECONDS,
    DEFAULT_WORKER_MAX_ATTEMPTS,
    create_store,
)
from packages.deployment import DeploymentConfigurationError, validate_current_environment

from .pipeline import PipelineEvent, WorkerPipeline


WORKER_ID_ENV = "AI_JSUNPACK_WORKER_ID"
WORKER_LEASE_SECONDS_ENV = "AI_JSUNPACK_WORKER_LEASE_SECONDS"
WORKER_POLL_SECONDS_ENV = "AI_JSUNPACK_WORKER_POLL_SECONDS"
WORKER_MAX_ATTEMPTS_ENV = "AI_JSUNPACK_WORKER_MAX_ATTEMPTS"
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
    def __init__(self, store, *, job_id: str, worker_id: str, lease_seconds: int) -> None:
        self.store = store
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_seconds = max(1, lease_seconds)
        self.interval_seconds = max(1.0, self.lease_seconds / 2)
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

    def run_once(self) -> QueueRunResult | None:
        self.store.requeue_expired_leases(max_attempts=self.max_attempts)
        job = self.store.lease_next_job(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
        )
        if job is None:
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
                )
                renewer.start()
                try:
                    pipeline_run = self.pipeline.run(job.id, input_path=input_path, store=proxy)
                finally:
                    renewer.stop()

            current = self.store.get_job(job.id)
            status = current.status if current is not None else "failed"
            message = pipeline_run.events[-1].message if pipeline_run.events else "Worker pipeline finished."
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
