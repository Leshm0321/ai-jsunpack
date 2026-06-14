from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .models import ArtifactRecord, CreateJobRequest, JobSummary, RuntimeValidationRun
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


@app.get("/jobs/{job_id}/runtime-validations/latest", response_model=RuntimeValidationRun)
def get_latest_runtime_validation(job_id: str) -> RuntimeValidationRun:
    validations = list_runtime_validations(job_id)
    if not validations:
        raise HTTPException(status_code=404, detail="Runtime validation not found")
    return validations[-1]


@app.get("/jobs/{job_id}/artifacts/{artifact_id}/download")
def download_artifact(job_id: str, artifact_id: str) -> FileResponse:
    require_job(job_id)
    artifact = store.get_artifact(job_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    artifact_path = Path(artifact.storage_uri)
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact content not found")
    return FileResponse(
        artifact_path,
        media_type=artifact.content_type,
        filename=artifact_path.name,
    )


def require_job(job_id: str) -> None:
    if store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")


def runtime_validation_from_artifact(job_id: str, artifact: ArtifactRecord) -> RuntimeValidationRun:
    try:
        return RuntimeValidationRun.model_validate_json(store.read_artifact(job_id, artifact.id))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Invalid runtime validation artifact: {artifact.id}") from error
