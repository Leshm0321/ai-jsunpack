import unittest

from apps.worker.worker.agent_runtime import AgentRuntimeRequest, ModelPolicyResolver


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


if __name__ == "__main__":
    unittest.main()
