import unittest

from packages.deployment import (
    DeploymentConfigurationError,
    validate_current_environment,
    validate_service_environment,
)


class DeploymentConfigTest(unittest.TestCase):
    def test_api_profile_allows_http_database_and_artifact_store_config(self):
        profile = validate_service_environment(
            "api",
            environ={
                "AI_JSUNPACK_SERVICE_ROLE": "api",
                "AI_JSUNPACK_AUTH_SECRET": "secret",
                "AI_JSUNPACK_CORS_ORIGINS": "http://127.0.0.1:5173",
                "AI_JSUNPACK_DATABASE_URL": "postgresql+psycopg://api:api@db:5432/ai_jsunpack",
                "AI_JSUNPACK_ARTIFACT_STORE": "minio",
                "AI_JSUNPACK_ARTIFACT_S3_BUCKET": "artifacts",
                "AI_JSUNPACK_ARTIFACT_S3_ENDPOINT_URL": "http://artifact-store:9000",
            },
        )

        self.assertEqual(profile.status, "ok")
        self.assertEqual(profile.violations, ())

    def test_api_profile_rejects_worker_and_browser_execution_config(self):
        profile = validate_service_environment(
            "api",
            environ={
                "AI_JSUNPACK_SERVICE_ROLE": "api",
                "AI_JSUNPACK_SANDBOX_RUNNER": "container",
                "AI_JSUNPACK_SANDBOX_IMAGE": "node:20-bookworm-slim",
                "AI_JSUNPACK_AGENT_MODEL": "gpt-example",
                "AI_JSUNPACK_AGENT_BASE_URL": "https://agent.example.test",
                "AI_JSUNPACK_LOCAL_AGENT_API_KEY": "local-secret",
                "AI_JSUNPACK_BROWSER_RUNNER_URL": "http://browser-runner:9222",
                "OPENAI_API_KEY": "secret",
            },
        )

        violation_names = {violation.name for violation in profile.violations}
        self.assertEqual(profile.status, "invalid")
        self.assertIn("AI_JSUNPACK_SANDBOX_RUNNER", violation_names)
        self.assertIn("AI_JSUNPACK_AGENT_MODEL", violation_names)
        self.assertIn("AI_JSUNPACK_AGENT_BASE_URL", violation_names)
        self.assertIn("AI_JSUNPACK_LOCAL_AGENT_API_KEY", violation_names)
        self.assertIn("AI_JSUNPACK_BROWSER_RUNNER_URL", violation_names)
        self.assertIn("OPENAI_API_KEY", violation_names)

    def test_current_api_environment_is_strict_when_service_role_is_explicit(self):
        with self.assertRaises(DeploymentConfigurationError) as context:
            validate_current_environment(
                "api",
                environ={
                    "AI_JSUNPACK_SERVICE_ROLE": "api",
                    "AI_JSUNPACK_SANDBOX_RUNNER": "container",
                },
            )

        self.assertIn("AI_JSUNPACK_SANDBOX_RUNNER", str(context.exception))

    def test_current_api_environment_reports_warning_without_explicit_service_role(self):
        profile = validate_current_environment(
            "api",
            environ={
                "AI_JSUNPACK_SANDBOX_RUNNER": "container",
            },
        )

        self.assertFalse(profile.strict)
        self.assertEqual(profile.status, "warning")
        self.assertEqual(profile.violations[0].name, "AI_JSUNPACK_SANDBOX_RUNNER")

    def test_current_environment_rejects_role_mismatch(self):
        with self.assertRaises(DeploymentConfigurationError) as context:
            validate_current_environment(
                "api",
                environ={
                    "AI_JSUNPACK_SERVICE_ROLE": "worker",
                },
            )

        self.assertIn("预期为 'api'", str(context.exception))

    def test_worker_profile_accepts_execution_config(self):
        profile = validate_service_environment(
            "worker",
            environ={
                "AI_JSUNPACK_SERVICE_ROLE": "worker",
                "AI_JSUNPACK_SANDBOX_RUNNER": "container",
                "AI_JSUNPACK_SANDBOX_IMAGE": "node:20-bookworm-slim",
                "AI_JSUNPACK_AGENT_MODEL": "gpt-example",
                "AI_JSUNPACK_AGENT_BASE_URL": "https://agent.example.test",
                "AI_JSUNPACK_AGENT_API_KEY": "secret",
                "AI_JSUNPACK_AGENT_TIMEOUT_SECONDS": "30",
                "AI_JSUNPACK_AGENT_TEMPERATURE": "0.2",
                "AI_JSUNPACK_LOCAL_AGENT_MODEL": "local-example",
                "AI_JSUNPACK_LOCAL_AGENT_BASE_URL": "http://host.docker.internal:11434/v1",
                "AI_JSUNPACK_LOCAL_AGENT_API_KEY": "local-secret",
                "AI_JSUNPACK_CORE_CLI_PATH": "packages/core/dist/cli.js",
                "AI_JSUNPACK_CREWAI_DATA_ROOT": "/var/lib/ai-jsunpack/crewai",
            },
        )

        self.assertEqual(profile.status, "ok")
        self.assertEqual(profile.violations, ())


if __name__ == "__main__":
    unittest.main()
