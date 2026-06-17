import json
import tempfile
import unittest
from pathlib import Path

from apps.api.app.models import CreateJobRequest
from apps.api.app.store import create_store
from apps.worker.worker.runtime_smoke import (
    BrowserSmokeCapture,
    BrowserSmokeRequest,
    RuntimeCompareReviewGate,
    RuntimeCompareRunner,
    RuntimeSmokeRunner,
    _encode_png_rgba,
)


class FakeBrowserAdapter:
    def __init__(self, capture: BrowserSmokeCapture | None = None) -> None:
        self.capture_result = capture or BrowserSmokeCapture()
        self.requests: list[BrowserSmokeRequest] = []

    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        self.requests.append(request)
        request.screenshot_path.write_bytes(b"\x89PNG\r\n\x1a\nruntime-smoke")
        return self.capture_result


class SequencedBrowserAdapter:
    def __init__(self) -> None:
        self.requests: list[BrowserSmokeRequest] = []

    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        index = len(self.requests)
        self.requests.append(request)
        target = "original" if index == 0 else "reconstructed"
        request.screenshot_path.write_bytes(f"\x89PNG\r\n\x1a\nruntime-{target}".encode("utf-8"))
        return BrowserSmokeCapture(
            console_messages=[f"log: {target}"],
            responses=[f"200 {request.entry_url}"],
            dom_summary={
                "title": target,
                "nodeCount": 3,
                "textLength": len(target),
            },
        )


class PixelDiffBrowserAdapter:
    def __init__(self) -> None:
        self.requests: list[BrowserSmokeRequest] = []

    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        index = len(self.requests)
        self.requests.append(request)
        if index % 2 == 0:
            pixels = b"\xff\x00\x00\xff\x00\xff\x00\xff"
            title = "original"
        else:
            pixels = b"\xff\x00\x00\xff\x00\x00\xff\xff"
            title = "reconstructed"
        request.screenshot_path.write_bytes(_encode_png_rgba(2, 1, pixels))
        return BrowserSmokeCapture(
            responses=[f"200 {request.entry_url}"],
            dom_summary={"title": title, "nodeCount": 2},
        )


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
                self.assertIn("ai-jsunpack-sandbox-", str(adapter.requests[0].screenshot_path))
                self.assertFalse(adapter.requests[0].screenshot_path.exists())
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

    def test_runtime_compare_persists_scenario_comparison_trace_and_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = root / "original"
            reconstructed_root = root / "reconstructed"
            original_root.mkdir()
            reconstructed_root.mkdir()
            (original_root / "index.html").write_text("<h1>Original</h1>", encoding="utf-8")
            (reconstructed_root / "index.html").write_text("<h1>Reconstructed</h1>", encoding="utf-8")
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                adapter = SequencedBrowserAdapter()
                result = RuntimeCompareRunner(browser_adapter=adapter).run_compare(
                    job_id=job.id,
                    store=store,
                    original_input_path=original_root,
                    reconstructed_input_path=reconstructed_root,
                    scenario_config={
                        "name": "fixture scenario",
                        "waitFor": [{"kind": "selector", "selector": "body", "timeoutMs": 1000}],
                        "assertions": [{"kind": "selector_visible", "selector": "body"}],
                        "networkPolicy": "deny",
                    },
                )

                self.assertEqual(result.validation.status, "pass")
                self.assertEqual(len(adapter.requests), 2)
                self.assertEqual(adapter.requests[0].scenario.name, "fixture scenario")
                self.assertEqual(adapter.requests[0].network_policy, "deny")
                self.assertEqual(len(result.screenshot_artifacts), 2)

                scenarios = store.list_artifacts(job.id, kind="runtime_scenario")
                comparisons = store.list_artifacts(job.id, kind="runtime_comparison")
                traces = store.list_artifacts(job.id, kind="runtime_trace")
                reports = store.list_artifacts(job.id, kind="runtime_validation")
                screenshots = store.list_artifacts(job.id, kind="runtime_screenshot")
                self.assertEqual(len(scenarios), 1)
                self.assertEqual(len(comparisons), 1)
                self.assertEqual(len(traces), 1)
                self.assertEqual(len(reports), 1)
                self.assertEqual(len(screenshots), 2)

                scenario_payload = json.loads(store.read_artifact(job.id, scenarios[0].id))
                comparison_payload = json.loads(store.read_artifact(job.id, comparisons[0].id))
                validation_payload = json.loads(store.read_artifact(job.id, reports[0].id))
                self.assertEqual(scenario_payload["networkPolicy"], "deny")
                self.assertEqual(comparison_payload["scenarioArtifactId"], scenarios[0].id)
                self.assertEqual(comparison_payload["differences"]["domChanged"], True)
                self.assertEqual(comparison_payload["differences"]["consoleChanged"], True)
                self.assertEqual(comparison_payload["differences"]["screenshotDiff"]["changed"], True)
                self.assertEqual(comparison_payload["differences"]["screenshotDiff"]["pixelDiffStatus"], "unavailable")
                self.assertEqual(comparison_payload["differences"]["comparisonScope"]["scenarioName"], "fixture scenario")
                self.assertEqual(comparison_payload["differences"]["comparisonScope"]["viewport"]["width"], 1365)
                self.assertIn("title", [item["path"] for item in comparison_payload["differences"]["domDifferences"]])
                self.assertEqual(comparison_payload["differences"]["networkDiff"]["changed"], True)
                self.assertIn("status_2xx", comparison_payload["differences"]["networkDiff"]["groups"])
                self.assertEqual(comparison_payload["differences"]["consoleDiff"]["changed"], True)
                self.assertIn("log", comparison_payload["differences"]["consoleDiff"]["groups"])
                self.assertEqual(validation_payload["comparisonArtifactId"], comparisons[0].id)
                self.assertEqual(set(validation_payload["screenshotArtifactIds"]), {artifact.id for artifact in screenshots})
            finally:
                store.close()

    def test_runtime_compare_records_real_pixel_diff_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = root / "original"
            reconstructed_root = root / "reconstructed"
            original_root.mkdir()
            reconstructed_root.mkdir()
            (original_root / "index.html").write_text("<h1>Original</h1>", encoding="utf-8")
            (reconstructed_root / "index.html").write_text("<h1>Reconstructed</h1>", encoding="utf-8")
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                adapter = PixelDiffBrowserAdapter()
                result = RuntimeCompareRunner(browser_adapter=adapter).run_compare(
                    job_id=job.id,
                    store=store,
                    original_input_path=original_root,
                    reconstructed_input_path=reconstructed_root,
                    scenario_config={"name": "pixel scenario", "viewport": {"name": "tiny", "width": 2, "height": 1}},
                )

                comparisons = store.list_artifacts(job.id, kind="runtime_comparison")
                screenshots = store.list_artifacts(job.id, kind="runtime_screenshot")
                comparison_payload = json.loads(store.read_artifact(job.id, comparisons[0].id))
                screenshot_diff = comparison_payload["differences"]["screenshotDiff"]
                self.assertEqual(len(adapter.requests), 2)
                self.assertEqual(adapter.requests[0].viewport.width, 2)
                self.assertEqual(adapter.requests[0].viewport.height, 1)
                self.assertEqual(result.validation.status, "pass")
                self.assertEqual(len(result.screenshot_artifacts), 3)
                self.assertEqual(len(screenshots), 3)
                self.assertEqual(screenshot_diff["pixelDiffStatus"], "compared")
                self.assertEqual(screenshot_diff["pixelCount"], 2)
                self.assertEqual(screenshot_diff["changedPixelCount"], 1)
                self.assertEqual(screenshot_diff["changedPixelRatio"], 0.5)
                self.assertEqual(screenshot_diff["width"], 2)
                self.assertEqual(screenshot_diff["height"], 1)
                self.assertIn(screenshot_diff["diffArtifactId"], [artifact.id for artifact in screenshots])
                self.assertIn(screenshot_diff["diffArtifactId"], comparison_payload["screenshotArtifactIds"])
            finally:
                store.close()

    def test_runtime_compare_runs_scenario_viewport_matrix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = root / "original"
            reconstructed_root = root / "reconstructed"
            original_root.mkdir()
            reconstructed_root.mkdir()
            (original_root / "index.html").write_text("<h1>Original</h1>", encoding="utf-8")
            (reconstructed_root / "index.html").write_text("<h1>Reconstructed</h1>", encoding="utf-8")
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                adapter = SequencedBrowserAdapter()
                result = RuntimeCompareRunner(browser_adapter=adapter).run_compare(
                    job_id=job.id,
                    store=store,
                    original_input_path=original_root,
                    reconstructed_input_path=reconstructed_root,
                    scenario_config={
                        "scenarios": [{"name": "load"}, {"name": "body", "waitFor": [{"kind": "selector", "selector": "body"}]}],
                        "viewports": [
                            {"name": "mobile", "width": 375, "height": 667},
                            {"name": "desktop", "width": 1365, "height": 768},
                        ],
                    },
                )

                scenarios = store.list_artifacts(job.id, kind="runtime_scenario")
                comparisons = store.list_artifacts(job.id, kind="runtime_comparison")
                validations = store.list_artifacts(job.id, kind="runtime_validation")
                screenshots = store.list_artifacts(job.id, kind="runtime_screenshot")
                comparison_payloads = [json.loads(store.read_artifact(job.id, artifact.id)) for artifact in comparisons]
                scope_labels = {
                    (
                        payload["differences"]["comparisonScope"]["scenarioName"],
                        payload["differences"]["comparisonScope"]["viewport"]["name"],
                    )
                    for payload in comparison_payloads
                }

                self.assertEqual(len(adapter.requests), 8)
                self.assertEqual(len(result.validations), 4)
                self.assertEqual(len(result.comparison_artifacts), 4)
                self.assertEqual(len(scenarios), 4)
                self.assertEqual(len(comparisons), 4)
                self.assertEqual(len(validations), 4)
                self.assertEqual(len(screenshots), 8)
                self.assertEqual(
                    scope_labels,
                    {("load", "mobile"), ("load", "desktop"), ("body", "mobile"), ("body", "desktop")},
                )
                self.assertEqual([request.viewport.width for request in adapter.requests[:4]], [375, 375, 1365, 1365])
            finally:
                store.close()

    def test_runtime_compare_review_gate_writes_review_and_repair_instruction_for_differences(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = root / "original"
            reconstructed_root = root / "reconstructed"
            original_root.mkdir()
            reconstructed_root.mkdir()
            (original_root / "index.html").write_text("<h1>Original</h1>", encoding="utf-8")
            (reconstructed_root / "index.html").write_text("<h1>Reconstructed</h1>", encoding="utf-8")
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                compare_result = RuntimeCompareRunner(browser_adapter=SequencedBrowserAdapter()).run_compare(
                    job_id=job.id,
                    store=store,
                    original_input_path=original_root,
                    reconstructed_input_path=reconstructed_root,
                    scenario_config={"name": "gate scenario"},
                )

                gate_result = RuntimeCompareReviewGate().run(
                    job_id=job.id,
                    store=store,
                    comparison_artifacts=compare_result.comparison_artifacts,
                    parent_artifact_ids=compare_result.artifact_ids,
                )

                review_artifacts = store.list_artifacts(job.id, kind="review_run")
                repair_artifacts = store.list_artifacts(job.id, kind="repair_instruction")
                review_payload = json.loads(store.read_artifact(job.id, gate_result.review_artifact.id))
                repair_payload = json.loads(store.read_artifact(job.id, gate_result.repair_artifact.id))

                self.assertTrue(gate_result.enabled)
                self.assertTrue(gate_result.triggered)
                self.assertEqual(len(review_artifacts), 1)
                self.assertEqual(len(repair_artifacts), 1)
                self.assertEqual(review_payload["reviewType"], "runtime_compare")
                self.assertEqual(review_payload["status"], "fail")
                self.assertEqual(review_payload["failureClass"], "runtime_error")
                self.assertEqual(review_payload["repairInstructionIds"], [gate_result.repair_artifact.id])
                self.assertEqual(review_payload["evidenceRefs"][0]["artifactId"], compare_result.comparison_artifact.id)
                self.assertIn("blocked automatic behavioral equivalence", review_payload["decision"])
                self.assertEqual(repair_payload["targetStage"], "runtime_compare")
                self.assertEqual(repair_payload["status"], "planned")
                self.assertEqual(repair_payload["actions"], [])
                self.assertEqual(repair_payload["inputArtifactIds"], [compare_result.comparison_artifact.id])
            finally:
                store.close()

    def test_runtime_compare_review_gate_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = root / "original"
            reconstructed_root = root / "reconstructed"
            original_root.mkdir()
            reconstructed_root.mkdir()
            (original_root / "index.html").write_text("<h1>Original</h1>", encoding="utf-8")
            (reconstructed_root / "index.html").write_text("<h1>Reconstructed</h1>", encoding="utf-8")
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                compare_result = RuntimeCompareRunner(browser_adapter=SequencedBrowserAdapter()).run_compare(
                    job_id=job.id,
                    store=store,
                    original_input_path=original_root,
                    reconstructed_input_path=reconstructed_root,
                )

                gate_result = RuntimeCompareReviewGate().run(
                    job_id=job.id,
                    store=store,
                    comparison_artifacts=compare_result.comparison_artifacts,
                    job_config={"runtimeCompare": {"reviewGate": {"enabled": False}}},
                    parent_artifact_ids=compare_result.artifact_ids,
                )

                self.assertFalse(gate_result.enabled)
                self.assertFalse(gate_result.triggered)
                self.assertEqual(store.list_artifacts(job.id, kind="review_run"), [])
                self.assertEqual(store.list_artifacts(job.id, kind="repair_instruction"), [])
            finally:
                store.close()

    def test_runtime_compare_review_gate_respects_pixel_ratio_threshold(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_root = root / "original"
            reconstructed_root = root / "reconstructed"
            original_root.mkdir()
            reconstructed_root.mkdir()
            (original_root / "index.html").write_text("<h1>Original</h1>", encoding="utf-8")
            (reconstructed_root / "index.html").write_text("<h1>Reconstructed</h1>", encoding="utf-8")
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                compare_result = RuntimeCompareRunner(browser_adapter=PixelDiffBrowserAdapter()).run_compare(
                    job_id=job.id,
                    store=store,
                    original_input_path=original_root,
                    reconstructed_input_path=reconstructed_root,
                    scenario_config={"name": "pixel threshold", "viewport": {"name": "tiny", "width": 2, "height": 1}},
                )

                gate_result = RuntimeCompareReviewGate().run(
                    job_id=job.id,
                    store=store,
                    comparison_artifacts=compare_result.comparison_artifacts,
                    job_config={
                        "runtimeCompare": {
                            "reviewGate": {
                                "failOnDomChanged": False,
                                "failOnNetworkChanged": False,
                                "failOnConsoleChanged": False,
                                "maxChangedPixelRatio": 0.5,
                            }
                        }
                    },
                    parent_artifact_ids=compare_result.artifact_ids,
                )
                review_payload = json.loads(store.read_artifact(job.id, gate_result.review_artifact.id))

                self.assertTrue(gate_result.enabled)
                self.assertFalse(gate_result.triggered)
                self.assertEqual(review_payload["reviewType"], "runtime_compare")
                self.assertEqual(review_payload["status"], "pass")
                self.assertEqual(review_payload["repairInstructionIds"], [])
                self.assertEqual(store.list_artifacts(job.id, kind="repair_instruction"), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
