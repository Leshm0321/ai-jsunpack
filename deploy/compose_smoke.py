from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "docker-compose.yml"
DEVELOPMENT_COMPOSE_FILE = ROOT / "deploy" / "docker-compose.dev.yml"
DEFAULT_COMPOSE_FILES = (COMPOSE_FILE, DEVELOPMENT_COMPOSE_FILE)
DEFAULT_OUTPUT = ROOT / "tmp" / "deployment-compose-smoke" / "compose-smoke.json"
DEFAULT_ARTIFACT_ROOT = ROOT / "tmp" / "deployment-compose-smoke" / "artifacts"
DEFAULT_DEPLOYMENT_SMOKE_OUTPUT = ROOT / "tmp" / "deployment-compose-smoke" / "deployment-smoke.json"
DEFAULT_DATABASE_URL = "postgresql+psycopg://ai_jsunpack:ai_jsunpack@127.0.0.1:5432/ai_jsunpack"


@dataclass(frozen=True)
class ComposeSmokeConfig:
    output_path: Path = DEFAULT_OUTPUT
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT
    deployment_smoke_output: Path = DEFAULT_DEPLOYMENT_SMOKE_OUTPUT
    database_url: str = DEFAULT_DATABASE_URL
    artifact_store_endpoint_url: str = "http://127.0.0.1:9000"
    artifact_store_bucket: str = "ai-jsunpack-artifacts"
    artifact_store_access_key: str = "ai-jsunpack"
    artifact_store_secret_key: str = "replace-with-minio-secret"
    artifact_store_prefix: str = "compose-smoke"
    project_name: str = "ai-jsunpack-smoke"
    compose_files: tuple[Path, ...] = DEFAULT_COMPOSE_FILES
    profiles: tuple[str, ...] = ("worker", "browser-runner")
    health_timeout_seconds: int = 180
    soak_instances: int = 2
    soak_workers_per_instance: int = 1
    soak_runs: int = 10
    skip_build: bool = False
    keep_running: bool = False
    dry_run: bool = False


def parse_args(argv: list[str] | None = None) -> ComposeSmokeConfig:
    parser = argparse.ArgumentParser(description="运行 docker compose 部署冒烟演练。")
    parser.add_argument("--output", dest="output_path", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--deployment-smoke-output", type=Path, default=DEFAULT_DEPLOYMENT_SMOKE_OUTPUT)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--artifact-store-endpoint-url", default="http://127.0.0.1:9000")
    parser.add_argument("--artifact-store-bucket", default="ai-jsunpack-artifacts")
    parser.add_argument("--artifact-store-access-key", default="ai-jsunpack")
    parser.add_argument("--artifact-store-secret-key", default="replace-with-minio-secret")
    parser.add_argument("--artifact-store-prefix", default="compose-smoke")
    parser.add_argument("--project-name", default="ai-jsunpack-smoke")
    parser.add_argument("--compose-file", dest="compose_files", action="append", type=Path, default=None)
    parser.add_argument("--profile", dest="profiles", action="append", default=None)
    parser.add_argument("--health-timeout-seconds", type=int, default=180)
    parser.add_argument("--soak-instances", type=int, default=2)
    parser.add_argument("--soak-workers-per-instance", type=int, default=1)
    parser.add_argument("--soak-runs", type=int, default=10)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    profiles = tuple(args.profiles) if args.profiles is not None else ("worker", "browser-runner")
    return ComposeSmokeConfig(
        output_path=args.output_path,
        artifact_root=args.artifact_root,
        deployment_smoke_output=args.deployment_smoke_output,
        database_url=args.database_url,
        artifact_store_endpoint_url=args.artifact_store_endpoint_url,
        artifact_store_bucket=args.artifact_store_bucket,
        artifact_store_access_key=args.artifact_store_access_key,
        artifact_store_secret_key=args.artifact_store_secret_key,
        artifact_store_prefix=args.artifact_store_prefix,
        project_name=args.project_name,
        compose_files=tuple(args.compose_files) if args.compose_files else DEFAULT_COMPOSE_FILES,
        profiles=profiles,
        health_timeout_seconds=max(1, args.health_timeout_seconds),
        soak_instances=max(1, args.soak_instances),
        soak_workers_per_instance=max(1, args.soak_workers_per_instance),
        soak_runs=max(1, args.soak_runs),
        skip_build=args.skip_build,
        keep_running=args.keep_running,
        dry_run=args.dry_run,
    )


def run_compose_smoke(config: ComposeSmokeConfig) -> dict[str, Any]:
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    commands = compose_command_plan(config)
    report: dict[str, Any] = {
        "kind": "compose_smoke_report",
        "schemaVersion": "1",
        "status": "running",
        "generatedAt": utc_now(),
        "config": safe_config(config),
        "commands": commands,
        "checks": checks,
        "serviceHealth": {},
        "deploymentSmoke": None,
        "logs": {},
    }

    if config.dry_run:
        add_check(checks, "compose_command_plan", True, evidence={"commandCount": len(commands)})
        compose_files_present = all(path.exists() for path in config.compose_files)
        add_check(
            checks,
            "compose_file_present",
            compose_files_present,
            evidence={"paths": [str(path) for path in config.compose_files]},
        )
        finalize_report(report, started)
        write_report(config.output_path, report)
        return report

    try:
        config.output_path.parent.mkdir(parents=True, exist_ok=True)
        config.artifact_root.mkdir(parents=True, exist_ok=True)
        run_compose(["version"], config, checks, "compose_available")
        if not config.skip_build:
            run_compose(["build"], config, checks, "compose_build")
        run_compose(["up", "-d"], config, checks, "compose_up")
        health = wait_for_services(config, checks)
        report["serviceHealth"] = health
        deployment_smoke = run_deployment_smoke(config, checks)
        report["deploymentSmoke"] = deployment_smoke
        if deployment_smoke.get("status") == "pass":
            add_check(checks, "deployment_smoke_archive_ready", True, evidence=deployment_smoke.get("archive_manifest", {}))
        else:
            add_check(
                checks,
                "deployment_smoke_archive_ready",
                False,
                evidence={"status": deployment_smoke.get("status"), "failedChecks": deployment_smoke.get("failedChecks")},
            )
    finally:
        report["logs"] = collect_logs(config)
        if not config.keep_running:
            run_compose(["down"], config, checks, "compose_down", check=False)

    finalize_report(report, started)
    write_report(config.output_path, report)
    return report


def compose_command_plan(config: ComposeSmokeConfig) -> list[list[str]]:
    commands: list[list[str]] = []
    base = compose_base_command(config)
    commands.append([*base, "version"])
    if not config.skip_build:
        commands.append([*base, "build"])
    commands.append([*base, "up", "-d"])
    commands.append([*base, "ps", "--format", "json"])
    commands.append(deployment_smoke_command(config))
    if not config.keep_running:
        commands.append([*base, "down"])
    return commands


def compose_base_command(config: ComposeSmokeConfig) -> list[str]:
    command = ["docker", "compose", "-p", config.project_name]
    for compose_file in config.compose_files:
        command.extend(["-f", str(compose_file)])
    for profile in config.profiles:
        command.extend(["--profile", profile])
    return command


def deployment_smoke_command(config: ComposeSmokeConfig) -> list[str]:
    return [
        sys.executable,
        "-m",
        "apps.api.app.deployment_smoke",
        "--database-url",
        config.database_url,
        "--artifact-root",
        str(config.artifact_root),
        "--soak-instances",
        str(config.soak_instances),
        "--soak-workers-per-instance",
        str(config.soak_workers_per_instance),
        "--soak-runs",
        str(config.soak_runs),
        "--output",
        str(config.deployment_smoke_output),
    ]


def run_compose(
    args: list[str],
    config: ComposeSmokeConfig,
    checks: list[dict[str, Any]],
    check_name: str,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [*compose_base_command(config), *args]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    passed = result.returncode == 0
    add_check(
        checks,
        check_name,
        passed if check else True,
        evidence={
            "command": command,
            "returnCode": result.returncode,
            "stdout": tail_text(result.stdout),
            "stderr": tail_text(result.stderr),
        },
    )
    if check and not passed:
        raise RuntimeError(f"{check_name} 失败，退出码为 {result.returncode}")
    return result


def wait_for_services(config: ComposeSmokeConfig, checks: list[dict[str, Any]]) -> dict[str, Any]:
    deadline = time.monotonic() + config.health_timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() <= deadline:
        latest = compose_ps(config)
        unhealthy = [
            service
            for service in latest.get("services", [])
            if service.get("expectedHealthy") and service.get("health") not in {"healthy", "none"}
        ]
        if not unhealthy:
            add_check(checks, "compose_services_healthy", True, evidence=latest)
            return latest
        time.sleep(2)
    add_check(checks, "compose_services_healthy", False, evidence=latest)
    raise TimeoutError("等待 compose 服务进入健康状态时超时。")


def compose_ps(config: ComposeSmokeConfig) -> dict[str, Any]:
    result = subprocess.run(
        [*compose_base_command(config), "ps", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    services: list[dict[str, Any]] = []
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = str(item.get("Service") or item.get("Name") or "")
            health = str(item.get("Health") or item.get("State") or "none")
            services.append(
                {
                    "name": name,
                    "state": item.get("State"),
                    "health": health,
                    "expectedHealthy": name in {"db", "artifact-store", "api", "browser-runner", "web"},
                }
            )
    return {
        "returnCode": result.returncode,
        "stdout": tail_text(result.stdout),
        "stderr": tail_text(result.stderr),
        "services": services,
    }


def run_deployment_smoke(config: ComposeSmokeConfig, checks: list[dict[str, Any]]) -> dict[str, Any]:
    command = deployment_smoke_command(config)
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=deployment_smoke_environment(config),
    )
    evidence = {
        "command": command,
        "returnCode": result.returncode,
        "stdout": tail_text(result.stdout),
        "stderr": tail_text(result.stderr),
        "outputPath": str(config.deployment_smoke_output),
    }
    if config.deployment_smoke_output.exists():
        try:
            payload = json.loads(config.deployment_smoke_output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {"status": "fail", "failedChecks": ["invalid_deployment_smoke_json"]}
    else:
        payload = {"status": "fail", "failedChecks": ["missing_deployment_smoke_report"]}
    add_check(checks, "deployment_smoke_command", result.returncode == 0 and payload.get("status") == "pass", evidence=evidence)
    return payload


def deployment_smoke_environment(config: ComposeSmokeConfig) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "AI_JSUNPACK_ARTIFACT_STORE": "minio",
            "AI_JSUNPACK_ARTIFACT_S3_ENDPOINT_URL": config.artifact_store_endpoint_url,
            "AI_JSUNPACK_ARTIFACT_S3_BUCKET": config.artifact_store_bucket,
            "AI_JSUNPACK_ARTIFACT_S3_ACCESS_KEY_ID": config.artifact_store_access_key,
            "AI_JSUNPACK_ARTIFACT_S3_SECRET_ACCESS_KEY": config.artifact_store_secret_key,
            "AI_JSUNPACK_ARTIFACT_S3_REGION": "us-east-1",
            "AI_JSUNPACK_ARTIFACT_S3_ADDRESSING_STYLE": "path",
            "AI_JSUNPACK_ARTIFACT_S3_PREFIX": config.artifact_store_prefix,
        }
    )
    return env


def collect_logs(config: ComposeSmokeConfig) -> dict[str, str]:
    logs: dict[str, str] = {}
    for service in ("db", "artifact-store", "artifact-store-init", "api", "worker", "browser-runner", "web"):
        result = subprocess.run(
            [*compose_base_command(config), "logs", "--tail", "80", service],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        logs[service] = tail_text(result.stdout + result.stderr, limit=12000)
    return logs


def add_check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    *,
    evidence: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    checks.append(
        {
            "name": name,
            "status": "pass" if passed else "fail",
            "evidence": evidence or {},
            "error": error,
        }
    )


def finalize_report(report: dict[str, Any], started: float) -> None:
    failed = [check for check in report["checks"] if check["status"] != "pass"]
    report["status"] = "pass" if not failed else "fail"
    report["failedChecks"] = [check["name"] for check in failed]
    report["durationMs"] = int((time.perf_counter() - started) * 1000)
    report["generatedAt"] = utc_now()


def safe_config(config: ComposeSmokeConfig) -> dict[str, Any]:
    payload = asdict(config)
    for key, value in list(payload.items()):
        if isinstance(value, Path):
            payload[key] = str(value)
        elif isinstance(value, tuple):
            payload[key] = [str(item) if isinstance(item, Path) else item for item in value]
    payload["database_url"] = redact_database_url(config.database_url)
    payload["artifact_store_secret_key"] = "<redacted>"
    return payload


def redact_database_url(value: str) -> str:
    if "@" not in value or "://" not in value:
        return value
    scheme, rest = value.split("://", 1)
    return f"{scheme}://<redacted>@{rest.split('@', 1)[1]}"


def tail_text(value: str, *, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    try:
        report = run_compose_smoke(config)
    except Exception as error:
        report = {
            "kind": "compose_smoke_report",
            "schemaVersion": "1",
            "status": "fail",
            "generatedAt": utc_now(),
            "config": safe_config(config),
            "commands": compose_command_plan(config),
            "checks": [
                {
                    "name": "compose_smoke_exception",
                    "status": "fail",
                    "evidence": {},
                    "error": str(error),
                }
            ],
            "failedChecks": ["compose_smoke_exception"],
            "durationMs": 0,
        }
        write_report(config.output_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
