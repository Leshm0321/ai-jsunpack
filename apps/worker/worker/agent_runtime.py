from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from packages.knowledge import POST_CORE_KINDS, StaticKnowledgeRetriever
from packages.memory import JobMemoryService

from .agent_artifacts import AgentArtifactWriter
from .agent_context import AgentContextBuilder, AgentContextRedactor
from .agent_contracts import (
    AGENT_PROMPT_VERSION,
    AGENT_TOOL_VERSION,
    CREW_AGENT_NAMES,
    AgentFeedbackRefinement,
    AgentInferenceDraft,
    AgentModelPolicy,
    AgentProvider,
    AgentProviderDraft,
    AgentRepairInstructionDraft,
    AgentReportSectionDraft,
    AgentReviewDraft,
    AgentRunSummary,
    AgentRuntimeDiagnosisDraft,
    AgentRuntimeError,
    AgentRuntimeRequest,
    AgentRuntimeResult,
    CrewAgentExecution,
    CrewAgentSpec,
    CrewConflictRecord,
    CrewExecutionStatus,
    CrewStageExecution,
    CrewStageName,
    CrewStructuredAgentOutput,
)
from .agent_feedback import AgentFeedbackRefiner
from .agent_providers import CrewAIExecutionAdapter, ModelPolicyResolver
from .agent_tools import AgentToolRegistryBuilder, ToolSpec


@dataclass(frozen=True)
class RuntimeContext:
    request: AgentRuntimeRequest
    memory_context: Any
    memory_artifacts: list[Any]
    memory_artifact_ids: list[str]
    knowledge_hits: list[Any]
    knowledge_artifact: Any
    tool_registry_artifact: Any
    tool_registry_entries: list[Any]
    evidence_refs: list[Any]
    policy: AgentModelPolicy
    prompt_context: dict[str, Any]
    plan_payload: dict[str, Any]
    feedback: AgentFeedbackRefinement
    redaction_result: Any


class CrewRuntimePlanner:
    """Builds the CrewAI-centric multi-stage plan for a runtime pass."""

    def build_specs(self) -> list[CrewAgentSpec]:
        return [
            CrewAgentSpec(
                name="PlannerAgent",
                stage="planner",
                responsibility="Plan the agent graph, stage ordering, and evidence focus.",
                role="Planner Agent",
                goal="Plan a safe JavaScript reconstruction analysis pass from audited artifacts.",
                backstory="You create schema-first plans for artifact-driven reverse analysis.",
                output_kind="agent_plan",
                allow_parallel=False,
            ),
            CrewAgentSpec(
                name="AnalysisAgent",
                stage="analysis",
                responsibility="Produce overall semantic and module-level analysis.",
                role="Analysis Agent",
                goal="Identify overall semantic structure, likely module split boundaries, and uncertainty.",
                backstory="You reason only from provided evidence references and deterministic summaries.",
                output_kind="inference_record",
                allow_parallel=False,
                dependencies=["PlannerAgent"],
            ),
            CrewAgentSpec(
                name="NamingAgent",
                stage="specialists",
                responsibility="Propose naming-oriented inferences only.",
                role="Naming Agent",
                goal="Assess symbol naming quality and preserve uncertainty for reverse naming.",
                backstory="You only produce naming-scoped evidence and never rewrite source directly.",
                output_kind="inference_record",
                allow_parallel=True,
                dependencies=["AnalysisAgent"],
            ),
            CrewAgentSpec(
                name="TypeAgent",
                stage="specialists",
                responsibility="Propose type-boundary inferences only.",
                role="Type Agent",
                goal="Assess TypeScript boundary hints and type inference opportunities.",
                backstory="You produce type-oriented evidence only and preserve conservative defaults.",
                output_kind="inference_record",
                allow_parallel=True,
                dependencies=["AnalysisAgent"],
            ),
            CrewAgentSpec(
                name="FrameworkAgent",
                stage="specialists",
                responsibility="Assess framework/runtime framework signals only.",
                role="Framework Agent",
                goal="Classify framework cues and component/runtime patterns from evidence.",
                backstory="You reason only about framework cues from deterministic evidence.",
                output_kind="inference_record",
                allow_parallel=True,
                dependencies=["AnalysisAgent"],
            ),
            CrewAgentSpec(
                name="DeadCodeAgent",
                stage="specialists",
                responsibility="Assess dead-code and retention decisions only.",
                role="Dead Code Agent",
                goal="Identify conservative dead-code hypotheses and preserve rollback options.",
                backstory="You only emit dead-code-scoped hypotheses with explicit uncertainty.",
                output_kind="inference_record",
                allow_parallel=True,
                dependencies=["AnalysisAgent"],
            ),
            CrewAgentSpec(
                name="RuntimeAgent",
                stage="specialists",
                responsibility="Assess runtime/browser behavior diagnosis only.",
                role="Runtime Agent",
                goal="Summarize runtime/browser execution risks and diagnosis from evidence.",
                backstory="You preserve uncertainty and route runtime decisions through deterministic gates.",
                output_kind="runtime_diagnosis",
                allow_parallel=True,
                dependencies=["AnalysisAgent"],
            ),
            CrewAgentSpec(
                name="RepairAgent",
                stage="synthesis",
                responsibility="Emit structured repair drafts only.",
                role="Repair Agent",
                goal="Propose only structured, deterministic-safe repair instructions.",
                backstory="You never directly mutate source and only output constrained repair actions.",
                output_kind="repair_instruction",
                allow_parallel=False,
                dependencies=["NamingAgent", "TypeAgent", "FrameworkAgent", "DeadCodeAgent", "RuntimeAgent"],
            ),
            CrewAgentSpec(
                name="ReportAgent",
                stage="synthesis",
                responsibility="Emit report sections only.",
                role="Report Agent",
                goal="Summarize cross-agent findings into report sections for packaging and audit.",
                backstory="You produce structured report sections and preserve audit traceability.",
                output_kind="report_section",
                allow_parallel=False,
                dependencies=["NamingAgent", "TypeAgent", "FrameworkAgent", "DeadCodeAgent", "RuntimeAgent"],
            ),
            CrewAgentSpec(
                name="ReviewAgent",
                stage="review",
                responsibility="Review conflicts, consensus, and final runtime verdict only.",
                role="Review Agent",
                goal="Review cross-agent findings, detect conflicts, and issue a final verdict.",
                backstory="You reject unsupported conclusions and preserve uncertainty in audit records.",
                output_kind="review_run",
                allow_parallel=False,
                dependencies=["RepairAgent", "ReportAgent"],
            ),
        ]

    def build_stage_order(self, specs: list[CrewAgentSpec]) -> list[tuple[CrewStageName, list[CrewAgentSpec]]]:
        order: list[CrewStageName] = ["planner", "analysis", "specialists", "synthesis", "review"]
        by_stage: dict[CrewStageName, list[CrewAgentSpec]] = {stage: [] for stage in order}
        for spec in specs:
            by_stage[spec.stage].append(spec)
        return [(stage, by_stage[stage]) for stage in order]


class CrewConflictDetector:
    """Creates structured conflict summaries for cross-agent review."""

    def detect(self, stage: CrewStageExecution) -> list[CrewConflictRecord]:
        if stage.stage != "specialists":
            return []
        inference_types: dict[str, list[str]] = {}
        for execution in stage.agent_executions:
            for inference in execution.inferences:
                inference_types.setdefault(inference.type, []).append(execution.spec.name)
        conflicts: list[CrewConflictRecord] = []
        for inference_type, agents in inference_types.items():
            unique_agents = sorted(set(agents))
            if len(unique_agents) > 1:
                conflicts.append(
                    CrewConflictRecord(
                        key=f"inference:{inference_type}",
                        severity="info",
                        agents=unique_agents,
                        summary=f"Multiple agents produced {inference_type} evidence; ReviewAgent should consolidate.",
                    )
                )
        statuses = {execution.status for execution in stage.agent_executions}
        if "fail" in statuses and len(statuses) > 1:
            conflicts.append(
                CrewConflictRecord(
                    key="stage:status-divergence",
                    severity="warning",
                    agents=[execution.spec.name for execution in stage.agent_executions],
                    summary="Parallel specialist stage mixed failed and non-failed executions.",
                )
            )
        return conflicts


class CrewExecutionManager:
    """Runs CrewAI agents stage by stage while keeping runtime orchestration local."""

    def __init__(
        self,
        *,
        adapter: CrewAIExecutionAdapter | None = None,
        conflict_detector: CrewConflictDetector | None = None,
    ) -> None:
        self.adapter = adapter or CrewAIExecutionAdapter()
        self.conflict_detector = conflict_detector or CrewConflictDetector()

    def execute(self, *, context: RuntimeContext, specs: list[CrewAgentSpec]) -> tuple[list[CrewStageExecution], AgentProviderDraft]:
        stages: list[CrewStageExecution] = []
        stage_order = CrewRuntimePlanner().build_stage_order(specs)
        executions_by_name: dict[str, CrewAgentExecution] = {}

        for stage_name, stage_specs in stage_order:
            stage_started = time.perf_counter()
            stage_executions: list[CrewAgentExecution] = []
            for spec in stage_specs:
                execution = self.adapter.execute_agent(
                    spec=spec,
                    policy=context.policy,
                    prompt_context=self._prompt_context_for_agent(
                        context=context,
                        spec=spec,
                        stages=stages,
                        executions_by_name=executions_by_name,
                    ),
                    input_artifact_ids=[
                        *context.request.input_artifact_ids,
                        *context.memory_artifact_ids,
                        context.knowledge_artifact.id,
                        context.tool_registry_artifact.id,
                    ],
                    evidence_refs=context.evidence_refs,
                )
                stage_executions.append(
                    CrewAgentExecution(
                        spec=execution.spec,
                        status=execution.status,
                        failure_class=execution.failure_class,
                        attempt=execution.attempt,
                        duration_ms=round((time.perf_counter() - stage_started) * 1000, 3) if len(stage_specs) == 1 else 0.0,
                        input_artifact_ids=execution.input_artifact_ids,
                        evidence_refs=execution.evidence_refs,
                        message=execution.message,
                        raw_output=execution.raw_output,
                        inferences=execution.inferences,
                        runtime_diagnoses=execution.runtime_diagnoses,
                        report_sections=execution.report_sections,
                        repair_instructions=execution.repair_instructions,
                        review=execution.review,
                        model_provider=execution.model_provider,
                        model_name=execution.model_name,
                    )
                )
                executions_by_name[spec.name] = stage_executions[-1]

            status = self._stage_status(stage_executions)
            failure_class = self._stage_failure_class(stage_executions)
            stage = CrewStageExecution(
                stage=stage_name,
                status=status,
                agent_executions=stage_executions,
                duration_ms=round((time.perf_counter() - stage_started) * 1000, 3),
                failure_class=failure_class,
                conflict_summary=[],
                notes=[],
            )
            conflicts = self.conflict_detector.detect(stage)
            stages.append(
                CrewStageExecution(
                    stage=stage.stage,
                    status=stage.status,
                    agent_executions=stage.agent_executions,
                    duration_ms=stage.duration_ms,
                    failure_class=stage.failure_class,
                    conflict_summary=conflicts,
                    notes=self._stage_notes(stage_name=stage.stage, conflicts=conflicts),
                )
            )

        return stages, self._aggregate_provider_draft(context=context, stages=stages)

    def _prompt_context_for_agent(
        self,
        *,
        context: RuntimeContext,
        spec: CrewAgentSpec,
        stages: list[CrewStageExecution],
        executions_by_name: dict[str, CrewAgentExecution],
    ) -> dict[str, Any]:
        completed = {
            name: {
                "status": execution.status,
                "message": execution.message,
                "inferenceCount": len(execution.inferences),
                "runtimeDiagnosisCount": len(execution.runtime_diagnoses),
                "reportSectionCount": len(execution.report_sections),
                "repairInstructionCount": len(execution.repair_instructions),
            }
            for name, execution in executions_by_name.items()
        }
        conflict_summary = [
            {
                "stage": stage.stage,
                "status": stage.status,
                "conflicts": [
                    {
                        "key": conflict.key,
                        "severity": conflict.severity,
                        "agents": conflict.agents,
                        "summary": conflict.summary,
                    }
                    for conflict in stage.conflict_summary
                ],
            }
            for stage in stages
            if stage.conflict_summary
        ]
        return {
            **context.prompt_context,
            "agent": {
                "name": spec.name,
                "stage": spec.stage,
                "responsibility": spec.responsibility,
                "dependencies": spec.dependencies,
            },
            "completedAgents": completed,
            "stageSummaries": [
                {
                    "stage": stage.stage,
                    "status": stage.status,
                    "failureClass": stage.failure_class,
                    "agents": [execution.spec.name for execution in stage.agent_executions],
                }
                for stage in stages
            ],
            "conflictSummary": conflict_summary,
            "plannedAgents": list(CREW_AGENT_NAMES),
        }

    def _aggregate_provider_draft(self, *, context: RuntimeContext, stages: list[CrewStageExecution]) -> AgentProviderDraft:
        executions = [execution for stage in stages for execution in stage.agent_executions]
        planner = next((execution for execution in executions if execution.spec.name == "PlannerAgent"), None)
        review_execution = next((execution for execution in executions if execution.spec.name == "ReviewAgent"), None)

        aggregated_inferences = [draft for execution in executions for draft in execution.inferences]
        aggregated_runtime_diagnoses = [draft for execution in executions for draft in execution.runtime_diagnoses]
        aggregated_report_sections = [draft for execution in executions for draft in execution.report_sections]
        aggregated_repairs = [draft for execution in executions for draft in execution.repair_instructions]
        review = review_execution.review if review_execution is not None and review_execution.review is not None else AgentReviewDraft(
            status="best_effort",
            decision="CrewAI runtime completed without an explicit ReviewAgent verdict.",
            failure_class="unknown",
        )

        plan_payload = {
            **context.plan_payload,
            "runtimeStatus": self._runtime_status(stages),
            "plannedAgents": planner.raw_output.get("plannedAgents", list(CREW_AGENT_NAMES)) if planner else list(CREW_AGENT_NAMES),
            "agentGraph": self._agent_graph(stages),
            "stagePlan": self._stage_plan(stages),
            "stageSummaries": self._stage_summaries(stages),
            "parallelStages": [stage.stage for stage in stages if len(stage.agent_executions) > 1],
            "conflictSummary": [
                {
                    "stage": stage.stage,
                    "status": stage.status,
                    "conflicts": [
                        {
                            "key": conflict.key,
                            "severity": conflict.severity,
                            "agents": conflict.agents,
                            "summary": conflict.summary,
                            "evidenceRefs": conflict.evidence_refs,
                        }
                        for conflict in stage.conflict_summary
                    ],
                }
                for stage in stages
                if stage.conflict_summary
            ],
            "limitations": self._limitations(stages),
        }
        draft = AgentProviderDraft(
            plan_payload=plan_payload,
            evidence_refs=context.redaction_result.evidence_refs,
            inferences=aggregated_inferences,
            runtime_diagnoses=aggregated_runtime_diagnoses or self._default_runtime_diagnoses(review.failure_class),
            report_sections=aggregated_report_sections or self._default_report_sections(review.failure_class, review.decision),
            repair_instructions=aggregated_repairs or self._default_repair_instructions(review.failure_class, review.decision),
            review=review,
            model_provider=context.policy.model_provider,
            model_name=context.policy.model_name,
            prompt_version=context.policy.prompt_version,
            tool_name=self.adapter.tool_name,
            tool_version=self.adapter.tool_version,
            tool_status="fail" if review.failure_class in {"policy_denied", "agent_failed"} else "pass",
            tool_failure_class=review.failure_class,
            message=self._message(stages=stages, review=review),
        )
        return self.adapter.feedback_refiner.merge(draft, context.feedback)

    def _default_runtime_diagnoses(self, failure_class: str) -> list[AgentRuntimeDiagnosisDraft]:
        return [
            AgentRuntimeDiagnosisDraft(
                target_stage="runtime_compare",
                status="best_effort" if failure_class == "policy_denied" else "fail",
                failure_class="policy_denied" if failure_class == "policy_denied" else "agent_failed",
                diagnosis=(
                    "RuntimeAgent preserved runtime uncertainty because CrewAI execution could not produce "
                    "agent-specific runtime diagnosis output."
                ),
                recommended_actions=[
                    "Inspect runtime_validation and runtime_comparison artifacts when available.",
                    "Keep deterministic build/runtime gates as the authority for applied repairs.",
                ],
                confidence=0.35,
                uncertainty_reasons=[
                    "CrewAI execution did not yield a runtime-scoped diagnosis artifact.",
                    "Deterministic review and repair stages remain authoritative.",
                ],
            )
        ]

    def _default_report_sections(self, failure_class: str, decision: str) -> list[AgentReportSectionDraft]:
        status = "best_effort" if failure_class == "policy_denied" else "fail"
        return [
            AgentReportSectionDraft(
                title="Agent Runtime Summary",
                anchor="agent-runtime-summary",
                summary=decision,
                content=(
                    "Planner, Analysis, Naming, Type, Framework, Dead-Code, Runtime, Repair, "
                    "Report, and Review Agent surfaces were orchestrated through the CrewAI runtime. "
                    "This fallback summary preserves audit continuity when no dedicated report section was produced."
                ),
                status=status,  # type: ignore[arg-type]
                confidence=0.35,
                uncertainty_reasons=[
                    "CrewAI execution did not yield a dedicated report-section artifact.",
                    "Deterministic packaging still received structured runtime summary evidence.",
                ],
            )
        ]

    def _default_repair_instructions(self, failure_class: str, decision: str) -> list[AgentRepairInstructionDraft]:
        status = "skipped" if failure_class == "policy_denied" else "skipped"
        return [
            AgentRepairInstructionDraft(
                target_stage="runtime_compare",
                failure_class="policy_denied" if failure_class == "policy_denied" else "agent_failed",  # type: ignore[arg-type]
                decision=(
                    "Repair Agent recorded no free-form source mutation. "
                    f"{decision} Deterministic build/runtime repair loops remain responsible for applied changes."
                ),
                status=status,
                risk_level="low",
            )
        ]

    def _agent_graph(self, stages: list[CrewStageExecution]) -> list[dict[str, Any]]:
        graph: list[dict[str, Any]] = []
        for stage in stages:
            for execution in stage.agent_executions:
                graph.append(
                    {
                        "name": execution.spec.name,
                        "stage": execution.spec.stage,
                        "responsibility": execution.spec.responsibility,
                        "allowParallel": execution.spec.allow_parallel,
                        "dependencies": execution.spec.dependencies,
                        "status": execution.status,
                        "failureClass": execution.failure_class,
                    }
                )
        return graph

    def _stage_plan(self, stages: list[CrewStageExecution]) -> list[dict[str, Any]]:
        return [
            {
                "stage": stage.stage,
                "parallel": len(stage.agent_executions) > 1,
                "agents": [execution.spec.name for execution in stage.agent_executions],
                "status": stage.status,
                "failureClass": stage.failure_class,
            }
            for stage in stages
        ]

    def _stage_summaries(self, stages: list[CrewStageExecution]) -> list[dict[str, Any]]:
        return [
            {
                "stage": stage.stage,
                "status": stage.status,
                "failureClass": stage.failure_class,
                "durationMs": stage.duration_ms,
                "agentCount": len(stage.agent_executions),
                "agentStatuses": {execution.spec.name: execution.status for execution in stage.agent_executions},
            }
            for stage in stages
        ]

    def _limitations(self, stages: list[CrewStageExecution]) -> list[str]:
        limitations: list[str] = []
        for stage in stages:
            for execution in stage.agent_executions:
                if execution.status in {"best_effort", "fail"}:
                    limitations.append(execution.message)
        return limitations

    def _runtime_status(self, stages: list[CrewStageExecution]) -> str:
        statuses = {stage.status for stage in stages}
        if "fail" in statuses:
            return "agent_failed"
        if "best_effort" in statuses or "retry" in statuses:
            return "completed_best_effort"
        return "completed"

    def _stage_status(self, executions: list[CrewAgentExecution]) -> CrewExecutionStatus:
        statuses = {execution.status for execution in executions}
        if "fail" in statuses:
            return "fail"
        if "best_effort" in statuses:
            return "best_effort"
        if "retry" in statuses:
            return "retry"
        if statuses == {"skipped"}:
            return "skipped"
        return "pass"

    def _stage_failure_class(self, executions: list[CrewAgentExecution]) -> str:
        for execution in executions:
            if execution.failure_class != "none":
                return execution.failure_class
        return "none"

    def _stage_notes(self, *, stage_name: CrewStageName, conflicts: list[CrewConflictRecord]) -> list[str]:
        notes: list[str] = []
        if stage_name == "specialists":
            notes.append("Parallel specialist crews executed in isolated runtime contexts.")
        if conflicts:
            notes.append("Conflict summary persisted for ReviewAgent consumption.")
        return notes

    def _message(self, *, stages: list[CrewStageExecution], review: AgentReviewDraft) -> str:
        parallel_count = sum(1 for stage in stages if len(stage.agent_executions) > 1)
        return (
            "CrewAI runtime executed staged multi-agent analysis "
            f"across {len(stages)} stage(s), including {parallel_count} parallel group(s). "
            f"Final review status: {review.status}."
        )


class AgentRuntime:
    def __init__(
        self,
        provider: AgentProvider | None = None,
        memory_service: JobMemoryService | None = None,
        knowledge_retriever: StaticKnowledgeRetriever | None = None,
        context_builder: AgentContextBuilder | None = None,
        tool_registry_builder: AgentToolRegistryBuilder | None = None,
        artifact_writer: AgentArtifactWriter | None = None,
        runtime_planner: CrewRuntimePlanner | None = None,
        execution_manager: CrewExecutionManager | None = None,
    ) -> None:
        self.provider = provider
        self.memory_service = memory_service or JobMemoryService()
        self.knowledge_retriever = knowledge_retriever or StaticKnowledgeRetriever()
        self.context_builder = context_builder or AgentContextBuilder()
        self.tool_registry_builder = tool_registry_builder or AgentToolRegistryBuilder()
        self.artifact_writer = artifact_writer or AgentArtifactWriter(self.knowledge_retriever)
        self.runtime_planner = runtime_planner or CrewRuntimePlanner()
        self.execution_manager = execution_manager or CrewExecutionManager()

    def run(self, *, job_id: str, store, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        started_at = time.perf_counter()
        try:
            store.update_status(job_id, "agent_planning")
            context = self._build_runtime_context(job_id=job_id, store=store, request=request)
            specs = self.runtime_planner.build_specs()
            stages, provider_draft = self.execution_manager.execute(context=context, specs=specs)
            outputs = self.artifact_writer.persist_runtime_outputs(
                job_id=job_id,
                store=store,
                request=request,
                provider_draft=provider_draft,
                stages=stages,
                memory_artifact_ids=context.memory_artifact_ids,
                knowledge_artifact=context.knowledge_artifact,
                tool_registry_artifact=context.tool_registry_artifact,
                started_at=started_at,
            )
        except Exception as error:
            raise AgentRuntimeError(f"Agent runtime failed: {error}") from error

        return AgentRunSummary(
            plan_artifact=outputs.plan_artifact,
            memory_artifacts=context.memory_artifacts,
            knowledge_artifact=context.knowledge_artifact,
            tool_registry_artifact=context.tool_registry_artifact,
            agent_execution_artifacts=outputs.agent_execution_artifacts,
            inference_artifacts=outputs.inference_artifacts,
            runtime_diagnosis_artifacts=outputs.runtime_diagnosis_artifacts,
            report_section_artifacts=outputs.report_section_artifacts,
            repair_instruction_artifacts=outputs.repair_instruction_artifacts,
            review_artifact=outputs.review_artifact,
            tool_call_artifact=outputs.tool_call_artifact,
            message=provider_draft.message,
        )

    def _build_runtime_context(self, *, job_id: str, store, request: AgentRuntimeRequest) -> RuntimeContext:
        prior_artifact_payloads = self._current_job_artifact_payloads(job_id=job_id, store=store)
        historical_artifact_payloads = self._historical_project_artifact_payloads(
            job_id=job_id,
            project_id=request.project_id,
            store=store,
        )
        historical_artifact_ids = [
            artifact_id
            for artifact_id in (payload.get("artifactId") for payload in historical_artifact_payloads)
            if isinstance(artifact_id, str) and artifact_id
        ]
        memory_context = self.memory_service.create_context(
            job_id=job_id,
            project_id=request.project_id,
            source_artifact_ids=request.input_artifact_ids,
            inventory_payload=request.inventory_payload,
            ast_index_payload=request.ast_index_payload,
            cloud_mode=request.cloud_mode,
        )
        memory_artifacts = [
            self.artifact_writer.write_memory_artifact(
                job_id=job_id,
                store=store,
                memory_record=memory_record,
                parent_artifact_ids=request.input_artifact_ids,
            )
            for memory_record in memory_context.records
        ]
        memory_artifact_ids = [artifact.id for artifact in memory_artifacts]
        tool_registry_entries = self.tool_registry_builder.entries(job_id)
        tool_registry_artifact = self.artifact_writer.write_tool_registry_artifact(
            job_id=job_id,
            store=store,
            entries=tool_registry_entries,
            parent_artifact_ids=request.input_artifact_ids,
        )
        knowledge_hits = self.knowledge_retriever.retrieve(
            inventory_payload=request.inventory_payload,
            ast_index_payload=request.ast_index_payload,
            prior_artifact_payloads=prior_artifact_payloads,
            historical_artifact_payloads=historical_artifact_payloads,
        )
        knowledge_artifact = self.artifact_writer.write_knowledge_artifact(
            job_id=job_id,
            store=store,
            hits=knowledge_hits,
            parent_artifact_ids=[
                *request.input_artifact_ids,
                *memory_artifact_ids,
                tool_registry_artifact.id,
                *historical_artifact_ids,
            ],
            prior_artifact_payloads=prior_artifact_payloads,
            historical_artifact_payloads=historical_artifact_payloads,
        )
        evidence_refs = self.context_builder.evidence_refs(
            request=request,
            memory_artifacts=memory_artifacts,
            memory_records=memory_context.records,
            knowledge_artifact=knowledge_artifact,
            knowledge_hits=knowledge_hits,
        )
        adapter = self.execution_manager.adapter
        policy, prompt_context, plan_payload, feedback, redaction_result = adapter.prepare_context(
            request=request,
            memory_context=memory_context,
            memory_artifact_ids=memory_artifact_ids,
            knowledge_hits=knowledge_hits,
            knowledge_artifact_id=knowledge_artifact.id,
            evidence_refs=evidence_refs,
        )
        return RuntimeContext(
            request=request,
            memory_context=memory_context,
            memory_artifacts=memory_artifacts,
            memory_artifact_ids=memory_artifact_ids,
            knowledge_hits=knowledge_hits,
            knowledge_artifact=knowledge_artifact,
            tool_registry_artifact=tool_registry_artifact,
            tool_registry_entries=tool_registry_entries,
            evidence_refs=evidence_refs,
            policy=policy,
            prompt_context=prompt_context,
            plan_payload=plan_payload,
            feedback=feedback,
            redaction_result=redaction_result,
        )

    def _current_job_artifact_payloads(self, *, job_id: str, store) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for artifact in store.list_artifacts(job_id):
            if artifact.kind not in POST_CORE_KINDS:
                continue
            try:
                payload = json.loads(store.read_artifact(job_id, artifact.id).decode("utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict):
                payload.setdefault("artifactId", artifact.id)
                payload.setdefault("kind", artifact.kind)
                payloads.append(payload)
        return payloads

    def _historical_project_artifact_payloads(
        self,
        *,
        job_id: str,
        project_id: str,
        store,
    ) -> list[dict[str, Any]]:
        return store.list_project_artifact_payloads(
            project_id=project_id,
            kinds=("repair_instruction", "review_run", "runtime_comparison"),
            exclude_job_id=job_id,
            limit=12,
        )


__all__ = [
    "AGENT_PROMPT_VERSION",
    "AGENT_TOOL_VERSION",
    "AgentContextBuilder",
    "AgentContextRedactor",
    "AgentFeedbackRefinement",
    "AgentFeedbackRefiner",
    "AgentInferenceDraft",
    "AgentModelPolicy",
    "AgentProvider",
    "AgentProviderDraft",
    "AgentRepairInstructionDraft",
    "AgentReportSectionDraft",
    "AgentReviewDraft",
    "AgentRuntime",
    "AgentRuntimeDiagnosisDraft",
    "AgentRuntimeError",
    "AgentRuntimeRequest",
    "AgentRuntimeResult",
    "AgentToolRegistryBuilder",
    "CrewAIExecutionAdapter",
    "CrewAgentExecution",
    "CrewAgentSpec",
    "CrewConflictDetector",
    "CrewExecutionManager",
    "CrewRuntimePlanner",
    "CrewStructuredAgentOutput",
    "ModelPolicyResolver",
    "ToolSpec",
]
