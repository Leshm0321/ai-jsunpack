import json
import shutil
import subprocess
import tempfile
import unittest
from urllib.request import urlopen
import zipfile
from pathlib import Path

from apps.api.app.models import CreateJobRequest
from apps.api.app.store import create_store
from apps.worker.worker.pipeline import WorkerPipeline
from apps.worker.worker.runtime_smoke import BrowserSmokeCapture, BrowserSmokeRequest, RuntimeSmokeRunner


ROOT = Path(__file__).resolve().parents[1]


class FakeBrowserAdapter:
    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        request.screenshot_path.write_bytes(b"\x89PNG\r\n\x1a\nworker-runtime")
        return BrowserSmokeCapture()


class ContentAwareBrowserAdapter:
    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        with urlopen(request.entry_url, timeout=2) as response:
            content = response.read().decode("utf-8")
        label = "original" if "Original App" in content else "generated"
        request.screenshot_path.write_bytes(f"\x89PNG\r\n\x1a\n{label}".encode("utf-8"))
        return BrowserSmokeCapture(
            console_messages=[f"rendered:{label}"],
            responses=["200 /index.html"],
            dom_summary={
                "title": label,
                "nodeCount": content.count("<"),
                "textLength": len(content),
                "textSample": label,
            },
        )


class AlwaysDifferentBrowserAdapter:
    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        with urlopen(request.entry_url, timeout=2) as response:
            content = response.read().decode("utf-8")
        label = request.target
        request.screenshot_path.write_bytes(f"\x89PNG\r\n\x1a\n{label}-{request.attempt}".encode("utf-8"))
        return BrowserSmokeCapture(
            console_messages=[f"rendered:{label}"],
            responses=[f"200 {request.source_entry_path or '/index.html'}"],
            dom_summary={
                "title": label,
                "nodeCount": content.count("<"),
                "textLength": len(content),
                "textSample": label,
            },
        )


class WorkerPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        npm = shutil.which("npm")
        node = shutil.which("node")
        if npm is None or node is None:
            raise unittest.SkipTest("npm and node are required for worker Core integration checks")

        subprocess.run(
            [npm, "run", "build", "--workspace", "@ai-jsunpack/shared"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [npm, "run", "build", "--workspace", "@ai-jsunpack/core"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_worker_pipeline_reaches_completed(self):
        run = WorkerPipeline().run("job_test")

        self.assertEqual(run.events[0].status, "leased")
        self.assertEqual(run.events[-1].status, "completed")
        self.assertTrue(any(event.status == "runtime_smoke" for event in run.events))
        self.assertTrue(any(event.status == "agent_pass" for event in run.events))

    def test_worker_pipeline_persists_core_inventory_and_ast_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "dist"
            asset_root = input_root / "assets"
            asset_root.mkdir(parents=True)
            (input_root / "index.html").write_text(
                '<div id="app"></div><script type="module" src="/assets/app.js"></script>',
                encoding="utf-8",
            )
            (asset_root / "app.js").write_text("function boot(){return 1} export { boot };", encoding="utf-8")

            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                runner = RuntimeSmokeRunner(browser_adapter=FakeBrowserAdapter())
                run = WorkerPipeline(runtime_smoke_runner=runner).run(job.id, input_path=input_root, store=store)
                artifacts = store.list_artifacts(job.id)
                artifact_by_kind = {artifact.kind: artifact for artifact in artifacts}
                artifacts_by_kind: dict[str, list] = {}
                for artifact in artifacts:
                    artifacts_by_kind.setdefault(artifact.kind, []).append(artifact)
                persisted_job = store.get_job(job.id)

                self.assertEqual(run.events[0].status, "leased")
                self.assertTrue(any(event.status == "intake" for event in run.events))
                self.assertTrue(any(event.status == "indexing" for event in run.events))
                self.assertTrue(any(event.status == "reconstructing" for event in run.events))
                self.assertTrue(any(event.status == "building" for event in run.events))
                self.assertTrue(any(event.status == "typechecking" for event in run.events))
                self.assertTrue(any(event.status == "runtime_smoke" for event in run.events))
                self.assertTrue(any(event.status == "runtime_compare" for event in run.events))
                self.assertTrue(any(event.status == "reviewing" for event in run.events))
                self.assertTrue(any(event.status == "packaging" for event in run.events))
                self.assertEqual(run.events[-1].status, "completed_best_effort")
                self.assertIsNotNone(persisted_job)
                self.assertEqual(persisted_job.status, "completed_best_effort")
                self.assertEqual(persisted_job.failure_class, "policy_denied")
                self.assertIn("input_inventory", artifact_by_kind)
                self.assertIn("ast_index", artifact_by_kind)
                self.assertIn("agent_plan", artifact_by_kind)
                self.assertIn("reconstruction_plan", artifact_by_kind)
                self.assertIn("generated_project", artifact_by_kind)
                self.assertIn("memory_record", artifact_by_kind)
                self.assertIn("tool_registry", artifact_by_kind)
                self.assertIn("knowledge_evidence", artifact_by_kind)
                self.assertIn("runtime_diagnosis", artifact_by_kind)
                self.assertIn("report_section", artifact_by_kind)
                self.assertIn("build_log", artifact_by_kind)
                self.assertIn("build_artifact", artifact_by_kind)
                self.assertIn("review_run", artifact_by_kind)
                self.assertIn("tool_call", artifact_by_kind)
                self.assertIn("runtime_validation", artifact_by_kind)
                self.assertIn("runtime_trace", artifact_by_kind)
                self.assertIn("runtime_screenshot", artifact_by_kind)
                self.assertIn("runtime_scenario", artifact_by_kind)
                self.assertIn("runtime_comparison", artifact_by_kind)
                self.assertIn("audit_report", artifact_by_kind)
                self.assertIn("html_report", artifact_by_kind)
                self.assertIn("evidence_index", artifact_by_kind)
                self.assertIn("result_package", artifact_by_kind)
                inference_artifacts = [artifact for artifact in artifacts if artifact.kind == "inference_record"]
                build_log_artifacts = [artifact for artifact in artifacts if artifact.kind == "build_log"]
                build_artifacts = [artifact for artifact in artifacts if artifact.kind == "build_artifact"]
                review_artifacts = [artifact for artifact in artifacts if artifact.kind == "review_run"]
                self.assertGreaterEqual(len(inference_artifacts), 1)
                self.assertEqual(len(build_log_artifacts), 2)
                self.assertEqual(len(build_artifacts), 2)

                inventory_artifact = artifact_by_kind["input_inventory"]
                ast_index_artifact = artifact_by_kind["ast_index"]
                memory_artifacts = artifacts_by_kind["memory_record"]
                memory_by_type = {
                    json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))["memoryType"]: artifact
                    for artifact in memory_artifacts
                }
                memory_artifact = memory_by_type["short_term"]
                tool_registry_artifact = artifact_by_kind["tool_registry"]
                knowledge_artifact = artifact_by_kind["knowledge_evidence"]
                agent_plan_artifact = artifact_by_kind["agent_plan"]
                reconstruction_plan_artifact = artifact_by_kind["reconstruction_plan"]
                generated_project_artifact = artifact_by_kind["generated_project"]
                tool_call_artifact = artifact_by_kind["tool_call"]
                inventory_payload = json.loads(Path(inventory_artifact.storage_uri).read_text(encoding="utf-8"))
                ast_index_payload = json.loads(Path(ast_index_artifact.storage_uri).read_text(encoding="utf-8"))
                memory_payload = json.loads(Path(memory_artifact.storage_uri).read_text(encoding="utf-8"))
                memory_payloads = {
                    memory_type: json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))
                    for memory_type, artifact in memory_by_type.items()
                }
                tool_registry_payload = json.loads(Path(tool_registry_artifact.storage_uri).read_text(encoding="utf-8"))
                knowledge_payload = json.loads(Path(knowledge_artifact.storage_uri).read_text(encoding="utf-8"))
                agent_plan_payload = json.loads(Path(agent_plan_artifact.storage_uri).read_text(encoding="utf-8"))
                reconstruction_plan_payload = json.loads(
                    Path(reconstruction_plan_artifact.storage_uri).read_text(encoding="utf-8")
                )
                inference_payload = json.loads(Path(inference_artifacts[0].storage_uri).read_text(encoding="utf-8"))
                review_payloads = [
                    (artifact, json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8")))
                    for artifact in review_artifacts
                ]
                build_log_payloads = [
                    (artifact, json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8")))
                    for artifact in build_log_artifacts
                ]
                build_artifact_payloads = [
                    (artifact, json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8")))
                    for artifact in build_artifacts
                ]
                review_artifact, review_payload = next(
                    (artifact, payload) for artifact, payload in review_payloads if payload["reviewType"] == "agent_review"
                )
                build_review_artifact, build_review_payload = next(
                    (artifact, payload) for artifact, payload in review_payloads if payload["reviewType"] == "build"
                )
                typecheck_review_artifact, typecheck_review_payload = next(
                    (artifact, payload) for artifact, payload in review_payloads if payload["reviewType"] == "typecheck"
                )
                runtime_compare_review_artifact, runtime_compare_review_payload = next(
                    (artifact, payload)
                    for artifact, payload in review_payloads
                    if payload["reviewType"] == "runtime_compare"
                )
                build_log_artifact, build_log_payload = next(
                    (artifact, payload) for artifact, payload in build_log_payloads if payload["reviewType"] == "build"
                )
                typecheck_log_artifact, typecheck_log_payload = next(
                    (artifact, payload) for artifact, payload in build_log_payloads if payload["reviewType"] == "typecheck"
                )
                build_artifact, build_artifact_payload = next(
                    (artifact, payload) for artifact, payload in build_artifact_payloads if payload["reviewType"] == "build"
                )
                typecheck_artifact, typecheck_artifact_payload = next(
                    (artifact, payload)
                    for artifact, payload in build_artifact_payloads
                    if payload["reviewType"] == "typecheck"
                )
                tool_call_payload = json.loads(Path(tool_call_artifact.storage_uri).read_text(encoding="utf-8"))

                self.assertEqual(inventory_payload["kind"], "input_inventory")
                self.assertEqual(inventory_payload["inventory"]["entries"], ["index.html"])
                self.assertEqual(inventory_payload["inventory"]["scripts"], ["assets/app.js"])
                self.assertEqual(ast_index_payload["kind"], "ast_index")
                self.assertEqual(ast_index_artifact.parent_artifact_ids, [inventory_artifact.id])
                self.assertTrue(
                    any(symbol["name"] == "boot" for symbol in ast_index_payload["astIndexes"][0]["symbols"])
                )
                self.assertEqual(agent_plan_payload["kind"], "agent_plan")
                self.assertEqual(reconstruction_plan_payload["kind"], "reconstruction_plan")
                self.assertEqual(reconstruction_plan_payload["plan"]["strategy"], "static_host_project")
                self.assertTrue(Path(generated_project_artifact.storage_uri).is_dir())
                self.assertTrue((Path(generated_project_artifact.storage_uri) / "src" / "main.ts").exists())
                self.assertEqual(memory_payload["memoryType"], "short_term")
                self.assertIn("boot", memory_payload["content"])
                self.assertEqual(set(memory_payloads), {"short_term", "long_term", "entity", "scenario"})
                self.assertEqual(memory_payloads["long_term"]["scope"], "project")
                self.assertEqual(memory_payloads["entity"]["scope"], "project")
                self.assertEqual(memory_payloads["scenario"]["scope"], "project")
                self.assertEqual(tool_registry_payload["kind"], "tool_registry")
                self.assertTrue(any(entry["toolName"] == "crewai.agent_pass" for entry in tool_registry_payload["entries"]))
                self.assertEqual(knowledge_payload["kind"], "knowledge_evidence")
                self.assertTrue(knowledge_payload["hits"])
                self.assertEqual(agent_plan_payload["provider"], "crewai")
                self.assertEqual(agent_plan_payload["runtimeStatus"], "policy_denied")
                self.assertFalse(agent_plan_payload["modelPolicy"]["allowed"])
                self.assertEqual(agent_plan_payload["memoryRecordArtifactId"], memory_artifact.id)
                self.assertEqual(set(agent_plan_payload["memoryRecordArtifactIds"]), {artifact.id for artifact in memory_artifacts})
                self.assertEqual(agent_plan_payload["toolRegistryArtifactId"], tool_registry_artifact.id)
                self.assertEqual(agent_plan_payload["knowledgeEvidenceArtifactId"], knowledge_artifact.id)
                self.assertEqual(
                    agent_plan_artifact.parent_artifact_ids,
                    [
                        inventory_artifact.id,
                        ast_index_artifact.id,
                        *[artifact.id for artifact in memory_artifacts],
                        knowledge_artifact.id,
                        tool_registry_artifact.id,
                    ],
                )
                self.assertEqual(inference_payload["modelProvider"], "local")
                self.assertIn(tool_registry_artifact.id, inference_payload["inputArtifactIds"])
                self.assertTrue(all(artifact.id in inference_payload["inputArtifactIds"] for artifact in memory_artifacts))
                self.assertEqual(inference_payload["outputArtifactIds"], [agent_plan_artifact.id])
                self.assertTrue(
                    any(ref["artifactId"] == knowledge_artifact.id for ref in inference_payload["evidenceRefs"])
                )
                self.assertEqual(review_payload["reviewType"], "agent_review")
                self.assertEqual(review_payload["status"], "best_effort")
                self.assertEqual(review_payload["failureClass"], "policy_denied")
                self.assertEqual(build_log_payload["status"], "pass")
                self.assertEqual(typecheck_log_payload["status"], "pass")
                self.assertEqual(build_log_payload["limitations"], [])
                self.assertEqual(typecheck_log_payload["limitations"], [])
                self.assertEqual(build_artifact_payload["logsArtifactId"], build_log_artifact.id)
                self.assertEqual(typecheck_artifact_payload["logsArtifactId"], typecheck_log_artifact.id)
                self.assertEqual(build_artifact_payload["resourcePolicy"]["enforcement"], "local_best_effort")
                self.assertEqual(typecheck_artifact_payload["diagnostics"], [])
                self.assertEqual(build_review_payload["logsArtifactId"], build_log_artifact.id)
                self.assertEqual(typecheck_review_payload["logsArtifactId"], typecheck_log_artifact.id)
                self.assertEqual(build_review_payload["evidenceRefs"][0]["artifactId"], build_artifact.id)
                self.assertEqual(typecheck_review_payload["evidenceRefs"][0]["artifactId"], typecheck_artifact.id)
                self.assertEqual(runtime_compare_review_payload["status"], "pass")
                self.assertEqual(runtime_compare_review_payload["failureClass"], "none")
                self.assertEqual(runtime_compare_review_payload["repairInstructionIds"], [])
                self.assertEqual(
                    runtime_compare_review_payload["evidenceRefs"][0]["artifactId"],
                    artifact_by_kind["runtime_comparison"].id,
                )
                self.assertIn(generated_project_artifact.id, build_log_artifact.parent_artifact_ids)
                self.assertIn(generated_project_artifact.id, typecheck_log_artifact.parent_artifact_ids)
                self.assertIn(build_log_artifact.id, build_artifact.parent_artifact_ids)
                self.assertIn(typecheck_log_artifact.id, typecheck_artifact.parent_artifact_ids)
                self.assertEqual(tool_call_payload["toolName"], "crewai.agent_pass")
                self.assertEqual(tool_call_payload["status"], "fail")
                self.assertEqual(tool_call_payload["failureClass"], "policy_denied")
                self.assertIn(review_artifact.id, tool_call_payload["outputArtifactIds"])
                runtime_payload = json.loads(
                    Path(artifact_by_kind["runtime_validation"].storage_uri).read_text(encoding="utf-8")
                )
                self.assertEqual(runtime_payload["status"], "pass")
                self.assertEqual(runtime_payload["comparisonArtifactId"], artifact_by_kind["runtime_comparison"].id)
                self.assertIn(artifact_by_kind["runtime_screenshot"].id, runtime_payload["screenshotArtifactIds"])
                runtime_comparison_payload = json.loads(
                    Path(artifact_by_kind["runtime_comparison"].storage_uri).read_text(encoding="utf-8")
                )
                self.assertEqual(runtime_comparison_payload["status"], "pass")
                self.assertEqual(runtime_comparison_payload["scenarioArtifactId"], artifact_by_kind["runtime_scenario"].id)
                self.assertIn("screenshotDiff", runtime_comparison_payload["differences"])
                self.assertIn("domDifferences", runtime_comparison_payload["differences"])
                self.assertIn("networkDiff", runtime_comparison_payload["differences"])
                self.assertIn("consoleDiff", runtime_comparison_payload["differences"])
                self.assertEqual(runtime_comparison_payload["differences"]["comparisonScope"]["scenarioName"], "default-load")
                runtime_trace_artifacts = [artifact for artifact in artifacts if artifact.kind == "runtime_trace"]
                runtime_compare_trace_artifact = next(
                    artifact
                    for artifact in runtime_trace_artifacts
                    if json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8")).get("target") == "comparison"
                )
                self.assertIn(build_log_artifact.id, runtime_compare_trace_artifact.parent_artifact_ids)
                self.assertIn(build_artifact.id, runtime_compare_trace_artifact.parent_artifact_ids)
                self.assertIn(generated_project_artifact.id, runtime_compare_trace_artifact.parent_artifact_ids)
                self.assertIn(build_review_artifact.id, runtime_compare_trace_artifact.parent_artifact_ids)
                self.assertIn(typecheck_log_artifact.id, runtime_compare_trace_artifact.parent_artifact_ids)
                self.assertIn(typecheck_artifact.id, runtime_compare_trace_artifact.parent_artifact_ids)
                self.assertIn(typecheck_review_artifact.id, runtime_compare_trace_artifact.parent_artifact_ids)
                self.assertIn(runtime_compare_review_artifact.id, artifact_by_kind["audit_report"].parent_artifact_ids)
                audit_report = Path(artifact_by_kind["audit_report"].storage_uri).read_text(encoding="utf-8")
                self.assertIn("# AI JS Unpack Audit Report", audit_report)
                self.assertIn("completed_best_effort", audit_report)
                self.assertIn("## Evidence Attachment Index", audit_report)
                self.assertIn("## Review/Fix Convergence", audit_report)
                self.assertIn("## Runtime Compare Difference Summary", audit_report)
                self.assertIn("## Agent Runtime Audit", audit_report)
                html_report = Path(artifact_by_kind["html_report"].storage_uri).read_text(encoding="utf-8")
                self.assertIn("<!doctype html>", html_report)
                self.assertIn("Evidence Attachment Index", html_report)
                self.assertIn("Review/Fix Convergence", html_report)
                self.assertIn("Runtime Compare Difference Summary", html_report)
                self.assertIn("Agent Runtime Audit", html_report)
                evidence_index_payload = json.loads(
                    Path(artifact_by_kind["evidence_index"].storage_uri).read_text(encoding="utf-8")
                )
                self.assertEqual(evidence_index_payload["kind"], "evidence_index")
                self.assertEqual(evidence_index_payload["includedCount"], 12)
                package_paths = {item["packagePath"] for item in evidence_index_payload["attachments"] if item["included"]}
                self.assertIn(f"evidence/build_log/{build_log_artifact.id}.json", package_paths)
                self.assertIn(f"evidence/build_log/{typecheck_log_artifact.id}.json", package_paths)
                self.assertIn(f"evidence/runtime_trace/{runtime_compare_trace_artifact.id}.json", package_paths)
                self.assertTrue(
                    any(path and path.startswith("evidence/runtime_trace/") for path in package_paths)
                )
                self.assertIn(f"evidence/runtime_screenshot/{artifact_by_kind['runtime_screenshot'].id}.png", package_paths)
                self.assertIn(f"evidence/runtime_scenario/{artifact_by_kind['runtime_scenario'].id}.json", package_paths)
                self.assertIn(f"evidence/runtime_comparison/{artifact_by_kind['runtime_comparison'].id}.json", package_paths)
                report_sections = {item["anchor"]: item for item in evidence_index_payload["reportSections"]}
                self.assertIn("review-fix-convergence", report_sections)
                with zipfile.ZipFile(artifact_by_kind["result_package"].storage_uri) as archive:
                    names = set(archive.namelist())
                    review_fix_summary = json.loads(archive.read("review-fix-summary.json").decode("utf-8"))
                self.assertIn("audit-report.md", names)
                self.assertIn("audit-report.html", names)
                self.assertIn("audit.json", names)
                self.assertIn("evidence-index.json", names)
                self.assertIn("artifact-manifest.json", names)
                self.assertIn("build-artifacts.json", names)
                self.assertIn("inference-records.json", names)
                self.assertIn("runtime-report.json", names)
                self.assertIn("runtime-comparisons.json", names)
                self.assertIn("review-runs.json", names)
                self.assertIn("tool-calls.json", names)
                self.assertIn("tool-registry.json", names)
                self.assertIn("memory-records.json", names)
                self.assertIn("runtime-diagnoses.json", names)
                self.assertIn("report-sections.json", names)
                self.assertIn("review-fix-summary.json", names)
                self.assertEqual(review_fix_summary["target"], "review_fix_convergence_summary")
                self.assertIn(f"evidence/build_log/{build_log_artifact.id}.json", names)
                self.assertIn(f"evidence/runtime_screenshot/{artifact_by_kind['runtime_screenshot'].id}.png", names)
                self.assertIn(f"evidence/runtime_comparison/{artifact_by_kind['runtime_comparison'].id}.json", names)
                self.assertIn("generated_project/src/main.ts", names)
                self.assertIn(artifact_by_kind["audit_report"].id, artifact_by_kind["result_package"].parent_artifact_ids)
                self.assertIn(artifact_by_kind["html_report"].id, artifact_by_kind["result_package"].parent_artifact_ids)
                self.assertIn(artifact_by_kind["evidence_index"].id, artifact_by_kind["result_package"].parent_artifact_ids)
            finally:
                store.close()

    def test_desensitized_job_redacts_agent_model_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "dist"
            asset_root = input_root / "assets"
            asset_root.mkdir(parents=True)
            (input_root / "index.html").write_text(
                '<div id="app"></div><script type="module" src="/assets/secret-app.js"></script>',
                encoding="utf-8",
            )
            (asset_root / "secret-app.js").write_text(
                "function customerSecretToken(){return 'sensitive'} export { customerSecretToken };",
                encoding="utf-8",
            )

            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(
                    CreateJobRequest(
                        project_id="proj",
                        owner_id="owner",
                        cloud_mode="desensitized",
                        config={"agentModel": "provider/model-b", "agentModelProvider": "provider"},
                    )
                )
                runner = RuntimeSmokeRunner(browser_adapter=FakeBrowserAdapter())
                WorkerPipeline(runtime_smoke_runner=runner).run(job.id, input_path=input_root, store=store)
                artifacts = store.list_artifacts(job.id)
                artifact_by_kind = {artifact.kind: artifact for artifact in artifacts}
                inference_artifact = next(artifact for artifact in artifacts if artifact.kind == "inference_record")

                memory_payload = next(
                    json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))
                    for artifact in artifacts
                    if artifact.kind == "memory_record"
                    and json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))["memoryType"] == "short_term"
                )
                agent_plan_payload = json.loads(
                    Path(artifact_by_kind["agent_plan"].storage_uri).read_text(encoding="utf-8")
                )
                inference_payload = json.loads(Path(inference_artifact.storage_uri).read_text(encoding="utf-8"))
                model_context_payload = {
                    "inputSummary": agent_plan_payload["inputSummary"],
                    "planEvidenceRefs": agent_plan_payload["evidenceRefs"],
                    "inferenceEvidenceRefs": inference_payload["evidenceRefs"],
                }
                serialized_context = json.dumps(model_context_payload, ensure_ascii=False, sort_keys=True)

                self.assertIn("customerSecretToken", memory_payload["content"])
                self.assertTrue(agent_plan_payload["modelPolicy"]["sanitizedContext"])
                self.assertTrue(agent_plan_payload["modelPolicy"]["redaction"]["applied"])
                self.assertEqual(
                    agent_plan_payload["modelPolicy"]["redaction"]["strategy"],
                    "deterministic_context_redaction_v1",
                )
                self.assertGreater(agent_plan_payload["modelPolicy"]["redaction"]["replacementCount"], 0)
                self.assertNotIn("customerSecretToken", serialized_context)
                self.assertNotIn("secret-app.js", serialized_context)
                self.assertTrue(
                    any(
                        str(value).startswith("redacted:symbol:")
                        for value in agent_plan_payload["inputSummary"]["symbolSample"]
                    )
                )
                self.assertTrue(
                    any(
                        ref.get("excerpt", "").startswith("redacted:source:")
                        for ref in inference_payload["evidenceRefs"]
                    )
                )
            finally:
                store.close()

    def test_worker_pipeline_applies_runtime_compare_repair_and_retries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "dist"
            asset_root = input_root / "assets"
            asset_root.mkdir(parents=True)
            (input_root / "index.html").write_text(
                '<h1>Original App</h1><script type="module" src="/assets/app.js"></script>',
                encoding="utf-8",
            )
            (asset_root / "app.js").write_text("console.log('original app');", encoding="utf-8")
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                runner = RuntimeSmokeRunner(browser_adapter=ContentAwareBrowserAdapter())
                run = WorkerPipeline(runtime_smoke_runner=runner).run(job.id, input_path=input_root, store=store)
                artifacts = store.list_artifacts(job.id)
                persisted_job = store.get_job(job.id)
                repair_payloads = [
                    json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))
                    for artifact in artifacts
                    if artifact.kind == "repair_instruction"
                ]
                review_payloads = [
                    json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))
                    for artifact in artifacts
                    if artifact.kind == "review_run"
                ]
                runtime_compare_reviews = [
                    payload for payload in review_payloads if payload.get("reviewType") == "runtime_compare"
                ]
                runtime_comparisons = [
                    json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))
                    for artifact in artifacts
                    if artifact.kind == "runtime_comparison"
                ]
                generated_projects = [artifact for artifact in artifacts if artifact.kind == "generated_project"]
                audit_report = next(artifact for artifact in artifacts if artifact.kind == "audit_report")
                result_package = next(artifact for artifact in artifacts if artifact.kind == "result_package")
                with zipfile.ZipFile(result_package.storage_uri) as archive:
                    audit_payload = json.loads(archive.read("audit.json").decode("utf-8"))
                    review_fix_summary = json.loads(archive.read("review-fix-summary.json").decode("utf-8"))

                self.assertEqual(sum(1 for event in run.events if event.status == "runtime_smoke"), 2)
                self.assertEqual(sum(1 for event in run.events if event.status == "runtime_compare"), 2)
                self.assertTrue(any(event.status == "repairing" for event in run.events))
                self.assertIsNotNone(persisted_job)
                self.assertEqual(persisted_job.failure_class, "policy_denied")
                self.assertIn("planned", {payload["status"] for payload in repair_payloads})
                self.assertIn("applied", {payload["status"] for payload in repair_payloads})
                self.assertTrue(
                    any(
                        action["action"] == "mirror_original_static_entry"
                        for payload in repair_payloads
                        for action in payload["actions"]
                    )
                )
                self.assertGreaterEqual(len(generated_projects), 2)
                self.assertEqual(max(artifact.attempt for artifact in generated_projects), 1)
                self.assertEqual(
                    {payload["attempt"]: payload["status"] for payload in runtime_compare_reviews},
                    {0: "fail", 1: "pass"},
                )
                self.assertEqual(
                    {payload["attempt"]: payload["status"] for payload in runtime_comparisons},
                    {0: "pass", 1: "pass"},
                )
                self.assertTrue(
                    any(payload["attempt"] == 0 and payload["differences"]["domChanged"] for payload in runtime_comparisons)
                )
                self.assertTrue(
                    any(
                        payload["attempt"] == 1 and not payload["differences"]["domChanged"]
                        for payload in runtime_comparisons
                    )
                )
                self.assertFalse(
                    any(
                        observation["group"] == "reviewRuns" and observation["failureClass"] == "runtime_error"
                        for observation in audit_payload["completionDecision"]["observations"]
                    )
                )
                self.assertEqual(review_fix_summary["target"], "review_fix_convergence_summary")
                self.assertEqual(review_fix_summary["finalOutcome"], "repaired_passed")
                self.assertEqual(audit_payload["reviewFixSummary"]["finalOutcome"], "repaired_passed")
                self.assertEqual(review_fix_summary["runtimeCompare"]["attemptsUsed"], 2)
                self.assertEqual(review_fix_summary["runtimeCompare"]["appliedRepairCount"], 1)
                self.assertFalse(review_fix_summary["runtimeCompare"]["budgetExhausted"])
                self.assertIn(audit_report.id, result_package.parent_artifact_ids)
                with zipfile.ZipFile(result_package.storage_uri) as archive:
                    repaired_index = archive.read("generated_project/index.html").decode("utf-8")
                self.assertIn("Original App", repaired_index)
            finally:
                store.close()

    def test_worker_pipeline_respects_runtime_compare_max_attempts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "dist"
            asset_root = input_root / "assets"
            asset_root.mkdir(parents=True)
            (input_root / "index.html").write_text(
                '<h1>Original App</h1><script type="module" src="/assets/app.js"></script>',
                encoding="utf-8",
            )
            (asset_root / "app.js").write_text("console.log('original app');", encoding="utf-8")
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(
                    CreateJobRequest(
                        project_id="proj",
                        owner_id="owner",
                        config={"runtimeCompare": {"maxAttempts": 3}},
                    )
                )
                runner = RuntimeSmokeRunner(browser_adapter=AlwaysDifferentBrowserAdapter())
                run = WorkerPipeline(runtime_smoke_runner=runner).run(job.id, input_path=input_root, store=store)
                artifacts = store.list_artifacts(job.id)
                persisted_job = store.get_job(job.id)
                review_payloads = [
                    json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))
                    for artifact in artifacts
                    if artifact.kind == "review_run"
                ]
                runtime_compare_reviews = [
                    payload for payload in review_payloads if payload.get("reviewType") == "runtime_compare"
                ]
                runtime_comparisons = [
                    json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))
                    for artifact in artifacts
                    if artifact.kind == "runtime_comparison"
                ]
                runtime_traces = [
                    json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))
                    for artifact in artifacts
                    if artifact.kind == "runtime_trace"
                ]
                generated_projects = [artifact for artifact in artifacts if artifact.kind == "generated_project"]
                result_package = next(artifact for artifact in artifacts if artifact.kind == "result_package")
                evidence_index = next(artifact for artifact in artifacts if artifact.kind == "evidence_index")
                with zipfile.ZipFile(result_package.storage_uri) as archive:
                    audit_payload = json.loads(archive.read("audit.json").decode("utf-8"))
                    review_fix_summary = json.loads(archive.read("review-fix-summary.json").decode("utf-8"))
                evidence_index_payload = json.loads(Path(evidence_index.storage_uri).read_text(encoding="utf-8"))

                self.assertEqual(sum(1 for event in run.events if event.status == "runtime_smoke"), 3)
                self.assertEqual(sum(1 for event in run.events if event.status == "runtime_compare"), 3)
                self.assertIsNotNone(persisted_job)
                self.assertEqual(persisted_job.status, "completed_best_effort")
                self.assertEqual(max(artifact.attempt for artifact in generated_projects), 2)
                self.assertEqual(
                    {payload["attempt"]: payload["status"] for payload in runtime_compare_reviews},
                    {0: "fail", 1: "fail", 2: "fail"},
                )
                self.assertEqual({payload["attempt"] for payload in runtime_comparisons}, {0, 1, 2})
                self.assertTrue(all(payload["differences"]["domChanged"] for payload in runtime_comparisons))
                retry_summary = next(
                    payload for payload in runtime_traces if payload.get("target") == "runtime_compare_retry_summary"
                )
                matrix_summaries = [payload for payload in runtime_traces if payload.get("target") == "runtime_compare_matrix"]
                self.assertEqual(retry_summary["maxAttempts"], 3)
                self.assertEqual(retry_summary["attemptsUsed"], 3)
                self.assertTrue(retry_summary["budgetExhausted"])
                self.assertEqual(retry_summary["stoppedReason"], "retry_budget_exhausted")
                convergence_summary = next(
                    payload for payload in runtime_traces if payload.get("target") == "review_fix_convergence_summary"
                )
                self.assertEqual(convergence_summary["finalOutcome"], "budget_exhausted_best_effort")
                self.assertEqual(review_fix_summary["finalOutcome"], "budget_exhausted_best_effort")
                self.assertEqual(audit_payload["reviewFixSummary"]["finalOutcome"], "budget_exhausted_best_effort")
                self.assertEqual(review_fix_summary["runtimeCompare"]["attemptsUsed"], 3)
                self.assertTrue(review_fix_summary["runtimeCompare"]["budgetExhausted"])
                self.assertEqual([payload["attempt"] for payload in matrix_summaries], [0, 1, 2])
                report_sections = {item["anchor"]: item for item in evidence_index_payload["reportSections"]}
                runtime_details = report_sections["runtime-compare-difference-summary"]["details"]
                review_fix_details = report_sections["review-fix-convergence"]["details"]
                matrix_detail = next(item for item in runtime_details if item["label"] == "Runtime compare matrix summary")
                scope_detail = next(item for item in runtime_details if item["label"] == "Runtime compare scope")
                review_fix_detail = next(
                    item for item in review_fix_details if item["label"] == "Review/Fix convergence summary"
                )
                self.assertEqual(matrix_detail["status"], "fail")
                self.assertTrue(matrix_detail["details"]["retryBudget"]["budgetExhausted"])
                self.assertEqual(matrix_detail["details"]["retryBudget"]["attemptsUsed"], 3)
                self.assertEqual(review_fix_detail["status"], "best_effort")
                self.assertEqual(review_fix_detail["details"]["finalOutcome"], "budget_exhausted_best_effort")
                self.assertEqual(len(scope_detail["details"]["attemptHistory"]), 3)
                self.assertTrue(
                    any(
                        observation["group"] == "reviewRuns" and observation["failureClass"] == "runtime_error"
                        for observation in audit_payload["completionDecision"]["observations"]
                    )
                )
                self.assertTrue(
                    any(
                        item["label"] == "Runtime compare matrix summary" and item["status"] == "fail"
                        for item in report_sections["risk-and-failure-groups"]["details"]
                    )
                )
                self.assertTrue(
                    any(
                        item["label"] == "Review/Fix convergence summary" and item["status"] == "best_effort"
                        for item in report_sections["risk-and-failure-groups"]["details"]
                    )
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
