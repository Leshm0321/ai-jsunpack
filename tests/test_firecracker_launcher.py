import base64
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


LAUNCHER_PATH = Path(__file__).resolve().parents[1] / "deploy" / "firecracker" / "launcher.py"
SPEC = importlib.util.spec_from_file_location("firecracker_launcher", LAUNCHER_PATH)
firecracker_launcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = firecracker_launcher
SPEC.loader.exec_module(firecracker_launcher)


class FirecrackerLauncherTemplateTest(unittest.TestCase):
    def _request(self, workspace: Path, **overrides):
        payload = {
            "version": 1,
            "runnerKind": "firecracker",
            "workspace": str(workspace),
            "workingDirectory": ".",
            "command": [sys.executable, "-c", "print('guest')"],
            "stdinBase64": None,
            "environment": {"PATH": "/usr/bin"},
            "timeoutMs": 1000,
            "outputLimitBytes": 4096,
            "networkPolicy": "deny",
            "resourcePolicy": {
                "runnerKind": "firecracker",
                "processLimit": 8,
                "cpuTimeLimitMs": 1000,
                "memoryLimitBytes": 64 * 1024 * 1024,
            },
        }
        payload.update(overrides)
        return payload

    def test_prepare_request_rejects_workspace_escape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)

            with self.assertRaises(firecracker_launcher.LauncherError) as context:
                firecracker_launcher.prepare_request(self._request(workspace, workingDirectory=".."))

        self.assertEqual(context.exception.failure_class, "sandbox_denied")
        self.assertIn("workingDirectory", str(context.exception))

    def test_prepare_request_decodes_stdin_and_keeps_relative_workdir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "project").mkdir()

            request = firecracker_launcher.prepare_request(
                self._request(
                    workspace,
                    workingDirectory="project",
                    stdinBase64=base64.b64encode(b"input").decode("ascii"),
                )
            )

        self.assertEqual(request.working_directory_relative, "project")
        self.assertEqual(request.stdin_bytes, b"input")
        self.assertEqual(request.network_policy, "deny")

    def test_dry_run_returns_protocol_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kernel = root / "vmlinux"
            rootfs = root / "rootfs.ext4"
            jailer = root / "jailer"
            firecracker = root / "firecracker"
            socket_dir = root / "sockets"
            for path in (kernel, rootfs, jailer, firecracker):
                path.write_text("placeholder", encoding="utf-8")
            request = firecracker_launcher.prepare_request(self._request(root))
            args = firecracker_launcher.build_arg_parser().parse_args(
                [
                    "--kernel",
                    str(kernel),
                    "--rootfs",
                    str(rootfs),
                    "--jailer",
                    str(jailer),
                    "--firecracker",
                    str(firecracker),
                    "--socket-dir",
                    str(socket_dir),
                    "--dry-run",
                ]
            )

            response = firecracker_launcher.run_launcher(args, request, started_at=0)

        payload = json.loads(response["stdout"])
        self.assertEqual(response["failureClass"], "none")
        self.assertEqual(payload["runnerKind"], "firecracker")
        self.assertEqual(payload["networkPolicy"], "deny")
        self.assertEqual(payload["resourcePolicy"]["runnerKind"], "firecracker")

    def test_missing_runtime_input_is_sandbox_denied(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = firecracker_launcher.build_arg_parser().parse_args(
                [
                    "--kernel",
                    str(root / "missing-vmlinux"),
                    "--rootfs",
                    str(root / "rootfs.ext4"),
                    "--jailer",
                    str(root / "jailer"),
                    "--firecracker",
                    str(root / "firecracker"),
                ]
            )

            with self.assertRaises(firecracker_launcher.LauncherError) as context:
                firecracker_launcher.ensure_runtime_inputs(args)

        self.assertEqual(context.exception.failure_class, "sandbox_denied")
        self.assertIn("kernel", str(context.exception))

    def test_wrapper_json_response_is_normalized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            kernel = root / "vmlinux"
            rootfs = root / "rootfs.ext4"
            jailer = root / "jailer"
            firecracker = root / "firecracker"
            socket_dir = root / "sockets"
            wrapper = root / "wrapper.py"
            for path in (kernel, rootfs, jailer, firecracker):
                path.write_text("placeholder", encoding="utf-8")
            wrapper.write_text(
                (
                    "import json, sys\n"
                    "control = json.loads(sys.stdin.read())\n"
                    "print(json.dumps({"
                    "'stdout': json.dumps({'command': control['command'], 'workingDirectory': control['workingDirectory']}),"
                    "'stderr': '',"
                    "'exitCode': 0,"
                    "'timedOut': False,"
                    "'outputTruncated': False,"
                    "'failureClass': 'none'"
                    "}))\n"
                ),
                encoding="utf-8",
            )
            (root / "project").mkdir()
            request = firecracker_launcher.prepare_request(self._request(root, workingDirectory="project"))
            args = firecracker_launcher.build_arg_parser().parse_args(
                [
                    "--kernel",
                    str(kernel),
                    "--rootfs",
                    str(rootfs),
                    "--jailer",
                    str(jailer),
                    "--firecracker",
                    str(firecracker),
                    "--socket-dir",
                    str(socket_dir),
                    "--wrapper-command",
                    sys.executable,
                    str(wrapper),
                ]
            )

            response = firecracker_launcher.run_launcher(args, request, started_at=0)

        payload = json.loads(response["stdout"])
        self.assertEqual(response["failureClass"], "none")
        self.assertEqual(response["exitCode"], 0)
        self.assertEqual(payload["workingDirectory"], "project")


if __name__ == "__main__":
    unittest.main()
