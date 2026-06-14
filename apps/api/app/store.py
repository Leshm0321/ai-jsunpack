from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, create_engine, insert, select, update
from sqlalchemy.engine import Engine

from .models import (
    ArtifactKind,
    ArtifactRecord,
    CreateJobRequest,
    FailureClass,
    JobRecord,
    JobStatus,
    new_artifact_id,
    new_job_id,
    utc_now,
)


DATABASE_URL_ENV = "AI_JSUNPACK_DATABASE_URL"
ARTIFACT_ROOT_ENV = "AI_JSUNPACK_ARTIFACT_ROOT"
DEFAULT_DATABASE_URL = "postgresql+psycopg://ai_jsunpack:ai_jsunpack@127.0.0.1:5432/ai_jsunpack"
CONTRACT_SCHEMA_VERSION = "2026-06-14"

metadata = MetaData()

jobs_table = Table(
    "jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("status", String, nullable=False),
    Column("owner_id", String, nullable=False),
    Column("project_id", String, nullable=False),
    Column("input_artifact_id", String, nullable=True),
    Column("config", JSON, nullable=False),
    Column("cloud_mode", String, nullable=False),
    Column("review_attempt", Integer, nullable=False),
    Column("worker_lease", JSON, nullable=True),
    Column("failure_class", String, nullable=False),
    Column("failure_reason", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

artifacts_table = Table(
    "artifacts",
    metadata,
    Column("id", String, primary_key=True),
    Column("job_id", String, nullable=False, index=True),
    Column("kind", String, nullable=False),
    Column("stage", String, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("schema_version", String, nullable=False),
    Column("content_type", String, nullable=False),
    Column("hash", String, nullable=False),
    Column("size", Integer, nullable=False),
    Column("storage_uri", String, nullable=False),
    Column("parent_artifact_ids", JSON, nullable=False),
    Column("producer", String, nullable=False),
    Column("sensitivity_class", String, nullable=False),
    Column("retention_class", String, nullable=False),
    Column("created_at", String, nullable=False),
)


class DatabaseStore:
    def __init__(self, database_url: str | None = None, artifact_root: Path | str | None = None) -> None:
        self.database_url = database_url or os.getenv(DATABASE_URL_ENV, DEFAULT_DATABASE_URL)
        self.engine: Engine = create_engine(self.database_url, future=True)
        self.artifact_root = Path(artifact_root or os.getenv(ARTIFACT_ROOT_ENV, "artifacts"))
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self._schema_ready = False

    def initialize(self) -> None:
        if self._schema_ready:
            return
        metadata.create_all(self.engine)
        self._schema_ready = True

    def close(self) -> None:
        self.engine.dispose()

    def create_job(self, request: CreateJobRequest) -> JobRecord:
        self.initialize()
        now = utc_now()
        row = {
            "id": new_job_id(),
            "status": "queued",
            "owner_id": request.owner_id,
            "project_id": request.project_id,
            "input_artifact_id": None,
            "config": request.config,
            "cloud_mode": request.cloud_mode,
            "review_attempt": 0,
            "worker_lease": None,
            "failure_class": "none",
            "failure_reason": None,
            "created_at": now,
            "updated_at": now,
        }
        with self.engine.begin() as connection:
            connection.execute(insert(jobs_table).values(**row))
        return self._job_from_row(row)

    def get_job(self, job_id: str) -> JobRecord | None:
        self.initialize()
        with self.engine.begin() as connection:
            row = connection.execute(select(jobs_table).where(jobs_table.c.id == job_id)).mappings().first()
        return self._job_from_row(row) if row else None

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        failure_reason: str | None = None,
        failure_class: FailureClass | None = None,
    ) -> JobRecord:
        self.initialize()
        values: dict[str, Any] = {
            "status": status,
            "updated_at": utc_now(),
        }
        if failure_reason:
            values["failure_reason"] = failure_reason
        if failure_class:
            values["failure_class"] = failure_class
        with self.engine.begin() as connection:
            result = connection.execute(update(jobs_table).where(jobs_table.c.id == job_id).values(**values))
        if result.rowcount == 0:
            raise KeyError(f"Job not found: {job_id}")
        updated_job = self.get_job(job_id)
        if updated_job is None:
            raise KeyError(f"Job not found: {job_id}")
        return updated_job

    def list_artifacts(self, job_id: str) -> list[ArtifactRecord]:
        self.initialize()
        with self.engine.begin() as connection:
            rows = (
                connection.execute(
                    select(artifacts_table)
                    .where(artifacts_table.c.job_id == job_id)
                    .order_by(artifacts_table.c.created_at, artifacts_table.c.id)
                )
                .mappings()
                .all()
            )
        return [self._artifact_from_row(row) for row in rows]

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
        self.initialize()
        artifact_id = new_artifact_id()
        with self.engine.begin() as connection:
            job_row = connection.execute(select(jobs_table.c.input_artifact_id).where(jobs_table.c.id == job_id)).first()
        if job_row is None:
            raise KeyError(f"Job not found: {job_id}")

        job_dir = self.artifact_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        safe_filename = Path(filename).name or "artifact.bin"
        target = job_dir / f"{artifact_id}-{safe_filename}"
        target.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        row = {
            "id": artifact_id,
            "job_id": job_id,
            "kind": kind,
            "stage": stage,
            "attempt": 0,
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "content_type": content_type,
            "hash": digest,
            "size": len(content),
            "storage_uri": str(target),
            "parent_artifact_ids": parent_artifact_ids or [],
            "producer": producer,
            "sensitivity_class": "source_sensitive",
            "retention_class": "project",
            "created_at": utc_now(),
        }
        with self.engine.begin() as connection:
            connection.execute(insert(artifacts_table).values(**row))
            if kind == "source_input" and not job_row.input_artifact_id:
                connection.execute(
                    update(jobs_table)
                    .where(jobs_table.c.id == job_id)
                    .values(input_artifact_id=artifact_id, updated_at=utc_now())
                )
        return self._artifact_from_row(row)

    def _job_from_row(self, row: Any) -> JobRecord:
        data = dict(row)
        return JobRecord.model_validate(data)

    def _artifact_from_row(self, row: Any) -> ArtifactRecord:
        data = dict(row)
        return ArtifactRecord.model_validate(data)


def create_store(database_url: str | None = None, artifact_root: Path | str | None = None) -> DatabaseStore:
    return DatabaseStore(database_url=database_url, artifact_root=artifact_root)


store = create_store()
