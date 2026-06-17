from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from apps.api.app.models import ArtifactRecord

from .core_bridge import CoreBridge, CoreBridgeError


class ReconstructionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconstructionResult:
    plan_artifact: ArtifactRecord
    generated_project_artifact: ArtifactRecord
    project_path: Path | None
    message: str

    @property
    def artifact_ids(self) -> list[str]:
        return [self.plan_artifact.id, self.generated_project_artifact.id]


class ReconstructionRunner:
    """Runs the deterministic Core writer and persists reconstruction artifacts."""

    def __init__(self, core_bridge: CoreBridge | None = None) -> None:
        self.core_bridge = core_bridge or CoreBridge()

    def run(
        self,
        *,
        job_id: str,
        input_path: Path | str,
        store,
        parent_artifact_ids: list[str] | None = None,
    ) -> ReconstructionResult:
        parents = parent_artifact_ids or []
        store.update_status(job_id, "reconstructing")
        try:
            with tempfile.TemporaryDirectory(prefix="ai-jsunpack-generated-") as temp_dir:
                output_dir = Path(temp_dir) / "generated_project"
                core_result = self.core_bridge.reconstruct_input_package(
                    job_id=job_id,
                    input_path=input_path,
                    output_dir=output_dir,
                )
                plan_artifact = store.write_artifact(
                    job_id,
                    kind="reconstruction_plan",
                    stage="reconstructing",
                    filename="reconstruction-plan.json",
                    content=self._json_bytes(core_result.reconstruction_plan_payload),
                    content_type="application/json",
                    producer="worker.reconstruction",
                    parent_artifact_ids=parents,
                )
                generated_project_artifact = store.register_artifact_path(
                    job_id,
                    kind="generated_project",
                    stage="reconstructing",
                    filename="generated-project",
                    source_path=core_result.generated_project_path,
                    content_type="application/vnd.ai-jsunpack.generated-project+directory",
                    producer="worker.reconstruction",
                    parent_artifact_ids=[*parents, plan_artifact.id],
                )
        except CoreBridgeError as error:
            raise ReconstructionError(str(error)) from error

        return ReconstructionResult(
            plan_artifact=plan_artifact,
            generated_project_artifact=generated_project_artifact,
            project_path=store.artifact_local_path(generated_project_artifact),
            message="Deterministic writer produced a generated_project directory for sandbox validation.",
        )

    def _json_bytes(self, payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
