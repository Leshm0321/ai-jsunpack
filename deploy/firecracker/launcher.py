#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FAILURE_CLASSES = {
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


class LauncherError(RuntimeError):
    def __init__(self, message: str, failure_class: str = "sandbox_denied") -> None:
        super().__init__(message)
        self.failure_class = failure_class


@dataclass(frozen=True)
class PreparedRequest:
    workspace: Path
    working_directory: Path
    working_directory_relative: str
    command: list[str]
    stdin_bytes: bytes | None
    environment: dict[str, str]
    timeout_ms: int
    output_limit_bytes: int
    network_policy: str
    resource_policy: dict[str, Any]


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    started_at = time.perf_counter()

    try:
        request = parse_request(sys.stdin.read())
        prepared = prepare_request(request)
        response = run_launcher(args, prepared, started_at=started_at)
    except LauncherError as error:
        response = failure_response(str(error), error.failure_class)
    except Exception as error:  # pragma: no cover - last-ditch production guard
        response = failure_response(f"Firecracker launcher failed unexpectedly: {error}", "unknown")

    print(json.dumps(response, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Production Firecracker launcher template for ai-jsunpack. It validates the Worker protocol, "
            "prepares a microVM exchange directory, and delegates actual KVM/jailer execution to a deployment "
            "wrapper command."
        )
    )
    parser.add_argument("--kernel", required=True, help="Path to the prepared guest kernel image.")
    parser.add_argument("--rootfs", required=True, help="Path to the prepared guest rootfs image.")
    parser.add_argument("--jailer", default="firecracker-jailer", help="Firecracker jailer binary path.")
    parser.add_argument("--firecracker", default="firecracker", help="Firecracker binary path.")
    parser.add_argument("--socket-dir", default="/run/ai-jsunpack/firecracker", help="Host directory for API sockets.")
    parser.add_argument("--tap-device", default="", help="Optional tap device used only when networkPolicy is allow.")
    parser.add_argument(
        "--wrapper-command",
        nargs="+",
        help=(
            "Deployment command that starts the microVM and executes the guest command. It receives a JSON "
            "control document on stdin and must return the standard launcher JSON response on stdout."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and echo the protocol without starting a microVM. For deployment smoke tests only.",
    )
    return parser


def parse_request(raw_stdin: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_stdin)
    except json.JSONDecodeError as error:
        raise LauncherError(f"Invalid launcher request JSON: {error}", "invalid_input") from error
    if not isinstance(payload, dict):
        raise LauncherError("Launcher request must be a JSON object.", "invalid_input")
    return payload


def prepare_request(payload: dict[str, Any]) -> PreparedRequest:
    if payload.get("version") != 1:
        raise LauncherError("Unsupported launcher request version.", "invalid_input")
    if payload.get("runnerKind") != "firecracker":
        raise LauncherError("Launcher request runnerKind must be firecracker.", "invalid_input")

    workspace = _safe_existing_directory(payload.get("workspace"), field_name="workspace")
    working_directory_relative = _relative_working_directory(payload.get("workingDirectory"))
    working_directory = (workspace / working_directory_relative).resolve()
    try:
        working_directory.relative_to(workspace.resolve())
    except ValueError as error:
        raise LauncherError("workingDirectory escaped the workspace.", "sandbox_denied") from error
    if not working_directory.is_dir():
        raise LauncherError("workingDirectory must exist under workspace.", "invalid_input")

    command = payload.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise LauncherError("command must be a non-empty string array.", "invalid_input")

    environment = payload.get("environment") or {}
    if not isinstance(environment, dict) or not all(
        isinstance(name, str) and isinstance(value, str) for name, value in environment.items()
    ):
        raise LauncherError("environment must be an object with string keys and values.", "invalid_input")

    stdin_bytes = _stdin_bytes(payload.get("stdinBase64"))
    resource_policy = payload.get("resourcePolicy") or {}
    if not isinstance(resource_policy, dict):
        raise LauncherError("resourcePolicy must be a JSON object.", "invalid_input")

    return PreparedRequest(
        workspace=workspace,
        working_directory=working_directory,
        working_directory_relative=working_directory_relative,
        command=command,
        stdin_bytes=stdin_bytes,
        environment=environment,
        timeout_ms=_positive_int(payload.get("timeoutMs"), default=120_000),
        output_limit_bytes=_positive_int(payload.get("outputLimitBytes"), default=128 * 1024),
        network_policy=_network_policy(payload.get("networkPolicy")),
        resource_policy=resource_policy,
    )


def run_launcher(args: argparse.Namespace, request: PreparedRequest, *, started_at: float) -> dict[str, Any]:
    ensure_runtime_inputs(args)
    with tempfile.TemporaryDirectory(prefix="ai-jsunpack-firecracker-") as exchange_dir:
        exchange_path = Path(exchange_dir)
        control_path = exchange_path / "control.json"
        stdin_path = exchange_path / "stdin.bin"
        control = control_document(args, request, exchange_path)
        control_path.write_text(json.dumps(control, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        if request.stdin_bytes is not None:
            stdin_path.write_bytes(request.stdin_bytes)

        if args.dry_run:
            return success_response(
                stdout=json.dumps(
                    {
                        "dryRun": True,
                        "runnerKind": "firecracker",
                        "command": request.command,
                        "workingDirectory": request.working_directory_relative,
                        "networkPolicy": request.network_policy,
                        "resourcePolicy": request.resource_policy,
                    },
                    sort_keys=True,
                ),
                stderr="",
                exit_code=0,
                timed_out=False,
                output_truncated=False,
                failure_class="none",
            )

        if not args.wrapper_command:
            raise LauncherError(
                "No --wrapper-command was provided; configure the deployment-specific Firecracker/jailer wrapper.",
                "sandbox_denied",
            )

        return run_wrapper(args.wrapper_command, control, request, started_at=started_at)


def ensure_runtime_inputs(args: argparse.Namespace) -> None:
    for label, path_value in (("kernel", args.kernel), ("rootfs", args.rootfs)):
        path = Path(path_value)
        if not path.is_file():
            raise LauncherError(f"Firecracker {label} image does not exist: {path}", "sandbox_denied")
    Path(args.socket_dir).mkdir(parents=True, exist_ok=True)
    if shutil.which(args.jailer) is None and not Path(args.jailer).exists():
        raise LauncherError(f"Firecracker jailer binary is unavailable: {args.jailer}", "sandbox_denied")
    if shutil.which(args.firecracker) is None and not Path(args.firecracker).exists():
        raise LauncherError(f"Firecracker binary is unavailable: {args.firecracker}", "sandbox_denied")


def control_document(args: argparse.Namespace, request: PreparedRequest, exchange_path: Path) -> dict[str, Any]:
    resource = request.resource_policy
    return {
        "version": 1,
        "runnerKind": "firecracker",
        "kernel": str(Path(args.kernel).resolve()),
        "rootfs": str(Path(args.rootfs).resolve()),
        "jailer": args.jailer,
        "firecracker": args.firecracker,
        "socketDir": str(Path(args.socket_dir).resolve()),
        "tapDevice": args.tap_device if request.network_policy == "allow" else None,
        "networkPolicy": request.network_policy,
        "workspace": str(request.workspace),
        "workingDirectory": request.working_directory_relative,
        "exchangeDir": str(exchange_path),
        "command": request.command,
        "environment": request.environment,
        "stdinPath": str(exchange_path / "stdin.bin") if request.stdin_bytes is not None else None,
        "timeoutMs": request.timeout_ms,
        "outputLimitBytes": request.output_limit_bytes,
        "resourcePolicy": resource,
        "limits": {
            "processLimit": resource.get("processLimit"),
            "cpuTimeLimitMs": resource.get("cpuTimeLimitMs"),
            "memoryLimitBytes": resource.get("memoryLimitBytes"),
        },
    }


def run_wrapper(
    wrapper_command: list[str],
    control: dict[str, Any],
    request: PreparedRequest,
    *,
    started_at: float,
) -> dict[str, Any]:
    try:
        process = subprocess.Popen(
            wrapper_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except OSError as error:
        raise LauncherError(f"Firecracker wrapper could not be started: {error}", "sandbox_denied") from error

    timed_out = False
    try:
        stdout_bytes, stderr_bytes = process.communicate(
            json.dumps(control, ensure_ascii=False).encode("utf-8"),
            timeout=max(request.timeout_ms, 1) / 1000,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout_bytes, stderr_bytes = process.communicate()

    stdout, stderr, output_truncated = decode_limited_output(
        stdout_bytes,
        stderr_bytes,
        request.output_limit_bytes,
    )
    if timed_out:
        return success_response(
            stdout=stdout,
            stderr=stderr,
            exit_code=process.returncode,
            timed_out=True,
            output_truncated=output_truncated,
            failure_class="timeout",
        )

    parsed = parse_wrapper_response(stdout)
    if parsed is not None:
        return normalize_wrapper_response(parsed, stderr, output_truncated)

    failure_class = failure_class_for_exit(process.returncode, "unknown")
    return success_response(
        stdout=stdout,
        stderr=stderr,
        exit_code=process.returncode,
        timed_out=False,
        output_truncated=output_truncated,
        failure_class=failure_class,
        denied_reason=None if failure_class != "sandbox_denied" else "Firecracker wrapper denied execution.",
    )


def parse_wrapper_response(stdout: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def normalize_wrapper_response(payload: dict[str, Any], wrapper_stderr: str, wrapper_truncated: bool) -> dict[str, Any]:
    exit_code = optional_int(payload.get("exitCode"))
    failure_class = payload.get("failureClass")
    if not isinstance(failure_class, str) or failure_class not in FAILURE_CLASSES:
        failure_class = failure_class_for_exit(exit_code, "unknown")
    stderr = str(payload.get("stderr", ""))
    if wrapper_stderr:
        stderr = f"{stderr}\n[wrapper stderr]\n{wrapper_stderr}".strip()
    return success_response(
        stdout=str(payload.get("stdout", "")),
        stderr=stderr,
        exit_code=exit_code,
        timed_out=bool(payload.get("timedOut")),
        output_truncated=wrapper_truncated or bool(payload.get("outputTruncated")),
        failure_class=failure_class,
        denied_reason=payload.get("deniedReason") if isinstance(payload.get("deniedReason"), str) else None,
    )


def success_response(
    *,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    timed_out: bool,
    output_truncated: bool,
    failure_class: str,
    denied_reason: str | None = None,
) -> dict[str, Any]:
    response = {
        "stdout": stdout,
        "stderr": stderr,
        "exitCode": exit_code,
        "timedOut": timed_out,
        "outputTruncated": output_truncated,
        "failureClass": failure_class,
    }
    if denied_reason:
        response["deniedReason"] = denied_reason
    return response


def failure_response(message: str, failure_class: str) -> dict[str, Any]:
    return success_response(
        stdout="",
        stderr=message,
        exit_code=None,
        timed_out=False,
        output_truncated=False,
        failure_class=failure_class if failure_class in FAILURE_CLASSES else "unknown",
        denied_reason=message if failure_class in {"sandbox_denied", "policy_denied"} else None,
    )


def failure_class_for_exit(exit_code: int | None, default: str) -> str:
    if exit_code in (None, 0):
        return "none" if exit_code == 0 else default
    return default


def decode_limited_output(stdout: bytes, stderr: bytes, limit: int) -> tuple[str, str, bool]:
    safe_limit = max(limit, 0)
    total_size = len(stdout) + len(stderr)
    output_truncated = total_size > safe_limit
    if output_truncated:
        stdout_limit = min(len(stdout), safe_limit)
        stderr_limit = max(0, safe_limit - stdout_limit)
        stdout = stdout[:stdout_limit]
        stderr = stderr[:stderr_limit]
    return (
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        output_truncated,
    )


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_existing_directory(value: object, *, field_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise LauncherError(f"{field_name} must be a non-empty path.", "invalid_input")
    path = Path(value).resolve()
    if not path.is_dir():
        raise LauncherError(f"{field_name} must be an existing directory.", "invalid_input")
    return path


def _relative_working_directory(value: object) -> str:
    if value is None:
        return "."
    if not isinstance(value, str) or not value.strip():
        raise LauncherError("workingDirectory must be a relative path string.", "invalid_input")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise LauncherError("workingDirectory must stay inside workspace.", "sandbox_denied")
    return "." if value == "." else value.replace("\\", "/")


def _stdin_bytes(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LauncherError("stdinBase64 must be a base64 string or null.", "invalid_input")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as error:
        raise LauncherError("stdinBase64 is not valid base64.", "invalid_input") from error


def _positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, 1)


def _network_policy(value: object) -> str:
    if value in {"deny", "allow"}:
        return str(value)
    raise LauncherError("networkPolicy must be deny or allow.", "invalid_input")


if __name__ == "__main__":
    raise SystemExit(main())
