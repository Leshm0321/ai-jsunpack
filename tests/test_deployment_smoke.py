import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from apps.api.app.deployment_smoke import DeploymentSmokeConfig, main, run_deployment_smoke


ROOT = Path(__file__).resolve().parents[1]


class DeploymentSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        npm = shutil.which("npm")
        node = shutil.which("node")
        if npm is None or node is None:
            raise unittest.SkipTest("部署冒烟检查需要 npm 和 node")

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

    def test_local_deployment_smoke_report_contains_release_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "deployment-smoke.json"

            report = run_deployment_smoke(
                DeploymentSmokeConfig(
                    output_path=str(output_path),
                    soak_instances=1,
                    soak_workers_per_instance=1,
                    soak_runs=1,
                    soak_capture_delay_ms=0,
                )
            )

            persisted = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass")
            self.assertEqual(persisted["kind"], "deployment_smoke_report")
            self.assertEqual(persisted["status"], "pass")
            self.assertEqual(persisted["config"]["auth_secret"], "<redacted>")
            self.assertGreaterEqual(persisted["webhookPayloadCount"], 1)

            checks = {check["name"]: check for check in persisted["checks"]}
            for name in (
                "api_health",
                "api_create_job",
                "api_upload_source",
                "worker_pipeline_packaged_result",
                "api_latest_runtime_validation",
                "api_reports_list",
                "api_result_package_download",
                "ops_metrics",
                "ops_prometheus",
                "ops_alert_events_and_webhook",
                "ops_alert_event_history",
                "retention_cleanup_dry_run",
                "retention_cleanup_execute",
                "retention_deleted_artifact_hidden",
                "browser_runner_soak_baseline",
                "archive_manifest_complete",
            ):
                self.assertEqual(checks[name]["status"], "pass", name)

            self.assertGreater(persisted["result_package"]["bytes"], 0)
            archive_manifest = persisted["archive_manifest"]
            self.assertEqual(archive_manifest["kind"], "deployment_smoke_archive_manifest")
            self.assertEqual(archive_manifest["topologyMode"], "ephemeral_local")
            self.assertFalse(archive_manifest["archiveReady"])
            self.assertGreater(archive_manifest["artifactCount"], 0)
            self.assertIn("result_package", archive_manifest["artifactKinds"])
            self.assertEqual(
                archive_manifest["retainedEvidence"]["resultPackageSha256"],
                persisted["result_package"]["sha256"],
            )
            self.assertEqual(archive_manifest["retainedEvidence"]["alertDeliveryStatus"], "delivered")
            self.assertTrue(archive_manifest["retainedEvidence"]["prometheusScraped"])
            self.assertEqual(persisted["alerts"]["json"]["delivery"]["status"], "delivered")
            self.assertGreater(persisted["retention_dry_run"]["json"]["candidateCount"], 0)
            self.assertEqual(
                persisted["retention_execute"]["json"]["deletedCount"],
                persisted["retention_execute"]["json"]["candidateCount"],
            )
            self.assertEqual(
                persisted["soak_result"]["backendAssessment"]["recommendation"],
                "continue_shared_db_backend",
            )
            self.assertFalse(persisted["soak_result"]["backendAssessment"]["messageQueueMigrationRequired"])

    def test_deployment_smoke_marks_retained_archive_ready_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_path = root / "deployment-smoke.json"
            artifact_root = root / "artifacts"
            database_url = f"sqlite:///{(root / 'metadata.db').as_posix()}"

            report = run_deployment_smoke(
                DeploymentSmokeConfig(
                    database_url=database_url,
                    artifact_root=str(artifact_root),
                    output_path=str(output_path),
                    soak_instances=1,
                    soak_workers_per_instance=1,
                    soak_runs=1,
                    soak_capture_delay_ms=0,
                )
            )

            archive_manifest = report["archive_manifest"]
            self.assertEqual(report["status"], "pass")
            self.assertEqual(archive_manifest["topologyMode"], "retained_local")
            self.assertTrue(archive_manifest["archiveReady"])
            self.assertEqual(archive_manifest["outputPath"], str(output_path))
            self.assertEqual(archive_manifest["artifactRoot"], str(artifact_root))
            self.assertTrue(output_path.exists())
            self.assertTrue(any(artifact_root.rglob("*")))

    def test_cli_returns_nonzero_and_writes_failure_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "deployment-smoke-failed.json"
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--output",
                        str(output_path),
                        "--soak-runs",
                        "0",
                        "--soak-instances",
                        "1",
                        "--soak-workers-per-instance",
                        "1",
                    ]
                )

            persisted = json.loads(output_path.read_text(encoding="utf-8"))
            printed = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertEqual(persisted["status"], "fail")
            self.assertEqual(printed["status"], "fail")
            self.assertIn("deployment_smoke_exception", persisted["failedChecks"])


if __name__ == "__main__":
    unittest.main()
