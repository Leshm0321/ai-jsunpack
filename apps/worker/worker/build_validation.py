from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence
from uuid import uuid4

from apps.api.app.models import (
    ArtifactRecord,
    EvidenceRef,
    FailureClass,
    JobStatus,
    ReviewRun,
    ReviewType,
    RunStatus,
)
from packages.sandbox import LocalSandboxRunner, SandboxCommand, SandboxPolicy, SandboxResult


DEFAULT_BUILD_COMMAND = ("npm", "run", "build")
DEFAULT_TYPECHECK_COMMAND = ("npm", "run", "typecheck")


BuildStage = Literal["building", "typechecking"]


class BuildValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuildValidationStageResult:
    log_artifact: ArtifactRecord
    review_artifact: ArtifactRecord
    review_run: ReviewRun
    message: str

    @property
    def artifact_ids(self) -> list[str]:
        return [self.log_artifact.id, self.review_artifact.id]


@dataclass(frozen=True)
class BuildValidationResult:
    build: BuildValidationStageResult
    typecheck: BuildValidationStageResult

    @property
    def artifact_ids(self) -> list[str]:
        return [*self.build.artifact_ids, *self.typecheck.artifact_ids]


class BuildValidationRunner:
    """Runs build and typecheck checks through the sandbox evidence surface."""

    def __init__(
        self,
        sandbox_runner: LocalSandboxRunner | None = None,
        build_command: Sequence[str] = DEFAULT_BUILD_COMMAND,
        typecheck_command: Sequence[str] = DEFAULT_TYPECHECK_COMMAND,
    ) -> None:
        self.build_command = tuple(build_command)
        self.typecheck_command = tuple(typecheck_command)
        self.sandbox_runner = sandbox_runner or LocalSandboxRunner(
            SandboxPolicy(
                allowed_commands=(self.build_command, self.typecheck_command),
                timeout_ms=120_000,
                output_limit_bytes=128 * 1024,
            )
        )

    def run(
        self,
        *,
        job_id: str,
        store,
        parent_artifact_ids: list[str] | None = None,
        project_path: Path | str | None = None,
    ) -> BuildValidationResult:
        parents = parent_artifact_ids or []
        generated_project_artifact = self._latest_generated_project(job_id=job_id, store=store)
        source_project_path = (
            Path(project_path) if project_path is not None else self._project_path(generated_project_artifact)
        )
        source_project_parents = [*parents]
        if generated_project_artifact is not None:
            source_project_parents.append(generated_project_artifact.id)

        build = self._run_stage(
            job_id=job_id,
            store=store,
            stage="building",
            review_type="build",
            command=self.build_command,
            command_failure_class="build_error",
            source_project_path=source_project_path,
            parent_artifact_ids=source_project_parents,
        )
        typecheck = self._run_stage(
            job_id=job_id,
            store=store,
            stage="typechecking",
            review_type="typecheck",
            command=self.typecheck_command,
            command_failure_class="type_error",
            source_project_path=source_project_path,
            parent_artifact_ids=[*source_project_parents, *build.artifact_ids],
        )
        return BuildValidationResult(build=build, typecheck=typecheck)

    def _run_stage(
        self,
        *,
        job_id: str,
        store,
        stage: BuildStage,
        review_type: ReviewType,
        command: tuple[str, ...],
        command_failure_class: FailureClass,
        source_project_path: Path | None,
        parent_artifact_ids: list[str],
    ) -> BuildValidationStageResult:
        store.update_status(job_id, stage)
        if source_project_path is None or not source_project_path.is_dir():
            return self._persist_best_effort(
                job_id=job_id,
                store=store,
                stage=stage,
                review_type=review_type,
                command=command,
                parent_artifact_ids=parent_artifact_ids,
                limitation="No generated_project directory is available; deterministic writer output is required before real sandbox validation can run.",
            )

        with self.sandbox_runner.attempt_workspace() as workspace:
            project_root = workspace / "project"
            shutil.copytree(source_project_path, project_root, dirs_exist_ok=True)
            result = self.sandbox_runner.run_in_workspace(
                SandboxCommand(
                    executable=command[0],
                    args=tuple(command[1:]),
                    working_directory="project",
                    failure_class=command_failure_class,
                ),
                workspace,
            )
        return self._persist_sandbox_result(
            job_id=job_id,
            store=store,
            stage=stage,
            review_type=review_type,
            command=command,
            result=result,
            parent_artifact_ids=parent_artifact_ids,
        )

    def _persist_best_effort(
        self,
        *,
        job_id: str,
        store,
        stage: BuildStage,
        review_type: ReviewType,
        command: tuple[str, ...],
        parent_artifact_ids: list[str],
        limitation: str,
    ) -> BuildValidationStageResult:
        decision = f"Sandbox {review_type} validation recorded as best-effort because no generated project is available."
        log_payload = self._log_payload(
            job_id=job_id,
            stage=stage,
            review_type=review_type,
            status="best_effort",
            decision=decision,
            command=list(command),
            stdout="",
            stderr="",
            exit_code=None,
            duration_ms=0,
            failure_class="none",
            timed_out=False,
            output_truncated=False,
            working_directory=None,
            limitations=[limitation],
        )
        log_artifact = self._write_log_artifact(
            job_id=job_id,
            store=store,
            stage=stage,
            review_type=review_type,
            payload=log_payload,
            parent_artifact_ids=parent_artifact_ids,
        )
        review_run = self._review_run(
            job_id=job_id,
            review_type=review_type,
            status="best_effort",
            decision=decision,
            failure_class="none",
            log_artifact=log_artifact,
        )
        review_artifact = self._write_review_artifact(
            job_id=job_id,
            store=store,
            stage=stage,
            review_type=review_type,
            review_run=review_run,
            parent_artifact_ids=[*parent_artifact_ids, log_artifact.id],
        )
        return BuildValidationStageResult(
            log_artifact=log_artifact,
            review_artifact=review_artifact,
            review_run=review_run,
            message=decision,
        )

    def _persist_sandbox_result(
        self,
        *,
        job_id: str,
        store,
        stage: BuildStage,
        review_type: ReviewType,
        command: tuple[str, ...],
        result: SandboxResult,
        parent_artifact_ids: list[str],
    ) -> BuildValidationStageResult:
        status = self._status_for_result(result)
        decision = self._decision_for_result(review_type=review_type, status=status, result=result)
        log_payload = self._log_payload(
            job_id=job_id,
            stage=stage,
            review_type=review_type,
            status=status,
            decision=decision,
            command=list(command),
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            failure_class=result.failure_class,
            timed_out=result.timed_out,
            output_truncated=result.output_truncated,
            working_directory=result.working_directory,
            limitations=[],
        )
        log_artifact = self._write_log_artifact(
            job_id=job_id,
            store=store,
            stage=stage,
            review_type=review_type,
            payload=log_payload,
            parent_artifact_ids=parent_artifact_ids,
        )
        review_run = self._review_run(
            job_id=job_id,
            review_type=review_type,
            status=status,
            decision=decision,
            failure_class=result.failure_class,
            log_artifact=log_artifact,
        )
        review_artifact = self._write_review_artifact(
            job_id=job_id,
            store=store,
            stage=stage,
            review_type=review_type,
            review_run=review_run,
            parent_artifact_ids=[*parent_artifact_ids, log_artifact.id],
        )
        return BuildValidationStageResult(
            log_artifact=log_artifact,
            review_artifact=review_artifact,
            review_run=review_run,
            message=decision,
        )

    def _latest_generated_project(self, *, job_id: str, store) -> ArtifactRecord | None:
        artifacts = store.list_artifacts(job_id, kind="generated_project")
        return artifacts[-1] if artifacts else None

    def _project_path(self, artifact: ArtifactRecord | None) -> Path | None:
        if artifact is None:
            return None
        path = Path(artifact.storage_uri)
        if path.is_dir():
            return path
        return None

    def _status_for_result(self, result: SandboxResult) -> RunStatus:
        if result.failure_class == "none" and result.exit_code == 0:
            return "pass"
        return "fail"

    def _decision_for_result(self, *, review_type: ReviewType, status: RunStatus, result: SandboxResult) -> str:
        if status == "pass":
            return f"Sandbox {review_type} validation passed."
        detail = result.denied_reason or result.stderr or result.stdout or "sandbox command failed"
        return f"Sandbox {review_type} validation failed with {result.failure_class}: {detail}"

    def _log_payload(
        self,
        *,
        job_id: str,
        stage: BuildStage,
        review_type: ReviewType,
        status: RunStatus,
        decision: str,
        command: list[str],
        stdout: str,
        stderr: str,
        exit_code: int | None,
        duration_ms: int,
        failure_class: FailureClass,
        timed_out: bool,
        output_truncated: bool,
        working_directory: str | None,
        limitations: list[str],
    ) -> dict:
        return {
            "kind": "build_log",
            "jobId": job_id,
            "stage": stage,
            "reviewType": review_type,
            "status": status,
            "decision": decision,
            "command": command,
            "stdout": stdout,
            "stderr": stderr,
            "exitCode": exit_code,
            "durationMs": duration_ms,
            "failureClass": failure_class,
            "timedOut": timed_out,
            "outputTruncated": output_truncated,
            "workingDirectory": working_directory,
            "networkPolicy": self.sandbox_runner.policy.network_policy,
            "limitations": limitations,
        }

    def _write_log_artifact(
        self,
        *,
        job_id: str,
        store,
        stage: JobStatus,
        review_type: ReviewType,
        payload: dict,
        parent_artifact_ids: list[str],
    ) -> ArtifactRecord:
        return store.write_artifact(
            job_id,
            kind="build_log",
            stage=stage,
            filename=f"{review_type}-log.json",
            content=self._json_bytes(payload),
            content_type="application/json",
            producer="worker.build_validation",
            parent_artifact_ids=parent_artifact_ids,
        )

    def _write_review_artifact(
        self,
        *,
        job_id: str,
        store,
        stage: JobStatus,
        review_type: ReviewType,
        review_run: ReviewRun,
        parent_artifact_ids: list[str],
    ) -> ArtifactRecord:
        return store.write_artifact(
            job_id,
            kind="review_run",
            stage=stage,
            filename=f"{review_type}-review-run.json",
            content=review_run.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.build_validation",
            parent_artifact_ids=parent_artifact_ids,
        )

    def _review_run(
        self,
        *,
        job_id: str,
        review_type: ReviewType,
        status: RunStatus,
        decision: str,
        failure_class: FailureClass,
        log_artifact: ArtifactRecord,
    ) -> ReviewRun:
        return ReviewRun(
            id=f"review_{uuid4().hex[:12]}",
            job_id=job_id,
            attempt=0,
            review_type=review_type,
            status=status,
            decision=decision,
            failure_class=failure_class,
            evidence_refs=[
                EvidenceRef(
                    artifact_id=log_artifact.id,
                    label=f"Sandbox {review_type} log",
                    locator="artifact:build_log",
                    excerpt=decision[:240],
                )
            ],
            repair_instruction_ids=[],
            logs_artifact_id=log_artifact.id,
        )

    def _json_bytes(self, payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
