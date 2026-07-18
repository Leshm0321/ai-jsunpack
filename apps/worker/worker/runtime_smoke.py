from __future__ import annotations

import binascii
import base64
import io
import json
import hashlib
import os
import shutil
import struct
import time
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Iterator, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote, urlparse
from uuid import uuid4

from apps.api.app.models import (
    ArtifactRecord,
    BrowserRunRequest,
    BrowserRunSourceArchive,
    BrowserRunSummary,
    EvidenceRef,
    FailureClass,
    NetworkPolicy,
    RepairAction,
    RepairInstruction,
    ReviewRun,
    RunStatus,
    RuntimeCaptureSummary,
    RuntimeCollectionDiff,
    RuntimeComparisonReport,
    RuntimeComparisonScope,
    RuntimeDifferenceSet,
    RuntimeDomDifference,
    RuntimeScenario,
    RuntimeScreenshotDiff,
    RuntimeTarget,
    RuntimeValidationRun,
    RuntimeViewport,
    RuntimeWaitFor,
)
from packages.sandbox import LocalSandboxRunner, deployment_profile, is_production_profile

DEFAULT_VIEWPORT = {"name": "desktop", "width": 1365, "height": 768}
REMOTE_BROWSER_RUNNER_URL_ENV = "AI_JSUNPACK_BROWSER_RUNNER_URL"
REMOTE_BROWSER_RUNNER_TOKEN_ENV = "AI_JSUNPACK_BROWSER_RUNNER_TOKEN"
REMOTE_BROWSER_RUNNER_TOKEN_FILE_ENV = "AI_JSUNPACK_BROWSER_RUNNER_TOKEN_FILE"
REMOTE_BROWSER_RUNNER_POLL_SECONDS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_POLL_SECONDS"
REMOTE_BROWSER_RUNNER_TIMEOUT_MS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_TIMEOUT_MS"


@dataclass(frozen=True)
class PngImage:
    width: int
    height: int
    rgba: bytes


@dataclass(frozen=True)
class RuntimeImageDiffPolicy:
    pixel_threshold: int = 0
    threshold_mode: str = "per_channel_rgba"
    max_changed_pixel_ratio: float = 0.0


@dataclass(frozen=True)
class RuntimeCompareMatrixItem:
    scenario: RuntimeScenario
    image_diff_policy: RuntimeImageDiffPolicy
    requested_index: int


class PngDecodeError(ValueError):
    pass


def _detect_image_format(content: bytes | None) -> str | None:
    if content is None:
        return None
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if content.startswith(b"BM"):
        return "bmp"
    prefix = content[:128].lstrip().lower()
    if prefix.startswith(b"<svg") or prefix.startswith(b"<?xml") and b"<svg" in prefix:
        return "svg"
    return "unknown"


def _decode_png_rgba(content: bytes) -> PngImage:
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PngDecodeError("Screenshot is not a PNG image.")

    offset = 8
    width = 0
    height = 0
    bit_depth = 0
    color_type = 0
    compression = 0
    filter_method = 0
    interlace = 0
    idat_chunks: list[bytes] = []
    while offset + 8 <= len(content):
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        chunk_type = content[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        if data_end + 4 > len(content):
            raise PngDecodeError("PNG chunk is truncated.")
        data = content[data_start:data_end]
        offset = data_end + 4
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", data)
        elif chunk_type == b"IDAT":
            idat_chunks.append(data)
        elif chunk_type == b"IEND":
            break

    if width < 1 or height < 1 or not idat_chunks:
        raise PngDecodeError("PNG is missing image metadata or pixel data.")
    if bit_depth != 8 or color_type not in {0, 2, 4, 6} or compression != 0 or filter_method != 0 or interlace != 0:
        raise PngDecodeError("PNG pixel diff supports only 8-bit non-interlaced grayscale/RGB/RGBA screenshots.")

    channels_by_color_type = {0: 1, 2: 3, 4: 2, 6: 4}
    channels = channels_by_color_type[color_type]
    stride = width * channels
    try:
        raw = zlib.decompress(b"".join(idat_chunks))
    except zlib.error as error:
        raise PngDecodeError(f"PNG pixel data could not be decompressed: {error}") from error

    expected = (stride + 1) * height
    if len(raw) < expected:
        raise PngDecodeError("PNG pixel data is shorter than expected.")

    rows: list[bytes] = []
    previous = bytearray(stride)
    position = 0
    for _ in range(height):
        filter_type = raw[position]
        position += 1
        scanline = bytearray(raw[position : position + stride])
        position += stride
        _unfilter_png_scanline(scanline, previous, channels, filter_type)
        rows.append(bytes(scanline))
        previous = scanline

    rgba = bytearray(width * height * 4)
    out = 0
    for row in rows:
        for index in range(0, len(row), channels):
            if color_type == 0:
                rgba[out] = row[index]
                rgba[out + 1] = row[index]
                rgba[out + 2] = row[index]
                rgba[out + 3] = 255
            elif color_type == 4:
                rgba[out] = row[index]
                rgba[out + 1] = row[index]
                rgba[out + 2] = row[index]
                rgba[out + 3] = row[index + 1]
            else:
                rgba[out] = row[index]
                rgba[out + 1] = row[index + 1]
                rgba[out + 2] = row[index + 2]
                rgba[out + 3] = row[index + 3] if color_type == 6 else 255
            out += 4
    return PngImage(width=width, height=height, rgba=bytes(rgba))


def _unfilter_png_scanline(scanline: bytearray, previous: bytearray, bpp: int, filter_type: int) -> None:
    if filter_type == 0:
        return
    for index, value in enumerate(scanline):
        left = scanline[index - bpp] if index >= bpp else 0
        up = previous[index]
        up_left = previous[index - bpp] if index >= bpp else 0
        if filter_type == 1:
            scanline[index] = (value + left) & 0xFF
        elif filter_type == 2:
            scanline[index] = (value + up) & 0xFF
        elif filter_type == 3:
            scanline[index] = (value + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            scanline[index] = (value + _png_paeth(left, up, up_left)) & 0xFF
        else:
            raise PngDecodeError(f"Unsupported PNG filter type: {filter_type}.")


def _png_paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    up_left_distance = abs(estimate - up_left)
    if left_distance <= up_distance and left_distance <= up_left_distance:
        return left
    if up_distance <= up_left_distance:
        return up
    return up_left


def _encode_png_rgba(width: int, height: int, rgba: bytes) -> bytes:
    if len(rgba) != width * height * 4:
        raise ValueError("RGBA pixel data does not match PNG dimensions.")
    raw = bytearray()
    stride = width * 4
    for row in range(height):
        raw.append(0)
        start = row * stride
        raw.extend(rgba[start : start + stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _png_chunk(b"IEND", b"")
    )


def _zip_directory_bytes(root: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(path for path in root.rglob("*") if path.is_file()):
            if file_path.is_symlink():
                continue
            relative = file_path.relative_to(root).as_posix()
            archive.write(file_path, relative)
    return buffer.getvalue()


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = binascii.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


@dataclass(frozen=True)
class BrowserSmokeRequest:
    entry_url: str
    screenshot_path: Path
    timeout_ms: int
    job_id: str = "runtime_smoke"
    target: RuntimeTarget = "reconstructed"
    attempt: int = 0
    wait_for_selector: str | None = None
    scenario: RuntimeScenario | None = None
    network_policy: NetworkPolicy = "deny"
    viewport: RuntimeViewport | None = None
    source_root: Path | None = None
    source_entry_path: str | None = None


@dataclass(frozen=True)
class BrowserSmokeCapture:
    console_messages: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)
    assertion_failures: list[str] = field(default_factory=list)
    dom_summary: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    execution_boundary: dict[str, Any] = field(default_factory=dict)


class BrowserSmokeAdapter(Protocol):
    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        ...


class RuntimeSmokeError(Exception):
    def __init__(self, message: str, failure_class: FailureClass = "runtime_error") -> None:
        super().__init__(message)
        self.failure_class = failure_class


class PolicyDeniedBrowserAdapter:
    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        raise RuntimeSmokeError(
            "Local Playwright execution is disabled by the production deployment profile; configure the remote Browser Runner.",
            "policy_denied",
        )


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
                    viewport = request.viewport or RuntimeViewport(**DEFAULT_VIEWPORT)
                    page = browser.new_page(viewport={"width": viewport.width, "height": viewport.height})
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


class RemoteBrowserRunnerAdapter:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        poll_seconds: float | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        raw_base_url = base_url or os.getenv(REMOTE_BROWSER_RUNNER_URL_ENV) or ""
        self.base_url = self._validated_base_url(raw_base_url) if raw_base_url.strip() else ""
        self.token_file: Path | None = None
        self.token = token if token is not None else self._token_from_environment()
        self.poll_seconds = poll_seconds if poll_seconds is not None else self._float_env(REMOTE_BROWSER_RUNNER_POLL_SECONDS_ENV, 0.25)
        self.timeout_ms = timeout_ms if timeout_ms is not None else self._int_env(REMOTE_BROWSER_RUNNER_TIMEOUT_MS_ENV, 60_000)
        if not self.base_url:
            raise RuntimeSmokeError(f"{REMOTE_BROWSER_RUNNER_URL_ENV} is not configured.", "policy_denied")
        if not self.token:
            raise RuntimeSmokeError(
                f"{REMOTE_BROWSER_RUNNER_TOKEN_ENV} or {REMOTE_BROWSER_RUNNER_TOKEN_FILE_ENV} is not configured.",
                "policy_denied",
            )

    def _validated_base_url(self, value: str) -> str:
        base_url = value.strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeSmokeError(f"{REMOTE_BROWSER_RUNNER_URL_ENV} must use http or https.", "policy_denied")
        if not parsed.hostname:
            raise RuntimeSmokeError(f"{REMOTE_BROWSER_RUNNER_URL_ENV} must include a hostname.", "policy_denied")
        if parsed.username or parsed.password:
            raise RuntimeSmokeError(f"{REMOTE_BROWSER_RUNNER_URL_ENV} must not include credentials.", "policy_denied")
        return base_url

    @classmethod
    def from_environment(cls) -> "RemoteBrowserRunnerAdapter | None":
        if not os.getenv(REMOTE_BROWSER_RUNNER_URL_ENV):
            return None
        return cls()

    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        payload = BrowserRunRequest(
            job_id=request.job_id,
            target=request.target,
            attempt=request.attempt,
            entry_url=request.entry_url,
            timeout_ms=request.timeout_ms,
            wait_for_selector=request.wait_for_selector,
            scenario=request.scenario,
            network_policy=request.network_policy,
            viewport=request.viewport,
            source_archive=self._source_archive(request),
        ).model_dump(by_alias=True)
        summary = self._post("/browser-runs", payload)
        run_id = str(summary.get("id") or "")
        if not run_id:
            raise RuntimeSmokeError("Browser Runner did not return a run id.", "runtime_error")
        deadline = time.monotonic() + (self.timeout_ms / 1000)
        while True:
            current = BrowserRunSummary.model_validate(self._get(f"/browser-runs/{quote(run_id)}"))
            if current.status in {"pass", "fail", "best_effort"}:
                if current.result is None:
                    raise RuntimeSmokeError("Browser Runner completed without a result payload.", "runtime_error")
                return self._capture_from_result(request, current)
            if time.monotonic() >= deadline:
                raise RuntimeSmokeError(f"Browser Runner run timed out while waiting for {run_id}.", "timeout")
            time.sleep(max(0.05, self.poll_seconds))

    def _capture_from_result(self, request: BrowserSmokeRequest, summary: BrowserRunSummary) -> BrowserSmokeCapture:
        result = summary.result
        if result is None:
            raise RuntimeSmokeError("Browser Runner result is unavailable.", "runtime_error")
        if result.screenshot_base64:
            request.screenshot_path.write_bytes(base64.b64decode(result.screenshot_base64.encode("ascii")))
        boundary = dict(result.execution_boundary)
        boundary.setdefault("remoteRunId", summary.id)
        boundary.setdefault("serviceUrl", self.base_url)
        boundary.setdefault("auth", "bearer_hmac")
        return BrowserSmokeCapture(
            console_messages=result.console_messages,
            console_errors=result.console_errors,
            page_errors=result.page_errors,
            failed_requests=result.failed_requests,
            responses=result.responses,
            assertion_failures=result.assertion_failures,
            dom_summary=result.dom_summary,
            limitations=result.limitations,
            execution_boundary=boundary,
        )

    def _source_archive(self, request: BrowserSmokeRequest) -> BrowserRunSourceArchive | None:
        if request.source_root is None or request.source_entry_path is None:
            return None
        content = _zip_directory_bytes(request.source_root)
        return BrowserRunSourceArchive(
            content_base64=base64.b64encode(content).decode("ascii"),
            entry_path=request.source_entry_path,
        )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        token = self._authorization_token()
        request = Request(  # noqa: S310 - base_url is validated in __init__.
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        return self._json_request(request)

    def _get(self, path: str) -> dict[str, Any]:
        token = self._authorization_token()
        request = Request(  # noqa: S310 - base_url is validated in __init__.
            f"{self.base_url}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        return self._json_request(request)

    def _json_request(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=max(1, self.timeout_ms / 1000)) as response:  # noqa: S310  # nosec B310
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            failure_class: FailureClass = "policy_denied" if error.code in {401, 403} else "runtime_error"
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeSmokeError(f"Browser Runner request failed with HTTP {error.code}: {detail}", failure_class) from error
        except URLError as error:
            raise RuntimeSmokeError(f"Browser Runner is unreachable: {error.reason}", "runtime_error") from error
        except TimeoutError as error:
            raise RuntimeSmokeError("Browser Runner request timed out.", "timeout") from error
        if not isinstance(payload, dict):
            raise RuntimeSmokeError("Browser Runner returned a non-object JSON payload.", "runtime_error")
        return payload

    def _int_env(self, name: str, default: int) -> int:
        try:
            return max(1, int(os.getenv(name, str(default))))
        except ValueError:
            return default

    def _float_env(self, name: str, default: float) -> float:
        try:
            return max(0.05, float(os.getenv(name, str(default))))
        except ValueError:
            return default

    def _token_from_environment(self) -> str | None:
        token = os.getenv(REMOTE_BROWSER_RUNNER_TOKEN_ENV)
        if token and token.strip():
            return token.strip()
        token_file = os.getenv(REMOTE_BROWSER_RUNNER_TOKEN_FILE_ENV)
        if not token_file or not token_file.strip():
            return None
        self.token_file = Path(token_file.strip())
        return self._read_token_file()

    def _authorization_token(self) -> str:
        if self.token_file is not None:
            return self._read_token_file()
        if not self.token:
            raise RuntimeSmokeError(f"{REMOTE_BROWSER_RUNNER_TOKEN_ENV} is not configured.", "policy_denied")
        return self.token

    def _read_token_file(self) -> str:
        if self.token_file is None:
            raise RuntimeSmokeError(f"{REMOTE_BROWSER_RUNNER_TOKEN_FILE_ENV} is not configured.", "policy_denied")
        try:
            value = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeSmokeError(
                f"Unable to read {REMOTE_BROWSER_RUNNER_TOKEN_FILE_ENV}: {error}",
                "policy_denied",
            ) from error
        if not value:
            raise RuntimeSmokeError(f"{REMOTE_BROWSER_RUNNER_TOKEN_FILE_ENV} is empty.", "policy_denied")
        return value


@dataclass(frozen=True)
class RuntimeSmokeResult:
    validation: RuntimeValidationRun
    report_artifact: ArtifactRecord
    trace_artifact: ArtifactRecord
    screenshot_artifact: ArtifactRecord | None
    message: str

    @property
    def artifact_ids(self) -> list[str]:
        return [
            artifact.id
            for artifact in (self.trace_artifact, self.report_artifact, self.screenshot_artifact)
            if artifact is not None
        ]


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
        remote_adapter = None if browser_adapter is not None else RemoteBrowserRunnerAdapter.from_environment()
        self.browser_adapter = browser_adapter or remote_adapter or PlaywrightBrowserAdapter()
        self._using_local_browser_fallback = browser_adapter is None and remote_adapter is None
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
        attempt: int = 0,
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
                    execution_boundary={},
                    parent_artifact_ids=parents,
                    attempt=attempt,
                    duration_ms=self._duration_ms(started_at),
                    message="Runtime smoke skipped because no HTML entry was found.",
            )

        with self.sandbox_runner.attempt_workspace() as temp_dir:
            screenshot_path = temp_dir / "runtime-smoke.png"
            resolved_url = entry.entry_url or "about:blank"
            try:
                with self._entry_url(entry) as resolved_url:
                    capture = self._browser_adapter_for_job(job_id=job_id, store=store).capture(
                        BrowserSmokeRequest(
                            entry_url=resolved_url,
                            screenshot_path=screenshot_path,
                            timeout_ms=self.timeout_ms,
                            job_id=job_id,
                            target=target,
                            attempt=attempt,
                            wait_for_selector=wait_for_selector,
                            scenario=scenario,
                            network_policy=network_policy,
                            source_root=entry.serve_root,
                            source_entry_path=entry.relative_entry,
                        )
                    )
                status = self._status_for_capture(capture)
                screenshot_bytes = screenshot_path.read_bytes() if screenshot_path.exists() else None
                limitations = [*entry.limitations, *capture.limitations]
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
                    execution_boundary=capture.execution_boundary,
                    parent_artifact_ids=parents,
                    attempt=attempt,
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
                    execution_boundary={},
                    parent_artifact_ids=parents,
                    attempt=attempt,
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
        execution_boundary: dict[str, Any],
        parent_artifact_ids: list[str],
        attempt: int,
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
                attempt=attempt,
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
            "executionBoundary": execution_boundary,
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
            attempt=attempt,
        )

        validation = RuntimeValidationRun(
            id=f"runtime_{uuid4().hex[:12]}",
            job_id=job_id,
            attempt=attempt,
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
            attempt=attempt,
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

    def _browser_adapter_for_job(self, *, job_id: str, store) -> BrowserSmokeAdapter:
        if not self._using_local_browser_fallback:
            return self.browser_adapter
        job = store.get_job(job_id)
        raw_config = job.config if job is not None and isinstance(job.config, dict) else {}
        profile = deployment_profile(raw_config.get("deploymentProfile"))
        if is_production_profile(profile):
            return PolicyDeniedBrowserAdapter()
        return self.browser_adapter

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
    validations: list[RuntimeValidationRun]
    scenario_artifacts: list[ArtifactRecord]
    comparison_artifacts: list[ArtifactRecord]
    trace_artifacts: list[ArtifactRecord]
    report_artifacts: list[ArtifactRecord]
    message: str

    @property
    def artifact_ids(self) -> list[str]:
        ordered = [
            *[artifact.id for artifact in self.scenario_artifacts],
            *[artifact.id for artifact in self.comparison_artifacts],
            *[artifact.id for artifact in self.trace_artifacts],
            *[artifact.id for artifact in self.report_artifacts],
            *[artifact.id for artifact in self.screenshot_artifacts],
        ]
        return list(dict.fromkeys(ordered))


@dataclass(frozen=True)
class RuntimeCompareReviewGateResult:
    enabled: bool
    triggered: bool
    review_artifact: ArtifactRecord | None
    repair_artifact: ArtifactRecord | None
    message: str

    @property
    def artifact_ids(self) -> list[str]:
        return [
            artifact.id
            for artifact in (self.review_artifact, self.repair_artifact)
            if artifact is not None
        ]


class RuntimeCompareReviewGate:
    """将 runtime comparison 差异转为 review 和 repair evidence。"""

    def run(
        self,
        *,
        job_id: str,
        store,
        comparison_artifacts: list[ArtifactRecord],
        job_config: dict[str, Any] | None = None,
        parent_artifact_ids: list[str] | None = None,
        attempt: int = 0,
    ) -> RuntimeCompareReviewGateResult:
        policy = self._policy(job_config)
        if not policy["enabled"]:
            return RuntimeCompareReviewGateResult(
                enabled=False,
                triggered=False,
                review_artifact=None,
                repair_artifact=None,
                message="Runtime compare review gate disabled by job configuration.",
            )

        store.update_status(job_id, "reviewing")
        parents = parent_artifact_ids or [artifact.id for artifact in comparison_artifacts]
        comparisons = [
            (artifact, self._load_comparison(job_id=job_id, store=store, artifact=artifact))
            for artifact in comparison_artifacts
        ]
        gate_observations = [
            observation
            for artifact, comparison in comparisons
            for observation in self._gate_observations(artifact=artifact, comparison=comparison, policy=policy)
        ]
        if not comparisons:
            gate_observations.append("No runtime comparison artifacts were available for review gate evaluation.")
        triggered = bool(gate_observations)
        evidence_refs = [
            self._evidence_ref(artifact=artifact, comparison=comparison)
            for artifact, comparison in comparisons
        ]
        repair_artifact = None
        if triggered:
            repair_attempt = attempt + 1
            repair_instruction = RepairInstruction(
                id=f"repair_{uuid4().hex[:12]}",
                job_id=job_id,
                attempt=repair_attempt,
                target_stage="runtime_compare",
                failure_class="runtime_error",
                input_artifact_ids=[artifact.id for artifact, _ in comparisons],
                evidence_refs=evidence_refs,
                actions=[],
                status="planned",
                risk_level="medium",
                decision=(
                    "Runtime comparison differences require Review/Fix handling before the "
                    "reconstructed project can be treated as behaviorally equivalent. No "
                    "deterministic runtime repair action was applied in this pass."
                ),
            )
            repair_artifact = store.write_artifact(
                job_id,
                kind="repair_instruction",
                stage="repairing",
                filename=f"runtime-compare-repair-instruction-attempt-{repair_attempt}.json",
                content=repair_instruction.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                content_type="application/json",
                producer="worker.runtime_compare_review",
                parent_artifact_ids=parents,
                attempt=repair_attempt,
            )

        decision = self._decision(
            comparisons=comparisons,
            gate_observations=gate_observations,
            policy=policy,
        )
        review_run = ReviewRun(
            id=f"review_{uuid4().hex[:12]}",
            job_id=job_id,
            attempt=attempt,
            review_type="runtime_compare",
            status="fail" if triggered else "pass",
            decision=decision,
            failure_class="runtime_error" if triggered else "none",
            evidence_refs=evidence_refs,
            repair_instruction_ids=[repair_artifact.id] if repair_artifact is not None else [],
            logs_artifact_id=None,
        )
        review_parent_ids = [*parents]
        if repair_artifact is not None:
            review_parent_ids.append(repair_artifact.id)
        review_artifact = store.write_artifact(
            job_id,
            kind="review_run",
            stage="reviewing",
            filename="runtime-compare-review.json",
            content=review_run.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.runtime_compare_review",
            parent_artifact_ids=review_parent_ids,
            attempt=attempt,
        )
        if triggered:
            store.update_status(job_id, "repairing")

        return RuntimeCompareReviewGateResult(
            enabled=True,
            triggered=triggered,
            review_artifact=review_artifact,
            repair_artifact=repair_artifact,
            message=decision,
        )

    def _load_comparison(self, *, job_id: str, store, artifact: ArtifactRecord) -> RuntimeComparisonReport:
        payload = json.loads(store.read_artifact(job_id, artifact.id).decode("utf-8"))
        return RuntimeComparisonReport.model_validate(payload)

    def _policy(self, job_config: dict[str, Any] | None) -> dict[str, Any]:
        runtime_compare = job_config.get("runtimeCompare") if isinstance(job_config, dict) else None
        if not isinstance(runtime_compare, dict) and isinstance(job_config, dict):
            runtime_compare = job_config
        review_gate = runtime_compare.get("reviewGate") if isinstance(runtime_compare, dict) else None
        review_gate = review_gate if isinstance(review_gate, dict) else {}
        image_diff = runtime_compare.get("imageDiff") if isinstance(runtime_compare, dict) else None
        image_diff = image_diff if isinstance(image_diff, dict) else {}
        default_ratio = self._float_config(image_diff.get("maxChangedPixelRatio"), default=0.0)
        return {
            "enabled": self._bool_config(review_gate.get("enabled"), default=True),
            "failOnDomChanged": self._bool_config(review_gate.get("failOnDomChanged"), default=True),
            "failOnNetworkChanged": self._bool_config(review_gate.get("failOnNetworkChanged"), default=True),
            "failOnConsoleChanged": self._bool_config(review_gate.get("failOnConsoleChanged"), default=True),
            "failOnScreenshotChanged": self._bool_config(review_gate.get("failOnScreenshotChanged"), default=True),
            "maxChangedPixelRatio": self._float_config(review_gate.get("maxChangedPixelRatio"), default=default_ratio),
        }

    def _gate_observations(
        self,
        *,
        artifact: ArtifactRecord,
        comparison: RuntimeComparisonReport,
        policy: dict[str, Any],
    ) -> list[str]:
        differences = comparison.differences
        scope = self._scope_label(comparison)
        observations: list[str] = []
        if comparison.status in {"fail", "retry"}:
            observations.append(f"{scope}: runtime comparison status is {comparison.status}.")
        if policy["failOnDomChanged"] and differences.dom_changed:
            count = len(differences.dom_differences) or len(differences.changed_dom_fields)
            observations.append(f"{scope}: DOM changed across {count} field(s).")
        if policy["failOnNetworkChanged"] and differences.network_changed:
            observations.append(
                f"{scope}: network changed with "
                f"{len(differences.original_only_requests)} original-only and "
                f"{len(differences.reconstructed_only_requests)} reconstructed-only request(s)."
            )
        if policy["failOnConsoleChanged"] and differences.console_changed:
            observations.append(
                f"{scope}: console changed with "
                f"{len(differences.original_only_console)} original-only and "
                f"{len(differences.reconstructed_only_console)} reconstructed-only line(s)."
            )
        screenshot_reason = self._screenshot_gate_reason(differences, policy)
        if screenshot_reason:
            observations.append(f"{scope}: {screenshot_reason}")
        return [f"{artifact.id} {observation}" for observation in observations]

    def _screenshot_gate_reason(self, differences: RuntimeDifferenceSet, policy: dict[str, Any]) -> str | None:
        if not policy["failOnScreenshotChanged"]:
            return None
        screenshot = differences.screenshot_diff
        if screenshot.pixel_diff_status == "compared":
            changed_ratio = screenshot.changed_pixel_ratio or 0.0
            max_ratio = policy["maxChangedPixelRatio"]
            if changed_ratio > max_ratio:
                return f"screenshot pixel diff ratio {changed_ratio:.6f} exceeded {max_ratio:.6f}."
            return None
        if screenshot.changed is True:
            reason = screenshot.reason or "screenshot hash or size changed while pixel diff was unavailable."
            return f"screenshot changed without comparable pixel diff ({reason})"
        return None

    def _decision(
        self,
        *,
        comparisons: list[tuple[ArtifactRecord, RuntimeComparisonReport]],
        gate_observations: list[str],
        policy: dict[str, Any],
    ) -> str:
        if not comparisons:
            return "Runtime compare review gate could not find comparison evidence to evaluate."
        if not gate_observations:
            return (
                "Runtime compare review gate passed across "
                f"{len(comparisons)} comparison artifact(s); configured thresholds were not exceeded."
            )
        observations = "; ".join(gate_observations[:5])
        if len(gate_observations) > 5:
            observations += f"; plus {len(gate_observations) - 5} additional observation(s)"
        return (
            "Runtime compare review gate blocked automatic behavioral equivalence: "
            f"{observations}. Policy: dom={policy['failOnDomChanged']}, "
            f"network={policy['failOnNetworkChanged']}, console={policy['failOnConsoleChanged']}, "
            f"screenshot={policy['failOnScreenshotChanged']}, "
            f"maxChangedPixelRatio={policy['maxChangedPixelRatio']}."
        )

    def _evidence_ref(self, *, artifact: ArtifactRecord, comparison: RuntimeComparisonReport) -> EvidenceRef:
        return EvidenceRef(
            artifact_id=artifact.id,
            label=f"Runtime comparison: {self._scope_label(comparison)}",
            locator=f"artifact://{artifact.id}",
            excerpt=self._comparison_excerpt(comparison),
        )

    def _comparison_excerpt(self, comparison: RuntimeComparisonReport) -> str:
        differences = comparison.differences
        return (
            f"status={comparison.status}; dom={differences.dom_changed}; "
            f"network={differences.network_changed}; console={differences.console_changed}; "
            f"screenshot={differences.screenshot_changed}"
        )

    def _scope_label(self, comparison: RuntimeComparisonReport) -> str:
        scope = comparison.differences.comparison_scope
        viewport = scope.viewport
        viewport_label = f"{viewport.name or 'viewport'} {viewport.width}x{viewport.height}" if viewport else "viewport unknown"
        return f"{scope.scenario_name} / {viewport_label}"

    def _bool_config(self, value: Any, *, default: bool) -> bool:
        return value if isinstance(value, bool) else default

    def _float_config(self, value: Any, *, default: float) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0.0, float(value))
        return default


@dataclass(frozen=True)
class RuntimeCompareRepairResult:
    repair_artifact: ArtifactRecord | None
    applied_project_artifact: ArtifactRecord | None
    message: str

    @property
    def artifact_ids(self) -> list[str]:
        return [
            artifact.id
            for artifact in (self.repair_artifact, self.applied_project_artifact)
            if artifact is not None
        ]


class RuntimeCompareRepairRunner:
    """对生成工程 attempt 应用低风险确定性 runtime repair 动作。"""

    PROTECTED_ROOTS = {"src", "scripts", "node_modules", ".git"}
    PROTECTED_FILES = {
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "tsconfig.json",
        "tsconfig.base.json",
        "vite.config.js",
        "vite.config.mjs",
        "vite.config.ts",
        "rollup.config.js",
        "webpack.config.js",
    }

    def run(
        self,
        *,
        job_id: str,
        store,
        generated_project_artifact: ArtifactRecord | None,
        planned_repair_artifact: ArtifactRecord | None,
        job_config: dict[str, Any] | None = None,
        parent_artifact_ids: list[str] | None = None,
        attempt: int = 1,
    ) -> RuntimeCompareRepairResult:
        if planned_repair_artifact is None:
            return RuntimeCompareRepairResult(
                repair_artifact=None,
                applied_project_artifact=None,
                message="No planned runtime compare repair instruction was available.",
            )

        store.update_status(job_id, "repairing")
        parents = list(dict.fromkeys([*(parent_artifact_ids or []), planned_repair_artifact.id]))
        planned = self._load_repair_instruction(job_id=job_id, store=store, artifact=planned_repair_artifact)
        evidence_refs = planned.evidence_refs
        input_artifact_ids = list(dict.fromkeys([planned_repair_artifact.id, *planned.input_artifact_ids]))
        policy = self._policy(job_config)

        if not policy["allowLowRiskRepairs"] or not self._repair_action_allowed("mirror_original_static_entry", policy):
            repair_artifact = self._write_repair_instruction(
                job_id=job_id,
                store=store,
                attempt=attempt,
                parent_artifact_ids=parents,
                input_artifact_ids=input_artifact_ids,
                evidence_refs=evidence_refs,
                actions=[],
                status="skipped",
                risk_level="medium",
                decision="Runtime compare repair skipped because reviewFix policy disabled the low-risk static mirror action.",
            )
            return RuntimeCompareRepairResult(
                repair_artifact=repair_artifact,
                applied_project_artifact=None,
                message="Runtime compare repair skipped by Review/Fix policy.",
            )

        if generated_project_artifact is None:
            repair_artifact = self._write_repair_instruction(
                job_id=job_id,
                store=store,
                attempt=attempt,
                parent_artifact_ids=parents,
                input_artifact_ids=input_artifact_ids,
                evidence_refs=evidence_refs,
                actions=[],
                status="skipped",
                risk_level="medium",
                decision="Runtime compare repair skipped because no generated_project artifact was available.",
            )
            return RuntimeCompareRepairResult(
                repair_artifact=repair_artifact,
                applied_project_artifact=None,
                message="Runtime compare repair skipped because no generated_project artifact was available.",
            )

        source_project = store.artifact_local_path(generated_project_artifact)
        if source_project is not None and not source_project.is_dir():
            source_project = None
        if source_project is None and not store.artifact_is_directory(generated_project_artifact):
            repair_artifact = self._write_repair_instruction(
                job_id=job_id,
                store=store,
                attempt=attempt,
                parent_artifact_ids=[*parents, generated_project_artifact.id],
                input_artifact_ids=[*input_artifact_ids, generated_project_artifact.id],
                evidence_refs=evidence_refs,
                actions=[],
                status="skipped",
                risk_level="medium",
                decision="Runtime compare repair skipped because generated_project storage was not a directory.",
            )
            return RuntimeCompareRepairResult(
                repair_artifact=repair_artifact,
                applied_project_artifact=None,
                message="Runtime compare repair skipped because generated_project storage was not a directory.",
            )

        with tempfile.TemporaryDirectory(prefix="ai-jsunpack-runtime-repair-") as temp_dir:
            if source_project is None:
                source_project = store.materialize_artifact_directory(
                    generated_project_artifact,
                    Path(temp_dir) / "source_project",
                )
            attempt_root = Path(temp_dir) / "generated_project"
            shutil.copytree(source_project, attempt_root)
            actions, decision = self._apply_runtime_actions(project_root=attempt_root)
            status = "applied" if actions else "skipped"
            risk_level = "low" if actions else "medium"
            repair_artifact = self._write_repair_instruction(
                job_id=job_id,
                store=store,
                attempt=attempt,
                parent_artifact_ids=[*parents, generated_project_artifact.id],
                input_artifact_ids=[*input_artifact_ids, generated_project_artifact.id],
                evidence_refs=evidence_refs,
                actions=actions,
                status=status,
                risk_level=risk_level,
                decision=decision,
            )
            if not actions:
                return RuntimeCompareRepairResult(
                    repair_artifact=repair_artifact,
                    applied_project_artifact=None,
                    message=decision,
                )

            applied_project_artifact = store.register_artifact_path(
                job_id,
                kind="generated_project",
                stage="repairing",
                filename=f"runtime-repaired-generated-project-attempt-{attempt}",
                source_path=attempt_root,
                content_type="application/vnd.ai-jsunpack.generated-project+directory",
                producer="worker.runtime_compare_repair",
                parent_artifact_ids=[*parents, generated_project_artifact.id, repair_artifact.id],
                attempt=attempt,
            )
            return RuntimeCompareRepairResult(
                repair_artifact=repair_artifact,
                applied_project_artifact=applied_project_artifact,
                message=decision,
            )

    def _load_repair_instruction(self, *, job_id: str, store, artifact: ArtifactRecord) -> RepairInstruction:
        payload = json.loads(store.read_artifact(job_id, artifact.id).decode("utf-8"))
        return RepairInstruction.model_validate(payload)

    def _policy(self, job_config: dict[str, Any] | None) -> dict[str, Any]:
        review_fix = job_config.get("reviewFix") if isinstance(job_config, dict) else None
        review_fix = review_fix if isinstance(review_fix, dict) else {}
        runtime_policy = review_fix.get("runtimeCompare") if isinstance(review_fix.get("runtimeCompare"), dict) else {}
        return {
            "allowLowRiskRepairs": self._bool_config(
                runtime_policy.get("allowLowRiskRepairs", review_fix.get("allowLowRiskRepairs")),
                default=True,
            ),
            "allowedRepairActions": self._allowed_repair_actions_config(
                runtime_policy.get("allowedRepairActions", review_fix.get("allowedRepairActions"))
            ),
        }

    def _repair_action_allowed(self, action: str, policy: dict[str, Any]) -> bool:
        allowed_actions = policy.get("allowedRepairActions")
        return not isinstance(allowed_actions, tuple) or action in allowed_actions

    def _bool_config(self, value: Any, *, default: bool) -> bool:
        return value if isinstance(value, bool) else default

    def _allowed_repair_actions_config(self, value: Any) -> tuple[str, ...] | None:
        if value is None or not isinstance(value, list):
            return None
        allowed = [
            item
            for item in value
            if item in {"add_package_script", "replace_package_script", "mirror_original_static_entry"}
        ]
        return tuple(dict.fromkeys(allowed))

    def _apply_runtime_actions(self, *, project_root: Path) -> tuple[list[RepairAction], str]:
        source_root, limitation = self._source_root(project_root)
        if source_root is None:
            return [], limitation

        copied = 0
        skipped = 0
        for source_file in sorted(path for path in source_root.rglob("*") if path.is_file()):
            if source_file.is_symlink():
                skipped += 1
                continue
            relative_path = source_file.relative_to(source_root)
            if self._is_protected_mirror_path(relative_path):
                skipped += 1
                continue
            target_path = project_root / relative_path
            try:
                target_path.resolve().relative_to(project_root.resolve())
            except ValueError:
                skipped += 1
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_path)
            copied += 1

        if copied < 1:
            return [], "Runtime compare repair skipped because no safe static files were available to mirror."

        action = RepairAction(
            action="mirror_original_static_entry",
            path="projectRoot",
            value="public/original",
            reason=(
                f"Mirrored {copied} static file(s) from public/original into the generated project root "
                f"for runtime compare retry; skipped {skipped} protected or unsafe file(s)."
            ),
        )
        return [action], (
            "Applied deterministic runtime repair by mirroring the original static entry into a new "
            f"generated_project attempt ({copied} file(s) copied, {skipped} skipped)."
        )

    def _source_root(self, project_root: Path) -> tuple[Path | None, str]:
        manifest_path = project_root / "src" / "reconstruction-manifest.json"
        if not manifest_path.is_file():
            return None, "Runtime compare repair skipped because reconstruction manifest was missing."
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None, "Runtime compare repair skipped because reconstruction manifest was not valid JSON."

        source_root_value = manifest.get("sourceRoot")
        if not isinstance(source_root_value, str) or not source_root_value:
            return None, "Runtime compare repair skipped because reconstruction manifest sourceRoot was missing."
        source_root_relative = Path(source_root_value)
        if source_root_relative.is_absolute() or ".." in source_root_relative.parts:
            return None, "Runtime compare repair skipped because reconstruction manifest sourceRoot was unsafe."
        source_root = project_root / source_root_relative
        if not source_root.is_dir():
            return None, f"Runtime compare repair skipped because sourceRoot was unavailable: {source_root_value}."
        return source_root, ""

    def _is_protected_mirror_path(self, relative_path: Path) -> bool:
        parts = relative_path.parts
        if not parts:
            return True
        first = parts[0].lower()
        if first in self.PROTECTED_ROOTS:
            return True
        normalized = relative_path.as_posix().lower()
        return normalized in self.PROTECTED_FILES

    def _write_repair_instruction(
        self,
        *,
        job_id: str,
        store,
        attempt: int,
        parent_artifact_ids: list[str],
        input_artifact_ids: list[str],
        evidence_refs: list[EvidenceRef],
        actions: list[RepairAction],
        status: str,
        risk_level: str,
        decision: str,
    ) -> ArtifactRecord:
        repair_instruction = RepairInstruction(
            id=f"repair_{uuid4().hex[:12]}",
            job_id=job_id,
            attempt=attempt,
            target_stage="runtime_compare",
            failure_class="runtime_error",
            input_artifact_ids=list(dict.fromkeys(input_artifact_ids)),
            evidence_refs=evidence_refs,
            actions=actions,
            status=status,
            risk_level=risk_level,
            decision=decision,
        )
        return store.write_artifact(
            job_id,
            kind="repair_instruction",
            stage="repairing",
            filename=f"runtime-compare-applied-repair-attempt-{attempt}.json",
            content=repair_instruction.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.runtime_compare_repair",
            parent_artifact_ids=list(dict.fromkeys(parent_artifact_ids)),
            attempt=attempt,
        )


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
        attempt: int = 0,
    ) -> RuntimeCompareResult:
        parents = parent_artifact_ids or []
        store.update_status(job_id, "runtime_compare")
        matrix, matrix_plan = self._scenario_matrix_from_config(job_id=job_id, config=scenario_config)
        matrix_trace_artifact = self._write_matrix_trace(
            job_id=job_id,
            store=store,
            parent_artifact_ids=parents,
            matrix_plan=matrix_plan,
            attempt=attempt,
        )
        matrix_limitation = self._matrix_limitation(matrix_plan)
        run_parent_ids = [*parents, *([matrix_trace_artifact.id] if matrix_trace_artifact else [])]
        results = [
            self._run_single_compare(
                job_id=job_id,
                store=store,
                original_input_path=original_input_path,
                reconstructed_input_path=reconstructed_input_path,
                scenario=item.scenario,
                image_diff_policy=item.image_diff_policy,
                matrix_index=index,
                matrix_limitation=matrix_limitation,
                parent_artifact_ids=run_parent_ids,
                attempt=attempt,
            )
            for index, item in enumerate(matrix)
        ]

        primary = results[0]
        validations = [result.validation for result in results]
        scenario_artifacts = [result.scenario_artifact for result in results]
        comparison_artifacts = [result.comparison_artifact for result in results]
        trace_artifacts = [
            *([matrix_trace_artifact] if matrix_trace_artifact else []),
            *[result.trace_artifact for result in results],
        ]
        report_artifacts = [result.report_artifact for result in results]
        screenshot_artifacts = [artifact for result in results for artifact in result.screenshot_artifacts]
        statuses = {validation.status for validation in validations}
        aggregate_status = "best_effort" if "best_effort" in statuses else "fail" if "fail" in statuses else "retry" if "retry" in statuses else "pass"
        return RuntimeCompareResult(
            validation=primary.validation,
            scenario_artifact=primary.scenario_artifact,
            comparison_artifact=primary.comparison_artifact,
            trace_artifact=primary.trace_artifact,
            screenshot_artifacts=screenshot_artifacts,
            report_artifact=primary.report_artifact,
            validations=validations,
            scenario_artifacts=scenario_artifacts,
            comparison_artifacts=comparison_artifacts,
            trace_artifacts=trace_artifacts,
            report_artifacts=report_artifacts,
            message=(
                f"Runtime compare completed {len(results)} scenario/viewport run(s) "
                f"with aggregate status {aggregate_status}."
            ),
        )

    def _run_single_compare(
        self,
        *,
        job_id: str,
        store,
        original_input_path: Path | str | None,
        reconstructed_input_path: Path | str | None,
        scenario: RuntimeScenario,
        image_diff_policy: RuntimeImageDiffPolicy,
        matrix_index: int,
        matrix_limitation: str | None,
        parent_artifact_ids: list[str],
        attempt: int,
    ) -> RuntimeCompareResult:
        parents = parent_artifact_ids
        artifact_suffix = f"{matrix_index:02d}"
        scenario_artifact = store.write_artifact(
            job_id,
            kind="runtime_scenario",
            stage="runtime_compare",
            filename=f"runtime-scenario-{artifact_suffix}.json",
            content=scenario.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.runtime_compare",
            parent_artifact_ids=parents,
            attempt=attempt,
        )

        with self.sandbox_runner.attempt_workspace() as temp_dir:
            original = self._capture_target(
                job_id=job_id,
                store=store,
                target="original",
                input_path=original_input_path,
                scenario=scenario,
                temp_dir=temp_dir,
                matrix_index=matrix_index,
                parent_artifact_ids=[*parents, scenario_artifact.id],
                attempt=attempt,
            )
            reconstructed = self._capture_target(
                job_id=job_id,
                store=store,
                target="reconstructed",
                input_path=reconstructed_input_path,
                scenario=scenario,
                temp_dir=temp_dir,
                matrix_index=matrix_index,
                parent_artifact_ids=[*parents, scenario_artifact.id],
                attempt=attempt,
            )

        screenshot_artifacts = [artifact for artifact in (original["screenshot"], reconstructed["screenshot"]) if artifact]
        screenshot_diff, diff_artifact = self._screenshot_diff(
            job_id=job_id,
            store=store,
            changed=None,
            original_hash=original["screenshot_hash"],
            reconstructed_hash=reconstructed["screenshot_hash"],
            original_size=original["screenshot_size"],
            reconstructed_size=reconstructed["screenshot_size"],
            original_bytes=original["screenshot_bytes"],
            reconstructed_bytes=reconstructed["screenshot_bytes"],
            image_diff_policy=image_diff_policy,
            filename_suffix=artifact_suffix,
            parent_artifact_ids=[
                *parents,
                scenario_artifact.id,
                *[artifact.id for artifact in screenshot_artifacts],
            ],
            attempt=attempt,
        )
        if diff_artifact is not None:
            screenshot_artifacts.append(diff_artifact)
        trace_payload = {
            "kind": "runtime_trace",
            "jobId": job_id,
            "target": "comparison",
            "scenarioArtifactId": scenario_artifact.id,
            "scenarioName": scenario.name,
            "viewport": scenario.viewport.model_dump(by_alias=True) if scenario.viewport else None,
            "imageDiffPolicy": {
                "pixelThreshold": image_diff_policy.pixel_threshold,
                "thresholdMode": image_diff_policy.threshold_mode,
                "maxChangedPixelRatio": image_diff_policy.max_changed_pixel_ratio,
            },
            "original": original["summary"].model_dump(by_alias=True),
            "reconstructed": reconstructed["summary"].model_dump(by_alias=True),
            "executionBoundary": {
                "original": original["execution_boundary"],
                "reconstructed": reconstructed["execution_boundary"],
            },
        }
        trace_artifact = store.write_artifact(
            job_id,
            kind="runtime_trace",
            stage="runtime_compare",
            filename=f"runtime-compare-trace-{artifact_suffix}.json",
            content=self._json_bytes(trace_payload),
            content_type="application/json",
            producer="worker.runtime_compare",
            parent_artifact_ids=[*parents, scenario_artifact.id, *[artifact.id for artifact in screenshot_artifacts]],
            attempt=attempt,
        )

        differences = self._compare_captures(
            original=original["summary"],
            reconstructed=reconstructed["summary"],
            scenario=scenario,
            screenshot_diff=screenshot_diff,
        )
        status = self._comparison_status(original["summary"], reconstructed["summary"])
        comparison = RuntimeComparisonReport(
            id=f"runtime_comparison_{uuid4().hex[:12]}",
            job_id=job_id,
            attempt=attempt,
            status=status,
            scenario_artifact_id=scenario_artifact.id,
            original=original["summary"],
            reconstructed=reconstructed["summary"],
            differences=differences,
            screenshot_artifact_ids=[artifact.id for artifact in screenshot_artifacts],
            trace_artifact_ids=[trace_artifact.id],
            limitations=[
                *original["summary"].limitations,
                *reconstructed["summary"].limitations,
                *([matrix_limitation] if matrix_limitation else []),
            ],
        )
        comparison_artifact = store.write_artifact(
            job_id,
            kind="runtime_comparison",
            stage="runtime_compare",
            filename=f"runtime-comparison-{artifact_suffix}.json",
            content=comparison.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.runtime_compare",
            parent_artifact_ids=[*parents, scenario_artifact.id, trace_artifact.id, *[artifact.id for artifact in screenshot_artifacts]],
            attempt=attempt,
        )

        validation = RuntimeValidationRun(
            id=f"runtime_{uuid4().hex[:12]}",
            job_id=job_id,
            attempt=attempt,
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
            filename=f"runtime-compare-validation-{artifact_suffix}.json",
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
            attempt=attempt,
        )
        return RuntimeCompareResult(
            validation=validation,
            scenario_artifact=scenario_artifact,
            comparison_artifact=comparison_artifact,
            trace_artifact=trace_artifact,
            screenshot_artifacts=screenshot_artifacts,
            report_artifact=report_artifact,
            validations=[validation],
            scenario_artifacts=[scenario_artifact],
            comparison_artifacts=[comparison_artifact],
            trace_artifacts=[trace_artifact],
            report_artifacts=[report_artifact],
            message=f"Runtime compare completed with status {status}.",
        )

    def _scenario_matrix_from_config(
        self,
        *,
        job_id: str,
        config: dict[str, Any] | None,
    ) -> tuple[list[RuntimeCompareMatrixItem], dict[str, Any]]:
        payload = dict(config or {})
        is_matrix = any(key in payload for key in ("runtimeCompare", "runtimeScenario", "scenarios", "runtimeScenarios", "viewports", "viewportMatrix"))
        if "runtimeCompare" in payload and isinstance(payload["runtimeCompare"], dict):
            payload = dict(payload["runtimeCompare"])

        max_runs = self._max_matrix_runs_from_config(payload)
        selection = self._matrix_selection_from_config(payload)
        requested_matrix: list[RuntimeCompareMatrixItem] = []
        if is_matrix:
            scenario_payloads = payload.get("scenarios", payload.get("runtimeScenarios"))
            if scenario_payloads is None and isinstance(payload.get("runtimeScenario"), dict):
                scenario_payloads = [payload["runtimeScenario"]]
            if scenario_payloads is None:
                scenario_payloads = [{}]
            if isinstance(scenario_payloads, dict):
                scenario_payloads = [scenario_payloads]
            if not isinstance(scenario_payloads, list) or not scenario_payloads:
                scenario_payloads = [{}]

            global_viewports = self._viewports_from_config(
                payload.get("viewports", payload.get("viewportMatrix", payload.get("viewport"))),
                default=True,
            )
            global_policy = self._image_diff_policy_from_config(payload)
            for scenario_payload in scenario_payloads:
                scenario_dict = dict(scenario_payload) if isinstance(scenario_payload, dict) else {}
                scenario_viewports = self._viewports_from_config(
                    scenario_dict.get("viewports", scenario_dict.get("viewportMatrix", scenario_dict.get("viewport"))),
                    default=False,
                ) or global_viewports
                policy = self._image_diff_policy_from_config(scenario_dict, default=global_policy)
                for viewport in scenario_viewports:
                    scenario_config = dict(scenario_dict)
                    scenario_config["viewport"] = viewport.model_dump(by_alias=True)
                    requested_matrix.append(
                        RuntimeCompareMatrixItem(
                            scenario=self._scenario_from_config(job_id=job_id, config=scenario_config),
                            image_diff_policy=policy,
                            requested_index=len(requested_matrix),
                        )
                    )
        else:
            requested_matrix.append(
                RuntimeCompareMatrixItem(
                    scenario=self._scenario_from_config(job_id=job_id, config=payload),
                    image_diff_policy=self._image_diff_policy_from_config(payload),
                    requested_index=0,
                )
            )

        if not requested_matrix:
            requested_matrix = [
                RuntimeCompareMatrixItem(
                    scenario=self._scenario_from_config(job_id=job_id, config=None),
                    image_diff_policy=RuntimeImageDiffPolicy(),
                    requested_index=0,
                )
            ]
        selected_matrix = self._select_matrix_runs(requested_matrix, max_runs=max_runs, selection=selection)
        selected_indexes = {item.requested_index for item in selected_matrix}
        omitted_matrix = [item for item in requested_matrix if item.requested_index not in selected_indexes]
        matrix_plan = {
            "requestedRunCount": len(requested_matrix),
            "selectedRunCount": len(selected_matrix),
            "omittedRunCount": len(omitted_matrix),
            "maxMatrixRuns": max_runs,
            "matrixSelection": selection,
            "selectedRuns": [self._matrix_item_summary(item) for item in selected_matrix],
            "omittedRuns": [self._matrix_item_summary(item) for item in omitted_matrix],
        }
        return selected_matrix, matrix_plan

    def _viewports_from_config(self, value: Any, *, default: bool) -> list[RuntimeViewport]:
        if value is None:
            return [RuntimeViewport(**DEFAULT_VIEWPORT)] if default else []
        if isinstance(value, dict):
            return [RuntimeViewport.model_validate(value)]
        if isinstance(value, list):
            viewports = [RuntimeViewport.model_validate(item) for item in value if isinstance(item, dict)]
            return viewports or ([RuntimeViewport(**DEFAULT_VIEWPORT)] if default else [])
        return [RuntimeViewport(**DEFAULT_VIEWPORT)] if default else []

    def _image_diff_policy_from_config(
        self,
        payload: dict[str, Any],
        *,
        default: RuntimeImageDiffPolicy | None = None,
    ) -> RuntimeImageDiffPolicy:
        fallback = default or RuntimeImageDiffPolicy()
        image_diff = payload.get("imageDiff") if isinstance(payload, dict) else None
        image_diff = image_diff if isinstance(image_diff, dict) else {}
        pixel_threshold = self._int_config(
            self._first_config(
                image_diff,
                ("channelThreshold", "pixelThreshold", "pixelDiffThreshold"),
                self._first_config(payload, ("pixelDiffThreshold", "pixelThreshold"), fallback.pixel_threshold),
            ),
            default=fallback.pixel_threshold,
        )
        return RuntimeImageDiffPolicy(
            pixel_threshold=pixel_threshold,
            threshold_mode=self._threshold_mode_from_config(image_diff.get("thresholdMode"), default=fallback.threshold_mode),
            max_changed_pixel_ratio=self._float_config(
                image_diff.get("maxChangedPixelRatio"),
                default=fallback.max_changed_pixel_ratio,
            ),
        )

    def _first_config(self, payload: dict[str, Any], keys: tuple[str, ...], default: Any) -> Any:
        for key in keys:
            if key in payload:
                return payload[key]
        return default

    def _threshold_mode_from_config(self, value: Any, *, default: str) -> str:
        if value == "per_channel_rgba":
            return value
        return default

    def _int_config(self, value: Any, *, default: int) -> int:
        try:
            if isinstance(value, bool):
                return default
            return max(0, int(value))
        except (TypeError, ValueError):
            return default

    def _float_config(self, value: Any, *, default: float) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0.0, float(value))
        return default

    def _max_matrix_runs_from_config(self, payload: dict[str, Any]) -> int:
        return max(1, self._int_config(payload.get("maxMatrixRuns", payload.get("max_matrix_runs", 24)), default=24))

    def _matrix_selection_from_config(self, payload: dict[str, Any]) -> str:
        value = payload.get("matrixSelection", payload.get("matrix_selection", "balanced"))
        return value if value in {"balanced", "ordered"} else "balanced"

    def _select_matrix_runs(
        self,
        matrix: list[RuntimeCompareMatrixItem],
        *,
        max_runs: int,
        selection: str,
    ) -> list[RuntimeCompareMatrixItem]:
        if len(matrix) <= max_runs:
            return matrix
        if selection == "ordered":
            return matrix[:max_runs]
        if max_runs == 1:
            return [matrix[0]]
        selected_indexes = {
            round(position * (len(matrix) - 1) / (max_runs - 1))
            for position in range(max_runs)
        }
        for index in range(len(matrix)):
            if len(selected_indexes) >= max_runs:
                break
            selected_indexes.add(index)
        return [matrix[index] for index in sorted(selected_indexes)[:max_runs]]

    def _write_matrix_trace(
        self,
        *,
        job_id: str,
        store,
        parent_artifact_ids: list[str],
        matrix_plan: dict[str, Any],
        attempt: int,
    ) -> ArtifactRecord:
        payload = {
            "kind": "runtime_trace",
            "jobId": job_id,
            "target": "runtime_compare_matrix",
            "attempt": attempt,
            "pruned": matrix_plan["omittedRunCount"] > 0,
            **matrix_plan,
        }
        return store.write_artifact(
            job_id,
            kind="runtime_trace",
            stage="runtime_compare",
            filename=f"runtime-compare-matrix-attempt-{attempt}.json",
            content=self._json_bytes(payload),
            content_type="application/json",
            producer="worker.runtime_compare",
            parent_artifact_ids=parent_artifact_ids,
            attempt=attempt,
        )

    def _matrix_limitation(self, matrix_plan: dict[str, Any]) -> str | None:
        if matrix_plan["omittedRunCount"] <= 0:
            return None
        return (
            "Runtime compare matrix was pruned from "
            f"{matrix_plan['requestedRunCount']} to {matrix_plan['selectedRunCount']} run(s) "
            f"by maxMatrixRuns={matrix_plan['maxMatrixRuns']} using "
            f"{matrix_plan['matrixSelection']} selection; "
            f"{matrix_plan['omittedRunCount']} run(s) were omitted."
        )

    def _matrix_item_summary(self, item: RuntimeCompareMatrixItem) -> dict[str, Any]:
        viewport = item.scenario.viewport
        return {
            "requestedIndex": item.requested_index,
            "scenarioId": item.scenario.id,
            "scenarioName": item.scenario.name,
            "viewport": viewport.model_dump(by_alias=True) if viewport else None,
            "imageDiffPolicy": {
                "pixelThreshold": item.image_diff_policy.pixel_threshold,
                "thresholdMode": item.image_diff_policy.threshold_mode,
                "maxChangedPixelRatio": item.image_diff_policy.max_changed_pixel_ratio,
            },
        }

    def _scenario_from_config(self, *, job_id: str, config: dict[str, Any] | None) -> RuntimeScenario:
        payload = dict(config or {})
        wait_for = payload.get("waitFor", payload.get("wait_for"))
        if wait_for is None:
            wait_for = [{"kind": "load_state", "state": "load", "timeoutMs": self.timeout_ms}]
        viewport = self._viewports_from_config(payload.get("viewport"), default=True)[0]
        return RuntimeScenario(
            id=str(payload.get("id") or f"runtime_scenario_{uuid4().hex[:12]}"),
            job_id=job_id,
            name=str(payload.get("name") or "default-load"),
            entry_url=payload.get("entryUrl", payload.get("entry_url")),
            viewport=viewport,
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
        matrix_index: int,
        parent_artifact_ids: list[str],
        attempt: int,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        entry = self._resolve_scenario_entry(input_path=input_path, scenario=scenario)
        screenshot_path = temp_dir / f"runtime-compare-{matrix_index:02d}-{target}.png"
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
                    capture = self._browser_adapter_for_job(job_id=job_id, store=store).capture(
                        BrowserSmokeRequest(
                            entry_url=resolved_url,
                            screenshot_path=screenshot_path,
                            timeout_ms=scenario.timeout_ms,
                            job_id=job_id,
                            target=target,
                            attempt=attempt,
                            scenario=scenario,
                            network_policy=scenario.network_policy,
                            viewport=scenario.viewport,
                            source_root=entry.serve_root,
                            source_entry_path=entry.relative_entry,
                        )
                    )
                status = self._status_for_capture(capture)
                failure_class = "none" if status == "pass" else "runtime_error"
                screenshot_bytes = screenshot_path.read_bytes() if screenshot_path.exists() else None
                limitations.extend(capture.limitations)
                if screenshot_bytes is None:
                    limitations.append("Playwright completed without producing a comparison screenshot.")
            except RuntimeSmokeError as error:
                status = "best_effort"
                failure_class = error.failure_class
                capture = BrowserSmokeCapture(page_errors=[str(error)])
                limitations.append("Playwright runtime compare target capture could not complete.")

        screenshot_artifact = None
        screenshot_hash = None
        screenshot_size = None
        if screenshot_bytes is not None:
            screenshot_size = len(screenshot_bytes)
            screenshot_hash = hashlib.sha256(screenshot_bytes).hexdigest()
            screenshot_artifact = store.write_artifact(
                job_id,
                kind="runtime_screenshot",
                stage="runtime_compare",
                filename=f"runtime-compare-{matrix_index:02d}-{target}.png",
                content=screenshot_bytes,
                content_type="image/png",
                producer="worker.runtime_compare",
                parent_artifact_ids=parent_artifact_ids,
                attempt=attempt,
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
        return {
            "summary": summary,
            "screenshot": screenshot_artifact,
            "screenshot_hash": screenshot_hash,
            "screenshot_size": screenshot_size,
            "screenshot_bytes": screenshot_bytes,
            "execution_boundary": capture.execution_boundary,
        }

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
        scenario: RuntimeScenario,
        screenshot_diff: RuntimeScreenshotDiff,
    ) -> RuntimeDifferenceSet:
        original_requests = sorted(set(original.responses + original.failed_requests))
        reconstructed_requests = sorted(set(reconstructed.responses + reconstructed.failed_requests))
        original_console = sorted(set(original.console_messages + original.console_errors))
        reconstructed_console = sorted(set(reconstructed.console_messages + reconstructed.console_errors))
        changed_dom_fields = sorted(
            key
            for key in set(original.dom_summary) | set(reconstructed.dom_summary)
            if original.dom_summary.get(key) != reconstructed.dom_summary.get(key)
        )
        return RuntimeDifferenceSet(
            screenshot_changed=screenshot_diff.changed,
            dom_changed=bool(changed_dom_fields),
            network_changed=original_requests != reconstructed_requests,
            console_changed=original_console != reconstructed_console,
            original_only_requests=sorted(set(original_requests) - set(reconstructed_requests)),
            reconstructed_only_requests=sorted(set(reconstructed_requests) - set(original_requests)),
            original_only_console=sorted(set(original_console) - set(reconstructed_console)),
            reconstructed_only_console=sorted(set(reconstructed_console) - set(original_console)),
            changed_dom_fields=changed_dom_fields,
            screenshot_diff=screenshot_diff,
            dom_differences=self._dom_differences(original.dom_summary, reconstructed.dom_summary),
            network_diff=self._collection_diff(original_requests, reconstructed_requests, "network"),
            console_diff=self._collection_diff(original_console, reconstructed_console, "console"),
            comparison_scope=RuntimeComparisonScope(
                scenario_name=scenario.name,
                network_policy=scenario.network_policy,
                timeout_ms=scenario.timeout_ms,
                viewport=scenario.viewport or RuntimeViewport(**DEFAULT_VIEWPORT),
            ),
        )

    def _screenshot_diff(
        self,
        *,
        job_id: str,
        store,
        changed: bool | None,
        original_hash: str | None,
        reconstructed_hash: str | None,
        original_size: int | None,
        reconstructed_size: int | None,
        original_bytes: bytes | None,
        reconstructed_bytes: bytes | None,
        image_diff_policy: RuntimeImageDiffPolicy,
        filename_suffix: str,
        parent_artifact_ids: list[str],
        attempt: int,
    ) -> tuple[RuntimeScreenshotDiff, ArtifactRecord | None]:
        hash_changed = (
            None
            if original_hash is None or reconstructed_hash is None
            else original_hash != reconstructed_hash
        )
        original_format = _detect_image_format(original_bytes)
        reconstructed_format = _detect_image_format(reconstructed_bytes)
        if original_bytes is None or reconstructed_bytes is None:
            return (
                RuntimeScreenshotDiff(
                    changed=changed if changed is not None else hash_changed,
                    original_hash=original_hash,
                    reconstructed_hash=reconstructed_hash,
                    original_size_bytes=original_size,
                    reconstructed_size_bytes=reconstructed_size,
                    original_format=original_format,
                    reconstructed_format=reconstructed_format,
                    pixel_diff_status="unavailable",
                    threshold=image_diff_policy.pixel_threshold,
                    threshold_mode=image_diff_policy.threshold_mode,
                    max_changed_pixel_ratio=image_diff_policy.max_changed_pixel_ratio,
                    reason="One or both runtime comparison screenshots were unavailable.",
                ),
                None,
            )
        if original_format != "png" or reconstructed_format != "png":
            return (
                RuntimeScreenshotDiff(
                    changed=changed if changed is not None else hash_changed,
                    original_hash=original_hash,
                    reconstructed_hash=reconstructed_hash,
                    original_size_bytes=original_size,
                    reconstructed_size_bytes=reconstructed_size,
                    original_format=original_format,
                    reconstructed_format=reconstructed_format,
                    pixel_diff_status="unavailable",
                    threshold=image_diff_policy.pixel_threshold,
                    threshold_mode=image_diff_policy.threshold_mode,
                    max_changed_pixel_ratio=image_diff_policy.max_changed_pixel_ratio,
                    reason=(
                        "Runtime comparison pixel diff currently supports PNG screenshots only; "
                        f"detected original={original_format or 'missing'} and "
                        f"reconstructed={reconstructed_format or 'missing'}."
                    ),
                ),
                None,
            )

        try:
            original_image = _decode_png_rgba(original_bytes)
            reconstructed_image = _decode_png_rgba(reconstructed_bytes)
        except PngDecodeError as error:
            return (
                RuntimeScreenshotDiff(
                    changed=changed if changed is not None else hash_changed,
                    original_hash=original_hash,
                    reconstructed_hash=reconstructed_hash,
                    original_size_bytes=original_size,
                    reconstructed_size_bytes=reconstructed_size,
                    original_format=original_format,
                    reconstructed_format=reconstructed_format,
                    pixel_diff_status="unavailable",
                    threshold=image_diff_policy.pixel_threshold,
                    threshold_mode=image_diff_policy.threshold_mode,
                    max_changed_pixel_ratio=image_diff_policy.max_changed_pixel_ratio,
                    reason=str(error),
                ),
                None,
            )

        if original_image.width != reconstructed_image.width or original_image.height != reconstructed_image.height:
            return (
                RuntimeScreenshotDiff(
                    changed=True,
                    original_hash=original_hash,
                    reconstructed_hash=reconstructed_hash,
                    original_size_bytes=original_size,
                    reconstructed_size_bytes=reconstructed_size,
                    original_format=original_format,
                    reconstructed_format=reconstructed_format,
                    pixel_diff_status="unavailable",
                    threshold=image_diff_policy.pixel_threshold,
                    threshold_mode=image_diff_policy.threshold_mode,
                    max_changed_pixel_ratio=image_diff_policy.max_changed_pixel_ratio,
                    width=original_image.width,
                    height=original_image.height,
                    reason=(
                        "Runtime comparison screenshots have different dimensions: "
                        f"{original_image.width}x{original_image.height} vs "
                        f"{reconstructed_image.width}x{reconstructed_image.height}."
                    ),
                ),
                None,
            )

        diff_pixels = bytearray(len(original_image.rgba))
        changed_pixels = 0
        for index in range(0, len(original_image.rgba), 4):
            if any(
                abs(original_image.rgba[index + channel] - reconstructed_image.rgba[index + channel])
                > image_diff_policy.pixel_threshold
                for channel in range(4)
            ):
                changed_pixels += 1
                diff_pixels[index : index + 4] = b"\xff\x00\x00\xff"
            else:
                diff_pixels[index : index + 4] = b"\x00\x00\x00\x00"

        diff_bytes = _encode_png_rgba(original_image.width, original_image.height, bytes(diff_pixels))
        diff_artifact = store.write_artifact(
            job_id,
            kind="runtime_screenshot",
            stage="runtime_compare",
            filename=f"runtime-compare-{filename_suffix}-pixel-diff.png",
            content=diff_bytes,
            content_type="image/png",
            producer="worker.runtime_compare",
            parent_artifact_ids=parent_artifact_ids,
            attempt=attempt,
        )
        pixel_count = original_image.width * original_image.height
        return (
            RuntimeScreenshotDiff(
                changed=changed_pixels > 0,
                original_hash=original_hash,
                reconstructed_hash=reconstructed_hash,
                original_size_bytes=original_size,
                reconstructed_size_bytes=reconstructed_size,
                original_format=original_format,
                reconstructed_format=reconstructed_format,
                pixel_diff_status="compared",
                pixel_count=pixel_count,
                changed_pixel_count=changed_pixels,
                changed_pixel_ratio=changed_pixels / pixel_count if pixel_count else 0,
                threshold=image_diff_policy.pixel_threshold,
                threshold_mode=image_diff_policy.threshold_mode,
                max_changed_pixel_ratio=image_diff_policy.max_changed_pixel_ratio,
                width=original_image.width,
                height=original_image.height,
                diff_artifact_id=diff_artifact.id,
                reason=None,
            ),
            diff_artifact,
        )

    def _dom_differences(self, original: dict[str, Any], reconstructed: dict[str, Any]) -> list[RuntimeDomDifference]:
        original_flat = self._flatten_dom_summary(original)
        reconstructed_flat = self._flatten_dom_summary(reconstructed)
        differences: list[RuntimeDomDifference] = []
        for path in sorted(set(original_flat) | set(reconstructed_flat)):
            original_value = original_flat.get(path)
            reconstructed_value = reconstructed_flat.get(path)
            if original_value == reconstructed_value:
                continue
            differences.append(
                RuntimeDomDifference(
                    path=path,
                    original=original_value,
                    reconstructed=reconstructed_value,
                    summary=f"DOM summary path {path} changed.",
                )
            )
        return differences[:80]

    def _flatten_dom_summary(self, value: Any, prefix: str = "", depth: int = 0) -> dict[str, Any]:
        if depth >= 4 or value is None or isinstance(value, (str, int, float, bool)):
            return {prefix or "value": value}
        if isinstance(value, dict):
            flattened: dict[str, Any] = {}
            for key in sorted(value):
                path = f"{prefix}.{key}" if prefix else str(key)
                flattened.update(self._flatten_dom_summary(value[key], path, depth + 1))
            return flattened
        if isinstance(value, list):
            flattened = {}
            for index, item in enumerate(value[:12]):
                path = f"{prefix}[{index}]" if prefix else f"[{index}]"
                flattened.update(self._flatten_dom_summary(item, path, depth + 1))
            if len(value) > 12:
                flattened[f"{prefix}.truncated" if prefix else "truncated"] = len(value) - 12
            return flattened
        return {prefix or "value": str(value)}

    def _collection_diff(
        self,
        original_items: list[str],
        reconstructed_items: list[str],
        group_kind: str,
    ) -> RuntimeCollectionDiff:
        original_set = set(original_items)
        reconstructed_set = set(reconstructed_items)
        combined = sorted(original_set | reconstructed_set)
        return RuntimeCollectionDiff(
            changed=original_set != reconstructed_set,
            original_count=len(original_items),
            reconstructed_count=len(reconstructed_items),
            shared=sorted(original_set & reconstructed_set),
            original_only=sorted(original_set - reconstructed_set),
            reconstructed_only=sorted(reconstructed_set - original_set),
            groups=self._group_runtime_lines(combined, group_kind),
        )

    def _group_runtime_lines(self, items: list[str], group_kind: str) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for item in items:
            key = self._runtime_line_group(item, group_kind)
            groups.setdefault(key, []).append(item)
        return groups

    def _runtime_line_group(self, item: str, group_kind: str) -> str:
        if group_kind == "network":
            first_token = item.split(" ", 1)[0]
            if first_token.isdigit():
                status = int(first_token)
                return f"status_{status // 100}xx"
            if item.startswith("network_policy_denied"):
                return "policy_denied"
            if "request failed" in item:
                return "request_failed"
            return "network_other"
        message_type = item.split(":", 1)[0].strip().lower()
        if message_type in {"error", "warning", "warn", "log", "info", "debug"}:
            return message_type
        return "console_other"

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
