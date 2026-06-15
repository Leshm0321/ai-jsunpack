import json
import sys
import tempfile
import unittest
from pathlib import Path

from apps.api.app.models import CreateJobRequest
from apps.api.app.store import create_store
from apps.worker.worker.build_validation import BuildValidationRunner
from packages.sandbox import LocalSandboxRunner, SandboxPolicy


class BuildValidationRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = create_store(
            database_url=f"sqlite:///{(self.root / 'metadata.db').as_posix()}",
            artifact_root=self.root / "artifacts",
        )
        self.job = self.store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_records_best_effort_when_generated_project_is_missing(self):
        result = BuildValidationRunner().run(
            job_id=self.job.id,
            store=self.store,
            parent_artifact_ids=["artifact_parent"],
        )

        log_artifacts = self.store.list_artifacts(self.job.id, kind="build_log")
        review_artifacts = self.store.list_artifacts(self.job.id, kind="review_run")
        log_payloads = [
            json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8")) for artifact in log_artifacts
        ]
        review_payloads = [
            json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8")) for artifact in review_artifacts
        ]

        self.assertEqual(result.build.review_run.status, "best_effort")
        self.assertEqual(result.typecheck.review_run.status, "best_effort")
        self.assertEqual({payload["reviewType"] for payload in log_payloads}, {"build", "typecheck"})
        self.assertTrue(all(payload["status"] == "best_effort" for payload in log_payloads))
        self.assertTrue(all(payload["failureClass"] == "none" for payload in review_payloads))
        self.assertTrue(all(payload["logsArtifactId"] for payload in review_payloads))
        self.assertIn("deterministic writer", log_payloads[0]["limitations"][0])

    def test_runs_project_commands_and_classifies_build_failure(self):
        project_root = self.root / "generated"
        project_root.mkdir()
        (project_root / "package.json").write_text("{}", encoding="utf-8")
        runner = BuildValidationRunner(
            sandbox_runner=LocalSandboxRunner(SandboxPolicy(allowed_commands=((sys.executable,),))),
            build_command=(sys.executable, "-c", "import sys; print('build bad'); sys.exit(7)"),
            typecheck_command=(sys.executable, "-c", "print('type ok')"),
        )

        result = runner.run(job_id=self.job.id, store=self.store, project_path=project_root)

        build_log = json.loads(Path(result.build.log_artifact.storage_uri).read_text(encoding="utf-8"))
        typecheck_log = json.loads(Path(result.typecheck.log_artifact.storage_uri).read_text(encoding="utf-8"))
        self.assertEqual(result.build.review_run.status, "fail")
        self.assertEqual(result.build.review_run.failure_class, "build_error")
        self.assertEqual(result.typecheck.review_run.status, "pass")
        self.assertEqual(result.typecheck.review_run.failure_class, "none")
        self.assertEqual(build_log["exitCode"], 7)
        self.assertIn("build bad", build_log["stdout"])
        self.assertEqual(typecheck_log["status"], "pass")
        self.assertIn("type ok", typecheck_log["stdout"])


if __name__ == "__main__":
    unittest.main()
