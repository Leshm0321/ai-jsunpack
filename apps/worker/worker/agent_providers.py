from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from apps.api.app.models import CloudMode, EvidenceRef, FailureClass, InferenceType, InferenceValidationStatus, RunStatus
from packages.knowledge import KnowledgeHit
from packages.memory import JobMemoryContext

from .agent_context import AgentContextBuilder, AgentContextRedactor
from .agent_contracts import (
    AGENT_MODEL_ENV,
    AGENT_PROMPT_VERSION,
    AGENT_PROVIDER_ENV,
    AGENT_TOOL_VERSION,
    CREWAI_DATA_ROOT_ENV,
    LOCAL_AGENT_MODEL_ENV,
    LOCAL_AGENT_PROVIDER_ENV,
    AgentInferenceDraft,
    AgentModelPolicy,
    AgentProviderDraft,
    AgentRepairInstructionDraft,
    AgentReportSectionDraft,
    AgentReviewDraft,
    AgentRuntimeDiagnosisDraft,
    AgentRuntimeRequest,
    CrewAgentPassOutput,
    CrewInferenceOutput,
)
from .agent_feedback import AgentFeedbackRefiner


class ModelPolicyResolver:
    def resolve(self, request: AgentRuntimeRequest) -> AgentModelPolicy:
        if request.cloud_mode == "cloud_allowed":
            return self._cloud_allowed(request)
        if request.cloud_mode == "desensitized":
            return self._desensitized(request)
        return self._local_only(request)

    def _cloud_allowed(self, request: AgentRuntimeRequest) -> AgentModelPolicy:
        model_name = self._config_or_env(request.job_config, "agentModel", AGENT_MODEL_ENV)
        provider = self._config_or_env(request.job_config, "agentModelProvider", AGENT_PROVIDER_ENV) or "cloud"
        return self._policy(
            cloud_mode=request.cloud_mode,
            model_provider=provider,
            model_name=model_name,
            sanitized_context=False,
            denial_reason="cloud_allowed mode requires config.agentModel or AI_JSUNPACK_AGENT_MODEL.",
        )

    def _local_only(self, request: AgentRuntimeRequest) -> AgentModelPolicy:
        model_name = self._config_or_env(request.job_config, "localAgentModel", LOCAL_AGENT_MODEL_ENV)
        provider = self._config_or_env(request.job_config, "localAgentProvider", LOCAL_AGENT_PROVIDER_ENV) or "local"
        return self._policy(
            cloud_mode=request.cloud_mode,
            model_provider=provider,
            model_name=model_name,
            sanitized_context=False,
            denial_reason="local_only mode requires config.localAgentModel or AI_JSUNPACK_LOCAL_AGENT_MODEL.",
        )

    def _desensitized(self, request: AgentRuntimeRequest) -> AgentModelPolicy:
        model_name = (
            self._config_or_env(request.job_config, "agentModel", AGENT_MODEL_ENV)
            or self._config_or_env(request.job_config, "localAgentModel", LOCAL_AGENT_MODEL_ENV)
        )
        provider = (
            self._config_or_env(request.job_config, "agentModelProvider", AGENT_PROVIDER_ENV)
            or self._config_or_env(request.job_config, "localAgentProvider", LOCAL_AGENT_PROVIDER_ENV)
            or "desensitized"
        )
        return self._policy(
            cloud_mode=request.cloud_mode,
            model_provider=provider,
            model_name=model_name,
            sanitized_context=True,
            denial_reason=(
                "desensitized mode requires config.agentModel, config.localAgentModel, "
                "AI_JSUNPACK_AGENT_MODEL, or AI_JSUNPACK_LOCAL_AGENT_MODEL."
            ),
        )

    def _policy(
        self,
        *,
        cloud_mode: CloudMode,
        model_provider: str,
        model_name: str | None,
        sanitized_context: bool,
        denial_reason: str,
    ) -> AgentModelPolicy:
        if model_name:
            return AgentModelPolicy(
                allowed=True,
                cloud_mode=cloud_mode,
                model_provider=model_provider,
                model_name=model_name,
                prompt_version=AGENT_PROMPT_VERSION,
                sanitized_context=sanitized_context,
            )
        return AgentModelPolicy(
            allowed=False,
            cloud_mode=cloud_mode,
            model_provider=model_provider,
            model_name="unconfigured",
            prompt_version=AGENT_PROMPT_VERSION,
            sanitized_context=sanitized_context,
            denial_reason=denial_reason,
        )

    def _config_or_env(self, config: dict[str, Any], key: str, env_name: str) -> str | None:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        env_value = os.getenv(env_name)
        if env_value and env_value.strip():
            return env_value.strip()
        return None


class CrewAIAgentProvider:
    """由 CrewAI 支撑的 provider，提供可审计策略和失败 fallback。"""

    tool_name = "crewai.agent_pass"
    tool_version = AGENT_TOOL_VERSION

    def __init__(
        self,
        policy_resolver: ModelPolicyResolver | None = None,
        redactor: AgentContextRedactor | None = None,
        feedback_refiner: AgentFeedbackRefiner | None = None,
        context_builder: AgentContextBuilder | None = None,
    ) -> None:
        self.policy_resolver = policy_resolver or ModelPolicyResolver()
        self.redactor = redactor or AgentContextRedactor()
        self.feedback_refiner = feedback_refiner or AgentFeedbackRefiner()
        self.context_builder = context_builder or AgentContextBuilder()

    def run(
        self,
        *,
        request: AgentRuntimeRequest,
        memory_context: JobMemoryContext,
        memory_artifact_ids: list[str],
        knowledge_hits: list[KnowledgeHit],
        knowledge_artifact_id: str,
        evidence_refs: list[EvidenceRef],
    ) -> AgentProviderDraft:
        policy = self.policy_resolver.resolve(request)
        redaction = self.redactor.redact(
            policy=policy,
            input_summary=self.context_builder.input_summary(request),
            memory_excerpt=memory_context.prompt_excerpt,
            evidence_refs=evidence_refs,
        )
        plan_payload = self._plan_payload(
            request=request,
            policy=policy,
            memory_artifact_ids=memory_artifact_ids,
            knowledge_artifact_id=knowledge_artifact_id,
            knowledge_hits=knowledge_hits,
            input_summary=redaction.input_summary,
            evidence_refs=redaction.evidence_refs,
            redaction_metadata=redaction.metadata,
        )
        feedback = self.feedback_refiner.refine(knowledge_hits=knowledge_hits)
        if not policy.allowed:
            return self.feedback_refiner.merge(
                self._policy_denied_draft(
                    request=request,
                    policy=policy,
                    plan_payload=plan_payload,
                    evidence_refs=redaction.evidence_refs,
                ),
                feedback,
            )

        prompt_context = self._prompt_context(
            request=request,
            policy=policy,
            memory_excerpt=redaction.memory_excerpt,
            input_summary=redaction.input_summary,
            knowledge_hits=knowledge_hits,
            evidence_refs=redaction.evidence_refs,
            redaction_metadata=redaction.metadata,
        )
        try:
            crew_output = self._run_crewai(prompt_context=prompt_context, policy=policy)
        except Exception as error:
            return self.feedback_refiner.merge(
                self._agent_failed_draft(
                    policy=policy,
                    plan_payload={
                        **plan_payload,
                        "runtimeStatus": "agent_failed",
                        "limitations": [*plan_payload["limitations"], str(error)],
                    },
                    error=error,
                    evidence_refs=redaction.evidence_refs,
                ),
                feedback,
            )

        crew_output = crew_output if crew_output.inferences else self._empty_crewai_output()
        return self.feedback_refiner.merge(
            AgentProviderDraft(
                plan_payload={
                    **plan_payload,
                    "runtimeStatus": "completed",
                    "plannedAgents": crew_output.planned_agents or plan_payload["plannedAgents"],
                    "limitations": [*plan_payload["limitations"], *crew_output.limitations],
                },
                evidence_refs=redaction.evidence_refs,
                inferences=[self._draft_from_crewai(inference) for inference in crew_output.inferences],
                runtime_diagnoses=self._runtime_diagnoses(
                    status=self._run_status(crew_output.review_status, default="best_effort"),
                    failure_class="none",
                    uncertainty=crew_output.limitations or ["Runtime Agent output is based on available Core evidence."],
                ),
                report_sections=self._report_sections(
                    status=self._run_status(crew_output.review_status, default="best_effort"),
                    summary=crew_output.review_decision,
                    uncertainty=crew_output.limitations or ["Report Agent section is generated from Agent pass evidence."],
                ),
                repair_instructions=self._repair_instructions(
                    status=self._run_status(crew_output.review_status, default="best_effort"),
                    failure_class="none",
                    decision=crew_output.review_decision,
                ),
                review=AgentReviewDraft(
                    status=self._run_status(crew_output.review_status, default="best_effort"),
                    decision=crew_output.review_decision,
                    failure_class="none",
                ),
                model_provider=policy.model_provider,
                model_name=policy.model_name,
                prompt_version=policy.prompt_version,
                tool_name=self.tool_name,
                tool_version=self.tool_version,
                tool_status="pass",
                tool_failure_class="none",
                message="CrewAI runtime produced schema-valid Agent inference and review records.",
            ),
            feedback,
        )

    def _run_crewai(self, *, prompt_context: dict[str, Any], policy: AgentModelPolicy) -> CrewAgentPassOutput:
        self._prepare_crewai_storage()
        from crewai import Agent, Crew, Process, Task

        planner = Agent(
            role="Planner Agent",
            goal="Plan a safe JavaScript reconstruction analysis pass from audited artifacts.",
            backstory="You create schema-first plans for artifact-driven reverse analysis.",
            llm=policy.model_name,
            allow_delegation=False,
            verbose=False,
        )
        analysis = Agent(
            role="Analysis Agent",
            goal="Identify module, framework, runtime, and uncertainty evidence from Core artifacts.",
            backstory="You reason only from provided evidence references and deterministic summaries.",
            llm=policy.model_name,
            allow_delegation=False,
            verbose=False,
        )
        review = Agent(
            role="Review Agent",
            goal="Return validated structured findings for downstream deterministic writers.",
            backstory="You reject unsupported conclusions and preserve uncertainty in audit records.",
            llm=policy.model_name,
            allow_delegation=False,
            verbose=False,
        )
        context_json = json.dumps(prompt_context, ensure_ascii=False, indent=2, sort_keys=True)
        plan_task = Task(
            description=(
                "Read this audited context and outline the Agent pass. "
                "Do not request source text outside evidence references.\n\n{agent_context}"
            ),
            expected_output="A concise plan naming the evidence-backed Agent checks.",
            agent=planner,
        )
        analysis_task = Task(
            description=(
                "Analyze module split, framework, runtime, and uncertainty evidence. "
                "Use only the context provided by the planner and input artifacts."
            ),
            expected_output="Evidence-backed findings with confidence and uncertainty.",
            agent=analysis,
            context=[plan_task],
        )
        review_task = Task(
            description=(
                "Return a structured Agent pass output. Include plannedAgents, inferences, "
                "reviewStatus, reviewDecision, and limitations. Keep every inference evidence-bound."
            ),
            expected_output="A CrewAgentPassOutput-compatible object.",
            agent=review,
            context=[plan_task, analysis_task],
            output_pydantic=CrewAgentPassOutput,
        )
        crew = Crew(
            agents=[planner, analysis, review],
            tasks=[plan_task, analysis_task, review_task],
            process=Process.sequential,
            verbose=False,
        )
        result = crew.kickoff(inputs={"agent_context": context_json})
        return self._parse_crewai_result(result)

    def _prepare_crewai_storage(self) -> None:
        data_root = Path(os.getenv(CREWAI_DATA_ROOT_ENV, Path.cwd() / ".crewai-data")).resolve()
        data_root.mkdir(parents=True, exist_ok=True)
        os.environ["LOCALAPPDATA"] = str(data_root)
        os.environ.setdefault("XDG_DATA_HOME", str(data_root))
        os.environ.setdefault("CREWAI_STORAGE_DIR", "ai-jsunpack")
        try:
            import appdirs

            def project_user_data_dir(
                appname: str | None = None,
                appauthor: str | None = None,
                version: str | None = None,
                roaming: bool = False,
            ) -> str:
                parts = [part for part in (appauthor, appname, version) if part]
                target = data_root.joinpath(*parts) if parts else data_root
                target.mkdir(parents=True, exist_ok=True)
                return str(target)

            appdirs.user_data_dir = project_user_data_dir
        except Exception:
            return

    def _parse_crewai_result(self, result: Any) -> CrewAgentPassOutput:
        structured = getattr(result, "pydantic", None)
        if isinstance(structured, CrewAgentPassOutput):
            return structured
        if isinstance(structured, BaseModel):
            return CrewAgentPassOutput.model_validate(structured.model_dump())
        if isinstance(result, CrewAgentPassOutput):
            return result
        if isinstance(result, BaseModel):
            return CrewAgentPassOutput.model_validate(result.model_dump())
        if isinstance(result, dict):
            return CrewAgentPassOutput.model_validate(result)
        raw = getattr(result, "raw", None) or str(result)
        return CrewAgentPassOutput.model_validate(json.loads(raw))

    def _plan_payload(
        self,
        *,
        request: AgentRuntimeRequest,
        policy: AgentModelPolicy,
        memory_artifact_ids: list[str],
        knowledge_artifact_id: str,
        knowledge_hits: list[KnowledgeHit],
        input_summary: dict[str, Any],
        evidence_refs: list[EvidenceRef],
        redaction_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "kind": "agent_plan",
            "jobId": request.job_id,
            "provider": "crewai",
            "modelProvider": policy.model_provider,
            "modelName": policy.model_name,
            "promptVersion": policy.prompt_version,
            "cloudMode": policy.cloud_mode,
            "inputArtifactIds": request.input_artifact_ids,
            "memoryRecordArtifactId": memory_artifact_ids[0] if memory_artifact_ids else None,
            "memoryRecordArtifactIds": memory_artifact_ids,
            "knowledgeEvidenceArtifactId": knowledge_artifact_id,
            "plannedAgents": [
                "PlannerAgent",
                "AnalysisAgent",
                "NamingAgent",
                "TypeAgent",
                "FrameworkAgent",
                "DeadCodeAgent",
                "RuntimeAgent",
                "RepairAgent",
                "ReportAgent",
                "ReviewAgent",
            ],
            "inputSummary": input_summary,
            "knowledgeHitIds": [hit.id for hit in knowledge_hits],
            "evidenceRefs": [ref.model_dump(by_alias=True, exclude_none=True) for ref in evidence_refs],
            "modelPolicy": {
                "allowed": policy.allowed,
                "sanitizedContext": policy.sanitized_context,
                "denialReason": policy.denial_reason,
                "redaction": redaction_metadata,
            },
            "runtimeStatus": "planned",
            "limitations": [],
        }

    def _prompt_context(
        self,
        *,
        request: AgentRuntimeRequest,
        policy: AgentModelPolicy,
        memory_excerpt: str,
        input_summary: dict[str, Any],
        knowledge_hits: list[KnowledgeHit],
        evidence_refs: list[EvidenceRef],
        redaction_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "jobId": request.job_id,
            "projectId": request.project_id,
            "cloudMode": policy.cloud_mode,
            "sanitizedContext": policy.sanitized_context,
            "redactionPolicy": redaction_metadata,
            "inputSummary": input_summary,
            "memory": memory_excerpt,
            "knowledgeHits": [
                {
                    "id": hit.id,
                    "category": hit.category,
                    "label": hit.label,
                    "locator": hit.locator,
                    "excerpt": hit.excerpt,
                    "confidence": hit.confidence,
                }
                for hit in knowledge_hits
            ],
            "evidenceRefs": [ref.model_dump(by_alias=True, exclude_none=True) for ref in evidence_refs],
        }


    def _policy_denied_draft(
        self,
        *,
        request: AgentRuntimeRequest,
        policy: AgentModelPolicy,
        plan_payload: dict[str, Any],
        evidence_refs: list[EvidenceRef],
    ) -> AgentProviderDraft:
        reason = policy.denial_reason or "Agent model policy denied execution."
        return AgentProviderDraft(
            plan_payload={
                **plan_payload,
                "runtimeStatus": "policy_denied",
                "limitations": [reason],
            },
            evidence_refs=evidence_refs,
            inferences=self._best_effort_inferences(
                uncertainty=[
                    reason,
                    "CrewAI model execution was not attempted because model policy was not satisfied.",
                ]
            ),
            runtime_diagnoses=self._runtime_diagnoses(
                status="best_effort",
                failure_class="policy_denied",
                uncertainty=[reason],
            ),
            report_sections=self._report_sections(
                status="best_effort",
                summary=f"CrewAI runtime was blocked by model policy: {reason}",
                uncertainty=[reason],
            ),
            repair_instructions=self._repair_instructions(
                status="best_effort",
                failure_class="policy_denied",
                decision="No Agent repair action was generated because model policy denied execution.",
            ),
            review=AgentReviewDraft(
                status="best_effort",
                decision=f"CrewAI runtime was blocked by model policy: {reason}",
                failure_class="policy_denied",
            ),
            model_provider=policy.model_provider,
            model_name=policy.model_name,
            prompt_version=policy.prompt_version,
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            tool_status="fail",
            tool_failure_class="policy_denied",
            message="CrewAI runtime blocked by model policy and persisted best-effort audit records.",
        )

    def _agent_failed_draft(
        self,
        *,
        policy: AgentModelPolicy,
        plan_payload: dict[str, Any],
        error: Exception,
        evidence_refs: list[EvidenceRef],
    ) -> AgentProviderDraft:
        detail = f"CrewAI runtime failed: {error}"
        return AgentProviderDraft(
            plan_payload=plan_payload,
            evidence_refs=evidence_refs,
            inferences=self._best_effort_inferences(
                uncertainty=[
                    detail,
                    "Deterministic Core evidence remains available for later Agent retry.",
                ]
            ),
            runtime_diagnoses=self._runtime_diagnoses(
                status="fail",
                failure_class="agent_failed",
                uncertainty=[detail],
            ),
            report_sections=self._report_sections(
                status="fail",
                summary=detail,
                uncertainty=[detail],
            ),
            repair_instructions=self._repair_instructions(
                status="fail",
                failure_class="agent_failed",
                decision="No Agent repair action was generated because Agent execution failed.",
            ),
            review=AgentReviewDraft(status="fail", decision=detail, failure_class="agent_failed"),
            model_provider=policy.model_provider,
            model_name=policy.model_name,
            prompt_version=policy.prompt_version,
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            tool_status="fail",
            tool_failure_class="agent_failed",
            message="CrewAI runtime failed and persisted failure audit records.",
        )

    def _best_effort_inferences(self, *, uncertainty: list[str]) -> list[AgentInferenceDraft]:
        return [
            AgentInferenceDraft(
                type="naming",
                agent_name="NamingAgent",
                confidence=0.2,
                uncertainty_reasons=uncertainty,
                alternatives=["preserve original symbol names until NamingAgent can run"],
            ),
            AgentInferenceDraft(
                type="module_split",
                agent_name="AnalysisAgent",
                confidence=0.2,
                uncertainty_reasons=uncertainty,
                alternatives=["retry with an allowed CrewAI model provider"],
            ),
            AgentInferenceDraft(
                type="type_inference",
                agent_name="TypeAgent",
                confidence=0.2,
                uncertainty_reasons=uncertainty,
                alternatives=["emit unknown TypeScript boundaries until TypeAgent can run"],
            ),
            AgentInferenceDraft(
                type="framework",
                agent_name="FrameworkAgent",
                confidence=0.2,
                uncertainty_reasons=uncertainty,
                alternatives=["classify framework after CrewAI execution succeeds"],
            ),
            AgentInferenceDraft(
                type="dead_code",
                agent_name="DeadCodeAgent",
                confidence=0.2,
                uncertainty_reasons=uncertainty,
                alternatives=["retain suspected dead code until evidence-backed review succeeds"],
            ),
            AgentInferenceDraft(
                type="runtime",
                agent_name="RuntimeAgent",
                confidence=0.25,
                uncertainty_reasons=uncertainty,
                alternatives=["re-evaluate after runtime smoke and model review evidence are available"],
            ),
            AgentInferenceDraft(
                type="repair",
                agent_name="RepairAgent",
                confidence=0.2,
                uncertainty_reasons=uncertainty,
                alternatives=["defer repair planning to deterministic build/runtime gates"],
            ),
        ]

    def _runtime_diagnoses(
        self,
        *,
        status: RunStatus,
        failure_class: FailureClass,
        uncertainty: list[str],
    ) -> list[AgentRuntimeDiagnosisDraft]:
        return [
            AgentRuntimeDiagnosisDraft(
                target_stage="runtime_compare",
                status=status,
                failure_class=failure_class,
                diagnosis=(
                    "Runtime Agent preserved browser validation uncertainty as structured diagnosis "
                    "for downstream review and report generation."
                ),
                recommended_actions=[
                    "Inspect runtime_validation and runtime_comparison artifacts when available.",
                    "Keep deterministic build/runtime gates as the authority for applied repairs.",
                ],
                confidence=0.35 if status != "pass" else 0.7,
                uncertainty_reasons=uncertainty,
            )
        ]

    def _report_sections(
        self,
        *,
        status: RunStatus,
        summary: str,
        uncertainty: list[str],
    ) -> list[AgentReportSectionDraft]:
        return [
            AgentReportSectionDraft(
                title="Agent Runtime Summary",
                anchor="agent-runtime-summary",
                summary=summary,
                content=(
                    "Planner, Analysis, Naming, Type, Framework, Dead-Code, Runtime, Repair, "
                    "Report, and Review Agent surfaces are represented as schema-valid audit records."
                ),
                status=status,
                confidence=0.35 if status != "pass" else 0.75,
                uncertainty_reasons=uncertainty,
            )
        ]

    def _repair_instructions(
        self,
        *,
        status: RunStatus,
        failure_class: FailureClass,
        decision: str,
    ) -> list[AgentRepairInstructionDraft]:
        return [
            AgentRepairInstructionDraft(
                target_stage="runtime_compare",
                failure_class=failure_class,
                decision=(
                    f"Repair Agent recorded no free-form source mutation. {decision} "
                    "Deterministic build/runtime repair loops remain responsible for applied changes."
                ),
                status="skipped" if status != "pass" else "planned",
                risk_level="low",
            )
        ]


    def _draft_from_crewai(self, inference: CrewInferenceOutput) -> AgentInferenceDraft:
        return AgentInferenceDraft(
            type=self._inference_type(inference.type),
            agent_name=inference.agent_name,
            confidence=max(0, min(1, inference.confidence)),
            uncertainty_reasons=inference.uncertainty_reasons or ["CrewAI output did not include uncertainty details."],
            alternatives=inference.alternatives or ["keep deterministic Core evidence unchanged"],
            validation_status=self._validation_status(inference.validation_status),
        )

    def _empty_crewai_output(self) -> CrewAgentPassOutput:
        return CrewAgentPassOutput(
            plannedAgents=[
                "PlannerAgent",
                "AnalysisAgent",
                "NamingAgent",
                "TypeAgent",
                "FrameworkAgent",
                "DeadCodeAgent",
                "RuntimeAgent",
                "RepairAgent",
                "ReportAgent",
                "ReviewAgent",
            ],
            inferences=[
                CrewInferenceOutput(
                    type="module_split",
                    agentName="AnalysisAgent",
                    confidence=0.35,
                    uncertaintyReasons=["CrewAI returned no inference records."],
                    alternatives=["retry Agent pass with stricter structured-output prompting"],
                )
            ],
            reviewStatus="best_effort",
            reviewDecision="CrewAI runtime completed but returned no inference records.",
            limitations=["No CrewAI inferences were returned."],
        )

    def _inference_type(self, value: str) -> InferenceType:
        allowed = {"naming", "module_split", "type_inference", "framework", "dead_code", "runtime", "repair"}
        return value if value in allowed else "module_split"  # type: ignore[return-value]

    def _validation_status(self, value: str) -> InferenceValidationStatus:
        allowed = {"unverified", "accepted", "rejected", "needs_review"}
        return value if value in allowed else "needs_review"  # type: ignore[return-value]

    def _run_status(self, value: str, *, default: RunStatus) -> RunStatus:
        allowed = {"pass", "retry", "best_effort", "fail"}
        return value if value in allowed else default  # type: ignore[return-value]

