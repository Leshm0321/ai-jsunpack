import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from packages.configuration import (
    ConfigurationError,
    RuntimeSettingsPatch,
    apply_application_config_to_environment,
    load_application_config,
    merge_runtime_settings,
    redact_secrets,
)


class ConfigurationTest(unittest.TestCase):
    def test_json_and_yaml_use_the_same_schema_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "version": 1,
                "api": {"port": 8100, "maxUploadBytes": 2048},
                "worker": {
                    "agent": {
                        "cloud": {
                            "model": "cloud-model",
                            "baseUrl": "https://ai.example/v1",
                            "apiKeySecretRef": "ai/cloud",
                        }
                    }
                },
            }
            json_path = root / "config.json"
            yaml_path = root / "config.yaml"
            json_path.write_text(json.dumps(payload), encoding="utf-8")
            yaml_path.write_text(
                "version: 1\napi:\n  port: 8100\n  maxUploadBytes: 2048\n"
                "worker:\n  agent:\n    cloud:\n      model: cloud-model\n"
                "      baseUrl: https://ai.example/v1\n      apiKeySecretRef: ai/cloud\n",
                encoding="utf-8",
            )

            json_config = load_application_config(json_path, environ={}).config
            yaml_config = load_application_config(yaml_path, environ={}).config
            overridden = load_application_config(
                yaml_path,
                environ={"AI_JSUNPACK_API_PORT": "8200", "AI_JSUNPACK_AGENT_MODEL": "override-model"},
            )

            self.assertEqual(json_config, yaml_config)
            self.assertEqual(overridden.config.api.port, 8200)
            self.assertEqual(overridden.config.worker.agent.cloud.model, "override-model")
            self.assertEqual(overridden.source, "environment")

    def test_unknown_fields_and_invalid_extensions_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unknown_path = root / "config.json"
            unknown_path.write_text('{"unknown": true}', encoding="utf-8")
            text_path = root / "config.txt"
            text_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(ConfigurationError):
                load_application_config(unknown_path, environ={})
            with self.assertRaises(ConfigurationError):
                load_application_config(text_path, environ={})

    def test_runtime_settings_merge_and_secret_redaction(self):
        system = RuntimeSettingsPatch.model_validate(
            {"ai": {"cloud": {"model": "model-a"}}, "agents": {"maxParallel": 3}}
        )
        project = RuntimeSettingsPatch.model_validate({"agents": {"maxParallel": 2}})

        effective = merge_runtime_settings(system, project)
        redacted = redact_secrets({"apiKey": "secret-value", "apiKeySecretRef": "ai/cloud"})

        self.assertEqual(effective.ai.cloud.model, "model-a")
        self.assertEqual(effective.agents.max_parallel, 2)
        self.assertEqual(redacted, {"apiKey": "[redacted]", "apiKeySecretRef": "ai/cloud"})

    def test_module_cli_validates_and_prints_effective_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text("version: 1\napi:\n  port: 8123\n", encoding="utf-8")
            validated = subprocess.run(
                [sys.executable, "-m", "packages.configuration", "validate", str(config_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            printed = subprocess.run(
                [sys.executable, "-m", "packages.configuration", "print-effective", str(config_path)],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertTrue(json.loads(validated.stdout)["valid"])
            self.assertEqual(printed.returncode, 0, printed.stderr)
            self.assertEqual(json.loads(printed.stdout)["api"]["port"], 8123)

    def test_apply_configuration_maps_file_values_without_overwriting_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                "version: 1\napi:\n  host: 0.0.0.0\n  port: 8123\n  artifactRoot: configured-artifacts\n",
                encoding="utf-8",
            )
            environ = {
                "AI_JSUNPACK_CONFIG_FILE": str(config_path),
                "AI_JSUNPACK_API_PORT": "9000",
            }

            loaded = apply_application_config_to_environment("api", environ=environ)

            self.assertEqual(loaded.config.api.port, 9000)
            self.assertEqual(environ["AI_JSUNPACK_API_HOST"], "0.0.0.0")
            self.assertEqual(environ["AI_JSUNPACK_API_PORT"], "9000")
            self.assertEqual(environ["AI_JSUNPACK_ARTIFACT_ROOT"], "configured-artifacts")


if __name__ == "__main__":
    unittest.main()
