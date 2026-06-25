from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence
from uuid import uuid4

from apps.api.app.models import (
    ArtifactRecord,
    BuildArtifact,
    EvidenceRef,
    FailureClass,
    JobStatus,
    RepairAction,
    RepairInstruction,
    ReviewRun,
    ReviewType,
    RunStatus,
    SandboxResourcePolicy as SandboxResourcePolicyModel,
    TypeScriptDiagnostic,
    TypeScriptRelatedInformation,
)
from packages.sandbox import (
    DEFAULT_CONTAINER_IMAGE,
    ContainerSandboxRunner,
    FirecrackerSandboxRunner,
    GVisorSandboxRunner,
    LocalSandboxRunner,
    PROFILE_ONLY_RUNNERS,
    ProfileOnlySandboxRunner,
    SandboxCommand,
    SandboxPolicy,
    SandboxResult,
)


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
TSC_PAREN_DIAGNOSTIC_RE = re.compile(
    r"^(?P<path>.+?)\((?P<line>\d+),(?P<column>\d+)\):\s*"
    r"(?P<category>error|warning|message|suggestion)\s+(?P<code>TS\d+):\s*(?P<message>.*)$",
    re.IGNORECASE,
)
TSC_COLON_DIAGNOSTIC_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?P<column>\d+)\s+-\s*"
    r"(?P<category>error|warning|message|suggestion)\s+(?P<code>TS\d+):\s*(?P<message>.*)$",
    re.IGNORECASE,
)
TSC_GLOBAL_DIAGNOSTIC_RE = re.compile(
    r"^(?P<category>error|warning|message|suggestion)\s+(?P<code>TS\d+):\s*(?P<message>.*)$",
    re.IGNORECASE,
)
TSC_RELATED_PAREN_RE = re.compile(
    r"^(?P<path>.+?)\((?P<line>\d+),(?P<column>\d+)\):\s*(?P<message>.+)$",
    re.IGNORECASE,
)
TSC_RELATED_COLON_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?P<column>\d+)\s+-\s*(?P<message>.+)$",
    re.IGNORECASE,
)
VITE_DIAGNOSTIC_RE = re.compile(r"^\[vite(?::[^\]]+)?\]:?\s*(?P<message>.+)$", re.IGNORECASE)
VITE_IMPORT_SOURCE_RE = re.compile(r"\bfrom\s+[\"'](?P<path>[^\"']+)[\"']")
ESBUILD_DIAGNOSTIC_RE = re.compile(
    r"^(?:✘|x)\s+\[(?P<category>ERROR|WARNING)\]\s*(?P<message>.+)$",
    re.IGNORECASE,
)
ESBUILD_LOCATION_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?P<column>\d+):\s*$")
MAX_DIAGNOSTICS = 100
MAX_DIAGNOSTIC_CONTEXT_LINES = 30


BuildStage = Literal["building", "typechecking"]
BuildPhase = Literal["install", "build", "typecheck"]
CommandSource = Literal["configured", "npm_script", "fallback_shim", "npm_install", "missing"]
SandboxRunnerKind = Literal["local", "container", "gvisor", "firecracker", "remote_browser_runner"]


class BuildValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuildValidationConfig:
    max_attempts: int = 2
    install_dependencies: bool = False
    allow_low_risk_repairs: bool = True
    allowed_repair_actions: tuple[str, ...] | None = None
    sandbox_runner: SandboxRunnerKind = "local"
    container_image: str = DEFAULT_CONTAINER_IMAGE
    container_runtime_command: tuple[str, ...] | None = None
    gvisor_runtime_command: tuple[str, ...] | None = None
    firecracker_runner_command: tuple[str, ...] | None = None
    sandbox_runtime_name: str | None = None
    sandbox_runtime_version: str | None = None


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
    build_artifact: ArtifactRecord
    review_artifact: ArtifactRecord
    review_run: ReviewRun
    message: str

    @property
    def artifact_ids(self) -> list[str]:
        return [self.log_artifact.id, self.build_artifact.id, self.review_artifact.id]


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
        sandbox_runner: LocalSandboxRunner
        | ContainerSandboxRunner
        | GVisorSandboxRunner
        | FirecrackerSandboxRunner
        | ProfileOnlySandboxRunner
        | None = None,
        build_command: Sequence[str] = DEFAULT_BUILD_COMMAND,
        typecheck_command: Sequence[str] = DEFAULT_TYPECHECK_COMMAND,
    ) -> None:
        self.build_command = tuple(build_command)
        self.typecheck_command = tuple(typecheck_command)
        self.use_package_scripts = (
            self.build_command == DEFAULT_BUILD_COMMAND and self.typecheck_command == DEFAULT_TYPECHECK_COMMAND
        )
        self._provided_sandbox_runner = sandbox_runner
        self.sandbox_runner = sandbox_runner or self._sandbox_runner_for_config(BuildValidationConfig())

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
        source_project_parents = [*parents]
        if generated_project_artifact is not None:
            source_project_parents.append(generated_project_artifact.id)

        config = self._config(job_id=job_id, store=store)
        self.sandbox_runner = self._provided_sandbox_runner or self._sandbox_runner_for_config(config)
        source_project_path = Path(project_path) if project_path is not None else None
        if source_project_path is None or not source_project_path.is_dir():
            local_artifact_path = (
                store.artifact_local_path(generated_project_artifact) if generated_project_artifact is not None else None
            )
            if local_artifact_path is not None and local_artifact_path.is_dir():
                source_project_path = local_artifact_path
        if (source_project_path is None and generated_project_artifact is None) or (
            source_project_path is not None and not source_project_path.is_dir()
        ):
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
        current_project_artifact = generated_project_artifact
        current_parent_ids = source_project_parents
        last_build: BuildValidationStageResult | None = None
        last_typecheck: BuildValidationStageResult | None = None

        for attempt in range(config.max_attempts):
            with self.sandbox_runner.attempt_workspace() as workspace:
                project_root = workspace / "project"
                if current_project_path is None:
                    current_project_path = self._project_path(current_project_artifact, store, workspace / "source_project")
                if current_project_path is None or not current_project_path.is_dir():
                    build = self._persist_best_effort(
                        job_id=job_id,
                        store=store,
                        stage="building",
                        review_type="build",
                        attempt=attempt,
                        command=self.build_command,
                        parent_artifact_ids=current_parent_ids,
                        limitation="No generated_project directory is available; deterministic writer output is required before real sandbox validation can run.",
                    )
                    typecheck = self._persist_best_effort(
                        job_id=job_id,
                        store=store,
                        stage="typechecking",
                        review_type="typecheck",
                        attempt=attempt,
                        command=self.typecheck_command,
                        parent_artifact_ids=[*current_parent_ids, *build.artifact_ids],
                        limitation="No generated_project directory is available; deterministic writer output is required before real sandbox validation can run.",
                    )
                    return BuildValidationResult(build=build, typecheck=typecheck, install_logs=install_logs)
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
                if failed_observations and config.allow_low_risk_repairs and attempt + 1 < config.max_attempts:
                    repair_outcome = self._write_repair_instruction(
                        job_id=job_id,
                        store=store,
                        project_root=project_root,
                        attempt=attempt,
                        failed_observations=failed_observations,
                        allowed_repair_actions=config.allowed_repair_actions,
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
                current_project_artifact = repair_outcome.applied_project_artifact
                current_project_path = store.artifact_local_path(current_project_artifact)
                if current_project_path is not None and not current_project_path.is_dir():
                    current_project_path = None
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
        build_artifact_payload = self._build_artifact(
            job_id=job_id,
            observation=observation,
            decision=decision,
            log_artifact=log_artifact,
            repair_instruction_ids=repair_instruction_ids,
        )
        build_artifact = self._write_build_artifact(
            job_id=job_id,
            store=store,
            stage=observation.stage,
            review_type=observation.review_type,
            phase=observation.plan.phase,
            attempt=observation.attempt,
            build_artifact=build_artifact_payload,
            parent_artifact_ids=[*parent_artifact_ids, log_artifact.id, *repair_instruction_ids],
        )
        review_run = self._review_run(
            job_id=job_id,
            review_type=observation.review_type,
            attempt=observation.attempt,
            status=observation.status,
            decision=decision,
            failure_class=observation.failure_class,
            log_artifact=log_artifact,
            build_artifact=build_artifact,
            repair_instruction_ids=repair_instruction_ids,
        )
        review_artifact = self._write_review_artifact(
            job_id=job_id,
            store=store,
            stage=observation.stage,
            review_type=observation.review_type,
            attempt=observation.attempt,
            review_run=review_run,
            parent_artifact_ids=[*parent_artifact_ids, log_artifact.id, build_artifact.id, *repair_instruction_ids],
        )
        return BuildValidationStageResult(
            log_artifact=log_artifact,
            build_artifact=build_artifact,
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
        allowed_repair_actions: tuple[str, ...] | None,
        parent_artifact_ids: list[str],
    ) -> RepairOutcome:
        store.update_status(job_id, "repairing")
        actions = self._repair_actions(
            project_root=project_root,
            failed_observations=failed_observations,
            allowed_repair_actions=allowed_repair_actions,
        )
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
        allowed_repair_actions: tuple[str, ...] | None = None,
    ) -> list[RepairAction]:
        package_json = self._read_package_json(project_root)
        if package_json is None:
            return []
        scripts = package_json.get("scripts")
        if not isinstance(scripts, dict):
            scripts = {}
        actions: list[RepairAction] = []
        failed_by_review_type = {observation.review_type: observation for observation in failed_observations}
        allowed = set(allowed_repair_actions) if allowed_repair_actions is not None else None
        if (
            "build" in failed_by_review_type
            and self._repair_action_allowed("add_package_script", allowed)
            and "build" not in scripts
            and (project_root / "scripts" / "build.mjs").exists()
        ):
            actions.append(
                RepairAction(
                    action="add_package_script",
                    path="package.json:scripts.build",
                    value="node scripts/build.mjs",
                    reason="A generated build shim exists and package.json does not define scripts.build.",
                )
            )
        if (
            "typecheck" in failed_by_review_type
            and self._repair_action_allowed("add_package_script", allowed)
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
        if (
            "build" in failed_by_review_type
            and self._repair_action_allowed("replace_package_script", allowed)
            and isinstance(scripts.get("build"), str)
            and scripts.get("build") != "node scripts/build.mjs"
            and (project_root / "scripts" / "build.mjs").exists()
        ):
            actions.append(
                RepairAction(
                    action="replace_package_script",
                    path="package.json:scripts.build",
                    value="node scripts/build.mjs",
                    reason=(
                        "scripts.build failed during validation while a generated build shim exists; "
                        "replace the generated-project script with the deterministic shim for retry."
                    ),
                )
            )
        if (
            "typecheck" in failed_by_review_type
            and self._repair_action_allowed("replace_package_script", allowed)
            and isinstance(scripts.get("typecheck"), str)
            and scripts.get("typecheck") != "node scripts/typecheck.mjs"
            and (project_root / "scripts" / "typecheck.mjs").exists()
        ):
            actions.append(
                RepairAction(
                    action="replace_package_script",
                    path="package.json:scripts.typecheck",
                    value="node scripts/typecheck.mjs",
                    reason=(
                        "scripts.typecheck failed during validation while a generated typecheck shim exists; "
                        "replace the generated-project script with the deterministic shim for retry."
                    ),
                )
            )
        return actions

    def _repair_action_allowed(self, action: str, allowed: set[str] | None) -> bool:
        return allowed is None or action in allowed

    def _apply_repair_actions(self, *, project_root: Path, actions: list[RepairAction]) -> None:
        package_path = project_root / "package.json"
        package_json = self._read_package_json(project_root) or {}
        scripts = package_json.get("scripts")
        if not isinstance(scripts, dict):
            scripts = {}
        for action in actions:
            if action.action in {"add_package_script", "replace_package_script"}:
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
        review_fix = raw_config.get("reviewFix") if isinstance(raw_config, dict) else None
        review_fix = review_fix if isinstance(review_fix, dict) else {}
        scoped = raw_config.get("buildValidation") if isinstance(raw_config, dict) else None
        config = scoped if isinstance(scoped, dict) else raw_config if isinstance(raw_config, dict) else {}
        build_review_fix = review_fix.get("buildValidation") if isinstance(review_fix.get("buildValidation"), dict) else {}
        max_attempts_value = config.get("maxAttempts", build_review_fix.get("maxAttempts", review_fix.get("maxAttempts", 2)))
        try:
            max_attempts = int(max_attempts_value)
        except (TypeError, ValueError):
            max_attempts = 2
        max_attempts = max(1, min(max_attempts, 5))
        allowed_actions = self._allowed_repair_actions_config(
            config.get("allowedRepairActions")
            if "allowedRepairActions" in config
            else build_review_fix.get("allowedRepairActions", review_fix.get("allowedRepairActions"))
        )
        allow_low_risk_repairs = self._bool_config(
            config.get("allowLowRiskRepairs")
            if "allowLowRiskRepairs" in config
            else build_review_fix.get("allowLowRiskRepairs", review_fix.get("allowLowRiskRepairs")),
            default=True,
        )
        sandbox_runner = self._sandbox_runner_name(config.get("sandboxRunner") or os.getenv("AI_JSUNPACK_SANDBOX_RUNNER"))
        container_image = self._string_config(
            config.get("containerImage") or os.getenv("AI_JSUNPACK_SANDBOX_IMAGE"),
            default=DEFAULT_CONTAINER_IMAGE,
        )
        gvisor_runtime_command_value = (
            config.get("gvisorRuntimeCommand")
            if "gvisorRuntimeCommand" in config
            else os.getenv("AI_JSUNPACK_SANDBOX_GVISOR_RUNTIME_COMMAND")
        )
        return BuildValidationConfig(
            max_attempts=max_attempts,
            install_dependencies=bool(config.get("installDependencies", False)),
            allow_low_risk_repairs=allow_low_risk_repairs,
            allowed_repair_actions=allowed_actions,
            sandbox_runner=sandbox_runner,
            container_image=container_image,
            container_runtime_command=self._container_runtime_command_config(config.get("containerRuntimeCommand")),
            gvisor_runtime_command=self._runner_command_config(gvisor_runtime_command_value),
            firecracker_runner_command=self._runner_command_config(
                config.get("firecrackerRunnerCommand") or os.getenv("AI_JSUNPACK_FIRECRACKER_RUNNER_COMMAND")
            ),
            sandbox_runtime_name=self._optional_string_config(
                config.get("sandboxRuntimeName") or os.getenv("AI_JSUNPACK_SANDBOX_RUNTIME_NAME")
            ),
            sandbox_runtime_version=self._optional_string_config(
                config.get("sandboxRuntimeVersion") or os.getenv("AI_JSUNPACK_SANDBOX_RUNTIME_VERSION")
            ),
        )

    def _bool_config(self, value: Any, *, default: bool) -> bool:
        return value if isinstance(value, bool) else default

    def _allowed_repair_actions_config(self, value: Any) -> tuple[str, ...] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            return None
        allowed = [
            item
            for item in value
            if item in {"add_package_script", "replace_package_script", "mirror_original_static_entry"}
        ]
        return tuple(dict.fromkeys(allowed))

    def _sandbox_runner_for_config(
        self,
        config: BuildValidationConfig,
    ) -> LocalSandboxRunner | ContainerSandboxRunner | GVisorSandboxRunner | FirecrackerSandboxRunner | ProfileOnlySandboxRunner:
        policy = self._sandbox_policy()
        if config.sandbox_runner == "container":
            return ContainerSandboxRunner(
                policy,
                image=config.container_image,
                runtime_command=config.container_runtime_command,
            )
        if config.sandbox_runner == "gvisor":
            return GVisorSandboxRunner(
                policy,
                image=config.container_image,
                runtime_command=(
                    config.gvisor_runtime_command
                    if config.gvisor_runtime_command is not None
                    else config.container_runtime_command
                ),
                gvisor_runtime=config.sandbox_runtime_name,
                runtime_version=config.sandbox_runtime_version,
            )
        if config.sandbox_runner == "firecracker":
            return FirecrackerSandboxRunner(
                policy,
                runner_command=config.firecracker_runner_command,
                runtime_name=config.sandbox_runtime_name,
                runtime_version=config.sandbox_runtime_version,
            )
        if config.sandbox_runner in PROFILE_ONLY_RUNNERS:
            return ProfileOnlySandboxRunner(
                policy,
                runner_kind=config.sandbox_runner,
                runtime_name=config.sandbox_runtime_name,
                runtime_version=config.sandbox_runtime_version,
            )
        return LocalSandboxRunner(policy)

    def _sandbox_policy(self) -> SandboxPolicy:
        return SandboxPolicy(
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

    def _sandbox_runner_name(self, value: Any) -> SandboxRunnerKind:
        normalized = self._normalized_runner_name(value)
        if normalized in {"container", "gvisor", "firecracker", "remote_browser_runner"}:
            return normalized
        return "local"

    def _normalized_runner_name(self, value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip().lower().replace("-", "_")

    def _string_config(self, value: Any, *, default: str) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return default

    def _optional_string_config(self, value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _container_runtime_command_config(self, value: Any) -> tuple[str, ...] | None:
        return self._runner_command_config(value)

    def _runner_command_config(self, value: Any) -> tuple[str, ...] | None:
        if isinstance(value, list) and all(isinstance(part, str) and part for part in value):
            return tuple(value)
        if isinstance(value, tuple) and all(isinstance(part, str) and part for part in value):
            return tuple(value)
        if isinstance(value, str) and value.strip():
            try:
                return tuple(part for part in shlex.split(value) if part)
            except ValueError:
                return None
        return None

    def _latest_generated_project(self, *, job_id: str, store) -> ArtifactRecord | None:
        artifacts = store.list_artifacts(job_id, kind="generated_project")
        return artifacts[-1] if artifacts else None

    def _project_path(self, artifact: ArtifactRecord | None, store, target_dir: Path) -> Path | None:
        if artifact is None:
            return None
        path = store.artifact_local_path(artifact)
        if path is not None and path.is_dir():
            return path
        try:
            return store.materialize_artifact_directory(artifact, target_dir)
        except Exception:
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

    def _build_artifact(
        self,
        *,
        job_id: str,
        observation: StageObservation,
        decision: str,
        log_artifact: ArtifactRecord,
        repair_instruction_ids: list[str],
    ) -> BuildArtifact:
        result = observation.result
        command_source = observation.plan.command_source
        return BuildArtifact(
            id=f"build_{uuid4().hex[:12]}",
            job_id=job_id,
            stage=observation.stage,
            review_type=observation.review_type,
            phase=observation.plan.phase,
            attempt=observation.attempt,
            status=observation.status,
            decision=decision,
            command=list(observation.plan.command or []),
            command_source=command_source,
            script_name=observation.plan.script_name,
            package_manager="npm" if command_source in {"npm_script", "npm_install"} else None,
            exit_code=result.exit_code if result is not None else None,
            duration_ms=result.duration_ms if result is not None else 0,
            failure_class=observation.failure_class,
            timed_out=result.timed_out if result is not None else False,
            output_truncated=result.output_truncated if result is not None else False,
            working_directory=result.working_directory if result is not None else None,
            network_policy=result.network_policy if result is not None else self.sandbox_runner.policy.network_policy,
            resource_policy=self._resource_policy_for_result(result),
            diagnostics=self._diagnostics_for_observation(observation),
            logs_artifact_id=log_artifact.id,
            repair_instruction_ids=repair_instruction_ids,
            limitations=observation.limitations,
        )

    def _resource_policy_for_result(self, result: SandboxResult | None) -> SandboxResourcePolicyModel:
        resource_policy = result.resource_policy if result is not None else self.sandbox_runner.policy.resource_policy
        payload = asdict(resource_policy)
        payload["limitations"] = list(payload.get("limitations") or [])
        payload["capabilities"] = list(payload.get("capabilities") or [])
        return SandboxResourcePolicyModel.model_validate(payload)

    def _diagnostics_for_observation(self, observation: StageObservation) -> list[TypeScriptDiagnostic]:
        if observation.review_type != "typecheck" or observation.result is None:
            return []
        diagnostics: list[TypeScriptDiagnostic] = []
        diagnostics.extend(self._parse_typescript_diagnostics(observation.result.stderr, source="stderr"))
        diagnostics.extend(self._parse_typescript_diagnostics(observation.result.stdout, source="stdout"))
        return diagnostics[:MAX_DIAGNOSTICS]

    def _parse_typescript_diagnostics(self, text: str, *, source: Literal["stdout", "stderr"]) -> list[TypeScriptDiagnostic]:
        diagnostics: list[TypeScriptDiagnostic] = []
        current: TypeScriptDiagnostic | None = None
        for raw_line in text.splitlines():
            if len(diagnostics) >= MAX_DIAGNOSTICS:
                break
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            diagnostic = self._parse_typescript_diagnostic_line(stripped, source=source)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
                current = diagnostic
                continue
            diagnostic = self._parse_build_tool_diagnostic_line(stripped, source=source)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
                current = diagnostic
                continue
            if current is None:
                continue
            related = self._parse_typescript_related_information(stripped)
            if related is not None:
                current.related_information.append(related)
                continue
            esbuild_location = ESBUILD_LOCATION_RE.match(stripped)
            if esbuild_location is not None and current.tool == "esbuild" and current.file_path is None:
                current.file_path = esbuild_location.group("path")
                current.line = int(esbuild_location.group("line"))
                current.column = int(esbuild_location.group("column"))
                self._append_diagnostic_context(current, line)
                continue
            if self._is_diagnostic_context_line(raw_line):
                self._append_diagnostic_context(current, line)
        return diagnostics

    def _parse_typescript_diagnostic_line(
        self,
        line: str,
        *,
        source: Literal["stdout", "stderr"],
    ) -> TypeScriptDiagnostic | None:
        for pattern in (TSC_PAREN_DIAGNOSTIC_RE, TSC_COLON_DIAGNOSTIC_RE):
            match = pattern.match(line)
            if match is None:
                continue
            return TypeScriptDiagnostic(
                source=source,
                tool="tsc",
                category=match.group("category").lower(),
                code=match.group("code"),
                message=match.group("message"),
                file_path=match.group("path"),
                line=int(match.group("line")),
                column=int(match.group("column")),
            )

        match = TSC_GLOBAL_DIAGNOSTIC_RE.match(line)
        if match is None:
            return None
        return TypeScriptDiagnostic(
            source=source,
            tool="tsc",
            category=match.group("category").lower(),
            code=match.group("code"),
            message=match.group("message"),
        )

    def _parse_build_tool_diagnostic_line(
        self,
        line: str,
        *,
        source: Literal["stdout", "stderr"],
    ) -> TypeScriptDiagnostic | None:
        esbuild_match = ESBUILD_DIAGNOSTIC_RE.match(line)
        if esbuild_match is not None:
            return TypeScriptDiagnostic(
                source=source,
                tool="esbuild",
                category=self._diagnostic_category(esbuild_match.group("category")),
                code=None,
                message=esbuild_match.group("message").strip(),
            )

        vite_match = VITE_DIAGNOSTIC_RE.match(line)
        if vite_match is None:
            return None
        message = vite_match.group("message").strip()
        source_match = VITE_IMPORT_SOURCE_RE.search(message)
        return TypeScriptDiagnostic(
            source=source,
            tool="vite",
            category="error" if self._message_reads_as_error(message) else "unknown",
            code=None,
            message=message,
            file_path=source_match.group("path") if source_match is not None else None,
        )

    def _parse_typescript_related_information(self, line: str) -> TypeScriptRelatedInformation | None:
        for pattern in (TSC_RELATED_PAREN_RE, TSC_RELATED_COLON_RE):
            match = pattern.match(line)
            if match is None:
                continue
            message = match.group("message").strip()
            if not message:
                return None
            code_match = re.search(r"\b(TS\d+)\b", message)
            return TypeScriptRelatedInformation(
                message=message,
                file_path=match.group("path"),
                line=int(match.group("line")),
                column=int(match.group("column")),
                code=code_match.group(1) if code_match is not None else None,
            )
        return None

    def _append_diagnostic_context(self, diagnostic: TypeScriptDiagnostic, line: str) -> None:
        if len(diagnostic.context_lines) < MAX_DIAGNOSTIC_CONTEXT_LINES:
            diagnostic.context_lines.append(line)

    def _is_diagnostic_context_line(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        return bool(line[:1].isspace()) or stripped.startswith((">", "|", "│", "╵", "~", "^"))

    def _diagnostic_category(self, value: str) -> Literal["error", "warning", "message", "suggestion", "unknown"]:
        normalized = value.strip().lower()
        if normalized in {"error", "warning", "message", "suggestion"}:
            return normalized
        return "unknown"

    def _message_reads_as_error(self, message: str) -> bool:
        normalized = message.lower()
        return any(token in normalized for token in ("error", "failed", "cannot", "could not", "rollup"))

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

    def _write_build_artifact(
        self,
        *,
        job_id: str,
        store,
        stage: JobStatus,
        review_type: ReviewType,
        phase: BuildPhase,
        attempt: int,
        build_artifact: BuildArtifact,
        parent_artifact_ids: list[str],
    ) -> ArtifactRecord:
        return store.write_artifact(
            job_id,
            kind="build_artifact",
            stage=stage,
            filename=f"{review_type}-{phase}-artifact-attempt-{attempt}.json",
            content=build_artifact.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
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
        build_artifact: ArtifactRecord,
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
                    artifact_id=build_artifact.id,
                    label=f"Structured sandbox {review_type} artifact",
                    locator="artifact:build_artifact",
                    excerpt=decision[:240],
                ),
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
