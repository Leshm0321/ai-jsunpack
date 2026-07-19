from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
# 调用器只启动固定的 Python 模块，绝不启用 shell。
import subprocess  # nosec B404
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence

from pydantic import ValidationError

from .agent_contracts import (
    CREWAI_DATA_ROOT_ENV,
    AgentModelPolicy,
    CrewAgentSpec,
    CrewStructuredAgentOutput,
)


AGENT_PROCESS_PROTOCOL_VERSION = 1
DEFAULT_PROCESS_TIMEOUT_GRACE_SECONDS = 5.0
DEFAULT_WORKER_MODULE = "apps.worker.worker.agent_process_worker"

ProcessInvocationStatus = Literal[
    "success",
    "timeout",
    "child_error",
    "exit_error",
    "invalid_response",
    "process_error",
]


@dataclass(frozen=True)
class CrewAIProcessRequest:
    job_id: str
    spec: CrewAgentSpec
    prompt_context: dict[str, Any]
    policy: AgentModelPolicy
    process_timeout_seconds: float | None = None


@dataclass(frozen=True)
class CrewAIProcessResult:
    status: ProcessInvocationStatus
    message: str
    duration_ms: float
    data_root: str
    invocation_id: str
    output: CrewStructuredAgentOutput | None = None
    process_exit_status: int | None = None
    isolation_mode: str = "process"

    @property
    def succeeded(self) -> bool:
        return self.status == "success" and self.output is not None


class CrewAIProcessError(RuntimeError):
    def __init__(self, result: CrewAIProcessResult) -> None:
        super().__init__(result.message)
        self.result = result


class CrewAIProcessInvoker(Protocol):
    parallel_safe: bool
    isolation_mode: str

    def invoke(self, request: CrewAIProcessRequest) -> CrewAIProcessResult:
        ...


class SubprocessCrewAIInvoker:
    """在使用隔离存储的全新 Python 进程中运行每轮 CrewAI 调用。"""

    parallel_safe = True
    isolation_mode = "process"

    def __init__(
        self,
        *,
        data_root_base: str | Path | None = None,
        python_executable: str | None = None,
        worker_module: str = DEFAULT_WORKER_MODULE,
        timeout_grace_seconds: float = DEFAULT_PROCESS_TIMEOUT_GRACE_SECONDS,
    ) -> None:
        configured_root = data_root_base or os.getenv(CREWAI_DATA_ROOT_ENV) or Path.cwd() / ".crewai-data"
        self.data_root_base = Path(configured_root).resolve()
        self.python_executable = python_executable or sys.executable
        self.worker_module = worker_module
        self.timeout_grace_seconds = max(0.0, timeout_grace_seconds)

    def invoke(self, request: CrewAIProcessRequest) -> CrewAIProcessResult:
        started = time.monotonic()
        invocation_id = uuid.uuid4().hex
        data_root = self._invocation_data_root(request, invocation_id)
        payload = self._request_payload(request, invocation_id=invocation_id, data_root=data_root)
        timeout_seconds = self._process_timeout(request)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

        try:
            completed = subprocess.run(  # nosec B603  # noqa: S603 - 可执行文件与模块参数均为固定值。
                [self.python_executable, "-m", self.worker_module],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired:
            return self._result(
                status="timeout",
                message=f"CrewAI 子进程在 {timeout_seconds:g} 秒后超时。",
                started=started,
                data_root=data_root,
                invocation_id=invocation_id,
            )
        except OSError as error:
            return self._result(
                status="process_error",
                message=f"CrewAI 子进程无法启动（{type(error).__name__}）。",
                started=started,
                data_root=data_root,
                invocation_id=invocation_id,
            )

        if completed.returncode != 0:
            return self._result(
                status="exit_error",
                message=f"CrewAI 子进程以状态 {completed.returncode} 退出。",
                started=started,
                data_root=data_root,
                invocation_id=invocation_id,
                process_exit_status=completed.returncode,
            )

        response = self._decode_protocol_response(completed.stdout)
        if response is None:
            return self._result(
                status="invalid_response",
                message="CrewAI 子进程返回了无效 JSON。",
                started=started,
                data_root=data_root,
                invocation_id=invocation_id,
                process_exit_status=completed.returncode,
            )
        if not isinstance(response, dict) or response.get("protocolVersion") != AGENT_PROCESS_PROTOCOL_VERSION:
            return self._result(
                status="invalid_response",
                message="CrewAI 子进程返回了不兼容的协议响应。",
                started=started,
                data_root=data_root,
                invocation_id=invocation_id,
                process_exit_status=completed.returncode,
            )
        if response.get("status") != "success":
            error_kind = response.get("errorKind")
            return self._result(
                status="invalid_response" if error_kind == "schema_error" else "child_error",
                message=(
                    "CrewAI 子进程调用返回了不符合角色 schema 的输出。"
                    if error_kind == "schema_error"
                    else "CrewAI 子进程调用失败。"
                ),
                started=started,
                data_root=data_root,
                invocation_id=invocation_id,
                process_exit_status=completed.returncode,
            )

        sanitized_output = self._redact_sensitive_values(
            response.get("output"),
            sensitive_values=(request.policy.api_key, request.policy.base_url),
        )
        try:
            output = CrewStructuredAgentOutput.model_validate(sanitized_output)
        except ValidationError:
            return self._result(
                status="invalid_response",
                message="CrewAI 子进程返回了不符合 schema 的输出。",
                started=started,
                data_root=data_root,
                invocation_id=invocation_id,
                process_exit_status=completed.returncode,
            )
        return self._result(
            status="success",
            message=f"{request.spec.name} 已在隔离的 CrewAI 子进程中完成。",
            started=started,
            data_root=data_root,
            invocation_id=invocation_id,
            process_exit_status=completed.returncode,
            output=output,
        )

    def _decode_protocol_response(self, stdout: str) -> dict[str, Any] | None:
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(stdout)
            if isinstance(payload, dict):
                return payload
        for line in reversed(stdout.splitlines()):
            with contextlib.suppress(json.JSONDecodeError):
                payload = json.loads(line)
                if isinstance(payload, dict) and payload.get("protocolVersion") == AGENT_PROCESS_PROTOCOL_VERSION:
                    return payload
        return None

    def _request_payload(
        self,
        request: CrewAIProcessRequest,
        *,
        invocation_id: str,
        data_root: Path,
    ) -> dict[str, Any]:
        policy = asdict(request.policy)
        return {
            "protocolVersion": AGENT_PROCESS_PROTOCOL_VERSION,
            "invocationId": invocation_id,
            "jobId": request.job_id,
            "dataRoot": str(data_root),
            "spec": asdict(request.spec),
            "promptContext": request.prompt_context,
            "policy": policy,
        }

    def _invocation_data_root(self, request: CrewAIProcessRequest, invocation_id: str) -> Path:
        return (
            self.data_root_base
            / "isolated"
            / self._safe_path_segment(request.job_id)
            / self._safe_path_segment(request.spec.name)
            / invocation_id
        )

    def _process_timeout(self, request: CrewAIProcessRequest) -> float:
        if request.process_timeout_seconds is not None:
            return max(0.1, request.process_timeout_seconds)
        return max(0.1, request.policy.timeout_seconds + self.timeout_grace_seconds)

    def _result(
        self,
        *,
        status: ProcessInvocationStatus,
        message: str,
        started: float,
        data_root: Path,
        invocation_id: str,
        output: CrewStructuredAgentOutput | None = None,
        process_exit_status: int | None = None,
    ) -> CrewAIProcessResult:
        result = CrewAIProcessResult(
            status=status,
            message=message,
            duration_ms=(time.monotonic() - started) * 1000,
            data_root=str(data_root),
            invocation_id=invocation_id,
            output=output,
            process_exit_status=process_exit_status,
        )
        shutil.rmtree(data_root, ignore_errors=True)
        return result

    def _redact_sensitive_values(self, value: Any, *, sensitive_values: Sequence[str | None]) -> Any:
        secrets = tuple(item for item in sensitive_values if item)
        if isinstance(value, str):
            redacted = value
            for secret in secrets:
                redacted = redacted.replace(secret, "[REDACTED]")
            return redacted
        if isinstance(value, list):
            return [self._redact_sensitive_values(item, sensitive_values=secrets) for item in value]
        if isinstance(value, dict):
            return {
                key: self._redact_sensitive_values(item, sensitive_values=secrets)
                for key, item in value.items()
            }
        return value

    def _safe_path_segment(self, value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
        return (normalized or "unknown")[:80]


class IsolatedCrewAIBackend:
    """由每次调用一个子进程支撑、可直接替换 CrewAIBackend 的实现。"""

    parallel_safe = True
    isolation_mode = "process"

    def __init__(self, *, invoker: CrewAIProcessInvoker | None = None) -> None:
        self.invoker = invoker or SubprocessCrewAIInvoker()
        self._local = threading.local()

    def run_agent(
        self,
        *,
        spec: CrewAgentSpec,
        prompt_context: dict[str, Any],
        policy: AgentModelPolicy,
    ) -> CrewStructuredAgentOutput:
        job_id = str(prompt_context.get("jobId") or "unknown-job")
        result = self.invoker.invoke(
            CrewAIProcessRequest(
                job_id=job_id,
                spec=spec,
                prompt_context=prompt_context,
                policy=policy,
            )
        )
        self._local.last_result = result
        if not result.succeeded or result.output is None:
            raise CrewAIProcessError(result)
        return result.output

    def consume_last_result(self) -> CrewAIProcessResult | None:
        result = getattr(self._local, "last_result", None)
        if hasattr(self._local, "last_result"):
            del self._local.last_result
        return result


class BoundedCrewAIProcessPool:
    """在保持请求顺序的同时限制独立进程调用数量。"""

    parallel_safe = True
    isolation_mode = "process"

    def __init__(self, *, max_parallel: int, invoker: CrewAIProcessInvoker | None = None) -> None:
        if not 1 <= max_parallel <= 10:
            raise ValueError("max_parallel 必须介于 1 和 10 之间。")
        self.max_parallel = max_parallel
        self.invoker = invoker or SubprocessCrewAIInvoker()

    def invoke_all(self, requests: Sequence[CrewAIProcessRequest]) -> list[CrewAIProcessResult]:
        if not requests:
            return []
        worker_count = min(self.max_parallel, len(requests))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="crewai-process") as executor:
            return list(executor.map(self.invoker.invoke, requests))


__all__ = [
    "AGENT_PROCESS_PROTOCOL_VERSION",
    "BoundedCrewAIProcessPool",
    "CrewAIProcessError",
    "CrewAIProcessInvoker",
    "CrewAIProcessRequest",
    "CrewAIProcessResult",
    "IsolatedCrewAIBackend",
    "ProcessInvocationStatus",
    "SubprocessCrewAIInvoker",
]
