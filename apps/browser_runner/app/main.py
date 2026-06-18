from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from urllib.parse import quote
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException

from apps.api.app.auth import AccessContext, SERVICE_ROLE_WORKER, require_access
from apps.api.app.models import (
    BrowserRunRequest,
    BrowserRunResult,
    BrowserRunSourceArchive,
    BrowserRunStatus,
    BrowserRunSummary,
    FailureClass,
    RunStatus,
)
from apps.api.app.models import utc_now
from apps.worker.worker.runtime_smoke import (
    BrowserSmokeAdapter,
    BrowserSmokeCapture,
    BrowserSmokeRequest,
    PlaywrightBrowserAdapter,
    RuntimeSmokeError,
)
from packages.deployment import DeploymentConfigurationError, validate_current_environment

BROWSER_RUNNER_WORKERS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_WORKERS"
BROWSER_RUNNER_WORKDIR_ENV = "AI_JSUNPACK_BROWSER_RUNNER_WORKDIR"
BROWSER_RUNNER_DB_PATH_ENV = "AI_JSUNPACK_BROWSER_RUNNER_DB_PATH"
BROWSER_RUNNER_QUEUE_BACKEND_ENV = "AI_JSUNPACK_BROWSER_RUNNER_QUEUE_BACKEND"
BROWSER_RUNNER_MAX_ATTEMPTS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_MAX_ATTEMPTS"
BROWSER_RUNNER_LEASE_SECONDS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_LEASE_SECONDS"
BROWSER_RUNNER_RETRY_BACKOFF_SECONDS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_RETRY_BACKOFF_SECONDS"
BROWSER_RUNNER_POLL_SECONDS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_POLL_SECONDS"
DEFAULT_BROWSER_RUNNER_WORKERS = 2
DEFAULT_BROWSER_RUNNER_MAX_ATTEMPTS = 3
DEFAULT_BROWSER_RUNNER_LEASE_SECONDS = 120
DEFAULT_BROWSER_RUNNER_RETRY_BACKOFF_SECONDS = 1.0
DEFAULT_BROWSER_RUNNER_POLL_SECONDS = 0.25

try:
    DEPLOYMENT_PROFILE = validate_current_environment("browser-runner")
except DeploymentConfigurationError as error:
    raise RuntimeError(str(error)) from error


@dataclass(frozen=True)
class BrowserRunRecord:
    id: str
    request: BrowserRunRequest
    status: BrowserRunStatus
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: BrowserRunResult | None = None
    error: str | None = None
    attempt: int = 0
    max_attempts: int = DEFAULT_BROWSER_RUNNER_MAX_ATTEMPTS
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    next_run_at: str | None = None
    worker_id: str | None = None
    queue_backend: str = "sqlite"
    lease_recovered: bool = False

    def summary(self) -> BrowserRunSummary:
        return BrowserRunSummary(
            id=self.id,
            status=self.status,
            result=self.result,
            error=self.error,
            created_at=self.created_at,
            updated_at=self.updated_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            attempt=self.attempt,
            max_attempts=self.max_attempts,
            lease_owner=self.lease_owner,
            lease_expires_at=self.lease_expires_at,
            next_run_at=self.next_run_at,
            worker_id=self.worker_id,
            queue_backend=self.queue_backend,
            lease_recovered=self.lease_recovered,
        )


class BrowserRunnerQueue:
    def __init__(
        self,
        *,
        browser_adapter: BrowserSmokeAdapter | None = None,
        max_workers: int | None = None,
        workdir: Path | str | None = None,
        db_path: Path | str | None = None,
        max_attempts: int | None = None,
        lease_seconds: int | None = None,
        retry_backoff_seconds: float | None = None,
        poll_seconds: float | None = None,
        auto_start: bool = True,
    ) -> None:
        self.browser_adapter = browser_adapter or PlaywrightBrowserAdapter()
        self.max_workers = max_workers or parse_int_env(BROWSER_RUNNER_WORKERS_ENV, DEFAULT_BROWSER_RUNNER_WORKERS)
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self.workdir = Path(workdir or os.getenv(BROWSER_RUNNER_WORKDIR_ENV, tempfile.gettempdir()))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.queue_backend = os.getenv(BROWSER_RUNNER_QUEUE_BACKEND_ENV, "sqlite").strip().lower() or "sqlite"
        if self.queue_backend != "sqlite":
            self.queue_backend = "sqlite"
        self.db_path = Path(db_path or os.getenv(BROWSER_RUNNER_DB_PATH_ENV, self.workdir / "browser-runs.sqlite3"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts or parse_int_env(
            BROWSER_RUNNER_MAX_ATTEMPTS_ENV,
            DEFAULT_BROWSER_RUNNER_MAX_ATTEMPTS,
        )
        self.lease_seconds = lease_seconds or parse_int_env(
            BROWSER_RUNNER_LEASE_SECONDS_ENV,
            DEFAULT_BROWSER_RUNNER_LEASE_SECONDS,
        )
        self.retry_backoff_seconds = (
            retry_backoff_seconds
            if retry_backoff_seconds is not None
            else parse_float_env(BROWSER_RUNNER_RETRY_BACKOFF_SECONDS_ENV, DEFAULT_BROWSER_RUNNER_RETRY_BACKOFF_SECONDS)
        )
        self.poll_seconds = poll_seconds if poll_seconds is not None else parse_float_env(
            BROWSER_RUNNER_POLL_SECONDS_ENV,
            DEFAULT_BROWSER_RUNNER_POLL_SECONDS,
        )
        self.worker_id = f"browser-runner-{os.getpid()}-{uuid4().hex[:8]}"
        self.auto_start = auto_start
        self._lock = threading.Lock()
        self._submitted: set[str] = set()
        self._stop = threading.Event()
        self._scheduler = threading.Thread(target=self._scheduler_loop, name="browser-runner-scheduler", daemon=True)
        self._scheduler_started = False
        self._initialize_storage()
        self.recover_expired_leases(schedule=False)
        if self.auto_start:
            self._scheduler.start()
            self._scheduler_started = True
            self._schedule_due_runs()

    def submit(self, request: BrowserRunRequest) -> BrowserRunSummary:
        run_id = f"browser_run_{uuid4().hex[:12]}"
        now = utc_now()
        record = BrowserRunRecord(
            id=run_id,
            request=request,
            status="queued",
            created_at=now,
            updated_at=now,
            next_run_at=now,
            max_attempts=self.max_attempts,
            worker_id=self.worker_id,
            queue_backend=self.queue_backend,
        )
        self._insert(record)
        if self.auto_start:
            self._schedule_due_runs()
        return record.summary()

    def get(self, run_id: str) -> BrowserRunSummary | None:
        record = self._record(run_id)
        return record.summary() if record else None

    def close(self) -> None:
        self._stop.set()
        if self._scheduler_started:
            self._scheduler.join(timeout=max(1.0, self.poll_seconds + 1))
        self.executor.shutdown(wait=True, cancel_futures=False)

    def recover_expired_leases(self, *, now: str | None = None, schedule: bool = True) -> list[BrowserRunSummary]:
        timestamp = now or utc_now()
        current_time = parse_timestamp(timestamp)
        recovered: list[BrowserRunSummary] = []
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM browser_runs WHERE status = 'running'").fetchall()
            for row in rows:
                record = self._record_from_row(row)
                if record.id in self._submitted:
                    continue
                expires_at = parse_optional_timestamp(record.lease_expires_at)
                if expires_at is None or expires_at > current_time:
                    continue
                if record.attempt >= record.max_attempts:
                    result = BrowserRunResult(
                        status="best_effort",
                        failure_class="timeout",
                        page_errors=[f"Browser Runner lease expired after {record.attempt} attempt(s)."],
                        limitations=["Remote Browser Runner run expired before capture evidence could be completed."],
                        execution_boundary=self._execution_boundary(record, lease_recovered=True),
                    )
                    updated = replace(
                        record,
                        status="best_effort",
                        result=result,
                        error=result.page_errors[0],
                        updated_at=timestamp,
                        finished_at=timestamp,
                        lease_owner=None,
                        lease_expires_at=None,
                        lease_recovered=True,
                    )
                else:
                    updated = replace(
                        record,
                        status="queued",
                        updated_at=timestamp,
                        lease_owner=None,
                        lease_expires_at=None,
                        next_run_at=timestamp,
                        error="Previous Browser Runner lease expired; run returned to queue.",
                        lease_recovered=True,
                    )
                self._save(connection, updated)
                recovered.append(updated.summary())
        if schedule and self.auto_start:
            self._schedule_due_runs()
        return recovered

    def _execute(self, run_id: str) -> None:
        record = self._claim(run_id)
        if record is None:
            self._discard_submitted(run_id)
            return
        try:
            result = self._capture(record)
            self._update(
                run_id,
                status=result.status if result.status in {"pass", "fail", "best_effort"} else "best_effort",
                result=result,
                finished_at=utc_now(),
                lease_owner=None,
                lease_expires_at=None,
            )
        except Exception as error:
            current = self._record(run_id) or record
            failure_class = classify_browser_runner_error(error)
            retryable = failure_class not in {"invalid_input", "policy_denied", "sandbox_denied"}
            if retryable and current.attempt < current.max_attempts:
                next_run_at = (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, self.retry_backoff_seconds))).isoformat()
                self._update(
                    run_id,
                    status="queued",
                    error=str(error),
                    lease_owner=None,
                    lease_expires_at=None,
                    next_run_at=next_run_at,
                )
                return
            result = BrowserRunResult(
                status="best_effort",
                failure_class=failure_class,
                page_errors=[str(error)],
                limitations=["Browser Runner queue execution failed before capture evidence could be completed."],
                execution_boundary=self._execution_boundary(current),
            )
            self._update(
                run_id,
                status="best_effort",
                result=result,
                error=str(error),
                finished_at=utc_now(),
                lease_owner=None,
                lease_expires_at=None,
            )
        finally:
            self._discard_submitted(run_id)
            if self.auto_start:
                self._schedule_due_runs()

    def _capture(self, record: BrowserRunRecord) -> BrowserRunResult:
        limitations: list[str] = []
        request = record.request
        with tempfile.TemporaryDirectory(prefix="ai-jsunpack-browser-run-", dir=self.workdir) as temp_dir:
            temp_root = Path(temp_dir)
            screenshot_path = temp_root / "runtime-screenshot.png"
            source_root: Path | None = None
            if request.source_archive is not None:
                source_root = temp_root / "source"
                source_root.mkdir(parents=True, exist_ok=True)
                _extract_source_archive(request.source_archive, source_root)

            with _entry_url(source_root=source_root, source_archive=request.source_archive, fallback_url=request.entry_url) as entry_url:
                try:
                    capture = self.browser_adapter.capture(
                        BrowserSmokeRequest(
                            entry_url=entry_url,
                            screenshot_path=screenshot_path,
                            timeout_ms=request.timeout_ms,
                            wait_for_selector=request.wait_for_selector,
                            scenario=request.scenario,
                            network_policy=request.network_policy,
                            viewport=request.viewport,
                        )
                    )
                    status = _status_for_capture(capture)
                    failure_class: FailureClass = "none" if status == "pass" else "runtime_error"
                except RuntimeSmokeError as error:
                    capture = BrowserSmokeCapture(page_errors=[str(error)])
                    status = "best_effort"
                    failure_class = error.failure_class
                    limitations.append("Remote Browser Runner capture could not complete.")

            screenshot_base64 = None
            if screenshot_path.exists():
                screenshot_base64 = base64.b64encode(screenshot_path.read_bytes()).decode("ascii")
            else:
                limitations.append("Remote Browser Runner completed without producing a screenshot.")

        return BrowserRunResult(
            status=status,
            failure_class=failure_class,
            console_messages=capture.console_messages,
            console_errors=capture.console_errors,
            page_errors=capture.page_errors,
            failed_requests=capture.failed_requests,
            responses=capture.responses,
            assertion_failures=capture.assertion_failures,
            dom_summary=capture.dom_summary,
            screenshot_base64=screenshot_base64,
            limitations=[*limitations, "Browser execution ran in the remote browser-runner service boundary."],
            execution_boundary=self._execution_boundary(record),
        )

    def _execution_boundary(self, record: BrowserRunRecord, *, lease_recovered: bool | None = None) -> dict[str, Any]:
        return {
            "runnerKind": "remote_browser_runner",
            "enforcement": "remote_isolated",
            "serviceRole": "browser-runner",
            "remoteRunId": record.id,
            "auth": "bearer_hmac",
            "artifactExchange": "worker_request_archive_and_worker_registered_runtime_artifacts",
            "queueBackend": record.queue_backend,
            "maxWorkers": self.max_workers,
            "runAttempt": record.attempt,
            "maxAttempts": record.max_attempts,
            "leaseSeconds": self.lease_seconds,
            "retryBackoffSeconds": self.retry_backoff_seconds,
            "leaseRecovered": record.lease_recovered if lease_recovered is None else lease_recovered,
        }

    def _initialize_storage(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS browser_runs (
                    id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result_json TEXT,
                    error TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 1,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    next_run_at TEXT,
                    worker_id TEXT,
                    queue_backend TEXT NOT NULL DEFAULT 'sqlite',
                    lease_recovered INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS browser_runs_due_idx ON browser_runs(status, next_run_at, created_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS browser_runs_lease_idx ON browser_runs(status, lease_expires_at)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def _insert(self, record: BrowserRunRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO browser_runs (
                    id, request_json, status, created_at, updated_at, started_at, finished_at,
                    result_json, error, attempt, max_attempts, lease_owner, lease_expires_at,
                    next_run_at, worker_id, queue_backend, lease_recovered
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._row_values(record),
            )

    def _save(self, connection: sqlite3.Connection, record: BrowserRunRecord) -> None:
        connection.execute(
            """
            UPDATE browser_runs
            SET request_json = ?, status = ?, created_at = ?, updated_at = ?, started_at = ?,
                finished_at = ?, result_json = ?, error = ?, attempt = ?, max_attempts = ?,
                lease_owner = ?, lease_expires_at = ?, next_run_at = ?, worker_id = ?,
                queue_backend = ?, lease_recovered = ?
            WHERE id = ?
            """,
            (*self._row_values(record)[1:], record.id),
        )

    def _row_values(self, record: BrowserRunRecord) -> tuple[Any, ...]:
        return (
            record.id,
            record.request.model_dump_json(by_alias=True),
            record.status,
            record.created_at,
            record.updated_at,
            record.started_at,
            record.finished_at,
            record.result.model_dump_json(by_alias=True) if record.result else None,
            record.error,
            record.attempt,
            record.max_attempts,
            record.lease_owner,
            record.lease_expires_at,
            record.next_run_at,
            record.worker_id,
            record.queue_backend,
            1 if record.lease_recovered else 0,
        )

    def _record(self, run_id: str) -> BrowserRunRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM browser_runs WHERE id = ?", (run_id,)).fetchone()
        return self._record_from_row(row) if row else None

    def _record_from_row(self, row: sqlite3.Row) -> BrowserRunRecord:
        result_json = row["result_json"]
        return BrowserRunRecord(
            id=row["id"],
            request=BrowserRunRequest.model_validate(json.loads(row["request_json"])),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            result=BrowserRunResult.model_validate(json.loads(result_json)) if result_json else None,
            error=row["error"],
            attempt=int(row["attempt"] or 0),
            max_attempts=int(row["max_attempts"] or self.max_attempts),
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            next_run_at=row["next_run_at"],
            worker_id=row["worker_id"],
            queue_backend=row["queue_backend"] or self.queue_backend,
            lease_recovered=bool(row["lease_recovered"]),
        )

    def _update(self, run_id: str, **changes) -> None:
        record = self._record(run_id)
        if record is None:
            return
        updated = replace(record, updated_at=utc_now(), **changes)
        with self._connect() as connection:
            self._save(connection, updated)

    def _claim(self, run_id: str) -> BrowserRunRecord | None:
        started_at = utc_now()
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=max(1, self.lease_seconds))).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM browser_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None or row["status"] != "queued":
                connection.execute("ROLLBACK")
                return None
            record = self._record_from_row(row)
            updated = replace(
                record,
                status="running",
                attempt=record.attempt + 1,
                started_at=record.started_at or started_at,
                updated_at=started_at,
                lease_owner=self.worker_id,
                lease_expires_at=lease_expires_at,
                worker_id=self.worker_id,
            )
            self._save(connection, updated)
            connection.execute("COMMIT")
            return updated

    def _scheduler_loop(self) -> None:
        while not self._stop.wait(max(0.05, self.poll_seconds)):
            self.recover_expired_leases(schedule=False)
            self._schedule_due_runs()

    def _schedule_due_runs(self) -> None:
        now = utc_now()
        with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT id FROM browser_runs
                    WHERE status = 'queued' AND (next_run_at IS NULL OR next_run_at <= ?)
                    ORDER BY next_run_at, created_at, id
                    """,
                    (now,),
                ).fetchall()
            for row in rows:
                run_id = row["id"]
                if run_id in self._submitted:
                    continue
                self._submitted.add(run_id)
                self.executor.submit(self._execute, run_id)

    def _discard_submitted(self, run_id: str) -> None:
        with self._lock:
            self._submitted.discard(run_id)


def create_app(*, queue: BrowserRunnerQueue | None = None, adapter: BrowserSmokeAdapter | None = None) -> FastAPI:
    app = FastAPI(title="AI JS Unpack Browser Runner", version="0.1.0")
    app.state.browser_runner_queue = queue or BrowserRunnerQueue(browser_adapter=adapter)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "serviceRole": "browser-runner",
            "deploymentProfile": DEPLOYMENT_PROFILE.status,
        }

    @app.post("/browser-runs", response_model=BrowserRunSummary)
    def create_browser_run(
        request: BrowserRunRequest,
        access: AccessContext = Depends(require_access),
    ) -> BrowserRunSummary:
        require_worker_service(access)
        return app.state.browser_runner_queue.submit(request)

    @app.get("/browser-runs/{run_id}", response_model=BrowserRunSummary)
    def get_browser_run(
        run_id: str,
        access: AccessContext = Depends(require_access),
    ) -> BrowserRunSummary:
        require_worker_service(access)
        summary = app.state.browser_runner_queue.get(run_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="Browser run not found")
        return summary

    return app


def require_worker_service(access: AccessContext) -> None:
    if access.kind != "service" or not access.has_service_role(SERVICE_ROLE_WORKER):
        raise HTTPException(status_code=403, detail="Browser Runner requires a worker service credential")


def _extract_source_archive(source_archive: BrowserRunSourceArchive, target_root: Path) -> None:
    content = base64.b64decode(source_archive.content_base64.encode("ascii"))
    archive_path = target_root.parent / "source.zip"
    archive_path.write_bytes(content)
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            relative = _safe_source_relative_path(member.filename)
            if member.is_dir():
                continue
            output = target_root.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.read(member))


@contextmanager
def _entry_url(
    *,
    source_root: Path | None,
    source_archive: BrowserRunSourceArchive | None,
    fallback_url: str,
) -> Iterator[str]:
    if source_root is None or source_archive is None:
        yield fallback_url
        return
    handler = partial(_QuietStaticHandler, directory=str(source_root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        entry_path = _safe_source_relative_path(source_archive.entry_path).as_posix()
        yield f"http://{host}:{port}/{quote(entry_path)}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _status_for_capture(capture: BrowserSmokeCapture) -> RunStatus:
    if capture.console_errors or capture.page_errors or capture.failed_requests or capture.assertion_failures:
        return "fail"
    return "pass"


def _safe_source_relative_path(file_path: str) -> PurePosixPath:
    if not file_path or "\0" in file_path:
        raise ValueError(f"Unsafe source archive member path: {file_path}")
    normalized = file_path.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//") or _has_windows_drive_prefix(normalized):
        raise ValueError(f"Unsafe source archive member path: {file_path}")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe source archive member path: {file_path}")
    return PurePosixPath(*parts)


def _has_windows_drive_prefix(file_path: str) -> bool:
    return len(file_path) >= 2 and file_path[1] == ":" and file_path[0].isalpha()


def classify_browser_runner_error(error: Exception) -> FailureClass:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, (ValueError, zipfile.BadZipFile)):
        return "invalid_input"
    if isinstance(error, PermissionError):
        return "sandbox_denied"
    return "runtime_error"


def parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_optional_timestamp(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


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
    return max(0.0, parsed)


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return


app = create_app()
