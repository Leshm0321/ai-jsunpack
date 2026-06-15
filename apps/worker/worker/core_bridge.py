from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


NODE_BINARY_ENV = "AI_JSUNPACK_NODE_BINARY"
CORE_CLI_PATH_ENV = "AI_JSUNPACK_CORE_CLI_PATH"


@dataclass(frozen=True)
class CoreAnalysisResult:
    inventory_artifact_payload: dict[str, Any]
    ast_index_artifact_payload: dict[str, Any]


@dataclass(frozen=True)
class CoreReconstructionResult:
    reconstruction_plan_payload: dict[str, Any]
    generated_project_manifest_payload: dict[str, Any]
    generated_project_path: Path


class CoreBridgeError(RuntimeError):
    pass


class CoreBridge:
    def __init__(self, node_binary: str | None = None, cli_path: Path | str | None = None) -> None:
        self.node_binary = node_binary or os.getenv(NODE_BINARY_ENV, "node")
        self.cli_path = Path(cli_path or os.getenv(CORE_CLI_PATH_ENV, self._default_cli_path()))

    def analyze_input_package(self, *, job_id: str, input_path: Path | str) -> CoreAnalysisResult:
        if not self.cli_path.exists():
            raise CoreBridgeError(f"Core CLI is not built: {self.cli_path}")

        command = [
            self.node_binary,
            str(self.cli_path),
            "analyze",
            str(Path(input_path)),
            "--job-id",
            job_id,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise CoreBridgeError(f"Failed to launch Core CLI: {error}") from error

        if result.returncode != 0:
            stderr = result.stderr.strip() or "Core CLI failed without stderr."
            raise CoreBridgeError(stderr)

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CoreBridgeError(f"Core CLI returned invalid JSON: {error}") from error

        inventory_payload = payload.get("inventoryArtifactPayload")
        ast_index_payload = payload.get("astIndexArtifactPayload")
        if not isinstance(inventory_payload, dict) or not isinstance(ast_index_payload, dict):
            raise CoreBridgeError("Core CLI response is missing artifact payloads.")

        return CoreAnalysisResult(
            inventory_artifact_payload=inventory_payload,
            ast_index_artifact_payload=ast_index_payload,
        )

    def reconstruct_input_package(
        self,
        *,
        job_id: str,
        input_path: Path | str,
        output_dir: Path | str,
    ) -> CoreReconstructionResult:
        if not self.cli_path.exists():
            raise CoreBridgeError(f"Core CLI is not built: {self.cli_path}")

        command = [
            self.node_binary,
            str(self.cli_path),
            "reconstruct",
            str(Path(input_path)),
            "--job-id",
            job_id,
            "--output-dir",
            str(Path(output_dir)),
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise CoreBridgeError(f"Failed to launch Core CLI: {error}") from error

        if result.returncode != 0:
            stderr = result.stderr.strip() or "Core CLI reconstruct failed without stderr."
            raise CoreBridgeError(stderr)

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CoreBridgeError(f"Core CLI reconstruct returned invalid JSON: {error}") from error

        reconstruction_plan_payload = payload.get("reconstructionPlanPayload")
        generated_project_manifest_payload = payload.get("generatedProjectManifestPayload")
        generated_project_path = payload.get("generatedProjectPath")
        if (
            not isinstance(reconstruction_plan_payload, dict)
            or not isinstance(generated_project_manifest_payload, dict)
            or not isinstance(generated_project_path, str)
        ):
            raise CoreBridgeError("Core CLI reconstruct response is missing artifact payloads.")

        return CoreReconstructionResult(
            reconstruction_plan_payload=reconstruction_plan_payload,
            generated_project_manifest_payload=generated_project_manifest_payload,
            generated_project_path=Path(generated_project_path),
        )

    def _default_cli_path(self) -> Path:
        return Path(__file__).resolve().parents[3] / "packages" / "core" / "dist" / "cli.js"
