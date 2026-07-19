from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "tmp" / "release-gate" / "release-readiness.json"
DEFAULT_WORKFLOW = ROOT / ".github" / "workflows" / "release-gate.yml"
CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]

REQUIRED_PRODUCTION_SECRETS = (
    "AI_JSUNPACK_AUTH_SECRET",
    "AI_JSUNPACK_ARTIFACT_S3_SECRET_ACCESS_KEY",
    "AI_JSUNPACK_BROWSER_RUNNER_TOKEN",
)
OPTIONAL_PRODUCTION_SECRETS = (
    "AI_JSUNPACK_ALERT_WEBHOOK_URL",
    "model provider credentials",
)


@dataclass(frozen=True)
class ReleaseReadinessConfig:
    repository: str = ""
    secret_environment: str = "production"
    workflow_path: Path = DEFAULT_WORKFLOW
    output_path: Path = DEFAULT_OUTPUT
    require_docker: bool = True


def parse_args(argv: list[str] | None = None) -> ReleaseReadinessConfig:
    parser = argparse.ArgumentParser(description="检查本地环境是否已准备好执行真实的生产发布归档。")
    parser.add_argument("--repository", default="", help="owner/name 格式的 GitHub 仓库；默认从 git remote 获取。")
    parser.add_argument("--secret-environment", default="production")
    parser.add_argument("--workflow-path", type=Path, default=DEFAULT_WORKFLOW)
    parser.add_argument("--output", dest="output_path", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-docker", dest="require_docker", action="store_false")
    return ReleaseReadinessConfig(**vars(parser.parse_args(argv)))


def run_release_readiness(
    config: ReleaseReadinessConfig,
    *,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    run_command = command_runner or default_command_runner
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    workflow_present = config.workflow_path.exists()
    add_check(
        checks,
        "workflow_file_present",
        workflow_present,
        evidence={"path": str(config.workflow_path)},
    )
    if not workflow_present:
        blockers.append("workflow_file_present")

    repository = config.repository or discover_repository(run_command)
    repository_valid = is_repository_slug(repository)
    add_check(checks, "github_repository_known", repository_valid, evidence={"repository": repository or ""})
    if not repository_valid:
        blockers.append("github_repository_known")

    gh_check = run_tool_check(run_command, ["gh", "--version"])
    add_check(checks, "github_cli_available", gh_check["passed"], evidence=gh_check)
    if not gh_check["passed"]:
        blockers.append("github_cli_available")

    auth_check = run_tool_check(run_command, ["gh", "auth", "status"])
    add_check(checks, "github_cli_authenticated", auth_check["passed"], evidence=auth_check)
    if not auth_check["passed"]:
        blockers.append("github_cli_authenticated")

    if repository_valid and auth_check["passed"]:
        workflow_check = run_tool_check(run_command, ["gh", "workflow", "list", "--repo", repository])
        workflow_stdout = workflow_check["stdout"].lower()
        workflow_visible = workflow_check["passed"] and ("release-gate" in workflow_stdout or "release gate" in workflow_stdout)
        add_check(
            checks,
            "release_gate_workflow_visible",
            workflow_visible,
            evidence={**workflow_check, "repository": repository},
        )
        if not workflow_visible:
            blockers.append("release_gate_workflow_visible")
    else:
        add_check(
            checks,
            "release_gate_workflow_visible",
            False,
            evidence={"reason": "需要 github_repository_known 和 github_cli_authenticated"},
        )
        blockers.append("release_gate_workflow_visible")

    if config.require_docker:
        docker_check = run_tool_check(run_command, ["docker", "info", "--format", "{{json .ServerVersion}}"])
        add_check(checks, "docker_daemon_available", docker_check["passed"], evidence=docker_check)
        if not docker_check["passed"]:
            blockers.append("docker_daemon_available")
    else:
        add_check(checks, "docker_daemon_available", True, evidence={"skipped": True, "reason": "--skip-docker"})
        warnings.append("配置要求跳过 docker_daemon_available")

    report: dict[str, Any] = {
        "kind": "production_release_readiness_report",
        "schemaVersion": "1",
        "status": "running",
        "generatedAt": utc_now(),
        "durationMs": 0,
        "config": safe_config(config),
        "repository": repository,
        "secretEnvironment": config.secret_environment,
        "requiredSecrets": required_secret_summary(config.secret_environment),
        "checks": checks,
        "failedChecks": [],
        "blockers": sorted(set(blockers)),
        "warnings": warnings,
        "nextActions": next_actions(blockers),
    }
    finalize_report(report, started)
    write_json(config.output_path, report)
    return report


def default_command_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)


def discover_repository(command_runner: CommandRunner) -> str:
    for command in (["git", "config", "--get", "remote.origin.url"], ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]):
        result = command_runner(command)
        if result.returncode == 0:
            repository = parse_repository(result.stdout.strip())
            if repository:
                return repository
    return ""


def parse_repository(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    if cleaned.startswith("git@github.com:"):
        cleaned = cleaned.removeprefix("git@github.com:").removesuffix(".git")
    elif "github.com/" in cleaned:
        cleaned = cleaned.split("github.com/", 1)[1].removesuffix(".git")
    return cleaned if is_repository_slug(cleaned) else ""


def is_repository_slug(value: str) -> bool:
    parts = value.strip().split("/")
    return len(parts) == 2 and all(part and " " not in part and ":" not in part for part in parts)


def run_tool_check(command_runner: CommandRunner, command: list[str]) -> dict[str, Any]:
    try:
        result = command_runner(command)
    except FileNotFoundError as error:
        return {
            "command": command,
            "passed": False,
            "returnCode": None,
            "stdout": "",
            "stderr": str(error),
        }
    return {
        "command": command,
        "passed": result.returncode == 0,
        "returnCode": result.returncode,
        "stdout": tail_text(result.stdout),
        "stderr": tail_text(result.stderr),
    }


def required_secret_summary(environment: str) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "required": True,
            "githubEnvironment": environment,
            "containsSecretValues": False,
        }
        for name in REQUIRED_PRODUCTION_SECRETS
    ] + [
        {
            "name": name,
            "required": False,
            "githubEnvironment": environment,
            "containsSecretValues": False,
        }
        for name in OPTIONAL_PRODUCTION_SECRETS
    ]


def next_actions(blockers: list[str]) -> list[str]:
    actions = []
    blocker_set = set(blockers)
    if "github_repository_known" in blocker_set:
        actions.append("配置 git remote origin，或传入 --repository <owner/repo>。")
    if "github_cli_authenticated" in blocker_set:
        actions.append("触发真实工作流前，请完成 GitHub CLI 身份验证或提供 GH_TOKEN。")
    if "docker_daemon_available" in blocker_set:
        actions.append("在 release runner 上启动 Docker daemon，以便执行 image build/push。")
    if "release_gate_workflow_visible" in blocker_set:
        actions.append("确认目标 GitHub 仓库中存在 .github/workflows/release-gate.yml。")
    if "workflow_file_present" in blocker_set:
        actions.append("发布前恢复 .github/workflows/release-gate.yml。")
    if not actions:
        actions.append("使用 secret_environment=production 和 push_images=true 触发 release-gate workflow。")
    return actions


def add_check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    *,
    evidence: dict[str, Any] | None = None,
) -> None:
    checks.append(
        {
            "name": name,
            "status": "pass" if passed else "fail",
            "evidence": redact_environment_values(evidence or {}),
        }
    )


def redact_environment_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_environment_values(child) for key, child in value.items()}
    if isinstance(value, list):
        return [redact_environment_values(item) for item in value]
    if isinstance(value, str):
        token = os.getenv("GH_TOKEN")
        if token:
            return value.replace(token, "<redacted>")
    return value


def finalize_report(report: dict[str, Any], started: float) -> None:
    failed = [check for check in report["checks"] if check["status"] != "pass"]
    report["status"] = "ready" if not report["blockers"] and not failed else "blocked"
    report["failedChecks"] = [check["name"] for check in failed]
    report["durationMs"] = int((time.perf_counter() - started) * 1000)
    report["generatedAt"] = utc_now()


def safe_config(config: ReleaseReadinessConfig) -> dict[str, str | bool]:
    payload = asdict(config)
    return {key: str(value) if isinstance(value, Path) else value for key, value in payload.items()}


def tail_text(value: str, *, limit: int = 1200) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    report = run_release_readiness(config)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("status") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
