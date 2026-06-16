from __future__ import annotations

import os
import math
import platform
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator, Literal, Mapping, Sequence

from apps.api.app.models import FailureClass


NetworkPolicy = Literal["deny", "allow"]
ResourcePolicyEnforcement = Literal["local_best_effort", "container_enforced"]
SandboxRunnerKind = Literal["local", "container"]
SandboxCapabilityName = Literal["network", "process", "cpu", "memory", "filesystem"]
SandboxCapabilityStatus = Literal["enforced", "best_effort", "unsupported", "unknown"]
AllowedCommand = str | Sequence[str]
DEFAULT_CONTAINER_IMAGE = "node:20-bookworm-slim"


@dataclass(frozen=True)
class SandboxRuntimeCapability:
    name: SandboxCapabilityName
    status: SandboxCapabilityStatus
    detail: str


def _host_platform() -> str:
    system = platform.system() or "unknown"
    release = platform.release() or "unknown"
    machine = platform.machine() or "unknown"
    return f"{system} {release} ({machine})"


def _local_capabilities() -> tuple[SandboxRuntimeCapability, ...]:
    return (
        SandboxRuntimeCapability(
            name="network",
            status="best_effort",
            detail="Local runner records network policy but does not enforce OS-level network isolation.",
        ),
        SandboxRuntimeCapability(
            name="process",
            status="best_effort",
            detail="Local runner records process limits but does not enforce a process-count boundary.",
        ),
        SandboxRuntimeCapability(
            name="cpu",
            status="best_effort",
            detail="Local runner enforces wall-clock timeout only; CPU time limits are audit metadata.",
        ),
        SandboxRuntimeCapability(
            name="memory",
            status="best_effort",
            detail="Local runner records memory limits but does not enforce an OS memory boundary.",
        ),
        SandboxRuntimeCapability(
            name="filesystem",
            status="best_effort",
            detail="Local runner executes in a temporary attempt workspace and validates relative working directories.",
        ),
    )


@dataclass(frozen=True)
class SandboxResourcePolicy:
    process_limit: int | None = None
    cpu_time_limit_ms: int | None = None
    memory_limit_bytes: int | None = None
    enforcement: ResourcePolicyEnforcement = "local_best_effort"
    runner_kind: SandboxRunnerKind = "local"
    runtime_name: str | None = None
    runtime_version: str | None = None
    host_platform: str = field(default_factory=_host_platform)
    capabilities: tuple[SandboxRuntimeCapability, ...] = field(default_factory=_local_capabilities)
    limitations: tuple[str, ...] = (
        "Local sandbox runner records process, CPU, and memory policy but does not enforce OS/container isolation.",
    )


@dataclass(frozen=True)
class SandboxPolicy:
    allowed_commands: tuple[AllowedCommand, ...] = field(default_factory=tuple)
    timeout_ms: int = 30_000
    output_limit_bytes: int = 64 * 1024
    network_policy: NetworkPolicy = "deny"
    resource_policy: SandboxResourcePolicy = field(default_factory=SandboxResourcePolicy)
    allowed_environment: tuple[str, ...] = (
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
    )
    temp_prefix: str = "ai-jsunpack-sandbox-"


@dataclass(frozen=True)
class SandboxCommand:
    executable: str
    args: tuple[str, ...] = field(default_factory=tuple)
    working_directory: str | None = None
    stdin: bytes | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    failure_class: FailureClass = "unknown"

    @property
    def argv(self) -> list[str]:
        return [self.executable, *self.args]


@dataclass(frozen=True)
class SandboxResult:
    command: list[str]
    stdout: str
    stderr: str
    exit_code: int | None
    duration_ms: int
    failure_class: FailureClass
    timed_out: bool
    output_truncated: bool
    working_directory: str
    network_policy: NetworkPolicy
    resource_policy: SandboxResourcePolicy
    denied_reason: str | None = None


class LocalSandboxRunner:
    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        self.policy = policy or SandboxPolicy()

    @contextmanager
    def attempt_workspace(self) -> Iterator[Path]:
        with tempfile.TemporaryDirectory(prefix=self.policy.temp_prefix) as temp_dir:
            yield Path(temp_dir)

    def run(self, command: SandboxCommand) -> SandboxResult:
        started_at = time.perf_counter()
        with self.attempt_workspace() as workspace:
            return self.run_in_workspace(command, workspace, started_at=started_at)

    def run_in_workspace(
        self,
        command: SandboxCommand,
        workspace: Path,
        *,
        started_at: float | None = None,
    ) -> SandboxResult:
        started = started_at if started_at is not None else time.perf_counter()
        denied_reason = self._denied_reason(command, workspace)
        if denied_reason is not None:
            return self._denied_result(command, workspace, started, denied_reason)

        working_directory = self._working_directory(command, workspace)
        working_directory.mkdir(parents=True, exist_ok=True)
        try:
            process = subprocess.Popen(
                command.argv,
                cwd=working_directory,
                env=self._environment(command),
                stdin=subprocess.PIPE if command.stdin is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as error:
            return SandboxResult(
                command=command.argv,
                stdout="",
                stderr=str(error),
                exit_code=None,
                duration_ms=self._duration_ms(started),
                failure_class=command.failure_class,
                timed_out=False,
                output_truncated=False,
                working_directory=str(working_directory),
                network_policy=self.policy.network_policy,
                resource_policy=self.policy.resource_policy,
            )
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = process.communicate(
                input=command.stdin,
                timeout=max(self.policy.timeout_ms, 1) / 1000,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            stdout_bytes, stderr_bytes = process.communicate()

        stdout, stderr, output_truncated = self._decode_limited_output(stdout_bytes, stderr_bytes)
        failure_class = self._failure_class(
            exit_code=process.returncode,
            timed_out=timed_out,
            output_truncated=output_truncated,
            command_failure_class=command.failure_class,
        )
        return SandboxResult(
            command=command.argv,
            stdout=stdout,
            stderr=stderr,
            exit_code=process.returncode,
            duration_ms=self._duration_ms(started),
            failure_class=failure_class,
            timed_out=timed_out,
            output_truncated=output_truncated,
            working_directory=str(working_directory),
            network_policy=self.policy.network_policy,
            resource_policy=self.policy.resource_policy,
        )

    def _denied_reason(self, command: SandboxCommand, workspace: Path) -> str | None:
        if not self._is_allowed(command.argv):
            return f"Command is not allowed: {command.executable}"
        if command.working_directory is None:
            return None
        candidate = Path(command.working_directory)
        if candidate.is_absolute():
            return "Sandbox working directory must be relative to the attempt workspace."
        resolved = (workspace / candidate).resolve()
        try:
            resolved.relative_to(workspace.resolve())
        except ValueError:
            return "Sandbox working directory escaped the attempt workspace."
        return None

    def _denied_result(
        self,
        command: SandboxCommand,
        workspace: Path,
        started_at: float,
        denied_reason: str,
    ) -> SandboxResult:
        return SandboxResult(
            command=command.argv,
            stdout="",
            stderr=denied_reason,
            exit_code=None,
            duration_ms=self._duration_ms(started_at),
            failure_class="sandbox_denied",
            timed_out=False,
            output_truncated=False,
            working_directory=str(workspace),
            network_policy=self.policy.network_policy,
            resource_policy=self.policy.resource_policy,
            denied_reason=denied_reason,
        )

    def _is_allowed(self, argv: list[str]) -> bool:
        executable = Path(argv[0]).name.lower()
        full_executable = argv[0].lower()
        for allowed in self.policy.allowed_commands:
            if isinstance(allowed, str):
                allowed_value = allowed.lower()
                if allowed_value in {executable, full_executable}:
                    return True
                continue
            allowed_parts = [str(part).lower() for part in allowed]
            if len(argv) < len(allowed_parts):
                continue
            candidate = [part.lower() for part in argv[: len(allowed_parts)]]
            if candidate == allowed_parts:
                return True
            if len(allowed_parts) == 1 and allowed_parts[0] in {executable, full_executable}:
                return True
        return False

    def _working_directory(self, command: SandboxCommand, workspace: Path) -> Path:
        if command.working_directory is None:
            return workspace
        return workspace / command.working_directory

    def _environment(self, command: SandboxCommand) -> dict[str, str]:
        allowed_names = set(self.policy.allowed_environment)
        clean_env = {name: value for name, value in os.environ.items() if name in allowed_names}
        for name, value in command.environment.items():
            if name in allowed_names:
                clean_env[name] = value
        return clean_env

    def _decode_limited_output(self, stdout: bytes, stderr: bytes) -> tuple[str, str, bool]:
        limit = max(self.policy.output_limit_bytes, 0)
        total_size = len(stdout) + len(stderr)
        output_truncated = total_size > limit
        if output_truncated:
            stdout_limit = min(len(stdout), limit)
            stderr_limit = max(0, limit - stdout_limit)
            stdout = stdout[:stdout_limit]
            stderr = stderr[:stderr_limit]
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            output_truncated,
        )

    def _failure_class(
        self,
        *,
        exit_code: int | None,
        timed_out: bool,
        output_truncated: bool,
        command_failure_class: FailureClass,
    ) -> FailureClass:
        if timed_out:
            return "timeout"
        if output_truncated and exit_code == 0:
            return "resource_limit"
        if exit_code and exit_code != 0:
            return command_failure_class
        return "none"

    def _duration_ms(self, started_at: float) -> int:
        return int((time.perf_counter() - started_at) * 1000)


class ContainerSandboxRunner(LocalSandboxRunner):
    """Runs sandbox commands through Docker or Podman with container-level limits."""

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        image: str = DEFAULT_CONTAINER_IMAGE,
        runtime_command: Sequence[str] | None = None,
    ) -> None:
        self.image = image
        self.runtime_command = tuple(runtime_command) if runtime_command is not None else None
        runtime = self._runtime_command()
        super().__init__(self._container_policy(policy or SandboxPolicy(), runtime_command=runtime))

    def run_in_workspace(
        self,
        command: SandboxCommand,
        workspace: Path,
        *,
        started_at: float | None = None,
    ) -> SandboxResult:
        started = started_at if started_at is not None else time.perf_counter()
        denied_reason = self._denied_reason(command, workspace)
        if denied_reason is not None:
            return self._denied_result(command, workspace, started, denied_reason)

        runtime_command = self._runtime_command()
        if runtime_command is None:
            return self._denied_result(
                command,
                workspace,
                started,
                "Container runtime is not available; install docker or podman, or use the local sandbox runner.",
            )

        working_directory = self._working_directory(command, workspace)
        working_directory.mkdir(parents=True, exist_ok=True)
        container_argv = self._container_argv(
            runtime_command=runtime_command,
            command=command,
            workspace=workspace,
            working_directory=working_directory,
        )
        try:
            process = subprocess.Popen(
                container_argv,
                cwd=workspace,
                env=self._environment(command),
                stdin=subprocess.PIPE if command.stdin is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as error:
            return SandboxResult(
                command=command.argv,
                stdout="",
                stderr=str(error),
                exit_code=None,
                duration_ms=self._duration_ms(started),
                failure_class=command.failure_class,
                timed_out=False,
                output_truncated=False,
                working_directory=str(working_directory),
                network_policy=self.policy.network_policy,
                resource_policy=self.policy.resource_policy,
            )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = process.communicate(
                input=command.stdin,
                timeout=max(self.policy.timeout_ms, 1) / 1000,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            stdout_bytes, stderr_bytes = process.communicate()

        stdout, stderr, output_truncated = self._decode_limited_output(stdout_bytes, stderr_bytes)
        failure_class = self._failure_class(
            exit_code=process.returncode,
            timed_out=timed_out,
            output_truncated=output_truncated,
            command_failure_class=command.failure_class,
        )
        return SandboxResult(
            command=command.argv,
            stdout=stdout,
            stderr=stderr,
            exit_code=process.returncode,
            duration_ms=self._duration_ms(started),
            failure_class=failure_class,
            timed_out=timed_out,
            output_truncated=output_truncated,
            working_directory=str(working_directory),
            network_policy=self.policy.network_policy,
            resource_policy=self.policy.resource_policy,
        )

    def _container_policy(self, policy: SandboxPolicy, *, runtime_command: tuple[str, ...] | None) -> SandboxPolicy:
        resource_policy = policy.resource_policy
        limitations = tuple(resource_policy.limitations or ())
        local_default_limitations = SandboxResourcePolicy().limitations
        container_limitations = (
            "Container sandbox runner maps resource policy to Docker/Podman flags; enforcement depends on the selected runtime, host OS, and container backend.",
            "CPU time limits use container ulimit settings and remain best-effort across Docker/Podman and host platforms.",
            "Windows and macOS container resource controls are enforced by the backing Linux VM/cgroup layer when available.",
            "For stricter multi-tenant isolation, evaluate gVisor, Firecracker, or an equivalent stronger sandbox runtime before production exposure.",
        )
        if not limitations:
            limitations = container_limitations
        elif resource_policy.enforcement == "local_best_effort" and limitations == local_default_limitations:
            limitations = container_limitations
        elif resource_policy.enforcement == "local_best_effort":
            limitations = (*container_limitations, *limitations)
        runtime_name = self._runtime_name(runtime_command)
        return replace(
            policy,
            resource_policy=replace(
                resource_policy,
                enforcement="container_enforced",
                runner_kind="container",
                runtime_name=runtime_name,
                runtime_version=None,
                host_platform=_host_platform(),
                capabilities=self._container_capabilities(
                    resource_policy,
                    network_policy=policy.network_policy,
                    runtime_name=runtime_name,
                ),
                limitations=limitations,
            ),
        )

    def _container_capabilities(
        self,
        resource_policy: SandboxResourcePolicy,
        *,
        network_policy: NetworkPolicy,
        runtime_name: str | None,
    ) -> tuple[SandboxRuntimeCapability, ...]:
        runtime_label = runtime_name or "unavailable container runtime"
        if runtime_name is None:
            return (
                SandboxRuntimeCapability(
                    name="network",
                    status="unsupported",
                    detail="No Docker or Podman runtime was found, so network isolation cannot be applied.",
                ),
                SandboxRuntimeCapability(
                    name="process",
                    status="unsupported",
                    detail="No Docker or Podman runtime was found, so process limits cannot be applied.",
                ),
                SandboxRuntimeCapability(
                    name="cpu",
                    status="unsupported",
                    detail="No Docker or Podman runtime was found, so CPU limits cannot be applied.",
                ),
                SandboxRuntimeCapability(
                    name="memory",
                    status="unsupported",
                    detail="No Docker or Podman runtime was found, so memory limits cannot be applied.",
                ),
                SandboxRuntimeCapability(
                    name="filesystem",
                    status="unsupported",
                    detail="No Docker or Podman runtime was found, so the attempt workspace cannot be mounted into a container.",
                ),
            )

        network_status: SandboxCapabilityStatus = "enforced" if network_policy == "deny" else "best_effort"
        network_detail = (
            f"{runtime_label} receives --network none for deny policy."
            if network_policy == "deny"
            else f"Network access is allowed by policy; {runtime_label} network isolation is not requested."
        )
        process_status: SandboxCapabilityStatus = "enforced" if resource_policy.process_limit is not None else "best_effort"
        process_detail = (
            f"{runtime_label} receives --pids-limit={resource_policy.process_limit}."
            if resource_policy.process_limit is not None
            else f"No process limit is configured; {runtime_label} uses its default process boundary."
        )
        memory_status: SandboxCapabilityStatus = "enforced" if resource_policy.memory_limit_bytes is not None else "best_effort"
        memory_detail = (
            f"{runtime_label} receives --memory={resource_policy.memory_limit_bytes}; enforcement depends on cgroup support in the host/container backend."
            if resource_policy.memory_limit_bytes is not None
            else f"No memory limit is configured; {runtime_label} uses its default memory boundary."
        )
        cpu_detail = (
            f"{runtime_label} receives a CPU ulimit derived from {resource_policy.cpu_time_limit_ms} ms; support differs across Docker/Podman and host platforms."
            if resource_policy.cpu_time_limit_ms is not None
            else f"No CPU time limit is configured; {runtime_label} uses its default CPU boundary."
        )
        return (
            SandboxRuntimeCapability(name="network", status=network_status, detail=network_detail),
            SandboxRuntimeCapability(name="process", status=process_status, detail=process_detail),
            SandboxRuntimeCapability(name="cpu", status="best_effort", detail=cpu_detail),
            SandboxRuntimeCapability(name="memory", status=memory_status, detail=memory_detail),
            SandboxRuntimeCapability(
                name="filesystem",
                status="best_effort",
                detail="The attempt workspace is bind-mounted at /workspace; no read-only root filesystem or stronger runtime isolation is configured.",
            ),
        )

    def _runtime_name(self, runtime_command: tuple[str, ...] | None) -> str | None:
        if runtime_command is None:
            return None
        stem = Path(runtime_command[0]).stem.lower()
        return stem if stem in {"docker", "podman"} else "custom"

    def _runtime_command(self) -> tuple[str, ...] | None:
        if self.runtime_command is not None:
            return self.runtime_command or None
        for candidate in ("docker", "podman"):
            resolved = shutil.which(candidate)
            if resolved is not None:
                return (resolved,)
        return None

    def _container_argv(
        self,
        *,
        runtime_command: tuple[str, ...],
        command: SandboxCommand,
        workspace: Path,
        working_directory: Path,
    ) -> list[str]:
        argv = [*runtime_command, "run", "--rm"]
        if command.stdin is not None:
            argv.append("-i")
        if self.policy.network_policy == "deny":
            argv.extend(["--network", "none"])
        if self.policy.resource_policy.process_limit is not None:
            argv.extend(["--pids-limit", str(self.policy.resource_policy.process_limit)])
        if self.policy.resource_policy.memory_limit_bytes is not None:
            argv.extend(["--memory", str(self.policy.resource_policy.memory_limit_bytes)])
        if self.policy.resource_policy.cpu_time_limit_ms is not None:
            cpu_seconds = max(1, math.ceil(self.policy.resource_policy.cpu_time_limit_ms / 1000))
            argv.extend(["--ulimit", f"cpu={cpu_seconds}"])

        resolved_workspace = workspace.resolve()
        resolved_working_directory = working_directory.resolve()
        relative_workdir = resolved_working_directory.relative_to(resolved_workspace).as_posix()
        container_workdir = "/workspace" if relative_workdir == "." else f"/workspace/{relative_workdir}"
        argv.extend(["-v", f"{resolved_workspace}:/workspace", "-w", container_workdir])
        for name, value in self._container_environment(command).items():
            argv.extend(["-e", f"{name}={value}"])
        argv.append(self.image)
        argv.extend(command.argv)
        return argv

    def _container_environment(self, command: SandboxCommand) -> dict[str, str]:
        allowed_names = set(self.policy.allowed_environment)
        return {name: value for name, value in command.environment.items() if name in allowed_names}
