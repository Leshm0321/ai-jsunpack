from __future__ import annotations

import json
import hashlib
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Iterator, Protocol
from urllib.parse import quote, urlparse
from uuid import uuid4

from apps.api.app.models import (
    ArtifactRecord,
    FailureClass,
    NetworkPolicy,
    RunStatus,
    RuntimeCaptureSummary,
    RuntimeComparisonReport,
    RuntimeDifferenceSet,
    RuntimeScenario,
    RuntimeTarget,
    RuntimeValidationRun,
    RuntimeWaitFor,
)
from packages.sandbox import LocalSandboxRunner


@dataclass(frozen=True)
class BrowserSmokeRequest:
    entry_url: str
    screenshot_path: Path
    timeout_ms: int
    wait_for_selector: str | None = None
    scenario: RuntimeScenario | None = None
    network_policy: NetworkPolicy = "deny"


@dataclass(frozen=True)
class BrowserSmokeCapture:
    console_messages: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)
    assertion_failures: list[str] = field(default_factory=list)
    dom_summary: dict[str, Any] = field(default_factory=dict)


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

        console_messages: list[str] = []
        console_errors: list[str] = []
        page_errors: list[str] = []
        failed_requests: list[str] = []
        responses: list[str] = []
        assertion_failures: list[str] = []
        dom_summary: dict[str, Any] = {}

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1365, "height": 768})
                    if request.network_policy == "deny":
                        page.route("**/*", lambda route: self._route_network(route, failed_requests))
                    page.on("console", lambda message: self._capture_console(message, console_messages, console_errors))
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.on("requestfailed", lambda failed: self._capture_failed_request(failed, failed_requests))
                    page.on("response", lambda response: self._capture_failed_response(response, failed_requests, responses))

                    page.goto(request.entry_url, wait_until="load", timeout=request.timeout_ms)
                    self._apply_waits(page, request.scenario, request.timeout_ms)
                    if request.wait_for_selector:
                        page.wait_for_selector(request.wait_for_selector, timeout=request.timeout_ms)
                    self._apply_interactions(page, request.scenario, request.timeout_ms)
                    assertion_failures = self._assertions(page, request.scenario)
                    dom_summary = self._dom_summary(page)
                    page.screenshot(path=str(request.screenshot_path), full_page=True)
                finally:
                    browser.close()
        except Exception as error:
            failure_class: FailureClass = "timeout" if "Timeout" in type(error).__name__ else "runtime_error"
            raise RuntimeSmokeError(str(error), failure_class) from error

        return BrowserSmokeCapture(
            console_messages=console_messages,
            console_errors=console_errors,
            page_errors=page_errors,
            failed_requests=failed_requests,
            responses=responses,
            assertion_failures=assertion_failures,
            dom_summary=dom_summary,
        )

    def _capture_console(self, message, console_messages: list[str], console_errors: list[str]) -> None:
        message_type = getattr(message, "type", "")
        text = getattr(message, "text", "")
        console_messages.append(f"{message_type}: {text}")
        if message_type == "error":
            console_errors.append(text)

    def _route_network(self, route, failed_requests: list[str]) -> None:
        request = route.request
        if self._network_allowed(request.url):
            route.continue_()
            return
        failed_requests.append(f"network_policy_denied {request.method} {request.url}")
        route.abort()

    def _network_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme in {"about", "blob", "data"}:
            return True
        if parsed.scheme in {"http", "https"}:
            return parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        return False

    def _capture_failed_request(self, request, failed_requests: list[str]) -> None:
        failure = getattr(request, "failure", None)
        detail = failure() if callable(failure) else failure
        failed_requests.append(f"{request.method} {request.url} {detail or 'request failed'}")

    def _capture_failed_response(self, response, failed_requests: list[str], responses: list[str]) -> None:
        responses.append(f"{response.status} {response.url}")
        if response.status >= 400:
            failed_requests.append(f"{response.status} {response.url}")

    def _apply_waits(self, page, scenario: RuntimeScenario | None, default_timeout_ms: int) -> None:
        if scenario is None:
            return
        for wait in scenario.wait_for:
            timeout_ms = wait.timeout_ms or scenario.timeout_ms or default_timeout_ms
            if wait.kind == "load_state":
                page.wait_for_load_state(wait.state or "load", timeout=timeout_ms)
            elif wait.kind == "selector" and wait.selector:
                page.wait_for_selector(wait.selector, timeout=timeout_ms)
            elif wait.kind == "timeout":
                page.wait_for_timeout(timeout_ms)

    def _apply_interactions(self, page, scenario: RuntimeScenario | None, default_timeout_ms: int) -> None:
        if scenario is None:
            return
        for interaction in scenario.interactions:
            timeout_ms = interaction.timeout_ms or scenario.timeout_ms or default_timeout_ms
            if interaction.action == "click" and interaction.selector:
                page.click(interaction.selector, timeout=timeout_ms)
            elif interaction.action == "fill" and interaction.selector:
                page.fill(interaction.selector, interaction.value or "", timeout=timeout_ms)
            elif interaction.action == "press":
                key = interaction.key or interaction.value
                if not key:
                    raise RuntimeSmokeError("Runtime scenario press interaction requires key or value.", "invalid_input")
                if interaction.selector:
                    page.press(interaction.selector, key, timeout=timeout_ms)
                else:
                    page.keyboard.press(key)
            elif interaction.action == "wait":
                if interaction.selector:
                    page.wait_for_selector(interaction.selector, timeout=timeout_ms)
                else:
                    page.wait_for_timeout(timeout_ms)

    def _assertions(self, page, scenario: RuntimeScenario | None) -> list[str]:
        if scenario is None:
            return []
        failures: list[str] = []
        for assertion in scenario.assertions:
            if assertion.kind == "selector_visible":
                selector = assertion.selector
                if not selector:
                    failures.append("selector_visible assertion requires selector.")
                elif not page.locator(selector).first.is_visible():
                    failures.append(f"Expected selector to be visible: {selector}")
            elif assertion.kind == "text_contains":
                text = assertion.text or assertion.value
                if not text:
                    failures.append("text_contains assertion requires text or value.")
                elif text not in page.locator("body").inner_text(timeout=1000):
                    failures.append(f"Expected body text to contain: {text}")
            elif assertion.kind == "url_contains":
                value = assertion.value or assertion.text
                if not value:
                    failures.append("url_contains assertion requires value or text.")
                elif value not in page.url:
                    failures.append(f"Expected URL to contain: {value}")
        return failures

    def _dom_summary(self, page) -> dict[str, Any]:
        return page.evaluate(
            """() => {
              const bodyText = (document.body && document.body.innerText || "").replace(/\\s+/g, " ").trim();
              const headings = Array.from(document.querySelectorAll("h1,h2,h3"))
                .slice(0, 12)
                .map((node) => ({ tag: node.tagName.toLowerCase(), text: (node.textContent || "").trim().slice(0, 120) }));
              return {
                title: document.title,
                url: location.href,
                nodeCount: document.querySelectorAll("*").length,
                textLength: bodyText.length,
                textSample: bodyText.slice(0, 240),
                headings,
                forms: document.forms.length,
                links: document.querySelectorAll("a[href]").length,
                images: document.images.length,
                scripts: document.scripts.length,
                stylesheets: document.styleSheets.length
              };
            }"""
        )


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
    def __init__(
        self,
        browser_adapter: BrowserSmokeAdapter | None = None,
        timeout_ms: int = 10_000,
        sandbox_runner: LocalSandboxRunner | None = None,
    ) -> None:
        self.browser_adapter = browser_adapter or PlaywrightBrowserAdapter()
        self.timeout_ms = timeout_ms
        self.sandbox_runner = sandbox_runner or LocalSandboxRunner()

    def run(
        self,
        *,
        job_id: str,
        store,
        input_path: Path | str | None = None,
        entry_url: str | None = None,
        target: RuntimeTarget = "reconstructed",
        wait_for_selector: str | None = None,
        scenario: RuntimeScenario | None = None,
        network_policy: NetworkPolicy = "deny",
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
                    console_messages=[],
                    assertion_failures=[],
                    dom_summary={},
                    screenshot_bytes=None,
                    limitations=entry.limitations,
                    failure_class="invalid_input",
                    scenario=scenario,
                    network_policy=network_policy,
                    parent_artifact_ids=parents,
                    duration_ms=self._duration_ms(started_at),
                    message="Runtime smoke skipped because no HTML entry was found.",
            )

        with self.sandbox_runner.attempt_workspace() as temp_dir:
            screenshot_path = temp_dir / "runtime-smoke.png"
            resolved_url = entry.entry_url or "about:blank"
            try:
                with self._entry_url(entry) as resolved_url:
                    capture = self.browser_adapter.capture(
                        BrowserSmokeRequest(
                            entry_url=resolved_url,
                            screenshot_path=screenshot_path,
                            timeout_ms=self.timeout_ms,
                            wait_for_selector=wait_for_selector,
                            scenario=scenario,
                            network_policy=network_policy,
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
                    console_messages=capture.console_messages,
                    assertion_failures=capture.assertion_failures,
                    dom_summary=capture.dom_summary,
                    screenshot_bytes=screenshot_bytes,
                    limitations=limitations,
                    failure_class="none" if status == "pass" else "runtime_error",
                    scenario=scenario,
                    network_policy=network_policy,
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
                    console_messages=[],
                    assertion_failures=[],
                    dom_summary={},
                    screenshot_bytes=None,
                    limitations=[*entry.limitations, "Playwright runtime smoke could not complete."],
                    failure_class=error.failure_class,
                    scenario=scenario,
                    network_policy=network_policy,
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
        console_messages: list[str],
        assertion_failures: list[str],
        dom_summary: dict[str, Any],
        screenshot_bytes: bytes | None,
        limitations: list[str],
        failure_class: FailureClass,
        scenario: RuntimeScenario | None,
        network_policy: NetworkPolicy,
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
            "scenarioId": scenario.id if scenario else None,
            "networkPolicy": network_policy,
            "status": status,
            "failureClass": failure_class,
            "durationMs": duration_ms,
            "consoleMessages": console_messages,
            "consoleErrors": console_errors,
            "pageErrors": page_errors,
            "failedRequests": failed_requests,
            "responses": responses,
            "assertionFailures": assertion_failures,
            "domSummary": dom_summary,
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
        if capture.console_errors or capture.page_errors or capture.failed_requests or capture.assertion_failures:
            return "fail"
        return "pass"

    def _duration_ms(self, started_at: float) -> int:
        return int((time.perf_counter() - started_at) * 1000)

    def _json_bytes(self, payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


@dataclass(frozen=True)
class RuntimeCompareResult:
    validation: RuntimeValidationRun
    scenario_artifact: ArtifactRecord
    comparison_artifact: ArtifactRecord
    trace_artifact: ArtifactRecord
    screenshot_artifacts: list[ArtifactRecord]
    report_artifact: ArtifactRecord
    message: str

    @property
    def artifact_ids(self) -> list[str]:
        return [
            self.scenario_artifact.id,
            self.comparison_artifact.id,
            self.trace_artifact.id,
            self.report_artifact.id,
            *[artifact.id for artifact in self.screenshot_artifacts],
        ]


class RuntimeCompareRunner(RuntimeSmokeRunner):
    def run_compare(
        self,
        *,
        job_id: str,
        store,
        original_input_path: Path | str | None,
        reconstructed_input_path: Path | str | None,
        scenario_config: dict[str, Any] | None = None,
        parent_artifact_ids: list[str] | None = None,
    ) -> RuntimeCompareResult:
        parents = parent_artifact_ids or []
        store.update_status(job_id, "runtime_compare")
        scenario = self._scenario_from_config(job_id=job_id, config=scenario_config)
        scenario_artifact = store.write_artifact(
            job_id,
            kind="runtime_scenario",
            stage="runtime_compare",
            filename="runtime-scenario.json",
            content=scenario.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.runtime_compare",
            parent_artifact_ids=parents,
        )

        with self.sandbox_runner.attempt_workspace() as temp_dir:
            original = self._capture_target(
                job_id=job_id,
                store=store,
                target="original",
                input_path=original_input_path,
                scenario=scenario,
                temp_dir=temp_dir,
                parent_artifact_ids=[*parents, scenario_artifact.id],
            )
            reconstructed = self._capture_target(
                job_id=job_id,
                store=store,
                target="reconstructed",
                input_path=reconstructed_input_path,
                scenario=scenario,
                temp_dir=temp_dir,
                parent_artifact_ids=[*parents, scenario_artifact.id],
            )

        screenshot_artifacts = [artifact for artifact in (original["screenshot"], reconstructed["screenshot"]) if artifact]
        trace_payload = {
            "kind": "runtime_trace",
            "jobId": job_id,
            "target": "comparison",
            "scenarioArtifactId": scenario_artifact.id,
            "original": original["summary"].model_dump(by_alias=True),
            "reconstructed": reconstructed["summary"].model_dump(by_alias=True),
        }
        trace_artifact = store.write_artifact(
            job_id,
            kind="runtime_trace",
            stage="runtime_compare",
            filename="runtime-compare-trace.json",
            content=self._json_bytes(trace_payload),
            content_type="application/json",
            producer="worker.runtime_compare",
            parent_artifact_ids=[*parents, scenario_artifact.id, *[artifact.id for artifact in screenshot_artifacts]],
        )

        differences = self._compare_captures(
            original=original["summary"],
            reconstructed=reconstructed["summary"],
            original_screenshot_hash=original["screenshot_hash"],
            reconstructed_screenshot_hash=reconstructed["screenshot_hash"],
        )
        status = self._comparison_status(original["summary"], reconstructed["summary"])
        comparison = RuntimeComparisonReport(
            id=f"runtime_comparison_{uuid4().hex[:12]}",
            job_id=job_id,
            attempt=0,
            status=status,
            scenario_artifact_id=scenario_artifact.id,
            original=original["summary"],
            reconstructed=reconstructed["summary"],
            differences=differences,
            screenshot_artifact_ids=[artifact.id for artifact in screenshot_artifacts],
            trace_artifact_ids=[trace_artifact.id],
            limitations=[*original["summary"].limitations, *reconstructed["summary"].limitations],
        )
        comparison_artifact = store.write_artifact(
            job_id,
            kind="runtime_comparison",
            stage="runtime_compare",
            filename="runtime-comparison.json",
            content=comparison.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.runtime_compare",
            parent_artifact_ids=[*parents, scenario_artifact.id, trace_artifact.id, *[artifact.id for artifact in screenshot_artifacts]],
        )

        validation = RuntimeValidationRun(
            id=f"runtime_{uuid4().hex[:12]}",
            job_id=job_id,
            attempt=0,
            target="reconstructed",
            entry_url=reconstructed["summary"].entry_url,
            status=status,
            console_errors=reconstructed["summary"].console_errors,
            page_errors=[
                *original["summary"].page_errors,
                *reconstructed["summary"].page_errors,
                *original["summary"].assertion_failures,
                *reconstructed["summary"].assertion_failures,
            ],
            failed_requests=reconstructed["summary"].failed_requests,
            screenshot_artifact_ids=[artifact.id for artifact in screenshot_artifacts],
            trace_artifact_id=trace_artifact.id,
            comparison_artifact_id=comparison_artifact.id,
        )
        report_artifact = store.write_artifact(
            job_id,
            kind="runtime_validation",
            stage="runtime_compare",
            filename="runtime-compare-validation.json",
            content=validation.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.runtime_compare",
            parent_artifact_ids=[
                *parents,
                scenario_artifact.id,
                trace_artifact.id,
                comparison_artifact.id,
                *[artifact.id for artifact in screenshot_artifacts],
            ],
        )
        return RuntimeCompareResult(
            validation=validation,
            scenario_artifact=scenario_artifact,
            comparison_artifact=comparison_artifact,
            trace_artifact=trace_artifact,
            screenshot_artifacts=screenshot_artifacts,
            report_artifact=report_artifact,
            message=f"Runtime compare completed with status {status}.",
        )

    def _scenario_from_config(self, *, job_id: str, config: dict[str, Any] | None) -> RuntimeScenario:
        payload = dict(config or {})
        wait_for = payload.get("waitFor", payload.get("wait_for"))
        if wait_for is None:
            wait_for = [{"kind": "load_state", "state": "load", "timeoutMs": self.timeout_ms}]
        return RuntimeScenario(
            id=str(payload.get("id") or f"runtime_scenario_{uuid4().hex[:12]}"),
            job_id=job_id,
            name=str(payload.get("name") or "default-load"),
            entry_url=payload.get("entryUrl", payload.get("entry_url")),
            wait_for=[RuntimeWaitFor.model_validate(item) for item in wait_for],
            interactions=list(payload.get("interactions") or []),
            assertions=list(payload.get("assertions") or []),
            network_policy=payload.get("networkPolicy", payload.get("network_policy", "deny")),
            timeout_ms=int(payload.get("timeoutMs", payload.get("timeout_ms", self.timeout_ms))),
        )

    def _capture_target(
        self,
        *,
        job_id: str,
        store,
        target: RuntimeTarget,
        input_path: Path | str | None,
        scenario: RuntimeScenario,
        temp_dir: Path,
        parent_artifact_ids: list[str],
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        entry = self._resolve_scenario_entry(input_path=input_path, scenario=scenario)
        screenshot_path = temp_dir / f"runtime-compare-{target}.png"
        screenshot_bytes = None
        capture = BrowserSmokeCapture()
        failure_class: FailureClass = "none"
        limitations = list(entry.limitations)
        resolved_url = entry.entry_url or "about:blank"

        if entry.entry_url is None and entry.serve_root is None:
            status: RunStatus = "best_effort"
            failure_class = "invalid_input"
        else:
            try:
                with self._entry_url(entry) as resolved_url:
                    capture = self.browser_adapter.capture(
                        BrowserSmokeRequest(
                            entry_url=resolved_url,
                            screenshot_path=screenshot_path,
                            timeout_ms=scenario.timeout_ms,
                            scenario=scenario,
                            network_policy=scenario.network_policy,
                        )
                    )
                status = self._status_for_capture(capture)
                failure_class = "none" if status == "pass" else "runtime_error"
                screenshot_bytes = screenshot_path.read_bytes() if screenshot_path.exists() else None
                if screenshot_bytes is None:
                    limitations.append("Playwright completed without producing a comparison screenshot.")
            except RuntimeSmokeError as error:
                status = "best_effort"
                failure_class = error.failure_class
                capture = BrowserSmokeCapture(page_errors=[str(error)])
                limitations.append("Playwright runtime compare target capture could not complete.")

        screenshot_artifact = None
        screenshot_hash = None
        if screenshot_bytes is not None:
            screenshot_hash = hashlib.sha256(screenshot_bytes).hexdigest()
            screenshot_artifact = store.write_artifact(
                job_id,
                kind="runtime_screenshot",
                stage="runtime_compare",
                filename=f"runtime-compare-{target}.png",
                content=screenshot_bytes,
                content_type="image/png",
                producer="worker.runtime_compare",
                parent_artifact_ids=parent_artifact_ids,
            )

        summary = RuntimeCaptureSummary(
            target=target,
            entry_url=resolved_url,
            status=status,
            failure_class=failure_class,
            console_messages=capture.console_messages,
            console_errors=capture.console_errors,
            page_errors=capture.page_errors,
            failed_requests=capture.failed_requests,
            responses=capture.responses,
            assertion_failures=capture.assertion_failures,
            dom_summary=capture.dom_summary,
            screenshot_artifact_id=screenshot_artifact.id if screenshot_artifact else None,
            duration_ms=self._duration_ms(started_at),
            limitations=limitations,
        )
        return {"summary": summary, "screenshot": screenshot_artifact, "screenshot_hash": screenshot_hash}

    def _resolve_scenario_entry(self, *, input_path: Path | str | None, scenario: RuntimeScenario) -> RuntimeEntry:
        if scenario.entry_url:
            parsed = urlparse(scenario.entry_url)
            if parsed.scheme in {"http", "https", "about", "data", "file"}:
                return RuntimeEntry(entry_url=scenario.entry_url, serve_root=None, relative_entry=None, limitations=[])
        if scenario.entry_url and input_path is not None:
            relative_entry = Path(scenario.entry_url)
            if relative_entry.is_absolute() or ".." in relative_entry.parts:
                return RuntimeEntry(
                    entry_url=None,
                    serve_root=None,
                    relative_entry=None,
                    limitations=[f"Runtime scenario entryUrl is not a safe relative path: {scenario.entry_url}."],
                )
            root = Path(input_path)
            if root.is_file():
                root = root.parent
            return RuntimeEntry(
                entry_url=None,
                serve_root=root,
                relative_entry=relative_entry.as_posix(),
                limitations=[] if (root / relative_entry).exists() else [f"Runtime scenario entry was not found: {scenario.entry_url}."],
            )
        return self._resolve_entry(input_path=input_path, entry_url=None)

    def _compare_captures(
        self,
        *,
        original: RuntimeCaptureSummary,
        reconstructed: RuntimeCaptureSummary,
        original_screenshot_hash: str | None,
        reconstructed_screenshot_hash: str | None,
    ) -> RuntimeDifferenceSet:
        original_requests = set(original.responses + original.failed_requests)
        reconstructed_requests = set(reconstructed.responses + reconstructed.failed_requests)
        original_console = set(original.console_messages + original.console_errors)
        reconstructed_console = set(reconstructed.console_messages + reconstructed.console_errors)
        changed_dom_fields = sorted(
            key
            for key in set(original.dom_summary) | set(reconstructed.dom_summary)
            if original.dom_summary.get(key) != reconstructed.dom_summary.get(key)
        )
        screenshot_changed = (
            None
            if original_screenshot_hash is None or reconstructed_screenshot_hash is None
            else original_screenshot_hash != reconstructed_screenshot_hash
        )
        return RuntimeDifferenceSet(
            screenshot_changed=screenshot_changed,
            dom_changed=bool(changed_dom_fields),
            network_changed=original_requests != reconstructed_requests,
            console_changed=original_console != reconstructed_console,
            original_only_requests=sorted(original_requests - reconstructed_requests),
            reconstructed_only_requests=sorted(reconstructed_requests - original_requests),
            original_only_console=sorted(original_console - reconstructed_console),
            reconstructed_only_console=sorted(reconstructed_console - original_console),
            changed_dom_fields=changed_dom_fields,
        )

    def _comparison_status(self, original: RuntimeCaptureSummary, reconstructed: RuntimeCaptureSummary) -> RunStatus:
        statuses = {original.status, reconstructed.status}
        if "fail" in statuses:
            return "fail"
        if "best_effort" in statuses:
            return "best_effort"
        if "retry" in statuses:
            return "retry"
        return "pass"


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return
