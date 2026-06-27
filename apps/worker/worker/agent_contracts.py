from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from apps.api.app.models import (
    ArtifactRecord,
    CloudMode,
    EvidenceRef,
    FailureClass,
    InferenceType,
    InferenceValidationStatus,
    RepairAction,
    RunStatus,
    ToolCallStatus,
)
from packages.knowledge import KnowledgeHit
from packages.memory import JobMemoryContext


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
