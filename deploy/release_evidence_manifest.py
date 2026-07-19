from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "tmp" / "release-gate" / "production-evidence-manifest.json"


@dataclass(frozen=True)
class ReleaseEvidenceManifestConfig:
    release_gate_report: Path
    output_path: Path = DEFAULT_OUTPUT
    compose_smoke_report: Path | None = None
    deployment_smoke_report: Path | None = None
    database_snapshot_ref: str = ""
    artifact_store_export_ref: str = ""
    service_logs_ref: str = ""
    secret_revision_ref: str = ""
    secret_approval_record: str = ""
    previous_version: str = ""


def parse_args(argv: list[str] | None = None) -> ReleaseEvidenceManifestConfig:
    parser = argparse.ArgumentParser(description="生成 production release evidence Manifest 模板。")
    parser.add_argument("--release-gate-report", type=Path, required=True)
    parser.add_argument("--output", dest="output_path", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--compose-smoke-report", type=Path)
    parser.add_argument("--deployment-smoke-report", type=Path)
    parser.add_argument("--database-snapshot-ref", default="")
    parser.add_argument("--artifact-store-export-ref", default="")
    parser.add_argument("--service-logs-ref", default="")
    parser.add_argument("--secret-revision-ref", default="")
    parser.add_argument("--secret-approval-record", default="")
    parser.add_argument("--previous-version", default="")
    return ReleaseEvidenceManifestConfig(**vars(parser.parse_args(argv)))


def run_release_evidence_manifest(config: ReleaseEvidenceManifestConfig) -> dict[str, Any]:
    started = time.perf_counter()
    release_gate = read_json(config.release_gate_report)
    if release_gate.get("kind") != "release_gate_report":
        raise ValueError("--release-gate-report 必须指向 release_gate_report JSON 文件。")

    manifest: dict[str, Any] = {
        "kind": "production_release_evidence_manifest",
        "schemaVersion": "1",
        "generatedAt": utc_now(),
        "durationMs": 0,
        "sourceReports": source_reports(config),
        "ciRun": ci_run_from_release_gate(release_gate),
        "registryDigests": registry_digest_templates(release_gate),
        "secretManager": secret_manager_template(release_gate, config),
        "databaseSnapshot": evidence_template(
            config.database_snapshot_ref,
            placeholder=placeholder_ref(release_gate, "db.dump"),
            sha_placeholder="<database-snapshot-sha256>",
        ),
        "artifactStoreExport": evidence_template(
            config.artifact_store_export_ref,
            placeholder=placeholder_ref(release_gate, "artifacts/"),
            sha_placeholder="<artifact-store-export-sha256>",
        ),
        "serviceLogs": {
            "evidenceRef": config.service_logs_ref or placeholder_ref(release_gate, "service-logs.txt"),
            "containsSecretValues": False,
        },
        "rollbackEvidence": rollback_evidence(release_gate, config),
        "platformDifferences": [],
        "completionNotes": completion_notes(release_gate),
    }
    manifest["durationMs"] = int((time.perf_counter() - started) * 1000)
    write_json(config.output_path, manifest)
    return manifest


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def source_reports(config: ReleaseEvidenceManifestConfig) -> dict[str, str]:
    reports = {"releaseGateReport": str(config.release_gate_report)}
    if config.compose_smoke_report is not None:
        reports["composeSmokeReport"] = str(config.compose_smoke_report)
    if config.deployment_smoke_report is not None:
        reports["deploymentSmokeReport"] = str(config.deployment_smoke_report)
    return reports


def ci_run_from_release_gate(release_gate: dict[str, Any]) -> dict[str, Any]:
    ci_platform = release_gate.get("ciPlatform", {}) if isinstance(release_gate.get("ciPlatform"), dict) else {}
    run_context = ci_platform.get("runContext", {}) if isinstance(ci_platform.get("runContext"), dict) else {}
    artifacts = release_gate.get("archivePlan", {}).get("githubActionsArtifacts", [])
    artifact_names = [
        item.get("name")
        for item in artifacts
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip()
    ]
    return {
        "runUrl": run_context.get("runUrl") or "<ci-run-url>",
        "runId": run_context.get("runId") or "<ci-run-id>",
        "commit": run_context.get("commit") or release_gate.get("config", {}).get("git_sha") or "<commit-sha>",
        "environment": run_context.get("environment")
        or ci_platform.get("secretEnvironment")
        or release_gate.get("config", {}).get("secret_environment")
        or "<deployment-environment>",
        "artifacts": artifact_names,
    }


def registry_digest_templates(release_gate: dict[str, Any]) -> list[dict[str, str]]:
    templates: list[dict[str, str]] = []
    for image in release_gate.get("images", []):
        if not isinstance(image, dict):
            continue
        service = str(image.get("service") or "")
        tag = str(image.get("versionTag") or "")
        repository = str(image.get("repository") or tag.rsplit(":", 1)[0])
        templates.append(
            {
                "service": service,
                "tag": tag,
                "digest": "sha256:<replace-with-ghcr-digest>",
                "digestReference": f"{repository}@sha256:<replace-with-ghcr-digest>",
            }
        )
    return templates


def secret_manager_template(
    release_gate: dict[str, Any],
    config: ReleaseEvidenceManifestConfig,
) -> dict[str, Any]:
    ci_platform = release_gate.get("ciPlatform", {}) if isinstance(release_gate.get("ciPlatform"), dict) else {}
    provider = "github_environments" if ci_platform.get("name") == "github_actions" else "external_secret_manager"
    environment = (
        release_gate.get("config", {}).get("secret_environment")
        or ci_platform.get("secretEnvironment")
        or ci_run_from_release_gate(release_gate).get("environment")
    )
    return {
        "provider": provider,
        "environment": environment or "<deployment-environment>",
        "revision": config.secret_revision_ref or "<secret-environment-revision-or-change-record>",
        "approvalRecord": config.secret_approval_record or "<approval-or-deployment-protection-record>",
        "containsSecretValues": False,
    }


def evidence_template(reference: str, *, placeholder: str, sha_placeholder: str) -> dict[str, Any]:
    return {
        "evidenceRef": reference or placeholder,
        "sha256": sha_placeholder,
        "containsSecretValues": False,
    }


def rollback_evidence(release_gate: dict[str, Any], config: ReleaseEvidenceManifestConfig) -> dict[str, str]:
    previous_version = config.previous_version or release_gate.get("config", {}).get("previous_version") or "<previous-version>"
    rollback = release_gate.get("rollback", {})
    mapping = rollback.get("imageTagMapping", []) if isinstance(rollback, dict) else []
    first_rollback = next(
        (item.get("rollback") for item in mapping if isinstance(item, dict) and item.get("rollback")),
        "",
    )
    return {
        "evidenceRef": first_rollback or f"<rollback-image-tag-for-{previous_version}>",
        "previousVersion": previous_version,
    }


def completion_notes(release_gate: dict[str, Any]) -> list[str]:
    push_required = bool(release_gate.get("config", {}).get("push"))
    notes = [
        "将每个 registryDigests[].digest placeholder 替换为 container registry 报告的 digest。",
        "执行 archive verification 前，将 snapshot/export/log placeholder 替换为已保留的生产证据引用。",
        "保持 containsSecretValues=false，且不要在此 Manifest 中记录 secret values。",
    ]
    if push_required:
        notes.append("由于 release gate 使用了 push=true，deploy.release_archive 要求每个 service image 都提供有效 digest。")
    return notes


def placeholder_ref(release_gate: dict[str, Any], leaf: str) -> str:
    version = release_gate.get("config", {}).get("version") or "<version>"
    return f"s3://release-archive/{version}/{leaf}"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_config(config: ReleaseEvidenceManifestConfig) -> dict[str, str]:
    payload = asdict(config)
    return {key: str(value) for key, value in payload.items() if value not in (None, "")}


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    manifest = run_release_evidence_manifest(config)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
