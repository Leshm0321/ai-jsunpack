import json
import shutil
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

    def test_skips_dependency_install_without_policy(self):
        project_root = self.root / "generated"
        project_root.mkdir()
        (project_root / "package.json").write_text(
            json.dumps(
                {
                    "dependencies": {
                        "left-pad": "1.3.0",
                    }
                }
            ),
            encoding="utf-8",
        )
        runner = BuildValidationRunner(
            sandbox_runner=LocalSandboxRunner(SandboxPolicy(allowed_commands=((sys.executable,),))),
            build_command=(sys.executable, "-c", "print('build ok')"),
            typecheck_command=(sys.executable, "-c", "print('type ok')"),
        )

        result = runner.run(job_id=self.job.id, store=self.store, project_path=project_root)

        log_payloads = [
            json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))
            for artifact in self.store.list_artifacts(self.job.id, kind="build_log")
        ]
        install_log = next(payload for payload in log_payloads if payload["phase"] == "install")
        self.assertEqual(install_log["status"], "best_effort")
        self.assertEqual(install_log["failureClass"], "dependency_missing")
        self.assertIn("disabled by policy", install_log["decision"])
        self.assertEqual(result.build.review_run.status, "pass")
        self.assertEqual(result.typecheck.review_run.status, "pass")

    def test_uses_npm_package_scripts_when_declared(self):
        if shutil.which("npm") is None or shutil.which("node") is None:
            raise unittest.SkipTest("npm and node are required for package script validation")

        project_root = self.root / "generated"
        project_root.mkdir()
        (project_root / "package.json").write_text(
            json.dumps(
                {
                    "scripts": {
                        "build": "node -e \"console.log('package build ok')\"",
                        "typecheck": "node -e \"console.log('package typecheck ok')\"",
                    }
                }
            ),
            encoding="utf-8",
        )

        result = BuildValidationRunner().run(job_id=self.job.id, store=self.store, project_path=project_root)

        build_log = json.loads(Path(result.build.log_artifact.storage_uri).read_text(encoding="utf-8"))
        typecheck_log = json.loads(Path(result.typecheck.log_artifact.storage_uri).read_text(encoding="utf-8"))
        self.assertEqual(build_log["commandSource"], "npm_script")
        self.assertEqual(typecheck_log["commandSource"], "npm_script")
        self.assertEqual(build_log["scriptName"], "build")
        self.assertEqual(typecheck_log["scriptName"], "typecheck")
        self.assertIn("package build ok", build_log["stdout"])
        self.assertIn("package typecheck ok", typecheck_log["stdout"])

    def test_failed_attempt_writes_repair_instruction_and_repaired_project_artifact(self):
        project_root = self.root / "generated"
        (project_root / "scripts").mkdir(parents=True)
        (project_root / "package.json").write_text("{}", encoding="utf-8")
        (project_root / "scripts" / "build.mjs").write_text("console.log('shim build')", encoding="utf-8")
        (project_root / "scripts" / "typecheck.mjs").write_text("console.log('shim typecheck')", encoding="utf-8")
        runner = BuildValidationRunner(
            sandbox_runner=LocalSandboxRunner(SandboxPolicy(allowed_commands=((sys.executable,),))),
            build_command=(sys.executable, "-c", "import sys; print('build still bad'); sys.exit(7)"),
            typecheck_command=(sys.executable, "-c", "print('type ok')"),
        )

        result = runner.run(job_id=self.job.id, store=self.store, project_path=project_root)

        repair_artifacts = self.store.list_artifacts(self.job.id, kind="repair_instruction")
        generated_project_artifacts = self.store.list_artifacts(self.job.id, kind="generated_project")
        review_payloads = [
            json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))
            for artifact in self.store.list_artifacts(self.job.id, kind="review_run")
        ]
        repair_payload = json.loads(Path(repair_artifacts[0].storage_uri).read_text(encoding="utf-8"))
        repaired_package = json.loads(
            (Path(generated_project_artifacts[0].storage_uri) / "package.json").read_text(encoding="utf-8")
        )
        build_attempt_zero_review = next(
            payload for payload in review_payloads if payload["reviewType"] == "build" and payload["attempt"] == 0
        )

        self.assertEqual(result.build.review_run.attempt, 1)
        self.assertEqual(result.build.review_run.status, "fail")
        self.assertEqual(repair_payload["status"], "applied")
        self.assertEqual(repair_payload["attempt"], 1)
        self.assertEqual(repair_payload["actions"][0]["path"], "package.json:scripts.build")
        self.assertEqual(generated_project_artifacts[0].attempt, 1)
        self.assertEqual(repaired_package["scripts"]["build"], "node scripts/build.mjs")
        self.assertEqual(build_attempt_zero_review["repairInstructionIds"], [repair_artifacts[0].id])


if __name__ == "__main__":
    unittest.main()
