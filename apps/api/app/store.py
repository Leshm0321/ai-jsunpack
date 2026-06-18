from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, create_engine, insert, inspect, select, text, update
from sqlalchemy.engine import Engine

from .artifact_store import (
    ArtifactStore,
    ArtifactStoreConfigurationError,
    Boto3ObjectStorageClient,
    LocalArtifactStore,
    ObjectStorageClient,
    S3CompatibleArtifactStore,
    artifact_lifecycle_rules,
)
from .models import (
    ArtifactKind,
    ArtifactRecord,
    CreateJobRequest,
    FailureClass,
    JobRecord,
    JobStatus,
    RetentionCategory,
    RetentionClass,
    RetentionCleanupItem,
    RetentionCleanupRequest,
    RetentionCleanupResult,
    SensitivityClass,
    new_artifact_id,
    new_job_id,
    utc_now,
)


DATABASE_URL_ENV = "AI_JSUNPACK_DATABASE_URL"
ARTIFACT_ROOT_ENV = "AI_JSUNPACK_ARTIFACT_ROOT"
ARTIFACT_STORE_ENV = "AI_JSUNPACK_ARTIFACT_STORE"
ARTIFACT_S3_BUCKET_ENV = "AI_JSUNPACK_ARTIFACT_S3_BUCKET"
ARTIFACT_S3_PREFIX_ENV = "AI_JSUNPACK_ARTIFACT_S3_PREFIX"
ARTIFACT_S3_ENDPOINT_URL_ENV = "AI_JSUNPACK_ARTIFACT_S3_ENDPOINT_URL"
ARTIFACT_S3_REGION_ENV = "AI_JSUNPACK_ARTIFACT_S3_REGION"
ARTIFACT_S3_ACCESS_KEY_ID_ENV = "AI_JSUNPACK_ARTIFACT_S3_ACCESS_KEY_ID"
ARTIFACT_S3_SECRET_ACCESS_KEY_ENV = "AI_JSUNPACK_ARTIFACT_S3_SECRET_ACCESS_KEY"
ARTIFACT_S3_SESSION_TOKEN_ENV = "AI_JSUNPACK_ARTIFACT_S3_SESSION_TOKEN"
ARTIFACT_S3_ADDRESSING_STYLE_ENV = "AI_JSUNPACK_ARTIFACT_S3_ADDRESSING_STYLE"
ARTIFACT_S3_PRESIGN_TTL_SECONDS_ENV = "AI_JSUNPACK_ARTIFACT_S3_PRESIGN_TTL_SECONDS"
ARTIFACT_S3_LIFECYCLE_ENABLED_ENV = "AI_JSUNPACK_ARTIFACT_S3_LIFECYCLE_ENABLED"
DEFAULT_DATABASE_URL = "postgresql+psycopg://ai_jsunpack:ai_jsunpack@127.0.0.1:5432/ai_jsunpack"
CONTRACT_SCHEMA_VERSION = "2026-06-14"
EPHEMERAL_RETENTION_DAYS = 7
DEFAULT_PRESIGN_TTL_SECONDS = 3600
DEFAULT_WORKER_LEASE_SECONDS = 300
DEFAULT_WORKER_MAX_ATTEMPTS = 3
QUEUE_ELIGIBLE_STATUSES: tuple[JobStatus, ...] = ("queued", "intake")
TERMINAL_JOB_STATUSES: set[JobStatus] = {"completed", "completed_best_effort", "failed", "cancelled"}

RETENTION_CATEGORY_BY_KIND: dict[str, RetentionCategory] = {
    "source_input": "source",
    "result_package": "package",
    "audit_report": "package",
    "html_report": "package",
    "evidence_index": "package",
    "build_log": "logs",
    "runtime_trace": "logs",
    "runtime_screenshot": "screenshots",
    "memory_record": "memory",
}
DEFAULT_RETENTION_BY_CATEGORY: dict[RetentionCategory, RetentionClass] = {
    "source": "archive",
    "derived": "project",
    "package": "archive",
    "logs": "ephemeral",
    "screenshots": "ephemeral",
    "memory": "project",
}

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
    Column("run_attempt", Integer, nullable=False, default=0),
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
    Column("expires_at", String, nullable=True),
    Column("deleted_at", String, nullable=True),
    Column("deletion_reason", String, nullable=True),
)


class DatabaseStore:
    def __init__(
        self,
        database_url: str | None = None,
        artifact_root: Path | str | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self.database_url = database_url or os.getenv(DATABASE_URL_ENV, DEFAULT_DATABASE_URL)
        self.engine: Engine = create_engine(self.database_url, future=True)
        self.artifact_root = Path(artifact_root or os.getenv(ARTIFACT_ROOT_ENV, "artifacts"))
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.artifact_store = artifact_store or create_artifact_store(self.artifact_root)
        self._schema_ready = False

    def initialize(self) -> None:
        if self._schema_ready:
            return
        metadata.create_all(self.engine)
        self._ensure_jobs_queue_columns()
        self._ensure_artifacts_lifecycle_columns()
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
            "run_attempt": 0,
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
        expected_worker_id: str | None = None,
    ) -> JobRecord:
        self.initialize()
        with self.engine.begin() as connection:
            current_row = connection.execute(select(jobs_table).where(jobs_table.c.id == job_id)).mappings().first()
            if current_row is None:
                raise KeyError(f"Job not found: {job_id}")
            current_job = self._job_from_row(current_row)
            if current_job.status in TERMINAL_JOB_STATUSES and status != current_job.status:
                return current_job
            if expected_worker_id is not None and lease_worker_id(current_job.worker_lease) != expected_worker_id:
                return current_job

            values: dict[str, Any] = {
                "status": status,
                "updated_at": utc_now(),
            }
            if failure_reason:
                values["failure_reason"] = failure_reason
            if failure_class:
                values["failure_class"] = failure_class
            if status in TERMINAL_JOB_STATUSES:
                values["worker_lease"] = None

            result = connection.execute(update(jobs_table).where(jobs_table.c.id == job_id).values(**values))
            if result.rowcount == 0:
                raise KeyError(f"Job not found: {job_id}")
            row = connection.execute(select(jobs_table).where(jobs_table.c.id == job_id)).mappings().first()
        if row is None:
            raise KeyError(f"Job not found: {job_id}")
        return self._job_from_row(row)

    def lease_next_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int = DEFAULT_WORKER_LEASE_SECONDS,
        max_attempts: int = DEFAULT_WORKER_MAX_ATTEMPTS,
        now: str | None = None,
    ) -> JobRecord | None:
        self.initialize()
        lease_seconds = max(1, lease_seconds)
        max_attempts = max(1, max_attempts)
        leased_at = parse_timestamp(now) if now is not None else datetime.now(timezone.utc)
        expires_at = (leased_at + timedelta(seconds=lease_seconds)).isoformat()
        timestamp = leased_at.isoformat()

        with self.engine.begin() as connection:
            rows = (
                connection.execute(
                    select(jobs_table)
                    .where(
                        jobs_table.c.status.in_(QUEUE_ELIGIBLE_STATUSES),
                        jobs_table.c.input_artifact_id.is_not(None),
                        jobs_table.c.run_attempt < max_attempts,
                    )
                    .order_by(jobs_table.c.created_at, jobs_table.c.id)
                )
                .mappings()
                .all()
            )
            for row in rows:
                run_attempt = int(row["run_attempt"] or 0)
                result = connection.execute(
                    update(jobs_table)
                    .where(
                        jobs_table.c.id == row["id"],
                        jobs_table.c.status == row["status"],
                        jobs_table.c.input_artifact_id.is_not(None),
                        jobs_table.c.run_attempt < max_attempts,
                    )
                    .values(
                        status="leased",
                        run_attempt=run_attempt + 1,
                        worker_lease={"worker_id": worker_id, "expires_at": expires_at},
                        failure_class="none",
                        failure_reason=None,
                        updated_at=timestamp,
                    )
                )
                if result.rowcount == 0:
                    continue
                leased_row = connection.execute(select(jobs_table).where(jobs_table.c.id == row["id"])).mappings().first()
                if leased_row is not None:
                    return self._job_from_row(leased_row)
        return None

    def renew_lease(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_seconds: int = DEFAULT_WORKER_LEASE_SECONDS,
        now: str | None = None,
    ) -> JobRecord | None:
        self.initialize()
        lease_seconds = max(1, lease_seconds)
        renewed_at = parse_timestamp(now) if now is not None else datetime.now(timezone.utc)
        expires_at = (renewed_at + timedelta(seconds=lease_seconds)).isoformat()
        timestamp = renewed_at.isoformat()

        with self.engine.begin() as connection:
            row = connection.execute(select(jobs_table).where(jobs_table.c.id == job_id)).mappings().first()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            job = self._job_from_row(row)
            if job.status in TERMINAL_JOB_STATUSES or lease_worker_id(job.worker_lease) != worker_id:
                return None
            connection.execute(
                update(jobs_table)
                .where(jobs_table.c.id == job_id)
                .values(worker_lease={"worker_id": worker_id, "expires_at": expires_at}, updated_at=timestamp)
            )
            renewed_row = connection.execute(select(jobs_table).where(jobs_table.c.id == job_id)).mappings().first()
        return self._job_from_row(renewed_row) if renewed_row else None

    def request_cancel(self, job_id: str, reason: str = "cancel requested") -> JobRecord:
        self.initialize()
        timestamp = utc_now()
        with self.engine.begin() as connection:
            row = connection.execute(select(jobs_table).where(jobs_table.c.id == job_id)).mappings().first()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            job = self._job_from_row(row)
            if job.status in TERMINAL_JOB_STATUSES:
                return job
            connection.execute(
                update(jobs_table)
                .where(jobs_table.c.id == job_id)
                .values(
                    status="cancelled",
                    worker_lease=None,
                    failure_class="none",
                    failure_reason=normalize_cleanup_reason(reason),
                    updated_at=timestamp,
                )
            )
            cancelled_row = connection.execute(select(jobs_table).where(jobs_table.c.id == job_id)).mappings().first()
        if cancelled_row is None:
            raise KeyError(f"Job not found: {job_id}")
        return self._job_from_row(cancelled_row)

    def requeue_expired_leases(
        self,
        *,
        max_attempts: int = DEFAULT_WORKER_MAX_ATTEMPTS,
        now: str | None = None,
    ) -> list[JobRecord]:
        self.initialize()
        max_attempts = max(1, max_attempts)
        timestamp = parse_timestamp(now).isoformat() if now is not None else datetime.now(timezone.utc).isoformat()
        current_time = parse_timestamp(timestamp)
        updated_jobs: list[JobRecord] = []

        with self.engine.begin() as connection:
            rows = connection.execute(select(jobs_table).where(jobs_table.c.status == "leased")).mappings().all()
            for row in rows:
                job = self._job_from_row(row)
                expires_at = lease_expires_at(job.worker_lease)
                if expires_at is None or expires_at > current_time:
                    continue
                if job.run_attempt >= max_attempts:
                    values = {
                        "status": "failed",
                        "worker_lease": None,
                        "failure_class": "timeout",
                        "failure_reason": f"Worker lease expired after {job.run_attempt} attempt(s).",
                        "updated_at": timestamp,
                    }
                else:
                    values = {
                        "status": "queued",
                        "worker_lease": None,
                        "failure_class": "none",
                        "failure_reason": "Previous worker lease expired; job returned to queue.",
                        "updated_at": timestamp,
                    }
                connection.execute(update(jobs_table).where(jobs_table.c.id == job.id).values(**values))
                updated_row = connection.execute(select(jobs_table).where(jobs_table.c.id == job.id)).mappings().first()
                if updated_row is not None:
                    updated_jobs.append(self._job_from_row(updated_row))
        return updated_jobs

    def list_artifacts(
        self,
        job_id: str,
        *,
        kind: ArtifactKind | None = None,
        stage: JobStatus | None = None,
        include_deleted: bool = False,
    ) -> list[ArtifactRecord]:
        self.initialize()
        query = select(artifacts_table).where(artifacts_table.c.job_id == job_id)
        if not include_deleted:
            query = query.where(artifacts_table.c.deleted_at.is_(None))
        if kind is not None:
            query = query.where(artifacts_table.c.kind == kind)
        if stage is not None:
            query = query.where(artifacts_table.c.stage == stage)
        with self.engine.begin() as connection:
            rows = (
                connection.execute(
                    query.order_by(artifacts_table.c.created_at, artifacts_table.c.id)
                )
                .mappings()
                .all()
            )
        return [self._artifact_from_row(row) for row in rows]

    def get_artifact(self, job_id: str, artifact_id: str, *, include_deleted: bool = False) -> ArtifactRecord | None:
        self.initialize()
        query = select(artifacts_table).where(
            artifacts_table.c.job_id == job_id,
            artifacts_table.c.id == artifact_id,
        )
        if not include_deleted:
            query = query.where(artifacts_table.c.deleted_at.is_(None))
        with self.engine.begin() as connection:
            row = (
                connection.execute(query)
                .mappings()
                .first()
            )
        return self._artifact_from_row(row) if row else None

    def read_artifact(self, job_id: str, artifact_id: str) -> bytes:
        artifact = self.get_artifact(job_id, artifact_id)
        if artifact is None:
            raise KeyError(f"Artifact not found: {artifact_id}")
        return self.read_artifact_record(artifact)

    def read_artifact_record(self, artifact: ArtifactRecord) -> bytes:
        return self.artifact_store.read_bytes(artifact.storage_uri)

    def artifact_exists(self, artifact: ArtifactRecord) -> bool:
        return self.artifact_store.exists(artifact.storage_uri)

    def artifact_is_file(self, artifact: ArtifactRecord) -> bool:
        return self.artifact_store.is_file(artifact.storage_uri)

    def artifact_is_directory(self, artifact: ArtifactRecord) -> bool:
        return self.artifact_store.is_directory(artifact.storage_uri)

    def artifact_local_path(self, artifact: ArtifactRecord) -> Path | None:
        return self.artifact_store.local_path(artifact.storage_uri)

    def materialize_artifact_directory(self, artifact: ArtifactRecord, target_dir: Path | str) -> Path:
        return self.artifact_store.materialize_directory(artifact.storage_uri, target_dir)

    def artifact_filename(self, artifact: ArtifactRecord) -> str:
        return self.artifact_store.filename(artifact.storage_uri)

    def artifact_suffix(self, artifact: ArtifactRecord) -> str:
        return self.artifact_store.suffix(artifact.storage_uri)

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
        attempt: int = 0,
        sensitivity_class: SensitivityClass = "source_sensitive",
        retention_class: RetentionClass | None = None,
        expires_at: str | None = None,
    ) -> ArtifactRecord:
        self.initialize()
        artifact_id = new_artifact_id()
        with self.engine.begin() as connection:
            job_row = connection.execute(select(jobs_table.c.input_artifact_id).where(jobs_table.c.id == job_id)).first()
        if job_row is None:
            raise KeyError(f"Job not found: {job_id}")
        resolved_retention_class = retention_class or default_retention_class(kind)
        resolved_expires_at = expires_at if expires_at is not None else default_expires_at(resolved_retention_class)
        metadata, tags = artifact_object_metadata(
            job_id=job_id,
            artifact_id=artifact_id,
            kind=kind,
            stage=stage,
            producer=producer,
            sensitivity_class=sensitivity_class,
            retention_class=resolved_retention_class,
        )
        stored = self.artifact_store.write_bytes(
            job_id=job_id,
            artifact_id=artifact_id,
            filename=filename,
            content=content,
            metadata=metadata,
            tags=tags,
        )
        row = {
            "id": artifact_id,
            "job_id": job_id,
            "kind": kind,
            "stage": stage,
            "attempt": attempt,
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "content_type": content_type,
            "hash": stored.hash,
            "size": stored.size,
            "storage_uri": stored.storage_uri,
            "parent_artifact_ids": parent_artifact_ids or [],
            "producer": producer,
            "sensitivity_class": sensitivity_class,
            "retention_class": resolved_retention_class,
            "created_at": utc_now(),
            "expires_at": resolved_expires_at,
            "deleted_at": None,
            "deletion_reason": None,
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

    def register_artifact_path(
        self,
        job_id: str,
        *,
        kind: ArtifactKind,
        stage: JobStatus,
        filename: str,
        source_path: Path | str,
        content_type: str,
        producer: str,
        parent_artifact_ids: list[str] | None = None,
        attempt: int = 0,
        sensitivity_class: SensitivityClass = "source_sensitive",
        retention_class: RetentionClass | None = None,
        expires_at: str | None = None,
    ) -> ArtifactRecord:
        self.initialize()
        artifact_id = new_artifact_id()
        with self.engine.begin() as connection:
            job_row = connection.execute(select(jobs_table.c.input_artifact_id).where(jobs_table.c.id == job_id)).first()
        if job_row is None:
            raise KeyError(f"Job not found: {job_id}")

        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Artifact source path does not exist: {source}")
        resolved_retention_class = retention_class or default_retention_class(kind)
        resolved_expires_at = expires_at if expires_at is not None else default_expires_at(resolved_retention_class)
        metadata, tags = artifact_object_metadata(
            job_id=job_id,
            artifact_id=artifact_id,
            kind=kind,
            stage=stage,
            producer=producer,
            sensitivity_class=sensitivity_class,
            retention_class=resolved_retention_class,
        )
        stored = self.artifact_store.copy_path(
            job_id=job_id,
            artifact_id=artifact_id,
            filename=filename,
            source_path=source,
            metadata=metadata,
            tags=tags,
        )

        row = {
            "id": artifact_id,
            "job_id": job_id,
            "kind": kind,
            "stage": stage,
            "attempt": attempt,
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "content_type": content_type,
            "hash": stored.hash,
            "size": stored.size,
            "storage_uri": stored.storage_uri,
            "parent_artifact_ids": parent_artifact_ids or [],
            "producer": producer,
            "sensitivity_class": sensitivity_class,
            "retention_class": resolved_retention_class,
            "created_at": utc_now(),
            "expires_at": resolved_expires_at,
            "deleted_at": None,
            "deletion_reason": None,
        }
        with self.engine.begin() as connection:
            connection.execute(insert(artifacts_table).values(**row))
        return self._artifact_from_row(row)

    def cleanup_retention(self, job_id: str, request: RetentionCleanupRequest) -> RetentionCleanupResult:
        self.initialize()
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"Job not found: {job_id}")

        requested_at = request.now or utc_now()
        now = parse_timestamp(requested_at)
        active_artifacts = self.list_artifacts(job_id)
        candidates = [
            artifact
            for artifact in active_artifacts
            if cleanup_matches(artifact=artifact, request=request, now=now)
        ]
        reason = normalize_cleanup_reason(request.reason)
        items: list[RetentionCleanupItem] = []
        errors: list[str] = []
        deleted_count = 0

        for artifact in candidates:
            category = retention_category_for_kind(artifact.kind)
            if request.dry_run:
                items.append(
                    RetentionCleanupItem(
                        artifact_id=artifact.id,
                        kind=artifact.kind,
                        category=category,
                        retention_class=artifact.retention_class,
                        storage_uri=artifact.storage_uri,
                        deleted=False,
                        reason="dry_run",
                    )
                )
                continue

            try:
                self.artifact_store.delete(artifact.storage_uri)
                with self.engine.begin() as connection:
                    connection.execute(
                        update(artifacts_table)
                        .where(artifacts_table.c.id == artifact.id)
                        .values(deleted_at=requested_at, deletion_reason=reason)
                    )
                deleted_count += 1
                items.append(
                    RetentionCleanupItem(
                        artifact_id=artifact.id,
                        kind=artifact.kind,
                        category=category,
                        retention_class=artifact.retention_class,
                        storage_uri=artifact.storage_uri,
                        deleted=True,
                        reason=reason,
                    )
                )
            except Exception as error:
                message = f"{artifact.id}: {error}"
                errors.append(message)
                items.append(
                    RetentionCleanupItem(
                        artifact_id=artifact.id,
                        kind=artifact.kind,
                        category=category,
                        retention_class=artifact.retention_class,
                        storage_uri=artifact.storage_uri,
                        deleted=False,
                        reason=reason,
                        error=str(error),
                    )
                )

        return RetentionCleanupResult(
            job_id=job_id,
            dry_run=request.dry_run,
            requested_at=requested_at,
            candidate_count=len(candidates),
            deleted_count=deleted_count,
            skipped_count=len(active_artifacts) - len(candidates),
            error_count=len(errors),
            items=items,
            errors=errors,
        )

    def _job_from_row(self, row: Any) -> JobRecord:
        data = dict(row)
        data.setdefault("run_attempt", 0)
        return JobRecord.model_validate(data)

    def _artifact_from_row(self, row: Any) -> ArtifactRecord:
        data = dict(row)
        data.setdefault("expires_at", None)
        data.setdefault("deleted_at", None)
        data.setdefault("deletion_reason", None)
        return ArtifactRecord.model_validate(data)

    def _ensure_jobs_queue_columns(self) -> None:
        existing_columns = {column["name"] for column in inspect(self.engine).get_columns("jobs")}
        with self.engine.begin() as connection:
            if "run_attempt" not in existing_columns:
                connection.execute(text("ALTER TABLE jobs ADD COLUMN run_attempt INTEGER NOT NULL DEFAULT 0"))

    def _ensure_artifacts_lifecycle_columns(self) -> None:
        existing_columns = {column["name"] for column in inspect(self.engine).get_columns("artifacts")}
        lifecycle_columns = {
            "expires_at": "VARCHAR",
            "deleted_at": "VARCHAR",
            "deletion_reason": "VARCHAR",
        }
        with self.engine.begin() as connection:
            for column_name, column_type in lifecycle_columns.items():
                if column_name not in existing_columns:
                    connection.execute(text(f"ALTER TABLE artifacts ADD COLUMN {column_name} {column_type}"))


def retention_category_for_kind(kind: str) -> RetentionCategory:
    return RETENTION_CATEGORY_BY_KIND.get(kind, "derived")


def default_retention_class(kind: str) -> RetentionClass:
    return DEFAULT_RETENTION_BY_CATEGORY[retention_category_for_kind(kind)]


def default_expires_at(retention_class: RetentionClass) -> str | None:
    if retention_class != "ephemeral":
        return None
    return (datetime.now(timezone.utc) + timedelta(days=EPHEMERAL_RETENTION_DAYS)).isoformat()


def cleanup_matches(*, artifact: ArtifactRecord, request: RetentionCleanupRequest, now: datetime) -> bool:
    categories = set(request.categories)
    retention_classes = set(request.retention_classes)
    has_selector = bool(categories or retention_classes)

    if request.delete_expired and not artifact_is_expired(artifact, now):
        return False
    if not request.delete_expired and not has_selector:
        return False
    if categories and retention_category_for_kind(artifact.kind) not in categories:
        return False
    if retention_classes and artifact.retention_class not in retention_classes:
        return False
    return True


def artifact_is_expired(artifact: ArtifactRecord, now: datetime) -> bool:
    if not artifact.expires_at:
        return False
    return parse_timestamp(artifact.expires_at) <= now


def parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def lease_worker_id(worker_lease: Any) -> str | None:
    if worker_lease is None:
        return None
    if hasattr(worker_lease, "worker_id"):
        return getattr(worker_lease, "worker_id")
    if isinstance(worker_lease, dict):
        value = worker_lease.get("worker_id", worker_lease.get("workerId"))
        return str(value) if value is not None else None
    return None


def lease_expires_at(worker_lease: Any) -> datetime | None:
    if worker_lease is None:
        return None
    if hasattr(worker_lease, "expires_at"):
        value = getattr(worker_lease, "expires_at")
    elif isinstance(worker_lease, dict):
        value = worker_lease.get("expires_at", worker_lease.get("expiresAt"))
    else:
        value = None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def normalize_cleanup_reason(reason: str) -> str:
    stripped = reason.strip()
    return stripped or "retention cleanup"


def artifact_object_metadata(
    *,
    job_id: str,
    artifact_id: str,
    kind: ArtifactKind,
    stage: JobStatus,
    producer: str,
    sensitivity_class: SensitivityClass,
    retention_class: RetentionClass,
) -> tuple[dict[str, str], dict[str, str]]:
    metadata = {
        "job-id": job_id,
        "artifact-id": artifact_id,
        "artifact-kind": kind,
        "stage": stage,
        "producer": producer,
        "sensitivity-class": sensitivity_class,
        "retention-class": retention_class,
    }
    tags = {
        "jobId": job_id,
        "artifactId": artifact_id,
        "artifactKind": kind,
        "stage": stage,
        "sensitivityClass": sensitivity_class,
        "retentionClass": retention_class,
    }
    return metadata, tags


def create_artifact_store(
    artifact_root: Path | str,
    *,
    object_client: ObjectStorageClient | None = None,
) -> ArtifactStore:
    backend = os.getenv(ARTIFACT_STORE_ENV, "local").strip().lower()
    if backend in {"s3", "minio"}:
        bucket = os.getenv(ARTIFACT_S3_BUCKET_ENV, "").strip()
        prefix = os.getenv(ARTIFACT_S3_PREFIX_ENV, "").strip()
        if not bucket:
            raise ArtifactStoreConfigurationError("S3-compatible artifact store requires a bucket name.")
        presign_ttl_seconds = parse_positive_int_env(
            ARTIFACT_S3_PRESIGN_TTL_SECONDS_ENV,
            DEFAULT_PRESIGN_TTL_SECONDS,
        )
        client = object_client or Boto3ObjectStorageClient(
            endpoint_url=optional_env(ARTIFACT_S3_ENDPOINT_URL_ENV),
            region_name=optional_env(ARTIFACT_S3_REGION_ENV),
            access_key_id=optional_env(ARTIFACT_S3_ACCESS_KEY_ID_ENV),
            secret_access_key=optional_env(ARTIFACT_S3_SECRET_ACCESS_KEY_ENV),
            session_token=optional_env(ARTIFACT_S3_SESSION_TOKEN_ENV),
            addressing_style=optional_env(ARTIFACT_S3_ADDRESSING_STYLE_ENV) or "auto",
        )
        if parse_bool_env(ARTIFACT_S3_LIFECYCLE_ENABLED_ENV, False):
            configure_s3_lifecycle(client=client, bucket=bucket, prefix=prefix)
        return S3CompatibleArtifactStore(
            bucket=bucket,
            prefix=prefix,
            client=client,
            presign_ttl_seconds=presign_ttl_seconds,
        )
    return LocalArtifactStore(artifact_root)


def configure_s3_lifecycle(*, client: ObjectStorageClient, bucket: str, prefix: str) -> None:
    if not bucket:
        raise ArtifactStoreConfigurationError("S3 artifact lifecycle requires a bucket name.")
    configurator = getattr(client, "configure_lifecycle_rules", None)
    if not callable(configurator):
        raise ArtifactStoreConfigurationError("S3 artifact lifecycle requires a client with lifecycle support.")
    configurator(bucket, artifact_lifecycle_rules(prefix=prefix, ephemeral_days=EPHEMERAL_RETENTION_DAYS))


def optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def parse_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ArtifactStoreConfigurationError(f"{name} must be a boolean value.")


def parse_positive_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ArtifactStoreConfigurationError(f"{name} must be an integer.") from error
    if parsed <= 0:
        raise ArtifactStoreConfigurationError(f"{name} must be greater than zero.")
    return parsed


def create_store(
    database_url: str | None = None,
    artifact_root: Path | str | None = None,
    artifact_store: ArtifactStore | None = None,
) -> DatabaseStore:
    return DatabaseStore(database_url=database_url, artifact_root=artifact_root, artifact_store=artifact_store)


store = create_store()
