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
DEFAULT_SCAN_DIR = ROOT / "tmp" / "release-gate" / "scans"
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
    ci_platform: str = "generic"
    output_path: Path = DEFAULT_OUTPUT
    sbom_output_dir: Path = DEFAULT_SBOM_DIR
    scan_output_dir: Path = DEFAULT_SCAN_DIR
    compose_smoke_output: Path = DEFAULT_COMPOSE_SMOKE_OUTPUT
    compose_artifact_root: Path = DEFAULT_COMPOSE_ARTIFACT_ROOT
    deployment_smoke_output: Path = DEFAULT_DEPLOYMENT_SMOKE_OUTPUT
    project_name: str = "ai-jsunpack-release-gate"
    secret_environment: str = ""
    sbom_tool: str = "syft"
    scan_tool: str = "trivy"
    scan_severity: str = "HIGH,CRITICAL"
    soak_instances: int = 2
    soak_workers_per_instance: int = 1
    soak_runs: int = 10
    execute: bool = False
    push: bool = False


def parse_args(argv: list[str] | None = None) -> ReleaseGateConfig:
    parser = argparse.ArgumentParser(description="规划或执行可审计的镜像发布门禁。")
    parser.add_argument("--registry", required=True, help="容器注册表主机，例如 registry.example.com。")
    parser.add_argument(
        "--repository-prefix",
        required=True,
        help="注册表中的仓库命名空间，例如 ai-jsunpack。",
    )
    parser.add_argument("--version", required=True, help="不可变的发布版本或标签。")
    parser.add_argument("--git-sha", default="", help="作为辅助镜像标签固定的提交 SHA。")
    parser.add_argument("--previous-version", default="", help="用于回滚证据的上一已知良好镜像版本。")
    parser.add_argument(
        "--ci-platform",
        choices=("generic", "github_actions"),
        default="generic",
        help="要写入发布报告的 CI 平台元数据。",
    )
    parser.add_argument("--output", dest="output_path", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sbom-output-dir", type=Path, default=DEFAULT_SBOM_DIR)
    parser.add_argument("--scan-output-dir", type=Path, default=DEFAULT_SCAN_DIR)
    parser.add_argument("--compose-smoke-output", type=Path, default=DEFAULT_COMPOSE_SMOKE_OUTPUT)
    parser.add_argument("--compose-artifact-root", type=Path, default=DEFAULT_COMPOSE_ARTIFACT_ROOT)
    parser.add_argument("--deployment-smoke-output", type=Path, default=DEFAULT_DEPLOYMENT_SMOKE_OUTPUT)
    parser.add_argument("--project-name", default="ai-jsunpack-release-gate")
    parser.add_argument(
        "--secret-environment",
        default="",
        help="secret manager environment 名称，例如 GitHub Environment。",
    )
    parser.add_argument("--sbom-tool", default="syft", help="SBOM 工具命令；使用 'none' 可跳过。")
    parser.add_argument("--scan-tool", default="trivy", help="镜像扫描器命令；使用 'none' 可跳过。")
    parser.add_argument("--scan-severity", default="HIGH,CRITICAL")
    parser.add_argument("--soak-instances", type=int, default=2)
    parser.add_argument("--soak-workers-per-instance", type=int, default=1)
    parser.add_argument("--soak-runs", type=int, default=10)
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--dry-run", dest="execute", action="store_false", default=False)
    execution.add_argument("--execute", dest="execute", action="store_true")
    parser.add_argument("--push", action="store_true", help="本地构建成功后推送镜像标签。")
    args = parser.parse_args(argv)
    git_sha = args.git_sha or discover_git_sha()
    config = ReleaseGateConfig(
        registry=args.registry,
        repository_prefix=args.repository_prefix,
        version=args.version,
        git_sha=git_sha,
        previous_version=args.previous_version,
        ci_platform=args.ci_platform,
        output_path=args.output_path,
        sbom_output_dir=args.sbom_output_dir,
        scan_output_dir=args.scan_output_dir,
        compose_smoke_output=args.compose_smoke_output,
        compose_artifact_root=args.compose_artifact_root,
        deployment_smoke_output=args.deployment_smoke_output,
        project_name=args.project_name,
        secret_environment=args.secret_environment,
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
        raise ValueError("--version 必须是与 Docker 标签兼容的值。")
    if config.previous_version and not VERSION_PATTERN.match(config.previous_version):
        raise ValueError("--previous-version 必须是与 Docker 标签兼容的值。")
    if not config.registry.strip("/"):
        raise ValueError("--registry 不能为空。")
    if not config.repository_prefix.strip("/"):
        raise ValueError("--repository-prefix 不能为空。")


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
        "ciPlatform": ci_platform_summary(config),
        "requiredSecrets": required_secret_template(config),
        "commandPlan": plan,
        "releaseGates": release_gate_summary(config),
        "archivePlan": archive_plan(config, images),
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
        add_check(
            checks,
            "production_archive_plan",
            True,
            evidence={
                "productionArchiveItems": len(report["archivePlan"]["productionArchiveChecklist"]),
                "registryDigestItems": len(report["archivePlan"]["registryDigestEvidence"]),
                "secretManager": report["archivePlan"]["secretManagerEvidence"]["provider"],
            },
        )
        if not dockerfiles_present:
            raise FileNotFoundError("缺少一个或多个服务 Dockerfile。")

        if config.execute:
            config.output_path.parent.mkdir(parents=True, exist_ok=True)
            config.sbom_output_dir.mkdir(parents=True, exist_ok=True)
            config.scan_output_dir.mkdir(parents=True, exist_ok=True)
            run_commands("image_build", plan["build"], checks)
            run_commands("sbom_generation", plan["sbom"], checks, allow_empty=True)
            run_commands("vulnerability_scan", plan["scan"], checks, allow_empty=True)
            if config.push:
                run_commands("image_push", plan["push"], checks)
            else:
                add_check(checks, "image_push_skipped", True, evidence={"reason": "未设置 --push"})
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
            "对 API 和 Browser Runner 执行 GET /health",
            "使用 ops-read Bearer token 执行 GET /ops/metrics",
            "使用 ops-read Bearer token 执行 GET /ops/prometheus",
            "冒烟门禁后执行 GET /ops/alert-events",
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
    output_path = config.scan_output_dir / f"{image['service']}-{config.version}.scan.json"
    if Path(config.scan_tool).name.lower() == "trivy" or tool == "trivy":
        return [
            config.scan_tool,
            "image",
            "--exit-code",
            "1",
            "--format",
            "json",
            "--output",
            str(output_path),
            "--severity",
            config.scan_severity,
            image["versionTag"],
        ]
    if Path(config.scan_tool).name.lower() == "grype" or tool == "grype":
        return [config.scan_tool, image["versionTag"], "--fail-on", "high", "-o", "json", "--file", str(output_path)]
    return [config.scan_tool, image["versionTag"]]


def push_command(tag: str) -> list[str]:
    return ["docker", "push", tag]


def tool_enabled(value: str) -> bool:
    return value.strip().lower() not in DISABLED_TOOL_NAMES


def required_secret_template(config: ReleaseGateConfig) -> list[dict[str, str]]:
    secrets = [
        {
            "name": "AI_JSUNPACK_AUTH_SECRET",
            "scope": "api,worker,browser-runner",
            "injection": "secret manager 或 sealed CI variable",
        },
        {
            "name": "AI_JSUNPACK_ARTIFACT_S3_SECRET_ACCESS_KEY",
            "scope": "api,worker,browser-runner",
            "injection": "作用域限定为 artifact bucket 的 object-store credential",
        },
        {
            "name": "AI_JSUNPACK_BROWSER_RUNNER_TOKEN",
            "scope": "worker",
            "injection": "包含 serviceRoles=[\"worker\"] 的 HMAC Bearer token",
        },
        {
            "name": "VITE_API_AUTH_TOKEN",
            "scope": "web",
            "injection": "runtime/session token，不作为长期 production secret 烘焙进镜像",
        },
        {
            "name": "AI_JSUNPACK_ALERT_WEBHOOK_URL",
            "scope": "api",
            "injection": "可选的 ops webhook endpoint",
        },
        {
            "name": "model provider credentials",
            "scope": "worker",
            "injection": "仅在部署启用 cloud_allowed 模式时注入",
        },
    ]
    if config.ci_platform == "github_actions":
        for secret in secrets:
            secret["githubActions"] = github_secret_mapping(secret["name"])
            secret["githubEnvironment"] = config.secret_environment or "<github-environment>"
        secrets.insert(
            0,
            {
                "name": "GITHUB_TOKEN",
                "scope": "发布工作流",
                "injection": "具有 contents:read 和 packages:write 权限的 GitHub Actions 自动 token",
                "githubActions": "${{ github.token }}",
                "githubEnvironment": config.secret_environment or "<workflow>",
            },
        )
    return secrets


def github_secret_mapping(name: str) -> str:
    if name == "model provider credentials":
        return "所选 provider 的仓库/环境 secret，仅注入 Worker 发布任务"
    return "${{ secrets." + name + " }}"


def ci_platform_summary(config: ReleaseGateConfig) -> dict[str, Any]:
    if config.ci_platform == "github_actions":
        return {
            "name": "github_actions",
            "registry": config.registry,
            "registryLogin": "使用 github.actor 和 GITHUB_TOKEN 执行 docker login",
            "permissions": {
                "contents": "read",
                "packages": "write",
            },
            "workflow": ".github/workflows/release-gate.yml",
            "secretStore": "GitHub repository 或 environment secrets",
            "secretEnvironment": config.secret_environment or "<github-environment>",
            "artifactStore": "GitHub Actions Artifact，以及保留的 production DB/Artifact Store snapshot",
            "runContext": github_actions_run_context(config),
        }
    return {
        "name": "generic",
        "registry": config.registry,
        "secretStore": "外部 CI secret store、deployment secret manager 或 sealed variable",
        "artifactStore": "发布系统 Artifact archive，以及保留的 production DB/Artifact Store snapshot",
    }


def archive_plan(config: ReleaseGateConfig, images: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "localArtifacts": [
            str(config.output_path),
            str(config.compose_smoke_output),
            str(config.deployment_smoke_output),
            str(config.sbom_output_dir),
            str(config.scan_output_dir),
        ],
        "externalEvidence": [
            "每个已推送 tag 对应的 container registry digest",
            "保留在 CI 工作区之外的 PostgreSQL 转储或卷快照",
            "保留在 CI workspace 之外的 Artifact Store bucket 或 prefix export",
            "回滚或拆除前捕获的 Compose 日志",
            "不含 secret values 的 secret manager revision 或 deployment environment record",
        ],
        "githubActionsArtifacts": github_actions_artifacts(config) if config.ci_platform == "github_actions" else [],
        "ciRun": ci_run_context(config),
        "registryDigestEvidence": registry_digest_evidence(images),
        "secretManagerEvidence": secret_manager_evidence(config),
        "productionArchiveChecklist": production_archive_checklist(config, images),
    }


def github_actions_artifacts(config: ReleaseGateConfig) -> list[dict[str, str]]:
    return [
        {"name": "release-gate-report", "path": str(config.output_path)},
        {"name": "release-gate-sbom", "path": str(config.sbom_output_dir)},
        {"name": "release-gate-scans", "path": str(config.scan_output_dir)},
        {"name": "compose-smoke-report", "path": str(config.compose_smoke_output)},
        {"name": "deployment-smoke-report", "path": str(config.deployment_smoke_output)},
    ]


def ci_run_context(config: ReleaseGateConfig) -> dict[str, Any]:
    if config.ci_platform == "github_actions":
        return github_actions_run_context(config)
    return {
        "provider": "generic",
        "runUrl": "<ci-run-url>",
        "runId": "<ci-run-id>",
        "repository": "<repository>",
        "commit": short_sha(config.git_sha),
    }


def github_actions_run_context(config: ReleaseGateConfig) -> dict[str, Any]:
    server_url = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repository = os.getenv("GITHUB_REPOSITORY", "<owner/repo>")
    run_id = os.getenv("GITHUB_RUN_ID", "<run-id>")
    return {
        "provider": "github_actions",
        "workflow": os.getenv("GITHUB_WORKFLOW", "Release Gate"),
        "runId": run_id,
        "runNumber": os.getenv("GITHUB_RUN_NUMBER", "<run-number>"),
        "runAttempt": os.getenv("GITHUB_RUN_ATTEMPT", "<run-attempt>"),
        "runUrl": f"{server_url}/{repository}/actions/runs/{run_id}",
        "repository": repository,
        "actor": os.getenv("GITHUB_ACTOR", "<actor>"),
        "ref": os.getenv("GITHUB_REF_NAME", "<ref>"),
        "commit": short_sha(os.getenv("GITHUB_SHA", config.git_sha)),
        "environment": config.secret_environment or os.getenv("GITHUB_ENVIRONMENT_NAME", "<github-environment>"),
    }


def registry_digest_evidence(images: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "service": image["service"],
            "tag": image["versionTag"],
            "digestReference": f"{image['repository']}@sha256:<registry-digest>",
            "source": "push_images=true 后的 container registry digest",
        }
        for image in images
    ]


def secret_manager_evidence(config: ReleaseGateConfig) -> dict[str, Any]:
    if config.ci_platform == "github_actions":
        return {
            "provider": "github_environments",
            "environment": config.secret_environment or os.getenv("GITHUB_ENVIRONMENT_NAME", "<github-environment>"),
            "requiredEvidence": [
                "不含值的 environment secret revision 或配置变更记录",
                "启用时的工作流环境审批记录或部署保护记录",
                "与 requiredSecrets[].githubActions 匹配的 repository/environment secret names",
            ],
            "containsSecretValues": False,
        }
    return {
        "provider": "external_secret_manager",
        "environment": config.secret_environment or "<deployment-environment>",
        "requiredEvidence": [
            "不含值的 secret manager revision 或 sealed variable revision",
            "目标平台支持审批时的部署审批记录",
            "与 requiredSecrets[].name 匹配的 secret names",
        ],
        "containsSecretValues": False,
    }


def production_archive_checklist(config: ReleaseGateConfig, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions_artifacts = github_actions_artifacts(config) if config.ci_platform == "github_actions" else []
    return [
        {
            "name": "ci_workflow_run",
            "required": True,
            "status": "external_required",
            "evidenceRef": ci_run_context(config)["runUrl"],
            "containsSecretValues": False,
        },
        {
            "name": "actions_artifacts",
            "required": config.ci_platform == "github_actions",
            "status": "external_required" if config.ci_platform == "github_actions" else "not_applicable",
            "evidenceRef": [item["name"] for item in actions_artifacts],
            "containsSecretValues": False,
        },
        {
            "name": "ghcr_image_digests",
            "required": config.push,
            "status": "external_required",
            "evidenceRef": [item["digestReference"] for item in registry_digest_evidence(images)],
            "containsSecretValues": False,
        },
        {
            "name": "database_snapshot",
            "required": True,
            "status": "external_required",
            "evidenceRef": "保留在 CI 工作区之外的 PostgreSQL 转储或卷快照",
            "containsSecretValues": False,
        },
        {
            "name": "artifact_store_export",
            "required": True,
            "status": "external_required",
            "evidenceRef": "保留在 CI workspace 之外的 Artifact Store bucket 或 prefix export",
            "containsSecretValues": False,
        },
        {
            "name": "secret_manager_revision",
            "required": True,
            "status": "external_required",
            "evidenceRef": secret_manager_evidence(config)["environment"],
            "containsSecretValues": False,
        },
        {
            "name": "rollback_evidence",
            "required": True,
            "status": "external_required",
            "evidenceRef": config.previous_version or "<previous-known-good-tag>",
            "containsSecretValues": False,
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
        {
            "name": "production_archive_evidence",
            "required": True,
            "evidence": [
                "archivePlan.productionArchiveChecklist",
                "archivePlan.registryDigestEvidence",
                "archivePlan.secretManagerEvidence",
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
            "已保留的 PostgreSQL 导出或卷快照",
            "已保留的 Artifact Store bucket export 或 prefix snapshot",
            "回滚前捕获的 Compose 日志",
        ],
        "procedure": [
            "保留 release gate、Compose smoke、deployment smoke、DB、Artifact Store 和 service logs。",
            "将 Compose 镜像环境变量恢复为回滚标签。",
            "使用 --skip-build 重新运行 deploy.compose_smoke，并比较 archive_manifest 中保留的证据。",
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
        add_check(checks, f"{check_prefix}_skipped", True, evidence={"reason": "工具已禁用"})
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
            raise RuntimeError(f"{check_prefix}_{index} 失败，退出码为 {result.returncode}")


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
