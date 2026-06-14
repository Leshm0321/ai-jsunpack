from __future__ import annotations

import hashlib
from pathlib import Path

from .models import ArtifactKind, ArtifactRecord, CreateJobRequest, JobRecord, JobStatus, new_artifact_id, new_job_id, utc_now


class InMemoryStore:
    def __init__(self, artifact_root: Path | None = None) -> None:
        self.jobs: dict[str, JobRecord] = {}
        self.artifacts: dict[str, ArtifactRecord] = {}
        self.artifact_root = artifact_root or Path("artifacts")
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    def create_job(self, request: CreateJobRequest) -> JobRecord:
        now = utc_now()
        job = JobRecord(
            id=new_job_id(),
            status="queued",
            owner_id=request.owner_id,
            project_id=request.project_id,
            input_artifact_id=None,
            config=request.config,
            cloud_mode=request.cloud_mode,
            review_attempt=0,
            worker_lease=None,
            failure_class="none",
            failure_reason=None,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job.id] = job
        return job

    def get_job(self, job_id: str) -> JobRecord | None:
        return self.jobs.get(job_id)

    def update_status(self, job_id: str, status: JobStatus, failure_reason: str | None = None) -> JobRecord:
        job = self.jobs[job_id]
        job.status = status
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
        kind: ArtifactKind,
        stage: JobStatus,
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
            id=new_artifact_id(),
            job_id=job_id,
            kind=kind,
            stage=stage,
            attempt=0,
            schema_version="2026-06-14",
            content_type=content_type,
            hash=digest,
            size=len(content),
            storage_uri=str(target),
            parent_artifact_ids=parent_artifact_ids or [],
            producer=producer,
            sensitivity_class="source_sensitive",
            retention_class="project",
            created_at=utc_now(),
        )
        self.artifacts[artifact.id] = artifact
        job = self.jobs[job_id]
        if kind == "input_inventory" and not job.input_artifact_id:
            job.input_artifact_id = artifact.id
            job.updated_at = utc_now()
        return artifact


store = InMemoryStore()
