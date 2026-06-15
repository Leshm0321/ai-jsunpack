from __future__ import annotations

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
    ReviewRun,
    RunStatus,
    ToolCall,
    ToolCallStatus,
)
from packages.knowledge import KnowledgeHit, StaticKnowledgeRetriever
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
    memory_artifact: ArtifactRecord
    knowledge_artifact: ArtifactRecord
    inference_artifacts: list[ArtifactRecord]
    review_artifact: ArtifactRecord
    tool_call_artifact: ArtifactRecord
    message: str


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
class AgentProviderDraft:
    plan_payload: dict[str, Any]
    inferences: list[AgentInferenceDraft]
    review: AgentReviewDraft
    model_provider: str
    model_name: str
    prompt_version: str
    tool_name: str
    tool_version: str
    tool_status: ToolCallStatus
    tool_failure_class: FailureClass
    message: str


class AgentProvider(Protocol):
    tool_name: str
    tool_version: str

    def run(
        self,
        *,
        request: AgentRuntimeRequest,
        memory_context: JobMemoryContext,
        memory_artifact_id: str,
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

    def __init__(self, policy_resolver: ModelPolicyResolver | None = None) -> None:
        self.policy_resolver = policy_resolver or ModelPolicyResolver()

    def run(
        self,
        *,
        request: AgentRuntimeRequest,
        memory_context: JobMemoryContext,
        memory_artifact_id: str,
        knowledge_hits: list[KnowledgeHit],
        knowledge_artifact_id: str,
        evidence_refs: list[EvidenceRef],
    ) -> AgentProviderDraft:
        policy = self.policy_resolver.resolve(request)
        plan_payload = self._plan_payload(
            request=request,
            policy=policy,
            memory_artifact_id=memory_artifact_id,
            knowledge_artifact_id=knowledge_artifact_id,
            knowledge_hits=knowledge_hits,
            evidence_refs=evidence_refs,
        )
        if not policy.allowed:
            return self._policy_denied_draft(
                request=request,
                policy=policy,
                plan_payload=plan_payload,
            )

        prompt_context = self._prompt_context(
            request=request,
            policy=policy,
            memory_context=memory_context,
            knowledge_hits=knowledge_hits,
            evidence_refs=evidence_refs,
        )
        try:
            crew_output = self._run_crewai(prompt_context=prompt_context, policy=policy)
        except Exception as error:
            return self._agent_failed_draft(
                policy=policy,
                plan_payload={
                    **plan_payload,
                    "runtimeStatus": "agent_failed",
                    "limitations": [*plan_payload["limitations"], str(error)],
                },
                error=error,
            )

        crew_output = crew_output if crew_output.inferences else self._empty_crewai_output()
        return AgentProviderDraft(
            plan_payload={
                **plan_payload,
                "runtimeStatus": "completed",
                "plannedAgents": crew_output.planned_agents or plan_payload["plannedAgents"],
                "limitations": [*plan_payload["limitations"], *crew_output.limitations],
            },
            inferences=[self._draft_from_crewai(inference) for inference in crew_output.inferences],
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
        memory_artifact_id: str,
        knowledge_artifact_id: str,
        knowledge_hits: list[KnowledgeHit],
        evidence_refs: list[EvidenceRef],
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
            "memoryRecordArtifactId": memory_artifact_id,
            "knowledgeEvidenceArtifactId": knowledge_artifact_id,
            "plannedAgents": ["PlannerAgent", "AnalysisAgent", "FrameworkAgent", "RuntimeAgent", "ReviewAgent"],
            "inputSummary": self._input_summary(request),
            "knowledgeHitIds": [hit.id for hit in knowledge_hits],
            "evidenceRefs": [ref.model_dump(by_alias=True, exclude_none=True) for ref in evidence_refs],
            "modelPolicy": {
                "allowed": policy.allowed,
                "sanitizedContext": policy.sanitized_context,
                "denialReason": policy.denial_reason,
            },
            "runtimeStatus": "planned",
            "limitations": [],
        }

    def _prompt_context(
        self,
        *,
        request: AgentRuntimeRequest,
        policy: AgentModelPolicy,
        memory_context: JobMemoryContext,
        knowledge_hits: list[KnowledgeHit],
        evidence_refs: list[EvidenceRef],
    ) -> dict[str, Any]:
        return {
            "jobId": request.job_id,
            "projectId": request.project_id,
            "cloudMode": policy.cloud_mode,
            "sanitizedContext": policy.sanitized_context,
            "inputSummary": self._input_summary(request),
            "memory": memory_context.prompt_excerpt,
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
    ) -> AgentProviderDraft:
        reason = policy.denial_reason or "Agent model policy denied execution."
        return AgentProviderDraft(
            plan_payload={
                **plan_payload,
                "runtimeStatus": "policy_denied",
                "limitations": [reason],
            },
            inferences=self._best_effort_inferences(
                uncertainty=[
                    reason,
                    "CrewAI model execution was not attempted because model policy was not satisfied.",
                ]
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
    ) -> AgentProviderDraft:
        detail = f"CrewAI runtime failed: {error}"
        return AgentProviderDraft(
            plan_payload=plan_payload,
            inferences=self._best_effort_inferences(
                uncertainty=[
                    detail,
                    "Deterministic Core evidence remains available for later Agent retry.",
                ]
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
                type="module_split",
                agent_name="AnalysisAgent",
                confidence=0.2,
                uncertainty_reasons=uncertainty,
                alternatives=["retry with an allowed CrewAI model provider"],
            ),
            AgentInferenceDraft(
                type="framework",
                agent_name="FrameworkAgent",
                confidence=0.2,
                uncertainty_reasons=uncertainty,
                alternatives=["classify framework after CrewAI execution succeeds"],
            ),
            AgentInferenceDraft(
                type="runtime",
                agent_name="RuntimeAgent",
                confidence=0.25,
                uncertainty_reasons=uncertainty,
                alternatives=["re-evaluate after runtime smoke and model review evidence are available"],
            ),
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
            plannedAgents=["PlannerAgent", "AnalysisAgent", "FrameworkAgent", "RuntimeAgent", "ReviewAgent"],
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
            memory_context = self.memory_service.create_context(
                job_id=job_id,
                project_id=request.project_id,
                source_artifact_ids=request.input_artifact_ids,
                inventory_payload=request.inventory_payload,
                ast_index_payload=request.ast_index_payload,
                cloud_mode=request.cloud_mode,
            )
            memory_artifact = self._write_memory_artifact(
                job_id=job_id,
                store=store,
                memory_record=memory_context.record,
                parent_artifact_ids=request.input_artifact_ids,
            )
            knowledge_hits = self.knowledge_retriever.retrieve(
                inventory_payload=request.inventory_payload,
                ast_index_payload=request.ast_index_payload,
            )
            knowledge_artifact = self._write_knowledge_artifact(
                job_id=job_id,
                store=store,
                hits=knowledge_hits,
                parent_artifact_ids=[*request.input_artifact_ids, memory_artifact.id],
            )
            evidence_refs = self._evidence_refs(
                request=request,
                memory_artifact=memory_artifact,
                memory_record=memory_context.record,
                knowledge_artifact=knowledge_artifact,
                knowledge_hits=knowledge_hits,
            )
            provider_draft = self.provider.run(
                request=request,
                memory_context=memory_context,
                memory_artifact_id=memory_artifact.id,
                knowledge_hits=knowledge_hits,
                knowledge_artifact_id=knowledge_artifact.id,
                evidence_refs=evidence_refs,
            )
            plan_artifact = store.write_artifact(
                job_id,
                kind="agent_plan",
                stage="agent_planning",
                filename="agent-plan.json",
                content=self._json_bytes(provider_draft.plan_payload),
                content_type="application/json",
                producer="worker.agent_runtime",
                parent_artifact_ids=[*request.input_artifact_ids, memory_artifact.id, knowledge_artifact.id],
            )

            store.update_status(job_id, "agent_pass")
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
                    input_artifact_ids=[*request.input_artifact_ids, memory_artifact.id, knowledge_artifact.id],
                    output_artifact_ids=[plan_artifact.id],
                    evidence_refs=evidence_refs,
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
                        parent_artifact_ids=[*request.input_artifact_ids, memory_artifact.id, knowledge_artifact.id, plan_artifact.id],
                    )
                )

            inference_artifact_ids = [artifact.id for artifact in inference_artifacts]
            review_run = ReviewRun(
                id=f"review_{uuid4().hex[:12]}",
                job_id=job_id,
                attempt=0,
                review_type="agent_review",
                status=provider_draft.review.status,
                decision=provider_draft.review.decision,
                failure_class=provider_draft.review.failure_class,
                evidence_refs=evidence_refs,
                repair_instruction_ids=provider_draft.review.repair_instruction_ids,
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
                parent_artifact_ids=[plan_artifact.id, *inference_artifact_ids],
            )

            output_artifact_ids = [plan_artifact.id, *inference_artifact_ids, review_artifact.id]
            tool_call = ToolCall(
                id=f"tool_call_{uuid4().hex[:12]}",
                job_id=job_id,
                caller="WorkerPipeline",
                tool_name=provider_draft.tool_name,
                tool_version=provider_draft.tool_version,
                input_artifact_ids=[*request.input_artifact_ids, memory_artifact.id, knowledge_artifact.id],
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
            memory_artifact=memory_artifact,
            knowledge_artifact=knowledge_artifact,
            inference_artifacts=inference_artifacts,
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
            filename="memory-record.json",
            content=memory_record.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.memory",
            parent_artifact_ids=parent_artifact_ids,
        )

    def _write_knowledge_artifact(
        self,
        *,
        job_id: str,
        store,
        hits: list[KnowledgeHit],
        parent_artifact_ids: list[str],
    ) -> ArtifactRecord:
        payload = self.knowledge_retriever.artifact_payload(
            job_id=job_id,
            input_artifact_ids=parent_artifact_ids,
            hits=hits,
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

    def _evidence_refs(
        self,
        *,
        request: AgentRuntimeRequest,
        memory_artifact: ArtifactRecord,
        memory_record: MemoryRecord,
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
            EvidenceRef(
                artifact_id=memory_artifact.id,
                label="Job short-term memory",
                locator="memory:short_term",
                excerpt=memory_record.content[:240],
            ),
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
