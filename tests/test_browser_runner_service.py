import base64
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.app.auth import create_auth_token
from apps.api.app.models import BrowserRunRequest
from apps.browser_runner.app.main import BrowserRunnerQueue, create_app
from apps.worker.worker.runtime_smoke import BrowserSmokeCapture, BrowserSmokeRequest


class ServiceFakeBrowserAdapter:
    def __init__(self) -> None:
        self.requests: list[BrowserSmokeRequest] = []

    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        self.requests.append(request)
        request.screenshot_path.write_bytes(b"\x89PNG\r\n\x1a\nservice")
        return BrowserSmokeCapture(
            console_messages=["log: service"],
            responses=[f"200 {request.entry_url}"],
            dom_summary={"title": "service", "nodeCount": 1},
        )


class BrowserRunnerServiceTest(unittest.TestCase):
    def test_browser_run_requires_worker_service_token_and_completes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = ServiceFakeBrowserAdapter()
            queue = BrowserRunnerQueue(browser_adapter=adapter, max_workers=1, workdir=Path(temp_dir))
            app = create_app(queue=queue)
            token = create_auth_token(
                subject="worker-service",
                kind="service",
                service_roles=["worker"],
                projects={"proj": "maintainer"},
                secret="test-secret",
                expires_at=4102444800,
            )
            archive = self._source_archive()
            request = BrowserRunRequest(
                job_id="job_service",
                target="reconstructed",
                attempt=0,
                entry_url="about:blank",
                timeout_ms=1000,
                network_policy="deny",
                source_archive=archive,
            )

            with patch.dict(os.environ, {"AI_JSUNPACK_AUTH_SECRET": "test-secret"}):
                client = TestClient(app)
                denied = client.post("/browser-runs", json=request.model_dump(by_alias=True))
                self.assertEqual(denied.status_code, 401)

                created = client.post(
                    "/browser-runs",
                    json=request.model_dump(by_alias=True),
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(created.status_code, 200)
                run_id = created.json()["id"]

                summary = self._wait_for_run(client, run_id, token)
                self.assertEqual(summary["status"], "pass")
                self.assertEqual(summary["result"]["executionBoundary"]["runnerKind"], "remote_browser_runner")
                self.assertEqual(
                    base64.b64decode(summary["result"]["screenshotBase64"].encode("ascii")),
                    b"\x89PNG\r\n\x1a\nservice",
                )
                self.assertEqual(len(adapter.requests), 1)
                self.assertTrue(adapter.requests[0].entry_url.startswith("http://127.0.0.1:"))

    def test_browser_run_rejects_unsafe_source_archive_paths_before_capture(self):
        unsafe_archives = [
            self._source_archive(member_path="../evil.html"),
            self._source_archive(member_path="assets\\..\\evil.html"),
            self._source_archive(member_path="C:/tmp/evil.html"),
            self._source_archive(entry_path="../index.html"),
        ]
        for archive in unsafe_archives:
            with self.subTest(archive=archive["entryPath"]):
                with tempfile.TemporaryDirectory() as temp_dir:
                    adapter = ServiceFakeBrowserAdapter()
                    queue = BrowserRunnerQueue(browser_adapter=adapter, max_workers=1, workdir=Path(temp_dir))
                    run = queue.submit(
                        BrowserRunRequest(
                            job_id="job_unsafe",
                            target="reconstructed",
                            attempt=0,
                            entry_url="about:blank",
                            timeout_ms=1000,
                            network_policy="deny",
                            source_archive=archive,
                        )
                    )

                    summary = self._wait_for_queue_run(queue, run.id)
                    self.assertEqual(summary.status, "best_effort")
                    self.assertIsNotNone(summary.result)
                    self.assertEqual(adapter.requests, [])
                    self.assertIn("Unsafe source archive member path", summary.result.page_errors[0])

    def _wait_for_run(self, client: TestClient, run_id: str, token: str) -> dict:
        for _ in range(50):
            response = client.get(
                f"/browser-runs/{run_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            if payload["status"] in {"pass", "fail", "best_effort"}:
                return payload
            time.sleep(0.02)
        self.fail("Browser run did not complete")

    def _wait_for_queue_run(self, queue: BrowserRunnerQueue, run_id: str):
        for _ in range(50):
            summary = queue.get(run_id)
            self.assertIsNotNone(summary)
            if summary and summary.status in {"pass", "fail", "best_effort"}:
                return summary
            time.sleep(0.02)
        self.fail("Browser run did not complete")

    def _source_archive(self, *, member_path: str = "index.html", entry_path: str = "index.html"):
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(member_path, "<h1>service</h1>")
        return {
            "contentBase64": base64.b64encode(buffer.getvalue()).decode("ascii"),
            "entryPath": entry_path,
        }


if __name__ == "__main__":
    unittest.main()
