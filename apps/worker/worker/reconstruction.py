from __future__ import annotations

import json
import inspect
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
                feedback = self._writer_feedback_inputs(
                    job_id=job_id,
                    store=store,
                    parent_artifact_ids=parents,
                )
                core_result = self._reconstruct_with_feedback(
                    job_id=job_id,
                    input_path=input_path,
                    output_dir=output_dir,
                    feedback=feedback,
                )
                reconstruction_plan_payload = self._with_writer_feedback(
                    payload=core_result.reconstruction_plan_payload,
                    feedback=feedback,
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
        payload: dict[str, Any],
        feedback: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if feedback is None:
            return payload
        updated = dict(payload)
        updated["agentFeedbackInputs"] = self._feedback_audit_summary(feedback)
        limitations = list(updated.get("limitations") if isinstance(updated.get("limitations"), list) else [])
        limitations.append(
            "Agent Review/Fix feedback was read as deterministic writer input; only low-risk supported actions are eligible for automatic consumption."
        )
        updated["limitations"] = limitations
        return updated

    def _reconstruct_with_feedback(
        self,
        *,
        job_id: str,
        input_path: Path | str,
        output_dir: Path,
        feedback: dict[str, Any] | None,
    ):
        try:
            parameters = inspect.signature(self.core_bridge.reconstruct_input_package).parameters
        except (TypeError, ValueError) as error:
            if feedback is not None:
                raise ReconstructionError(
                    "Core bridge feedback capability could not be verified; refusing to omit agent_feedback."
                ) from error
            parameters = {}
        kwargs: dict[str, Any] = {
            "job_id": job_id,
            "input_path": input_path,
            "output_dir": output_dir,
        }
        if feedback is not None:
            accepts_feedback = "agent_feedback" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            if not accepts_feedback:
                raise ReconstructionError(
                    "Core bridge does not accept agent_feedback; refusing to record unapplied writer feedback."
                )
            kwargs["agent_feedback"] = feedback
        return self.core_bridge.reconstruct_input_package(**kwargs)

    def _writer_feedback_inputs(
        self,
        *,
        job_id: str,
        store,
        parent_artifact_ids: list[str],
    ) -> dict[str, Any] | None:
        approved_repair_ids: set[str] = set()
        review_artifact_ids: list[str] = []
        rejected: list[dict[str, Any]] = []
        relevant_artifacts = False

        for artifact_id in parent_artifact_ids:
            artifact = store.get_artifact(job_id, artifact_id)
            if artifact is None or artifact.kind != "review_run":
                continue
            relevant_artifacts = True
            try:
                payload = json.loads(store.read_artifact(job_id, artifact.id).decode("utf-8"))
            except Exception as error:
                rejected.append({"sourceArtifactId": artifact.id, "reason": f"review_run could not be read: {error}"})
                continue
            if not isinstance(payload, dict) or payload.get("reviewType") != "agent_review":
                continue
            if payload.get("status") != "pass" or payload.get("failureClass", "none") != "none":
                rejected.append(
                    {
                        "sourceArtifactId": artifact.id,
                        "reason": "agent ReviewRun must pass with failureClass=none before repairs can be approved.",
                    }
                )
                continue
            repair_ids = payload.get("repairInstructionIds")
            if not isinstance(repair_ids, list) or not all(isinstance(item, str) and item for item in repair_ids):
                rejected.append(
                    {"sourceArtifactId": artifact.id, "reason": "agent ReviewRun has invalid repairInstructionIds."}
                )
                continue
            review_artifact_ids.append(artifact.id)
            approved_repair_ids.update(repair_ids)

        candidates: list[dict[str, Any]] = []
        for artifact_id in parent_artifact_ids:
            artifact = store.get_artifact(job_id, artifact_id)
            if artifact is None or artifact.kind != "repair_instruction":
                continue
            relevant_artifacts = True
            try:
                payload = json.loads(store.read_artifact(job_id, artifact.id).decode("utf-8"))
            except Exception as error:
                rejected.append({"sourceArtifactId": artifact.id, "reason": f"repair_instruction could not be read: {error}"})
                continue
            if not isinstance(payload, dict):
                rejected.append({"sourceArtifactId": artifact.id, "reason": "repair_instruction payload is not an object."})
                continue
            instruction_id = payload.get("id") if isinstance(payload.get("id"), str) else None
            actions = payload.get("actions")
            eligibility_reason = self._instruction_rejection_reason(
                artifact_id=artifact.id,
                instruction_id=instruction_id,
                payload=payload,
                approved_repair_ids=approved_repair_ids,
            )
            if not isinstance(actions, list) or not actions:
                rejected.append(
                    {
                        "sourceArtifactId": artifact.id,
                        **({"repairInstructionId": instruction_id} if instruction_id else {}),
                        "reason": eligibility_reason or "repair_instruction has no actions.",
                    }
                )
                continue
            for action_payload in actions:
                normalized, malformed_reason = self._normalize_action(
                    artifact_id=artifact.id,
                    instruction_id=instruction_id,
                    payload=action_payload,
                )
                if normalized is None:
                    rejected.append(
                        {
                            "sourceArtifactId": artifact.id,
                            **({"repairInstructionId": instruction_id} if instruction_id else {}),
                            "reason": malformed_reason,
                        }
                    )
                    continue
                if eligibility_reason:
                    rejected.append(self._rejected_action(normalized, eligibility_reason))
                    continue
                candidates.append(normalized)

        approved, conflict_rejections = self._reject_conflicting_actions(candidates)
        rejected.extend(conflict_rejections)
        if not relevant_artifacts:
            return None
        return {
            "kind": "agent_feedback",
            "protocolVersion": 1,
            "sourceReviewArtifactIds": list(dict.fromkeys(review_artifact_ids)),
            "approvedActions": approved,
            "rejectedActions": rejected,
        }

    def _instruction_rejection_reason(
        self,
        *,
        artifact_id: str,
        instruction_id: str | None,
        payload: dict[str, Any],
        approved_repair_ids: set[str],
    ) -> str | None:
        if artifact_id not in approved_repair_ids and (instruction_id is None or instruction_id not in approved_repair_ids):
            return "repair_instruction was not approved by an agent ReviewRun."
        if payload.get("riskLevel") != "low":
            return "repair_instruction riskLevel must be low."
        if payload.get("status") != "planned":
            return "repair_instruction status must be planned."
        return None

    def _normalize_action(
        self,
        *,
        artifact_id: str,
        instruction_id: str | None,
        payload: Any,
    ) -> tuple[dict[str, str] | None, str]:
        if not isinstance(payload, dict):
            return None, "repair action is not an object."
        required = ("action", "path", "value", "reason")
        if any(not isinstance(payload.get(field), str) or not payload[field].strip() for field in required):
            return None, "repair action requires non-empty action, path, value, and reason strings."
        if payload["action"] not in {
            "add_package_script",
            "replace_package_script",
            "mirror_original_static_entry",
        }:
            return None, f"Unsupported repair action: {payload['action']}."
        normalized = {
            "sourceArtifactId": artifact_id,
            "action": payload["action"],
            "path": payload["path"],
            "value": payload["value"],
            "reason": payload["reason"],
        }
        if instruction_id:
            normalized["repairInstructionId"] = instruction_id
        return normalized, ""

    def _reject_conflicting_actions(
        self, candidates: list[dict[str, str]]
    ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        by_target: dict[str, list[dict[str, str]]] = {}
        for action in candidates:
            target = "mirror:projectRoot" if action["action"] == "mirror_original_static_entry" else action["path"]
            by_target.setdefault(target, []).append(action)
        approved: list[dict[str, str]] = []
        rejected: list[dict[str, Any]] = []
        for actions in by_target.values():
            signatures = {(action["action"], action["value"]) for action in actions}
            if len(signatures) > 1:
                rejected.extend(
                    self._rejected_action(action, "Conflicting Review-approved actions target the same field.")
                    for action in actions
                )
                continue
            approved.append(actions[0])
            rejected.extend(
                self._rejected_action(action, "Duplicate Review-approved action was removed.")
                for action in actions[1:]
            )
        return approved, rejected

    def _rejected_action(self, action: dict[str, str], reason: str) -> dict[str, Any]:
        return {
            "sourceArtifactId": action["sourceArtifactId"],
            **({"repairInstructionId": action["repairInstructionId"]} if action.get("repairInstructionId") else {}),
            "action": action["action"],
            "path": action["path"],
            "reason": reason,
        }

    def _feedback_audit_summary(self, feedback: dict[str, Any]) -> dict[str, Any]:
        approved_by_artifact: dict[str, list[dict[str, Any]]] = {}
        for action in feedback["approvedActions"]:
            approved_by_artifact.setdefault(action["sourceArtifactId"], []).append(action)
        rejected_by_artifact: dict[str, list[dict[str, Any]]] = {}
        for rejection in feedback["rejectedActions"]:
            rejected_by_artifact.setdefault(rejection.get("sourceArtifactId", "unknown"), []).append(rejection)
        return {
            "source": "agent_review_approved_repair_instruction_parent_artifacts",
            "consumptionPolicy": "review_approved_planned_low_risk_supported_actions_only",
            "approvalPolicy": "agent_review_required",
            "sourceReviewArtifactIds": feedback["sourceReviewArtifactIds"],
            "approvedActions": feedback["approvedActions"],
            "rejectedActions": feedback["rejectedActions"],
            "lowRiskRepairInstructions": [
                {"artifactId": artifact_id, "actionCount": len(actions), "actions": actions}
                for artifact_id, actions in approved_by_artifact.items()
            ],
            "auditOnlyRepairInstructions": [
                {"artifactId": artifact_id, "reasons": actions}
                for artifact_id, actions in rejected_by_artifact.items()
            ],
        }

    def _json_bytes(self, payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
