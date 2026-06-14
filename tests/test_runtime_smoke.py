import json
import tempfile
import unittest
from pathlib import Path

from apps.api.app.models import CreateJobRequest
from apps.api.app.store import create_store
from apps.worker.worker.runtime_smoke import BrowserSmokeCapture, BrowserSmokeRequest, RuntimeSmokeRunner


class FakeBrowserAdapter:
    def __init__(self, capture: BrowserSmokeCapture | None = None) -> None:
        self.capture_result = capture or BrowserSmokeCapture()
        self.requests: list[BrowserSmokeRequest] = []

    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        self.requests.append(request)
        request.screenshot_path.write_bytes(b"\x89PNG\r\n\x1a\nruntime-smoke")
        return self.capture_result


class RuntimeSmokeRunnerTest(unittest.TestCase):
    def test_runtime_smoke_persists_report_trace_and_screenshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "dist"
            input_root.mkdir()
            (input_root / "index.html").write_text("<h1>Runtime fixture</h1>", encoding="utf-8")
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                adapter = FakeBrowserAdapter()
                result = RuntimeSmokeRunner(browser_adapter=adapter).run(
                    job_id=job.id,
                    store=store,
                    input_path=input_root,
                )

                self.assertEqual(result.validation.status, "pass")
                self.assertEqual(len(adapter.requests), 1)
                self.assertTrue(adapter.requests[0].entry_url.startswith("http://127.0.0.1:"))
                self.assertIsNotNone(result.screenshot_artifact)

                runtime_reports = store.list_artifacts(job.id, kind="runtime_validation")
                traces = store.list_artifacts(job.id, kind="runtime_trace")
                screenshots = store.list_artifacts(job.id, kind="runtime_screenshot")
                self.assertEqual(len(runtime_reports), 1)
                self.assertEqual(len(traces), 1)
                self.assertEqual(len(screenshots), 1)
                self.assertEqual(Path(screenshots[0].storage_uri).read_bytes(), b"\x89PNG\r\n\x1a\nruntime-smoke")

                report = json.loads(store.read_artifact(job.id, runtime_reports[0].id))
                self.assertEqual(report["status"], "pass")
                self.assertEqual(report["traceArtifactId"], traces[0].id)
                self.assertEqual(report["screenshotArtifactIds"], [screenshots[0].id])
            finally:
                store.close()

    def test_runtime_smoke_records_best_effort_when_html_entry_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "dist"
            input_root.mkdir()
            (input_root / "bundle.js").write_text("console.log('bundle only')", encoding="utf-8")
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                adapter = FakeBrowserAdapter()
                result = RuntimeSmokeRunner(browser_adapter=adapter).run(
                    job_id=job.id,
                    store=store,
                    input_path=input_root,
                )

                self.assertEqual(result.validation.status, "best_effort")
                self.assertEqual(adapter.requests, [])
                self.assertEqual(result.validation.screenshot_artifact_ids, [])

                trace = json.loads(store.read_artifact(job.id, result.trace_artifact.id))
                self.assertEqual(trace["failureClass"], "invalid_input")
                self.assertTrue(any("No HTML entry" in limitation for limitation in trace["limitations"]))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
