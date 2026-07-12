from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apps.api.app.models import (
    ArtifactRecord,
    CloudMode,
    EvidenceRef,
    FailureClass,
    InferenceType,
    InferenceValidationStatus,
    RepairAction,
    RepairActionName,
    RepairInstructionStatus,
    RepairRiskLevel,
    RepairTargetStage,
    RunStatus,
    ToolCallStatus,
)
from packages.knowledge import KnowledgeHit
from packages.memory import JobMemoryContext


AGENT_PROMPT_VERSION = "agent-runtime-v1"
AGENT_TOOL_VERSION = "0.2.0"
AGENT_MODEL_ENV = "AI_JSUNPACK_AGENT_MODEL"
AGENT_PROVIDER_ENV = "AI_JSUNPACK_AGENT_PROVIDER"
AGENT_BASE_URL_ENV = "AI_JSUNPACK_AGENT_BASE_URL"
AGENT_API_KEY_ENV = "AI_JSUNPACK_AGENT_API_KEY"
LOCAL_AGENT_MODEL_ENV = "AI_JSUNPACK_LOCAL_AGENT_MODEL"
LOCAL_AGENT_PROVIDER_ENV = "AI_JSUNPACK_LOCAL_AGENT_PROVIDER"
LOCAL_AGENT_BASE_URL_ENV = "AI_JSUNPACK_LOCAL_AGENT_BASE_URL"
LOCAL_AGENT_API_KEY_ENV = "AI_JSUNPACK_LOCAL_AGENT_API_KEY"
AGENT_TIMEOUT_SECONDS_ENV = "AI_JSUNPACK_AGENT_TIMEOUT_SECONDS"
AGENT_TEMPERATURE_ENV = "AI_JSUNPACK_AGENT_TEMPERATURE"
CREWAI_DATA_ROOT_ENV = "AI_JSUNPACK_CREWAI_DATA_ROOT"

CrewStageName = Literal["planner", "analysis", "specialists", "synthesis", "review"]
CrewExecutionStatus = Literal["pass", "retry", "best_effort", "fail", "skipped"]

CREW_AGENT_NAMES: tuple[str, ...] = (
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
)

SPECIALIST_AGENT_NAMES: tuple[str, ...] = (
    "NamingAgent",
    "TypeAgent",
    "FrameworkAgent",
    "DeadCodeAgent",
    "RuntimeAgent",
)
SpecialistAgentName = Literal[
    "NamingAgent",
    "TypeAgent",
    "FrameworkAgent",
    "DeadCodeAgent",
    "RuntimeAgent",
]


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
class CrewAgentSpec:
    name: str
    stage: CrewStageName
    responsibility: str
    role: str
    goal: str
    backstory: str
    output_kind: str
    allow_parallel: bool
    dependencies: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CrewTaskSpec:
    name: str
    description: str
    expected_output: str
    input_keys: list[str] = field(default_factory=list)
    context_agents: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CrewConflictRecord:
    key: str
    severity: str
    agents: list[str]
    summary: str
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentModelPolicy:
    allowed: bool
    cloud_mode: CloudMode
    model_provider: str
    model_name: str
    prompt_version: str
    sanitized_context: bool
    denial_reason: str | None = None
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False, compare=False)
    timeout_seconds: float = 30.0
    temperature: float | None = None

    @property
    def base_url_configured(self) -> bool:
        return bool(self.base_url)

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def custom_endpoint_enabled(self) -> bool:
        return self.model_provider.strip().lower() == "openai-compatible" and self.base_url_configured


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
    id: str = ""


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


@dataclass(frozen=True)
class CrewAgentExecution:
    spec: CrewAgentSpec
    status: CrewExecutionStatus
    failure_class: FailureClass
    attempt: int
    duration_ms: float
    input_artifact_ids: list[str]
    evidence_refs: list[EvidenceRef]
    message: str
    raw_output: dict[str, Any] = field(default_factory=dict)
    inferences: list[AgentInferenceDraft] = field(default_factory=list)
    runtime_diagnoses: list[AgentRuntimeDiagnosisDraft] = field(default_factory=list)
    report_sections: list[AgentReportSectionDraft] = field(default_factory=list)
    repair_instructions: list[AgentRepairInstructionDraft] = field(default_factory=list)
    review: AgentReviewDraft | None = None
    model_provider: str = "unknown"
    model_name: str = "unknown"
    model_base_url_configured: bool = False
    model_api_key_configured: bool = False
    model_custom_endpoint_enabled: bool = False
    model_timeout_seconds: float = 30.0
    model_temperature: float | None = None
    context_budget_audit: dict[str, Any] = field(default_factory=dict)
    isolation_mode: str = "in_process"
    process_exit_status: int | None = None
    process_data_root_configured: bool = False
    role_schema_validated: bool = False


@dataclass(frozen=True)
class CrewStageExecution:
    stage: CrewStageName
    status: CrewExecutionStatus
    agent_executions: list[CrewAgentExecution]
    duration_ms: float
    failure_class: FailureClass
    conflict_summary: list[CrewConflictRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentRunSummary:
    plan_artifact: ArtifactRecord
    memory_artifacts: list[ArtifactRecord]
    knowledge_artifact: ArtifactRecord
    tool_registry_artifact: ArtifactRecord
    agent_execution_artifacts: list[ArtifactRecord]
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


AgentRuntimeResult = AgentRunSummary


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


class CrewInferenceOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    type: str = "module_split"
    agent_name: str = Field(default="AnalysisAgent", alias="agentName")
    confidence: float = 0.35
    uncertainty_reasons: list[str] = Field(default_factory=list, alias="uncertaintyReasons")
    alternatives: list[str] = Field(default_factory=list)
    validation_status: str = Field(default="needs_review", alias="validationStatus")
    target: str | None = None
    value: str | None = None


class CrewRuntimeDiagnosisOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    agent_name: str = Field(default="RuntimeAgent", alias="agentName")
    target_stage: str = Field(default="runtime_compare", alias="targetStage")
    status: str = "best_effort"
    failure_class: str = Field(default="none", alias="failureClass")
    diagnosis: str = "Runtime evidence remains inconclusive."
    recommended_actions: list[str] = Field(default_factory=list, alias="recommendedActions")
    confidence: float = 0.35
    uncertainty_reasons: list[str] = Field(default_factory=list, alias="uncertaintyReasons")


class CrewReportSectionDetailOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    label: str
    value: str


class CrewReportSectionOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    agent_name: str = Field(default="ReportAgent", alias="agentName")
    title: str = "Agent Runtime Summary"
    anchor: str = "agent-runtime-summary"
    summary: str = "CrewAI runtime produced a report section."
    content: str = "Structured report content."
    status: str = "best_effort"
    confidence: float = 0.35
    uncertainty_reasons: list[str] = Field(default_factory=list, alias="uncertaintyReasons")
    details: list[CrewReportSectionDetailOutput] = Field(default_factory=list)


class CrewRepairActionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    action: RepairActionName
    path: str = Field(min_length=1)
    value: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class CrewRepairInstructionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    target_stage: RepairTargetStage = Field(default="runtime_compare", alias="targetStage")
    failure_class: FailureClass = Field(default="none", alias="failureClass")
    decision: str = "No deterministic repair action proposed."
    status: RepairInstructionStatus = "skipped"
    risk_level: RepairRiskLevel = Field(default="low", alias="riskLevel")
    actions: list[CrewRepairActionOutput] = Field(default_factory=list)


class CrewReviewOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: str = "best_effort"
    decision: str = "CrewAI runtime returned structured output."
    failure_class: str = Field(default="none", alias="failureClass")
    repair_instruction_ids: list[str] = Field(default_factory=list, alias="repairInstructionIds")
    limitations: list[str] = Field(default_factory=list)
    consensus_summary: str | None = Field(default=None, alias="consensusSummary")


class CrewStagePlanOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    stage: str
    agents: list[str]
    parallel: bool = False
    description: str = ""


class CrewStructuredAgentOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    planned_agents: list[str] = Field(default_factory=list, alias="plannedAgents")
    stage_plan: list[CrewStagePlanOutput] = Field(default_factory=list, alias="stagePlan")
    evidence_focus: list[str] = Field(default_factory=list, alias="evidenceFocus")
    inferences: list[CrewInferenceOutput] = Field(default_factory=list)
    runtime_diagnoses: list[CrewRuntimeDiagnosisOutput] = Field(default_factory=list, alias="runtimeDiagnoses")
    report_sections: list[CrewReportSectionOutput] = Field(default_factory=list, alias="reportSections")
    repair_instructions: list[CrewRepairInstructionOutput] = Field(default_factory=list, alias="repairInstructions")
    review: CrewReviewOutput | None = None
    limitations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class _CrewRoleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    limitations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CrewPlannerAgentOutput(_CrewRoleOutput):
    planned_agents: list[SpecialistAgentName] = Field(min_length=1, alias="plannedAgents")
    stage_plan: list[CrewStagePlanOutput] = Field(default_factory=list, alias="stagePlan")
    evidence_focus: list[str] = Field(default_factory=list, alias="evidenceFocus")


class _CrewInferenceAgentOutput(_CrewRoleOutput):
    allowed_inference_types: ClassVar[frozenset[str]] = frozenset()

    inferences: list[CrewInferenceOutput] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_inference_scope(self) -> _CrewInferenceAgentOutput:
        invalid = sorted({item.type for item in self.inferences if item.type not in self.allowed_inference_types})
        if invalid:
            allowed = ", ".join(sorted(self.allowed_inference_types))
            raise ValueError(f"Inference types {invalid!r} are outside this role contract; allowed: {allowed}.")
        return self


class CrewAnalysisAgentOutput(_CrewInferenceAgentOutput):
    allowed_inference_types = frozenset({"module_split"})


class CrewNamingAgentOutput(_CrewInferenceAgentOutput):
    allowed_inference_types = frozenset({"naming"})


class CrewTypeAgentOutput(_CrewInferenceAgentOutput):
    allowed_inference_types = frozenset({"type_inference"})


class CrewFrameworkAgentOutput(_CrewInferenceAgentOutput):
    allowed_inference_types = frozenset({"framework"})


class CrewDeadCodeAgentOutput(_CrewInferenceAgentOutput):
    allowed_inference_types = frozenset({"dead_code"})


class CrewRuntimeAgentOutput(_CrewRoleOutput):
    runtime_diagnoses: list[CrewRuntimeDiagnosisOutput] = Field(min_length=1, alias="runtimeDiagnoses")


class CrewRepairAgentOutput(_CrewRoleOutput):
    repair_instructions: list[CrewRepairInstructionOutput] = Field(min_length=1, alias="repairInstructions")


class CrewReportAgentOutput(_CrewRoleOutput):
    report_sections: list[CrewReportSectionOutput] = Field(min_length=1, alias="reportSections")


class CrewReviewAgentOutput(_CrewRoleOutput):
    review: CrewReviewOutput


CREW_ROLE_OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "PlannerAgent": CrewPlannerAgentOutput,
    "AnalysisAgent": CrewAnalysisAgentOutput,
    "NamingAgent": CrewNamingAgentOutput,
    "TypeAgent": CrewTypeAgentOutput,
    "FrameworkAgent": CrewFrameworkAgentOutput,
    "DeadCodeAgent": CrewDeadCodeAgentOutput,
    "RuntimeAgent": CrewRuntimeAgentOutput,
    "RepairAgent": CrewRepairAgentOutput,
    "ReportAgent": CrewReportAgentOutput,
    "ReviewAgent": CrewReviewAgentOutput,
}


def crew_output_model_for_agent(agent_name: str) -> type[BaseModel]:
    """Return the strict role contract used for one CrewAI task output."""

    try:
        return CREW_ROLE_OUTPUT_MODELS[agent_name]
    except KeyError as error:
        raise AgentRuntimeError(f"No structured output contract is registered for agent {agent_name!r}.") from error


def validate_crew_output_for_agent(
    agent_name: str,
    output: CrewStructuredAgentOutput | BaseModel | dict[str, Any],
) -> CrewStructuredAgentOutput:
    """Validate a role-scoped payload and normalize it to the provider-compatible envelope.

    Model-provided identity is never trusted: fields carrying ``agentName`` are
    deterministically rebound to the executing agent before validation.
    """

    if isinstance(output, BaseModel):
        payload = output.model_dump(by_alias=True, exclude_none=True)
        for field_name in (
            "plannedAgents",
            "stagePlan",
            "evidenceFocus",
            "inferences",
            "runtimeDiagnoses",
            "reportSections",
            "repairInstructions",
        ):
            if payload.get(field_name) == []:
                payload.pop(field_name, None)
    else:
        payload = dict(output)
    for collection_name in ("inferences", "runtimeDiagnoses", "reportSections"):
        collection = payload.get(collection_name)
        if isinstance(collection, list):
            for item in collection:
                if isinstance(item, dict):
                    item["agentName"] = agent_name
    validated = crew_output_model_for_agent(agent_name).model_validate(payload)
    return CrewStructuredAgentOutput.model_validate(validated.model_dump(by_alias=True, exclude_none=True))


class CrewAgentPassOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    planned_agents: list[str] = Field(default_factory=list, alias="plannedAgents")
    inferences: list[CrewInferenceOutput] = Field(default_factory=list)
    review_status: str = Field(default="best_effort", alias="reviewStatus")
    review_decision: str = Field(default="CrewAI runtime returned structured output.", alias="reviewDecision")
    limitations: list[str] = Field(default_factory=list)


__all__ = [
    "AGENT_API_KEY_ENV",
    "AGENT_BASE_URL_ENV",
    "AGENT_MODEL_ENV",
    "AGENT_PROMPT_VERSION",
    "AGENT_PROVIDER_ENV",
    "AGENT_TEMPERATURE_ENV",
    "AGENT_TIMEOUT_SECONDS_ENV",
    "AGENT_TOOL_VERSION",
    "CREWAI_DATA_ROOT_ENV",
    "CREW_AGENT_NAMES",
    "CREW_ROLE_OUTPUT_MODELS",
    "SPECIALIST_AGENT_NAMES",
    "CrewAgentPassOutput",
    "CrewAnalysisAgentOutput",
    "CrewAgentSpec",
    "CrewConflictRecord",
    "CrewExecutionStatus",
    "CrewInferenceOutput",
    "CrewDeadCodeAgentOutput",
    "CrewFrameworkAgentOutput",
    "CrewNamingAgentOutput",
    "CrewPlannerAgentOutput",
    "CrewRepairActionOutput",
    "CrewRepairInstructionOutput",
    "CrewRepairAgentOutput",
    "CrewReportSectionDetailOutput",
    "CrewReportSectionOutput",
    "CrewReportAgentOutput",
    "CrewReviewOutput",
    "CrewReviewAgentOutput",
    "CrewRuntimeDiagnosisOutput",
    "CrewRuntimeAgentOutput",
    "CrewStageExecution",
    "CrewStageName",
    "CrewStagePlanOutput",
    "CrewStructuredAgentOutput",
    "CrewTypeAgentOutput",
    "CrewTaskSpec",
    "LOCAL_AGENT_API_KEY_ENV",
    "LOCAL_AGENT_BASE_URL_ENV",
    "LOCAL_AGENT_MODEL_ENV",
    "LOCAL_AGENT_PROVIDER_ENV",
    "AgentFeedbackRefinement",
    "AgentInferenceDraft",
    "AgentModelPolicy",
    "AgentProvider",
    "AgentProviderDraft",
    "AgentRepairInstructionDraft",
    "AgentReportSectionDraft",
    "AgentReviewDraft",
    "AgentRunSummary",
    "AgentRuntimeDiagnosisDraft",
    "AgentRuntimeError",
    "AgentRuntimeRequest",
    "AgentRuntimeResult",
    "CrewAgentExecution",
    "crew_output_model_for_agent",
    "validate_crew_output_for_agent",
]
