import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from packages.sandbox import (
    ContainerSandboxRunner,
    LocalSandboxRunner,
    SandboxCommand,
    SandboxPolicy,
    SandboxResourcePolicy,
)


class LocalSandboxRunnerTest(unittest.TestCase):
    def test_denies_commands_not_in_allowlist(self):
        runner = LocalSandboxRunner(SandboxPolicy(allowed_commands=("node",)))

        result = runner.run(SandboxCommand(executable=sys.executable, args=("-c", "print('nope')")))

        self.assertEqual(result.failure_class, "sandbox_denied")
        self.assertIsNone(result.exit_code)
        self.assertIn("not allowed", result.denied_reason or "")

    def test_runs_allowed_command_in_clean_temporary_workspace(self):
        secret_name = "AI_JSUNPACK_TEST_SECRET"
        os.environ[secret_name] = "secret"
        try:
            runner = LocalSandboxRunner(SandboxPolicy(allowed_commands=((sys.executable,),)))
            result = runner.run(
                SandboxCommand(
                    executable=sys.executable,
                    args=(
                        "-c",
                        (
                            "import json, os, pathlib; "
                            "print(json.dumps({"
                            "'cwd': str(pathlib.Path.cwd()), "
                            "'hasSecret': 'AI_JSUNPACK_TEST_SECRET' in os.environ"
                            "}))"
                        ),
                    ),
                )
            )
        finally:
            os.environ.pop(secret_name, None)

        payload = json.loads(result.stdout)
        self.assertEqual(result.failure_class, "none")
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(payload["hasSecret"])
        self.assertIn("ai-jsunpack-sandbox-", payload["cwd"])
        self.assertFalse(Path(result.working_directory).exists())

    def test_times_out_long_running_command(self):
        runner = LocalSandboxRunner(SandboxPolicy(allowed_commands=((sys.executable,),), timeout_ms=100))

        result = runner.run(
            SandboxCommand(
                executable=sys.executable,
                args=("-c", "import time; time.sleep(2)"),
                failure_class="build_error",
            )
        )

        self.assertTrue(result.timed_out)
        self.assertEqual(result.failure_class, "timeout")

    def test_missing_allowed_executable_returns_failure_result(self):
        missing = "ai-jsunpack-missing-command"
        runner = LocalSandboxRunner(SandboxPolicy(allowed_commands=(missing,)))

        result = runner.run(SandboxCommand(executable=missing, failure_class="dependency_missing"))

        self.assertIsNone(result.exit_code)
        self.assertEqual(result.failure_class, "dependency_missing")
        self.assertIn("ai-jsunpack-missing-command", result.command)

    def test_truncates_output_and_classifies_resource_limit(self):
        runner = LocalSandboxRunner(
            SandboxPolicy(
                allowed_commands=((sys.executable,),),
                output_limit_bytes=12,
            )
        )

        result = runner.run(
            SandboxCommand(
                executable=sys.executable,
                args=("-c", "print('x' * 100)"),
                failure_class="build_error",
            )
        )

        self.assertTrue(result.output_truncated)
        self.assertEqual(result.failure_class, "resource_limit")
        self.assertLessEqual(len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")), 12)

    def test_denies_absolute_working_directory(self):
        runner = LocalSandboxRunner(SandboxPolicy(allowed_commands=((sys.executable,),)))

        result = runner.run(
            SandboxCommand(
                executable=sys.executable,
                args=("-c", "print('nope')"),
                working_directory=str(Path.cwd()),
            )
        )

        self.assertEqual(result.failure_class, "sandbox_denied")
        self.assertIn("relative", result.stderr)

    def test_records_resource_policy_for_audit(self):
        runner = LocalSandboxRunner(
            SandboxPolicy(
                allowed_commands=((sys.executable,),),
                resource_policy=SandboxResourcePolicy(
                    process_limit=8,
                    cpu_time_limit_ms=1000,
                    memory_limit_bytes=64 * 1024 * 1024,
                ),
            )
        )

        result = runner.run(SandboxCommand(executable=sys.executable, args=("-c", "print('policy')")))

        self.assertEqual(result.failure_class, "none")
        self.assertEqual(result.resource_policy.enforcement, "local_best_effort")
        self.assertEqual(result.resource_policy.process_limit, 8)
        self.assertIn("does not enforce", result.resource_policy.limitations[0])

    def test_container_runner_reports_missing_runtime_as_denied(self):
        runner = ContainerSandboxRunner(
            SandboxPolicy(allowed_commands=("node",)),
            runtime_command=(),
        )

        result = runner.run(SandboxCommand(executable="node", args=("-e", "console.log('ok')")))

        self.assertEqual(result.failure_class, "sandbox_denied")
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.resource_policy.enforcement, "container_enforced")
        self.assertIn("Container runtime is not available", result.denied_reason or "")

    def test_container_runner_maps_policy_to_runtime_arguments(self):
        secret_name = "AI_JSUNPACK_TEST_SECRET"
        os.environ[secret_name] = "secret"
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                fake_runtime = Path(temp_dir) / "fake_container_runtime.py"
                fake_runtime.write_text(
                    (
                        "import json, os, sys\n"
                        "print(json.dumps({"
                        "'argv': sys.argv[1:], "
                        "'hasSecret': 'AI_JSUNPACK_TEST_SECRET' in os.environ"
                        "}))\n"
                    ),
                    encoding="utf-8",
                )
                runner = ContainerSandboxRunner(
                    SandboxPolicy(
                        allowed_commands=("node",),
                        resource_policy=SandboxResourcePolicy(
                            process_limit=8,
                            cpu_time_limit_ms=1200,
                            memory_limit_bytes=64 * 1024 * 1024,
                        ),
                    ),
                    image="ai-jsunpack-test-image",
                    runtime_command=(sys.executable, str(fake_runtime)),
                )
                with runner.attempt_workspace() as workspace:
                    (workspace / "project").mkdir()
                    result = runner.run_in_workspace(
                        SandboxCommand(
                            executable="node",
                            args=("-e", "console.log('ok')"),
                            working_directory="project",
                        ),
                        workspace,
                    )
        finally:
            os.environ.pop(secret_name, None)

        payload = json.loads(result.stdout)
        argv = payload["argv"]
        self.assertEqual(result.failure_class, "none")
        self.assertFalse(payload["hasSecret"])
        self.assertEqual(result.resource_policy.enforcement, "container_enforced")
        self.assertIn("run", argv)
        self.assertIn("--rm", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("--pids-limit") + 1], "8")
        self.assertEqual(argv[argv.index("--memory") + 1], str(64 * 1024 * 1024))
        self.assertEqual(argv[argv.index("--ulimit") + 1], "cpu=2")
        self.assertTrue(argv[argv.index("-v") + 1].endswith(":/workspace"))
        self.assertEqual(argv[argv.index("-w") + 1], "/workspace/project")
        self.assertIn("ai-jsunpack-test-image", argv)
        self.assertNotIn(f"{secret_name}=secret", argv)


if __name__ == "__main__":
    unittest.main()
