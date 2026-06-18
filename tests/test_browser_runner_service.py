import base64
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from apps.api.app.auth import create_auth_token
from apps.api.app.models import BrowserRunRequest
from apps.browser_runner.app.main import BrowserRunnerQueue, SqlAlchemyBrowserRunQueueBackend, create_app, normalize_queue_backend
from apps.worker.worker.runtime_smoke import BrowserSmokeCapture, BrowserSmokeRequest


class ServiceFakeBrowserAdapter:
    def __init__(self, *, delay_seconds: float = 0.0, fail_times: int = 0) -> None:
        self.requests: list[BrowserSmokeRequest] = []
        self.delay_seconds = delay_seconds
        self.fail_times = fail_times
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        with self._lock:
            self.requests.append(request)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            request_index = len(self.requests)
        try:
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            if request_index <= self.fail_times:
                raise RuntimeError(f"planned failure {request_index}")
            request.screenshot_path.write_bytes(b"\x89PNG\r\n\x1a\nservice")
            return BrowserSmokeCapture(
                console_messages=["log: service"],
                responses=[f"200 {request.entry_url}"],
                dom_summary={"title": "service", "nodeCount": 1},
            )
        finally:
            with self._lock:
                self.active -= 1


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
                self.assertEqual(summary["attempt"], 1)
                self.assertEqual(summary["maxAttempts"], 3)
                self.assertEqual(summary["queueBackend"], "sqlite")
                self.assertEqual(summary["result"]["executionBoundary"]["queueBackend"], "sqlite")
                self.assertEqual(summary["result"]["executionBoundary"]["runAttempt"], 1)
            queue.close()

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
                    self.assertEqual(summary.attempt, 1)
                    queue.close()

    def test_browser_run_survives_queue_reconstruction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "browser-runs.sqlite3"
            first_queue = BrowserRunnerQueue(
                browser_adapter=ServiceFakeBrowserAdapter(),
                max_workers=1,
                workdir=Path(temp_dir),
                db_path=db_path,
                auto_start=False,
            )
            run = first_queue.submit(self._request("job_persist"))
            first_queue.close()

            second_adapter = ServiceFakeBrowserAdapter()
            second_queue = BrowserRunnerQueue(
                browser_adapter=second_adapter,
                max_workers=1,
                workdir=Path(temp_dir),
                db_path=db_path,
            )
            summary = self._wait_for_queue_run(second_queue, run.id)
            self.assertEqual(summary.status, "pass")
            self.assertEqual(summary.attempt, 1)
            self.assertEqual(len(second_adapter.requests), 1)
            second_queue.close()

    def test_expired_running_lease_is_recovered_and_retried(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "browser-runs.sqlite3"
            queue = BrowserRunnerQueue(
                browser_adapter=ServiceFakeBrowserAdapter(),
                max_workers=1,
                workdir=Path(temp_dir),
                db_path=db_path,
                lease_seconds=1,
                auto_start=False,
            )
            run = queue.submit(self._request("job_recover"))
            claimed = queue._claim(run.id)
            self.assertIsNotNone(claimed)
            expired = "2026-01-01T00:00:00+00:00"
            queue._update(run.id, lease_expires_at=expired)
            recovered = queue.recover_expired_leases(now="2026-01-01T00:00:02+00:00", schedule=False)
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].status, "queued")
            self.assertTrue(recovered[0].lease_recovered)
            queue._discard_submitted(run.id)
            queue._execute(run.id)
            summary = queue.get(run.id)
            self.assertIsNotNone(summary)
            self.assertEqual(summary.status, "pass")
            self.assertEqual(summary.attempt, 2)
            self.assertTrue(summary.lease_recovered)
            queue.close()

    def test_sqlalchemy_backend_shares_runs_between_queue_instances(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            engine = create_engine(f"sqlite:///{(root / 'shared-browser-runs.db').as_posix()}", future=True)
            first_queue = BrowserRunnerQueue(
                browser_adapter=ServiceFakeBrowserAdapter(),
                backend=SqlAlchemyBrowserRunQueueBackend(engine=engine),
                max_workers=1,
                workdir=root,
                auto_start=False,
            )
            run = first_queue.submit(self._request("job_shared_db"))
            first_queue.close()

            second_adapter = ServiceFakeBrowserAdapter()
            second_queue = BrowserRunnerQueue(
                browser_adapter=second_adapter,
                backend=SqlAlchemyBrowserRunQueueBackend(engine=engine),
                max_workers=1,
                workdir=root,
            )
            try:
                summary = self._wait_for_queue_run(second_queue, run.id)
                self.assertEqual(summary.status, "pass")
                self.assertEqual(summary.queue_backend, "postgresql")
                self.assertEqual(summary.result.execution_boundary["queueBackend"], "postgresql")
                self.assertEqual(len(second_adapter.requests), 1)
            finally:
                second_queue.close()
                engine.dispose()

    def test_sqlalchemy_backend_claim_due_is_atomic_across_instances(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            engine = create_engine(f"sqlite:///{(root / 'shared-claim.db').as_posix()}", future=True)
            first_backend = SqlAlchemyBrowserRunQueueBackend(engine=engine)
            second_backend = SqlAlchemyBrowserRunQueueBackend(engine=engine)
            queue = BrowserRunnerQueue(
                browser_adapter=ServiceFakeBrowserAdapter(),
                backend=first_backend,
                max_workers=1,
                workdir=root,
                auto_start=False,
            )
            try:
                run = queue.submit(self._request("job_claim"))
                claimed = first_backend.claim_due(
                    now="2099-01-01T00:00:00+00:00",
                    worker_id="browser-runner-a",
                    lease_seconds=60,
                    excluded_run_ids=set(),
                )
                self.assertIsNotNone(claimed)
                self.assertEqual(claimed.id, run.id)
                self.assertIsNone(
                    second_backend.claim_due(
                        now="2099-01-01T00:00:00+00:00",
                        worker_id="browser-runner-b",
                        lease_seconds=60,
                        excluded_run_ids=set(),
                    )
                )
                persisted = second_backend.get(run.id)
                self.assertEqual(persisted.status, "running")
                self.assertEqual(persisted.lease_owner, "browser-runner-a")
            finally:
                queue.close()
                engine.dispose()

    def test_sqlalchemy_backend_recovers_expired_lease_with_audit_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            engine = create_engine(f"sqlite:///{(root / 'shared-recovery.db').as_posix()}", future=True)
            backend = SqlAlchemyBrowserRunQueueBackend(engine=engine)
            queue = BrowserRunnerQueue(
                browser_adapter=ServiceFakeBrowserAdapter(),
                backend=backend,
                max_workers=1,
                workdir=root,
                lease_seconds=1,
                auto_start=False,
            )
            try:
                run = queue.submit(self._request("job_shared_recover"))
                claimed = backend.claim(run.id, worker_id="browser-runner-a", lease_seconds=1)
                self.assertIsNotNone(claimed)
                backend.update(run.id, lease_expires_at="2026-01-01T00:00:00+00:00")

                recovered = queue.recover_expired_leases(now="2026-01-01T00:00:02+00:00", schedule=False)
                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].status, "queued")
                self.assertEqual(recovered[0].queue_backend, "postgresql")
                self.assertTrue(recovered[0].lease_recovered)
                self.assertIsNone(recovered[0].lease_owner)
            finally:
                queue.close()
                engine.dispose()

    def test_browser_run_retries_transient_capture_failures(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = ServiceFakeBrowserAdapter(fail_times=1)
            queue = BrowserRunnerQueue(
                browser_adapter=adapter,
                max_workers=1,
                workdir=Path(temp_dir),
                max_attempts=2,
                retry_backoff_seconds=0,
            )
            run = queue.submit(self._request("job_retry"))
            summary = self._wait_for_queue_run(queue, run.id)
            self.assertEqual(summary.status, "pass")
            self.assertEqual(summary.attempt, 2)
            self.assertEqual(len(adapter.requests), 2)
            self.assertEqual(summary.result.execution_boundary["maxAttempts"], 2)
            queue.close()

    def test_browser_runner_respects_configured_worker_concurrency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = ServiceFakeBrowserAdapter(delay_seconds=0.05)
            queue = BrowserRunnerQueue(browser_adapter=adapter, max_workers=1, workdir=Path(temp_dir))
            runs = [queue.submit(self._request(f"job_concurrency_{index}")) for index in range(3)]
            for run in runs:
                summary = self._wait_for_queue_run(queue, run.id)
                self.assertEqual(summary.status, "pass")
            self.assertEqual(adapter.max_active, 1)
            queue.close()

    def test_queue_backend_aliases_normalize_to_supported_backends(self):
        self.assertEqual(normalize_queue_backend("postgres"), "postgresql")
        self.assertEqual(normalize_queue_backend("shared-db"), "postgresql")
        self.assertEqual(normalize_queue_backend("rabbitmq"), "sqlite")

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

    def _request(self, job_id: str) -> BrowserRunRequest:
        return BrowserRunRequest(
            job_id=job_id,
            target="reconstructed",
            attempt=0,
            entry_url="about:blank",
            timeout_ms=1000,
            network_policy="deny",
            source_archive=self._source_archive(),
        )

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
