from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "tmp" / "release-gate" / "release-gate.json"
DEFAULT_SBOM_DIR = ROOT / "tmp" / "release-gate" / "sbom"
DEFAULT_COMPOSE_SMOKE_OUTPUT = ROOT / "tmp" / "release-gate" / "compose-smoke.json"
DEFAULT_COMPOSE_ARTIFACT_ROOT = ROOT / "tmp" / "release-gate" / "artifacts"
DEFAULT_DEPLOYMENT_SMOKE_OUTPUT = ROOT / "tmp" / "release-gate" / "deployment-smoke.json"

SERVICE_IMAGES: tuple[dict[str, str], ...] = (
    {
        "service": "api",
        "composeEnvVar": "AI_JSUNPACK_API_IMAGE",
        "dockerfile": "deploy/docker/api.Dockerfile",
    },
    {
        "service": "worker",
        "composeEnvVar": "AI_JSUNPACK_WORKER_IMAGE",
        "dockerfile": "deploy/docker/worker.Dockerfile",
    },
    {
        "service": "browser-runner",
        "composeEnvVar": "AI_JSUNPACK_BROWSER_RUNNER_IMAGE",
        "dockerfile": "deploy/docker/browser-runner.Dockerfile",
    },
    {
        "service": "web",
        "composeEnvVar": "AI_JSUNPACK_WEB_IMAGE",
        "dockerfile": "deploy/docker/web.Dockerfile",
    },
)

DISABLED_TOOL_NAMES = {"", "none", "disabled", "skip"}
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class ReleaseGateConfig:
    registry: str
    repository_prefix: str
    version: str
    git_sha: str = ""
    previous_version: str = ""
    output_path: Path = DEFAULT_OUTPUT
    sbom_output_dir: Path = DEFAULT_SBOM_DIR
    compose_smoke_output: Path = DEFAULT_COMPOSE_SMOKE_OUTPUT
    compose_artifact_root: Path = DEFAULT_COMPOSE_ARTIFACT_ROOT
    deployment_smoke_output: Path = DEFAULT_DEPLOYMENT_SMOKE_OUTPUT
    project_name: str = "ai-jsunpack-release-gate"
    sbom_tool: str = "syft"
    scan_tool: str = "trivy"
    scan_severity: str = "HIGH,CRITICAL"
    soak_instances: int = 2
    soak_workers_per_instance: int = 1
    soak_runs: int = 10
    execute: bool = False
    push: bool = False


def parse_args(argv: list[str] | None = None) -> ReleaseGateConfig:
    parser = argparse.ArgumentParser(description="Plan or execute an auditable image release gate.")
    parser.add_argument("--registry", required=True, help="Container registry host, for example registry.example.com.")
    parser.add_argument(
        "--repository-prefix",
        required=True,
        help="Repository namespace under the registry, for example ai-jsunpack.",
    )
    parser.add_argument("--version", required=True, help="Immutable release version or tag.")
    parser.add_argument("--git-sha", default="", help="Commit SHA to pin as an auxiliary image tag.")
    parser.add_argument("--previous-version", default="", help="Previous known-good image version for rollback evidence.")
    parser.add_argument("--output", dest="output_path", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sbom-output-dir", type=Path, default=DEFAULT_SBOM_DIR)
    parser.add_argument("--compose-smoke-output", type=Path, default=DEFAULT_COMPOSE_SMOKE_OUTPUT)
    parser.add_argument("--compose-artifact-root", type=Path, default=DEFAULT_COMPOSE_ARTIFACT_ROOT)
    parser.add_argument("--deployment-smoke-output", type=Path, default=DEFAULT_DEPLOYMENT_SMOKE_OUTPUT)
    parser.add_argument("--project-name", default="ai-jsunpack-release-gate")
    parser.add_argument("--sbom-tool", default="syft", help="SBOM tool command, or 'none' to skip.")
    parser.add_argument("--scan-tool", default="trivy", help="Image scanner command, or 'none' to skip.")
    parser.add_argument("--scan-severity", default="HIGH,CRITICAL")
    parser.add_argument("--soak-instances", type=int, default=2)
    parser.add_argument("--soak-workers-per-instance", type=int, default=1)
    parser.add_argument("--soak-runs", type=int, default=10)
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--dry-run", dest="execute", action="store_false", default=False)
    execution.add_argument("--execute", dest="execute", action="store_true")
    parser.add_argument("--push", action="store_true", help="Push image tags after successful local build.")
    args = parser.parse_args(argv)
    git_sha = args.git_sha or discover_git_sha()
    config = ReleaseGateConfig(
        registry=args.registry,
        repository_prefix=args.repository_prefix,
        version=args.version,
        git_sha=git_sha,
        previous_version=args.previous_version,
        output_path=args.output_path,
        sbom_output_dir=args.sbom_output_dir,
        compose_smoke_output=args.compose_smoke_output,
        compose_artifact_root=args.compose_artifact_root,
        deployment_smoke_output=args.deployment_smoke_output,
        project_name=args.project_name,
        sbom_tool=args.sbom_tool,
        scan_tool=args.scan_tool,
        scan_severity=args.scan_severity,
        soak_instances=max(1, args.soak_instances),
        soak_workers_per_instance=max(1, args.soak_workers_per_instance),
        soak_runs=max(1, args.soak_runs),
        execute=args.execute,
        push=args.push,
    )
    validate_config(config)
    return config


def discover_git_sha() -> str:
    for name in ("GITHUB_SHA", "CI_COMMIT_SHA", "BUILD_SOURCEVERSION"):
        value = os.getenv(name)
        if value:
            return value
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "unknown"


def validate_config(config: ReleaseGateConfig) -> None:
    if not VERSION_PATTERN.match(config.version):
        raise ValueError("--version must be a Docker-tag-compatible value.")
    if config.previous_version and not VERSION_PATTERN.match(config.previous_version):
        raise ValueError("--previous-version must be a Docker-tag-compatible value.")
    if not config.registry.strip("/"):
        raise ValueError("--registry cannot be empty.")
    if not config.repository_prefix.strip("/"):
        raise ValueError("--repository-prefix cannot be empty.")


def run_release_gate(config: ReleaseGateConfig) -> dict[str, Any]:
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    images = image_matrix(config)
    plan = command_plan(config, images)
    report: dict[str, Any] = {
        "kind": "release_gate_report",
        "schemaVersion": "1",
        "status": "running",
        "mode": "execute" if config.execute else "dry_run",
        "generatedAt": utc_now(),
        "config": safe_config(config),
        "images": images,
        "requiredSecrets": required_secret_template(),
        "commandPlan": plan,
        "releaseGates": release_gate_summary(config),
        "rollback": rollback_summary(config, images),
        "checks": checks,
    }

    try:
        add_check(checks, "release_parameters", True, evidence={"version": config.version, "gitSha": short_sha(config.git_sha)})
        dockerfiles_present = all((ROOT / image["dockerfile"]).exists() for image in images)
        add_check(
            checks,
            "dockerfiles_present",
            dockerfiles_present,
            evidence={"dockerfiles": [image["dockerfile"] for image in images]},
        )
        add_check(
            checks,
            "compose_image_overrides",
            True,
            evidence=plan["composeSmokeGate"]["environment"],
        )
        if not dockerfiles_present:
            raise FileNotFoundError("One or more service Dockerfiles are missing.")

        if config.execute:
            config.output_path.parent.mkdir(parents=True, exist_ok=True)
            config.sbom_output_dir.mkdir(parents=True, exist_ok=True)
            run_commands("image_build", plan["build"], checks)
            run_commands("sbom_generation", plan["sbom"], checks, allow_empty=True)
            run_commands("vulnerability_scan", plan["scan"], checks, allow_empty=True)
            if config.push:
                run_commands("image_push", plan["push"], checks)
            else:
                add_check(checks, "image_push_skipped", True, evidence={"reason": "--push was not set"})
            run_commands(
                "compose_smoke_gate",
                [plan["composeSmokeGate"]["command"]],
                checks,
                env=plan["composeSmokeGate"]["environment"],
            )
        else:
            add_check(
                checks,
                "dry_run_command_plan",
                True,
                evidence={
                    "buildCommands": len(plan["build"]),
                    "sbomCommands": len(plan["sbom"]),
                    "scanCommands": len(plan["scan"]),
                    "pushCommands": len(plan["push"]),
                },
            )
    except Exception as error:
        add_check(checks, "release_gate_exception", False, error=str(error))

    finalize_report(report, started)
    write_report(config.output_path, report)
    return report


def image_matrix(config: ReleaseGateConfig) -> list[dict[str, Any]]:
    registry = config.registry.strip("/")
    prefix = config.repository_prefix.strip("/")
    sha_tag = short_sha(config.git_sha)
    images: list[dict[str, Any]] = []
    for service in SERVICE_IMAGES:
        repository = f"{registry}/{prefix}/{service['service']}"
        tags = [f"{repository}:{config.version}"]
        if sha_tag and sha_tag not in {config.version, "unknown"}:
            tags.append(f"{repository}:{sha_tag}")
        rollback_tag = (
            f"{repository}:{config.previous_version}"
            if config.previous_version
            else f"{repository}:<previous-known-good-tag>"
        )
        images.append(
            {
                **service,
                "repository": repository,
                "versionTag": tags[0],
                "gitShaTag": tags[1] if len(tags) > 1 else None,
                "tags": tags,
                "rollbackTag": rollback_tag,
            }
        )
    return images


def command_plan(config: ReleaseGateConfig, images: list[dict[str, Any]]) -> dict[str, Any]:
    build_commands = [build_command(image) for image in images]
    sbom_commands = [sbom_command(config, image) for image in images if tool_enabled(config.sbom_tool)]
    scan_commands = [scan_command(config, image) for image in images if tool_enabled(config.scan_tool)]
    push_commands = [push_command(tag) for image in images for tag in image["tags"]]
    compose_environment = {image["composeEnvVar"]: image["versionTag"] for image in images}
    compose_command = [
        sys.executable,
        "-m",
        "deploy.compose_smoke",
        "--skip-build",
        "--project-name",
        config.project_name,
        "--output",
        str(config.compose_smoke_output),
        "--artifact-root",
        str(config.compose_artifact_root),
        "--deployment-smoke-output",
        str(config.deployment_smoke_output),
        "--artifact-store-prefix",
        f"release-gate-{config.version}",
        "--soak-instances",
        str(config.soak_instances),
        "--soak-workers-per-instance",
        str(config.soak_workers_per_instance),
        "--soak-runs",
        str(config.soak_runs),
    ]
    return {
        "build": build_commands,
        "sbom": sbom_commands,
        "scan": scan_commands,
        "push": push_commands,
        "composeSmokeGate": {
            "command": compose_command,
            "environment": compose_environment,
            "expected": {
                "composeStatus": "pass",
                "deploymentSmokeStatus": "pass",
                "archiveReady": True,
            },
        },
        "postReleaseChecks": [
            "GET /health for API and Browser Runner",
            "GET /ops/metrics with ops-read Bearer token",
            "GET /ops/prometheus with ops-read Bearer token",
            "GET /ops/alert-events after smoke gate",
        ],
    }


def build_command(image: dict[str, Any]) -> list[str]:
    command = ["docker", "build", "-f", image["dockerfile"]]
    for tag in image["tags"]:
        command.extend(["-t", tag])
    command.append(".")
    return command


def sbom_command(config: ReleaseGateConfig, image: dict[str, Any]) -> list[str]:
    output_path = config.sbom_output_dir / f"{image['service']}-{config.version}.spdx.json"
    return [config.sbom_tool, image["versionTag"], "-o", f"spdx-json={output_path}"]


def scan_command(config: ReleaseGateConfig, image: dict[str, Any]) -> list[str]:
    tool = config.scan_tool.lower()
    if Path(config.scan_tool).name.lower() == "trivy" or tool == "trivy":
        return [
            config.scan_tool,
            "image",
            "--exit-code",
            "1",
            "--severity",
            config.scan_severity,
            image["versionTag"],
        ]
    if Path(config.scan_tool).name.lower() == "grype" or tool == "grype":
        return [config.scan_tool, image["versionTag"], "--fail-on", "high"]
    return [config.scan_tool, image["versionTag"]]


def push_command(tag: str) -> list[str]:
    return ["docker", "push", tag]


def tool_enabled(value: str) -> bool:
    return value.strip().lower() not in DISABLED_TOOL_NAMES


def required_secret_template() -> list[dict[str, str]]:
    return [
        {
            "name": "AI_JSUNPACK_AUTH_SECRET",
            "scope": "api,worker,browser-runner",
            "injection": "secret manager or sealed CI variable",
        },
        {
            "name": "AI_JSUNPACK_ARTIFACT_S3_SECRET_ACCESS_KEY",
            "scope": "api,worker,browser-runner",
            "injection": "object-store credential scoped to the artifact bucket",
        },
        {
            "name": "AI_JSUNPACK_BROWSER_RUNNER_TOKEN",
            "scope": "worker",
            "injection": "HMAC Bearer token with serviceRoles=[\"worker\"]",
        },
        {
            "name": "VITE_API_AUTH_TOKEN",
            "scope": "web",
            "injection": "runtime/session token, not baked as a long-lived production secret",
        },
        {
            "name": "AI_JSUNPACK_ALERT_WEBHOOK_URL",
            "scope": "api",
            "injection": "optional ops webhook endpoint",
        },
        {
            "name": "model provider credentials",
            "scope": "worker",
            "injection": "only when cloud_allowed mode is enabled for the deployment",
        },
    ]


def release_gate_summary(config: ReleaseGateConfig) -> list[dict[str, Any]]:
    return [
        {
            "name": "immutable_image_tags",
            "required": True,
            "evidence": ["images[].versionTag", "images[].gitShaTag"],
        },
        {
            "name": "sbom_generation",
            "required": tool_enabled(config.sbom_tool),
            "evidence": ["commandPlan.sbom", str(config.sbom_output_dir)],
        },
        {
            "name": "vulnerability_scan",
            "required": tool_enabled(config.scan_tool),
            "evidence": ["commandPlan.scan"],
        },
        {
            "name": "post_release_smoke",
            "required": True,
            "evidence": [
                str(config.compose_smoke_output),
                str(config.deployment_smoke_output),
                "deploymentSmoke.archive_manifest.archiveReady=true",
            ],
        },
    ]


def rollback_summary(config: ReleaseGateConfig, images: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "previousVersion": config.previous_version or "<previous-known-good-tag>",
        "imageTagMapping": [
            {
                "service": image["service"],
                "current": image["versionTag"],
                "rollback": image["rollbackTag"],
                "composeEnvVar": image["composeEnvVar"],
            }
            for image in images
        ],
        "requiredEvidence": [
            str(config.output_path),
            str(config.compose_smoke_output),
            str(config.deployment_smoke_output),
            "retained PostgreSQL export or volume snapshot",
            "retained Artifact Store bucket export or prefix snapshot",
            "compose logs captured before rollback",
        ],
        "procedure": [
            "Preserve release gate, compose smoke, deployment smoke, DB, Artifact Store, and service logs.",
            "Set compose image environment variables back to rollback tags.",
            "Re-run deploy.compose_smoke with --skip-build and compare archive_manifest retained evidence.",
        ],
    }


def run_commands(
    check_prefix: str,
    commands: list[list[str]],
    checks: list[dict[str, Any]],
    *,
    env: dict[str, str] | None = None,
    allow_empty: bool = False,
) -> None:
    if not commands and allow_empty:
        add_check(checks, f"{check_prefix}_skipped", True, evidence={"reason": "tool disabled"})
        return
    for index, command in enumerate(commands, start=1):
        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False, env=merged_env)
        passed = result.returncode == 0
        add_check(
            checks,
            f"{check_prefix}_{index}",
            passed,
            evidence={
                "command": command,
                "environment": env or {},
                "returnCode": result.returncode,
                "stdout": tail_text(result.stdout),
                "stderr": tail_text(result.stderr),
            },
        )
        if not passed:
            raise RuntimeError(f"{check_prefix}_{index} failed with exit code {result.returncode}")


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


def safe_config(config: ReleaseGateConfig) -> dict[str, Any]:
    payload = asdict(config)
    for key, value in list(payload.items()):
        if isinstance(value, Path):
            payload[key] = str(value)
    payload["git_sha"] = short_sha(config.git_sha)
    return payload


def short_sha(value: str) -> str:
    cleaned = "".join(character for character in value.strip() if character.isalnum())
    return cleaned[:12] or "unknown"


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
    report = run_release_gate(config)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
