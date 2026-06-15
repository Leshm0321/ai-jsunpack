import json
import shutil
import subprocess
import tempfile
import unittest
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
                persisted_job = store.get_job(job.id)

                self.assertEqual(run.events[0].status, "leased")
                self.assertTrue(any(event.status == "intake" for event in run.events))
                self.assertTrue(any(event.status == "indexing" for event in run.events))
                self.assertTrue(any(event.status == "reconstructing" for event in run.events))
                self.assertTrue(any(event.status == "building" for event in run.events))
                self.assertTrue(any(event.status == "typechecking" for event in run.events))
                self.assertTrue(any(event.status == "runtime_smoke" for event in run.events))
                self.assertIsNotNone(persisted_job)
                self.assertEqual(persisted_job.status, "runtime_smoke")
                self.assertIn("input_inventory", artifact_by_kind)
                self.assertIn("ast_index", artifact_by_kind)
                self.assertIn("agent_plan", artifact_by_kind)
                self.assertIn("reconstruction_plan", artifact_by_kind)
                self.assertIn("generated_project", artifact_by_kind)
                self.assertIn("memory_record", artifact_by_kind)
                self.assertIn("knowledge_evidence", artifact_by_kind)
                self.assertIn("build_log", artifact_by_kind)
                self.assertIn("build_artifact", artifact_by_kind)
                self.assertIn("review_run", artifact_by_kind)
                self.assertIn("tool_call", artifact_by_kind)
                self.assertIn("runtime_validation", artifact_by_kind)
                self.assertIn("runtime_trace", artifact_by_kind)
                self.assertIn("runtime_screenshot", artifact_by_kind)
                inference_artifacts = [artifact for artifact in artifacts if artifact.kind == "inference_record"]
                build_log_artifacts = [artifact for artifact in artifacts if artifact.kind == "build_log"]
                build_artifacts = [artifact for artifact in artifacts if artifact.kind == "build_artifact"]
                review_artifacts = [artifact for artifact in artifacts if artifact.kind == "review_run"]
                self.assertGreaterEqual(len(inference_artifacts), 1)
                self.assertEqual(len(build_log_artifacts), 2)
                self.assertEqual(len(build_artifacts), 2)

                inventory_artifact = artifact_by_kind["input_inventory"]
                ast_index_artifact = artifact_by_kind["ast_index"]
                memory_artifact = artifact_by_kind["memory_record"]
                knowledge_artifact = artifact_by_kind["knowledge_evidence"]
                agent_plan_artifact = artifact_by_kind["agent_plan"]
                reconstruction_plan_artifact = artifact_by_kind["reconstruction_plan"]
                generated_project_artifact = artifact_by_kind["generated_project"]
                tool_call_artifact = artifact_by_kind["tool_call"]
                inventory_payload = json.loads(Path(inventory_artifact.storage_uri).read_text(encoding="utf-8"))
                ast_index_payload = json.loads(Path(ast_index_artifact.storage_uri).read_text(encoding="utf-8"))
                memory_payload = json.loads(Path(memory_artifact.storage_uri).read_text(encoding="utf-8"))
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
                self.assertEqual(knowledge_payload["kind"], "knowledge_evidence")
                self.assertTrue(knowledge_payload["hits"])
                self.assertEqual(agent_plan_payload["provider"], "crewai")
                self.assertEqual(agent_plan_payload["runtimeStatus"], "policy_denied")
                self.assertFalse(agent_plan_payload["modelPolicy"]["allowed"])
                self.assertEqual(agent_plan_payload["memoryRecordArtifactId"], memory_artifact.id)
                self.assertEqual(agent_plan_payload["knowledgeEvidenceArtifactId"], knowledge_artifact.id)
                self.assertEqual(
                    agent_plan_artifact.parent_artifact_ids,
                    [inventory_artifact.id, ast_index_artifact.id, memory_artifact.id, knowledge_artifact.id],
                )
                self.assertEqual(inference_payload["modelProvider"], "local")
                self.assertEqual(
                    inference_payload["inputArtifactIds"],
                    [inventory_artifact.id, ast_index_artifact.id, memory_artifact.id, knowledge_artifact.id],
                )
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
                self.assertEqual(runtime_payload["screenshotArtifactIds"], [artifact_by_kind["runtime_screenshot"].id])
                self.assertIn(build_log_artifact.id, artifact_by_kind["runtime_trace"].parent_artifact_ids)
                self.assertIn(build_artifact.id, artifact_by_kind["runtime_trace"].parent_artifact_ids)
                self.assertIn(generated_project_artifact.id, artifact_by_kind["runtime_trace"].parent_artifact_ids)
                self.assertIn(build_review_artifact.id, artifact_by_kind["runtime_trace"].parent_artifact_ids)
                self.assertIn(typecheck_log_artifact.id, artifact_by_kind["runtime_trace"].parent_artifact_ids)
                self.assertIn(typecheck_artifact.id, artifact_by_kind["runtime_trace"].parent_artifact_ids)
                self.assertIn(typecheck_review_artifact.id, artifact_by_kind["runtime_trace"].parent_artifact_ids)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
