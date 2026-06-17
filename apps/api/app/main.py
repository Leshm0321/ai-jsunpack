from __future__ import annotations

from copy import deepcopy
import os

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from .models import (
    ArtifactRecord,
    CreateJobRequest,
    InferenceRecord,
    JobRecord,
    JobSummary,
    ReviewRun,
    RuntimeValidationRun,
    ToolCall,
)
from .store import store

app = FastAPI(title="AI JS Unpack API", version="0.1.0")

DEFAULT_CORS_ORIGINS = ["http://127.0.0.1:5173", "http://localhost:5173"]
CORS_ORIGINS_ENV = "AI_JSUNPACK_CORS_ORIGINS"
LOCAL_USER_ID = "local-user"
LOCAL_PROJECT_ID = "default"


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
    return {"status": "ok"}


@app.post("/jobs", response_model=JobSummary)
def create_job(
    request: CreateJobRequest,
    x_ai_jsunpack_user_id: str | None = Header(default=None),
    x_ai_jsunpack_project_id: str | None = Header(default=None),
) -> JobSummary:
    access = access_context(x_ai_jsunpack_user_id, x_ai_jsunpack_project_id)
    require_create_access(request, access)
    job = store.create_job(request)
    return JobSummary(job=job, artifacts=[])


@app.post("/jobs/{job_id}/upload", response_model=JobSummary)
async def upload_source(
    job_id: str,
    file: UploadFile = File(...),
    x_ai_jsunpack_user_id: str | None = Header(default=None),
    x_ai_jsunpack_project_id: str | None = Header(default=None),
) -> JobSummary:
    job = require_job(job_id, access_context(x_ai_jsunpack_user_id, x_ai_jsunpack_project_id))

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
    x_ai_jsunpack_user_id: str | None = Header(default=None),
    x_ai_jsunpack_project_id: str | None = Header(default=None),
) -> JobSummary:
    job = require_job(job_id, access_context(x_ai_jsunpack_user_id, x_ai_jsunpack_project_id))
    return JobSummary(job=job, artifacts=store.list_artifacts(job_id))


@app.get("/jobs/{job_id}/runtime-validations", response_model=list[RuntimeValidationRun])
def list_runtime_validations(
    job_id: str,
    x_ai_jsunpack_user_id: str | None = Header(default=None),
    x_ai_jsunpack_project_id: str | None = Header(default=None),
) -> list[RuntimeValidationRun]:
    require_job(job_id, access_context(x_ai_jsunpack_user_id, x_ai_jsunpack_project_id))
    artifacts = store.list_artifacts(job_id, kind="runtime_validation")
    return [runtime_validation_from_artifact(job_id, artifact) for artifact in artifacts]


@app.get("/jobs/{job_id}/inference-records", response_model=list[InferenceRecord])
def list_inference_records(
    job_id: str,
    x_ai_jsunpack_user_id: str | None = Header(default=None),
    x_ai_jsunpack_project_id: str | None = Header(default=None),
) -> list[InferenceRecord]:
    require_job(job_id, access_context(x_ai_jsunpack_user_id, x_ai_jsunpack_project_id))
    artifacts = store.list_artifacts(job_id, kind="inference_record")
    return [inference_record_from_artifact(job_id, artifact) for artifact in artifacts]


@app.get("/jobs/{job_id}/review-runs", response_model=list[ReviewRun])
def list_review_runs(
    job_id: str,
    x_ai_jsunpack_user_id: str | None = Header(default=None),
    x_ai_jsunpack_project_id: str | None = Header(default=None),
) -> list[ReviewRun]:
    require_job(job_id, access_context(x_ai_jsunpack_user_id, x_ai_jsunpack_project_id))
    artifacts = store.list_artifacts(job_id, kind="review_run")
    return [review_run_from_artifact(job_id, artifact) for artifact in artifacts]


@app.get("/jobs/{job_id}/tool-calls", response_model=list[ToolCall])
def list_tool_calls(
    job_id: str,
    x_ai_jsunpack_user_id: str | None = Header(default=None),
    x_ai_jsunpack_project_id: str | None = Header(default=None),
) -> list[ToolCall]:
    require_job(job_id, access_context(x_ai_jsunpack_user_id, x_ai_jsunpack_project_id))
    artifacts = store.list_artifacts(job_id, kind="tool_call")
    return [tool_call_from_artifact(job_id, artifact) for artifact in artifacts]


@app.get("/jobs/{job_id}/runtime-validations/latest", response_model=RuntimeValidationRun)
def get_latest_runtime_validation(
    job_id: str,
    x_ai_jsunpack_user_id: str | None = Header(default=None),
    x_ai_jsunpack_project_id: str | None = Header(default=None),
) -> RuntimeValidationRun:
    require_job(job_id, access_context(x_ai_jsunpack_user_id, x_ai_jsunpack_project_id))
    artifacts = store.list_artifacts(job_id, kind="runtime_validation")
    validations = [runtime_validation_from_artifact(job_id, artifact) for artifact in artifacts]
    if not validations:
        raise HTTPException(status_code=404, detail="Runtime validation not found")
    return validations[-1]


@app.get("/jobs/{job_id}/reports/audit")
def download_latest_audit_report(
    job_id: str,
    x_ai_jsunpack_user_id: str | None = Header(default=None),
    x_ai_jsunpack_project_id: str | None = Header(default=None),
) -> FileResponse:
    artifact = latest_artifact_or_404(
        job_id,
        "audit_report",
        "Audit report not found",
        access_context(x_ai_jsunpack_user_id, x_ai_jsunpack_project_id),
    )
    return file_response_for_artifact(artifact, filename="audit-report.md")


@app.get("/jobs/{job_id}/result-package")
def download_latest_result_package(
    job_id: str,
    x_ai_jsunpack_user_id: str | None = Header(default=None),
    x_ai_jsunpack_project_id: str | None = Header(default=None),
) -> FileResponse:
    artifact = latest_artifact_or_404(
        job_id,
        "result_package",
        "Result package not found",
        access_context(x_ai_jsunpack_user_id, x_ai_jsunpack_project_id),
    )
    return file_response_for_artifact(artifact, filename="result-package.zip")


@app.post("/jobs/{job_id}/rerun", response_model=JobSummary)
def rerun_job(
    job_id: str,
    x_ai_jsunpack_user_id: str | None = Header(default=None),
    x_ai_jsunpack_project_id: str | None = Header(default=None),
) -> JobSummary:
    source_job = require_job(job_id, access_context(x_ai_jsunpack_user_id, x_ai_jsunpack_project_id))
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


@app.get("/jobs/{job_id}/artifacts/{artifact_id}/download")
def download_artifact(
    job_id: str,
    artifact_id: str,
    x_ai_jsunpack_user_id: str | None = Header(default=None),
    x_ai_jsunpack_project_id: str | None = Header(default=None),
) -> FileResponse:
    require_job(job_id, access_context(x_ai_jsunpack_user_id, x_ai_jsunpack_project_id))
    artifact = store.get_artifact(job_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return file_response_for_artifact(artifact)


def latest_artifact_or_404(job_id: str, kind: str, detail: str, access: dict[str, str]) -> ArtifactRecord:
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


def access_context(user_id: str | None, project_id: str | None) -> dict[str, str]:
    return {
        "user_id": normalize_header_value(user_id) or LOCAL_USER_ID,
        "project_id": normalize_header_value(project_id) or "",
    }


def normalize_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def require_create_access(request: CreateJobRequest, access: dict[str, str]) -> None:
    if request.owner_id != access["user_id"]:
        raise HTTPException(status_code=403, detail="Caller is not allowed to create jobs for this owner")
    if access["project_id"] and request.project_id != access["project_id"]:
        raise HTTPException(status_code=403, detail="Caller is not allowed to create jobs for this project")


def require_job(job_id: str, access: dict[str, str]) -> JobRecord:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    project_id = access["project_id"] or LOCAL_PROJECT_ID
    if job.owner_id != access["user_id"] or job.project_id != project_id:
        raise HTTPException(status_code=403, detail="Caller is not allowed to access this job")
    return job


def runtime_validation_from_artifact(job_id: str, artifact: ArtifactRecord) -> RuntimeValidationRun:
    try:
        return RuntimeValidationRun.model_validate_json(store.read_artifact(job_id, artifact.id))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Invalid runtime validation artifact: {artifact.id}") from error


def inference_record_from_artifact(job_id: str, artifact: ArtifactRecord) -> InferenceRecord:
    try:
        return InferenceRecord.model_validate_json(store.read_artifact(job_id, artifact.id))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Invalid inference record artifact: {artifact.id}") from error


def review_run_from_artifact(job_id: str, artifact: ArtifactRecord) -> ReviewRun:
    try:
        return ReviewRun.model_validate_json(store.read_artifact(job_id, artifact.id))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Invalid review run artifact: {artifact.id}") from error


def tool_call_from_artifact(job_id: str, artifact: ArtifactRecord) -> ToolCall:
    try:
        return ToolCall.model_validate_json(store.read_artifact(job_id, artifact.id))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Invalid tool call artifact: {artifact.id}") from error
