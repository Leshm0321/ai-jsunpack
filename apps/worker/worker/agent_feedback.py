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
    """将 validation 与 repair knowledge 注入保守的 Agent output。"""

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
                    "Repair Agent 已将当前任务中的低风险 Review/Fix 证据提升为可供确定性流程使用："
                    f"{hit.excerpt}"
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
                "Review/Fix 反馈细化新增了"
                f" {len(inferences)} 条推断、{len(runtime_diagnoses)} 条运行时诊断记录和"
                f" {len(repair_instructions)} 条低风险修复指令；"
                f"另有 {len(audit_only_repair_hits)} 条修复提示仅用于审计。"
            )
        return AgentFeedbackRefinement(
            plan_payload={
                "source": "current_job_and_historical_project_knowledge_evidence",
                "validationFeedbackCount": len(validation_hits),
                "lowRiskRepairCount": len(low_risk_repair_hits),
                "auditOnlyRepairCount": len(audit_only_repair_hits),
                "historicalEvidenceCount": len(historical_hits),
                "targetStages": sorted({self._target_stage_for_hit(hit) for hit in [*validation_hits, *repair_hits]}),
                "consumptionPolicy": "只有低风险 repair_instruction 动作可供 deterministic writer 或 repair runner 使用。",
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
                    alternative="将框架证据作为保守的组件边界线索。",
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
                    alternative="在命名置信度提高前保留原始符号。",
                )
            )
        type_hit = self._first_hit(inference_hits, "source_map") or self._first_hit(inference_hits, "module_pattern")
        if type_hit is not None:
            drafts.append(
                self._feedback_inference(
                    inference_type="type_inference",
                    agent_name="TypeAgent",
                    hit=type_hit,
                    alternative="仅将 source map 或导出证据作为类型边界候选。",
                )
            )
        runtime_hit = validation_hits[0] if validation_hits else self._first_hit(inference_hits, "browser_shim")
        if runtime_hit is not None:
            drafts.append(
                self._feedback_inference(
                    inference_type="runtime",
                    agent_name="RuntimeAgent",
                    hit=runtime_hit,
                    alternative="让运行时不确定性经过验证和对比门禁。",
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
                        "仅将受支持的低风险修复动作提升给 deterministic writer；"
                        "中高风险建议只用于审计。"
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
                f"来源于当前任务的知识命中 {hit.id}。",
                "knowledge feedback 受 evidence 约束，不会覆盖当前 input Artifact。",
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
                    "应用修复前检查所引用的审查、运行时和构建证据。",
                    "仅允许确定性修复循环使用受支持的低风险动作。",
                ],
                confidence=max(0, min(1, hit.confidence)),
                uncertainty_reasons=[
                    "诊断来源于当前任务的 Review/Fix 反馈。",
                    "历史修复证据仍限定在同一项目范围内，且仅作为证据使用。",
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
            f"Review/Fix 反馈已路由 {len(low_risk_repair_hits)} 条低风险修复提示，"
            f"并保留 {len(audit_only_repair_hits)} 条仅用于审计的修复提示。"
        )
        content = (
            f"验证反馈命中：{len(validation_hits)}。"
            f"低风险确定性修复候选：{len(low_risk_repair_hits)}。"
            f"仅用于审计的修复提示：{len(audit_only_repair_hits)}。"
            "deterministic writer 只能使用包含受支持低风险动作的修复指令。"
        )
        return [
            AgentReportSectionDraft(
                title="Review/Fix 反馈路由",
                anchor="review-fix-feedback-routing",
                summary=summary,
                content=content,
                status="best_effort" if validation_hits or audit_only_repair_hits else "pass",
                confidence=0.72 if low_risk_repair_hits else 0.58,
                uncertainty_reasons=[
                    "反馈仅来自当前任务证据。",
                    "历史修复案例检索仍仅作为证据使用，并限定在同一项目范围内。",
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
                    reason="低风险运行时对比修复证据支持镜像原始静态入口文件。",
                )
            ]
        if target_stage == "building":
            return [
                RepairAction(
                    action="add_package_script",
                    path="package.json:scripts.build",
                    value="node scripts/build.mjs",
                    reason="缺少 package script 时，低风险构建反馈可以使用生成的构建垫片。",
                )
            ]
        if target_stage == "typechecking":
            return [
                RepairAction(
                    action="add_package_script",
                    path="package.json:scripts.typecheck",
                    value="node scripts/typecheck.mjs",
                    reason="缺少 package script 时，低风险类型检查反馈可以使用生成的类型检查垫片。",
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
