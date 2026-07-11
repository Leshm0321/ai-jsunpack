import sys
import tempfile
import unittest
from pathlib import Path

from apps.worker.worker.core_bridge import CoreBridge, CoreBridgeError


class CoreBridgeTest(unittest.TestCase):
    def test_analyze_decodes_utf8_cli_output_without_windows_codepage_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cli_path = self._write_fake_cli(Path(temp_dir))
            result = CoreBridge(node_binary=sys.executable, cli_path=cli_path).analyze_input_package(
                job_id="job_utf8",
                input_path=Path(temp_dir) / "agentApi.js",
            )

        self.assertEqual(result.inventory_artifact_payload["kind"], "input_inventory")
        self.assertEqual(result.ast_index_artifact_payload["kind"], "ast_index")

    def test_reconstruct_decodes_utf8_cli_output_without_windows_codepage_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cli_path = self._write_fake_cli(Path(temp_dir))
            result = CoreBridge(node_binary=sys.executable, cli_path=cli_path).reconstruct_input_package(
                job_id="job_utf8",
                input_path=Path(temp_dir) / "agentApi.js",
                output_dir=Path(temp_dir) / "generated",
            )

        self.assertEqual(result.reconstruction_plan_payload["kind"], "reconstruction_plan")
        self.assertEqual(result.generated_project_manifest_payload["kind"], "generated_project")
        self.assertEqual(result.generated_project_path, Path("generated"))

    def test_cli_failure_with_utf8_stderr_raises_core_bridge_error_not_unicode_decode_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cli_path = self._write_fake_cli(Path(temp_dir))
            bridge = CoreBridge(node_binary=sys.executable, cli_path=cli_path)

            with self.assertRaises(CoreBridgeError) as raised:
                bridge.analyze_input_package(job_id="job_utf8", input_path="fail-input.js")

        self.assertIn("Core failed with UTF-8 diagnostics", str(raised.exception))

    def _write_fake_cli(self, root: Path) -> Path:
        cli_path = root / "fake_core_cli.py"
        cli_path.write_text(
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "sys.stderr.buffer.write(b'Core diagnostic: \\xe2\\x82\\xac emoji \\xf0\\x9f\\x98\\x80\\n')",
                    "command = sys.argv[1]",
                    "input_path = sys.argv[2]",
                    "if 'fail' in input_path:",
                    "    sys.stderr.buffer.write(b'Core failed with UTF-8 diagnostics: \\xe2\\x82\\xac\\n')",
                    "    sys.exit(2)",
                    "if command == 'analyze':",
                    "    payload = {",
                    "        'inventoryArtifactPayload': {'kind': 'input_inventory', 'inventory': {'entries': [], 'scripts': []}},",
                    "        'astIndexArtifactPayload': {'kind': 'ast_index', 'astIndexes': [], 'detectedRuntime': []},",
                    "    }",
                    "elif command == 'reconstruct':",
                    "    payload = {",
                    "        'reconstructionPlanPayload': {'kind': 'reconstruction_plan', 'plan': {}},",
                    "        'generatedProjectManifestPayload': {'kind': 'generated_project', 'manifest': {}},",
                    "        'generatedProjectPath': 'generated',",
                    "    }",
                    "else:",
                    "    raise SystemExit(3)",
                    "print(json.dumps(payload))",
                ]
            ),
            encoding="utf-8",
        )
        return cli_path


if __name__ == "__main__":
    unittest.main()
