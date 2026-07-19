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
PROFILE_ONLY_RUNNERS: tuple[SandboxRunnerKind, ...] = ("remote_browser_runner",)
DEFAULT_GVISOR_RUNTIME_NAME = "runsc"
DEPLOYMENT_PROFILE_ENV = "AI_JSUNPACK_DEPLOYMENT_PROFILE"
SANDBOX_WORKSPACE_ROOT_ENV = "AI_JSUNPACK_SANDBOX_WORKSPACE_ROOT"
SANDBOX_VOLUME_NAME_ENV = "AI_JSUNPACK_SANDBOX_VOLUME_NAME"


def deployment_profile(value: object = None) -> str:
    configured = value if isinstance(value, str) and value.strip() else os.getenv(DEPLOYMENT_PROFILE_ENV, "development")
    return str(configured).strip().lower().replace("-", "_")


def is_production_profile(value: object = None) -> bool:
    return deployment_profile(value) in {"production", "prod"}


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
            detail="Local Sandbox Runner 会记录网络策略，但不会强制实施 OS 级网络隔离。",
        ),
        SandboxRuntimeCapability(
            name="process",
            status="best_effort",
            detail="Local Sandbox Runner 会记录进程限制，但不会强制实施进程数量边界。",
        ),
        SandboxRuntimeCapability(
            name="cpu",
            status="best_effort",
            detail="Local Sandbox Runner 仅强制实施墙钟超时；CPU 时间限制仅作为审计元数据。",
        ),
        SandboxRuntimeCapability(
            name="memory",
            status="best_effort",
            detail="Local Sandbox Runner 会记录内存限制，但不会强制实施 OS 内存边界。",
        ),
        SandboxRuntimeCapability(
            name="filesystem",
            status="best_effort",
            detail="Local Sandbox Runner 在临时尝试工作区中执行，并验证相对工作目录。",
        ),
    )


def _runner_label(runner_kind: SandboxRunnerKind) -> str:
    labels = {
        "local": "Local Sandbox Runner",
        "container": "Container Sandbox Runner",
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
                    f"{label} 是 profile-only Sandbox Runner；当前进程没有 {label} execution adapter，"
                    "因此不会应用该能力。"
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
                detail="Remote Browser Runner 服务负责浏览器出站策略和客户端网络暴露规则。",
            ),
            SandboxRuntimeCapability(
                name="process",
                status="enforced",
                detail="浏览器子进程在 Browser Runner 服务中运行，位于 Worker 进程边界之外。",
            ),
            SandboxRuntimeCapability(
                name="cpu",
                status="best_effort",
                detail="CPU 限制由远程服务或 orchestrator 实施，而非 Worker 进程。",
            ),
            SandboxRuntimeCapability(
                name="memory",
                status="best_effort",
                detail="内存限制由远程服务或 orchestrator 实施，而非 Worker 进程。",
            ),
            SandboxRuntimeCapability(
                name="filesystem",
                status="enforced",
                detail="浏览器 Artifact 通过 Artifact Store 跨越服务边界，而非使用主机共享路径。",
            ),
        )

    network_status = "enforced" if network_policy == "deny" else "best_effort"
    return (
        SandboxRuntimeCapability(
            name="network",
            status=network_status,
            detail=f"{label} deployment profile 要求在 Worker 进程之外实施网络策略。",
        ),
        SandboxRuntimeCapability(
            name="process",
            status="enforced",
            detail=f"{label} 通过更强的 runtime 边界隔离工作负载进程。",
        ),
        SandboxRuntimeCapability(
            name="cpu",
            status="best_effort",
            detail=f"{label} 的 CPU 限制取决于主机 runtime、cgroup 或 microVM 配置。",
        ),
        SandboxRuntimeCapability(
            name="memory",
            status="best_effort",
            detail=f"{label} 的内存限制取决于主机 runtime、cgroup 或 microVM 配置。",
        ),
        SandboxRuntimeCapability(
            name="filesystem",
            status="enforced",
            detail=f"{label} deployment profile 要求隔离的根文件系统或受控 workspace 挂载。",
        ),
    )


def _profile_limitations(
    runner_kind: SandboxRunnerKind,
    *,
    adapter_available: bool,
    label: str,
) -> tuple[str, ...]:
    adapter_note = (
        f"{label} 执行适配器尚未接入当前进程；命令执行会被拒绝，"
        "而不会回退到隔离更弱的 Sandbox Runner。"
        if not adapter_available
        else f"{label} 适配器已选中；在将限制视为已实施前，请验证运行时专属证据。"
    )
    if runner_kind == "gvisor":
        return (
            "gVisor 部署必须通过 Docker、containerd、Kubernetes 或 OCI 集成，将容器执行路由到 runsc。",
            "gVisor 可改善 syscall 隔离，但 Linux syscall、/proc 和 /sys 存在兼容性差异，可能影响任意生成项目。",
            adapter_note,
        )
    if runner_kind == "firecracker":
        return (
            "Firecracker 部署需要 Linux KVM、准备好的 guest kernel/rootfs、jailer 配置，并通过 VM 边界显式交换 Artifact Store 数据。",
            "Firecracker 提供 microVM 边界，但主机 CPU、内存、网络、存储和元数据控制必须由部署层配置。",
            adapter_note,
        )
    if runner_kind == "remote_browser_runner":
        return (
            "Remote Browser Runner 用于隔离浏览器/runtime 验证，不会在 Worker 进程中执行 build/typecheck 命令。",
            "Playwright client/server 版本、websocket authentication、client network exposure 和 Artifact Store 交换必须由 deployment profile 固定。",
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
        "Local Sandbox Runner 会记录进程、CPU 和内存策略，但不会强制实施 OS/容器隔离。",
        "Local Sandbox Runner 仅用于开发和审计；不得将其视为生产多租户隔离边界。",
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
    deployment_profile: str = field(default_factory=deployment_profile)


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
    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.policy = policy or SandboxPolicy()
        configured_root = workspace_root if workspace_root is not None else os.getenv(SANDBOX_WORKSPACE_ROOT_ENV)
        self.workspace_root = Path(configured_root).resolve() if configured_root else None

    @contextmanager
    def attempt_workspace(self) -> Iterator[Path]:
        if self.workspace_root is not None:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=self.policy.temp_prefix,
            dir=str(self.workspace_root) if self.workspace_root is not None else None,
        ) as temp_dir:
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
        if self.policy.resource_policy.runner_kind == "local" and is_production_profile(self.policy.deployment_profile):
            return "production deployment profile 已禁用 Local Sandbox Runner 执行。"
        if not self._is_allowed(command.argv):
            return f"不允许执行命令：{command.executable}"
        if command.working_directory is None:
            return None
        candidate = Path(command.working_directory)
        if candidate.is_absolute():
            return "沙箱工作目录必须是相对于尝试工作区的路径。"
        resolved = (workspace / candidate).resolve()
        try:
            resolved.relative_to(workspace.resolve())
        except ValueError:
            return "沙箱工作目录逃逸出尝试工作区。"
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
    """通过 Docker 或 Podman 执行 Container Sandbox Runner 命令，并应用容器级限制。"""

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        image: str = DEFAULT_CONTAINER_IMAGE,
        runtime_command: Sequence[str] | None = None,
        workspace_root: Path | str | None = None,
        volume_name: str | None = None,
    ) -> None:
        self.image = image
        self.runtime_command = tuple(runtime_command) if runtime_command is not None else None
        configured_volume = volume_name if volume_name is not None else os.getenv(SANDBOX_VOLUME_NAME_ENV)
        self.volume_name = configured_volume.strip() if isinstance(configured_volume, str) else ""
        runtime = self._runtime_command()
        super().__init__(
            self._container_policy(policy or SandboxPolicy(), runtime_command=runtime),
            workspace_root=workspace_root,
        )

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
                "container runtime 不可用；请安装 docker 或 podman，或使用 Local Sandbox Runner。",
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

    def _denied_reason(self, command: SandboxCommand, workspace: Path) -> str | None:
        denied_reason = super()._denied_reason(command, workspace)
        if denied_reason is not None or not self.volume_name:
            return denied_reason
        if self.workspace_root is None:
            return f"设置 {SANDBOX_VOLUME_NAME_ENV} 时必须配置 {SANDBOX_WORKSPACE_ROOT_ENV}。"
        try:
            self._volume_subpath(workspace)
        except ValueError as error:
            return str(error)
        return None

    def _container_policy(self, policy: SandboxPolicy, *, runtime_command: tuple[str, ...] | None) -> SandboxPolicy:
        resource_policy = policy.resource_policy
        limitations = tuple(resource_policy.limitations or ())
        local_default_limitations = SandboxResourcePolicy().limitations
        container_limitations = (
            "Container Sandbox Runner 将资源策略映射为 Docker/Podman 参数；实施效果取决于所选 container runtime、主机 OS 和容器 backend。",
            "CPU 时间限制使用容器 ulimit 设置，在不同 Docker/Podman 和主机平台上仍属于 best_effort。",
            "Windows 和 macOS 的容器资源控制由底层 Linux VM/cgroup 层实施（若可用）。",
            "生产暴露前，如需更严格的多租户隔离，请评估 gVisor、Firecracker 或等效的更强 sandbox runtime。",
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
        runtime_label = runtime_name or "不可用的容器 runtime"
        if runtime_name is None:
            return (
                SandboxRuntimeCapability(
                    name="network",
                    status="unsupported",
                    detail="未找到 Docker 或 Podman runtime，因此无法应用网络隔离。",
                ),
                SandboxRuntimeCapability(
                    name="process",
                    status="unsupported",
                    detail="未找到 Docker 或 Podman runtime，因此无法应用进程限制。",
                ),
                SandboxRuntimeCapability(
                    name="cpu",
                    status="unsupported",
                    detail="未找到 Docker 或 Podman runtime，因此无法应用 CPU 限制。",
                ),
                SandboxRuntimeCapability(
                    name="memory",
                    status="unsupported",
                    detail="未找到 Docker 或 Podman runtime，因此无法应用内存限制。",
                ),
                SandboxRuntimeCapability(
                    name="filesystem",
                    status="unsupported",
                    detail="未找到 Docker 或 Podman runtime，因此无法把尝试工作区挂载到容器中。",
                ),
            )

        network_status: SandboxCapabilityStatus = "enforced" if network_policy == "deny" else "best_effort"
        network_detail = (
            f"{runtime_label} 将接收 --network none 以实施 deny 策略。"
            if network_policy == "deny"
            else f"策略允许网络访问；未请求 {runtime_label} 网络隔离。"
        )
        process_status: SandboxCapabilityStatus = "enforced" if resource_policy.process_limit is not None else "best_effort"
        process_detail = (
            f"{runtime_label} 将接收 --pids-limit={resource_policy.process_limit}。"
            if resource_policy.process_limit is not None
            else f"未配置进程限制；{runtime_label} 使用默认进程边界。"
        )
        memory_status: SandboxCapabilityStatus = "enforced" if resource_policy.memory_limit_bytes is not None else "best_effort"
        memory_detail = (
            f"{runtime_label} 将接收 --memory={resource_policy.memory_limit_bytes}；实施效果取决于主机/容器 backend 的 cgroup 支持。"
            if resource_policy.memory_limit_bytes is not None
            else f"未配置内存限制；{runtime_label} 使用默认内存边界。"
        )
        cpu_detail = (
            f"{runtime_label} 将接收由 {resource_policy.cpu_time_limit_ms} ms 换算的 CPU ulimit；不同 Docker/Podman 和主机平台的支持情况存在差异。"
            if resource_policy.cpu_time_limit_ms is not None
            else f"未配置 CPU 时间限制；{runtime_label} 使用默认 CPU 边界。"
        )
        filesystem_detail = (
            f"尝试工作区从 Docker volume {self.volume_name!r} 挂载到 /workspace，并为每次尝试使用 volume subpath。"
            if self.volume_name
            else "尝试工作区以 bind mount 挂载到 /workspace；未配置只读根文件系统或更强的 runtime 隔离。"
        )
        return (
            SandboxRuntimeCapability(name="network", status=network_status, detail=network_detail),
            SandboxRuntimeCapability(name="process", status=process_status, detail=process_detail),
            SandboxRuntimeCapability(name="cpu", status="best_effort", detail=cpu_detail),
            SandboxRuntimeCapability(name="memory", status=memory_status, detail=memory_detail),
            SandboxRuntimeCapability(
                name="filesystem",
                status="best_effort",
                detail=filesystem_detail,
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
        if self.volume_name:
            volume_subpath = self._volume_subpath(resolved_workspace)
            argv.extend(
                [
                    "--mount",
                    f"type=volume,src={self.volume_name},dst=/workspace,volume-subpath={volume_subpath}",
                    "-w",
                    container_workdir,
                ]
            )
        else:
            argv.extend(["-v", f"{resolved_workspace}:/workspace", "-w", container_workdir])
        for name, value in self._container_environment(command).items():
            argv.extend(["-e", f"{name}={value}"])
        argv.append(self.image)
        argv.extend(command.argv)
        return argv

    def _volume_subpath(self, workspace: Path) -> str:
        if self.workspace_root is None:
            raise ValueError(f"设置 {SANDBOX_VOLUME_NAME_ENV} 时必须配置 {SANDBOX_WORKSPACE_ROOT_ENV}。")
        try:
            volume_subpath = workspace.resolve().relative_to(self.workspace_root).as_posix()
        except ValueError as error:
            raise ValueError("Sandbox workspace 位于配置的具名 volume 根目录之外。") from error
        if not volume_subpath or volume_subpath == ".":
            raise ValueError("Sandbox 具名 volume workspace 必须使用非根子目录。")
        return volume_subpath

    def _container_environment(self, command: SandboxCommand) -> dict[str, str]:
        allowed_names = set(self.policy.allowed_environment)
        return {name: value for name, value in command.environment.items() if name in allowed_names}


class GVisorSandboxRunner(ContainerSandboxRunner):
    """通过 Docker/Podman 和 gVisor runsc runtime 执行 sandbox 命令。"""

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        image: str = DEFAULT_CONTAINER_IMAGE,
        runtime_command: Sequence[str] | None = None,
        gvisor_runtime: str | None = None,
        runtime_version: str | None = None,
    ) -> None:
        self.gvisor_runtime = (gvisor_runtime or DEFAULT_GVISOR_RUNTIME_NAME).strip() or DEFAULT_GVISOR_RUNTIME_NAME
        self.gvisor_runtime_version = runtime_version
        super().__init__(policy, image=image, runtime_command=runtime_command)

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
        if self._runtime_command() is None:
            return self._denied_result(
                command,
                workspace,
                started,
                "未配置 gVisor container runtime 命令；请设置 "
                "AI_JSUNPACK_SANDBOX_GVISOR_RUNTIME_COMMAND、buildValidation.gvisorRuntimeCommand，"
                "或提供已配置 runsc 的 Docker/Podman。",
            )
        return super().run_in_workspace(command, workspace, started_at=started)

    def _container_policy(self, policy: SandboxPolicy, *, runtime_command: tuple[str, ...] | None) -> SandboxPolicy:
        resource_policy = sandbox_resource_policy_profile(
            policy.resource_policy,
            runner_kind="gvisor",
            network_policy=policy.network_policy,
            runtime_name=self.gvisor_runtime,
            runtime_version=self.gvisor_runtime_version,
            adapter_available=runtime_command is not None,
        )
        return replace(policy, resource_policy=resource_policy)

    def _container_argv(
        self,
        *,
        runtime_command: tuple[str, ...],
        command: SandboxCommand,
        workspace: Path,
        working_directory: Path,
    ) -> list[str]:
        argv = super()._container_argv(
            runtime_command=runtime_command,
            command=command,
            workspace=workspace,
            working_directory=working_directory,
        )
        image_index = argv.index(self.image)
        argv[image_index:image_index] = ["--runtime", self.gvisor_runtime]
        return argv


class FirecrackerSandboxRunner(LocalSandboxRunner):
    """将 sandbox 命令委托给部署方提供的 Firecracker launcher。"""

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
                "未配置 Firecracker 运行器命令；请设置 AI_JSUNPACK_FIRECRACKER_RUNNER_COMMAND，"
                "或通过 buildValidation.firecrackerRunnerCommand 使用 Firecracker 适配器。",
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
            denied_reason = f"无法启动 Firecracker 运行器命令：{error}"
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
                denied_reason="Firecracker 运行器未返回有效的 JSON 结果。",
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
    """记录 profile-only Sandbox Runner 配置，并在 adapter 接入前拒绝执行。"""

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        runner_kind: SandboxRunnerKind,
        runtime_name: str | None = None,
        runtime_version: str | None = None,
    ) -> None:
        if runner_kind not in PROFILE_ONLY_RUNNERS:
            raise ValueError(f"{runner_kind!r} 不是 profile-only Sandbox Runner。")
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
            f"{self.runtime_label} 已配置为 profile-only Sandbox Runner 的审计 profile，但此 Worker 不包含 "
            f"{self.runtime_label} execution adapter。请配置受支持的 adapter，或使用 Container Sandbox Runner。"
        )
        command_denied_reason = self._denied_reason(command, workspace)
        if command_denied_reason is not None:
            denied_reason = f"{command_denied_reason}; {denied_reason}"
        return self._denied_result(command, workspace, started, denied_reason)
