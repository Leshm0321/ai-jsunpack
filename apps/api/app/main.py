from __future__ import annotations

from fastapi import FastAPI, File, HTTPException, UploadFile

from .models import CreateJobRequest, JobSummary
from .store import store

app = FastAPI(title="AI JS Unpack API", version="0.1.0")


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
        kind="input_inventory",
        stage="intake",
        filename=file.filename or "input.bin",
        content=content,
        content_type=file.content_type or "application/octet-stream",
        producer="api.upload",
    )
    job.input_artifact_id = artifact.id
    store.update_status(job_id, "intake")
    return JobSummary(job=job, artifacts=store.list_artifacts(job_id))


@app.get("/jobs/{job_id}", response_model=JobSummary)
def get_job(job_id: str) -> JobSummary:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobSummary(job=job, artifacts=store.list_artifacts(job_id))

