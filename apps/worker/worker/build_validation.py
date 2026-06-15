from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence
from uuid import uuid4

from apps.api.app.models import (
    ArtifactRecord,
    EvidenceRef,
    FailureClass,
    JobStatus,
    RepairAction,
    RepairInstruction,
    ReviewRun,
    ReviewType,
    RunStatus,
)
from packages.sandbox import LocalSandboxRunner, SandboxCommand, SandboxPolicy, SandboxResult


DEFAULT_NODE_EXECUTABLE = shutil.which("node") or "node"
DEFAULT_NPM_EXECUTABLE = shutil.which("npm") or "npm"
DEFAULT_BUILD_COMMAND = (DEFAULT_NODE_EXECUTABLE, "scripts/build.mjs")
DEFAULT_TYPECHECK_COMMAND = (DEFAULT_NODE_EXECUTABLE, "scripts/typecheck.mjs")
DEFAULT_NPM_INSTALL_COMMAND = (
    DEFAULT_NPM_EXECUTABLE,
    "install",
    "--ignore-scripts",
    "--no-audit",
    "--no-fund",
)
DEFAULT_NPM_BUILD_COMMAND = (DEFAULT_NPM_EXECUTABLE, "run", "--ignore-scripts", "build")
DEFAULT_NPM_TYPECHECK_COMMAND = (DEFAULT_NPM_EXECUTABLE, "run", "--ignore-scripts", "typecheck")
DEFAULT_NPM_CHECK_COMMAND = (DEFAULT_NPM_EXECUTABLE, "run", "--ignore-scripts", "check")


BuildStage = Literal["building", "typechecking"]
BuildPhase = Literal["install", "build", "typecheck"]
CommandSource = Literal["configured", "npm_script", "fallback_shim", "npm_install", "missing"]


class BuildValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuildValidationConfig:
    max_attempts: int = 2
    install_dependencies: bool = False


@dataclass(frozen=True)
class BuildValidationLogResult:
    log_artifact: ArtifactRecord
    status: RunStatus
    failure_class: FailureClass
    message: str

    @property
    def artifact_ids(self) -> list[str]:
        return [self.log_artifact.id]


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
    install_logs: list[BuildValidationLogResult] = field(default_factory=list)
    repair_artifacts: list[ArtifactRecord] = field(default_factory=list)

    @property
    def artifact_ids(self) -> list[str]:
        return [
            *[artifact_id for log in self.install_logs for artifact_id in log.artifact_ids],
            *self.build.artifact_ids,
            *self.typecheck.artifact_ids,
            *[artifact.id for artifact in self.repair_artifacts],
        ]


@dataclass(frozen=True)
class CommandPlan:
    command: tuple[str, ...] | None
    phase: BuildPhase
    script_name: str | None
    command_source: CommandSource
    failure_class: FailureClass
    missing_reason: str | None = None


@dataclass(frozen=True)
class StageObservation:
    stage: BuildStage
    review_type: ReviewType
    attempt: int
    plan: CommandPlan
    status: RunStatus
    failure_class: FailureClass
    result: SandboxResult | None = None
    limitations: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status == "fail"


@dataclass(frozen=True)
class RepairOutcome:
    repair_artifact: ArtifactRecord
    applied_project_artifact: ArtifactRecord | None


class BuildValidationRunner:
    """Runs generated project install, build, typecheck, and repair evidence loops."""

    def __init__(
        self,
        sandbox_runner: LocalSandboxRunner | None = None,
        build_command: Sequence[str] = DEFAULT_BUILD_COMMAND,
        typecheck_command: Sequence[str] = DEFAULT_TYPECHECK_COMMAND,
    ) -> None:
        self.build_command = tuple(build_command)
        self.typecheck_command = tuple(typecheck_command)
        self.use_package_scripts = (
            self.build_command == DEFAULT_BUILD_COMMAND and self.typecheck_command == DEFAULT_TYPECHECK_COMMAND
        )
        self.sandbox_runner = sandbox_runner or LocalSandboxRunner(
            SandboxPolicy(
                allowed_commands=(
                    self.build_command,
                    self.typecheck_command,
                    DEFAULT_NPM_INSTALL_COMMAND,
                    DEFAULT_NPM_BUILD_COMMAND,
                    DEFAULT_NPM_TYPECHECK_COMMAND,
                    DEFAULT_NPM_CHECK_COMMAND,
                ),
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

        config = self._config(job_id=job_id, store=store)
        if source_project_path is None or not source_project_path.is_dir():
            build = self._persist_best_effort(
                job_id=job_id,
                store=store,
                stage="building",
                review_type="build",
                attempt=0,
                command=self.build_command,
                parent_artifact_ids=source_project_parents,
                limitation="No generated_project directory is available; deterministic writer output is required before real sandbox validation can run.",
            )
            typecheck = self._persist_best_effort(
                job_id=job_id,
                store=store,
                stage="typechecking",
                review_type="typecheck",
                attempt=0,
                command=self.typecheck_command,
                parent_artifact_ids=[*source_project_parents, *build.artifact_ids],
                limitation="No generated_project directory is available; deterministic writer output is required before real sandbox validation can run.",
            )
            return BuildValidationResult(build=build, typecheck=typecheck)

        install_logs: list[BuildValidationLogResult] = []
        repair_artifacts: list[ArtifactRecord] = []
        current_project_path = source_project_path
        current_parent_ids = source_project_parents
        last_build: BuildValidationStageResult | None = None
        last_typecheck: BuildValidationStageResult | None = None

        for attempt in range(config.max_attempts):
            with self.sandbox_runner.attempt_workspace() as workspace:
                project_root = workspace / "project"
                shutil.copytree(current_project_path, project_root, dirs_exist_ok=True)
                attempt_parent_ids = [*current_parent_ids]

                install_log = self._run_dependency_install(
                    job_id=job_id,
                    store=store,
                    workspace=workspace,
                    project_root=project_root,
                    attempt=attempt,
                    config=config,
                    parent_artifact_ids=attempt_parent_ids,
                )
                if install_log is not None:
                    install_logs.append(install_log)
                    attempt_parent_ids.extend(install_log.artifact_ids)

                build_observation = self._observe_stage(
                    job_id=job_id,
                    store=store,
                    workspace=workspace,
                    project_root=project_root,
                    stage="building",
                    review_type="build",
                    attempt=attempt,
                    command_plan=self._command_plan(project_root=project_root, review_type="build"),
                )
                typecheck_observation = self._observe_stage(
                    job_id=job_id,
                    store=store,
                    workspace=workspace,
                    project_root=project_root,
                    stage="typechecking",
                    review_type="typecheck",
                    attempt=attempt,
                    command_plan=self._command_plan(project_root=project_root, review_type="typecheck"),
                )

                failed_observations = [
                    observation for observation in (build_observation, typecheck_observation) if observation.failed
                ]
                repair_outcome: RepairOutcome | None = None
                if failed_observations and attempt + 1 < config.max_attempts:
                    repair_outcome = self._write_repair_instruction(
                        job_id=job_id,
                        store=store,
                        project_root=project_root,
                        attempt=attempt,
                        failed_observations=failed_observations,
                        parent_artifact_ids=attempt_parent_ids,
                    )
                    repair_artifacts.append(repair_outcome.repair_artifact)
                    if repair_outcome.applied_project_artifact is not None:
                        repair_artifacts.append(repair_outcome.applied_project_artifact)

                repair_instruction_ids = [repair_outcome.repair_artifact.id] if repair_outcome is not None else []
                last_build = self._persist_observation(
                    job_id=job_id,
                    store=store,
                    observation=build_observation,
                    parent_artifact_ids=attempt_parent_ids,
                    repair_instruction_ids=repair_instruction_ids if build_observation.failed else [],
                )
                last_typecheck = self._persist_observation(
                    job_id=job_id,
                    store=store,
                    observation=typecheck_observation,
                    parent_artifact_ids=[*attempt_parent_ids, *last_build.artifact_ids],
                    repair_instruction_ids=repair_instruction_ids if typecheck_observation.failed else [],
                )

                if not failed_observations:
                    break
                if repair_outcome is None or repair_outcome.applied_project_artifact is None:
                    break
                current_project_path = Path(repair_outcome.applied_project_artifact.storage_uri)
                current_parent_ids = [
                    *attempt_parent_ids,
                    repair_outcome.repair_artifact.id,
                    repair_outcome.applied_project_artifact.id,
                    *last_build.artifact_ids,
                    *last_typecheck.artifact_ids,
                ]

        if last_build is None or last_typecheck is None:
            raise BuildValidationError("Build validation did not produce build and typecheck review results.")
        return BuildValidationResult(
            build=last_build,
            typecheck=last_typecheck,
            install_logs=install_logs,
            repair_artifacts=repair_artifacts,
        )

    def _run_dependency_install(
        self,
        *,
        job_id: str,
        store,
        workspace: Path,
        project_root: Path,
        attempt: int,
        config: BuildValidationConfig,
        parent_artifact_ids: list[str],
    ) -> BuildValidationLogResult | None:
        package_json = self._read_package_json(project_root)
        if package_json is None or not self._has_declared_dependencies(package_json):
            return None

        command_plan = CommandPlan(
            command=DEFAULT_NPM_INSTALL_COMMAND,
            phase="install",
            script_name="install",
            command_source="npm_install",
            failure_class="install_failed",
        )
        if not config.install_dependencies:
            decision = (
                "Dependency installation skipped because buildValidation.installDependencies is not enabled; "
                "install is disabled by policy."
            )
            payload = self._log_payload(
                job_id=job_id,
                stage="building",
                review_type="build",
                phase="install",
                attempt=attempt,
                status="best_effort",
                decision=decision,
                command=list(DEFAULT_NPM_INSTALL_COMMAND),
                command_source="npm_install",
                script_name="install",
                stdout="",
                stderr="",
                exit_code=None,
                duration_ms=0,
                failure_class="dependency_missing",
                timed_out=False,
                output_truncated=False,
                working_directory=None,
                limitations=[
                    "The generated project declares dependencies, but dependency installation is disabled by policy."
                ],
                repair_instruction_ids=[],
            )
            log_artifact = self._write_log_artifact(
                job_id=job_id,
                store=store,
                stage="building",
                review_type="build",
                phase="install",
                attempt=attempt,
                payload=payload,
                parent_artifact_ids=parent_artifact_ids,
            )
            return BuildValidationLogResult(
                log_artifact=log_artifact,
                status="best_effort",
                failure_class="dependency_missing",
                message=decision,
            )

        result = self.sandbox_runner.run_in_workspace(
            SandboxCommand(
                executable=command_plan.command[0],
                args=tuple(command_plan.command[1:]),
                working_directory="project",
                failure_class="install_failed",
            ),
            workspace,
        )
        status = self._status_for_result(result)
        decision = self._decision_for_result(phase="install", status=status, result=result)
        payload = self._log_payload(
            job_id=job_id,
            stage="building",
            review_type="build",
            phase="install",
            attempt=attempt,
            status=status,
            decision=decision,
            command=list(command_plan.command),
            command_source=command_plan.command_source,
            script_name=command_plan.script_name,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            failure_class=result.failure_class,
            timed_out=result.timed_out,
            output_truncated=result.output_truncated,
            working_directory=result.working_directory,
            limitations=[],
            repair_instruction_ids=[],
        )
        log_artifact = self._write_log_artifact(
            job_id=job_id,
            store=store,
            stage="building",
            review_type="build",
            phase="install",
            attempt=attempt,
            payload=payload,
            parent_artifact_ids=parent_artifact_ids,
        )
        return BuildValidationLogResult(
            log_artifact=log_artifact,
            status=status,
            failure_class=result.failure_class,
            message=decision,
        )

    def _observe_stage(
        self,
        *,
        job_id: str,
        store,
        workspace: Path,
        project_root: Path,
        stage: BuildStage,
        review_type: ReviewType,
        attempt: int,
        command_plan: CommandPlan,
    ) -> StageObservation:
        store.update_status(job_id, stage)
        if command_plan.command is None:
            return StageObservation(
                stage=stage,
                review_type=review_type,
                attempt=attempt,
                plan=command_plan,
                status="best_effort",
                failure_class=command_plan.failure_class,
                limitations=[command_plan.missing_reason or "No validation command is available."],
            )

        result = self.sandbox_runner.run_in_workspace(
            SandboxCommand(
                executable=command_plan.command[0],
                args=tuple(command_plan.command[1:]),
                working_directory=str(project_root.relative_to(workspace)),
                failure_class=command_plan.failure_class,
            ),
            workspace,
        )
        return StageObservation(
            stage=stage,
            review_type=review_type,
            attempt=attempt,
            plan=command_plan,
            status=self._status_for_result(result),
            failure_class=result.failure_class,
            result=result,
        )

    def _persist_best_effort(
        self,
        *,
        job_id: str,
        store,
        stage: BuildStage,
        review_type: ReviewType,
        attempt: int,
        command: tuple[str, ...],
        parent_artifact_ids: list[str],
        limitation: str,
    ) -> BuildValidationStageResult:
        store.update_status(job_id, stage)
        phase: BuildPhase = "build" if review_type == "build" else "typecheck"
        observation = StageObservation(
            stage=stage,
            review_type=review_type,
            attempt=attempt,
            plan=CommandPlan(
                command=command,
                phase=phase,
                script_name=command[-1] if command else None,
                command_source="missing",
                failure_class="none",
                missing_reason=limitation,
            ),
            status="best_effort",
            failure_class="none",
            limitations=[limitation],
        )
        return self._persist_observation(
            job_id=job_id,
            store=store,
            observation=observation,
            parent_artifact_ids=parent_artifact_ids,
            repair_instruction_ids=[],
        )

    def _persist_observation(
        self,
        *,
        job_id: str,
        store,
        observation: StageObservation,
        parent_artifact_ids: list[str],
        repair_instruction_ids: list[str],
    ) -> BuildValidationStageResult:
        decision = self._decision_for_observation(observation)
        result = observation.result
        log_payload = self._log_payload(
            job_id=job_id,
            stage=observation.stage,
            review_type=observation.review_type,
            phase=observation.plan.phase,
            attempt=observation.attempt,
            status=observation.status,
            decision=decision,
            command=list(observation.plan.command or []),
            command_source=observation.plan.command_source,
            script_name=observation.plan.script_name,
            stdout=result.stdout if result is not None else "",
            stderr=result.stderr if result is not None else "",
            exit_code=result.exit_code if result is not None else None,
            duration_ms=result.duration_ms if result is not None else 0,
            failure_class=observation.failure_class,
            timed_out=result.timed_out if result is not None else False,
            output_truncated=result.output_truncated if result is not None else False,
            working_directory=result.working_directory if result is not None else None,
            limitations=observation.limitations,
            repair_instruction_ids=repair_instruction_ids,
        )
        log_artifact = self._write_log_artifact(
            job_id=job_id,
            store=store,
            stage=observation.stage,
            review_type=observation.review_type,
            phase=observation.plan.phase,
            attempt=observation.attempt,
            payload=log_payload,
            parent_artifact_ids=parent_artifact_ids,
        )
        review_run = self._review_run(
            job_id=job_id,
            review_type=observation.review_type,
            attempt=observation.attempt,
            status=observation.status,
            decision=decision,
            failure_class=observation.failure_class,
            log_artifact=log_artifact,
            repair_instruction_ids=repair_instruction_ids,
        )
        review_artifact = self._write_review_artifact(
            job_id=job_id,
            store=store,
            stage=observation.stage,
            review_type=observation.review_type,
            attempt=observation.attempt,
            review_run=review_run,
            parent_artifact_ids=[*parent_artifact_ids, log_artifact.id, *repair_instruction_ids],
        )
        return BuildValidationStageResult(
            log_artifact=log_artifact,
            review_artifact=review_artifact,
            review_run=review_run,
            message=decision,
        )

    def _write_repair_instruction(
        self,
        *,
        job_id: str,
        store,
        project_root: Path,
        attempt: int,
        failed_observations: list[StageObservation],
        parent_artifact_ids: list[str],
    ) -> RepairOutcome:
        store.update_status(job_id, "repairing")
        actions = self._repair_actions(project_root=project_root, failed_observations=failed_observations)
        status = "applied" if actions else "skipped"
        target_stage = failed_observations[0].stage
        failure_class = failed_observations[0].failure_class
        if actions:
            self._apply_repair_actions(project_root=project_root, actions=actions)
            decision = "Applied low-risk deterministic package script repair for generated project validation."
        else:
            decision = "No low-risk deterministic repair was available for the failed validation evidence."

        repair_instruction = RepairInstruction(
            id=f"repair_{uuid4().hex[:12]}",
            job_id=job_id,
            attempt=attempt + 1,
            target_stage=target_stage,
            failure_class=failure_class,
            input_artifact_ids=parent_artifact_ids,
            evidence_refs=self._repair_evidence_refs(parent_artifact_ids, failed_observations),
            actions=actions,
            status=status,
            risk_level="low" if actions else "medium",
            decision=decision,
        )
        repair_artifact = store.write_artifact(
            job_id,
            kind="repair_instruction",
            stage="repairing",
            filename=f"repair-instruction-attempt-{attempt + 1}.json",
            content=repair_instruction.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.build_validation",
            parent_artifact_ids=parent_artifact_ids,
            attempt=attempt + 1,
        )
        applied_project_artifact = None
        if actions:
            applied_project_artifact = store.register_artifact_path(
                job_id,
                kind="generated_project",
                stage="repairing",
                filename=f"generated-project-attempt-{attempt + 1}",
                source_path=project_root,
                content_type="application/vnd.ai-jsunpack.generated-project+directory",
                producer="worker.build_validation",
                parent_artifact_ids=[*parent_artifact_ids, repair_artifact.id],
                attempt=attempt + 1,
            )
        return RepairOutcome(repair_artifact=repair_artifact, applied_project_artifact=applied_project_artifact)

    def _repair_actions(
        self,
        *,
        project_root: Path,
        failed_observations: list[StageObservation],
    ) -> list[RepairAction]:
        package_json = self._read_package_json(project_root)
        if package_json is None:
            return []
        scripts = package_json.get("scripts")
        if not isinstance(scripts, dict):
            scripts = {}
        actions: list[RepairAction] = []
        failed_review_types = {observation.review_type for observation in failed_observations}
        if "build" in failed_review_types and "build" not in scripts and (project_root / "scripts" / "build.mjs").exists():
            actions.append(
                RepairAction(
                    action="add_package_script",
                    path="package.json:scripts.build",
                    value="node scripts/build.mjs",
                    reason="A generated build shim exists and package.json does not define scripts.build.",
                )
            )
        if (
            "typecheck" in failed_review_types
            and "typecheck" not in scripts
            and "check" not in scripts
            and (project_root / "scripts" / "typecheck.mjs").exists()
        ):
            actions.append(
                RepairAction(
                    action="add_package_script",
                    path="package.json:scripts.typecheck",
                    value="node scripts/typecheck.mjs",
                    reason="A generated typecheck shim exists and package.json does not define scripts.typecheck or scripts.check.",
                )
            )
        return actions

    def _apply_repair_actions(self, *, project_root: Path, actions: list[RepairAction]) -> None:
        package_path = project_root / "package.json"
        package_json = self._read_package_json(project_root) or {}
        scripts = package_json.get("scripts")
        if not isinstance(scripts, dict):
            scripts = {}
        for action in actions:
            if action.action == "add_package_script":
                script_name = action.path.rsplit(".", 1)[-1]
                scripts[script_name] = action.value
        package_json["scripts"] = scripts
        package_path.write_text(f"{json.dumps(package_json, ensure_ascii=False, indent=2, sort_keys=True)}\n", encoding="utf-8")

    def _command_plan(self, *, project_root: Path, review_type: ReviewType) -> CommandPlan:
        if review_type == "build":
            return self._build_command_plan(project_root)
        return self._typecheck_command_plan(project_root)

    def _build_command_plan(self, project_root: Path) -> CommandPlan:
        if not self.use_package_scripts:
            return CommandPlan(self.build_command, "build", self.build_command[-1], "configured", "build_error")
        package_json = self._read_package_json(project_root)
        scripts = package_json.get("scripts") if package_json else {}
        if isinstance(scripts, dict) and isinstance(scripts.get("build"), str):
            return CommandPlan(DEFAULT_NPM_BUILD_COMMAND, "build", "build", "npm_script", "build_error")
        if (project_root / "scripts" / "build.mjs").exists():
            return CommandPlan(DEFAULT_BUILD_COMMAND, "build", "scripts/build.mjs", "fallback_shim", "build_error")
        return CommandPlan(
            None,
            "build",
            "build",
            "missing",
            "build_error",
            "No scripts.build entry or scripts/build.mjs fallback exists in the generated project.",
        )

    def _typecheck_command_plan(self, project_root: Path) -> CommandPlan:
        if not self.use_package_scripts:
            return CommandPlan(
                self.typecheck_command,
                "typecheck",
                self.typecheck_command[-1],
                "configured",
                "type_error",
            )
        package_json = self._read_package_json(project_root)
        scripts = package_json.get("scripts") if package_json else {}
        if isinstance(scripts, dict) and isinstance(scripts.get("typecheck"), str):
            return CommandPlan(DEFAULT_NPM_TYPECHECK_COMMAND, "typecheck", "typecheck", "npm_script", "type_error")
        if isinstance(scripts, dict) and isinstance(scripts.get("check"), str):
            return CommandPlan(DEFAULT_NPM_CHECK_COMMAND, "typecheck", "check", "npm_script", "type_error")
        if (project_root / "scripts" / "typecheck.mjs").exists():
            return CommandPlan(DEFAULT_TYPECHECK_COMMAND, "typecheck", "scripts/typecheck.mjs", "fallback_shim", "type_error")
        return CommandPlan(
            None,
            "typecheck",
            "typecheck",
            "missing",
            "type_error",
            "No scripts.typecheck, scripts.check, or scripts/typecheck.mjs fallback exists in the generated project.",
        )

    def _config(self, *, job_id: str, store) -> BuildValidationConfig:
        job = store.get_job(job_id)
        raw_config = job.config if job is not None else {}
        scoped = raw_config.get("buildValidation") if isinstance(raw_config, dict) else None
        config = scoped if isinstance(scoped, dict) else raw_config if isinstance(raw_config, dict) else {}
        max_attempts_value = config.get("maxAttempts", 2)
        try:
            max_attempts = int(max_attempts_value)
        except (TypeError, ValueError):
            max_attempts = 2
        max_attempts = max(1, min(max_attempts, 5))
        return BuildValidationConfig(
            max_attempts=max_attempts,
            install_dependencies=bool(config.get("installDependencies", False)),
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

    def _read_package_json(self, project_root: Path) -> dict[str, Any] | None:
        package_path = project_root / "package.json"
        if not package_path.is_file():
            return None
        try:
            payload = json.loads(package_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _has_declared_dependencies(self, package_json: dict[str, Any]) -> bool:
        for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            value = package_json.get(key)
            if isinstance(value, dict) and value:
                return True
        return False

    def _status_for_result(self, result: SandboxResult) -> RunStatus:
        if result.failure_class == "none" and result.exit_code == 0:
            return "pass"
        return "fail"

    def _decision_for_observation(self, observation: StageObservation) -> str:
        if observation.status == "pass":
            return f"Sandbox {observation.review_type} validation passed on attempt {observation.attempt}."
        if observation.status == "best_effort":
            detail = observation.limitations[0] if observation.limitations else "validation command was unavailable"
            return f"Sandbox {observation.review_type} validation recorded as best-effort: {detail}"
        result = observation.result
        if result is None:
            detail = observation.plan.missing_reason or "validation command was unavailable"
            return f"Sandbox {observation.review_type} validation failed with {observation.failure_class}: {detail}"
        detail = result.denied_reason or result.stderr or result.stdout or "sandbox command failed"
        return f"Sandbox {observation.review_type} validation failed with {result.failure_class}: {detail}"

    def _decision_for_result(self, *, phase: BuildPhase, status: RunStatus, result: SandboxResult) -> str:
        if status == "pass":
            return f"Sandbox {phase} phase passed."
        detail = result.denied_reason or result.stderr or result.stdout or "sandbox command failed"
        return f"Sandbox {phase} phase failed with {result.failure_class}: {detail}"

    def _repair_evidence_refs(
        self,
        parent_artifact_ids: list[str],
        failed_observations: list[StageObservation],
    ) -> list[EvidenceRef]:
        artifact_id = parent_artifact_ids[-1] if parent_artifact_ids else "unregistered_generated_project"
        excerpt = "; ".join(self._decision_for_observation(observation)[:160] for observation in failed_observations)
        return [
            EvidenceRef(
                artifact_id=artifact_id,
                label="Build validation failure evidence",
                locator="artifact:generated_project",
                excerpt=excerpt,
            )
        ]

    def _log_payload(
        self,
        *,
        job_id: str,
        stage: BuildStage,
        review_type: ReviewType,
        phase: BuildPhase,
        attempt: int,
        status: RunStatus,
        decision: str,
        command: list[str],
        command_source: CommandSource,
        script_name: str | None,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        duration_ms: int,
        failure_class: FailureClass,
        timed_out: bool,
        output_truncated: bool,
        working_directory: str | None,
        limitations: list[str],
        repair_instruction_ids: list[str],
    ) -> dict:
        return {
            "kind": "build_log",
            "jobId": job_id,
            "stage": stage,
            "reviewType": review_type,
            "phase": phase,
            "attempt": attempt,
            "status": status,
            "decision": decision,
            "command": command,
            "commandSource": command_source,
            "scriptName": script_name,
            "packageManager": "npm" if command_source in {"npm_script", "npm_install"} else None,
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
            "repairInstructionIds": repair_instruction_ids,
        }

    def _write_log_artifact(
        self,
        *,
        job_id: str,
        store,
        stage: JobStatus,
        review_type: ReviewType,
        phase: BuildPhase,
        attempt: int,
        payload: dict,
        parent_artifact_ids: list[str],
    ) -> ArtifactRecord:
        return store.write_artifact(
            job_id,
            kind="build_log",
            stage=stage,
            filename=f"{review_type}-{phase}-attempt-{attempt}.json",
            content=self._json_bytes(payload),
            content_type="application/json",
            producer="worker.build_validation",
            parent_artifact_ids=parent_artifact_ids,
            attempt=attempt,
        )

    def _write_review_artifact(
        self,
        *,
        job_id: str,
        store,
        stage: JobStatus,
        review_type: ReviewType,
        attempt: int,
        review_run: ReviewRun,
        parent_artifact_ids: list[str],
    ) -> ArtifactRecord:
        return store.write_artifact(
            job_id,
            kind="review_run",
            stage=stage,
            filename=f"{review_type}-review-run-attempt-{attempt}.json",
            content=review_run.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.build_validation",
            parent_artifact_ids=parent_artifact_ids,
            attempt=attempt,
        )

    def _review_run(
        self,
        *,
        job_id: str,
        review_type: ReviewType,
        attempt: int,
        status: RunStatus,
        decision: str,
        failure_class: FailureClass,
        log_artifact: ArtifactRecord,
        repair_instruction_ids: list[str],
    ) -> ReviewRun:
        return ReviewRun(
            id=f"review_{uuid4().hex[:12]}",
            job_id=job_id,
            attempt=attempt,
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
            repair_instruction_ids=repair_instruction_ids,
            logs_artifact_id=log_artifact.id,
        )

    def _json_bytes(self, payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
