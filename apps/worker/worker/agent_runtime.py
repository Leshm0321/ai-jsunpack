from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, is_dataclass, replace
from typing import Any

from pydantic import BaseModel

from apps.api.app.models import FailureClass
from packages.knowledge import POST_CORE_KINDS, StaticKnowledgeRetriever
from packages.memory import JobMemoryService

from .agent_artifacts import AgentArtifactWriter
from .agent_context import AgentContextBuilder, AgentContextRedactor
from .agent_contracts import (
    AGENT_PROMPT_VERSION,
    AGENT_TOOL_VERSION,
    CREW_AGENT_NAMES,
    SPECIALIST_AGENT_NAMES,
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
                allow_parallel=True,
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
                allow_parallel=True,
                dependencies=["NamingAgent", "TypeAgent", "FrameworkAgent", "DeadCodeAgent", "RuntimeAgent"],
            ),
            CrewAgentSpec(
                name="ReviewAgent",
                stage="review",
                responsibility="Review conflicts, consensus, and final runtime verdict only.",
                role="Review Agent",
                goal=(
                    "Review cross-agent findings, detect conflicts, issue a final verdict, and explicitly list only "
                    "the provided low-risk repair instruction IDs that are approved for deterministic application."
                ),
                backstory=(
                    "You reject unsupported conclusions, never invent repair instruction IDs, and preserve "
                    "uncertainty in audit records."
                ),
                output_kind="review_run",
                allow_parallel=False,
                dependencies=["RepairAgent", "ReportAgent"],
            ),
        ]

    def build_stage_order(self, specs: list[CrewAgentSpec]) -> list[tuple[CrewStageName, list[CrewAgentSpec]]]:
        order: list[CrewStageName] = ["planner", "analysis", "specialists", "synthesis", "review"]
        self.validate_specs(specs, order=order)
        by_stage: dict[CrewStageName, list[CrewAgentSpec]] = {stage: [] for stage in order}
        for spec in specs:
            by_stage[spec.stage].append(spec)
        return [(stage, by_stage[stage]) for stage in order]

    def validate_specs(self, specs: list[CrewAgentSpec], *, order: list[CrewStageName] | None = None) -> None:
        stage_order = order or ["planner", "analysis", "specialists", "synthesis", "review"]
        stage_indexes = {stage: index for index, stage in enumerate(stage_order)}
        by_name = {spec.name: spec for spec in specs}
        if len(by_name) != len(specs):
            raise AgentRuntimeError("Agent execution plan contains duplicate agent names.")
        for spec in specs:
            if spec.stage not in stage_indexes:
                raise AgentRuntimeError(f"Agent {spec.name} uses unsupported stage {spec.stage!r}.")
            for dependency in spec.dependencies:
                dependency_spec = by_name.get(dependency)
                if dependency_spec is None:
                    raise AgentRuntimeError(f"Agent {spec.name} depends on missing agent {dependency}.")
                if stage_indexes[dependency_spec.stage] >= stage_indexes[spec.stage]:
                    raise AgentRuntimeError(
                        f"Agent {spec.name} dependency {dependency} must belong to an earlier stage."
                    )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise AgentRuntimeError(f"Agent execution plan contains a dependency cycle at {name}.")
            if name in visited:
                return
            visiting.add(name)
            for dependency in by_name[name].dependencies:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in by_name:
            visit(name)


class CrewConflictDetector:
    """Creates structured conflict summaries for cross-agent review."""

    def detect(self, stage: CrewStageExecution) -> list[CrewConflictRecord]:
        if stage.stage != "specialists":
            return []
        records: list[dict[str, Any]] = []
        for execution in stage.agent_executions:
            raw_inferences = execution.raw_output.get("inferences", [])
            for index, inference in enumerate(execution.inferences):
                raw = raw_inferences[index] if index < len(raw_inferences) and isinstance(raw_inferences[index], dict) else {}
                records.append(
                    {
                        "type": str(raw.get("type") or inference.type),
                        "target": self._normalized_optional(raw.get("target")),
                        "value": self._normalized_optional(raw.get("value")),
                        "agent": execution.spec.name,
                        "evidenceRefs": [ref.artifact_id for ref in execution.evidence_refs],
                    }
                )
        conflicts: list[CrewConflictRecord] = []
        targeted: dict[tuple[str, str], list[dict[str, Any]]] = {}
        unscoped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            if record["target"]:
                targeted.setdefault((record["type"], record["target"]), []).append(record)
            else:
                unscoped.setdefault(record["type"], []).append(record)

        for (inference_type, target), grouped in targeted.items():
            agents = sorted({record["agent"] for record in grouped})
            if len(agents) < 2:
                continue
            values = {record["value"] for record in grouped if record["value"] is not None}
            evidence_refs = sorted({ref for record in grouped for ref in record["evidenceRefs"]})
            if len(values) > 1:
                conflicts.append(
                    CrewConflictRecord(
                        key=f"conflict:{inference_type}:{target}",
                        severity="warning",
                        agents=agents,
                        summary=f"Agents proposed different {inference_type} values for target {target!r}.",
                        evidence_refs=evidence_refs,
                    )
                )
            else:
                conflicts.append(
                    CrewConflictRecord(
                        key=f"overlap:{inference_type}:{target}",
                        severity="info",
                        agents=agents,
                        summary=f"Agents produced overlapping {inference_type} evidence for target {target!r}.",
                        evidence_refs=evidence_refs,
                    )
                )
        for inference_type, grouped in unscoped.items():
            agents = sorted({record["agent"] for record in grouped})
            if len(agents) > 1:
                conflicts.append(
                    CrewConflictRecord(
                        key=f"overlap:{inference_type}",
                        severity="info",
                        agents=agents,
                        summary=(
                            f"Multiple agents produced {inference_type} evidence without a common target; "
                            "ReviewAgent should consolidate the overlap."
                        ),
                        evidence_refs=sorted({ref for record in grouped for ref in record["evidenceRefs"]}),
                    )
                )
        records_by_target: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            if record["target"]:
                records_by_target.setdefault(record["target"], []).append(record)
        dead_values = {"dead", "remove", "removed", "unused", "delete", "drop"}
        for target, grouped in records_by_target.items():
            dead_records = [
                record
                for record in grouped
                if record["type"] == "dead_code" and record["value"] in dead_values
            ]
            retained_records = [record for record in grouped if record["type"] != "dead_code"]
            if dead_records and retained_records:
                combined = [*dead_records, *retained_records]
                conflicts.append(
                    CrewConflictRecord(
                        key=f"conflict:retention:{target}",
                        severity="warning",
                        agents=sorted({record["agent"] for record in combined}),
                        summary=(
                            f"Dead-code evidence proposes removing target {target!r} while another specialist "
                            "produces retained semantic evidence for it."
                        ),
                        evidence_refs=sorted({ref for record in combined for ref in record["evidenceRefs"]}),
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

    def _normalized_optional(self, value: Any) -> str | None:
        if value is None:
            return None
        normalized = " ".join(str(value).strip().lower().split())
        return normalized or None


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
        requested_max_parallel = self._requested_max_parallel(context)
        effective_max_parallel = 1
        scheduler_mode = "serial"
        stage_worker_counts: dict[str, int] = {}
        parallel_stages: list[str] = []
        selection_enabled = any(spec.name == "PlannerAgent" for spec in specs)
        selected_specialists = [name for name in SPECIALIST_AGENT_NAMES if any(spec.name == name for spec in specs)]
        planner_fallback_reason: str | None = "planner_not_executed" if selection_enabled else None

        for stage_name, stage_specs in stage_order:
            if stage_name == "specialists" and selection_enabled:
                stage_specs = [spec for spec in stage_specs if spec.name in selected_specialists]
            elif stage_name == "synthesis" and selection_enabled:
                stage_specs = [
                    replace(
                        spec,
                        dependencies=[
                            name
                            for name in spec.dependencies
                            if name not in SPECIALIST_AGENT_NAMES or name in selected_specialists
                        ],
                    )
                    for spec in stage_specs
                ]
            stage_started = time.perf_counter()
            stage_worker_count = self._stage_worker_count(
                stage_specs=stage_specs,
                requested_max_parallel=requested_max_parallel,
            )
            stage_worker_counts[stage_name] = stage_worker_count
            effective_max_parallel = max(effective_max_parallel, stage_worker_count)
            if stage_worker_count > 1:
                scheduler_mode = "bounded_parallel"
                parallel_stages.append(stage_name)
                with ThreadPoolExecutor(max_workers=stage_worker_count, thread_name_prefix=f"agent-{stage_name}") as executor:
                    futures = {
                        spec.name: executor.submit(
                            self._execute_spec,
                            context=context,
                            spec=spec,
                            stages=stages,
                            executions_by_name=executions_by_name,
                        )
                        for spec in stage_specs
                    }
                    stage_executions = [futures[spec.name].result() for spec in stage_specs]
            else:
                stage_executions = [
                    self._execute_spec(
                        context=context,
                        spec=spec,
                        stages=stages,
                        executions_by_name=executions_by_name,
                    )
                    for spec in stage_specs
                ]
            executions_by_name.update({execution.spec.name: execution for execution in stage_executions})

            if stage_name == "planner" and selection_enabled:
                planner_execution = next(
                    (execution for execution in stage_executions if execution.spec.name == "PlannerAgent"),
                    None,
                )
                selected_specialists, planner_fallback_reason = self._planner_specialist_selection(
                    planner_execution=planner_execution,
                    available_specialists=[
                        name for name in SPECIALIST_AGENT_NAMES if any(spec.name == name for spec in specs)
                    ],
                )

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
                    notes=self._stage_notes(
                        stage_name=stage.stage,
                        conflicts=conflicts,
                        worker_count=stage_worker_count,
                    ),
                )
            )

        return stages, self._aggregate_provider_draft(
            context=context,
            stages=stages,
            selected_specialists=selected_specialists,
            planner_fallback_reason=planner_fallback_reason,
            requested_max_parallel=requested_max_parallel,
            effective_max_parallel=effective_max_parallel,
            scheduler_mode=scheduler_mode,
            stage_worker_counts=stage_worker_counts,
            parallel_stages=parallel_stages,
        )

    def _requested_max_parallel(self, context: RuntimeContext) -> int:
        agents_config = context.request.job_config.get("agents", {})
        raw_value = agents_config.get("maxParallel", 5) if isinstance(agents_config, dict) else 5
        try:
            return max(1, min(10, int(raw_value)))
        except (TypeError, ValueError):
            return 5

    def _stage_worker_count(self, *, stage_specs: list[CrewAgentSpec], requested_max_parallel: int) -> int:
        if len(stage_specs) <= 1 or not all(spec.allow_parallel for spec in stage_specs):
            return 1
        adapter_parallel_safe = bool(
            getattr(self.adapter, "parallel_safe", False)
            or getattr(self.adapter, "isolation_mode", None) == "process"
        )
        if not adapter_parallel_safe:
            return 1
        return max(1, min(requested_max_parallel, len(stage_specs)))

    def _planner_specialist_selection(
        self,
        *,
        planner_execution: CrewAgentExecution | None,
        available_specialists: list[str],
    ) -> tuple[list[str], str | None]:
        fallback = list(available_specialists)
        if planner_execution is None:
            return fallback, "planner_execution_missing"
        if (
            planner_execution.status in {"fail", "retry", "skipped"}
            or planner_execution.failure_class != "none"
        ):
            return fallback, f"planner_execution_{planner_execution.status}:{planner_execution.failure_class}"
        raw_selection = planner_execution.raw_output.get("plannedAgents")
        if not isinstance(raw_selection, list) or not raw_selection:
            return fallback, "planner_selection_empty"
        if any(not isinstance(name, str) or name not in SPECIALIST_AGENT_NAMES for name in raw_selection):
            return fallback, "planner_selection_outside_allowlist"
        selected = [name for name in SPECIALIST_AGENT_NAMES if name in raw_selection and name in available_specialists]
        if not selected:
            return fallback, "planner_selection_unavailable"
        return selected, None

    def _execute_spec(
        self,
        *,
        context: RuntimeContext,
        spec: CrewAgentSpec,
        stages: list[CrewStageExecution],
        executions_by_name: dict[str, CrewAgentExecution],
    ) -> CrewAgentExecution:
        started = time.perf_counter()
        blocked = [
            executions_by_name[name]
            for name in spec.dependencies
            if name in executions_by_name
            and not (spec.name == "AnalysisAgent" and name == "PlannerAgent")
            and (
                executions_by_name[name].status in {"fail", "retry", "skipped"}
                or executions_by_name[name].failure_class != "none"
            )
        ]
        if blocked:
            first = blocked[0]
            return CrewAgentExecution(
                spec=spec,
                status="skipped",
                failure_class=first.failure_class if first.failure_class != "none" else "agent_failed",
                attempt=0,
                duration_ms=0.0,
                input_artifact_ids=self._input_artifact_ids(context),
                evidence_refs=context.evidence_refs,
                message=f"{spec.name} skipped because dependency {first.spec.name} did not complete successfully.",
                model_provider=context.policy.model_provider,
                model_name=context.policy.model_name,
                model_base_url_configured=context.policy.base_url_configured,
                model_api_key_configured=context.policy.api_key_configured,
                model_custom_endpoint_enabled=context.policy.custom_endpoint_enabled,
                model_timeout_seconds=context.policy.timeout_seconds,
                model_temperature=context.policy.temperature,
            )
        prompt_context = self._prompt_context_for_agent(
            context=context,
            spec=spec,
            stages=stages,
            executions_by_name=executions_by_name,
        )
        prompt_context, budget_audit = self._apply_context_budget(
            context=context,
            spec=spec,
            prompt_context=prompt_context,
        )
        if not budget_audit["withinBudget"]:
            return self._context_budget_exceeded_execution(
                context=context,
                spec=spec,
                started=started,
                budget_audit=budget_audit,
            )
        execution = self.adapter.execute_agent(
            spec=spec,
            policy=context.policy,
            prompt_context=prompt_context,
            input_artifact_ids=self._input_artifact_ids(context),
            evidence_refs=context.evidence_refs,
        )
        repair_instructions = [
            replace(draft, id=draft.id or f"{spec.name}:repair:{index}")
            for index, draft in enumerate(execution.repair_instructions, start=1)
        ]
        return replace(
            execution,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            context_budget_audit=budget_audit,
            repair_instructions=repair_instructions,
        )

    def _context_budget_exceeded_execution(
        self,
        *,
        context: RuntimeContext,
        spec: CrewAgentSpec,
        started: float,
        budget_audit: dict[str, Any],
    ) -> CrewAgentExecution:
        message = (
            f"{spec.name} was not invoked because required context exceeded agents.contextBudget "
            f"({budget_audit['estimatedTokensAfter']} > {budget_audit['budgetTokens']} estimated tokens)."
        )
        review = (
            AgentReviewDraft(
                status="fail",
                decision=message,
                failure_class="resource_limit",
            )
            if spec.name == "ReviewAgent"
            else None
        )
        return CrewAgentExecution(
            spec=spec,
            status="fail",
            failure_class="resource_limit",
            attempt=0,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            input_artifact_ids=self._input_artifact_ids(context),
            evidence_refs=context.evidence_refs,
            message=message,
            raw_output={
                "limitations": [message],
                "contextBudgetAudit": budget_audit,
            },
            review=review,
            model_provider=context.policy.model_provider,
            model_name=context.policy.model_name,
            model_base_url_configured=context.policy.base_url_configured,
            model_api_key_configured=context.policy.api_key_configured,
            model_custom_endpoint_enabled=context.policy.custom_endpoint_enabled,
            model_timeout_seconds=context.policy.timeout_seconds,
            model_temperature=context.policy.temperature,
            context_budget_audit=budget_audit,
            isolation_mode=str(getattr(self.adapter, "isolation_mode", "in_process")),
        )

    def _input_artifact_ids(self, context: RuntimeContext) -> list[str]:
        return [
            *context.request.input_artifact_ids,
            *context.memory_artifact_ids,
            context.knowledge_artifact.id,
            context.tool_registry_artifact.id,
        ]

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
        context_agent_names = list(spec.dependencies)
        if spec.name == "ReviewAgent":
            context_agent_names = [
                name
                for name in (*SPECIALIST_AGENT_NAMES, "RepairAgent", "ReportAgent")
                if name in executions_by_name
            ]
        dependency_outputs = {
            name: self._structured_output_for_context(executions_by_name[name])
            for name in context_agent_names
            if name in executions_by_name
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
        prompt = {
            **context.prompt_context,
            "agent": {
                "name": spec.name,
                "stage": spec.stage,
                "responsibility": spec.responsibility,
                "dependencies": spec.dependencies,
            },
            "completedAgents": completed,
            "dependencyOutputs": dependency_outputs,
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
        if spec.name == "ReviewAgent":
            prompt["reviewInputs"] = {
                "specialists": [name for name in SPECIALIST_AGENT_NAMES if name in dependency_outputs],
                "synthesis": [name for name in ("RepairAgent", "ReportAgent") if name in dependency_outputs],
                "normalizedOutputsKey": "dependencyOutputs",
            }
        return prompt

    def _apply_context_budget(
        self,
        *,
        context: RuntimeContext,
        spec: CrewAgentSpec,
        prompt_context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        agents_config = context.request.job_config.get("agents", {})
        raw_budget = agents_config.get("contextBudget", 16_000) if isinstance(agents_config, dict) else 16_000
        try:
            budget = max(1_000, min(1_000_000, int(raw_budget)))
        except (TypeError, ValueError):
            budget = 16_000
        trimmed = self._context_value(prompt_context)
        before = self._estimate_prompt_tokens(trimmed)
        omissions = {
            "historicalKnowledgeHits": 0,
            "memory": 0,
            "nonDependencyAgentSummaries": 0,
            "evidenceExcerpts": 0,
        }

        knowledge_hits = trimmed.get("knowledgeHits")
        if isinstance(knowledge_hits, list) and self._estimate_prompt_tokens(trimmed) > budget:
            retained_hits = []
            for hit in knowledge_hits:
                if isinstance(hit, dict) and str(hit.get("category", "")).startswith("historical_"):
                    omissions["historicalKnowledgeHits"] += 1
                else:
                    retained_hits.append(hit)
            trimmed["knowledgeHits"] = retained_hits

        if self._estimate_prompt_tokens(trimmed) > budget and trimmed.get("memory"):
            trimmed["memory"] = "[omitted by context budget]"
            omissions["memory"] = 1

        completed_agents = trimmed.get("completedAgents")
        if isinstance(completed_agents, dict) and self._estimate_prompt_tokens(trimmed) > budget:
            dependency_names = set(spec.dependencies)
            if spec.name == "ReviewAgent":
                dependency_names.update((*SPECIALIST_AGENT_NAMES, "RepairAgent", "ReportAgent"))
            for name in list(completed_agents):
                if name not in dependency_names:
                    completed_agents.pop(name, None)
                    omissions["nonDependencyAgentSummaries"] += 1
                    if self._estimate_prompt_tokens(trimmed) <= budget:
                        break

        evidence_refs = trimmed.get("evidenceRefs")
        if isinstance(evidence_refs, list) and self._estimate_prompt_tokens(trimmed) > budget:
            for evidence_ref in evidence_refs:
                if not isinstance(evidence_ref, dict) or not evidence_ref.get("excerpt"):
                    continue
                evidence_ref["excerpt"] = "[omitted by context budget]"
                omissions["evidenceExcerpts"] += 1
                if self._estimate_prompt_tokens(trimmed) <= budget:
                    break

        after = self._estimate_prompt_tokens(trimmed)
        audit = {
            "agentName": spec.name,
            "budgetTokens": budget,
            "estimatedTokensBefore": before,
            "estimatedTokensAfter": after,
            "omissions": omissions,
            "withinBudget": after <= budget,
            "estimateMethod": "ceil_utf8_bytes_div_4",
        }
        trimmed["contextBudgetAudit"] = audit
        audit["estimatedTokensAfter"] = self._estimate_prompt_tokens(trimmed)
        audit["withinBudget"] = audit["estimatedTokensAfter"] <= budget
        return trimmed, audit

    def _estimate_prompt_tokens(self, value: Any) -> int:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return math.ceil(len(serialized.encode("utf-8")) / 4)

    def _structured_output_for_context(self, execution: CrewAgentExecution) -> dict[str, Any]:
        return {
            "status": execution.status,
            "failureClass": execution.failure_class,
            "message": execution.message,
            "rawOutput": self._context_value(execution.raw_output),
            "inferences": self._context_value(execution.inferences),
            "runtimeDiagnoses": self._context_value(execution.runtime_diagnoses),
            "reportSections": self._context_value(execution.report_sections),
            "repairInstructions": self._context_value(execution.repair_instructions),
            "review": self._context_value(execution.review),
        }

    def _context_value(self, value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(by_alias=True, exclude_none=True)
        if is_dataclass(value) and not isinstance(value, type):
            return self._context_value(asdict(value))
        if isinstance(value, dict):
            return {str(key): self._context_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._context_value(item) for item in value]
        return value

    def _aggregate_provider_draft(
        self,
        *,
        context: RuntimeContext,
        stages: list[CrewStageExecution],
        selected_specialists: list[str] | None = None,
        planner_fallback_reason: str | None = None,
        requested_max_parallel: int = 5,
        effective_max_parallel: int = 1,
        scheduler_mode: str = "serial",
        stage_worker_counts: dict[str, int] | None = None,
        parallel_stages: list[str] | None = None,
    ) -> AgentProviderDraft:
        executions = [execution for stage in stages for execution in stage.agent_executions]
        planner = next((execution for execution in executions if execution.spec.name == "PlannerAgent"), None)
        review_execution = next((execution for execution in executions if execution.spec.name == "ReviewAgent"), None)

        aggregated_inferences = [draft for execution in executions for draft in execution.inferences]
        aggregated_runtime_diagnoses = [draft for execution in executions for draft in execution.runtime_diagnoses]
        aggregated_report_sections = [draft for execution in executions for draft in execution.report_sections]
        aggregated_repairs = [draft for execution in executions for draft in execution.repair_instructions]
        fallback_failure_class = next(
            (stage.failure_class for stage in stages if stage.failure_class != "none"),
            "unknown",
        )
        review = review_execution.review if review_execution is not None and review_execution.review is not None else AgentReviewDraft(
            status="best_effort",
            decision="CrewAI runtime completed without an explicit ReviewAgent verdict.",
            failure_class=fallback_failure_class,  # type: ignore[arg-type]
        )
        known_repair_ids = {draft.id for draft in aggregated_repairs if draft.id}
        unknown_review_ids = sorted(set(review.repair_instruction_ids) - known_repair_ids)
        if unknown_review_ids:
            review = replace(
                review,
                status="fail",
                decision=(
                    f"{review.decision} ReviewAgent referenced unknown repair instruction IDs: "
                    f"{', '.join(unknown_review_ids)}."
                ),
                failure_class="agent_failed",
                repair_instruction_ids=[
                    instruction_id
                    for instruction_id in review.repair_instruction_ids
                    if instruction_id in known_repair_ids
                ],
            )

        plan_payload = {
            **context.plan_payload,
            "runtimeStatus": self._runtime_status(stages),
            "plannedAgents": planner.raw_output.get("plannedAgents", list(CREW_AGENT_NAMES)) if planner else list(CREW_AGENT_NAMES),
            "selectedAgents": list(selected_specialists or SPECIALIST_AGENT_NAMES),
            "fallbackReason": planner_fallback_reason,
            "requestedMaxParallel": requested_max_parallel,
            "effectiveMaxParallel": effective_max_parallel,
            "schedulerMode": scheduler_mode,
            "stageWorkerCounts": dict(stage_worker_counts or {}),
            "contextBudgetUsage": [
                execution.context_budget_audit
                for execution in executions
                if execution.context_budget_audit
            ],
            "agentGraph": self._agent_graph(stages),
            "stagePlan": self._stage_plan(
                stages,
                stage_worker_counts=stage_worker_counts or {},
            ),
            "stageSummaries": self._stage_summaries(stages),
            "parallelStages": list(parallel_stages or []),
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
            tool_status=(
                "pass"
                if review.status == "pass" and review.failure_class == "none"
                else "fail"
            ),
            tool_failure_class=review.failure_class,
            message=self._message(
                stages=stages,
                review=review,
                parallel_count=len(parallel_stages or []),
            ),
        )
        return self.adapter.feedback_refiner.merge(draft, context.feedback)

    def _default_runtime_diagnoses(self, failure_class: FailureClass) -> list[AgentRuntimeDiagnosisDraft]:
        fallback_failure_class: FailureClass = failure_class if failure_class != "none" else "agent_failed"
        return [
            AgentRuntimeDiagnosisDraft(
                target_stage="runtime_compare",
                status="best_effort" if failure_class == "policy_denied" else "fail",
                failure_class=fallback_failure_class,
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

    def _default_report_sections(self, failure_class: FailureClass, decision: str) -> list[AgentReportSectionDraft]:
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

    def _default_repair_instructions(
        self,
        failure_class: FailureClass,
        decision: str,
    ) -> list[AgentRepairInstructionDraft]:
        status = "skipped" if failure_class == "policy_denied" else "skipped"
        fallback_failure_class: FailureClass = failure_class if failure_class != "none" else "agent_failed"
        return [
            AgentRepairInstructionDraft(
                target_stage="runtime_compare",
                failure_class=fallback_failure_class,
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

    def _stage_plan(
        self,
        stages: list[CrewStageExecution],
        *,
        stage_worker_counts: dict[str, int],
    ) -> list[dict[str, Any]]:
        return [
            {
                "stage": stage.stage,
                "parallel": stage_worker_counts.get(stage.stage, 1) > 1,
                "workerCount": stage_worker_counts.get(stage.stage, 1),
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
        if "best_effort" in statuses or "retry" in statuses or "skipped" in statuses:
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
        if "skipped" in statuses:
            return "best_effort"
        return "pass"

    def _stage_failure_class(self, executions: list[CrewAgentExecution]) -> str:
        for execution in executions:
            if execution.failure_class != "none":
                return execution.failure_class
        return "none"

    def _stage_notes(
        self,
        *,
        stage_name: CrewStageName,
        conflicts: list[CrewConflictRecord],
        worker_count: int,
    ) -> list[str]:
        notes: list[str] = []
        if stage_name == "specialists":
            execution_mode = "in parallel" if worker_count > 1 else "serially"
            notes.append(f"Specialist crews executed {execution_mode} in isolated runtime contexts.")
        if conflicts:
            notes.append("Conflict summary persisted for ReviewAgent consumption.")
        return notes

    def _message(
        self,
        *,
        stages: list[CrewStageExecution],
        review: AgentReviewDraft,
        parallel_count: int,
    ) -> str:
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
