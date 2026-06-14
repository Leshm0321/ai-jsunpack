from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


JobStatus = Literal[
    "queued",
    "leased",
    "intake",
    "planning",
    "parsing",
    "indexing",
    "analyzing",
    "agent_planning",
    "agent_pass",
    "reconstructing",
    "building",
    "typechecking",
    "runtime_smoke",
    "runtime_compare",
    "reviewing",
    "repairing",
    "packaging",
    "completed",
    "completed_best_effort",
    "failed",
    "cancelled",
]

CloudMode = Literal["cloud_allowed", "local_only", "desensitized"]
FailureClass = Literal[
    "none",
    "invalid_input",
    "parse_error",
    "agent_failed",
    "dependency_missing",
    "install_failed",
    "type_error",
    "build_error",
    "runtime_error",
    "sandbox_denied",
    "policy_denied",
    "timeout",
    "resource_limit",
    "unknown",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CreateJobRequest(BaseModel):
    project_id: str = Field(default="default")
    owner_id: str = Field(default="local-user")
    cloud_mode: CloudMode = Field(default="local_only")
    config: dict[str, Any] = Field(default_factory=dict)


class JobRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"job_{uuid4().hex[:12]}")
    status: JobStatus = "queued"
    owner_id: str
    project_id: str
    input_artifact_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    cloud_mode: CloudMode = "local_only"
    review_attempt: int = 0
    failure_class: FailureClass = "none"
    failure_reason: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class ArtifactRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"artifact_{uuid4().hex[:12]}")
    job_id: str
    kind: str
    stage: JobStatus
    attempt: int = 0
    schema_version: str = "2026-06-14"
    content_type: str
    hash: str
    size: int
    storage_uri: str
    parent_artifact_ids: list[str] = Field(default_factory=list)
    producer: str
    sensitivity_class: str = "source_sensitive"
    retention_class: str = "project"
    created_at: str = Field(default_factory=utc_now)


class JobSummary(BaseModel):
    job: JobRecord
    artifacts: list[ArtifactRecord]

