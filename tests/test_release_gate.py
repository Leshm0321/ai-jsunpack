import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from deploy.release_gate import ReleaseGateConfig, main, run_release_gate


class ReleaseGateTest(unittest.TestCase):
    def test_dry_run_writes_release_report_with_pinned_images_and_gates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "release-gate.json"
            report = run_release_gate(
                ReleaseGateConfig(
                    registry="registry.example.com",
                    repository_prefix="security/ai-jsunpack",
                    version="2026.06.26",
                    git_sha="abcdef1234567890",
                    previous_version="2026.06.25",
                    output_path=output_path,
                )
            )

            persisted = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass")
            self.assertEqual(persisted["kind"], "release_gate_report")
            self.assertEqual(persisted["mode"], "dry_run")
            self.assertEqual(persisted["config"]["git_sha"], "abcdef123456")

            services = {image["service"]: image for image in persisted["images"]}
            self.assertEqual(
                services["api"]["versionTag"],
                "registry.example.com/security/ai-jsunpack/api:2026.06.26",
            )
            self.assertEqual(
                services["worker"]["gitShaTag"],
                "registry.example.com/security/ai-jsunpack/worker:abcdef123456",
            )
            self.assertEqual(
                services["web"]["rollbackTag"],
                "registry.example.com/security/ai-jsunpack/web:2026.06.25",
            )

            compose_env = persisted["commandPlan"]["composeSmokeGate"]["environment"]
            self.assertEqual(
                compose_env["AI_JSUNPACK_BROWSER_RUNNER_IMAGE"],
                "registry.example.com/security/ai-jsunpack/browser-runner:2026.06.26",
            )
            self.assertIn("deploymentSmoke.archive_manifest.archiveReady=true", persisted["releaseGates"][3]["evidence"])
            self.assertEqual(persisted["failedChecks"], [])

    def test_dry_run_can_disable_optional_sbom_and_scan_tools(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_release_gate(
                ReleaseGateConfig(
                    registry="registry.example.com",
                    repository_prefix="ai-jsunpack",
                    version="v1.2.3",
                    git_sha="abcdef123456",
                    output_path=Path(temp_dir) / "release-gate.json",
                    sbom_tool="none",
                    scan_tool="disabled",
                )
            )

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["commandPlan"]["sbom"], [])
            self.assertEqual(report["commandPlan"]["scan"], [])
            gates = {gate["name"]: gate for gate in report["releaseGates"]}
            self.assertFalse(gates["sbom_generation"]["required"])
            self.assertFalse(gates["vulnerability_scan"]["required"])

    def test_report_contains_secret_names_without_secret_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_release_gate(
                ReleaseGateConfig(
                    registry="registry.example.com",
                    repository_prefix="ai-jsunpack",
                    version="v1",
                    git_sha="abcdef",
                    output_path=Path(temp_dir) / "release-gate.json",
                )
            )
            serialized = json.dumps(report, ensure_ascii=False)

            self.assertIn("AI_JSUNPACK_AUTH_SECRET", serialized)
            self.assertIn("AI_JSUNPACK_ARTIFACT_S3_SECRET_ACCESS_KEY", serialized)
            self.assertNotIn("replace-with-a-shared-hmac-secret", serialized)
            self.assertNotIn("replace-with-minio-secret", serialized)

    def test_cli_dry_run_returns_success_and_prints_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "release-gate.json"
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--registry",
                        "registry.example.com",
                        "--repository-prefix",
                        "ai-jsunpack",
                        "--version",
                        "v1",
                        "--git-sha",
                        "1234567890abcdef",
                        "--output",
                        str(output_path),
                        "--dry-run",
                    ]
                )

            persisted = json.loads(output_path.read_text(encoding="utf-8"))
            printed = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(persisted["status"], "pass")
            self.assertEqual(printed["images"][0]["gitShaTag"], "registry.example.com/ai-jsunpack/api:1234567890ab")

    def test_cli_rejects_invalid_version(self):
        with self.assertRaises(ValueError):
            main(
                [
                    "--registry",
                    "registry.example.com",
                    "--repository-prefix",
                    "ai-jsunpack",
                    "--version",
                    "bad tag",
                    "--git-sha",
                    "123456",
                ]
            )


if __name__ == "__main__":
    unittest.main()
