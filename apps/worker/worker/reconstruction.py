from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    """运行确定性 Core writer，并持久化 reconstruction artifact。"""

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
                reconstruction_plan_payload = self._with_writer_feedback(
                    job_id=job_id,
                    store=store,
                    payload=core_result.reconstruction_plan_payload,
                    parent_artifact_ids=parents,
                )
                plan_artifact = store.write_artifact(
                    job_id,
                    kind="reconstruction_plan",
                    stage="reconstructing",
                    filename="reconstruction-plan.json",
                    content=self._json_bytes(reconstruction_plan_payload),
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

    def _with_writer_feedback(
        self,
        *,
        job_id: str,
        store,
        payload: dict[str, Any],
        parent_artifact_ids: list[str],
    ) -> dict[str, Any]:
        feedback = self._writer_feedback_inputs(
            job_id=job_id,
            store=store,
            parent_artifact_ids=parent_artifact_ids,
        )
        if not feedback["lowRiskRepairInstructions"] and not feedback["auditOnlyRepairInstructions"]:
            return payload
        updated = dict(payload)
        updated["agentFeedbackInputs"] = feedback
        limitations = list(updated.get("limitations") if isinstance(updated.get("limitations"), list) else [])
        limitations.append(
            "Agent Review/Fix feedback was read as deterministic writer input; only low-risk supported actions are eligible for automatic consumption."
        )
        updated["limitations"] = limitations
        return updated

    def _writer_feedback_inputs(self, *, job_id: str, store, parent_artifact_ids: list[str]) -> dict[str, Any]:
        low_risk: list[dict[str, Any]] = []
        audit_only: list[dict[str, Any]] = []
        for artifact_id in parent_artifact_ids:
            artifact = store.get_artifact(job_id, artifact_id)
            if artifact is None or artifact.kind != "repair_instruction":
                continue
            try:
                payload = json.loads(store.read_artifact(job_id, artifact.id).decode("utf-8"))
            except Exception as error:
                audit_only.append(
                    {
                        "artifactId": artifact.id,
                        "reason": f"repair_instruction could not be read: {error}",
                    }
                )
                continue
            if not isinstance(payload, dict):
                continue
            summary = self._repair_instruction_summary(artifact_id=artifact.id, payload=payload)
            actions = payload.get("actions")
            is_low_risk = (
                payload.get("riskLevel") == "low"
                and payload.get("status") in {"planned", "applied"}
                and isinstance(actions, list)
                and bool(actions)
            )
            if is_low_risk:
                low_risk.append(summary)
            else:
                audit_only.append(summary)
        return {
            "source": "agent_repair_instruction_parent_artifacts",
            "consumptionPolicy": "low_risk_supported_actions_only",
            "lowRiskRepairInstructions": low_risk,
            "auditOnlyRepairInstructions": audit_only,
        }

    def _repair_instruction_summary(self, *, artifact_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        actions = payload.get("actions")
        return {
            "artifactId": artifact_id,
            "targetStage": payload.get("targetStage"),
            "status": payload.get("status"),
            "riskLevel": payload.get("riskLevel"),
            "failureClass": payload.get("failureClass"),
            "actionCount": len(actions) if isinstance(actions, list) else 0,
            "actions": actions if isinstance(actions, list) else [],
            "decision": payload.get("decision"),
        }

    def _json_bytes(self, payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
