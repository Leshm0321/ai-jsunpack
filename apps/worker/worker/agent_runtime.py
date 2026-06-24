from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from apps.api.app.models import (
    ArtifactRecord,
    CloudMode,
    EvidenceRef,
    FailureClass,
    InferenceRecord,
    InferenceType,
    InferenceValidationStatus,
    MemoryRecord,
    RepairAction,
    RepairInstruction,
    ReviewRun,
    RuntimeDiagnosis,
    RunStatus,
    ReportSectionDetail,
    ToolCall,
    ToolCallStatus,
    ToolRegistryEntry,
    ReportSection,
)
from packages.knowledge import POST_CORE_KINDS, KnowledgeHit, StaticKnowledgeRetriever
from packages.memory import JobMemoryContext, JobMemoryService


AGENT_PROMPT_VERSION = "agent-runtime-v1"
AGENT_TOOL_VERSION = "0.2.0"
AGENT_MODEL_ENV = "AI_JSUNPACK_AGENT_MODEL"
AGENT_PROVIDER_ENV = "AI_JSUNPACK_AGENT_PROVIDER"
LOCAL_AGENT_MODEL_ENV = "AI_JSUNPACK_LOCAL_AGENT_MODEL"
LOCAL_AGENT_PROVIDER_ENV = "AI_JSUNPACK_LOCAL_AGENT_PROVIDER"
CREWAI_DATA_ROOT_ENV = "AI_JSUNPACK_CREWAI_DATA_ROOT"


class AgentRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentRuntimeRequest:
    job_id: str
    project_id: str
    cloud_mode: CloudMode
    job_config: dict[str, Any]
    inventory_artifact_id: str
    ast_index_artifact_id: str
    inventory_payload: dict[str, Any]
    ast_index_payload: dict[str, Any]

    @property
    def input_artifact_ids(self) -> list[str]:
        return [self.inventory_artifact_id, self.ast_index_artifact_id]


@dataclass(frozen=True)
class AgentRuntimeResult:
    plan_artifact: ArtifactRecord
    memory_artifacts: list[ArtifactRecord]
    knowledge_artifact: ArtifactRecord
    tool_registry_artifact: ArtifactRecord
    inference_artifacts: list[ArtifactRecord]
    runtime_diagnosis_artifacts: list[ArtifactRecord]
    report_section_artifacts: list[ArtifactRecord]
    repair_instruction_artifacts: list[ArtifactRecord]
    review_artifact: ArtifactRecord
    tool_call_artifact: ArtifactRecord
    message: str

    @property
    def memory_artifact(self) -> ArtifactRecord:
        return self.memory_artifacts[0]


@dataclass(frozen=True)
class AgentModelPolicy:
    allowed: bool
    cloud_mode: CloudMode
    model_provider: str
    model_name: str
    prompt_version: str
    sanitized_context: bool
    denial_reason: str | None = None


@dataclass(frozen=True)
class AgentContextRedactionResult:
    input_summary: dict[str, Any]
    memory_excerpt: str
    evidence_refs: list[EvidenceRef]
    metadata: dict[str, Any]


class AgentContextRedactor:
    strategy = "deterministic_context_redaction_v1"

    def redact(
        self,
        *,
        policy: AgentModelPolicy,
        input_summary: dict[str, Any],
        memory_excerpt: str,
        evidence_refs: list[EvidenceRef],
    ) -> AgentContextRedactionResult:
        if not policy.sanitized_context:
            return AgentContextRedactionResult(
                input_summary=input_summary,
                memory_excerpt=memory_excerpt,
                evidence_refs=evidence_refs,
                metadata={
                    "applied": False,
                    "strategy": "none",
                    "scope": [],
                    "placeholderFormat": None,
                    "replacementCount": 0,
                    "replacementCounts": {},
                    "limitations": [],
                },
            )

        counts: dict[str, int] = {}
        redacted_summary = self._redact_input_summary(input_summary, counts)
        redacted_memory = self._redact_text(memory_excerpt, "memory", counts) if memory_excerpt else memory_excerpt
        redacted_refs = [self._redact_evidence_ref(ref, counts) for ref in evidence_refs]
        return AgentContextRedactionResult(
            input_summary=redacted_summary,
            memory_excerpt=redacted_memory,
            evidence_refs=redacted_refs,
            metadata={
                "applied": True,
                "strategy": self.strategy,
                "scope": ["inputSummary", "memory", "evidenceRefs"],
                "placeholderFormat": "redacted:<kind>:<sha256-12>",
                "replacementCount": sum(counts.values()),
                "replacementCounts": dict(sorted(counts.items())),
                "limitations": [
                    "Original artifacts remain unchanged; redaction applies to model context and audit evidence excerpts.",
                    "Deterministic placeholders preserve stable references without exposing source text or source-derived names.",
                ],
            },
        )

    def _redact_input_summary(self, input_summary: dict[str, Any], counts: dict[str, int]) -> dict[str, Any]:
        redacted: dict[str, Any] = {}
        for key, value in input_summary.items():
            if key in {"entries", "scripts", "styles", "sourceMaps"}:
                redacted[key] = self._redact_string_list(value, "path", counts)
            elif key == "symbolSample":
                redacted[key] = self._redact_string_list(value, "symbol", counts)
            else:
                redacted[key] = value
        return redacted

    def _redact_evidence_ref(self, ref: EvidenceRef, counts: dict[str, int]) -> EvidenceRef:
        return EvidenceRef(
            artifact_id=ref.artifact_id,
            label=ref.label,
            locator=self._redact_locator(ref.locator, counts),
            excerpt=self._redact_text(ref.excerpt, "source", counts) if ref.excerpt else ref.excerpt,
        )

    def _redact_locator(self, locator: str | None, counts: dict[str, int]) -> str | None:
        if locator is None:
            return None
        if locator.startswith(("artifact:", "memory:", "knowledge:")):
            return locator
        if ":" in locator:
            prefix, value = locator.split(":", 1)
            return f"{prefix}:{self._redact_text(value, 'locator', counts)}"
        return self._redact_text(locator, "locator", counts)

    def _redact_string_list(self, value: Any, kind: str, counts: dict[str, int]) -> list[str]:
        if not isinstance(value, list):
            return []
        return [self._redact_text(item, kind, counts) for item in value if isinstance(item, str)]

    def _redact_text(self, value: str | None, kind: str, counts: dict[str, int]) -> str:
        if value is None:
            return ""
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        counts[kind] = counts.get(kind, 0) + 1
        return f"redacted:{kind}:{digest}"


@dataclass(frozen=True)
class AgentInferenceDraft:
    type: InferenceType
    agent_name: str
    confidence: float
    uncertainty_reasons: list[str]
    alternatives: list[str]
    validation_status: InferenceValidationStatus = "needs_review"
    rollback_ref: str | None = None


@dataclass(frozen=True)
class AgentReviewDraft:
    status: RunStatus
    decision: str
    failure_class: FailureClass
    repair_instruction_ids: list[str] = field(default_factory=list)
    logs_artifact_id: str | None = None


@dataclass(frozen=True)
class AgentRuntimeDiagnosisDraft:
    target_stage: str
    status: RunStatus
    failure_class: FailureClass
    diagnosis: str
    recommended_actions: list[str]
    confidence: float
    uncertainty_reasons: list[str]
    agent_name: str = "RuntimeAgent"


@dataclass(frozen=True)
class AgentReportSectionDraft:
    title: str
    anchor: str
    summary: str
    content: str
    status: RunStatus
    confidence: float
    uncertainty_reasons: list[str]
    details: list[tuple[str, str]] = field(default_factory=list)
    agent_name: str = "ReportAgent"


@dataclass(frozen=True)
class AgentRepairInstructionDraft:
    target_stage: str
    failure_class: FailureClass
    decision: str
    status: str = "skipped"
    risk_level: str = "low"
    actions: list[RepairAction] = field(default_factory=list)


@dataclass(frozen=True)
class AgentProviderDraft:
    plan_payload: dict[str, Any]
    evidence_refs: list[EvidenceRef]
    inferences: list[AgentInferenceDraft]
    runtime_diagnoses: list[AgentRuntimeDiagnosisDraft]
    report_sections: list[AgentReportSectionDraft]
    repair_instructions: list[AgentRepairInstructionDraft]
    review: AgentReviewDraft
    model_provider: str
    model_name: str
    prompt_version: str
    tool_name: str
    tool_version: str
    tool_status: ToolCallStatus
    tool_failure_class: FailureClass
    message: str


@dataclass(frozen=True)
class AgentFeedbackRefinement:
    plan_payload: dict[str, Any]
    inferences: list[AgentInferenceDraft]
    runtime_diagnoses: list[AgentRuntimeDiagnosisDraft]
    report_sections: list[AgentReportSectionDraft]
    repair_instructions: list[AgentRepairInstructionDraft]
    review_status: RunStatus
    failure_class: FailureClass
    decision_fragment: str | None


class AgentProvider(Protocol):
    tool_name: str
    tool_version: str

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
        ...


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


class CrewInferenceOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    type: str = "module_split"
    agent_name: str = Field(default="AnalysisAgent", alias="agentName")
    confidence: float = 0.35
    uncertainty_reasons: list[str] = Field(default_factory=list, alias="uncertaintyReasons")
    alternatives: list[str] = Field(default_factory=list)
    validation_status: str = Field(default="needs_review", alias="validationStatus")


class CrewAgentPassOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    planned_agents: list[str] = Field(default_factory=list, alias="plannedAgents")
    inferences: list[CrewInferenceOutput] = Field(default_factory=list)
    review_status: str = Field(default="best_effort", alias="reviewStatus")
    review_decision: str = Field(default="CrewAI runtime returned structured output.", alias="reviewDecision")
    limitations: list[str] = Field(default_factory=list)


class CrewAIAgentProvider:
    """CrewAI-backed provider with auditable policy and failure fallbacks."""

    tool_name = "crewai.agent_pass"
    tool_version = AGENT_TOOL_VERSION

    def __init__(
        self,
        policy_resolver: ModelPolicyResolver | None = None,
        redactor: AgentContextRedactor | None = None,
    ) -> None:
        self.policy_resolver = policy_resolver or ModelPolicyResolver()
        self.redactor = redactor or AgentContextRedactor()

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
            input_summary=self._input_summary(request),
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
        feedback = self._feedback_refinement(knowledge_hits=knowledge_hits)
        if not policy.allowed:
            return self._merge_feedback_refinement(
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
            return self._merge_feedback_refinement(
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
        return self._merge_feedback_refinement(
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

    def _input_summary(self, request: AgentRuntimeRequest) -> dict[str, Any]:
        inventory = request.inventory_payload.get("inventory", {})
        ast_indexes = request.ast_index_payload.get("astIndexes", [])
        symbol_names = self._symbol_names(request.ast_index_payload)
        return {
            "entries": self._list_excerpt(inventory.get("entries")),
            "scripts": self._list_excerpt(inventory.get("scripts")),
            "styles": self._list_excerpt(inventory.get("styles")),
            "sourceMaps": self._list_excerpt(inventory.get("sourceMaps")),
            "astIndexCount": len(ast_indexes) if isinstance(ast_indexes, list) else 0,
            "symbolCount": len(symbol_names),
            "symbolSample": symbol_names[:8],
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

    def _feedback_refinement(self, *, knowledge_hits: list[KnowledgeHit]) -> AgentFeedbackRefinement:
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

    def _merge_feedback_refinement(
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

    def _list_excerpt(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value[:8] if isinstance(item, str)]

    def _symbol_names(self, payload: dict[str, Any]) -> list[str]:
        names: list[str] = []
        ast_indexes = payload.get("astIndexes", [])
        if not isinstance(ast_indexes, list):
            return names
        for ast_index in ast_indexes:
            if not isinstance(ast_index, dict):
                continue
            symbols = ast_index.get("symbols", [])
            if not isinstance(symbols, list):
                continue
            for symbol in symbols:
                if isinstance(symbol, dict) and isinstance(symbol.get("name"), str):
                    names.append(symbol["name"])
        return names


class AgentRuntime:
    def __init__(
        self,
        provider: AgentProvider | None = None,
        memory_service: JobMemoryService | None = None,
        knowledge_retriever: StaticKnowledgeRetriever | None = None,
    ) -> None:
        self.provider = provider or CrewAIAgentProvider()
        self.memory_service = memory_service or JobMemoryService()
        self.knowledge_retriever = knowledge_retriever or StaticKnowledgeRetriever()

    def run(self, *, job_id: str, store, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        started_at = time.perf_counter()
        try:
            store.update_status(job_id, "agent_planning")
            prior_artifact_payloads = self._current_job_artifact_payloads(job_id=job_id, store=store)
            historical_artifact_payloads = self._historical_project_artifact_payloads(
                job_id=job_id,
                project_id=request.project_id,
                store=store,
            )
            historical_artifact_ids = [
                artifact_id
                for artifact_id in (
                    payload.get("artifactId") for payload in historical_artifact_payloads
                )
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
                self._write_memory_artifact(
                    job_id=job_id,
                    store=store,
                    memory_record=memory_record,
                    parent_artifact_ids=request.input_artifact_ids,
                )
                for memory_record in memory_context.records
            ]
            memory_artifact_ids = [artifact.id for artifact in memory_artifacts]
            tool_registry_entries = self._tool_registry_entries(job_id)
            tool_registry_artifact = self._write_tool_registry_artifact(
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
            knowledge_artifact = self._write_knowledge_artifact(
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
            evidence_refs = self._evidence_refs(
                request=request,
                memory_artifacts=memory_artifacts,
                memory_records=memory_context.records,
                knowledge_artifact=knowledge_artifact,
                knowledge_hits=knowledge_hits,
            )
            provider_draft = self.provider.run(
                request=request,
                memory_context=memory_context,
                memory_artifact_ids=memory_artifact_ids,
                knowledge_hits=knowledge_hits,
                knowledge_artifact_id=knowledge_artifact.id,
                evidence_refs=evidence_refs,
            )
            provider_plan_payload = {
                **provider_draft.plan_payload,
                "toolRegistryArtifactId": tool_registry_artifact.id,
            }
            plan_artifact = store.write_artifact(
                job_id,
                kind="agent_plan",
                stage="agent_planning",
                filename="agent-plan.json",
                content=self._json_bytes(provider_plan_payload),
                content_type="application/json",
                producer="worker.agent_runtime",
                parent_artifact_ids=[
                    *request.input_artifact_ids,
                    *memory_artifact_ids,
                    knowledge_artifact.id,
                    tool_registry_artifact.id,
                ],
            )

            store.update_status(job_id, "agent_pass")
            agent_input_artifact_ids = [
                *request.input_artifact_ids,
                *memory_artifact_ids,
                knowledge_artifact.id,
                tool_registry_artifact.id,
            ]
            inference_artifacts: list[ArtifactRecord] = []
            for index, draft in enumerate(provider_draft.inferences, start=1):
                record = InferenceRecord(
                    id=f"inference_{uuid4().hex[:12]}",
                    job_id=job_id,
                    type=draft.type,
                    agent_name=draft.agent_name,
                    model_provider=provider_draft.model_provider,
                    model_name=provider_draft.model_name,
                    prompt_version=provider_draft.prompt_version,
                    input_artifact_ids=agent_input_artifact_ids,
                    output_artifact_ids=[plan_artifact.id],
                    evidence_refs=provider_draft.evidence_refs,
                    confidence=draft.confidence,
                    uncertainty_reasons=draft.uncertainty_reasons,
                    alternatives=draft.alternatives,
                    validation_status=draft.validation_status,
                    rollback_ref=draft.rollback_ref,
                )
                inference_artifacts.append(
                    store.write_artifact(
                        job_id,
                        kind="inference_record",
                        stage="agent_pass",
                        filename=f"inference-record-{index}.json",
                        content=record.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                        content_type="application/json",
                        producer="worker.agent_runtime",
                        parent_artifact_ids=[*agent_input_artifact_ids, plan_artifact.id],
                    )
                )

            inference_artifact_ids = [artifact.id for artifact in inference_artifacts]
            runtime_diagnosis_artifacts: list[ArtifactRecord] = []
            for index, draft in enumerate(provider_draft.runtime_diagnoses, start=1):
                diagnosis = RuntimeDiagnosis(
                    id=f"runtime_diagnosis_{uuid4().hex[:12]}",
                    job_id=job_id,
                    attempt=0,
                    agent_name=draft.agent_name,
                    target_stage=draft.target_stage,
                    status=draft.status,
                    failure_class=draft.failure_class,
                    input_artifact_ids=agent_input_artifact_ids,
                    evidence_refs=provider_draft.evidence_refs,
                    diagnosis=draft.diagnosis,
                    recommended_actions=draft.recommended_actions,
                    confidence=max(0, min(1, draft.confidence)),
                    uncertainty_reasons=draft.uncertainty_reasons,
                )
                runtime_diagnosis_artifacts.append(
                    store.write_artifact(
                        job_id,
                        kind="runtime_diagnosis",
                        stage="agent_pass",
                        filename=f"runtime-diagnosis-{index}.json",
                        content=diagnosis.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                        content_type="application/json",
                        producer="worker.agent_runtime",
                        parent_artifact_ids=[*agent_input_artifact_ids, plan_artifact.id],
                    )
                )
            runtime_diagnosis_artifact_ids = [artifact.id for artifact in runtime_diagnosis_artifacts]

            report_section_artifacts: list[ArtifactRecord] = []
            for index, draft in enumerate(provider_draft.report_sections, start=1):
                report_section = ReportSection(
                    id=f"report_section_{uuid4().hex[:12]}",
                    job_id=job_id,
                    agent_name=draft.agent_name,
                    title=draft.title,
                    anchor=draft.anchor,
                    summary=draft.summary,
                    content=draft.content,
                    input_artifact_ids=agent_input_artifact_ids,
                    evidence_refs=provider_draft.evidence_refs,
                    status=draft.status,
                    confidence=max(0, min(1, draft.confidence)),
                    uncertainty_reasons=draft.uncertainty_reasons,
                    details=[ReportSectionDetail(label=label, value=value) for label, value in draft.details],
                )
                report_section_artifacts.append(
                    store.write_artifact(
                        job_id,
                        kind="report_section",
                        stage="agent_pass",
                        filename=f"report-section-{index}.json",
                        content=report_section.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                        content_type="application/json",
                        producer="worker.agent_runtime",
                        parent_artifact_ids=[*agent_input_artifact_ids, plan_artifact.id, *runtime_diagnosis_artifact_ids],
                    )
                )
            report_section_artifact_ids = [artifact.id for artifact in report_section_artifacts]

            repair_instruction_artifacts: list[ArtifactRecord] = []
            for index, draft in enumerate(provider_draft.repair_instructions, start=1):
                repair_instruction = RepairInstruction(
                    id=f"repair_{uuid4().hex[:12]}",
                    job_id=job_id,
                    attempt=0,
                    target_stage=draft.target_stage,  # type: ignore[arg-type]
                    failure_class=draft.failure_class,
                    input_artifact_ids=agent_input_artifact_ids,
                    evidence_refs=provider_draft.evidence_refs,
                    actions=draft.actions,
                    status=draft.status,  # type: ignore[arg-type]
                    risk_level=draft.risk_level,  # type: ignore[arg-type]
                    decision=draft.decision,
                )
                repair_instruction_artifacts.append(
                    store.write_artifact(
                        job_id,
                        kind="repair_instruction",
                        stage="agent_pass",
                        filename=f"agent-repair-instruction-{index}.json",
                        content=repair_instruction.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                        content_type="application/json",
                        producer="worker.agent_runtime",
                        parent_artifact_ids=[
                            *agent_input_artifact_ids,
                            plan_artifact.id,
                            *runtime_diagnosis_artifact_ids,
                            *report_section_artifact_ids,
                        ],
                    )
                )
            repair_instruction_artifact_ids = [artifact.id for artifact in repair_instruction_artifacts]

            review_run = ReviewRun(
                id=f"review_{uuid4().hex[:12]}",
                job_id=job_id,
                attempt=0,
                review_type="agent_review",
                status=provider_draft.review.status,
                decision=provider_draft.review.decision,
                failure_class=provider_draft.review.failure_class,
                evidence_refs=provider_draft.evidence_refs,
                repair_instruction_ids=[*provider_draft.review.repair_instruction_ids, *repair_instruction_artifact_ids],
                logs_artifact_id=provider_draft.review.logs_artifact_id,
            )
            review_artifact = store.write_artifact(
                job_id,
                kind="review_run",
                stage="agent_pass",
                filename="agent-review-run.json",
                content=review_run.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                content_type="application/json",
                producer="worker.agent_runtime",
                parent_artifact_ids=[
                    plan_artifact.id,
                    *inference_artifact_ids,
                    *runtime_diagnosis_artifact_ids,
                    *report_section_artifact_ids,
                    *repair_instruction_artifact_ids,
                ],
            )

            output_artifact_ids = [
                plan_artifact.id,
                *inference_artifact_ids,
                *runtime_diagnosis_artifact_ids,
                *report_section_artifact_ids,
                *repair_instruction_artifact_ids,
                review_artifact.id,
            ]
            tool_call = ToolCall(
                id=f"tool_call_{uuid4().hex[:12]}",
                job_id=job_id,
                caller="WorkerPipeline",
                tool_name=provider_draft.tool_name,
                tool_version=provider_draft.tool_version,
                input_artifact_ids=agent_input_artifact_ids,
                output_artifact_ids=output_artifact_ids,
                status=provider_draft.tool_status,
                duration=self._duration_ms(started_at),
                failure_class=provider_draft.tool_failure_class,
            )
            tool_call_artifact = store.write_artifact(
                job_id,
                kind="tool_call",
                stage="agent_pass",
                filename="agent-tool-call.json",
                content=tool_call.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                content_type="application/json",
                producer="worker.agent_runtime",
                parent_artifact_ids=output_artifact_ids,
            )
        except Exception as error:
            raise AgentRuntimeError(f"Agent runtime failed: {error}") from error

        return AgentRuntimeResult(
            plan_artifact=plan_artifact,
            memory_artifacts=memory_artifacts,
            knowledge_artifact=knowledge_artifact,
            tool_registry_artifact=tool_registry_artifact,
            inference_artifacts=inference_artifacts,
            runtime_diagnosis_artifacts=runtime_diagnosis_artifacts,
            report_section_artifacts=report_section_artifacts,
            repair_instruction_artifacts=repair_instruction_artifacts,
            review_artifact=review_artifact,
            tool_call_artifact=tool_call_artifact,
            message=provider_draft.message,
        )

    def _write_memory_artifact(
        self,
        *,
        job_id: str,
        store,
        memory_record: MemoryRecord,
        parent_artifact_ids: list[str],
    ) -> ArtifactRecord:
        return store.write_artifact(
            job_id,
            kind="memory_record",
            stage="agent_planning",
            filename=f"memory-record-{memory_record.memory_type}.json",
            content=memory_record.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.memory",
            parent_artifact_ids=parent_artifact_ids,
        )

    def _write_tool_registry_artifact(
        self,
        *,
        job_id: str,
        store,
        entries: list[ToolRegistryEntry],
        parent_artifact_ids: list[str],
    ) -> ArtifactRecord:
        payload = {
            "kind": "tool_registry",
            "jobId": job_id,
            "entries": [entry.model_dump(by_alias=True) for entry in entries],
        }
        return store.write_artifact(
            job_id,
            kind="tool_registry",
            stage="agent_planning",
            filename="tool-registry.json",
            content=self._json_bytes(payload),
            content_type="application/json",
            producer="worker.tool_registry",
            parent_artifact_ids=parent_artifact_ids,
            sensitivity_class="derived",
        )

    def _tool_registry_entries(self, job_id: str) -> list[ToolRegistryEntry]:
        return [
            ToolRegistryEntry(
                id=f"tool_registry_{uuid4().hex[:12]}",
                job_id=job_id,
                tool_name="crewai.agent_pass",
                tool_version=AGENT_TOOL_VERSION,
                category="model",
                caller="WorkerPipeline",
                input_artifact_kinds=["input_inventory", "ast_index", "memory_record", "knowledge_evidence"],
                output_artifact_kinds=[
                    "agent_plan",
                    "inference_record",
                    "runtime_diagnosis",
                    "report_section",
                    "repair_instruction",
                    "review_run",
                    "tool_call",
                ],
                failure_classes=["none", "policy_denied", "agent_failed"],
                description="Runs schema-first Agent analysis over deterministic Core evidence.",
            ),
            ToolRegistryEntry(
                id=f"tool_registry_{uuid4().hex[:12]}",
                job_id=job_id,
                tool_name="memory.context",
                tool_version="0.1.0",
                category="memory",
                caller="AgentRuntime",
                input_artifact_kinds=["input_inventory", "ast_index"],
                output_artifact_kinds=["memory_record"],
                failure_classes=["none", "unknown"],
                description="Builds short-term, long-term, entity, and scenario memory records for the current project.",
            ),
            ToolRegistryEntry(
                id=f"tool_registry_{uuid4().hex[:12]}",
                job_id=job_id,
                tool_name="knowledge.static_retrieval",
                tool_version="0.1.0",
                category="knowledge",
                caller="AgentRuntime",
                input_artifact_kinds=[
                    "input_inventory",
                    "ast_index",
                    "memory_record",
                    "build_artifact",
                    "build_log",
                    "runtime_trace",
                    "runtime_validation",
                    "runtime_comparison",
                    "review_run",
                    "repair_instruction",
                ],
                output_artifact_kinds=["knowledge_evidence"],
                failure_classes=["none", "unknown"],
                description="Retrieves static build, framework, runtime, repair, current-job validation, and same-project historical evidence hints.",
            ),
        ]

    def _write_knowledge_artifact(
        self,
        *,
        job_id: str,
        store,
        hits: list[KnowledgeHit],
        parent_artifact_ids: list[str],
        prior_artifact_payloads: list[dict[str, Any]] | None = None,
        historical_artifact_payloads: list[dict[str, Any]] | None = None,
    ) -> ArtifactRecord:
        payload = self.knowledge_retriever.artifact_payload(
            job_id=job_id,
            input_artifact_ids=parent_artifact_ids,
            hits=hits,
            prior_artifact_payloads=prior_artifact_payloads,
            historical_artifact_payloads=historical_artifact_payloads,
        )
        return store.write_artifact(
            job_id,
            kind="knowledge_evidence",
            stage="agent_planning",
            filename="knowledge-evidence.json",
            content=self._json_bytes(payload),
            content_type="application/json",
            producer="worker.knowledge",
            parent_artifact_ids=parent_artifact_ids,
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

    def _evidence_refs(
        self,
        *,
        request: AgentRuntimeRequest,
        memory_artifacts: list[ArtifactRecord],
        memory_records: list[MemoryRecord],
        knowledge_artifact: ArtifactRecord,
        knowledge_hits: list[KnowledgeHit],
    ) -> list[EvidenceRef]:
        return [
            EvidenceRef(
                artifact_id=request.inventory_artifact_id,
                label="Core input inventory",
                locator="artifact:input_inventory",
                excerpt=self._inventory_excerpt(request.inventory_payload),
            ),
            EvidenceRef(
                artifact_id=request.ast_index_artifact_id,
                label="Core AST index",
                locator="artifact:ast_index",
                excerpt=self._ast_excerpt(request.ast_index_payload),
            ),
            *[
                EvidenceRef(
                    artifact_id=artifact.id,
                    label=f"Memory: {record.memory_type}",
                    locator=f"memory:{record.memory_type}",
                    excerpt=record.content[:240],
                )
                for artifact, record in zip(memory_artifacts, memory_records)
            ],
            *[
                EvidenceRef(
                    artifact_id=knowledge_artifact.id,
                    label=f"Knowledge: {hit.label}",
                    locator=hit.locator,
                    excerpt=hit.excerpt,
                )
                for hit in knowledge_hits
            ],
        ]

    def _inventory_excerpt(self, payload: dict[str, Any]) -> str:
        inventory = payload.get("inventory", {})
        entries = inventory.get("entries", [])
        scripts = inventory.get("scripts", [])
        return f"entries={list(entries)[:3]}; scripts={list(scripts)[:3]}"

    def _ast_excerpt(self, payload: dict[str, Any]) -> str:
        symbols = CrewAIAgentProvider()._symbol_names(payload)
        return f"symbols={symbols[:5]}"

    def _duration_ms(self, started_at: float) -> float:
        return round((time.perf_counter() - started_at) * 1000, 3)

    def _json_bytes(self, payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
