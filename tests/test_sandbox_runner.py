import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from packages.sandbox import (
    ContainerSandboxRunner,
    FirecrackerSandboxRunner,
    GVisorSandboxRunner,
    LocalSandboxRunner,
    ProfileOnlySandboxRunner,
    SandboxCommand,
    SandboxPolicy,
    SandboxResourcePolicy,
    sandbox_resource_policy_profile,
)


class LocalSandboxRunnerTest(unittest.TestCase):
    def test_denies_commands_not_in_allowlist(self):
        runner = LocalSandboxRunner(SandboxPolicy(allowed_commands=("node",)))

        result = runner.run(SandboxCommand(executable=sys.executable, args=("-c", "print('nope')")))

        self.assertEqual(result.failure_class, "sandbox_denied")
        self.assertIsNone(result.exit_code)
        self.assertIn("not allowed", result.denied_reason or "")

    def test_production_profile_denies_allowed_local_command(self):
        runner = LocalSandboxRunner(
            SandboxPolicy(allowed_commands=((sys.executable,),), deployment_profile="production")
        )

        result = runner.run(SandboxCommand(executable=sys.executable, args=("-c", "print('nope')")))

        self.assertEqual(result.failure_class, "sandbox_denied")
        self.assertIn("production deployment profile", result.denied_reason or "")

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
        self.assertEqual(result.resource_policy.runner_kind, "local")
        self.assertIsNone(result.resource_policy.runtime_name)
        self.assertTrue(result.resource_policy.host_platform)
        self.assertEqual(result.resource_policy.process_limit, 8)
        self.assertIn("does not enforce", result.resource_policy.limitations[0])
        self.assertIn("production multi-tenant isolation", result.resource_policy.limitations[1])
        capabilities = {capability.name: capability for capability in result.resource_policy.capabilities}
        self.assertEqual(capabilities["network"].status, "best_effort")
        self.assertEqual(capabilities["cpu"].status, "best_effort")

    def test_container_runner_reports_missing_runtime_as_denied(self):
        runner = ContainerSandboxRunner(
            SandboxPolicy(allowed_commands=("node",)),
            runtime_command=(),
        )

        result = runner.run(SandboxCommand(executable="node", args=("-e", "console.log('ok')")))

        self.assertEqual(result.failure_class, "sandbox_denied")
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.resource_policy.enforcement, "container_enforced")
        self.assertEqual(result.resource_policy.runner_kind, "container")
        self.assertIsNone(result.resource_policy.runtime_name)
        capabilities = {capability.name: capability for capability in result.resource_policy.capabilities}
        self.assertEqual(capabilities["network"].status, "unsupported")
        self.assertEqual(capabilities["memory"].status, "unsupported")
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
        self.assertEqual(result.resource_policy.runner_kind, "container")
        self.assertEqual(result.resource_policy.runtime_name, "custom")
        self.assertTrue(result.resource_policy.host_platform)
        capabilities = {capability.name: capability for capability in result.resource_policy.capabilities}
        self.assertEqual(capabilities["network"].status, "enforced")
        self.assertEqual(capabilities["process"].status, "enforced")
        self.assertEqual(capabilities["memory"].status, "enforced")
        self.assertEqual(capabilities["cpu"].status, "best_effort")
        self.assertIn("gVisor", result.resource_policy.limitations[-1])
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

    def test_container_runner_maps_named_volume_subpath(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fake_runtime = root / "fake_container_runtime.py"
            fake_runtime.write_text(
                "import json, sys\nprint(json.dumps({'argv': sys.argv[1:]}))\n",
                encoding="utf-8",
            )
            workspace_root = root / "workspaces"
            runner = ContainerSandboxRunner(
                SandboxPolicy(allowed_commands=("node",)),
                image="ai-jsunpack-test-image",
                runtime_command=(sys.executable, str(fake_runtime)),
                workspace_root=workspace_root,
                volume_name="ai-jsunpack-sandbox-workspaces",
            )
            with runner.attempt_workspace() as workspace:
                (workspace / "project").mkdir()
                result = runner.run_in_workspace(
                    SandboxCommand(executable="node", working_directory="project"),
                    workspace,
                )

        argv = json.loads(result.stdout)["argv"]
        mount = argv[argv.index("--mount") + 1]
        self.assertIn("type=volume", mount)
        self.assertIn("src=ai-jsunpack-sandbox-workspaces", mount)
        self.assertIn("dst=/workspace", mount)
        self.assertIn("volume-subpath=ai-jsunpack-sandbox-", mount)
        self.assertNotIn("-v", argv)
        self.assertEqual(argv[argv.index("-w") + 1], "/workspace/project")
        filesystem = {item.name: item for item in result.resource_policy.capabilities}["filesystem"]
        self.assertIn("Docker volume 'ai-jsunpack-sandbox-workspaces'", filesystem.detail)

    def test_container_runner_denies_workspace_outside_named_volume_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = ContainerSandboxRunner(
                SandboxPolicy(allowed_commands=("node",)),
                runtime_command=(sys.executable, "unused"),
                workspace_root=root / "configured-root",
                volume_name="ai-jsunpack-sandbox-workspaces",
            )
            outside = root / "outside"
            outside.mkdir()
            result = runner.run_in_workspace(SandboxCommand(executable="node"), outside)

        self.assertEqual(result.failure_class, "sandbox_denied")
        self.assertIn("outside the configured named-volume root", result.denied_reason or "")

    def test_gvisor_profile_records_unsupported_capabilities_without_adapter(self):
        policy = sandbox_resource_policy_profile(
            SandboxResourcePolicy(process_limit=8),
            runner_kind="gvisor",
            network_policy="deny",
        )

        self.assertEqual(policy.enforcement, "runtime_isolated")
        self.assertEqual(policy.runner_kind, "gvisor")
        self.assertEqual(policy.runtime_name, "runsc")
        capabilities = {capability.name: capability for capability in policy.capabilities}
        self.assertEqual(capabilities["network"].status, "unsupported")
        self.assertIn("audit profile only", capabilities["network"].detail)
        self.assertIn("gVisor deployments", policy.limitations[0])

    def test_gvisor_runner_maps_policy_to_runsc_runtime_arguments(self):
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
                runner = GVisorSandboxRunner(
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
                    runtime_version="2026.06-test",
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
        capabilities = {capability.name: capability for capability in result.resource_policy.capabilities}
        self.assertEqual(result.failure_class, "none")
        self.assertFalse(payload["hasSecret"])
        self.assertEqual(result.resource_policy.enforcement, "runtime_isolated")
        self.assertEqual(result.resource_policy.runner_kind, "gvisor")
        self.assertEqual(result.resource_policy.runtime_name, "runsc")
        self.assertEqual(result.resource_policy.runtime_version, "2026.06-test")
        self.assertEqual(capabilities["network"].status, "enforced")
        self.assertEqual(capabilities["process"].status, "enforced")
        self.assertEqual(argv[argv.index("--runtime") + 1], "runsc")
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("--pids-limit") + 1], "8")
        self.assertEqual(argv[argv.index("--memory") + 1], str(64 * 1024 * 1024))
        self.assertIn("ai-jsunpack-test-image", argv)
        self.assertNotIn(f"{secret_name}=secret", argv)

    def test_gvisor_runner_requires_configured_container_runtime(self):
        runner = GVisorSandboxRunner(
            SandboxPolicy(allowed_commands=((sys.executable,),)),
            runtime_command=(),
        )

        result = runner.run(SandboxCommand(executable=sys.executable, args=("-c", "print('no runsc')")))

        self.assertEqual(result.failure_class, "sandbox_denied")
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.resource_policy.enforcement, "runtime_isolated")
        self.assertEqual(result.resource_policy.runner_kind, "gvisor")
        self.assertIn("gVisor container runtime command is not configured", result.denied_reason or "")

    def test_remote_browser_runner_profile_records_remote_isolation_boundary(self):
        policy = sandbox_resource_policy_profile(
            runner_kind="remote_browser_runner",
            network_policy="deny",
            adapter_available=True,
        )

        self.assertEqual(policy.enforcement, "remote_isolated")
        self.assertEqual(policy.runner_kind, "remote_browser_runner")
        self.assertEqual(policy.runtime_name, "playwright-remote")
        capabilities = {capability.name: capability for capability in policy.capabilities}
        self.assertEqual(capabilities["process"].status, "enforced")
        self.assertIn("Browser Runner service", capabilities["process"].detail)
        self.assertIn("browser/runtime validation", policy.limitations[0])

    def test_profile_only_runner_denies_execution_without_fallback(self):
        runner = ProfileOnlySandboxRunner(
            SandboxPolicy(allowed_commands=((sys.executable,),)),
            runner_kind="remote_browser_runner",
        )

        result = runner.run(SandboxCommand(executable=sys.executable, args=("-c", "print('no fallback')")))

        self.assertEqual(result.failure_class, "sandbox_denied")
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.resource_policy.enforcement, "remote_isolated")
        self.assertEqual(result.resource_policy.runner_kind, "remote_browser_runner")
        self.assertIn("does not include a Remote Browser Runner execution adapter", result.denied_reason or "")

    def test_firecracker_runner_requires_configured_launcher(self):
        runner = FirecrackerSandboxRunner(SandboxPolicy(allowed_commands=((sys.executable,),)))

        result = runner.run(SandboxCommand(executable=sys.executable, args=("-c", "print('no launcher')")))

        self.assertEqual(result.failure_class, "sandbox_denied")
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.resource_policy.enforcement, "runtime_isolated")
        self.assertEqual(result.resource_policy.runner_kind, "firecracker")
        self.assertIn("runner command is not configured", result.denied_reason or "")

    def test_firecracker_runner_delegates_to_launcher_protocol(self):
        secret_name = "AI_JSUNPACK_TEST_SECRET"
        os.environ[secret_name] = "secret"
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                fake_launcher = Path(temp_dir) / "fake_firecracker_launcher.py"
                request_path = Path(temp_dir) / "launcher-request.json"
                fake_launcher.write_text(
                    (
                        "import json, os, pathlib, sys\n"
                        "request = json.loads(sys.stdin.read())\n"
                        f"pathlib.Path({str(request_path)!r}).write_text(json.dumps(request, sort_keys=True), encoding='utf-8')\n"
                        "print(json.dumps({\n"
                        "  'stdout': json.dumps({'argv': request['command'], 'hasSecret': 'AI_JSUNPACK_TEST_SECRET' in os.environ}),\n"
                        "  'stderr': '',\n"
                        "  'exitCode': 0,\n"
                        "  'timedOut': False,\n"
                        "  'outputTruncated': False,\n"
                        "  'failureClass': 'none'\n"
                        "}))\n"
                    ),
                    encoding="utf-8",
                )
                runner = FirecrackerSandboxRunner(
                    SandboxPolicy(
                        allowed_commands=((sys.executable,),),
                        resource_policy=SandboxResourcePolicy(
                            process_limit=8,
                            cpu_time_limit_ms=1200,
                            memory_limit_bytes=64 * 1024 * 1024,
                        ),
                    ),
                    runner_command=(sys.executable, str(fake_launcher)),
                    runtime_version="2026.06-test",
                )
                with runner.attempt_workspace() as workspace:
                    (workspace / "project").mkdir()
                    result = runner.run_in_workspace(
                        SandboxCommand(
                            executable=sys.executable,
                            args=("-c", "print('guest')"),
                            working_directory="project",
                        ),
                        workspace,
                    )
                request_payload = json.loads(request_path.read_text(encoding="utf-8"))
        finally:
            os.environ.pop(secret_name, None)

        payload = json.loads(result.stdout)
        self.assertEqual(result.failure_class, "none")
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(payload["hasSecret"])
        self.assertEqual(payload["argv"][0], sys.executable)
        self.assertEqual(result.resource_policy.enforcement, "runtime_isolated")
        self.assertEqual(result.resource_policy.runner_kind, "firecracker")
        self.assertEqual(result.resource_policy.runtime_name, "firecracker")
        self.assertEqual(result.resource_policy.runtime_version, "2026.06-test")
        capabilities = {capability.name: capability for capability in result.resource_policy.capabilities}
        self.assertEqual(capabilities["network"].status, "enforced")
        self.assertEqual(capabilities["process"].status, "enforced")
        self.assertEqual(request_payload["runnerKind"], "firecracker")
        self.assertEqual(request_payload["workingDirectory"], "project")
        self.assertEqual(request_payload["networkPolicy"], "deny")
        self.assertEqual(request_payload["resourcePolicy"]["runnerKind"], "firecracker")
        self.assertEqual(request_payload["resourcePolicy"]["hostPlatform"], result.resource_policy.host_platform)

    def test_firecracker_runner_rejects_invalid_launcher_response(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_launcher = Path(temp_dir) / "invalid_firecracker_launcher.py"
            fake_launcher.write_text("print('not json')\n", encoding="utf-8")
            runner = FirecrackerSandboxRunner(
                SandboxPolicy(allowed_commands=((sys.executable,),)),
                runner_command=(sys.executable, str(fake_launcher)),
            )

            result = runner.run(SandboxCommand(executable=sys.executable, args=("-c", "print('guest')")))

        self.assertEqual(result.failure_class, "sandbox_denied")
        self.assertEqual(result.denied_reason, "Firecracker runner did not return a valid JSON result.")
        self.assertIn("not json", result.stdout)


if __name__ == "__main__":
    unittest.main()
