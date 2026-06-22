import json
import tempfile
import unittest
from pathlib import Path

from apps.browser_runner.benchmark import BrowserRunnerSoakConfig, run_browser_runner_soak


class BrowserRunnerBenchmarkTest(unittest.TestCase):
    def test_soak_baseline_reports_stable_shared_backend_metrics(self):
        result = run_browser_runner_soak(
            BrowserRunnerSoakConfig(
                instances=2,
                workers_per_instance=1,
                runs=6,
                capture_delay_ms=150,
                poll_seconds=0.01,
                timeout_seconds=5,
            )
        )

        self.assertEqual(result["kind"], "browser_runner_soak_baseline")
        self.assertEqual(result["submittedCount"], 6)
        self.assertEqual(result["completedCount"], 6)
        self.assertEqual(result["statusCounts"]["pass"], 6)
        self.assertGreater(result["throughputRunsPerSecond"], 0)
        self.assertEqual(result["queueMetrics"]["queueBackend"], "postgresql")
        self.assertEqual(result["queueMetrics"]["backendStatus"], "ok")
        self.assertEqual(result["queueMetrics"]["queuedCount"], 0)
        self.assertEqual(result["queueMetrics"]["runningCount"], 0)
        self.assertEqual(result["queueHealth"]["status"], "ok")
        self.assertGreaterEqual(result["syntheticAdapter"]["maxActiveCaptures"], 2)
        self.assertEqual(result["backendAssessment"]["recommendation"], "continue_shared_db_backend")
        self.assertFalse(result["backendAssessment"]["messageQueueMigrationRequired"])

        recovery = result["recoveryProbe"]
        self.assertEqual(recovery["recoveredCount"], 1)
        self.assertEqual(recovery["status"], "pass")
        self.assertEqual(recovery["attempt"], 2)
        self.assertTrue(recovery["leaseRecovered"])
        self.assertEqual(recovery["queueBackend"], "postgresql")

    def test_soak_baseline_writes_json_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "baseline.json"
            result = run_browser_runner_soak(
                BrowserRunnerSoakConfig(
                    instances=1,
                    workers_per_instance=1,
                    runs=1,
                    capture_delay_ms=0,
                    output_path=str(output_path),
                    include_recovery_probe=False,
                )
            )

            persisted = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["kind"], "browser_runner_soak_baseline")
            self.assertEqual(persisted["submittedCount"], 1)
            self.assertEqual(persisted["completedCount"], 1)
            self.assertEqual(persisted["backendAssessment"], result["backendAssessment"])


if __name__ == "__main__":
    unittest.main()
