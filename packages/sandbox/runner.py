from __future__ import annotations

import base64
import json
import os
import math
import platform
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterator, Literal, Mapping, Sequence

from apps.api.app.models import FailureClass


NetworkPolicy = Literal["deny", "allow"]
ResourcePolicyEnforcement = Literal[
    "local_best_effort",
    "container_enforced",
    "runtime_isolated",
    "remote_isolated",
]
SandboxRunnerKind = Literal["local", "container", "gvisor", "firecracker", "remote_browser_runner"]
SandboxCapabilityName = Literal["network", "process", "cpu", "memory", "filesystem"]
SandboxCapabilityStatus = Literal["enforced", "best_effort", "unsupported", "unknown"]
AllowedCommand = str | Sequence[str]
DEFAULT_CONTAINER_IMAGE = "node:20-bookworm-slim"
PROFILE_ONLY_RUNNERS: tuple[SandboxRunnerKind, ...] = ("gvisor", "remote_browser_runner")


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


def _runner_label(runner_kind: SandboxRunnerKind) -> str:
    labels = {
        "local": "Local",
        "container": "Docker/Podman container",
        "gvisor": "gVisor",
        "firecracker": "Firecracker",
        "remote_browser_runner": "Remote Browser Runner",
    }
    return labels[runner_kind]


def sandbox_resource_policy_profile(
    resource_policy: "SandboxResourcePolicy | None" = None,
    *,
    runner_kind: SandboxRunnerKind,
    network_policy: NetworkPolicy = "deny",
    runtime_name: str | None = None,
    runtime_version: str | None = None,
    adapter_available: bool = False,
) -> "SandboxResourcePolicy":
    base_policy = resource_policy or SandboxResourcePolicy()
    if runner_kind == "local":
        return replace(
            base_policy,
            enforcement="local_best_effort",
            runner_kind="local",
            runtime_name=runtime_name,
            runtime_version=runtime_version,
            host_platform=_host_platform(),
            capabilities=_local_capabilities(),
            limitations=SandboxResourcePolicy().limitations,
        )
    if runner_kind == "container":
        return replace(
            base_policy,
            enforcement="container_enforced",
            runner_kind="container",
            runtime_name=runtime_name,
            runtime_version=runtime_version,
            host_platform=_host_platform(),
        )

    enforcement: ResourcePolicyEnforcement = (
        "remote_isolated" if runner_kind == "remote_browser_runner" else "runtime_isolated"
    )
    label = _runner_label(runner_kind)
    return replace(
        base_policy,
        enforcement=enforcement,
        runner_kind=runner_kind,
        runtime_name=runtime_name or _default_runtime_name(runner_kind),
        runtime_version=runtime_version,
        host_platform=_host_platform(),
        capabilities=_profile_capabilities(
            runner_kind,
            network_policy=network_policy,
            adapter_available=adapter_available,
        ),
        limitations=_profile_limitations(runner_kind, adapter_available=adapter_available, label=label),
    )


def _default_runtime_name(runner_kind: SandboxRunnerKind) -> str | None:
    if runner_kind == "gvisor":
        return "runsc"
    if runner_kind == "firecracker":
        return "firecracker"
    if runner_kind == "remote_browser_runner":
        return "playwright-remote"
    return None


def _profile_capabilities(
    runner_kind: SandboxRunnerKind,
    *,
    network_policy: NetworkPolicy,
    adapter_available: bool,
) -> tuple[SandboxRuntimeCapability, ...]:
    label = _runner_label(runner_kind)
    if not adapter_available:
        return tuple(
            SandboxRuntimeCapability(
                name=name,
                status="unsupported",
                detail=(
                    f"{label} is configured as an audit profile only; this process does not have a "
                    f"{label} execution adapter, so the capability is not applied."
                ),
            )
            for name in ("network", "process", "cpu", "memory", "filesystem")
        )

    if runner_kind == "remote_browser_runner":
        network_status: SandboxCapabilityStatus = "enforced" if network_policy == "deny" else "best_effort"
        return (
            SandboxRuntimeCapability(
                name="network",
                status=network_status,
                detail="Remote browser service owns browser egress policy and client network exposure rules.",
            ),
            SandboxRuntimeCapability(
                name="process",
                status="enforced",
                detail="Browser child processes run outside the Worker process boundary in the Browser Runner service.",
            ),
            SandboxRuntimeCapability(
                name="cpu",
                status="best_effort",
                detail="CPU limits are enforced by the remote service or orchestrator, not by the Worker process.",
            ),
            SandboxRuntimeCapability(
                name="memory",
                status="best_effort",
                detail="Memory limits are enforced by the remote service or orchestrator, not by the Worker process.",
            ),
            SandboxRuntimeCapability(
                name="filesystem",
                status="enforced",
                detail="Browser artifacts cross the service boundary through Artifact Store instead of host shared paths.",
            ),
        )

    network_status = "enforced" if network_policy == "deny" else "best_effort"
    return (
        SandboxRuntimeCapability(
            name="network",
            status=network_status,
            detail=f"{label} deployment profile requires network policy enforcement outside the Worker process.",
        ),
        SandboxRuntimeCapability(
            name="process",
            status="enforced",
            detail=f"{label} isolates workload processes behind a stronger runtime boundary.",
        ),
        SandboxRuntimeCapability(
            name="cpu",
            status="best_effort",
            detail=f"{label} CPU limits depend on the host runtime, cgroup, or microVM configuration.",
        ),
        SandboxRuntimeCapability(
            name="memory",
            status="best_effort",
            detail=f"{label} memory limits depend on the host runtime, cgroup, or microVM configuration.",
        ),
        SandboxRuntimeCapability(
            name="filesystem",
            status="enforced",
            detail=f"{label} deployment profile requires an isolated root filesystem or mediated workspace mount.",
        ),
    )


def _profile_limitations(
    runner_kind: SandboxRunnerKind,
    *,
    adapter_available: bool,
    label: str,
) -> tuple[str, ...]:
    adapter_note = (
        f"{label} execution adapter is not wired in this process; command execution is denied instead of falling back "
        "to a weaker runner."
        if not adapter_available
        else f"{label} adapter is selected; verify runtime-specific evidence before treating limits as enforced."
    )
    if runner_kind == "gvisor":
        return (
            "gVisor deployments must route container execution through runsc via Docker, containerd, Kubernetes, or OCI integration.",
            "gVisor improves syscall isolation but has Linux syscall, /proc, and /sys compatibility differences that can affect arbitrary generated projects.",
            adapter_note,
        )
    if runner_kind == "firecracker":
        return (
            "Firecracker deployments require Linux KVM, a prepared guest kernel/rootfs, jailer setup, and explicit Artifact Store exchange across the VM boundary.",
            "Firecracker provides a microVM boundary but host CPU, memory, network, storage, and metadata controls must be configured by the deployment layer.",
            adapter_note,
        )
    if runner_kind == "remote_browser_runner":
        return (
            "Remote Browser Runner is for browser/runtime validation isolation and does not execute build/typecheck commands in the Worker process.",
            "Playwright client/server versions, websocket authentication, client network exposure, and Artifact Store exchange must be pinned by deployment configuration.",
            adapter_note,
        )
    return (adapter_note,)


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


class FirecrackerSandboxRunner(LocalSandboxRunner):
    """Delegates sandbox commands to a deployment-provided Firecracker launcher."""

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        runner_command: Sequence[str] | None = None,
        runtime_name: str | None = None,
        runtime_version: str | None = None,
    ) -> None:
        self.runner_command = tuple(runner_command) if runner_command is not None else None
        profile_policy = policy or SandboxPolicy()
        resource_policy = sandbox_resource_policy_profile(
            profile_policy.resource_policy,
            runner_kind="firecracker",
            network_policy=profile_policy.network_policy,
            runtime_name=runtime_name,
            runtime_version=runtime_version,
            adapter_available=bool(self.runner_command),
        )
        super().__init__(replace(profile_policy, resource_policy=resource_policy))

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
        if not self.runner_command:
            return self._denied_result(
                command,
                workspace,
                started,
                "Firecracker runner command is not configured; set AI_JSUNPACK_FIRECRACKER_RUNNER_COMMAND "
                "or buildValidation.firecrackerRunnerCommand to use the Firecracker adapter.",
            )

        working_directory = self._working_directory(command, workspace)
        working_directory.mkdir(parents=True, exist_ok=True)
        request = self._launcher_request(
            command=command,
            workspace=workspace,
            working_directory=working_directory,
        )
        try:
            process = subprocess.Popen(
                list(self.runner_command),
                cwd=workspace,
                env=self._environment(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as error:
            denied_reason = f"Firecracker runner command could not be started: {error}"
            return SandboxResult(
                command=command.argv,
                stdout="",
                stderr=denied_reason,
                exit_code=None,
                duration_ms=self._duration_ms(started),
                failure_class="sandbox_denied",
                timed_out=False,
                output_truncated=False,
                working_directory=str(working_directory),
                network_policy=self.policy.network_policy,
                resource_policy=self.policy.resource_policy,
                denied_reason=denied_reason,
            )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = process.communicate(
                input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                timeout=max(self.policy.timeout_ms, 1) / 1000,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            stdout_bytes, stderr_bytes = process.communicate()

        launcher_stdout, launcher_stderr, launcher_output_truncated = self._decode_limited_output(
            stdout_bytes,
            stderr_bytes,
        )
        if timed_out:
            return SandboxResult(
                command=command.argv,
                stdout=launcher_stdout,
                stderr=launcher_stderr,
                exit_code=process.returncode,
                duration_ms=self._duration_ms(started),
                failure_class="timeout",
                timed_out=True,
                output_truncated=launcher_output_truncated,
                working_directory=str(working_directory),
                network_policy=self.policy.network_policy,
                resource_policy=self.policy.resource_policy,
            )
        response = self._parse_launcher_response(launcher_stdout)
        if response is None:
            return SandboxResult(
                command=command.argv,
                stdout=launcher_stdout,
                stderr=launcher_stderr,
                exit_code=process.returncode,
                duration_ms=self._duration_ms(started),
                failure_class="sandbox_denied",
                timed_out=False,
                output_truncated=launcher_output_truncated,
                working_directory=str(working_directory),
                network_policy=self.policy.network_policy,
                resource_policy=self.policy.resource_policy,
                denied_reason="Firecracker runner did not return a valid JSON result.",
            )

        stdout, stderr, guest_output_truncated = self._decode_limited_output(
            str(response.get("stdout", "")).encode("utf-8"),
            str(response.get("stderr", "")).encode("utf-8"),
        )
        output_truncated = launcher_output_truncated or bool(response.get("outputTruncated")) or guest_output_truncated
        exit_code = self._optional_int(response.get("exitCode"))
        guest_timed_out = bool(response.get("timedOut"))
        raw_failure_class = response.get("failureClass")
        failure_class = raw_failure_class if self._is_failure_class(raw_failure_class) else self._failure_class(
            exit_code=exit_code,
            timed_out=guest_timed_out,
            output_truncated=output_truncated,
            command_failure_class=command.failure_class,
        )
        return SandboxResult(
            command=command.argv,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=self._duration_ms(started),
            failure_class=failure_class,
            timed_out=guest_timed_out,
            output_truncated=output_truncated,
            working_directory=str(working_directory),
            network_policy=self.policy.network_policy,
            resource_policy=self.policy.resource_policy,
            denied_reason=response.get("deniedReason") if isinstance(response.get("deniedReason"), str) else None,
        )

    def _launcher_request(
        self,
        *,
        command: SandboxCommand,
        workspace: Path,
        working_directory: Path,
    ) -> dict[str, object]:
        resolved_workspace = workspace.resolve()
        resolved_working_directory = working_directory.resolve()
        relative_workdir = resolved_working_directory.relative_to(resolved_workspace).as_posix()
        return {
            "version": 1,
            "runnerKind": "firecracker",
            "workspace": str(resolved_workspace),
            "workingDirectory": "." if relative_workdir == "." else relative_workdir,
            "command": command.argv,
            "stdinBase64": base64.b64encode(command.stdin).decode("ascii") if command.stdin is not None else None,
            "environment": self._guest_environment(command),
            "timeoutMs": self.policy.timeout_ms,
            "outputLimitBytes": self.policy.output_limit_bytes,
            "networkPolicy": self.policy.network_policy,
            "resourcePolicy": self._camelize_mapping(asdict(self.policy.resource_policy)),
        }

    def _guest_environment(self, command: SandboxCommand) -> dict[str, str]:
        allowed_names = set(self.policy.allowed_environment)
        return {name: value for name, value in command.environment.items() if name in allowed_names}

    def _camelize_mapping(self, value: object) -> object:
        if isinstance(value, dict):
            return {self._snake_to_camel(str(key)): self._camelize_mapping(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._camelize_mapping(item) for item in value]
        if isinstance(value, tuple):
            return [self._camelize_mapping(item) for item in value]
        return value

    def _snake_to_camel(self, value: str) -> str:
        parts = value.split("_")
        return parts[0] + "".join(part.capitalize() for part in parts[1:])

    def _parse_launcher_response(self, stdout: str) -> dict[str, object] | None:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _optional_int(self, value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _is_failure_class(self, value: object) -> bool:
        return isinstance(value, str) and value in {
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
        }


class ProfileOnlySandboxRunner(LocalSandboxRunner):
    """Records a stronger sandbox profile and denies execution until an adapter is wired."""

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        runner_kind: SandboxRunnerKind,
        runtime_name: str | None = None,
        runtime_version: str | None = None,
    ) -> None:
        if runner_kind not in PROFILE_ONLY_RUNNERS:
            raise ValueError(f"{runner_kind!r} is not a profile-only sandbox runner.")
        profile_policy = policy or SandboxPolicy()
        resource_policy = sandbox_resource_policy_profile(
            profile_policy.resource_policy,
            runner_kind=runner_kind,
            network_policy=profile_policy.network_policy,
            runtime_name=runtime_name,
            runtime_version=runtime_version,
            adapter_available=False,
        )
        self.runner_kind = runner_kind
        self.runtime_label = _runner_label(runner_kind)
        super().__init__(replace(profile_policy, resource_policy=resource_policy))

    def run_in_workspace(
        self,
        command: SandboxCommand,
        workspace: Path,
        *,
        started_at: float | None = None,
    ) -> SandboxResult:
        started = started_at if started_at is not None else time.perf_counter()
        denied_reason = (
            f"{self.runtime_label} sandbox runner is configured as an audit profile, but this Worker does not "
            f"include a {self.runtime_label} execution adapter. Configure a supported adapter or use the container runner."
        )
        command_denied_reason = self._denied_reason(command, workspace)
        if command_denied_reason is not None:
            denied_reason = f"{command_denied_reason}; {denied_reason}"
        return self._denied_result(command, workspace, started, denied_reason)
