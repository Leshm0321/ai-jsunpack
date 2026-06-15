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
        build_artifacts = self.store.list_artifacts(self.job.id, kind="build_artifact")
        review_artifacts = self.store.list_artifacts(self.job.id, kind="review_run")
        log_payloads = [
            json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8")) for artifact in log_artifacts
        ]
        build_payloads = [
            json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8")) for artifact in build_artifacts
        ]
        review_payloads = [
            json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8")) for artifact in review_artifacts
        ]

        self.assertEqual(result.build.review_run.status, "best_effort")
        self.assertEqual(result.typecheck.review_run.status, "best_effort")
        self.assertEqual({payload["reviewType"] for payload in build_payloads}, {"build", "typecheck"})
        self.assertTrue(all(payload["status"] == "best_effort" for payload in build_payloads))
        self.assertTrue(all(payload["resourcePolicy"]["enforcement"] == "local_best_effort" for payload in build_payloads))
        self.assertTrue(all(payload["logsArtifactId"] for payload in build_payloads))
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
        build_artifact = json.loads(Path(result.build.build_artifact.storage_uri).read_text(encoding="utf-8"))
        typecheck_log = json.loads(Path(result.typecheck.log_artifact.storage_uri).read_text(encoding="utf-8"))
        self.assertEqual(result.build.review_run.status, "fail")
        self.assertEqual(result.build.review_run.failure_class, "build_error")
        self.assertEqual(result.typecheck.review_run.status, "pass")
        self.assertEqual(result.typecheck.review_run.failure_class, "none")
        self.assertEqual(build_log["exitCode"], 7)
        self.assertIn("build bad", build_log["stdout"])
        self.assertEqual(build_artifact["logsArtifactId"], result.build.log_artifact.id)
        self.assertEqual(build_artifact["failureClass"], "build_error")
        self.assertEqual(result.build.review_run.evidence_refs[0].artifact_id, result.build.build_artifact.id)
        self.assertEqual(typecheck_log["status"], "pass")
        self.assertIn("type ok", typecheck_log["stdout"])

    def test_extracts_typescript_diagnostics_into_build_artifact(self):
        project_root = self.root / "generated"
        project_root.mkdir()
        (project_root / "package.json").write_text("{}", encoding="utf-8")
        runner = BuildValidationRunner(
            sandbox_runner=LocalSandboxRunner(SandboxPolicy(allowed_commands=((sys.executable,),))),
            build_command=(sys.executable, "-c", "print('build ok')"),
            typecheck_command=(
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stderr.write(\"src/index.ts(4,7): error TS2322: "
                    "Type 'str' is not assignable to type 'number'.\\n\"); "
                    "sys.exit(2)"
                ),
            ),
        )

        result = runner.run(job_id=self.job.id, store=self.store, project_path=project_root)

        typecheck_artifact = json.loads(Path(result.typecheck.build_artifact.storage_uri).read_text(encoding="utf-8"))
        self.assertEqual(result.typecheck.review_run.status, "fail")
        self.assertEqual(typecheck_artifact["failureClass"], "type_error")
        self.assertEqual(typecheck_artifact["diagnostics"][0]["code"], "TS2322")
        self.assertEqual(typecheck_artifact["diagnostics"][0]["filePath"], "src/index.ts")
        self.assertEqual(typecheck_artifact["diagnostics"][0]["line"], 4)
        self.assertEqual(typecheck_artifact["diagnostics"][0]["column"], 7)

    def test_extracts_multiline_and_related_typescript_diagnostics(self):
        project_root = self.root / "generated"
        project_root.mkdir()
        (project_root / "package.json").write_text("{}", encoding="utf-8")
        diagnostic_output = (
            "src/index.ts(4,7): error TS2322: Type 'str' is not assignable to type 'number'.\n"
            "  4 const value: number = 'str';\n"
            "        ~~~~~\n"
            "src/types.ts(1,14): The expected type comes from this declaration.\n"
        )
        runner = BuildValidationRunner(
            sandbox_runner=LocalSandboxRunner(SandboxPolicy(allowed_commands=((sys.executable,),))),
            build_command=(sys.executable, "-c", "print('build ok')"),
            typecheck_command=(
                sys.executable,
                "-c",
                f"import sys; sys.stderr.write({diagnostic_output!r}); sys.exit(2)",
            ),
        )

        result = runner.run(job_id=self.job.id, store=self.store, project_path=project_root)

        typecheck_artifact = json.loads(Path(result.typecheck.build_artifact.storage_uri).read_text(encoding="utf-8"))
        diagnostic = typecheck_artifact["diagnostics"][0]
        self.assertEqual(diagnostic["tool"], "tsc")
        self.assertEqual(diagnostic["contextLines"], ["  4 const value: number = 'str';", "        ~~~~~"])
        self.assertEqual(diagnostic["relatedInformation"][0]["filePath"], "src/types.ts")
        self.assertEqual(diagnostic["relatedInformation"][0]["line"], 1)
        self.assertEqual(diagnostic["relatedInformation"][0]["column"], 14)
        self.assertIn("expected type", diagnostic["relatedInformation"][0]["message"])

    def test_extracts_build_tool_specific_diagnostics(self):
        project_root = self.root / "generated"
        project_root.mkdir()
        (project_root / "package.json").write_text("{}", encoding="utf-8")
        diagnostic_output = (
            "[vite]: Rollup failed to resolve import './missing' from \"src/App.tsx\".\n"
            "x [ERROR] Could not resolve \"react\"\n"
            "    src/main.tsx:1:19:\n"
            "      1 | import React from \"react\";\n"
            "        |                   ~~~~~~~\n"
        )
        runner = BuildValidationRunner(
            sandbox_runner=LocalSandboxRunner(SandboxPolicy(allowed_commands=((sys.executable,),))),
            build_command=(sys.executable, "-c", "print('build ok')"),
            typecheck_command=(
                sys.executable,
                "-c",
                f"import sys; sys.stderr.write({diagnostic_output!r}); sys.exit(2)",
            ),
        )

        result = runner.run(job_id=self.job.id, store=self.store, project_path=project_root)

        typecheck_artifact = json.loads(Path(result.typecheck.build_artifact.storage_uri).read_text(encoding="utf-8"))
        diagnostics = typecheck_artifact["diagnostics"]
        self.assertEqual([diagnostic["tool"] for diagnostic in diagnostics], ["vite", "esbuild"])
        self.assertEqual(diagnostics[0]["filePath"], "src/App.tsx")
        self.assertIn("Rollup failed", diagnostics[0]["message"])
        self.assertEqual(diagnostics[1]["filePath"], "src/main.tsx")
        self.assertEqual(diagnostics[1]["line"], 1)
        self.assertEqual(diagnostics[1]["column"], 19)
        self.assertIn("Could not resolve", diagnostics[1]["message"])

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

    def test_uses_container_runner_when_configured(self):
        fake_runtime = self.root / "fake_container_runtime.py"
        fake_runtime.write_text(
            (
                "import json, sys\n"
                "print(json.dumps({'argv': sys.argv[1:]}))\n"
            ),
            encoding="utf-8",
        )
        job = self.store.create_job(
            CreateJobRequest(
                project_id="proj",
                owner_id="owner",
                config={
                    "buildValidation": {
                        "sandboxRunner": "container",
                        "containerImage": "ai-jsunpack-container-test-image",
                        "containerRuntimeCommand": [sys.executable, str(fake_runtime)],
                    }
                },
            )
        )
        project_root = self.root / "generated-container"
        (project_root / "scripts").mkdir(parents=True)
        (project_root / "package.json").write_text("{}", encoding="utf-8")
        (project_root / "scripts" / "build.mjs").write_text("console.log('build')", encoding="utf-8")
        (project_root / "scripts" / "typecheck.mjs").write_text("console.log('typecheck')", encoding="utf-8")

        result = BuildValidationRunner().run(job_id=job.id, store=self.store, project_path=project_root)

        build_artifact = json.loads(Path(result.build.build_artifact.storage_uri).read_text(encoding="utf-8"))
        build_log = json.loads(Path(result.build.log_artifact.storage_uri).read_text(encoding="utf-8"))
        runtime_payload = json.loads(build_log["stdout"])
        self.assertEqual(result.build.review_run.status, "pass")
        self.assertEqual(result.typecheck.review_run.status, "pass")
        self.assertEqual(build_artifact["resourcePolicy"]["enforcement"], "container_enforced")
        self.assertEqual(build_artifact["networkPolicy"], "deny")
        self.assertIn("--network", runtime_payload["argv"])
        self.assertIn("ai-jsunpack-container-test-image", runtime_payload["argv"])

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
        build_attempt_zero_artifact = next(
            json.loads(Path(artifact.storage_uri).read_text(encoding="utf-8"))
            for artifact in self.store.list_artifacts(self.job.id, kind="build_artifact")
            if artifact.attempt == 0 and artifact.stage == "building"
        )

        self.assertEqual(result.build.review_run.attempt, 1)
        self.assertEqual(result.build.review_run.status, "fail")
        self.assertEqual(repair_payload["status"], "applied")
        self.assertEqual(repair_payload["attempt"], 1)
        self.assertEqual(repair_payload["actions"][0]["path"], "package.json:scripts.build")
        self.assertEqual(generated_project_artifacts[0].attempt, 1)
        self.assertEqual(repaired_package["scripts"]["build"], "node scripts/build.mjs")
        self.assertEqual(build_attempt_zero_review["repairInstructionIds"], [repair_artifacts[0].id])
        self.assertEqual(build_attempt_zero_artifact["repairInstructionIds"], [repair_artifacts[0].id])


if __name__ == "__main__":
    unittest.main()
