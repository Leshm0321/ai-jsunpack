from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .models import (
    ArtifactRecord,
    CreateJobRequest,
    InferenceRecord,
    JobSummary,
    ReviewRun,
    RuntimeValidationRun,
    ToolCall,
)
from .store import store

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
    return {"status": "ok"}


@app.post("/jobs", response_model=JobSummary)
def create_job(request: CreateJobRequest) -> JobSummary:
    job = store.create_job(request)
    return JobSummary(job=job, artifacts=[])


@app.post("/jobs/{job_id}/upload", response_model=JobSummary)
async def upload_source(job_id: str, file: UploadFile = File(...)) -> JobSummary:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    content = await file.read()
    artifact = store.write_artifact(
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
def get_job(job_id: str) -> JobSummary:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobSummary(job=job, artifacts=store.list_artifacts(job_id))


@app.get("/jobs/{job_id}/runtime-validations", response_model=list[RuntimeValidationRun])
def list_runtime_validations(job_id: str) -> list[RuntimeValidationRun]:
    require_job(job_id)
    artifacts = store.list_artifacts(job_id, kind="runtime_validation")
    return [runtime_validation_from_artifact(job_id, artifact) for artifact in artifacts]


@app.get("/jobs/{job_id}/inference-records", response_model=list[InferenceRecord])
def list_inference_records(job_id: str) -> list[InferenceRecord]:
    require_job(job_id)
    artifacts = store.list_artifacts(job_id, kind="inference_record")
    return [inference_record_from_artifact(job_id, artifact) for artifact in artifacts]


@app.get("/jobs/{job_id}/review-runs", response_model=list[ReviewRun])
def list_review_runs(job_id: str) -> list[ReviewRun]:
    require_job(job_id)
    artifacts = store.list_artifacts(job_id, kind="review_run")
    return [review_run_from_artifact(job_id, artifact) for artifact in artifacts]


@app.get("/jobs/{job_id}/tool-calls", response_model=list[ToolCall])
def list_tool_calls(job_id: str) -> list[ToolCall]:
    require_job(job_id)
    artifacts = store.list_artifacts(job_id, kind="tool_call")
    return [tool_call_from_artifact(job_id, artifact) for artifact in artifacts]


@app.get("/jobs/{job_id}/runtime-validations/latest", response_model=RuntimeValidationRun)
def get_latest_runtime_validation(job_id: str) -> RuntimeValidationRun:
    validations = list_runtime_validations(job_id)
    if not validations:
        raise HTTPException(status_code=404, detail="Runtime validation not found")
    return validations[-1]


@app.get("/jobs/{job_id}/reports/audit")
def download_latest_audit_report(job_id: str) -> FileResponse:
    artifact = latest_artifact_or_404(job_id, "audit_report", "Audit report not found")
    return file_response_for_artifact(artifact, filename="audit-report.md")


@app.get("/jobs/{job_id}/result-package")
def download_latest_result_package(job_id: str) -> FileResponse:
    artifact = latest_artifact_or_404(job_id, "result_package", "Result package not found")
    return file_response_for_artifact(artifact, filename="result-package.zip")


@app.post("/jobs/{job_id}/rerun", response_model=JobSummary)
def rerun_job(job_id: str) -> JobSummary:
    source_job = store.get_job(job_id)
    if source_job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if source_job.input_artifact_id is None:
        raise HTTPException(status_code=400, detail="Job has no source input artifact to rerun")

    source_artifact = store.get_artifact(job_id, source_job.input_artifact_id)
    if source_artifact is None:
        raise HTTPException(status_code=404, detail="Source input artifact not found")
    source_path = Path(source_artifact.storage_uri)
    if not source_path.exists() or source_path.is_dir():
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
        filename=source_filename(source_path),
        content=source_path.read_bytes(),
        content_type=source_artifact.content_type,
        producer="api.rerun",
    )
    job = store.update_status(created.id, "intake")
    return JobSummary(job=job, artifacts=store.list_artifacts(created.id))


@app.get("/jobs/{job_id}/artifacts/{artifact_id}/download")
def download_artifact(job_id: str, artifact_id: str) -> FileResponse:
    require_job(job_id)
    artifact = store.get_artifact(job_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return file_response_for_artifact(artifact)


def latest_artifact_or_404(job_id: str, kind: str, detail: str) -> ArtifactRecord:
    require_job(job_id)
    artifacts = store.list_artifacts(job_id, kind=kind)
    if not artifacts:
        raise HTTPException(status_code=404, detail=detail)
    return artifacts[-1]


def file_response_for_artifact(artifact: ArtifactRecord, filename: str | None = None) -> FileResponse:
    artifact_path = Path(artifact.storage_uri)
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact content not found")
    if artifact_path.is_dir():
        raise HTTPException(status_code=400, detail="Directory artifact download requires a result package")
    return FileResponse(
        artifact_path,
        media_type=artifact.content_type,
        filename=filename or artifact_path.name,
    )


def source_filename(source_path: Path) -> str:
    suffix = source_path.suffix
    return f"rerun-source-input{suffix}" if suffix else "rerun-source-input"


def require_job(job_id: str) -> None:
    if store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")


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
