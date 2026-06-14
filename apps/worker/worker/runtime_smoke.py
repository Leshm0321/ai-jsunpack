from __future__ import annotations

import json
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Iterator, Protocol
from urllib.parse import quote
from uuid import uuid4

from apps.api.app.models import ArtifactRecord, FailureClass, RunStatus, RuntimeTarget, RuntimeValidationRun


@dataclass(frozen=True)
class BrowserSmokeRequest:
    entry_url: str
    screenshot_path: Path
    timeout_ms: int
    wait_for_selector: str | None = None


@dataclass(frozen=True)
class BrowserSmokeCapture:
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)


class BrowserSmokeAdapter(Protocol):
    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        ...


class RuntimeSmokeError(Exception):
    def __init__(self, message: str, failure_class: FailureClass = "runtime_error") -> None:
        super().__init__(message)
        self.failure_class = failure_class


class PlaywrightBrowserAdapter:
    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeSmokeError("Playwright is not installed.", "runtime_error") from error

        console_errors: list[str] = []
        page_errors: list[str] = []
        failed_requests: list[str] = []
        responses: list[str] = []

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1365, "height": 768})
                    page.on("console", lambda message: self._capture_console(message, console_errors))
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.on("requestfailed", lambda failed: self._capture_failed_request(failed, failed_requests))
                    page.on("response", lambda response: self._capture_failed_response(response, failed_requests, responses))

                    page.goto(request.entry_url, wait_until="load", timeout=request.timeout_ms)
                    if request.wait_for_selector:
                        page.wait_for_selector(request.wait_for_selector, timeout=request.timeout_ms)
                    page.screenshot(path=str(request.screenshot_path), full_page=True)
                finally:
                    browser.close()
        except Exception as error:
            failure_class: FailureClass = "timeout" if "Timeout" in type(error).__name__ else "runtime_error"
            raise RuntimeSmokeError(str(error), failure_class) from error

        return BrowserSmokeCapture(
            console_errors=console_errors,
            page_errors=page_errors,
            failed_requests=failed_requests,
            responses=responses,
        )

    def _capture_console(self, message, console_errors: list[str]) -> None:
        if getattr(message, "type", "") == "error":
            console_errors.append(message.text)

    def _capture_failed_request(self, request, failed_requests: list[str]) -> None:
        failure = getattr(request, "failure", None)
        detail = failure() if callable(failure) else failure
        failed_requests.append(f"{request.method} {request.url} {detail or 'request failed'}")

    def _capture_failed_response(self, response, failed_requests: list[str], responses: list[str]) -> None:
        responses.append(f"{response.status} {response.url}")
        if response.status >= 400:
            failed_requests.append(f"{response.status} {response.url}")


@dataclass(frozen=True)
class RuntimeSmokeResult:
    validation: RuntimeValidationRun
    report_artifact: ArtifactRecord
    trace_artifact: ArtifactRecord
    screenshot_artifact: ArtifactRecord | None
    message: str


@dataclass(frozen=True)
class RuntimeEntry:
    entry_url: str | None
    serve_root: Path | None
    relative_entry: str | None
    limitations: list[str]


class RuntimeSmokeRunner:
    def __init__(self, browser_adapter: BrowserSmokeAdapter | None = None, timeout_ms: int = 10_000) -> None:
        self.browser_adapter = browser_adapter or PlaywrightBrowserAdapter()
        self.timeout_ms = timeout_ms

    def run(
        self,
        *,
        job_id: str,
        store,
        input_path: Path | str | None = None,
        entry_url: str | None = None,
        target: RuntimeTarget = "reconstructed",
        wait_for_selector: str | None = None,
        parent_artifact_ids: list[str] | None = None,
    ) -> RuntimeSmokeResult:
        parents = parent_artifact_ids or []
        started_at = time.perf_counter()
        store.update_status(job_id, "runtime_smoke")
        entry = self._resolve_entry(input_path=input_path, entry_url=entry_url)

        if entry.entry_url is None and entry.serve_root is None:
            return self._persist_result(
                job_id=job_id,
                store=store,
                target=target,
                entry_url="about:blank",
                status="best_effort",
                console_errors=[],
                page_errors=[],
                failed_requests=[],
                responses=[],
                screenshot_bytes=None,
                limitations=entry.limitations,
                failure_class="invalid_input",
                parent_artifact_ids=parents,
                duration_ms=self._duration_ms(started_at),
                message="Runtime smoke skipped because no HTML entry was found.",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            screenshot_path = Path(temp_dir) / "runtime-smoke.png"
            resolved_url = entry.entry_url or "about:blank"
            try:
                with self._entry_url(entry) as resolved_url:
                    capture = self.browser_adapter.capture(
                        BrowserSmokeRequest(
                            entry_url=resolved_url,
                            screenshot_path=screenshot_path,
                            timeout_ms=self.timeout_ms,
                            wait_for_selector=wait_for_selector,
                        )
                    )
                status = self._status_for_capture(capture)
                screenshot_bytes = screenshot_path.read_bytes() if screenshot_path.exists() else None
                limitations = list(entry.limitations)
                if screenshot_bytes is None:
                    limitations.append("Playwright completed without producing a screenshot.")
                return self._persist_result(
                    job_id=job_id,
                    store=store,
                    target=target,
                    entry_url=resolved_url,
                    status=status,
                    console_errors=capture.console_errors,
                    page_errors=capture.page_errors,
                    failed_requests=capture.failed_requests,
                    responses=capture.responses,
                    screenshot_bytes=screenshot_bytes,
                    limitations=limitations,
                    failure_class="none" if status == "pass" else "runtime_error",
                    parent_artifact_ids=parents,
                    duration_ms=self._duration_ms(started_at),
                    message=f"Runtime smoke completed with status {status}.",
                )
            except RuntimeSmokeError as error:
                return self._persist_result(
                    job_id=job_id,
                    store=store,
                    target=target,
                    entry_url=resolved_url,
                    status="best_effort",
                    console_errors=[],
                    page_errors=[str(error)],
                    failed_requests=[],
                    responses=[],
                    screenshot_bytes=None,
                    limitations=[*entry.limitations, "Playwright runtime smoke could not complete."],
                    failure_class=error.failure_class,
                    parent_artifact_ids=parents,
                    duration_ms=self._duration_ms(started_at),
                    message=f"Runtime smoke produced best-effort evidence: {error}",
                )

    def _resolve_entry(self, *, input_path: Path | str | None, entry_url: str | None) -> RuntimeEntry:
        if entry_url:
            return RuntimeEntry(entry_url=entry_url, serve_root=None, relative_entry=None, limitations=[])

        if input_path is None:
            return RuntimeEntry(
                entry_url=None,
                serve_root=None,
                relative_entry=None,
                limitations=["No input path or explicit entry URL was provided."],
            )

        path = Path(input_path)
        if path.is_file() and path.suffix.lower() == ".html":
            return RuntimeEntry(entry_url=None, serve_root=path.parent, relative_entry=path.name, limitations=[])

        if path.is_dir():
            index = path / "index.html"
            if index.exists():
                return RuntimeEntry(entry_url=None, serve_root=path, relative_entry="index.html", limitations=[])
            html_entries = sorted(candidate for candidate in path.rglob("*.html") if candidate.is_file())
            if html_entries:
                relative_entry = html_entries[0].relative_to(path).as_posix()
                return RuntimeEntry(entry_url=None, serve_root=path, relative_entry=relative_entry, limitations=[])

        return RuntimeEntry(
            entry_url=None,
            serve_root=None,
            relative_entry=None,
            limitations=[f"No HTML entry was found under {path}."],
        )

    @contextmanager
    def _entry_url(self, entry: RuntimeEntry) -> Iterator[str]:
        if entry.entry_url:
            yield entry.entry_url
            return

        if entry.serve_root is None or entry.relative_entry is None:
            raise RuntimeSmokeError("Runtime entry is unavailable.", "invalid_input")

        with self._static_server(entry.serve_root) as base_url:
            yield f"{base_url}/{quote(entry.relative_entry)}"

    @contextmanager
    def _static_server(self, root: Path) -> Iterator[str]:
        handler = partial(_QuietStaticHandler, directory=str(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            yield f"http://{host}:{port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def _persist_result(
        self,
        *,
        job_id: str,
        store,
        target: RuntimeTarget,
        entry_url: str,
        status: RunStatus,
        console_errors: list[str],
        page_errors: list[str],
        failed_requests: list[str],
        responses: list[str],
        screenshot_bytes: bytes | None,
        limitations: list[str],
        failure_class: FailureClass,
        parent_artifact_ids: list[str],
        duration_ms: int,
        message: str,
    ) -> RuntimeSmokeResult:
        screenshot_artifact = None
        if screenshot_bytes is not None:
            screenshot_artifact = store.write_artifact(
                job_id,
                kind="runtime_screenshot",
                stage="runtime_smoke",
                filename="runtime-smoke.png",
                content=screenshot_bytes,
                content_type="image/png",
                producer="worker.runtime_smoke",
                parent_artifact_ids=parent_artifact_ids,
            )

        trace_payload = {
            "kind": "runtime_trace",
            "jobId": job_id,
            "target": target,
            "entryUrl": entry_url,
            "status": status,
            "failureClass": failure_class,
            "durationMs": duration_ms,
            "consoleErrors": console_errors,
            "pageErrors": page_errors,
            "failedRequests": failed_requests,
            "responses": responses,
            "limitations": limitations,
        }
        trace_artifact = store.write_artifact(
            job_id,
            kind="runtime_trace",
            stage="runtime_smoke",
            filename="runtime-trace.json",
            content=self._json_bytes(trace_payload),
            content_type="application/json",
            producer="worker.runtime_smoke",
            parent_artifact_ids=parent_artifact_ids,
        )

        validation = RuntimeValidationRun(
            id=f"runtime_{uuid4().hex[:12]}",
            job_id=job_id,
            attempt=0,
            target=target,
            entry_url=entry_url,
            status=status,
            console_errors=console_errors,
            page_errors=page_errors,
            failed_requests=failed_requests,
            screenshot_artifact_ids=[screenshot_artifact.id] if screenshot_artifact else [],
            trace_artifact_id=trace_artifact.id,
            comparison_artifact_id=None,
        )
        report_parent_ids = [*parent_artifact_ids, trace_artifact.id]
        if screenshot_artifact:
            report_parent_ids.append(screenshot_artifact.id)
        report_artifact = store.write_artifact(
            job_id,
            kind="runtime_validation",
            stage="runtime_smoke",
            filename="runtime-validation.json",
            content=validation.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.runtime_smoke",
            parent_artifact_ids=report_parent_ids,
        )
        return RuntimeSmokeResult(
            validation=validation,
            report_artifact=report_artifact,
            trace_artifact=trace_artifact,
            screenshot_artifact=screenshot_artifact,
            message=message,
        )

    def _status_for_capture(self, capture: BrowserSmokeCapture) -> RunStatus:
        if capture.console_errors or capture.page_errors or capture.failed_requests:
            return "fail"
        return "pass"

    def _duration_ms(self, started_at: float) -> int:
        return int((time.perf_counter() - started_at) * 1000)

    def _json_bytes(self, payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return
