import unittest

from apps.api.app.models import EvidenceRef
from apps.worker.worker.agent_runtime import AgentContextRedactor, AgentRuntimeRequest, ModelPolicyResolver


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


if __name__ == "__main__":
    unittest.main()
