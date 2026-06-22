from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from apps.api.app.models import BrowserRunRequest, BrowserRunSummary
from apps.browser_runner.app.main import BrowserRunnerQueue, SqlAlchemyBrowserRunQueueBackend
from apps.worker.worker.runtime_smoke import BrowserSmokeCapture, BrowserSmokeRequest


@dataclass(frozen=True)
class BrowserRunnerSoakConfig:
    instances: int = 2
    workers_per_instance: int = 2
    runs: int = 24
    capture_delay_ms: int = 25
    fail_every: int = 0
    max_attempts: int = 3
    lease_seconds: int = 30
    retry_backoff_ms: int = 0
    poll_seconds: float = 0.05
    timeout_seconds: float = 30.0
    database_url: str | None = None
    output_path: str | None = None
    include_recovery_probe: bool = True


class SyntheticBrowserAdapter:
    def __init__(self, *, capture_delay_ms: int, fail_every: int = 0) -> None:
        self.capture_delay_seconds = max(0, capture_delay_ms) / 1000
        self.fail_every = max(0, fail_every)
        self.total_captures = 0
        self.failed_captures = 0
        self.active_captures = 0
        self.max_active_captures = 0
        self._lock = threading.Lock()

    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        with self._lock:
            self.total_captures += 1
            capture_index = self.total_captures
            self.active_captures += 1
            self.max_active_captures = max(self.max_active_captures, self.active_captures)
        try:
            if self.capture_delay_seconds:
                time.sleep(self.capture_delay_seconds)
            if self.fail_every and capture_index % self.fail_every == 0:
                with self._lock:
                    self.failed_captures += 1
                raise RuntimeError(f"synthetic browser capture failure {capture_index}")
            request.screenshot_path.write_bytes(b"\x89PNG\r\n\x1a\nbrowser-runner-soak")
            return BrowserSmokeCapture(
                console_messages=[f"synthetic capture {capture_index}"],
                responses=[f"200 {request.entry_url}"],
                dom_summary={"title": "browser-runner-soak", "nodeCount": 1},
            )
        finally:
            with self._lock:
                self.active_captures -= 1


def run_browser_runner_soak(config: BrowserRunnerSoakConfig) -> dict[str, Any]:
    _validate_config(config)
    started_at = time.time()
    started_wall = _utc_timestamp()
    with tempfile.TemporaryDirectory(prefix="ai-jsunpack-browser-runner-soak-") as temp_dir:
        root = Path(temp_dir)
        engine = _create_engine(config, root)
        queues: list[BrowserRunnerQueue] = []
        adapter = SyntheticBrowserAdapter(capture_delay_ms=config.capture_delay_ms, fail_every=config.fail_every)
        try:
            for index in range(config.instances):
                queues.append(
                    BrowserRunnerQueue(
                        browser_adapter=adapter,
                        backend=SqlAlchemyBrowserRunQueueBackend(engine=engine),
                        max_workers=config.workers_per_instance,
                        workdir=root / f"instance-{index}",
                        max_attempts=config.max_attempts,
                        lease_seconds=config.lease_seconds,
                        retry_backoff_seconds=config.retry_backoff_ms / 1000,
                        poll_seconds=config.poll_seconds,
                    )
                )

            submitted = [queues[0].submit(_request(index)) for index in range(config.runs)]
            summaries = _wait_for_terminal_runs(queues[0], submitted, timeout_seconds=config.timeout_seconds)
            metrics = queues[0].metrics()
            health = queues[0].health()
            finished_at = time.time()
            duration_ms = int((finished_at - started_at) * 1000)
            status_counts = _status_counts(summaries)
            recovery_probe = _run_recovery_probe(root) if config.include_recovery_probe else None
            result = {
                "kind": "browser_runner_soak_baseline",
                "schemaVersion": "1",
                "startedAt": started_wall,
                "finishedAt": _utc_timestamp(),
                "durationMs": duration_ms,
                "config": asdict(config),
                "submittedCount": len(submitted),
                "completedCount": sum(status_counts.get(status, 0) for status in ("pass", "fail", "best_effort")),
                "statusCounts": status_counts,
                "throughputRunsPerSecond": _throughput(config.runs, duration_ms),
                "syntheticAdapter": {
                    "totalCaptures": adapter.total_captures,
                    "failedCaptures": adapter.failed_captures,
                    "maxActiveCaptures": adapter.max_active_captures,
                },
                "queueMetrics": metrics.model_dump(by_alias=True),
                "queueHealth": health.model_dump(by_alias=True),
                "recoveryProbe": recovery_probe,
            }
            result["backendAssessment"] = _backend_assessment(result)
            _write_output(config, result)
            return result
        finally:
            for queue in queues:
                queue.close()
            engine.dispose()


def _validate_config(config: BrowserRunnerSoakConfig) -> None:
    if config.instances < 1:
        raise ValueError("instances must be >= 1")
    if config.workers_per_instance < 1:
        raise ValueError("workers_per_instance must be >= 1")
    if config.runs < 1:
        raise ValueError("runs must be >= 1")
    if config.max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if config.lease_seconds < 1:
        raise ValueError("lease_seconds must be >= 1")
    if config.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")


def _create_engine(config: BrowserRunnerSoakConfig, root: Path) -> Engine:
    database_url = config.database_url or f"sqlite:///{(root / 'shared-browser-runner-soak.db').as_posix()}"
    return create_engine(database_url, future=True)


def _wait_for_terminal_runs(
    queue: BrowserRunnerQueue,
    submitted: list[BrowserRunSummary],
    *,
    timeout_seconds: float,
) -> list[BrowserRunSummary]:
    pending = {summary.id for summary in submitted}
    terminal: dict[str, BrowserRunSummary] = {}
    deadline = time.monotonic() + timeout_seconds
    while pending and time.monotonic() < deadline:
        for run_id in list(pending):
            summary = queue.get(run_id)
            if summary is not None and summary.status in {"pass", "fail", "best_effort"}:
                terminal[run_id] = summary
                pending.remove(run_id)
        if pending:
            time.sleep(0.02)
    if pending:
        raise TimeoutError(f"Browser Runner soak timed out with {len(pending)} pending run(s).")
    return [terminal[summary.id] for summary in submitted]


def _run_recovery_probe(root: Path) -> dict[str, Any]:
    engine = create_engine(f"sqlite:///{(root / 'shared-browser-runner-recovery.db').as_posix()}", future=True)
    stale_queue = BrowserRunnerQueue(
        browser_adapter=SyntheticBrowserAdapter(capture_delay_ms=0),
        backend=SqlAlchemyBrowserRunQueueBackend(engine=engine),
        max_workers=1,
        workdir=root / "recovery-stale",
        lease_seconds=1,
        max_attempts=2,
        auto_start=False,
    )
    recovery_queue = BrowserRunnerQueue(
        browser_adapter=SyntheticBrowserAdapter(capture_delay_ms=0),
        backend=SqlAlchemyBrowserRunQueueBackend(engine=engine),
        max_workers=1,
        workdir=root / "recovery-active",
        lease_seconds=1,
        max_attempts=2,
        auto_start=False,
    )
    try:
        run = stale_queue.submit(_request(0, prefix="recovery"))
        claimed = stale_queue._claim(run.id)
        if claimed is None:
            raise RuntimeError("Recovery probe could not claim the initial run.")
        stale_queue.backend.update(run.id, lease_expires_at="2026-01-01T00:00:00+00:00")
        recovered = recovery_queue.recover_expired_leases(now="2026-01-01T00:00:02+00:00", schedule=False)
        recovery_queue._execute(run.id)
        summary = recovery_queue.get(run.id)
        if summary is None:
            raise RuntimeError("Recovery probe run disappeared after execution.")
        return {
            "recoveredCount": len(recovered),
            "status": summary.status,
            "attempt": summary.attempt,
            "leaseRecovered": summary.lease_recovered,
            "queueBackend": summary.queue_backend,
            "metrics": recovery_queue.metrics().model_dump(by_alias=True),
        }
    finally:
        stale_queue.close()
        recovery_queue.close()
        engine.dispose()


def _request(index: int, *, prefix: str = "soak") -> BrowserRunRequest:
    return BrowserRunRequest(
        job_id=f"{prefix}_job_{index}",
        target="reconstructed",
        attempt=0,
        entry_url="about:blank",
        timeout_ms=1000,
        network_policy="deny",
    )


def _status_counts(summaries: list[BrowserRunSummary]) -> dict[str, int]:
    counts = {"queued": 0, "running": 0, "pass": 0, "fail": 0, "best_effort": 0}
    for summary in summaries:
        counts[summary.status] = counts.get(summary.status, 0) + 1
    return counts


def _throughput(runs: int, duration_ms: int) -> float:
    if duration_ms <= 0:
        return float(runs)
    return round(runs / (duration_ms / 1000), 3)


def _backend_assessment(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result["queueMetrics"]
    health = result["queueHealth"]
    recovery = result.get("recoveryProbe")
    complete = result["completedCount"] == result["submittedCount"]
    stable_metrics = (
        metrics.get("backendStatus") == "ok"
        and metrics.get("queuedCount") == 0
        and metrics.get("runningCount") == 0
        and metrics.get("expiredRunningCount") == 0
    )
    recovered = recovery is None or (
        recovery.get("recoveredCount") == 1
        and recovery.get("status") == "pass"
        and recovery.get("leaseRecovered") is True
        and recovery.get("attempt") == 2
    )
    acceptable = complete and stable_metrics and health.get("status") == "ok" and recovered
    return {
        "recommendation": "continue_shared_db_backend" if acceptable else "reassess_queue_backend",
        "confidence": "medium" if acceptable else "low",
        "rationale": (
            "Shared SQL queue completed the configured multi-instance soak with stable metrics and lease recovery."
            if acceptable
            else "The soak exposed incomplete runs, degraded health, active backlog, expired leases, or failed recovery."
        ),
        "messageQueueMigrationRequired": not acceptable,
    }


def _write_output(config: BrowserRunnerSoakConfig, result: dict[str, Any]) -> None:
    if not config.output_path:
        return
    path = Path(config.output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")


def _utc_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def parse_args(argv: list[str] | None = None) -> BrowserRunnerSoakConfig:
    parser = argparse.ArgumentParser(description="Run a Browser Runner multi-instance queue soak baseline.")
    parser.add_argument("--instances", type=int, default=BrowserRunnerSoakConfig.instances)
    parser.add_argument("--workers-per-instance", type=int, default=BrowserRunnerSoakConfig.workers_per_instance)
    parser.add_argument("--runs", type=int, default=BrowserRunnerSoakConfig.runs)
    parser.add_argument("--capture-delay-ms", type=int, default=BrowserRunnerSoakConfig.capture_delay_ms)
    parser.add_argument("--fail-every", type=int, default=BrowserRunnerSoakConfig.fail_every)
    parser.add_argument("--max-attempts", type=int, default=BrowserRunnerSoakConfig.max_attempts)
    parser.add_argument("--lease-seconds", type=int, default=BrowserRunnerSoakConfig.lease_seconds)
    parser.add_argument("--retry-backoff-ms", type=int, default=BrowserRunnerSoakConfig.retry_backoff_ms)
    parser.add_argument("--poll-seconds", type=float, default=BrowserRunnerSoakConfig.poll_seconds)
    parser.add_argument("--timeout-seconds", type=float, default=BrowserRunnerSoakConfig.timeout_seconds)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--output", dest="output_path", default=None)
    parser.add_argument("--skip-recovery-probe", action="store_false", dest="include_recovery_probe")
    args = parser.parse_args(argv)
    return BrowserRunnerSoakConfig(**vars(args))


def main(argv: list[str] | None = None) -> None:
    config = parse_args(argv)
    result = run_browser_runner_soak(config)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
