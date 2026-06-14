from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


JobStatus = Literal[
    "queued",
    "leased",
    "intake",
    "planning",
    "parsing",
    "indexing",
    "analyzing",
    "agent_planning",
    "agent_pass",
    "reconstructing",
    "building",
    "typechecking",
    "runtime_smoke",
    "runtime_compare",
    "reviewing",
    "repairing",
    "packaging",
    "completed",
    "completed_best_effort",
    "failed",
    "cancelled",
]

CloudMode = Literal["cloud_allowed", "local_only", "desensitized"]
ArtifactKind = Literal[
    "source_input",
    "input_inventory",
    "source_index",
    "ast_index",
    "agent_plan",
    "inference_record",
    "reconstruction_plan",
    "generated_project",
    "build_log",
    "runtime_validation",
    "runtime_trace",
    "runtime_screenshot",
    "review_run",
    "repair_instruction",
    "result_package",
    "audit_report",
]
FailureClass = Literal[
    "none",
    "invalid_input",
    "parse_error",
    "agent_failed",
    "dependency_missing",
    "install_failed",
    "type_error",
    "build_error",
    "runtime_error",
    "sandbox_denied",
    "policy_denied",
    "timeout",
    "resource_limit",
    "unknown",
]
SensitivityClass = Literal["public", "derived", "source_sensitive", "secret"]
RetentionClass = Literal["ephemeral", "project", "archive"]
InferenceType = Literal["naming", "module_split", "type_inference", "framework", "dead_code", "runtime", "repair"]
InferenceValidationStatus = Literal["unverified", "accepted", "rejected", "needs_review"]
ReviewType = Literal["build", "typecheck", "runtime_smoke", "runtime_compare", "agent_review"]
RunStatus = Literal["pass", "retry", "best_effort", "fail"]
RuntimeTarget = Literal["original", "reconstructed"]
ToolCallStatus = Literal["pass", "fail"]
MemoryScope = Literal["job", "project", "global"]
MemoryType = Literal["short_term", "long_term", "entity", "scenario"]


def to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_job_id() -> str:
    return f"job_{uuid4().hex[:12]}"


def new_artifact_id() -> str:
    return f"artifact_{uuid4().hex[:12]}"


class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        populate_by_name=True,
        protected_namespaces=(),
        validate_assignment=True,
    )


class CreateJobRequest(ContractModel):
    project_id: str = Field(default="default")
    owner_id: str = Field(default="local-user")
    cloud_mode: CloudMode = Field(default="local_only")
    config: dict[str, Any] = Field(default_factory=dict)


class WorkerLease(ContractModel):
    worker_id: str
    expires_at: str


class JobRecord(ContractModel):
    id: str
    status: JobStatus
    owner_id: str
    project_id: str
    input_artifact_id: str | None = None
    config: dict[str, Any]
    cloud_mode: CloudMode
    review_attempt: int = Field(ge=0)
    worker_lease: WorkerLease | None = None
    failure_class: FailureClass
    failure_reason: str | None = None
    created_at: str
    updated_at: str


class ArtifactRecord(ContractModel):
    id: str
    job_id: str
    kind: ArtifactKind
    stage: JobStatus
    attempt: int = Field(ge=0)
    schema_version: str
    content_type: str
    hash: str
    size: int = Field(ge=0)
    storage_uri: str
    parent_artifact_ids: list[str]
    producer: str
    sensitivity_class: SensitivityClass
    retention_class: RetentionClass
    created_at: str


class EvidenceRef(ContractModel):
    artifact_id: str
    label: str
    locator: str | None = None
    excerpt: str | None = None


class InferenceRecord(ContractModel):
    id: str
    job_id: str
    type: InferenceType
    agent_name: str
    model_provider: str
    model_name: str
    prompt_version: str
    input_artifact_ids: list[str]
    output_artifact_ids: list[str]
    evidence_refs: list[EvidenceRef]
    confidence: float = Field(ge=0, le=1)
    uncertainty_reasons: list[str]
    alternatives: list[str]
    validation_status: InferenceValidationStatus
    rollback_ref: str | None = None


class ReviewRun(ContractModel):
    id: str
    job_id: str
    attempt: int = Field(ge=0)
    review_type: ReviewType
    status: RunStatus
    decision: str
    failure_class: FailureClass
    evidence_refs: list[EvidenceRef]
    repair_instruction_ids: list[str]
    logs_artifact_id: str | None = None


class RuntimeValidationRun(ContractModel):
    id: str
    job_id: str
    attempt: int = Field(ge=0)
    target: RuntimeTarget
    entry_url: str
    status: RunStatus
    console_errors: list[str]
    page_errors: list[str]
    failed_requests: list[str]
    screenshot_artifact_ids: list[str]
    trace_artifact_id: str | None = None
    comparison_artifact_id: str | None = None


class ToolCall(ContractModel):
    id: str
    job_id: str
    caller: str
    tool_name: str
    tool_version: str
    input_artifact_ids: list[str]
    output_artifact_ids: list[str]
    status: ToolCallStatus
    duration: float = Field(ge=0)
    failure_class: FailureClass


class MemoryRecord(ContractModel):
    id: str
    scope: MemoryScope
    project_id: str
    job_id: str | None = None
    memory_type: MemoryType
    content: str
    source_artifact_ids: list[str]
    sensitivity_class: SensitivityClass
    retention_class: RetentionClass


class JobSummary(ContractModel):
    job: JobRecord
    artifacts: list[ArtifactRecord]
