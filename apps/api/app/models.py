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
    "build_artifact",
    "runtime_validation",
    "runtime_trace",
    "runtime_screenshot",
    "runtime_scenario",
    "runtime_comparison",
    "review_run",
    "tool_call",
    "tool_registry",
    "memory_record",
    "knowledge_evidence",
    "repair_instruction",
    "runtime_diagnosis",
    "report_section",
    "ops_alert_event",
    "result_package",
    "audit_report",
    "html_report",
    "evidence_index",
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
RetentionCategory = Literal["source", "derived", "package", "logs", "screenshots", "memory"]
InferenceType = Literal["naming", "module_split", "type_inference", "framework", "dead_code", "runtime", "repair"]
InferenceValidationStatus = Literal["unverified", "accepted", "rejected", "needs_review"]
ReviewType = Literal["build", "typecheck", "runtime_smoke", "runtime_compare", "agent_review"]
RunStatus = Literal["pass", "retry", "best_effort", "fail"]
RuntimeTarget = Literal["original", "reconstructed"]
ToolCallStatus = Literal["pass", "fail"]
ToolRegistryCategory = Literal["code", "build", "runtime", "audit", "knowledge", "memory", "model"]
MemoryScope = Literal["job", "project", "global"]
MemoryType = Literal["short_term", "long_term", "entity", "scenario"]
RepairTargetStage = Literal["building", "typechecking", "runtime_smoke", "runtime_compare"]
RepairInstructionStatus = Literal["planned", "applied", "skipped"]
RepairRiskLevel = Literal["low", "medium", "high"]
RepairActionName = Literal["add_package_script", "replace_package_script", "mirror_original_static_entry"]
BuildValidationStage = Literal["building", "typechecking"]
BuildPhase = Literal["install", "build", "typecheck"]
CommandSource = Literal["configured", "npm_script", "fallback_shim", "npm_install", "missing"]
NetworkPolicy = Literal["deny", "allow"]
SandboxResourceEnforcement = Literal[
    "local_best_effort",
    "container_enforced",
    "runtime_isolated",
    "remote_isolated",
]
SandboxRunnerKind = Literal["local", "container", "gvisor", "firecracker", "remote_browser_runner"]
SandboxCapabilityName = Literal["network", "process", "cpu", "memory", "filesystem"]
SandboxCapabilityStatus = Literal["enforced", "best_effort", "unsupported", "unknown"]
DiagnosticCategory = Literal["error", "warning", "message", "suggestion", "unknown"]
DiagnosticSource = Literal["stdout", "stderr"]
DiagnosticTool = Literal["tsc", "vite", "esbuild", "unknown"]
RuntimeWaitForKind = Literal["load_state", "selector", "timeout"]
RuntimeLoadState = Literal["load", "domcontentloaded", "networkidle"]
RuntimeInteractionAction = Literal["click", "fill", "press", "wait"]
RuntimeAssertionKind = Literal["selector_visible", "text_contains", "url_contains"]
BrowserRunStatus = Literal["queued", "running", "pass", "fail", "best_effort"]
BrowserRunnerHealthStatus = Literal["ok", "degraded"]
BrowserRunnerAlertSeverity = Literal["warning", "critical"]
OpsAlertSeverity = Literal["warning", "critical"]
OpsAlertRuleOperator = Literal["gt", "gte", "lt", "lte", "eq", "neq"]
OpsAlertRuleSource = Literal["default", "env"]
OpsAlertEventStatus = Literal["active", "resolved"]


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


class CancelJobRequest(ContractModel):
    reason: str = Field(default="user requested cancellation")


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
    run_attempt: int = Field(ge=0)
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
    expires_at: str | None = None
    deleted_at: str | None = None
    deletion_reason: str | None = None


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


class TypeScriptRelatedInformation(ContractModel):
    message: str
    file_path: str | None = None
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    code: str | None = None


class TypeScriptDiagnostic(ContractModel):
    source: DiagnosticSource
    tool: DiagnosticTool = "tsc"
    category: DiagnosticCategory
    code: str | None = None
    message: str
    file_path: str | None = None
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    context_lines: list[str] = Field(default_factory=list)
    related_information: list[TypeScriptRelatedInformation] = Field(default_factory=list)


class SandboxRuntimeCapability(ContractModel):
    name: SandboxCapabilityName
    status: SandboxCapabilityStatus
    detail: str


class SandboxResourcePolicy(ContractModel):
    process_limit: int | None = Field(default=None, ge=1)
    cpu_time_limit_ms: int | None = Field(default=None, ge=1)
    memory_limit_bytes: int | None = Field(default=None, ge=1)
    enforcement: SandboxResourceEnforcement
    runner_kind: SandboxRunnerKind = "local"
    runtime_name: str | None = None
    runtime_version: str | None = None
    host_platform: str = "unknown"
    capabilities: list[SandboxRuntimeCapability] = Field(default_factory=list)
    limitations: list[str]


class BuildArtifact(ContractModel):
    id: str
    job_id: str
    stage: BuildValidationStage
    review_type: ReviewType
    phase: BuildPhase
    attempt: int = Field(ge=0)
    status: RunStatus
    decision: str
    command: list[str]
    command_source: CommandSource
    script_name: str | None = None
    package_manager: str | None = None
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    failure_class: FailureClass
    timed_out: bool
    output_truncated: bool
    working_directory: str | None = None
    network_policy: NetworkPolicy
    resource_policy: SandboxResourcePolicy
    diagnostics: list[TypeScriptDiagnostic]
    logs_artifact_id: str | None = None
    repair_instruction_ids: list[str]
    limitations: list[str]


class RuntimeWaitFor(ContractModel):
    kind: RuntimeWaitForKind
    selector: str | None = None
    state: RuntimeLoadState | None = None
    timeout_ms: int | None = Field(default=None, ge=1)


class RuntimeInteraction(ContractModel):
    action: RuntimeInteractionAction
    selector: str | None = None
    value: str | None = None
    key: str | None = None
    timeout_ms: int | None = Field(default=None, ge=1)


class RuntimeAssertion(ContractModel):
    kind: RuntimeAssertionKind
    selector: str | None = None
    text: str | None = None
    value: str | None = None


class RuntimeViewport(ContractModel):
    name: str | None = None
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class RuntimeScenario(ContractModel):
    id: str
    job_id: str
    name: str
    entry_url: str | None = None
    viewport: RuntimeViewport | None = None
    wait_for: list[RuntimeWaitFor] = Field(default_factory=list)
    interactions: list[RuntimeInteraction] = Field(default_factory=list)
    assertions: list[RuntimeAssertion] = Field(default_factory=list)
    network_policy: NetworkPolicy = "deny"
    timeout_ms: int = Field(default=10_000, ge=1)


class RuntimeCaptureSummary(ContractModel):
    target: RuntimeTarget
    entry_url: str
    status: RunStatus
    failure_class: FailureClass
    console_messages: list[str] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    page_errors: list[str] = Field(default_factory=list)
    failed_requests: list[str] = Field(default_factory=list)
    responses: list[str] = Field(default_factory=list)
    assertion_failures: list[str] = Field(default_factory=list)
    dom_summary: dict[str, Any] = Field(default_factory=dict)
    screenshot_artifact_id: str | None = None
    duration_ms: int = Field(ge=0)
    limitations: list[str] = Field(default_factory=list)


class RuntimeScreenshotDiff(ContractModel):
    changed: bool | None = None
    original_hash: str | None = None
    reconstructed_hash: str | None = None
    original_size_bytes: int | None = Field(default=None, ge=0)
    reconstructed_size_bytes: int | None = Field(default=None, ge=0)
    original_format: str | None = None
    reconstructed_format: str | None = None
    pixel_diff_status: Literal["compared", "unavailable"]
    pixel_count: int | None = Field(default=None, ge=0)
    changed_pixel_count: int | None = Field(default=None, ge=0)
    changed_pixel_ratio: float | None = Field(default=None, ge=0)
    threshold: int | None = Field(default=None, ge=0)
    threshold_mode: str | None = None
    max_changed_pixel_ratio: float | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    diff_artifact_id: str | None = None
    reason: str | None = None


class RuntimeDomDifference(ContractModel):
    path: str
    original: Any
    reconstructed: Any
    summary: str


class RuntimeCollectionDiff(ContractModel):
    changed: bool
    original_count: int = Field(ge=0)
    reconstructed_count: int = Field(ge=0)
    shared: list[str] = Field(default_factory=list)
    original_only: list[str] = Field(default_factory=list)
    reconstructed_only: list[str] = Field(default_factory=list)
    groups: dict[str, list[str]] = Field(default_factory=dict)


class RuntimeComparisonScope(ContractModel):
    scenario_name: str
    network_policy: NetworkPolicy
    timeout_ms: int = Field(ge=1)
    viewport: RuntimeViewport | None = None


class RuntimeDifferenceSet(ContractModel):
    screenshot_changed: bool | None = None
    dom_changed: bool
    network_changed: bool
    console_changed: bool
    original_only_requests: list[str]
    reconstructed_only_requests: list[str]
    original_only_console: list[str]
    reconstructed_only_console: list[str]
    changed_dom_fields: list[str]
    screenshot_diff: RuntimeScreenshotDiff
    dom_differences: list[RuntimeDomDifference]
    network_diff: RuntimeCollectionDiff
    console_diff: RuntimeCollectionDiff
    comparison_scope: RuntimeComparisonScope


class RuntimeComparisonReport(ContractModel):
    id: str
    job_id: str
    attempt: int = Field(ge=0)
    status: RunStatus
    scenario_artifact_id: str
    original: RuntimeCaptureSummary
    reconstructed: RuntimeCaptureSummary
    differences: RuntimeDifferenceSet
    screenshot_artifact_ids: list[str]
    trace_artifact_ids: list[str]
    limitations: list[str]


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


class BrowserRunSourceArchive(ContractModel):
    content_base64: str
    entry_path: str


class BrowserRunRequest(ContractModel):
    job_id: str
    target: RuntimeTarget
    attempt: int = Field(ge=0)
    entry_url: str
    timeout_ms: int = Field(ge=1)
    wait_for_selector: str | None = None
    scenario: RuntimeScenario | None = None
    network_policy: NetworkPolicy = "deny"
    viewport: RuntimeViewport | None = None
    source_archive: BrowserRunSourceArchive | None = None


class BrowserRunResult(ContractModel):
    status: RunStatus
    failure_class: FailureClass
    console_messages: list[str] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    page_errors: list[str] = Field(default_factory=list)
    failed_requests: list[str] = Field(default_factory=list)
    responses: list[str] = Field(default_factory=list)
    assertion_failures: list[str] = Field(default_factory=list)
    dom_summary: dict[str, Any] = Field(default_factory=dict)
    screenshot_base64: str | None = None
    limitations: list[str] = Field(default_factory=list)
    execution_boundary: dict[str, Any] = Field(default_factory=dict)


class BrowserRunSummary(ContractModel):
    id: str
    status: BrowserRunStatus
    result: BrowserRunResult | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    next_run_at: str | None = None
    worker_id: str | None = None
    queue_backend: str | None = None
    lease_recovered: bool = False


class BrowserRunnerQueueMetrics(ContractModel):
    checked_at: str
    queue_backend: str
    backend_status: BrowserRunnerHealthStatus
    backend_error: str | None = None
    queued_count: int = Field(ge=0)
    running_count: int = Field(ge=0)
    terminal_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    oldest_queued_age_ms: int | None = Field(default=None, ge=0)
    claim_latency_ms: int | None = Field(default=None, ge=0)
    average_run_duration_ms: int | None = Field(default=None, ge=0)
    retry_rate: float = Field(ge=0)
    lease_recovery_count: int = Field(ge=0)
    expired_running_count: int = Field(ge=0)


class BrowserRunnerQueueAlert(ContractModel):
    code: str
    severity: BrowserRunnerAlertSeverity
    message: str
    field: str
    value: Any = None
    threshold: Any = None


class BrowserRunnerQueueHealth(ContractModel):
    status: BrowserRunnerHealthStatus
    service_role: str = "browser-runner"
    deployment_profile: str | None = None
    worker_id: str
    max_workers: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    lease_seconds: int = Field(ge=1)
    retry_backoff_seconds: float = Field(ge=0)
    poll_seconds: float = Field(ge=0)
    metrics: BrowserRunnerQueueMetrics
    alerts: list[BrowserRunnerQueueAlert]


class OpsAlert(ContractModel):
    code: str
    severity: OpsAlertSeverity
    message: str
    field: str
    value: Any = None
    threshold: Any = None
    service_role: str | None = None
    instance_id: str | None = None
    checked_at: str | None = None


class OpsHeartbeatRequest(ContractModel):
    service_role: str
    instance_id: str
    status: BrowserRunnerHealthStatus = "ok"
    ttl_seconds: int = Field(default=90, ge=1)
    metrics: dict[str, Any] = Field(default_factory=dict)
    alerts: list[OpsAlert] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    checked_at: str | None = None


class OpsHeartbeatRecord(ContractModel):
    service_role: str
    instance_id: str
    status: BrowserRunnerHealthStatus
    checked_at: str
    expires_at: str
    metrics: dict[str, Any]
    alerts: list[OpsAlert]
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class OpsMetricsSnapshot(ContractModel):
    checked_at: str
    service_role: str
    deployment_profile: str | None = None
    job_status_counts: dict[str, int] = Field(default_factory=dict)
    active_heartbeat_count: int = Field(ge=0)
    stale_heartbeat_count: int = Field(ge=0)
    service_heartbeat_counts: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    alerts: list[OpsAlert] = Field(default_factory=list)


class OpsAlertDelivery(ContractModel):
    status: Literal["not_configured", "delivered", "failed"]
    attempted: bool
    webhook_url_configured: bool
    event_id: str | None = None
    delivered_at: str | None = None
    error: str | None = None


class OpsAlertRule(ContractModel):
    code: str
    severity: OpsAlertSeverity
    metric_path: str
    operator: OpsAlertRuleOperator
    threshold: Any
    message: str
    service_role: str | None = None
    enabled: bool = True
    source: OpsAlertRuleSource = "default"


class OpsAlertEvent(ContractModel):
    id: str
    checked_at: str
    status: OpsAlertEventStatus = "active"
    severity: OpsAlertSeverity
    code: str
    message: str
    field: str
    value: Any = None
    threshold: Any = None
    service_role: str | None = None
    instance_id: str | None = None
    rule: OpsAlertRule | None = None
    alerts: list[OpsAlert] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    delivery: OpsAlertDelivery
    created_at: str
    updated_at: str


class OpsAlertResponse(ContractModel):
    checked_at: str
    alerts: list[OpsAlert]
    delivery: OpsAlertDelivery
    events: list[OpsAlertEvent] = Field(default_factory=list)


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


class ToolRegistryEntry(ContractModel):
    id: str
    job_id: str
    tool_name: str
    tool_version: str
    category: ToolRegistryCategory
    caller: str
    input_artifact_kinds: list[ArtifactKind]
    output_artifact_kinds: list[ArtifactKind]
    failure_classes: list[FailureClass]
    description: str


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


class RuntimeDiagnosis(ContractModel):
    id: str
    job_id: str
    attempt: int = Field(ge=0)
    agent_name: str
    target_stage: str
    status: RunStatus
    failure_class: FailureClass
    input_artifact_ids: list[str]
    evidence_refs: list[EvidenceRef]
    diagnosis: str
    recommended_actions: list[str]
    confidence: float = Field(ge=0, le=1)
    uncertainty_reasons: list[str]


class ReportSectionDetail(ContractModel):
    label: str
    value: str
    status: RunStatus = "best_effort"
    details: dict[str, Any] = Field(default_factory=dict)


class ReportSection(ContractModel):
    id: str
    job_id: str
    agent_name: str
    title: str
    anchor: str
    summary: str
    content: str
    input_artifact_ids: list[str]
    evidence_refs: list[EvidenceRef]
    status: RunStatus
    confidence: float = Field(ge=0, le=1)
    uncertainty_reasons: list[str]
    details: list[ReportSectionDetail] = Field(default_factory=list)


class RetentionCleanupRequest(ContractModel):
    dry_run: bool = True
    categories: list[RetentionCategory] = Field(default_factory=list)
    retention_classes: list[RetentionClass] = Field(default_factory=list)
    delete_expired: bool = True
    reason: str = "retention cleanup"
    now: str | None = None


class RetentionCleanupItem(ContractModel):
    artifact_id: str
    kind: ArtifactKind
    category: RetentionCategory
    retention_class: RetentionClass
    storage_uri: str
    deleted: bool
    reason: str
    error: str | None = None


class RetentionCleanupResult(ContractModel):
    job_id: str
    dry_run: bool
    requested_at: str
    candidate_count: int = Field(ge=0)
    deleted_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    items: list[RetentionCleanupItem]
    errors: list[str]


class RepairAction(ContractModel):
    action: RepairActionName
    path: str
    value: str
    reason: str


class RepairInstruction(ContractModel):
    id: str
    job_id: str
    attempt: int = Field(ge=0)
    target_stage: RepairTargetStage
    failure_class: FailureClass
    input_artifact_ids: list[str]
    evidence_refs: list[EvidenceRef]
    actions: list[RepairAction]
    status: RepairInstructionStatus
    risk_level: RepairRiskLevel
    decision: str


class AuditRecordCollection(ContractModel):
    job_id: str
    inference_records: list[InferenceRecord]
    review_runs: list[ReviewRun]
    tool_calls: list[ToolCall]


class JobSummary(ContractModel):
    job: JobRecord
    artifacts: list[ArtifactRecord]
