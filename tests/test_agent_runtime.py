import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from apps.api.app.models import CreateJobRequest, EvidenceRef
from apps.api.app.store import create_store
from apps.worker.worker.agent_runtime import (
    AgentContextRedactor,
    AgentRuntime,
    AgentRuntimeRequest,
    AgentToolRegistryBuilder,
    ModelPolicyResolver,
)
from apps.worker.worker.reconstruction import ReconstructionRunner
from packages.knowledge import StaticKnowledgeRetriever


class AgentRuntimePolicyTest(unittest.TestCase):
    def _request(self, *, cloud_mode: str, config: dict | None = None) -> AgentRuntimeRequest:
        return AgentRuntimeRequest(
            job_id="job_policy",
            project_id="proj",
            cloud_mode=cloud_mode,  # type: ignore[arg-type]
            job_config=config or {},
            inventory_artifact_id="artifact_inventory",
            ast_index_artifact_id="artifact_ast",
            inventory_payload={"kind": "input_inventory", "inventory": {"entries": [], "scripts": []}},
            ast_index_payload={"kind": "ast_index", "astIndexes": []},
        )

    def test_local_only_requires_local_model(self):
        policy = ModelPolicyResolver().resolve(self._request(cloud_mode="local_only"))

        self.assertFalse(policy.allowed)
        self.assertEqual(policy.model_provider, "local")
        self.assertEqual(policy.model_name, "unconfigured")
        self.assertIn("local_only", policy.denial_reason)

    def test_cloud_allowed_uses_agent_model_config(self):
        policy = ModelPolicyResolver().resolve(
            self._request(
                cloud_mode="cloud_allowed",
                config={"agentModel": "provider/model-a", "agentModelProvider": "provider"},
            )
        )

        self.assertTrue(policy.allowed)
        self.assertEqual(policy.model_provider, "provider")
        self.assertEqual(policy.model_name, "provider/model-a")
        self.assertFalse(policy.sanitized_context)

    def test_desensitized_marks_context_sanitized(self):
        policy = ModelPolicyResolver().resolve(
            self._request(
                cloud_mode="desensitized",
                config={"agentModel": "provider/model-b", "agentModelProvider": "provider"},
            )
        )

        self.assertTrue(policy.allowed)
        self.assertEqual(policy.model_name, "provider/model-b")
        self.assertTrue(policy.sanitized_context)

    def test_desensitized_redacts_model_context_material(self):
        policy = ModelPolicyResolver().resolve(
            self._request(
                cloud_mode="desensitized",
                config={"agentModel": "provider/model-b", "agentModelProvider": "provider"},
            )
        )

        result = AgentContextRedactor().redact(
            policy=policy,
            input_summary={
                "entries": ["index.html"],
                "scripts": ["assets/secret-app.js"],
                "styles": ["assets/secret.css"],
                "sourceMaps": ["assets/secret-app.js.map"],
                "astIndexCount": 1,
                "symbolCount": 1,
                "symbolSample": ["customerSecretToken"],
            },
            memory_excerpt="Job short-term memory mentions customerSecretToken and assets/secret-app.js",
            evidence_refs=[
                EvidenceRef(
                    artifact_id="artifact_ast",
                    label="Core AST index",
                    locator="file:assets/secret-app.js",
                    excerpt="symbols=['customerSecretToken']",
                ),
                EvidenceRef(
                    artifact_id="artifact_knowledge",
                    label="Knowledge",
                    locator="knowledge:module_pattern/esm",
                    excerpt="Named exports suggest module boundaries.",
                ),
            ],
        )

        serialized = repr(
            {
                "inputSummary": result.input_summary,
                "memory": result.memory_excerpt,
                "evidenceRefs": [ref.model_dump(by_alias=True, exclude_none=True) for ref in result.evidence_refs],
            }
        )
        self.assertTrue(result.metadata["applied"])
        self.assertGreater(result.metadata["replacementCount"], 0)
        self.assertIn("redacted:path:", result.input_summary["scripts"][0])
        self.assertIn("redacted:symbol:", result.input_summary["symbolSample"][0])
        self.assertNotIn("customerSecretToken", serialized)
        self.assertNotIn("secret-app.js", serialized)
        self.assertEqual(result.evidence_refs[1].locator, "knowledge:module_pattern/esm")

    def test_non_sanitized_policy_keeps_model_context_material(self):
        policy = ModelPolicyResolver().resolve(
            self._request(
                cloud_mode="cloud_allowed",
                config={"agentModel": "provider/model-a", "agentModelProvider": "provider"},
            )
        )

        result = AgentContextRedactor().redact(
            policy=policy,
            input_summary={"scripts": ["assets/app.js"], "symbolSample": ["boot"]},
            memory_excerpt="Job memory mentions boot.",
            evidence_refs=[
                EvidenceRef(
                    artifact_id="artifact_ast",
                    label="Core AST index",
                    locator="file:assets/app.js",
                    excerpt="symbols=['boot']",
                )
            ],
        )

        self.assertFalse(result.metadata["applied"])
        self.assertEqual(result.input_summary["scripts"], ["assets/app.js"])
        self.assertEqual(result.memory_excerpt, "Job memory mentions boot.")
        self.assertEqual(result.evidence_refs[0].excerpt, "symbols=['boot']")

    def test_tool_registry_marks_crewai_provider_as_stateful_not_parallel_safe(self):
        entries = AgentToolRegistryBuilder().entries("job_tools")
        crewai_entry = next(entry for entry in entries if entry.tool_name == "crewai.agent_pass")

        self.assertEqual(crewai_entry.category, "model")
        self.assertIn("stateful", crewai_entry.description)
        self.assertIn("not parallel-safe", crewai_entry.description)
        self.assertIn("model_call", crewai_entry.description)
        self.assertIn("crewai_storage", crewai_entry.description)
        self.assertIn("runtime_diagnosis", crewai_entry.output_artifact_kinds)
        self.assertIn("repair_instruction", crewai_entry.output_artifact_kinds)

    def test_static_knowledge_retriever_recognizes_core_and_runtime_patterns(self):
        retriever = StaticKnowledgeRetriever()
        inventory_payload = {
            "kind": "input_inventory",
            "inventory": {
                "entries": [],
                "scripts": ["assets/app.js", "assets/vendor.js"],
                "styles": ["assets/app.css"],
                "manifests": ["manifest.webmanifest"],
                "sourceMaps": [],
                "warnings": [],
                "isSingleBundle": False,
            },
        }
        ast_index_payload = {
            "kind": "ast_index",
            "detectedRuntime": ["multi_chunk", "vite_or_rollup"],
            "astIndexes": [
                {
                    "filePath": "assets/app.js",
                    "imports": ["react", "vue"],
                    "exports": ["default"],
                    "symbols": [
                        {"name": "a", "kind": "variable"},
                        {"name": "b", "kind": "variable"},
                        {"name": "c", "kind": "function"},
                        {"name": "window", "kind": "identifier"},
                        {"name": "process", "kind": "identifier"},
                        {"name": "globalThis", "kind": "identifier"},
                    ],
                    "warnings": [],
                }
            ],
        }

        hits = retriever.retrieve(inventory_payload=inventory_payload, ast_index_payload=ast_index_payload)
        hit_ids = {hit.id for hit in hits}
        categories = {hit.category for hit in hits}

        self.assertIn("knowledge_browser_shim_missing_html_entry", hit_ids)
        self.assertIn("knowledge_browser_shim_generated_host", hit_ids)
        self.assertIn("knowledge_runtime_multi_chunk", hit_ids)
        self.assertIn("knowledge_framework_react", hit_ids)
        self.assertIn("knowledge_framework_vue", hit_ids)
        self.assertIn("knowledge_framework_vite_rollup", hit_ids)
        self.assertIn("knowledge_obfuscation_short_symbols", hit_ids)
        self.assertIn("knowledge_browser_shim_dom_globals", hit_ids)
        self.assertIn("knowledge_browser_shim_node_globals", hit_ids)
        self.assertIn("knowledge_browser_shim_global_this", hit_ids)
        self.assertTrue({"browser_shim", "build_runtime", "framework_feature", "obfuscation_pattern"}.issubset(categories))

        artifact_payload = retriever.artifact_payload(
            job_id="job_knowledge",
            input_artifact_ids=["artifact_inventory", "artifact_ast"],
            hits=hits,
        )
        self.assertEqual(artifact_payload["retrievalSources"]["core"], ["input_inventory", "ast_index"])
        self.assertFalse(artifact_payload["retrievalSources"]["currentJobArtifacts"])

    def test_static_knowledge_retriever_includes_historical_project_repair_cases(self):
        retriever = StaticKnowledgeRetriever()
        historical_hits = retriever.retrieve(
            inventory_payload={"kind": "input_inventory", "inventory": {"entries": [], "scripts": []}},
            ast_index_payload={"kind": "ast_index", "astIndexes": []},
            historical_artifact_payloads=[
                {
                    "kind": "repair_instruction",
                    "artifactId": "artifact_history_repair",
                    "jobId": "job_history",
                    "projectId": "proj",
                    "targetStage": "runtime_compare",
                    "status": "planned",
                    "riskLevel": "low",
                    "failureClass": "runtime_error",
                    "decision": "Mirror the original static entry.",
                },
                {
                    "kind": "review_run",
                    "artifactId": "artifact_history_review",
                    "jobId": "job_history",
                    "projectId": "proj",
                    "reviewType": "runtime_compare",
                    "status": "fail",
                    "failureClass": "runtime_error",
                    "decision": "Runtime compare still differs.",
                },
            ],
        )
        hit_ids = {hit.id for hit in historical_hits}
        artifact_payload = retriever.artifact_payload(
            job_id="job_knowledge",
            input_artifact_ids=["artifact_inventory", "artifact_ast"],
            hits=historical_hits,
            historical_artifact_payloads=[
                {
                    "kind": "repair_instruction",
                    "artifactId": "artifact_history_repair",
                    "jobId": "job_history",
                    "projectId": "proj",
                    "targetStage": "runtime_compare",
                    "status": "planned",
                    "riskLevel": "low",
                    "failureClass": "runtime_error",
                    "decision": "Mirror the original static entry.",
                }
            ],
        )

        self.assertIn("knowledge_historical_repair_case_runtime_compare_low_artifact_history_repair", hit_ids)
        self.assertIn("knowledge_historical_review_feedback_runtime_compare_runtime_error_artifact_history_review", hit_ids)
        self.assertTrue(artifact_payload["retrievalSources"]["crossJobHistory"])
        self.assertTrue(artifact_payload["retrievalSources"]["historicalProjectArtifacts"])
        self.assertEqual(
            {item["artifactId"] for item in artifact_payload["retrievalSources"]["historicalProjectArtifacts"]},
            {"artifact_history_repair"},
        )

    def test_agent_runtime_reads_same_project_historical_repair_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                project_id = "proj"
                other_project_id = "other-proj"
                historical_job = store.create_job(CreateJobRequest(project_id=project_id, owner_id="owner"))
                other_job = store.create_job(CreateJobRequest(project_id=other_project_id, owner_id="owner"))
                job = store.create_job(CreateJobRequest(project_id=project_id, owner_id="owner"))
                inventory_payload = {
                    "kind": "input_inventory",
                    "inventory": {
                        "entries": ["index.html"],
                        "scripts": ["assets/app.js"],
                        "styles": [],
                        "sourceMaps": [],
                        "manifests": [],
                        "isSingleBundle": False,
                        "warnings": [],
                    },
                }
                ast_index_payload = {
                    "kind": "ast_index",
                    "detectedRuntime": ["vite_or_rollup"],
                    "astIndexes": [
                        {
                            "filePath": "assets/app.js",
                            "imports": [],
                            "exports": ["boot"],
                            "symbols": [{"name": "boot", "kind": "function"}],
                            "warnings": [],
                        }
                    ],
                }
                inventory_artifact = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="input_inventory",
                    stage="intake",
                    filename="input-inventory.json",
                    payload=inventory_payload,
                )
                ast_artifact = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="ast_index",
                    stage="indexing",
                    filename="ast-index.json",
                    payload=ast_index_payload,
                )
                self._write_json_artifact(
                    store=store,
                    job_id=historical_job.id,
                    kind="repair_instruction",
                    stage="repairing",
                    filename="history-repair.json",
                    payload={
                        "kind": "repair_instruction",
                        "targetStage": "runtime_compare",
                        "status": "planned",
                        "riskLevel": "low",
                        "failureClass": "runtime_error",
                        "decision": "Mirror the original static entry.",
                        "attempt": 0,
                    },
                )
                self._write_json_artifact(
                    store=store,
                    job_id=historical_job.id,
                    kind="review_run",
                    stage="reviewing",
                    filename="history-review.json",
                    payload={
                        "kind": "review_run",
                        "reviewType": "runtime_compare",
                        "status": "fail",
                        "failureClass": "runtime_error",
                        "decision": "Runtime compare still differs.",
                        "attempt": 0,
                    },
                )
                _ = self._write_json_artifact(
                    store=store,
                    job_id=other_job.id,
                    kind="repair_instruction",
                    stage="repairing",
                    filename="other-repair.json",
                    payload={
                        "kind": "repair_instruction",
                        "targetStage": "runtime_compare",
                        "status": "planned",
                        "riskLevel": "low",
                        "failureClass": "runtime_error",
                        "decision": "Do not leak me.",
                        "attempt": 0,
                    },
                )
                request = AgentRuntimeRequest(
                    job_id=job.id,
                    project_id=job.project_id,
                    cloud_mode=job.cloud_mode,
                    job_config=job.config,
                    inventory_artifact_id=inventory_artifact.id,
                    ast_index_artifact_id=ast_artifact.id,
                    inventory_payload=inventory_payload,
                    ast_index_payload=ast_index_payload,
                )

                result = AgentRuntime().run(job_id=job.id, store=store, request=request)
                knowledge_payload = json.loads(store.read_artifact(job.id, result.knowledge_artifact.id))
                hit_by_id = {hit["id"]: hit for hit in knowledge_payload["hits"]}
                retrieval_sources = knowledge_payload["retrievalSources"]
                evidence_refs = [
                    ref for ref in json.loads(store.read_artifact(job.id, result.plan_artifact.id))["evidenceRefs"]
                    if ref["locator"].startswith("knowledge:")
                ]

                self.assertTrue(
                    any(hit_id.startswith("knowledge_historical_repair_case_runtime_compare_low_") for hit_id in hit_by_id)
                )
                self.assertTrue(
                    any(hit_id.startswith("knowledge_historical_review_feedback_runtime_compare_runtime_error_") for hit_id in hit_by_id)
                )
                self.assertTrue(retrieval_sources["crossJobHistory"])
                self.assertTrue(
                    any(
                        item["jobId"] == historical_job.id
                        for item in retrieval_sources["historicalProjectArtifacts"]
                    )
                )
                self.assertFalse(
                    any(
                        item.get("jobId") == other_job.id
                        for item in retrieval_sources["historicalProjectArtifacts"]
                    )
                )
                self.assertGreaterEqual(retrieval_sources["historicalProjectArtifacts"][0]["attempt"], 0)
                self.assertEqual(
                    {item["jobId"] for item in retrieval_sources["historicalProjectArtifacts"]},
                    {historical_job.id},
                )
                self.assertTrue(
                    any(
                        ref["locator"].startswith("knowledge:repair_case/historical/")
                        for ref in evidence_refs
                    )
                )
            finally:
                store.close()

    def test_agent_runtime_reads_current_job_validation_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                inventory_payload = {
                    "kind": "input_inventory",
                    "inventory": {
                        "entries": ["index.html"],
                        "scripts": ["assets/app.js"],
                        "styles": [],
                        "sourceMaps": [],
                        "manifests": [],
                        "isSingleBundle": False,
                        "warnings": [],
                    },
                }
                ast_index_payload = {
                    "kind": "ast_index",
                    "detectedRuntime": ["vite_or_rollup"],
                    "astIndexes": [
                        {
                            "filePath": "assets/app.js",
                            "imports": [],
                            "exports": ["boot"],
                            "symbols": [{"name": "boot", "kind": "function"}],
                            "warnings": [],
                        }
                    ],
                }
                inventory_artifact = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="input_inventory",
                    stage="intake",
                    filename="input-inventory.json",
                    payload=inventory_payload,
                )
                ast_artifact = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="ast_index",
                    stage="indexing",
                    filename="ast-index.json",
                    payload=ast_index_payload,
                )
                build_artifact = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="build_artifact",
                    stage="building",
                    filename="build-artifact.json",
                    payload={
                        "kind": "build_artifact",
                        "reviewType": "build",
                        "status": "fail",
                        "failureClass": "build_error",
                        "decision": "Build failed because package.json lacked a safe build script.",
                        "attempt": 0,
                    },
                )
                runtime_trace_artifact = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="runtime_trace",
                    stage="runtime_smoke",
                    filename="runtime-trace.json",
                    payload={
                        "kind": "runtime_trace",
                        "target": "reconstructed",
                        "status": "best_effort",
                        "failureClass": "runtime_error",
                        "consoleErrors": ["ReferenceError: process is not defined"],
                        "pageErrors": [],
                        "failedRequests": [],
                        "attempt": 0,
                        "executionBoundary": {"runnerKind": "remote_browser_runner"},
                    },
                )
                review_artifact = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="review_run",
                    stage="reviewing",
                    filename="runtime-review.json",
                    payload={
                        "kind": "review_run",
                        "reviewType": "runtime_compare",
                        "status": "fail",
                        "failureClass": "runtime_error",
                        "decision": "Runtime compare still differs after initial reconstruction.",
                        "attempt": 0,
                    },
                )
                repair_artifact = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="repair_instruction",
                    stage="repairing",
                    filename="runtime-repair.json",
                    payload={
                        "kind": "repair_instruction",
                        "targetStage": "runtime_compare",
                        "status": "planned",
                        "riskLevel": "low",
                        "failureClass": "runtime_error",
                        "decision": "Mirror original static entry for runtime compare retry.",
                        "attempt": 1,
                    },
                )
                request = AgentRuntimeRequest(
                    job_id=job.id,
                    project_id=job.project_id,
                    cloud_mode=job.cloud_mode,
                    job_config=job.config,
                    inventory_artifact_id=inventory_artifact.id,
                    ast_index_artifact_id=ast_artifact.id,
                    inventory_payload=inventory_payload,
                    ast_index_payload=ast_index_payload,
                )

                result = AgentRuntime().run(job_id=job.id, store=store, request=request)
                agent_plan_payload = json.loads(store.read_artifact(job.id, result.plan_artifact.id))
                knowledge_payload = json.loads(store.read_artifact(job.id, result.knowledge_artifact.id))
                hit_by_id = {hit["id"]: hit for hit in knowledge_payload["hits"]}
                source_kinds = {
                    source["kind"]
                    for source in knowledge_payload["retrievalSources"]["currentJobArtifacts"]
                }
                agent_repair_payloads = [
                    json.loads(store.read_artifact(job.id, artifact.id))
                    for artifact in result.repair_instruction_artifacts
                ]
                low_risk_action_repairs = [payload for payload in agent_repair_payloads if payload["actions"]]

                self.assertIn("knowledge_validation_build_build_build_error", hit_by_id)
                self.assertIn("knowledge_browser_shim_runtime_boundary_remote_browser_runner", hit_by_id)
                self.assertIn("knowledge_validation_runtime_trace_reconstructed_runtime_error", hit_by_id)
                self.assertIn("knowledge_review_feedback_runtime_compare_runtime_error", hit_by_id)
                self.assertIn("knowledge_repair_case_runtime_compare_low", hit_by_id)
                self.assertEqual(
                    hit_by_id["knowledge_repair_case_runtime_compare_low"]["sourceArtifactIds"],
                    [repair_artifact.id],
                )
                self.assertEqual(
                    hit_by_id["knowledge_validation_build_build_build_error"]["sourceArtifactIds"],
                    [build_artifact.id],
                )
                self.assertEqual(
                    hit_by_id["knowledge_browser_shim_runtime_boundary_remote_browser_runner"]["sourceArtifactIds"],
                    [runtime_trace_artifact.id],
                )
                self.assertIn(review_artifact.id, hit_by_id["knowledge_review_feedback_runtime_compare_runtime_error"]["sourceArtifactIds"])
                self.assertTrue(
                    {"build_artifact", "runtime_trace", "review_run", "repair_instruction"}.issubset(source_kinds)
                )
                self.assertEqual(agent_plan_payload["reviewFixFeedback"]["lowRiskRepairCount"], 1)
                self.assertEqual(agent_plan_payload["reviewFixFeedback"]["auditOnlyRepairCount"], 0)
                self.assertEqual(agent_plan_payload["reviewFixFeedback"]["crossJobHistory"], False)
                self.assertTrue(low_risk_action_repairs)
                self.assertEqual(low_risk_action_repairs[0]["targetStage"], "runtime_compare")
                self.assertEqual(low_risk_action_repairs[0]["status"], "planned")
                self.assertEqual(low_risk_action_repairs[0]["riskLevel"], "low")
                self.assertEqual(low_risk_action_repairs[0]["actions"][0]["action"], "mirror_original_static_entry")
                self.assertFalse(knowledge_payload["retrievalSources"]["crossJobHistory"])
            finally:
                store.close()

    def test_reconstruction_plan_records_writer_feedback_inputs(self):
        class FakeCoreBridge:
            def reconstruct_input_package(self, *, job_id, input_path, output_dir):
                output_dir.mkdir(parents=True)
                (output_dir / "index.html").write_text("<div>generated</div>", encoding="utf-8")
                return SimpleNamespace(
                    reconstruction_plan_payload={
                        "kind": "reconstruction_plan",
                        "strategy": "static_host_project",
                        "limitations": [],
                    },
                    generated_project_path=output_dir,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "dist"
            input_root.mkdir()
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                low_risk_repair = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="repair_instruction",
                    stage="agent_pass",
                    filename="low-risk-repair.json",
                    payload={
                        "kind": "repair_instruction",
                        "targetStage": "runtime_compare",
                        "status": "planned",
                        "riskLevel": "low",
                        "failureClass": "runtime_error",
                        "decision": "Mirror static entry.",
                        "attempt": 0,
                        "actions": [
                            {
                                "action": "mirror_original_static_entry",
                                "path": "projectRoot",
                                "value": "public/original",
                                "reason": "Runtime compare retry can mirror original static files.",
                            }
                        ],
                    },
                )
                high_risk_repair = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="repair_instruction",
                    stage="agent_pass",
                    filename="high-risk-repair.json",
                    payload={
                        "kind": "repair_instruction",
                        "targetStage": "runtime_compare",
                        "status": "skipped",
                        "riskLevel": "high",
                        "failureClass": "runtime_error",
                        "decision": "Rewrite runtime bootstrap manually.",
                        "attempt": 0,
                        "actions": [],
                    },
                )

                result = ReconstructionRunner(core_bridge=FakeCoreBridge()).run(
                    job_id=job.id,
                    input_path=input_root,
                    store=store,
                    parent_artifact_ids=[low_risk_repair.id, high_risk_repair.id],
                )
                plan_payload = json.loads(store.read_artifact(job.id, result.plan_artifact.id))
                feedback = plan_payload["agentFeedbackInputs"]

                self.assertEqual(feedback["consumptionPolicy"], "low_risk_supported_actions_only")
                self.assertEqual(feedback["lowRiskRepairInstructions"][0]["artifactId"], low_risk_repair.id)
                self.assertEqual(feedback["lowRiskRepairInstructions"][0]["actionCount"], 1)
                self.assertEqual(feedback["auditOnlyRepairInstructions"][0]["artifactId"], high_risk_repair.id)
                self.assertIn("Agent Review/Fix feedback was read", plan_payload["limitations"][0])
            finally:
                store.close()

    def _write_json_artifact(self, *, store, job_id: str, kind: str, stage: str, filename: str, payload: dict):
        return store.write_artifact(
            job_id,
            kind=kind,  # type: ignore[arg-type]
            stage=stage,  # type: ignore[arg-type]
            filename=filename,
            content=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="tests.agent_runtime",
        )


if __name__ == "__main__":
    unittest.main()
