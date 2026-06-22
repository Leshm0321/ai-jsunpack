from __future__ import annotations

from copy import deepcopy
import json
import os
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from packages.deployment import DeploymentConfigurationError, validate_current_environment

from .auth import AccessContext, ProjectRole, SERVICE_ROLE_WORKER, require_access
from .models import (
    ArtifactRecord,
    AuditRecordCollection,
    CancelJobRequest,
    CreateJobRequest,
    OpsAlert,
    OpsAlertDelivery,
    OpsAlertResponse,
    OpsHeartbeatRecord,
    OpsHeartbeatRequest,
    OpsMetricsSnapshot,
    InferenceRecord,
    JobRecord,
    JobSummary,
    RetentionCleanupRequest,
    RetentionCleanupResult,
    ReviewRun,
    RuntimeValidationRun,
    ToolCall,
)
from .store import store

REPORT_ARTIFACT_KINDS = ("audit_report", "html_report", "evidence_index")
REPORT_KIND_ALIASES = {
    "audit": "audit_report",
    "audit-report": "audit_report",
    "audit_report": "audit_report",
    "html": "html_report",
    "html-report": "html_report",
    "html_report": "html_report",
    "evidence-index": "evidence_index",
    "evidence_index": "evidence_index",
}
REPORT_DOWNLOAD_FILENAMES = {
    "audit_report": "audit-report.md",
    "html_report": "audit-report.html",
    "evidence_index": "evidence-index.json",
}
AUDIT_RECORD_CATEGORIES = ("all", "inference", "review", "tool")
OPS_WEBHOOK_URL_ENV = "AI_JSUNPACK_ALERT_WEBHOOK_URL"
OPS_WEBHOOK_TIMEOUT_SECONDS_ENV = "AI_JSUNPACK_ALERT_WEBHOOK_TIMEOUT_SECONDS"
OPS_HEARTBEAT_TTL_SECONDS_ENV = "AI_JSUNPACK_OPS_HEARTBEAT_TTL_SECONDS"
OPS_INSTANCE_ID_ENV = "AI_JSUNPACK_INSTANCE_ID"

try:
    DEPLOYMENT_PROFILE = validate_current_environment("api")
except DeploymentConfigurationError as error:
    raise RuntimeError(str(error)) from error

app = FastAPI(title="AI JS Unpack API", version="0.1.0")

DEFAULT_CORS_ORIGINS = ["http://127.0.0.1:5173", "http://localhost:5173"]
CORS_ORIGINS_ENV = "AI_JSUNPACK_CORS_ORIGINS"


def configured_cors_origins() -> list[str]:
    configured = os.getenv(CORS_ORIGINS_ENV)
    if not configured:
        return DEFAULT_CORS_ORIGINS
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "serviceRole": "api", "deploymentProfile": DEPLOYMENT_PROFILE.status}


@app.post("/ops/heartbeats", response_model=OpsHeartbeatRecord)
def record_ops_heartbeat(
    request: OpsHeartbeatRequest,
    access: AccessContext = Depends(require_access),
) -> OpsHeartbeatRecord:
    require_ops_service(access)
    heartbeat = store.record_ops_heartbeat(request)
    return heartbeat


@app.get("/ops/heartbeats", response_model=list[OpsHeartbeatRecord])
def list_ops_heartbeats(
    service_role: str | None = None,
    active_only: bool = False,
    access: AccessContext = Depends(require_access),
) -> list[OpsHeartbeatRecord]:
    require_ops_read_access(access)
    refresh_api_heartbeat()
    return store.list_ops_heartbeats(service_role=service_role, include_stale=not active_only)


@app.get("/ops/metrics", response_model=OpsMetricsSnapshot)
def ops_metrics(access: AccessContext = Depends(require_access)) -> OpsMetricsSnapshot:
    require_ops_read_access(access)
    return build_ops_metrics_snapshot()


@app.get("/ops/alerts", response_model=OpsAlertResponse)
def ops_alerts(access: AccessContext = Depends(require_access)) -> OpsAlertResponse:
    require_ops_read_access(access)
    snapshot = build_ops_metrics_snapshot()
    alerts = snapshot.alerts
    delivery = deliver_ops_alerts(snapshot.checked_at, alerts, snapshot.metrics)
    return OpsAlertResponse(checked_at=snapshot.checked_at, alerts=alerts, delivery=delivery)


@app.post("/jobs", response_model=JobSummary)
def create_job(
    request: CreateJobRequest,
    access: AccessContext = Depends(require_access),
) -> JobSummary:
    require_create_access(request, access)
    job = store.create_job(request)
    return JobSummary(job=job, artifacts=[])


@app.post("/jobs/{job_id}/upload", response_model=JobSummary)
async def upload_source(
    job_id: str,
    file: UploadFile = File(...),
    access: AccessContext = Depends(require_access),
) -> JobSummary:
    job = require_job(job_id, access, minimum_role="maintainer")

    content = await file.read()
    store.write_artifact(
        job_id,
        kind="source_input",
        stage="intake",
        filename=file.filename or "input.bin",
        content=content,
        content_type=file.content_type or "application/octet-stream",
        producer="api.upload",
    )
    job = store.update_status(job_id, "intake")
    return JobSummary(job=job, artifacts=store.list_artifacts(job_id))


@app.get("/jobs/{job_id}", response_model=JobSummary)
def get_job(
    job_id: str,
    access: AccessContext = Depends(require_access),
) -> JobSummary:
    job = require_job(job_id, access)
    return JobSummary(job=job, artifacts=store.list_artifacts(job_id))


@app.get("/jobs/{job_id}/runtime-validations", response_model=list[RuntimeValidationRun])
def list_runtime_validations(
    job_id: str,
    access: AccessContext = Depends(require_access),
) -> list[RuntimeValidationRun]:
    require_job(job_id, access)
    artifacts = store.list_artifacts(job_id, kind="runtime_validation")
    return [runtime_validation_from_artifact(job_id, artifact) for artifact in artifacts]


@app.get("/jobs/{job_id}/inference-records", response_model=list[InferenceRecord])
def list_inference_records(
    job_id: str,
    access: AccessContext = Depends(require_access),
) -> list[InferenceRecord]:
    require_job(job_id, access)
    return list_inference_record_payloads(job_id)


@app.get("/jobs/{job_id}/review-runs", response_model=list[ReviewRun])
def list_review_runs(
    job_id: str,
    access: AccessContext = Depends(require_access),
) -> list[ReviewRun]:
    require_job(job_id, access)
    return list_review_run_payloads(job_id)


@app.get("/jobs/{job_id}/tool-calls", response_model=list[ToolCall])
def list_tool_calls(
    job_id: str,
    access: AccessContext = Depends(require_access),
) -> list[ToolCall]:
    require_job(job_id, access)
    return list_tool_call_payloads(job_id)


@app.get("/jobs/{job_id}/runtime-validations/latest", response_model=RuntimeValidationRun)
def get_latest_runtime_validation(
    job_id: str,
    access: AccessContext = Depends(require_access),
) -> RuntimeValidationRun:
    require_job(job_id, access)
    artifacts = store.list_artifacts(job_id, kind="runtime_validation")
    validations = [runtime_validation_from_artifact(job_id, artifact) for artifact in artifacts]
    if not validations:
        raise HTTPException(status_code=404, detail="Runtime validation not found")
    return validations[-1]


@app.get("/jobs/{job_id}/audit-records", response_model=AuditRecordCollection)
def list_audit_records(
    job_id: str,
    category: str = "all",
    access: AccessContext = Depends(require_access),
) -> AuditRecordCollection:
    require_job(job_id, access)
    normalized_category = normalize_audit_record_category(category)
    return AuditRecordCollection(
        job_id=job_id,
        inference_records=list_inference_record_payloads(job_id) if normalized_category in ("all", "inference") else [],
        review_runs=list_review_run_payloads(job_id) if normalized_category in ("all", "review") else [],
        tool_calls=list_tool_call_payloads(job_id) if normalized_category in ("all", "tool") else [],
    )


@app.get("/jobs/{job_id}/reports", response_model=list[ArtifactRecord])
def list_reports(
    job_id: str,
    kind: str | None = None,
    access: AccessContext = Depends(require_access),
) -> list[ArtifactRecord]:
    require_job(job_id, access)
    if kind is not None:
        return store.list_artifacts(job_id, kind=normalize_report_kind(kind))
    artifacts: list[ArtifactRecord] = []
    for report_kind in REPORT_ARTIFACT_KINDS:
        artifacts.extend(store.list_artifacts(job_id, kind=report_kind))
    return sorted(artifacts, key=lambda artifact: (artifact.created_at, artifact.id))


@app.get("/jobs/{job_id}/reports/audit")
def download_latest_audit_report(
    job_id: str,
    access: AccessContext = Depends(require_access),
) -> Response:
    return download_latest_report(job_id, "audit_report", access)


@app.get("/jobs/{job_id}/reports/{report_kind}")
def download_latest_report(
    job_id: str,
    report_kind: str,
    access: AccessContext = Depends(require_access),
) -> Response:
    require_job(job_id, access)
    normalized_kind = normalize_report_kind(report_kind)
    artifact = latest_artifact_or_404(
        job_id,
        normalized_kind,
        f"{report_kind} report not found",
        access,
    )
    return file_response_for_artifact(artifact, filename=REPORT_DOWNLOAD_FILENAMES[normalized_kind])


@app.get("/jobs/{job_id}/result-package")
def download_latest_result_package(
    job_id: str,
    access: AccessContext = Depends(require_access),
) -> FileResponse:
    artifact = latest_artifact_or_404(
        job_id,
        "result_package",
        "Result package not found",
        access,
    )
    return file_response_for_artifact(artifact, filename="result-package.zip")


@app.post("/jobs/{job_id}/rerun", response_model=JobSummary)
def rerun_job(
    job_id: str,
    access: AccessContext = Depends(require_access),
) -> JobSummary:
    source_job = require_job(job_id, access, minimum_role="maintainer")
    if source_job.input_artifact_id is None:
        raise HTTPException(status_code=400, detail="Job has no source input artifact to rerun")

    source_artifact = store.get_artifact(job_id, source_job.input_artifact_id)
    if source_artifact is None:
        raise HTTPException(status_code=404, detail="Source input artifact not found")
    if not store.artifact_exists(source_artifact) or not store.artifact_is_file(source_artifact):
        raise HTTPException(status_code=400, detail="Source input artifact is not a downloadable file")

    rerun_config = deepcopy(source_job.config)
    rerun_config["rerunOfJobId"] = source_job.id
    rerun_config["rerunOfArtifactId"] = source_artifact.id
    rerun_config["source"] = "api-rerun"
    created = store.create_job(
        CreateJobRequest(
            project_id=source_job.project_id,
            owner_id=source_job.owner_id,
            cloud_mode=source_job.cloud_mode,
            config=rerun_config,
        )
    )
    store.write_artifact(
        created.id,
        kind="source_input",
        stage="intake",
        filename=source_filename(source_artifact),
        content=store.read_artifact_record(source_artifact),
        content_type=source_artifact.content_type,
        producer="api.rerun",
    )
    job = store.update_status(created.id, "intake")
    return JobSummary(job=job, artifacts=store.list_artifacts(created.id))


@app.post("/jobs/{job_id}/cancel", response_model=JobSummary)
def cancel_job(
    job_id: str,
    request: CancelJobRequest | None = None,
    access: AccessContext = Depends(require_access),
) -> JobSummary:
    require_job(job_id, access, minimum_role="maintainer")
    cancel_request = request or CancelJobRequest()
    try:
        job = store.request_cancel(job_id, cancel_request.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobSummary(job=job, artifacts=store.list_artifacts(job_id))


@app.post("/jobs/{job_id}/retention/cleanup", response_model=RetentionCleanupResult)
def cleanup_retention(
    job_id: str,
    request: RetentionCleanupRequest,
    access: AccessContext = Depends(require_access),
) -> RetentionCleanupResult:
    require_job(job_id, access, minimum_role="maintainer")
    try:
        return store.cleanup_retention(job_id, request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found")


@app.get("/jobs/{job_id}/artifacts/{artifact_id}/download")
def download_artifact(
    job_id: str,
    artifact_id: str,
    access: AccessContext = Depends(require_access),
) -> FileResponse:
    require_job(job_id, access)
    artifact = store.get_artifact(job_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return file_response_for_artifact(artifact)


def latest_artifact_or_404(job_id: str, kind: str, detail: str, access: AccessContext) -> ArtifactRecord:
    require_job(job_id, access)
    artifacts = store.list_artifacts(job_id, kind=kind)
    if not artifacts:
        raise HTTPException(status_code=404, detail=detail)
    return artifacts[-1]


def file_response_for_artifact(artifact: ArtifactRecord, filename: str | None = None) -> Response:
    if not store.artifact_exists(artifact):
        raise HTTPException(status_code=404, detail="Artifact content not found")
    if store.artifact_is_directory(artifact):
        raise HTTPException(status_code=400, detail="Directory artifact download requires a result package")
    artifact_path = store.artifact_local_path(artifact)
    response_filename = filename or store.artifact_filename(artifact)
    if artifact_path is None:
        return Response(
            content=store.read_artifact_record(artifact),
            media_type=artifact.content_type,
            headers={"Content-Disposition": f'attachment; filename="{response_filename}"'},
        )
    return FileResponse(
        artifact_path,
        media_type=artifact.content_type,
        filename=response_filename,
    )


def source_filename(source_artifact: ArtifactRecord) -> str:
    suffix = store.artifact_suffix(source_artifact)
    return f"rerun-source-input{suffix}" if suffix else "rerun-source-input"


def normalize_report_kind(value: str) -> str:
    normalized = REPORT_KIND_ALIASES.get(value.strip().lower())
    if normalized is None:
        raise HTTPException(status_code=400, detail="Unsupported report kind")
    return normalized


def normalize_audit_record_category(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in AUDIT_RECORD_CATEGORIES:
        raise HTTPException(status_code=400, detail="Unsupported audit record category")
    return normalized


def require_create_access(request: CreateJobRequest, access: AccessContext) -> None:
    require_project_role(access, request.project_id, "maintainer")
    if access.kind == "user" and request.owner_id != access.subject:
        raise HTTPException(status_code=403, detail="Caller is not allowed to create jobs for this owner")


def require_job(job_id: str, access: AccessContext, *, minimum_role: ProjectRole = "viewer") -> JobRecord:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    require_project_role(access, job.project_id, minimum_role)
    return job


def require_project_role(access: AccessContext, project_id: str, minimum_role: ProjectRole) -> None:
    if access.kind == "service" and not access.has_service_role(SERVICE_ROLE_WORKER):
        raise HTTPException(status_code=403, detail="Service credential is not allowed to access this API")
    if not access.has_project_role(project_id, minimum_role):
        raise HTTPException(status_code=403, detail="Caller is not allowed to access this project")


def require_ops_service(access: AccessContext) -> None:
    if access.kind != "service" or not access.has_service_role(SERVICE_ROLE_WORKER):
        raise HTTPException(status_code=403, detail="Ops heartbeat ingestion requires a worker service credential")


def require_ops_read_access(access: AccessContext) -> None:
    if access.kind == "service" and access.has_service_role(SERVICE_ROLE_WORKER):
        return
    if access.kind == "user" and any(role in {"maintainer", "owner"} for role in access.projects.values()):
        return
    raise HTTPException(status_code=403, detail="Caller is not allowed to access ops telemetry")


def refresh_api_heartbeat() -> OpsHeartbeatRecord:
    checked_at = utc_now()
    profile = DEPLOYMENT_PROFILE.status
    metrics = {
        "deploymentProfile": profile,
        "jobStatusCounts": store.job_status_counts(),
        "corsOrigins": configured_cors_origins(),
    }
    alerts: list[OpsAlert] = []
    if profile != "ok":
        alerts.append(
            OpsAlert(
                code="deployment_profile_warning",
                severity="warning",
                message="API deployment profile is not fully ok.",
                field="deploymentProfile",
                value=profile,
                threshold="ok",
                service_role="api",
                instance_id=api_instance_id(),
                checked_at=checked_at,
            )
        )
    request = OpsHeartbeatRequest(
        service_role="api",
        instance_id=api_instance_id(),
        status="ok" if profile == "ok" else "degraded",
        ttl_seconds=parse_ops_heartbeat_ttl_seconds(),
        checked_at=checked_at,
        metrics=metrics,
        alerts=alerts,
        metadata={
            "deploymentProfile": profile,
            "corsOrigins": configured_cors_origins(),
        },
    )
    return store.record_ops_heartbeat(request)


def build_ops_metrics_snapshot() -> OpsMetricsSnapshot:
    api_heartbeat = refresh_api_heartbeat()
    checked_at = api_heartbeat.checked_at
    current_time = parse_timestamp(checked_at)
    heartbeats = store.list_ops_heartbeats(include_stale=True, now=checked_at)
    active_heartbeats = [heartbeat for heartbeat in heartbeats if parse_timestamp(heartbeat.expires_at) > current_time]
    stale_heartbeats = [heartbeat for heartbeat in heartbeats if parse_timestamp(heartbeat.expires_at) <= current_time]
    service_heartbeat_counts: dict[str, int] = {}
    alerts: list[OpsAlert] = []
    for heartbeat in heartbeats:
        service_heartbeat_counts[heartbeat.service_role] = service_heartbeat_counts.get(heartbeat.service_role, 0) + 1
        alerts.extend(heartbeat.alerts)
        if heartbeat.status != "ok":
            alerts.append(
                OpsAlert(
                    code=f"{heartbeat.service_role}_heartbeat_degraded",
                    severity="warning",
                    message=f"{heartbeat.service_role} heartbeat status is {heartbeat.status}.",
                    field="status",
                    value=heartbeat.status,
                    threshold="ok",
                    service_role=heartbeat.service_role,
                    instance_id=heartbeat.instance_id,
                    checked_at=heartbeat.checked_at,
                )
            )
        if parse_timestamp(heartbeat.expires_at) <= current_time:
            alerts.append(
                OpsAlert(
                    code="heartbeat_expired",
                    severity="critical",
                    message=f"{heartbeat.service_role} heartbeat is stale or expired.",
                    field="expiresAt",
                    value=heartbeat.expires_at,
                    threshold=checked_at,
                    service_role=heartbeat.service_role,
                    instance_id=heartbeat.instance_id,
                    checked_at=heartbeat.checked_at,
                )
            )
    metrics = {
        "api": api_heartbeat.metrics,
        "jobStatusCounts": store.job_status_counts(),
        "totalHeartbeatCount": len(heartbeats),
        "activeHeartbeatCount": len(active_heartbeats),
        "staleHeartbeatCount": len(stale_heartbeats),
        "serviceHeartbeatCounts": service_heartbeat_counts,
        "deploymentProfile": DEPLOYMENT_PROFILE.status,
    }
    return OpsMetricsSnapshot(
        checked_at=checked_at,
        service_role="api",
        deployment_profile=DEPLOYMENT_PROFILE.status,
        job_status_counts=store.job_status_counts(),
        active_heartbeat_count=len(active_heartbeats),
        stale_heartbeat_count=len(stale_heartbeats),
        service_heartbeat_counts=service_heartbeat_counts,
        metrics=metrics,
        alerts=alerts,
    )


def deliver_ops_alerts(checked_at: str, alerts: list[OpsAlert], metrics: dict[str, object]) -> OpsAlertDelivery:
    webhook_url = os.getenv(OPS_WEBHOOK_URL_ENV, "").strip()
    if not webhook_url:
        return OpsAlertDelivery(
            status="not_configured",
            attempted=False,
            webhook_url_configured=False,
            error=None,
        )
    payload = {
        "checkedAt": checked_at,
        "alerts": [alert.model_dump(by_alias=True) for alert in alerts],
        "metrics": metrics,
    }
    timeout_seconds = parse_float_env(OPS_WEBHOOK_TIMEOUT_SECONDS_ENV, 2.0)
    request = Request(
        webhook_url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response.read()
    except (URLError, TimeoutError, ValueError) as error:
        return OpsAlertDelivery(
            status="failed",
            attempted=True,
            webhook_url_configured=True,
            error=str(error),
        )
    return OpsAlertDelivery(
        status="delivered",
        attempted=True,
        webhook_url_configured=True,
        error=None,
    )


def api_instance_id() -> str:
    return os.getenv(OPS_INSTANCE_ID_ENV) or f"api-{os.getpid()}"


def parse_ops_heartbeat_ttl_seconds() -> int:
    return parse_int_env(OPS_HEARTBEAT_TTL_SECONDS_ENV, 90)


def parse_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return max(1, parsed)


def parse_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        parsed = float(raw_value)
    except ValueError:
        return default
    return max(0.1, parsed)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def runtime_validation_from_artifact(job_id: str, artifact: ArtifactRecord) -> RuntimeValidationRun:
    try:
        return RuntimeValidationRun.model_validate_json(store.read_artifact(job_id, artifact.id))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Invalid runtime validation artifact: {artifact.id}") from error


def list_inference_record_payloads(job_id: str) -> list[InferenceRecord]:
    artifacts = store.list_artifacts(job_id, kind="inference_record")
    return [inference_record_from_artifact(job_id, artifact) for artifact in artifacts]


def inference_record_from_artifact(job_id: str, artifact: ArtifactRecord) -> InferenceRecord:
    try:
        return InferenceRecord.model_validate_json(store.read_artifact(job_id, artifact.id))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Invalid inference record artifact: {artifact.id}") from error


def list_review_run_payloads(job_id: str) -> list[ReviewRun]:
    artifacts = store.list_artifacts(job_id, kind="review_run")
    return [review_run_from_artifact(job_id, artifact) for artifact in artifacts]


def review_run_from_artifact(job_id: str, artifact: ArtifactRecord) -> ReviewRun:
    try:
        return ReviewRun.model_validate_json(store.read_artifact(job_id, artifact.id))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Invalid review run artifact: {artifact.id}") from error


def list_tool_call_payloads(job_id: str) -> list[ToolCall]:
    artifacts = store.list_artifacts(job_id, kind="tool_call")
    return [tool_call_from_artifact(job_id, artifact) for artifact in artifacts]


def tool_call_from_artifact(job_id: str, artifact: ArtifactRecord) -> ToolCall:
    try:
        return ToolCall.model_validate_json(store.read_artifact(job_id, artifact.id))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Invalid tool call artifact: {artifact.id}") from error
