import json
import tempfile
import unittest
from pathlib import Path

from deploy.compose_smoke import ComposeSmokeConfig, compose_command_plan, main, run_compose_smoke


ROOT = Path(__file__).resolve().parents[1]


class ComposeSmokeTest(unittest.TestCase):
    def test_compose_file_declares_builds_healthchecks_and_bucket_init(self):
        compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

        for dockerfile in (
            "deploy/docker/api.Dockerfile",
            "deploy/docker/worker.Dockerfile",
            "deploy/docker/browser-runner.Dockerfile",
            "deploy/docker/web.Dockerfile",
        ):
            self.assertIn(dockerfile, compose)

        self.assertIn("artifact-store-init:", compose)
        self.assertIn("mc mb --ignore-existing", compose)
        self.assertIn("condition: service_healthy", compose)
        self.assertIn("condition: service_completed_successfully", compose)
        self.assertIn("pg_isready", compose)
        self.assertIn("/minio/health/ready", compose)
        self.assertIn("http://127.0.0.1:8000/health", compose)
        self.assertIn("http://127.0.0.1:5173/", compose)

    def test_dockerfiles_exist_and_use_service_entrypoints(self):
        dockerfiles = {
            "api.Dockerfile": "apps.api.app.main:app",
            "worker.Dockerfile": "apps.worker.worker.queue",
            "browser-runner.Dockerfile": "apps.browser_runner.app.main:app",
            "web.Dockerfile": "web-server.mjs",
        }

        for name, expected in dockerfiles.items():
            content = (ROOT / "deploy" / "docker" / name).read_text(encoding="utf-8")
            self.assertIn(expected, content)
            self.assertNotIn("dev_docs", content)

    def test_compose_smoke_dry_run_writes_command_plan_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "compose-smoke.json"
            report = run_compose_smoke(ComposeSmokeConfig(output_path=output_path, dry_run=True))

            persisted = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass")
            self.assertEqual(persisted["kind"], "compose_smoke_report")
            self.assertEqual(persisted["status"], "pass")
            self.assertIn("commands", persisted)
            self.assertGreaterEqual(len(persisted["commands"]), 4)
            self.assertEqual(persisted["config"]["database_url"], "postgresql+psycopg://<redacted>@127.0.0.1:5432/ai_jsunpack")
            self.assertEqual(persisted["config"]["artifact_store_endpoint_url"], "http://127.0.0.1:9000")
            self.assertEqual(persisted["config"]["artifact_store_secret_key"], "<redacted>")

            checks = {check["name"]: check for check in persisted["checks"]}
            self.assertEqual(checks["compose_command_plan"]["status"], "pass")
            self.assertEqual(checks["compose_file_present"]["status"], "pass")

    def test_compose_smoke_plan_includes_profiles_and_deployment_smoke(self):
        config = ComposeSmokeConfig(dry_run=True, skip_build=True, profiles=("worker", "browser-runner"))
        commands = compose_command_plan(config)
        flattened = [" ".join(command) for command in commands]

        self.assertFalse(any(command[-1] == "build" for command in commands))
        self.assertTrue(any("--profile worker" in command for command in flattened))
        self.assertTrue(any("--profile browser-runner" in command for command in flattened))
        self.assertTrue(any("apps.api.app.deployment_smoke" in command for command in flattened))

    def test_compose_smoke_cli_dry_run_returns_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "compose-smoke.json"
            exit_code = main(["--dry-run", "--output", str(output_path)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8"))["status"], "pass")


if __name__ == "__main__":
    unittest.main()
