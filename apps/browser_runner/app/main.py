from __future__ import annotations

import base64
import os
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Iterator
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
DEFAULT_BROWSER_RUNNER_WORKERS = 2

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
        )


class BrowserRunnerQueue:
    def __init__(
        self,
        *,
        browser_adapter: BrowserSmokeAdapter | None = None,
        max_workers: int | None = None,
        workdir: Path | str | None = None,
    ) -> None:
        self.browser_adapter = browser_adapter or PlaywrightBrowserAdapter()
        self.executor = ThreadPoolExecutor(max_workers=max_workers or self._configured_workers())
        self.workdir = Path(workdir or os.getenv(BROWSER_RUNNER_WORKDIR_ENV, tempfile.gettempdir()))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._runs: dict[str, BrowserRunRecord] = {}

    def submit(self, request: BrowserRunRequest) -> BrowserRunSummary:
        run_id = f"browser_run_{uuid4().hex[:12]}"
        now = utc_now()
        record = BrowserRunRecord(
            id=run_id,
            request=request,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._runs[run_id] = record
        self.executor.submit(self._execute, run_id)
        return record.summary()

    def get(self, run_id: str) -> BrowserRunSummary | None:
        with self._lock:
            record = self._runs.get(run_id)
        return record.summary() if record else None

    def _execute(self, run_id: str) -> None:
        self._update(run_id, status="running", started_at=utc_now())
        record = self._record(run_id)
        if record is None:
            return
        try:
            result = self._capture(record.id, record.request)
            self._update(
                run_id,
                status=result.status if result.status in {"pass", "fail", "best_effort"} else "best_effort",
                result=result,
                finished_at=utc_now(),
            )
        except Exception as error:
            result = BrowserRunResult(
                status="best_effort",
                failure_class="runtime_error",
                page_errors=[str(error)],
                limitations=["Browser Runner queue execution failed before capture evidence could be completed."],
                execution_boundary=self._execution_boundary(run_id),
            )
            self._update(run_id, status="best_effort", result=result, error=str(error), finished_at=utc_now())

    def _capture(self, run_id: str, request: BrowserRunRequest) -> BrowserRunResult:
        limitations: list[str] = []
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
            execution_boundary=self._execution_boundary(run_id),
        )

    def _execution_boundary(self, run_id: str) -> dict[str, str]:
        return {
            "runnerKind": "remote_browser_runner",
            "enforcement": "remote_isolated",
            "serviceRole": "browser-runner",
            "remoteRunId": run_id,
            "auth": "bearer_hmac",
            "artifactExchange": "worker_request_archive_and_worker_registered_runtime_artifacts",
        }

    def _record(self, run_id: str) -> BrowserRunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def _update(self, run_id: str, **changes) -> None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            self._runs[run_id] = replace(record, updated_at=utc_now(), **changes)

    def _configured_workers(self) -> int:
        raw_value = os.getenv(BROWSER_RUNNER_WORKERS_ENV)
        if raw_value is None:
            return DEFAULT_BROWSER_RUNNER_WORKERS
        try:
            return max(1, int(raw_value))
        except ValueError:
            return DEFAULT_BROWSER_RUNNER_WORKERS


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


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return


app = create_app()
