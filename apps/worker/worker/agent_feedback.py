from __future__ import annotations

from apps.api.app.models import FailureClass, InferenceType, InferenceValidationStatus, RepairAction, RunStatus
from packages.knowledge import KnowledgeHit

from .agent_contracts import (
    AgentFeedbackRefinement,
    AgentInferenceDraft,
    AgentProviderDraft,
    AgentRepairInstructionDraft,
    AgentReportSectionDraft,
    AgentReviewDraft,
    AgentRuntimeDiagnosisDraft,
)


class AgentFeedbackRefiner:
    """Routes validation and repair knowledge into conservative Agent outputs."""

    def refine(self, *, knowledge_hits: list[KnowledgeHit]) -> AgentFeedbackRefinement:
        validation_hits = [hit for hit in knowledge_hits if hit.category == "validation_feedback"]
        repair_hits = [hit for hit in knowledge_hits if hit.category == "repair_case"]
        historical_hits = [
            hit
            for hit in knowledge_hits
            if hit.category in {"historical_repair_case", "historical_validation_feedback"}
        ]
        low_risk_repair_hits: list[tuple[KnowledgeHit, list[RepairAction]]] = []
        for hit in repair_hits:
            if self._repair_risk(hit) != "low":
                continue
            actions = self._repair_actions_for_hit(hit)
            if actions:
                low_risk_repair_hits.append((hit, actions))
        low_risk_repair_hit_ids = {hit.id for hit, _ in low_risk_repair_hits}
        audit_only_repair_hits = [hit for hit in repair_hits if hit.id not in low_risk_repair_hit_ids]
        inference_hits = [hit for hit in knowledge_hits if hit.category in self._feedback_inference_categories()]

        inferences = self._feedback_inferences(
            inference_hits=inference_hits,
            validation_hits=validation_hits,
            repair_hits=repair_hits,
        )
        runtime_diagnoses = self._feedback_runtime_diagnoses(validation_hits=validation_hits)
        report_sections = self._feedback_report_sections(
            validation_hits=validation_hits,
            low_risk_repair_hits=[hit for hit, _ in low_risk_repair_hits],
            audit_only_repair_hits=audit_only_repair_hits,
        )
        repair_instructions = [
            AgentRepairInstructionDraft(
                target_stage=self._target_stage_for_hit(hit),
                failure_class=self._failure_class_for_hit(hit),
                decision=(
                    "Repair Agent promoted low-risk current-job Review/Fix evidence for "
                    f"deterministic consumption: {hit.excerpt}"
                ),
                status="planned",
                risk_level="low",
                actions=actions,
            )
            for hit, actions in low_risk_repair_hits
        ]
        feedback_status: RunStatus = "best_effort" if validation_hits or audit_only_repair_hits else "pass"
        failure_class = next(
            (
                self._failure_class_for_hit(hit)
                for hit in [*validation_hits, *audit_only_repair_hits]
                if self._failure_class_for_hit(hit) != "none"
            ),
            "none",
        )
        decision_fragment = None
        if validation_hits or repair_hits:
            decision_fragment = (
                "Review/Fix feedback refinement added "
                f"{len(inferences)} inference(s), {len(runtime_diagnoses)} runtime diagnosis record(s), "
                f"and {len(repair_instructions)} low-risk repair instruction(s); "
                f"{len(audit_only_repair_hits)} repair hint(s) remained audit-only."
            )
        return AgentFeedbackRefinement(
            plan_payload={
                "source": "current_job_and_historical_project_knowledge_evidence",
                "validationFeedbackCount": len(validation_hits),
                "lowRiskRepairCount": len(low_risk_repair_hits),
                "auditOnlyRepairCount": len(audit_only_repair_hits),
                "historicalEvidenceCount": len(historical_hits),
                "targetStages": sorted({self._target_stage_for_hit(hit) for hit in [*validation_hits, *repair_hits]}),
                "consumptionPolicy": "Only low-risk repair_instruction actions are eligible for deterministic writer or repair-runner consumption.",
                "crossJobHistory": bool(historical_hits),
            },
            inferences=inferences,
            runtime_diagnoses=runtime_diagnoses,
            report_sections=report_sections,
            repair_instructions=repair_instructions,
            review_status=feedback_status,
            failure_class=failure_class,
            decision_fragment=decision_fragment,
        )

    def merge(
        self,
        draft: AgentProviderDraft,
        feedback: AgentFeedbackRefinement,
    ) -> AgentProviderDraft:
        decision = draft.review.decision
        if feedback.decision_fragment:
            decision = f"{decision} {feedback.decision_fragment}"
        return AgentProviderDraft(
            plan_payload={
                **draft.plan_payload,
                "reviewFixFeedback": feedback.plan_payload,
                "reviewFixFeedbackStatus": feedback.review_status,
                "reviewFixFeedbackFailureClass": feedback.failure_class,
            },
            evidence_refs=draft.evidence_refs,
            inferences=[*draft.inferences, *feedback.inferences],
            runtime_diagnoses=[*draft.runtime_diagnoses, *feedback.runtime_diagnoses],
            report_sections=[*draft.report_sections, *feedback.report_sections],
            repair_instructions=[*draft.repair_instructions, *feedback.repair_instructions],
            review=AgentReviewDraft(
                status=draft.review.status,
                decision=decision,
                failure_class=draft.review.failure_class,
                repair_instruction_ids=draft.review.repair_instruction_ids,
                logs_artifact_id=draft.review.logs_artifact_id,
            ),
            model_provider=draft.model_provider,
            model_name=draft.model_name,
            prompt_version=draft.prompt_version,
            tool_name=draft.tool_name,
            tool_version=draft.tool_version,
            tool_status=draft.tool_status,
            tool_failure_class=draft.tool_failure_class,
            message=draft.message,
        )

    def _feedback_inference_categories(self) -> set[str]:
        return {
            "browser_shim",
            "build_runtime",
            "framework_feature",
            "module_pattern",
            "obfuscation_pattern",
            "source_map",
        }

    def _feedback_inferences(
        self,
        *,
        inference_hits: list[KnowledgeHit],
        validation_hits: list[KnowledgeHit],
        repair_hits: list[KnowledgeHit],
    ) -> list[AgentInferenceDraft]:
        drafts: list[AgentInferenceDraft] = []
        framework_hit = self._first_hit(inference_hits, "framework_feature")
        if framework_hit is not None:
            drafts.append(
                self._feedback_inference(
                    inference_type="framework",
                    agent_name="FrameworkAgent",
                    hit=framework_hit,
                    alternative="Use framework evidence as a conservative component-boundary hint.",
                    validation_status="accepted",
                )
            )
        naming_hit = self._first_hit(inference_hits, "obfuscation_pattern")
        if naming_hit is not None:
            drafts.append(
                self._feedback_inference(
                    inference_type="naming",
                    agent_name="NamingAgent",
                    hit=naming_hit,
                    alternative="Keep original symbols until naming confidence improves.",
                )
            )
        type_hit = self._first_hit(inference_hits, "source_map") or self._first_hit(inference_hits, "module_pattern")
        if type_hit is not None:
            drafts.append(
                self._feedback_inference(
                    inference_type="type_inference",
                    agent_name="TypeAgent",
                    hit=type_hit,
                    alternative="Use source-map or export evidence as type-boundary candidates only.",
                )
            )
        runtime_hit = validation_hits[0] if validation_hits else self._first_hit(inference_hits, "browser_shim")
        if runtime_hit is not None:
            drafts.append(
                self._feedback_inference(
                    inference_type="runtime",
                    agent_name="RuntimeAgent",
                    hit=runtime_hit,
                    alternative="Route runtime uncertainty through validation and compare gates.",
                )
            )
        repair_hit = repair_hits[0] if repair_hits else None
        if repair_hit is not None:
            risk = self._repair_risk(repair_hit)
            drafts.append(
                self._feedback_inference(
                    inference_type="repair",
                    agent_name="RepairAgent",
                    hit=repair_hit,
                    alternative=(
                        "Promote only low-risk supported repair actions to deterministic writers; "
                        "keep medium/high risk suggestions audit-only."
                    ),
                    validation_status="accepted" if risk == "low" else "needs_review",
                )
            )
        return drafts

    def _feedback_inference(
        self,
        *,
        inference_type: InferenceType,
        agent_name: str,
        hit: KnowledgeHit,
        alternative: str,
        validation_status: InferenceValidationStatus = "needs_review",
    ) -> AgentInferenceDraft:
        return AgentInferenceDraft(
            type=inference_type,
            agent_name=agent_name,
            confidence=max(0, min(1, hit.confidence)),
            uncertainty_reasons=[
                f"Derived from current-job knowledge hit {hit.id}.",
                "Knowledge feedback is evidence-bound and does not override current input artifacts.",
            ],
            alternatives=[alternative],
            validation_status=validation_status,
            rollback_ref=hit.locator,
        )

    def _feedback_runtime_diagnoses(self, *, validation_hits: list[KnowledgeHit]) -> list[AgentRuntimeDiagnosisDraft]:
        return [
            AgentRuntimeDiagnosisDraft(
                target_stage=self._target_stage_for_hit(hit),
                status="retry",
                failure_class=self._failure_class_for_hit(hit),
                diagnosis=f"{hit.label}: {hit.excerpt}",
                recommended_actions=[
                    "Inspect the referenced review/runtime/build evidence before applying repairs.",
                    "Allow deterministic repair loops to consume only low-risk supported actions.",
                ],
                confidence=max(0, min(1, hit.confidence)),
                uncertainty_reasons=[
                    "Diagnosis is derived from current-job Review/Fix feedback.",
                    "Historical repair evidence remains same-project scoped and evidence-only.",
                ],
            )
            for hit in validation_hits[:4]
        ]

    def _feedback_report_sections(
        self,
        *,
        validation_hits: list[KnowledgeHit],
        low_risk_repair_hits: list[KnowledgeHit],
        audit_only_repair_hits: list[KnowledgeHit],
    ) -> list[AgentReportSectionDraft]:
        if not validation_hits and not low_risk_repair_hits and not audit_only_repair_hits:
            return []
        summary = (
            f"Review/Fix feedback routed {len(low_risk_repair_hits)} low-risk repair hint(s) "
            f"and kept {len(audit_only_repair_hits)} repair hint(s) audit-only."
        )
        content = (
            f"Validation feedback hits: {len(validation_hits)}. "
            f"Low-risk deterministic repair candidates: {len(low_risk_repair_hits)}. "
            f"Audit-only repair hints: {len(audit_only_repair_hits)}. "
            "Only repair instructions with supported low-risk actions should be consumed by deterministic writers."
        )
        return [
            AgentReportSectionDraft(
                title="Review/Fix Feedback Routing",
                anchor="review-fix-feedback-routing",
                summary=summary,
                content=content,
                status="best_effort" if validation_hits or audit_only_repair_hits else "pass",
                confidence=0.72 if low_risk_repair_hits else 0.58,
                uncertainty_reasons=[
                    "Feedback comes from current-job evidence only.",
                    "Historical repair case retrieval remains evidence-only and same-project scoped.",
                ],
                agent_name="ReviewAgent",
            )
        ]

    def _repair_actions_for_hit(self, hit: KnowledgeHit) -> list[RepairAction]:
        target_stage = self._target_stage_for_hit(hit)
        if target_stage == "runtime_compare":
            return [
                RepairAction(
                    action="mirror_original_static_entry",
                    path="projectRoot",
                    value="public/original",
                    reason="Low-risk runtime compare repair evidence supports mirroring original static entry files.",
                )
            ]
        if target_stage == "building":
            return [
                RepairAction(
                    action="add_package_script",
                    path="package.json:scripts.build",
                    value="node scripts/build.mjs",
                    reason="Low-risk build feedback can use the generated build shim when package scripts are missing.",
                )
            ]
        if target_stage == "typechecking":
            return [
                RepairAction(
                    action="add_package_script",
                    path="package.json:scripts.typecheck",
                    value="node scripts/typecheck.mjs",
                    reason="Low-risk typecheck feedback can use the generated typecheck shim when package scripts are missing.",
                )
            ]
        return []

    def _target_stage_for_hit(self, hit: KnowledgeHit) -> str:
        haystack = f"{hit.id} {hit.locator} {hit.label}".lower()
        if "typecheck" in haystack or "type_error" in haystack:
            return "typechecking"
        if "build" in haystack or "install" in haystack:
            return "building"
        if "runtime_smoke" in haystack or "runtime_validation" in haystack:
            return "runtime_smoke"
        return "runtime_compare"

    def _failure_class_for_hit(self, hit: KnowledgeHit) -> FailureClass:
        haystack = f"{hit.id} {hit.locator} {hit.label}".lower()
        if "type_error" in haystack or "typecheck" in haystack:
            return "type_error"
        if "build_error" in haystack or "build" in haystack:
            return "build_error"
        if "timeout" in haystack:
            return "timeout"
        if "policy_denied" in haystack:
            return "policy_denied"
        if "runtime" in haystack:
            return "runtime_error"
        return "none"

    def _repair_risk(self, hit: KnowledgeHit) -> str:
        haystack = f"{hit.id} {hit.locator} {hit.label}".lower()
        if "high" in haystack:
            return "high"
        if "medium" in haystack:
            return "medium"
        if "low" in haystack:
            return "low"
        return "medium"

    def _first_hit(self, hits: list[KnowledgeHit], category: str) -> KnowledgeHit | None:
        return next((hit for hit in hits if hit.category == category), None)
