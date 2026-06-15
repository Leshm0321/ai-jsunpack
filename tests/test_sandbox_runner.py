import json
import os
import sys
import unittest
from pathlib import Path

from packages.sandbox import LocalSandboxRunner, SandboxCommand, SandboxPolicy, SandboxResourcePolicy


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


if __name__ == "__main__":
    unittest.main()
