import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.api.app.models import CreateJobRequest, EvidenceRef
from apps.api.app.store import create_store
from apps.worker.worker.agent_contracts import CrewAgentExecution, CrewAgentSpec, CrewStageExecution
from apps.worker.worker.agent_runtime import (
    AgentContextRedactor,
    AgentRuntime,
    AgentRuntimeRequest,
    AgentToolRegistryBuilder,
    ModelPolicyResolver,
    CrewExecutionManager,
    CrewRuntimePlanner,
)
from apps.worker.worker.agent_artifacts import AgentArtifactWriter
from apps.worker.worker.agent_providers import (
    CrewAIBackend,
    CrewAIExecutionAdapter,
    OpenAICompatibleCrewAILLM,
    OpenAICompatibleLLMError,
)
from apps.worker.worker.agent_contracts import CrewStructuredAgentOutput
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
        self.assertFalse(policy.custom_endpoint_enabled)

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

    def test_cloud_policy_reads_openai_compatible_endpoint_from_worker_env(self):
        with patch.dict(
            os.environ,
            {
                "AI_JSUNPACK_AGENT_BASE_URL": "https://agent.example.test",
                "AI_JSUNPACK_AGENT_API_KEY": "secret-value",
                "AI_JSUNPACK_AGENT_TIMEOUT_SECONDS": "12.5",
                "AI_JSUNPACK_AGENT_TEMPERATURE": "0.2",
            },
            clear=False,
        ):
            policy = ModelPolicyResolver().resolve(
                self._request(
                    cloud_mode="cloud_allowed",
                    config={"agentModel": "private-model", "agentModelProvider": "openai-compatible"},
                )
            )

        self.assertTrue(policy.allowed)
        self.assertTrue(policy.custom_endpoint_enabled)
        self.assertEqual(policy.base_url, "https://agent.example.test")
        self.assertTrue(policy.api_key_configured)
        self.assertEqual(policy.timeout_seconds, 12.5)
        self.assertEqual(policy.temperature, 0.2)
        self.assertNotIn("secret-value", repr(policy))

    def test_local_policy_reads_local_openai_compatible_endpoint_from_worker_env(self):
        with patch.dict(
            os.environ,
            {
                "AI_JSUNPACK_LOCAL_AGENT_BASE_URL": "http://127.0.0.1:11434/v1",
                "AI_JSUNPACK_LOCAL_AGENT_API_KEY": "",
            },
            clear=False,
        ):
            policy = ModelPolicyResolver().resolve(
                self._request(
                    cloud_mode="local_only",
                    config={"localAgentModel": "local-model", "localAgentProvider": "openai-compatible"},
                )
            )

        self.assertTrue(policy.allowed)
        self.assertTrue(policy.custom_endpoint_enabled)
        self.assertEqual(policy.base_url, "http://127.0.0.1:11434/v1")
        self.assertFalse(policy.api_key_configured)
        self.assertEqual(policy.timeout_seconds, 30.0)
        self.assertIsNone(policy.temperature)

    def test_cloud_policy_rejects_private_and_credentialed_endpoints(self):
        for base_url, expected in (
            ("https://127.0.0.1:11434/v1", "private"),
            ("https://user:secret@agent.example.test/v1", "credentials"),
            ("file:///tmp/model", "http or https"),
        ):
            with self.subTest(base_url=base_url), patch.dict(
                os.environ,
                {"AI_JSUNPACK_AGENT_BASE_URL": base_url},
                clear=False,
            ):
                policy = ModelPolicyResolver().resolve(
                    self._request(
                        cloud_mode="cloud_allowed",
                        config={"agentModel": "private-model", "agentModelProvider": "openai-compatible"},
                    )
                )

            self.assertFalse(policy.allowed)
            self.assertIn(expected, policy.denial_reason or "")

    def test_production_cloud_policy_requires_https(self):
        with patch.dict(
            os.environ,
            {
                "AI_JSUNPACK_DEPLOYMENT_PROFILE": "production",
                "AI_JSUNPACK_AGENT_BASE_URL": "http://agent.example.test/v1",
            },
            clear=False,
        ):
            policy = ModelPolicyResolver().resolve(
                self._request(
                    cloud_mode="cloud_allowed",
                    config={"agentModel": "private-model", "agentModelProvider": "openai-compatible"},
                )
            )

        self.assertFalse(policy.allowed)
        self.assertIn("must use https", policy.denial_reason or "")

    def test_invalid_timeout_and_temperature_fall_back_to_safe_defaults(self):
        with patch.dict(
            os.environ,
            {
                "AI_JSUNPACK_AGENT_TIMEOUT_SECONDS": "-1",
                "AI_JSUNPACK_AGENT_TEMPERATURE": "not-a-number",
            },
            clear=False,
        ):
            policy = ModelPolicyResolver().resolve(
                self._request(cloud_mode="cloud_allowed", config={"agentModel": "model-a"})
            )

        self.assertEqual(policy.timeout_seconds, 30.0)
        self.assertIsNone(policy.temperature)

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

    def test_crewai_backend_uses_string_model_when_no_custom_endpoint(self):
        policy = ModelPolicyResolver().resolve(
            self._request(cloud_mode="cloud_allowed", config={"agentModel": "provider/model-a"})
        )

        self.assertEqual(CrewAIBackend()._llm_for_policy(policy), "provider/model-a")

    def test_crewai_backend_uses_custom_llm_for_openai_compatible_endpoint(self):
        with patch.dict(
            os.environ,
            {"AI_JSUNPACK_AGENT_BASE_URL": "https://agent.example.test", "AI_JSUNPACK_AGENT_API_KEY": "secret"},
            clear=False,
        ):
            policy = ModelPolicyResolver().resolve(
                self._request(
                    cloud_mode="cloud_allowed",
                    config={"agentModel": "private-model", "agentModelProvider": "openai-compatible"},
                )
            )

        llm = CrewAIBackend()._llm_for_policy(policy)

        self.assertNotEqual(llm, "private-model")
        self.assertEqual(llm.model, "private-model")
        self.assertEqual(llm.base_url, "https://agent.example.test")
        self.assertEqual(llm._endpoint, "https://agent.example.test/v1/chat/completions")

    def test_openai_compatible_llm_posts_string_messages_and_returns_content(self):
        server = _OpenAICompatibleTestServer(
            [{"choices": [{"message": {"content": "adapter response"}}]}]
        )
        try:
            llm = OpenAICompatibleCrewAILLM(
                model="private-model",
                base_url=server.base_url,
                api_key="secret",
                timeout_seconds=5,
                temperature=0.1,
            )

            result = llm.call("hello")

            self.assertEqual(result, "adapter response")
            request_payload = server.requests[0]
            self.assertEqual(request_payload["headers"]["authorization"], "Bearer secret")
            self.assertEqual(request_payload["body"]["model"], "private-model")
            self.assertEqual(request_payload["body"]["messages"], [{"role": "user", "content": "hello"}])
            self.assertEqual(request_payload["body"]["temperature"], 0.1)
        finally:
            server.close()

    def test_openai_compatible_llm_posts_list_messages_and_tools(self):
        server = _OpenAICompatibleTestServer(
            [{"choices": [{"message": {"content": "tool-aware response"}}]}]
        )
        try:
            llm = OpenAICompatibleCrewAILLM(
                model="private-model",
                base_url=f"{server.base_url}/v1",
                api_key=None,
                timeout_seconds=5,
            )
            tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]

            result = llm.call([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], tools=tools)

            self.assertEqual(result, "tool-aware response")
            self.assertEqual(server.requests[0]["body"]["messages"][0]["role"], "system")
            self.assertEqual(server.requests[0]["body"]["tools"], tools)
            self.assertNotIn("authorization", server.requests[0]["headers"])
        finally:
            server.close()

    def test_openai_compatible_llm_raises_readable_http_error(self):
        server = _OpenAICompatibleTestServer([{"status": 500, "body": {"error": "failed"}}])
        try:
            llm = OpenAICompatibleCrewAILLM(
                model="private-model",
                base_url=server.base_url,
                api_key=None,
                timeout_seconds=5,
            )

            with self.assertRaisesRegex(OpenAICompatibleLLMError, "HTTP 500"):
                llm.call("hello")
        finally:
            server.close()

    def test_openai_compatible_llm_rejects_missing_content(self):
        server = _OpenAICompatibleTestServer([{"choices": [{"message": {}}]}])
        try:
            llm = OpenAICompatibleCrewAILLM(
                model="private-model",
                base_url=server.base_url,
                api_key=None,
                timeout_seconds=5,
            )

            with self.assertRaisesRegex(OpenAICompatibleLLMError, "choices\\[0\\].message.content"):
                llm.call("hello")
        finally:
            server.close()

    def test_openai_compatible_llm_raises_readable_transport_error(self):
        llm = OpenAICompatibleCrewAILLM(
            model="private-model",
            base_url="http://127.0.0.1:1",
            api_key=None,
            timeout_seconds=0.1,
        )

        with self.assertRaisesRegex(OpenAICompatibleLLMError, "request failed|timed out"):
            llm.call("hello")

    def test_backend_failure_cache_is_endpoint_scoped_and_expires(self):
        backend = _RecoveringCrewBackend()
        with patch.dict(os.environ, {"AI_JSUNPACK_AGENT_FAILURE_CACHE_SECONDS": "0.01"}, clear=False):
            adapter = CrewAIExecutionAdapter(backend=backend)
        spec = CrewRuntimePlanner().build_specs()[0]
        policy = ModelPolicyResolver().resolve(
            self._request(cloud_mode="cloud_allowed", config={"agentModel": "model-a"})
        )
        args = {
            "spec": spec,
            "policy": policy,
            "prompt_context": {},
            "input_artifact_ids": [],
            "evidence_refs": [],
        }

        first = adapter.execute_agent(**args)
        cached = adapter.execute_agent(**args)
        time.sleep(0.02)
        recovered = adapter.execute_agent(**args)

        self.assertEqual(first.status, "fail")
        self.assertEqual(cached.status, "fail")
        self.assertEqual(recovered.status, "best_effort")
        self.assertEqual(backend.calls, 2)

    def test_agent_plan_rejects_missing_and_same_stage_dependencies(self):
        planner = CrewRuntimePlanner()
        base = CrewAgentSpec(
            name="AnalysisAgent",
            stage="analysis",
            responsibility="Analyze",
            role="Analysis",
            goal="Analyze",
            backstory="Analysis",
            output_kind="inference_record",
            allow_parallel=False,
        )
        with self.assertRaisesRegex(Exception, "missing agent"):
            planner.build_stage_order([base, CrewAgentSpec(**{**base.__dict__, "name": "Other", "dependencies": ["Missing"]})])
        with self.assertRaisesRegex(Exception, "earlier stage"):
            planner.build_stage_order([base, CrewAgentSpec(**{**base.__dict__, "name": "Other", "dependencies": ["AnalysisAgent"]})])

    def test_parallel_stage_executes_concurrently_and_receives_dependency_outputs(self):
        barrier = threading.Barrier(3)
        adapter = _BarrierExecutionAdapter(barrier)
        manager = CrewExecutionManager(adapter=adapter)
        specs = [
            CrewAgentSpec(
                name=f"Specialist{index}",
                stage="specialists",
                responsibility="Analyze",
                role="Specialist",
                goal="Analyze",
                backstory="Specialist",
                output_kind="inference_record",
                allow_parallel=True,
            )
            for index in range(3)
        ]
        request = self._request(cloud_mode="cloud_allowed", config={"agentModel": "model-a"})
        context = SimpleNamespace(
            request=request,
            memory_artifact_ids=[],
            knowledge_artifact=SimpleNamespace(id="knowledge"),
            tool_registry_artifact=SimpleNamespace(id="tools"),
            evidence_refs=[],
            policy=ModelPolicyResolver().resolve(request),
            prompt_context={},
        )
        with patch.object(manager, "_aggregate_provider_draft", return_value=SimpleNamespace()):
            stages, _ = manager.execute(context=context, specs=specs)

        specialist_stage = next(stage for stage in stages if stage.stage == "specialists")
        self.assertEqual([execution.status for execution in specialist_stage.agent_executions], ["pass"] * 3)
        self.assertEqual(barrier.n_waiting, 0)

        dependency = specialist_stage.agent_executions[0]
        dependent_spec = CrewAgentSpec(
            name="ReportAgent",
            stage="synthesis",
            responsibility="Report",
            role="Report",
            goal="Report",
            backstory="Report",
            output_kind="report_section",
            allow_parallel=False,
            dependencies=[dependency.spec.name],
        )
        prompt = manager._prompt_context_for_agent(
            context=context,
            spec=dependent_spec,
            stages=stages,
            executions_by_name={dependency.spec.name: dependency},
        )
        self.assertEqual(prompt["dependencyOutputs"][dependency.spec.name]["rawOutput"]["agent"], dependency.spec.name)

    def test_agent_execution_artifact_records_endpoint_metadata_without_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                plan_artifact = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="agent_plan",
                    stage="agent_planning",
                    filename="agent-plan.json",
                    payload={"kind": "agent_plan"},
                )
                tool_registry_artifact = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="tool_registry",
                    stage="agent_planning",
                    filename="tool-registry.json",
                    payload={"kind": "tool_registry"},
                )
                knowledge_artifact = self._write_json_artifact(
                    store=store,
                    job_id=job.id,
                    kind="knowledge_evidence",
                    stage="agent_planning",
                    filename="knowledge.json",
                    payload={"kind": "knowledge_evidence"},
                )
                spec = CrewAgentSpec(
                    name="PlannerAgent",
                    stage="planner",
                    responsibility="Plan",
                    role="Planner",
                    goal="Plan",
                    backstory="Planner",
                    output_kind="agent_plan",
                    allow_parallel=False,
                )
                execution = CrewAgentExecution(
                    spec=spec,
                    status="pass",
                    failure_class="none",
                    attempt=0,
                    duration_ms=1.0,
                    input_artifact_ids=[],
                    evidence_refs=[],
                    message="done",
                    model_provider="openai-compatible",
                    model_name="private-model",
                    model_base_url_configured=True,
                    model_api_key_configured=True,
                    model_custom_endpoint_enabled=True,
                    model_timeout_seconds=42.0,
                    model_temperature=0.3,
                )
                stages = [
                    CrewStageExecution(
                        stage="planner",
                        status="pass",
                        agent_executions=[execution],
                        duration_ms=1.0,
                        failure_class="none",
                    )
                ]

                artifacts = AgentArtifactWriter()._write_agent_execution_artifacts(
                    job_id=job.id,
                    store=store,
                    stages=stages,
                    plan_artifact=plan_artifact,
                    tool_registry_artifact=tool_registry_artifact,
                    knowledge_artifact=knowledge_artifact,
                    base_input_artifact_ids=[],
                    model_provider="openai-compatible",
                    model_name="private-model",
                )
                agent_payload = json.loads(store.read_artifact(job.id, artifacts[0].id))
                serialized = json.dumps(agent_payload)

                self.assertTrue(agent_payload["modelBaseUrlConfigured"])
                self.assertTrue(agent_payload["modelApiKeyConfigured"])
                self.assertTrue(agent_payload["modelCustomEndpointEnabled"])
                self.assertEqual(agent_payload["modelTimeoutSeconds"], 42.0)
                self.assertEqual(agent_payload["modelTemperature"], 0.3)
                self.assertNotIn("secret", serialized)
                self.assertNotIn("http://", serialized)
            finally:
                store.close()

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
                plan_payload = json.loads(store.read_artifact(job.id, result.plan_artifact.id))
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
                self.assertTrue(result.agent_execution_artifacts)
                self.assertIn("agentGraph", plan_payload)
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
                self.assertIn("stagePlan", agent_plan_payload)
                self.assertTrue(result.agent_execution_artifacts)
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


class _RecoveringCrewBackend:
    def __init__(self) -> None:
        self.calls = 0

    def run_agent(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary endpoint failure")
        return CrewStructuredAgentOutput()


class _BarrierExecutionAdapter:
    tool_name = "test.agent"
    tool_version = "1"

    def __init__(self, barrier: threading.Barrier) -> None:
        self.barrier = barrier

    def execute_agent(self, *, spec, policy, prompt_context, input_artifact_ids, evidence_refs):
        del prompt_context
        self.barrier.wait(timeout=1)
        return CrewAgentExecution(
            spec=spec,
            status="pass",
            failure_class="none",
            attempt=0,
            duration_ms=0.0,
            input_artifact_ids=input_artifact_ids,
            evidence_refs=evidence_refs,
            message="done",
            raw_output={"agent": spec.name},
            model_provider=policy.model_provider,
            model_name=policy.model_name,
        )


class _OpenAICompatibleTestServer:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length)
                owner.requests.append(
                    {
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "body": json.loads(raw_body.decode("utf-8")),
                    }
                )
                response = owner.responses.pop(0)
                status = int(response.get("status", 200))
                body = response.get("body", response if "status" not in response else {})
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args):  # noqa: A002
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
