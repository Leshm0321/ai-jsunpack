from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Protocol
from urllib.parse import quote
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import JSON, Column, Index, Integer, MetaData, String, Table, create_engine, insert, inspect, select, text, update
from sqlalchemy.engine import Engine

from apps.api.app.auth import AccessContext, SERVICE_ROLE_WORKER, require_access
from apps.api.app.models import (
    BrowserRunnerQueueAlert,
    BrowserRunnerQueueHealth,
    BrowserRunnerQueueMetrics,
    BrowserRunRequest,
    BrowserRunResult,
    BrowserRunSourceArchive,
    BrowserRunStatus,
    BrowserRunSummary,
    FailureClass,
    OpsAlert,
    OpsHeartbeatRequest,
    RunStatus,
)
from apps.api.app.models import utc_now
from apps.api.app.store import DATABASE_URL_ENV, DEFAULT_DATABASE_URL, create_store
from apps.worker.worker.runtime_smoke import (
    BrowserSmokeAdapter,
    BrowserSmokeCapture,
    BrowserSmokeRequest,
    PlaywrightBrowserAdapter,
    RuntimeSmokeError,
)
from packages.configuration import apply_application_config_to_environment
from packages.deployment import DeploymentConfigurationError, validate_current_environment

BROWSER_RUNNER_WORKERS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_WORKERS"
BROWSER_RUNNER_WORKDIR_ENV = "AI_JSUNPACK_BROWSER_RUNNER_WORKDIR"
BROWSER_RUNNER_DB_PATH_ENV = "AI_JSUNPACK_BROWSER_RUNNER_DB_PATH"
BROWSER_RUNNER_QUEUE_BACKEND_ENV = "AI_JSUNPACK_BROWSER_RUNNER_QUEUE_BACKEND"
BROWSER_RUNNER_QUEUE_DATABASE_URL_ENV = "AI_JSUNPACK_BROWSER_RUNNER_QUEUE_DATABASE_URL"
BROWSER_RUNNER_MAX_ATTEMPTS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_MAX_ATTEMPTS"
BROWSER_RUNNER_LEASE_SECONDS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_LEASE_SECONDS"
BROWSER_RUNNER_RETRY_BACKOFF_SECONDS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_RETRY_BACKOFF_SECONDS"
BROWSER_RUNNER_POLL_SECONDS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_POLL_SECONDS"
BROWSER_RUNNER_MAX_QUEUE_AGE_MS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_MAX_QUEUE_AGE_MS"
BROWSER_RUNNER_MAX_CLAIM_LATENCY_MS_ENV = "AI_JSUNPACK_BROWSER_RUNNER_MAX_CLAIM_LATENCY_MS"
BROWSER_RUNNER_MAX_EXPIRED_RUNNING_ENV = "AI_JSUNPACK_BROWSER_RUNNER_MAX_EXPIRED_RUNNING"
BROWSER_RUNNER_MAX_RETRY_RATE_ENV = "AI_JSUNPACK_BROWSER_RUNNER_MAX_RETRY_RATE"
DEFAULT_BROWSER_RUNNER_WORKERS = 2
DEFAULT_BROWSER_RUNNER_MAX_ATTEMPTS = 3
DEFAULT_BROWSER_RUNNER_LEASE_SECONDS = 120
DEFAULT_BROWSER_RUNNER_RETRY_BACKOFF_SECONDS = 1.0
DEFAULT_BROWSER_RUNNER_POLL_SECONDS = 0.25
DEFAULT_BROWSER_RUNNER_MAX_QUEUE_AGE_MS = 60_000
DEFAULT_BROWSER_RUNNER_MAX_CLAIM_LATENCY_MS = 60_000
DEFAULT_BROWSER_RUNNER_MAX_EXPIRED_RUNNING = 0
DEFAULT_BROWSER_RUNNER_MAX_RETRY_RATE = 0.25
TERMINAL_BROWSER_RUN_STATUSES = {"pass", "fail", "best_effort"}

apply_application_config_to_environment("browser-runner")

try:
    DEPLOYMENT_PROFILE = validate_current_environment("browser-runner")
except DeploymentConfigurationError as error:
    raise RuntimeError(str(error)) from error


@dataclass(frozen=True)
class BrowserRunRecord:
    id: str
    request: BrowserRunRequest
    status: BrowserRunStatus
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: BrowserRunResult | None = None
    error: str | None = None
    attempt: int = 0
    max_attempts: int = DEFAULT_BROWSER_RUNNER_MAX_ATTEMPTS
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    next_run_at: str | None = None
    worker_id: str | None = None
    queue_backend: str = "sqlite"
    lease_recovered: bool = False

    def summary(self) -> BrowserRunSummary:
        return BrowserRunSummary(
            id=self.id,
            status=self.status,
            result=self.result,
            error=self.error,
            created_at=self.created_at,
            updated_at=self.updated_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            attempt=self.attempt,
            max_attempts=self.max_attempts,
            lease_owner=self.lease_owner,
            lease_expires_at=self.lease_expires_at,
            next_run_at=self.next_run_at,
            worker_id=self.worker_id,
            queue_backend=self.queue_backend,
            lease_recovered=self.lease_recovered,
        )


class BrowserRunQueueBackend(Protocol):
    name: str

    def initialize(self) -> None:
        ...

    def close(self) -> None:
        ...

    def insert(self, record: BrowserRunRecord) -> None:
        ...

    def get(self, run_id: str) -> BrowserRunRecord | None:
        ...

    def update(self, run_id: str, **changes) -> BrowserRunRecord | None:
        ...

    def claim(self, run_id: str, *, worker_id: str, lease_seconds: int) -> BrowserRunRecord | None:
        ...

    def claim_due(self, *, now: str, worker_id: str, lease_seconds: int, excluded_run_ids: set[str]) -> BrowserRunRecord | None:
        ...

    def recover_expired_leases(
        self,
        *,
        now: str,
        submitted_run_ids: set[str],
        execution_boundary,
    ) -> list[BrowserRunRecord]:
        ...

    def stats(self, *, now: str) -> BrowserRunnerQueueMetrics:
        ...


class SQLiteBrowserRunQueueBackend:
    name = "sqlite"

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS browser_runs (
                    id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result_json TEXT,
                    error TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 1,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    next_run_at TEXT,
                    worker_id TEXT,
                    queue_backend TEXT NOT NULL DEFAULT 'sqlite',
                    lease_recovered INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS browser_runs_due_idx ON browser_runs(status, next_run_at, created_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS browser_runs_lease_idx ON browser_runs(status, lease_expires_at)")

    def close(self) -> None:
        return

    def insert(self, record: BrowserRunRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO browser_runs (
                    id, request_json, status, created_at, updated_at, started_at, finished_at,
                    result_json, error, attempt, max_attempts, lease_owner, lease_expires_at,
                    next_run_at, worker_id, queue_backend, lease_recovered
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._row_values(record),
            )

    def get(self, run_id: str) -> BrowserRunRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM browser_runs WHERE id = ?", (run_id,)).fetchone()
        return self._record_from_row(row) if row else None

    def update(self, run_id: str, **changes) -> BrowserRunRecord | None:
        record = self.get(run_id)
        if record is None:
            return None
        updated = replace(record, updated_at=utc_now(), **changes)
        with self._connect() as connection:
            self._save(connection, updated)
        return updated

    def claim(self, run_id: str, *, worker_id: str, lease_seconds: int) -> BrowserRunRecord | None:
        started_at = utc_now()
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM browser_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None or row["status"] != "queued":
                connection.execute("ROLLBACK")
                return None
            record = self._record_from_row(row)
            updated = replace(
                record,
                status="running",
                attempt=record.attempt + 1,
                started_at=record.started_at or started_at,
                updated_at=started_at,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                worker_id=worker_id,
            )
            self._save(connection, updated)
            connection.execute("COMMIT")
            return updated

    def claim_due(self, *, now: str, worker_id: str, lease_seconds: int, excluded_run_ids: set[str]) -> BrowserRunRecord | None:
        started_at = utc_now()
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM browser_runs
                WHERE status = 'queued' AND (next_run_at IS NULL OR next_run_at <= ?)
                ORDER BY next_run_at, created_at, id
                """,
                (now,),
            ).fetchall()
            for row in rows:
                record = self._record_from_row(row)
                if record.id in excluded_run_ids:
                    continue
                updated = replace(
                    record,
                    status="running",
                    attempt=record.attempt + 1,
                    started_at=record.started_at or started_at,
                    updated_at=started_at,
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires_at,
                    worker_id=worker_id,
                )
                self._save(connection, updated)
                connection.execute("COMMIT")
                return updated
            connection.execute("ROLLBACK")
        return None

    def recover_expired_leases(
        self,
        *,
        now: str,
        submitted_run_ids: set[str],
        execution_boundary,
    ) -> list[BrowserRunRecord]:
        current_time = parse_timestamp(now)
        recovered: list[BrowserRunRecord] = []
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM browser_runs WHERE status = 'running'").fetchall()
            for row in rows:
                record = self._record_from_row(row)
                if record.id in submitted_run_ids:
                    continue
                expires_at = parse_optional_timestamp(record.lease_expires_at)
                if expires_at is None or expires_at > current_time:
                    continue
                updated = expired_record_update(record, timestamp=now, execution_boundary=execution_boundary)
                self._save(connection, updated)
                recovered.append(updated)
        return recovered

    def stats(self, *, now: str) -> BrowserRunnerQueueMetrics:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM browser_runs").fetchall()
        records = [self._record_from_row(row) for row in rows]
        return browser_runner_queue_metrics(records, now=now, queue_backend=self.name)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def _save(self, connection: sqlite3.Connection, record: BrowserRunRecord) -> None:
        connection.execute(
            """
            UPDATE browser_runs
            SET request_json = ?, status = ?, created_at = ?, updated_at = ?, started_at = ?,
                finished_at = ?, result_json = ?, error = ?, attempt = ?, max_attempts = ?,
                lease_owner = ?, lease_expires_at = ?, next_run_at = ?, worker_id = ?,
                queue_backend = ?, lease_recovered = ?
            WHERE id = ?
            """,
            (*self._row_values(record)[1:], record.id),
        )

    def _row_values(self, record: BrowserRunRecord) -> tuple[Any, ...]:
        return (
            record.id,
            record.request.model_dump_json(by_alias=True),
            record.status,
            record.created_at,
            record.updated_at,
            record.started_at,
            record.finished_at,
            record.result.model_dump_json(by_alias=True) if record.result else None,
            record.error,
            record.attempt,
            record.max_attempts,
            record.lease_owner,
            record.lease_expires_at,
            record.next_run_at,
            record.worker_id,
            record.queue_backend,
            1 if record.lease_recovered else 0,
        )

    def _record_from_row(self, row: sqlite3.Row) -> BrowserRunRecord:
        return browser_run_record_from_mapping(row, default_queue_backend=self.name)


browser_runner_metadata = MetaData()
browser_runs_table = Table(
    "browser_runs",
    browser_runner_metadata,
    Column("id", String, primary_key=True),
    Column("request_json", JSON, nullable=False),
    Column("status", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("started_at", String, nullable=True),
    Column("finished_at", String, nullable=True),
    Column("result_json", JSON, nullable=True),
    Column("error", String, nullable=True),
    Column("attempt", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False, default=1),
    Column("lease_owner", String, nullable=True),
    Column("lease_expires_at", String, nullable=True),
    Column("next_run_at", String, nullable=True),
    Column("worker_id", String, nullable=True),
    Column("queue_backend", String, nullable=False, default="postgresql"),
    Column("lease_recovered", Integer, nullable=False, default=0),
    Index("browser_runs_due_idx", "status", "next_run_at", "created_at"),
    Index("browser_runs_lease_idx", "status", "lease_expires_at"),
)


class SqlAlchemyBrowserRunQueueBackend:
    name = "postgresql"

    def __init__(self, database_url: str | None = None, engine: Engine | None = None) -> None:
        self.database_url = database_url or os.getenv(BROWSER_RUNNER_QUEUE_DATABASE_URL_ENV) or os.getenv(DATABASE_URL_ENV, DEFAULT_DATABASE_URL)
        self.engine = engine or create_engine(self.database_url, future=True)
        self._owns_engine = engine is None
        self._schema_ready = False

    def initialize(self) -> None:
        if self._schema_ready:
            return
        browser_runner_metadata.create_all(self.engine)
        self._ensure_columns()
        self._schema_ready = True

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()

    def insert(self, record: BrowserRunRecord) -> None:
        self.initialize()
        with self.engine.begin() as connection:
            connection.execute(insert(browser_runs_table).values(**self._row_values(record)))

    def get(self, run_id: str) -> BrowserRunRecord | None:
        self.initialize()
        with self.engine.begin() as connection:
            row = connection.execute(select(browser_runs_table).where(browser_runs_table.c.id == run_id)).mappings().first()
        return self._record_from_row(row) if row else None

    def update(self, run_id: str, **changes) -> BrowserRunRecord | None:
        self.initialize()
        with self.engine.begin() as connection:
            row = connection.execute(select(browser_runs_table).where(browser_runs_table.c.id == run_id)).mappings().first()
            if row is None:
                return None
            record = self._record_from_row(row)
            updated = replace(record, updated_at=utc_now(), **changes)
            connection.execute(update(browser_runs_table).where(browser_runs_table.c.id == run_id).values(**self._row_values(updated)))
        return updated

    def claim(self, run_id: str, *, worker_id: str, lease_seconds: int) -> BrowserRunRecord | None:
        self.initialize()
        started_at = utc_now()
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self.engine.begin() as connection:
            row = connection.execute(select(browser_runs_table).where(browser_runs_table.c.id == run_id)).mappings().first()
            if row is None or row["status"] != "queued":
                return None
            record = self._record_from_row(row)
            updated = replace(
                record,
                status="running",
                attempt=record.attempt + 1,
                started_at=record.started_at or started_at,
                updated_at=started_at,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                worker_id=worker_id,
            )
            result = connection.execute(
                update(browser_runs_table)
                .where(browser_runs_table.c.id == run_id, browser_runs_table.c.status == "queued")
                .values(**self._row_values(updated))
            )
            if result.rowcount == 0:
                return None
        return updated

    def claim_due(self, *, now: str, worker_id: str, lease_seconds: int, excluded_run_ids: set[str]) -> BrowserRunRecord | None:
        self.initialize()
        started_at = utc_now()
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self.engine.begin() as connection:
            rows = (
                connection.execute(
                    select(browser_runs_table)
                    .where(
                        browser_runs_table.c.status == "queued",
                        (browser_runs_table.c.next_run_at.is_(None)) | (browser_runs_table.c.next_run_at <= now),
                    )
                    .order_by(browser_runs_table.c.next_run_at, browser_runs_table.c.created_at, browser_runs_table.c.id)
                )
                .mappings()
                .all()
            )
            for row in rows:
                record = self._record_from_row(row)
                if record.id in excluded_run_ids:
                    continue
                updated = replace(
                    record,
                    status="running",
                    attempt=record.attempt + 1,
                    started_at=record.started_at or started_at,
                    updated_at=started_at,
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires_at,
                    worker_id=worker_id,
                )
                result = connection.execute(
                    update(browser_runs_table)
                    .where(browser_runs_table.c.id == record.id, browser_runs_table.c.status == "queued")
                    .values(**self._row_values(updated))
                )
                if result.rowcount:
                    return updated
        return None

    def recover_expired_leases(
        self,
        *,
        now: str,
        submitted_run_ids: set[str],
        execution_boundary,
    ) -> list[BrowserRunRecord]:
        self.initialize()
        current_time = parse_timestamp(now)
        recovered: list[BrowserRunRecord] = []
        with self.engine.begin() as connection:
            rows = connection.execute(select(browser_runs_table).where(browser_runs_table.c.status == "running")).mappings().all()
            for row in rows:
                record = self._record_from_row(row)
                if record.id in submitted_run_ids:
                    continue
                expires_at = parse_optional_timestamp(record.lease_expires_at)
                if expires_at is None or expires_at > current_time:
                    continue
                updated = expired_record_update(record, timestamp=now, execution_boundary=execution_boundary)
                result = connection.execute(
                    update(browser_runs_table)
                    .where(
                        browser_runs_table.c.id == record.id,
                        browser_runs_table.c.status == "running",
                        browser_runs_table.c.lease_expires_at == record.lease_expires_at,
                    )
                    .values(**self._row_values(updated))
                )
                if result.rowcount:
                    recovered.append(updated)
        return recovered

    def stats(self, *, now: str) -> BrowserRunnerQueueMetrics:
        self.initialize()
        with self.engine.begin() as connection:
            rows = connection.execute(select(browser_runs_table)).mappings().all()
        records = [self._record_from_row(row) for row in rows]
        return browser_runner_queue_metrics(records, now=now, queue_backend=self.name)

    def _row_values(self, record: BrowserRunRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "request_json": record.request.model_dump(by_alias=True),
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "result_json": record.result.model_dump(by_alias=True) if record.result else None,
            "error": record.error,
            "attempt": record.attempt,
            "max_attempts": record.max_attempts,
            "lease_owner": record.lease_owner,
            "lease_expires_at": record.lease_expires_at,
            "next_run_at": record.next_run_at,
            "worker_id": record.worker_id,
            "queue_backend": record.queue_backend,
            "lease_recovered": 1 if record.lease_recovered else 0,
        }

    def _record_from_row(self, row: Any) -> BrowserRunRecord:
        return browser_run_record_from_mapping(row, default_queue_backend=self.name)

    def _ensure_columns(self) -> None:
        existing_columns = {column["name"] for column in inspect(self.engine).get_columns("browser_runs")}
        expected_columns = {
            "started_at": "VARCHAR",
            "finished_at": "VARCHAR",
            "result_json": "JSON",
            "error": "VARCHAR",
            "attempt": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 1",
            "lease_owner": "VARCHAR",
            "lease_expires_at": "VARCHAR",
            "next_run_at": "VARCHAR",
            "worker_id": "VARCHAR",
            "queue_backend": "VARCHAR NOT NULL DEFAULT 'postgresql'",
            "lease_recovered": "INTEGER NOT NULL DEFAULT 0",
        }
        with self.engine.begin() as connection:
            for column_name, column_type in expected_columns.items():
                if column_name not in existing_columns:
                    connection.execute(text(f"ALTER TABLE browser_runs ADD COLUMN {column_name} {column_type}"))


def browser_run_record_from_mapping(row: Any, *, default_queue_backend: str) -> BrowserRunRecord:
    data = dict(row)
    request_json = data["request_json"]
    result_json = data.get("result_json")
    if isinstance(request_json, str):
        request_payload = json.loads(request_json)
    else:
        request_payload = request_json
    if isinstance(result_json, str):
        result_payload = json.loads(result_json) if result_json else None
    else:
        result_payload = result_json
    return BrowserRunRecord(
        id=data["id"],
        request=BrowserRunRequest.model_validate(request_payload),
        status=data["status"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        result=BrowserRunResult.model_validate(result_payload) if result_payload else None,
        error=data.get("error"),
        attempt=int(data.get("attempt") or 0),
        max_attempts=int(data.get("max_attempts") or DEFAULT_BROWSER_RUNNER_MAX_ATTEMPTS),
        lease_owner=data.get("lease_owner"),
        lease_expires_at=data.get("lease_expires_at"),
        next_run_at=data.get("next_run_at"),
        worker_id=data.get("worker_id"),
        queue_backend=data.get("queue_backend") or default_queue_backend,
        lease_recovered=bool(data.get("lease_recovered")),
    )


def browser_runner_queue_metrics(
    records: list[BrowserRunRecord],
    *,
    now: str,
    queue_backend: str,
) -> BrowserRunnerQueueMetrics:
    current_time = parse_timestamp(now)
    queued = [record for record in records if record.status == "queued"]
    running = [record for record in records if record.status == "running"]
    terminal = [record for record in records if record.status in TERMINAL_BROWSER_RUN_STATUSES]
    completed_with_duration = [
        duration
        for duration in (record_duration_ms(record) for record in terminal)
        if duration is not None
    ]
    retry_candidates = [record for record in records if record.attempt > 0]
    retry_count = sum(1 for record in retry_candidates if record.attempt > 1)
    expired_running = [
        record
        for record in running
        if (expires_at := parse_optional_timestamp(record.lease_expires_at)) is not None and expires_at <= current_time
    ]
    oldest_queued_age_ms = max(
        (
            max(0, duration_between_ms(parse_timestamp(record.created_at), current_time))
            for record in queued
        ),
        default=None,
    )
    claim_latency_ms = max(
        (
            max(0, duration_between_ms(parse_timestamp(record.next_run_at or record.created_at), current_time))
            for record in queued
        ),
        default=None,
    )
    average_run_duration_ms = (
        int(sum(completed_with_duration) / len(completed_with_duration))
        if completed_with_duration
        else None
    )
    retry_rate = retry_count / len(retry_candidates) if retry_candidates else 0.0
    return BrowserRunnerQueueMetrics(
        checked_at=now,
        queue_backend=queue_backend,
        backend_status="ok",
        queued_count=len(queued),
        running_count=len(running),
        terminal_count=len(terminal),
        total_count=len(records),
        oldest_queued_age_ms=oldest_queued_age_ms,
        claim_latency_ms=claim_latency_ms,
        average_run_duration_ms=average_run_duration_ms,
        retry_rate=retry_rate,
        lease_recovery_count=sum(1 for record in records if record.lease_recovered),
        expired_running_count=len(expired_running),
    )


def record_duration_ms(record: BrowserRunRecord) -> int | None:
    if record.started_at is None or record.finished_at is None:
        return None
    start = parse_optional_timestamp(record.started_at)
    finish = parse_optional_timestamp(record.finished_at)
    if start is None or finish is None:
        return None
    return max(0, duration_between_ms(start, finish))


def duration_between_ms(start: datetime, finish: datetime) -> int:
    return int((finish - start).total_seconds() * 1000)


def expired_record_update(record: BrowserRunRecord, *, timestamp: str, execution_boundary) -> BrowserRunRecord:
    if record.attempt >= record.max_attempts:
        result = BrowserRunResult(
            status="best_effort",
            failure_class="timeout",
            page_errors=[f"Browser Runner 租约在第 {record.attempt} 次尝试后过期。"],
            limitations=["Remote Browser Runner 任务在完成采集证据前已过期。"],
            execution_boundary=execution_boundary(record, lease_recovered=True),
        )
        return replace(
            record,
            status="best_effort",
            result=result,
            error=result.page_errors[0],
            updated_at=timestamp,
            finished_at=timestamp,
            lease_owner=None,
            lease_expires_at=None,
            lease_recovered=True,
        )
    return replace(
        record,
        status="queued",
        updated_at=timestamp,
        lease_owner=None,
        lease_expires_at=None,
        next_run_at=timestamp,
        error="上一个 Browser Runner 租约已过期；运行任务已返回队列。",
        lease_recovered=True,
    )


def create_queue_backend(
    *,
    backend: str | None = None,
    db_path: Path | str | None = None,
    workdir: Path | str | None = None,
    database_url: str | None = None,
    engine: Engine | None = None,
) -> BrowserRunQueueBackend:
    selected = normalize_queue_backend(backend or os.getenv(BROWSER_RUNNER_QUEUE_BACKEND_ENV, "sqlite"))
    if selected == "postgresql":
        return SqlAlchemyBrowserRunQueueBackend(database_url=database_url, engine=engine)
    root = Path(workdir or os.getenv(BROWSER_RUNNER_WORKDIR_ENV, tempfile.gettempdir()))
    path = Path(db_path or os.getenv(BROWSER_RUNNER_DB_PATH_ENV, root / "browser-runs.sqlite3"))
    return SQLiteBrowserRunQueueBackend(path)


def normalize_queue_backend(value: str | None) -> str:
    normalized = (value or "sqlite").strip().lower()
    if normalized in {"postgres", "postgresql", "shared-db", "shared_db", "database"}:
        return "postgresql"
    return "sqlite"


class BrowserRunnerQueue:
    def __init__(
        self,
        *,
        browser_adapter: BrowserSmokeAdapter | None = None,
        backend: BrowserRunQueueBackend | None = None,
        queue_backend: str | None = None,
        max_workers: int | None = None,
        workdir: Path | str | None = None,
        db_path: Path | str | None = None,
        database_url: str | None = None,
        engine: Engine | None = None,
        max_attempts: int | None = None,
        lease_seconds: int | None = None,
        retry_backoff_seconds: float | None = None,
        poll_seconds: float | None = None,
        auto_start: bool = True,
    ) -> None:
        self.browser_adapter = browser_adapter or PlaywrightBrowserAdapter()
        self.max_workers = max_workers or parse_int_env(BROWSER_RUNNER_WORKERS_ENV, DEFAULT_BROWSER_RUNNER_WORKERS)
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self.workdir = Path(workdir or os.getenv(BROWSER_RUNNER_WORKDIR_ENV, tempfile.gettempdir()))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.backend = backend or create_queue_backend(
            backend=queue_backend,
            db_path=db_path,
            workdir=self.workdir,
            database_url=database_url,
            engine=engine,
        )
        self.queue_backend = self.backend.name
        self.max_attempts = max_attempts or parse_int_env(
            BROWSER_RUNNER_MAX_ATTEMPTS_ENV,
            DEFAULT_BROWSER_RUNNER_MAX_ATTEMPTS,
        )
        self.lease_seconds = lease_seconds or parse_int_env(
            BROWSER_RUNNER_LEASE_SECONDS_ENV,
            DEFAULT_BROWSER_RUNNER_LEASE_SECONDS,
        )
        self.retry_backoff_seconds = (
            retry_backoff_seconds
            if retry_backoff_seconds is not None
            else parse_float_env(BROWSER_RUNNER_RETRY_BACKOFF_SECONDS_ENV, DEFAULT_BROWSER_RUNNER_RETRY_BACKOFF_SECONDS)
        )
        self.poll_seconds = poll_seconds if poll_seconds is not None else parse_float_env(
            BROWSER_RUNNER_POLL_SECONDS_ENV,
            DEFAULT_BROWSER_RUNNER_POLL_SECONDS,
        )
        self.max_queue_age_ms = parse_non_negative_int_env(
            BROWSER_RUNNER_MAX_QUEUE_AGE_MS_ENV,
            DEFAULT_BROWSER_RUNNER_MAX_QUEUE_AGE_MS,
        )
        self.max_claim_latency_ms = parse_non_negative_int_env(
            BROWSER_RUNNER_MAX_CLAIM_LATENCY_MS_ENV,
            DEFAULT_BROWSER_RUNNER_MAX_CLAIM_LATENCY_MS,
        )
        self.max_expired_running = parse_non_negative_int_env(
            BROWSER_RUNNER_MAX_EXPIRED_RUNNING_ENV,
            DEFAULT_BROWSER_RUNNER_MAX_EXPIRED_RUNNING,
        )
        self.max_retry_rate = parse_float_env(
            BROWSER_RUNNER_MAX_RETRY_RATE_ENV,
            DEFAULT_BROWSER_RUNNER_MAX_RETRY_RATE,
        )
        self.worker_id = f"browser-runner-{os.getpid()}-{uuid4().hex[:8]}"
        self.auto_start = auto_start
        self._lock = threading.Lock()
        self._submitted: set[str] = set()
        self._stop = threading.Event()
        self._scheduler = threading.Thread(target=self._scheduler_loop, name="browser-runner-scheduler", daemon=True)
        self._scheduler_started = False
        self.backend.initialize()
        self.recover_expired_leases(schedule=False)
        self.ops_store = None
        try:
            self.ops_store = self._create_ops_store(database_url=database_url, engine=engine)
        except Exception:
            self.ops_store = None
        self._record_ops_heartbeat()
        if self.auto_start:
            self._scheduler.start()
            self._scheduler_started = True
            self._schedule_due_runs()

    def submit(self, request: BrowserRunRequest) -> BrowserRunSummary:
        run_id = f"browser_run_{uuid4().hex[:12]}"
        now = utc_now()
        record = BrowserRunRecord(
            id=run_id,
            request=request,
            status="queued",
            created_at=now,
            updated_at=now,
            next_run_at=now,
            max_attempts=self.max_attempts,
            worker_id=self.worker_id,
            queue_backend=self.queue_backend,
        )
        self.backend.insert(record)
        if self.auto_start:
            self._schedule_due_runs()
        return record.summary()

    def get(self, run_id: str) -> BrowserRunSummary | None:
        record = self.backend.get(run_id)
        return record.summary() if record else None

    def metrics(self) -> BrowserRunnerQueueMetrics:
        return self._metrics_snapshot()

    def health(self) -> BrowserRunnerQueueHealth:
        status, metrics, alerts = self._ops_snapshot()
        self._record_ops_heartbeat(status=status, metrics=metrics, alerts=alerts)
        return BrowserRunnerQueueHealth(
            status=status,
            deployment_profile=DEPLOYMENT_PROFILE.status,
            worker_id=self.worker_id,
            max_workers=self.max_workers,
            max_attempts=self.max_attempts,
            lease_seconds=self.lease_seconds,
            retry_backoff_seconds=self.retry_backoff_seconds,
            poll_seconds=self.poll_seconds,
            metrics=metrics,
            alerts=alerts,
        )

    def close(self) -> None:
        self._stop.set()
        if self._scheduler_started:
            self._scheduler.join(timeout=max(1.0, self.poll_seconds + 1))
        self.executor.shutdown(wait=True, cancel_futures=False)
        if getattr(self, "ops_store", None) is not None:
            self.ops_store.close()
        self.backend.close()

    def recover_expired_leases(self, *, now: str | None = None, schedule: bool = True) -> list[BrowserRunSummary]:
        timestamp = now or utc_now()
        recovered = self.backend.recover_expired_leases(
            now=timestamp,
            submitted_run_ids=set(self._submitted),
            execution_boundary=self._execution_boundary,
        )
        if schedule and self.auto_start:
            self._schedule_due_runs()
        return [record.summary() for record in recovered]

    def _execute(self, run_id: str, record: BrowserRunRecord | None = None) -> None:
        record = record or self._claim(run_id)
        if record is None:
            self._discard_submitted(run_id)
            return
        try:
            result = self._capture(record)
            self._update(
                run_id,
                status=result.status if result.status in {"pass", "fail", "best_effort"} else "best_effort",
                result=result,
                finished_at=utc_now(),
                lease_owner=None,
                lease_expires_at=None,
            )
        except Exception as error:
            current = self.backend.get(run_id) or record
            failure_class = classify_browser_runner_error(error)
            retryable = failure_class not in {"invalid_input", "policy_denied", "sandbox_denied"}
            if retryable and current.attempt < current.max_attempts:
                next_run_at = (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, self.retry_backoff_seconds))).isoformat()
                self._update(
                    run_id,
                    status="queued",
                    error=str(error),
                    lease_owner=None,
                    lease_expires_at=None,
                    next_run_at=next_run_at,
                )
                return
            result = BrowserRunResult(
                status="best_effort",
                failure_class=failure_class,
                page_errors=[str(error)],
                limitations=["Browser Runner 队列任务在完成采集证据前执行失败。"],
                execution_boundary=self._execution_boundary(current),
            )
            self._update(
                run_id,
                status="best_effort",
                result=result,
                error=str(error),
                finished_at=utc_now(),
                lease_owner=None,
                lease_expires_at=None,
            )
        finally:
            self._record_ops_heartbeat()
            self._discard_submitted(run_id)
            if self.auto_start:
                self._schedule_due_runs()

    def _capture(self, record: BrowserRunRecord) -> BrowserRunResult:
        limitations: list[str] = []
        request = record.request
        with tempfile.TemporaryDirectory(prefix="ai-jsunpack-browser-run-", dir=self.workdir) as temp_dir:
            temp_root = Path(temp_dir)
            screenshot_path = temp_root / "runtime-screenshot.png"
            source_root: Path | None = None
            if request.source_archive is not None:
                source_root = temp_root / "source"
                source_root.mkdir(parents=True, exist_ok=True)
                _extract_source_archive(request.source_archive, source_root)

            with _entry_url(source_root=source_root, source_archive=request.source_archive, fallback_url=request.entry_url) as entry_url:
                try:
                    capture = self.browser_adapter.capture(
                        BrowserSmokeRequest(
                            entry_url=entry_url,
                            screenshot_path=screenshot_path,
                            timeout_ms=request.timeout_ms,
                            wait_for_selector=request.wait_for_selector,
                            scenario=request.scenario,
                            network_policy=request.network_policy,
                            viewport=request.viewport,
                        )
                    )
                    status = _status_for_capture(capture)
                    failure_class: FailureClass = "none" if status == "pass" else "runtime_error"
                except RuntimeSmokeError as error:
                    capture = BrowserSmokeCapture(page_errors=[str(error)])
                    status = "best_effort"
                    failure_class = error.failure_class
                    limitations.append("Remote Browser Runner 未能完成采集。")

            screenshot_base64 = None
            if screenshot_path.exists():
                screenshot_base64 = base64.b64encode(screenshot_path.read_bytes()).decode("ascii")
            else:
                limitations.append("Remote Browser Runner 已完成，但未生成截图。")

        return BrowserRunResult(
            status=status,
            failure_class=failure_class,
            console_messages=capture.console_messages,
            console_errors=capture.console_errors,
            page_errors=capture.page_errors,
            failed_requests=capture.failed_requests,
            responses=capture.responses,
            assertion_failures=capture.assertion_failures,
            dom_summary=capture.dom_summary,
            screenshot_base64=screenshot_base64,
            limitations=[*limitations, "浏览器执行位于 Remote Browser Runner 服务边界内。"],
            execution_boundary=self._execution_boundary(record),
        )

    def _execution_boundary(self, record: BrowserRunRecord, *, lease_recovered: bool | None = None) -> dict[str, Any]:
        metrics = self._metrics_snapshot()
        alerts = self._alerts(metrics)
        return {
            "runnerKind": "remote_browser_runner",
            "enforcement": "remote_isolated",
            "serviceRole": "browser-runner",
            "remoteRunId": record.id,
            "auth": "bearer_hmac",
            "artifactExchange": "worker_request_archive_and_worker_registered_runtime_artifacts",
            "queueBackend": record.queue_backend,
            "maxWorkers": self.max_workers,
            "runAttempt": record.attempt,
            "maxAttempts": record.max_attempts,
            "leaseSeconds": self.lease_seconds,
            "retryBackoffSeconds": self.retry_backoff_seconds,
            "leaseRecovered": record.lease_recovered if lease_recovered is None else lease_recovered,
            "queueLength": metrics.queued_count,
            "runningCount": metrics.running_count,
            "terminalCount": metrics.terminal_count,
            "totalCount": metrics.total_count,
            "oldestQueuedAgeMs": metrics.oldest_queued_age_ms,
            "claimLatencyMs": metrics.claim_latency_ms,
            "averageRunDurationMs": metrics.average_run_duration_ms,
            "retryRate": metrics.retry_rate,
            "leaseRecoveryCount": metrics.lease_recovery_count,
            "expiredRunningCount": metrics.expired_running_count,
            "backendHealthStatus": metrics.backend_status,
            "backendError": metrics.backend_error,
            "alerts": [alert.model_dump(by_alias=True) for alert in alerts],
        }

    def _metrics_snapshot(self) -> BrowserRunnerQueueMetrics:
        now = utc_now()
        try:
            return self.backend.stats(now=now)
        except Exception as error:
            return BrowserRunnerQueueMetrics(
                checked_at=now,
                queue_backend=self.queue_backend,
                backend_status="degraded",
                backend_error=str(error),
                queued_count=0,
                running_count=0,
                terminal_count=0,
                total_count=0,
                oldest_queued_age_ms=None,
                claim_latency_ms=None,
                average_run_duration_ms=None,
                retry_rate=0.0,
                lease_recovery_count=0,
                expired_running_count=0,
            )

    def _alerts(self, metrics: BrowserRunnerQueueMetrics) -> list[BrowserRunnerQueueAlert]:
        alerts: list[BrowserRunnerQueueAlert] = []
        if metrics.backend_status != "ok":
            alerts.append(
                BrowserRunnerQueueAlert(
                    code="backend_unhealthy",
                    severity="critical",
                    message="Browser Runner 队列后端健康检查失败。",
                    field="backendStatus",
                    value=metrics.backend_status,
                    threshold="ok",
                )
            )
        if metrics.queued_count > self.max_workers and (metrics.claim_latency_ms or 0) > 0:
            alerts.append(
                BrowserRunnerQueueAlert(
                    code="queue_backlog",
                    severity="warning",
                    message="Browser Runner 的 queued run 数超过本地 Worker concurrency。",
                    field="queuedCount",
                    value=metrics.queued_count,
                    threshold=self.max_workers,
                )
            )
        if metrics.oldest_queued_age_ms is not None and metrics.oldest_queued_age_ms > self.max_queue_age_ms:
            alerts.append(
                BrowserRunnerQueueAlert(
                    code="queue_age_high",
                    severity="warning",
                    message="最早排队的 Browser Runner 请求已超过配置的等待时长阈值。",
                    field="oldestQueuedAgeMs",
                    value=metrics.oldest_queued_age_ms,
                    threshold=self.max_queue_age_ms,
                )
            )
        if metrics.claim_latency_ms is not None and metrics.claim_latency_ms > self.max_claim_latency_ms:
            alerts.append(
                BrowserRunnerQueueAlert(
                    code="claim_latency_high",
                    severity="warning",
                    message="Browser Runner 队列的领取延迟已超过配置阈值。",
                    field="claimLatencyMs",
                    value=metrics.claim_latency_ms,
                    threshold=self.max_claim_latency_ms,
                )
            )
        if metrics.expired_running_count > self.max_expired_running:
            alerts.append(
                BrowserRunnerQueueAlert(
                    code="expired_running_leases",
                    severity="critical",
                    message="Browser Runner 存在租约已过期的运行中请求。",
                    field="expiredRunningCount",
                    value=metrics.expired_running_count,
                    threshold=self.max_expired_running,
                )
            )
        if metrics.retry_rate > self.max_retry_rate:
            alerts.append(
                BrowserRunnerQueueAlert(
                    code="retry_rate_high",
                    severity="warning",
                    message="Browser Runner 重试率已超过配置阈值。",
                    field="retryRate",
                    value=metrics.retry_rate,
                    threshold=self.max_retry_rate,
                )
            )
        return alerts

    def _ops_snapshot(self) -> tuple[str, BrowserRunnerQueueMetrics, list[BrowserRunnerQueueAlert]]:
        metrics = self._metrics_snapshot()
        alerts = self._alerts(metrics)
        status = "degraded" if metrics.backend_status == "degraded" or alerts else "ok"
        return status, metrics, alerts

    def _create_ops_store(self, *, database_url: str | None, engine: Engine | None):
        shared_engine = engine if engine is not None else getattr(self.backend, "engine", None)
        if shared_engine is not None:
            return create_store(
                engine=shared_engine,
                artifact_root=self.workdir / "ops-artifacts",
            )
        metadata_path = self.workdir / "ops-metadata.db"
        shared_database_url = database_url or f"sqlite:///{metadata_path.as_posix()}"
        return create_store(
            database_url=shared_database_url,
            artifact_root=self.workdir / "ops-artifacts",
        )

    def _record_ops_heartbeat(
        self,
        *,
        status: str | None = None,
        metrics: BrowserRunnerQueueMetrics | None = None,
        alerts: list[BrowserRunnerQueueAlert] | None = None,
    ) -> None:
        store = getattr(self, "ops_store", None)
        if store is None:
            return
        try:
            current_metrics = metrics or self._metrics_snapshot()
            current_alerts = alerts or self._alerts(current_metrics)
            current_status = status or ("degraded" if current_metrics.backend_status == "degraded" or current_alerts else "ok")
            store.record_ops_heartbeat(
                OpsHeartbeatRequest(
                    service_role="browser-runner",
                    instance_id=self.worker_id,
                    status=current_status,
                    ttl_seconds=max(int(self.lease_seconds * 2), 90),
                    metrics={
                        "queueBackend": current_metrics.queue_backend,
                        "backendStatus": current_metrics.backend_status,
                        "backendError": current_metrics.backend_error,
                        "queuedCount": current_metrics.queued_count,
                        "runningCount": current_metrics.running_count,
                        "terminalCount": current_metrics.terminal_count,
                        "totalCount": current_metrics.total_count,
                        "oldestQueuedAgeMs": current_metrics.oldest_queued_age_ms,
                        "claimLatencyMs": current_metrics.claim_latency_ms,
                        "averageRunDurationMs": current_metrics.average_run_duration_ms,
                        "retryRate": current_metrics.retry_rate,
                        "leaseRecoveryCount": current_metrics.lease_recovery_count,
                        "expiredRunningCount": current_metrics.expired_running_count,
                        "maxWorkers": self.max_workers,
                        "maxAttempts": self.max_attempts,
                        "leaseSeconds": self.lease_seconds,
                        "retryBackoffSeconds": self.retry_backoff_seconds,
                        "pollSeconds": self.poll_seconds,
                    },
                    alerts=[
                        OpsAlert(
                            code=alert.code,
                            severity=alert.severity,
                            message=alert.message,
                            field=alert.field,
                            value=alert.value,
                            threshold=alert.threshold,
                            service_role="browser-runner",
                            instance_id=self.worker_id,
                            checked_at=current_metrics.checked_at,
                        )
                        for alert in current_alerts
                    ],
                    metadata={
                        "deploymentProfile": DEPLOYMENT_PROFILE.status,
                        "queueBackend": current_metrics.queue_backend,
                        "workerId": self.worker_id,
                    },
                )
            )
        except Exception:
            return

    def _update(self, run_id: str, **changes) -> None:
        self.backend.update(run_id, **changes)

    def _claim(self, run_id: str) -> BrowserRunRecord | None:
        return self.backend.claim(run_id, worker_id=self.worker_id, lease_seconds=self.lease_seconds)

    def _scheduler_loop(self) -> None:
        while not self._stop.wait(max(0.05, self.poll_seconds)):
            self.recover_expired_leases(schedule=False)
            self._schedule_due_runs()

    def _schedule_due_runs(self) -> None:
        now = utc_now()
        with self._lock:
            while len(self._submitted) < self.max_workers:
                record = self.backend.claim_due(
                    now=now,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                    excluded_run_ids=self._submitted,
                )
                if record is None:
                    break
                run_id = record.id
                self._submitted.add(run_id)
                self.executor.submit(self._execute, run_id, record)

    def _discard_submitted(self, run_id: str) -> None:
        with self._lock:
            self._submitted.discard(run_id)


def create_app(*, queue: BrowserRunnerQueue | None = None, adapter: BrowserSmokeAdapter | None = None) -> FastAPI:
    app = FastAPI(title="AI JS Unpack Browser Runner", version="0.1.0")
    app.state.browser_runner_queue = queue or BrowserRunnerQueue(browser_adapter=adapter)

    @app.get("/health", response_model=BrowserRunnerQueueHealth)
    def health() -> BrowserRunnerQueueHealth:
        return app.state.browser_runner_queue.health()

    @app.post("/browser-runs", response_model=BrowserRunSummary)
    def create_browser_run(
        request: BrowserRunRequest,
        access: AccessContext = Depends(require_access),
    ) -> BrowserRunSummary:
        require_worker_service(access)
        return app.state.browser_runner_queue.submit(request)

    @app.get("/browser-runs/metrics", response_model=BrowserRunnerQueueMetrics)
    def browser_run_metrics(
        access: AccessContext = Depends(require_access),
    ) -> BrowserRunnerQueueMetrics:
        require_worker_service(access)
        return app.state.browser_runner_queue.metrics()

    @app.get("/browser-runs/{run_id}", response_model=BrowserRunSummary)
    def get_browser_run(
        run_id: str,
        access: AccessContext = Depends(require_access),
    ) -> BrowserRunSummary:
        require_worker_service(access)
        summary = app.state.browser_runner_queue.get(run_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="未找到浏览器运行记录")
        return summary

    return app


def require_worker_service(access: AccessContext) -> None:
    if access.kind != "service" or not access.has_service_role(SERVICE_ROLE_WORKER):
        raise HTTPException(status_code=403, detail="Browser Runner 需要 Worker service credential")


def _extract_source_archive(source_archive: BrowserRunSourceArchive, target_root: Path) -> None:
    content = base64.b64decode(source_archive.content_base64.encode("ascii"))
    archive_path = target_root.parent / "source.zip"
    archive_path.write_bytes(content)
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            relative = _safe_source_relative_path(member.filename)
            if member.is_dir():
                continue
            output = target_root.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.read(member))


@contextmanager
def _entry_url(
    *,
    source_root: Path | None,
    source_archive: BrowserRunSourceArchive | None,
    fallback_url: str,
) -> Iterator[str]:
    if source_root is None or source_archive is None:
        yield fallback_url
        return
    handler = partial(_QuietStaticHandler, directory=str(source_root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        entry_path = _safe_source_relative_path(source_archive.entry_path).as_posix()
        yield f"http://{host}:{port}/{quote(entry_path)}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _status_for_capture(capture: BrowserSmokeCapture) -> RunStatus:
    if capture.console_errors or capture.page_errors or capture.failed_requests or capture.assertion_failures:
        return "fail"
    return "pass"


def _safe_source_relative_path(file_path: str) -> PurePosixPath:
    if not file_path or "\0" in file_path:
        raise ValueError(f"源归档成员路径不安全：{file_path}")
    normalized = file_path.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//") or _has_windows_drive_prefix(normalized):
        raise ValueError(f"源归档成员路径不安全：{file_path}")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"源归档成员路径不安全：{file_path}")
    return PurePosixPath(*parts)


def _has_windows_drive_prefix(file_path: str) -> bool:
    return len(file_path) >= 2 and file_path[1] == ":" and file_path[0].isalpha()


def classify_browser_runner_error(error: Exception) -> FailureClass:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, (ValueError, zipfile.BadZipFile)):
        return "invalid_input"
    if isinstance(error, PermissionError):
        return "sandbox_denied"
    return "runtime_error"


def parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_optional_timestamp(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


def parse_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return max(1, parsed)


def parse_non_negative_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return max(0, parsed)


def parse_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        parsed = float(raw_value)
    except ValueError:
        return default
    return max(0.0, parsed)


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return


app = create_app()
