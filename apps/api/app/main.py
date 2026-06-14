from __future__ import annotations

import os

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .models import CreateJobRequest, JobSummary
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
