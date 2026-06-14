from __future__ import annotations

import hashlib
from pathlib import Path

from .models import ArtifactRecord, CreateJobRequest, JobRecord, utc_now


class InMemoryStore:
    def __init__(self, artifact_root: Path | None = None) -> None:
        self.jobs: dict[str, JobRecord] = {}
        self.artifacts: dict[str, ArtifactRecord] = {}
        self.artifact_root = artifact_root or Path("artifacts")
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    def create_job(self, request: CreateJobRequest) -> JobRecord:
        job = JobRecord(
            owner_id=request.owner_id,
            project_id=request.project_id,
            cloud_mode=request.cloud_mode,
            config=request.config,
        )
        self.jobs[job.id] = job
        return job

    def get_job(self, job_id: str) -> JobRecord | None:
        return self.jobs.get(job_id)

    def update_status(self, job_id: str, status: str, failure_reason: str | None = None) -> JobRecord:
        job = self.jobs[job_id]
        job.status = status  # type: ignore[assignment]
        job.updated_at = utc_now()
        if failure_reason:
            job.failure_reason = failure_reason
        self.jobs[job_id] = job
        return job

    def list_artifacts(self, job_id: str) -> list[ArtifactRecord]:
        return [artifact for artifact in self.artifacts.values() if artifact.job_id == job_id]

    def write_artifact(
        self,
        job_id: str,
        *,
        kind: str,
        stage: str,
        filename: str,
        content: bytes,
        content_type: str,
        producer: str,
        parent_artifact_ids: list[str] | None = None,
    ) -> ArtifactRecord:
        job_dir = self.artifact_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        target = job_dir / filename
        target.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        artifact = ArtifactRecord(
            job_id=job_id,
            kind=kind,
            stage=stage,  # type: ignore[arg-type]
            content_type=content_type,
            hash=digest,
            size=len(content),
            storage_uri=str(target),
            parent_artifact_ids=parent_artifact_ids or [],
            producer=producer,
        )
        self.artifacts[artifact.id] = artifact
        job = self.jobs[job_id]
        if kind == "input_inventory" and not job.input_artifact_id:
            job.input_artifact_id = artifact.id
            job.updated_at = utc_now()
        return artifact


store = InMemoryStore()

