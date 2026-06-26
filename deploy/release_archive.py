from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "tmp" / "release-archive" / "release-archive.json"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
PLACEHOLDER_PATTERN = re.compile(r"<[^>]+>")
SECRET_VALUE_KEYS = {
    "access_token",
    "accesstoken",
    "password",
    "private_key",
    "privatekey",
    "secret",
    "secret_access_key",
    "secret_value",
    "secretaccesskey",
    "secretvalue",
    "token",
    "value",
}


@dataclass(frozen=True)
class ReleaseArchiveConfig:
    release_gate_report: Path
    compose_smoke_report: Path
    deployment_smoke_report: Path
    evidence_manifest: Path
    output_path: Path = DEFAULT_OUTPUT


def parse_args(argv: list[str] | None = None) -> ReleaseArchiveConfig:
    parser = argparse.ArgumentParser(description="Verify a production release evidence archive.")
    parser.add_argument("--release-gate-report", type=Path, required=True)
    parser.add_argument("--compose-smoke-report", type=Path, required=True)
    parser.add_argument("--deployment-smoke-report", type=Path, required=True)
    parser.add_argument("--evidence-manifest", type=Path, required=True)
    parser.add_argument("--output", dest="output_path", type=Path, default=DEFAULT_OUTPUT)
    return ReleaseArchiveConfig(**vars(parser.parse_args(argv)))


def run_release_archive(config: ReleaseArchiveConfig) -> dict[str, Any]:
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    missing_evidence: list[str] = []
    release_gate = read_json(config.release_gate_report)
    compose_smoke = read_json(config.compose_smoke_report)
    deployment_smoke = read_json(config.deployment_smoke_report)
    manifest = read_json(config.evidence_manifest)

    add_check(
        checks,
        "release_gate_report_present",
        release_gate.get("kind") == "release_gate_report",
        evidence={"path": str(config.release_gate_report), "kind": release_gate.get("kind")},
    )
    add_check(
        checks,
        "release_gate_passed",
        release_gate.get("status") == "pass",
        evidence={"status": release_gate.get("status"), "failedChecks": release_gate.get("failedChecks", [])},
    )
    release_executed = release_gate.get("mode") == "execute" and bool(release_gate.get("config", {}).get("execute"))
    if not release_executed:
        missing_evidence.append("releaseGate.executed")
    add_check(
        checks,
        "release_gate_executed",
        release_executed,
        evidence={"mode": release_gate.get("mode"), "execute": release_gate.get("config", {}).get("execute")},
    )

    add_check(
        checks,
        "compose_smoke_passed",
        compose_smoke.get("kind") == "compose_smoke_report" and compose_smoke.get("status") == "pass",
        evidence={"kind": compose_smoke.get("kind"), "status": compose_smoke.get("status")},
    )
    add_check(
        checks,
        "deployment_smoke_passed",
        deployment_smoke.get("kind") == "deployment_smoke_report" and deployment_smoke.get("status") == "pass",
        evidence={"kind": deployment_smoke.get("kind"), "status": deployment_smoke.get("status")},
    )
    archive_ready = bool(
        deployment_smoke.get("archive_manifest", {}).get("archiveReady")
        and compose_smoke.get("deploymentSmoke", {}).get("archive_manifest", {}).get("archiveReady")
    )
    if not archive_ready:
        missing_evidence.append("deploymentSmoke.archive_manifest.archiveReady")
    add_check(
        checks,
        "deployment_archive_ready",
        archive_ready,
        evidence={
            "deploymentArchiveReady": deployment_smoke.get("archive_manifest", {}).get("archiveReady"),
            "composeDeploymentArchiveReady": compose_smoke.get("deploymentSmoke", {})
            .get("archive_manifest", {})
            .get("archiveReady"),
        },
    )

    add_check(
        checks,
        "evidence_manifest_kind",
        manifest.get("kind") == "production_release_evidence_manifest",
        evidence={"kind": manifest.get("kind"), "schemaVersion": manifest.get("schemaVersion")},
    )
    ci_run_valid = validate_ci_run(manifest.get("ciRun"))
    if not ci_run_valid:
        missing_evidence.append("ciRun")
    add_check(checks, "ci_run_evidence", ci_run_valid, evidence=ci_run_summary(manifest.get("ciRun")))

    placeholder_paths = placeholder_value_paths(manifest)
    if placeholder_paths:
        missing_evidence.append("placeholderValuesReplaced")
    add_check(
        checks,
        "placeholder_values_replaced",
        not placeholder_paths,
        evidence={"paths": placeholder_paths},
    )

    secret_paths = secret_value_paths(manifest)
    secret_flag_paths = secret_value_flag_paths(manifest)
    if secret_paths:
        missing_evidence.append("secretValuesAbsent")
    if secret_flag_paths:
        missing_evidence.append("containsSecretValuesFalse")
    add_check(
        checks,
        "secret_values_absent",
        not secret_paths and not secret_flag_paths,
        evidence={
            "containsSecretValues": bool(secret_paths or secret_flag_paths),
            "paths": secret_paths,
            "flagPaths": secret_flag_paths,
        },
    )
    secret_manager_valid = validate_secret_manager(manifest.get("secretManager"))
    if not secret_manager_valid:
        missing_evidence.append("secretManager")
    add_check(
        checks,
        "secret_manager_evidence",
        secret_manager_valid,
        evidence=secret_manager_summary(manifest.get("secretManager")),
    )

    for name in ("databaseSnapshot", "artifactStoreExport", "serviceLogs", "rollbackEvidence"):
        present = has_evidence_reference(manifest.get(name))
        if not present:
            missing_evidence.append(name)
        add_check(checks, f"{camel_to_snake(name)}_evidence", present, evidence=evidence_summary(manifest.get(name)))

    push_required = bool(release_gate.get("config", {}).get("push"))
    matched_digests, missing_digests = match_registry_digests(
        release_gate.get("images", []),
        manifest.get("registryDigests", []),
        required=push_required,
    )
    missing_evidence.extend(missing_digests)
    add_check(
        checks,
        "registry_digest_evidence",
        not missing_digests,
        evidence={
            "required": push_required,
            "matchedCount": len(matched_digests),
            "missing": missing_digests,
        },
    )

    report: dict[str, Any] = {
        "kind": "production_release_archive_report",
        "schemaVersion": "1",
        "status": "running",
        "generatedAt": utc_now(),
        "durationMs": 0,
        "config": safe_config(config),
        "release": release_summary(release_gate),
        "evidenceManifest": evidence_manifest_summary(manifest),
        "checks": checks,
        "failedChecks": [],
        "missingEvidence": sorted(set(missing_evidence)),
        "matchedRegistryDigests": matched_digests,
        "platformDifferences": redact_secret_values(manifest.get("platformDifferences", [])),
    }
    finalize_report(report, started)
    write_report(config.output_path, report)
    return report


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_ci_run(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return all(non_empty_string(value.get(field)) for field in ("runUrl", "runId", "commit", "environment"))


def ci_run_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "runUrl": value.get("runUrl"),
        "runId": value.get("runId"),
        "commit": value.get("commit"),
        "environment": value.get("environment"),
        "artifactCount": len(value.get("artifacts", [])) if isinstance(value.get("artifacts"), list) else 0,
    }


def validate_secret_manager(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    has_revision = any(non_empty_string(value.get(field)) for field in ("revision", "revisionRef", "approvalRecord"))
    return (
        non_empty_string(value.get("provider"))
        and non_empty_string(value.get("environment"))
        and has_revision
        and value.get("containsSecretValues", False) is False
    )


def secret_manager_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "provider": value.get("provider"),
        "environment": value.get("environment"),
        "revision": value.get("revision") or value.get("revisionRef"),
        "approvalRecord": value.get("approvalRecord"),
        "containsSecretValues": value.get("containsSecretValues", False),
    }


def match_registry_digests(
    images: Any,
    registry_digests: Any,
    *,
    required: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not required:
        return [], []
    if not isinstance(images, list):
        return [], ["registryDigests.images"]
    if not isinstance(registry_digests, list):
        return [], ["registryDigests"]
    matched: list[dict[str, Any]] = []
    missing: list[str] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        service = str(image.get("service") or "")
        version_tag = str(image.get("versionTag") or "")
        repository = str(image.get("repository") or version_tag.rsplit(":", 1)[0])
        candidate = find_digest_candidate(registry_digests, service=service, version_tag=version_tag, repository=repository)
        if candidate is None:
            missing.append(f"registryDigests.{service or version_tag or 'unknown'}")
            continue
        digest = normalize_digest(candidate)
        if not digest or not DIGEST_PATTERN.match(digest):
            missing.append(f"registryDigests.{service}.digest")
            continue
        matched.append(
            {
                "service": service,
                "tag": version_tag,
                "digest": digest,
                "digestReference": f"{repository}@{digest}",
            }
        )
    return matched, missing


def find_digest_candidate(registry_digests: list[Any], *, service: str, version_tag: str, repository: str) -> dict[str, Any] | None:
    for item in registry_digests:
        if not isinstance(item, dict):
            continue
        if item.get("service") == service and item.get("tag") in {None, "", version_tag}:
            return item
        reference = str(item.get("digestReference") or item.get("reference") or "")
        if reference.startswith(f"{repository}@"):
            return item
    return None


def normalize_digest(item: dict[str, Any]) -> str:
    digest = str(item.get("digest") or "")
    if digest:
        return digest
    reference = str(item.get("digestReference") or item.get("reference") or "")
    if "@sha256:" in reference:
        return reference.split("@", 1)[1]
    return ""


def secret_value_paths(value: Any, *, path: str = "$") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if normalize_key(key) in SECRET_VALUE_KEYS and child not in (None, "", False):
                paths.append(child_path)
                continue
            paths.extend(secret_value_paths(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(secret_value_paths(child, path=f"{path}[{index}]"))
    return paths


def placeholder_value_paths(value: Any, *, path: str = "$") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            paths.extend(placeholder_value_paths(child, path=f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(placeholder_value_paths(child, path=f"{path}[{index}]"))
    elif isinstance(value, str) and PLACEHOLDER_PATTERN.search(value):
        paths.append(path)
    return paths


def secret_value_flag_paths(value: Any, *, path: str = "$") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == "containsSecretValues" and child is True:
                paths.append(child_path)
                continue
            paths.extend(secret_value_flag_paths(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(secret_value_flag_paths(child, path=f"{path}[{index}]"))
    return paths


def redact_secret_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if normalize_key(key) in SECRET_VALUE_KEYS and child not in (None, "", False) else redact_secret_values(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_secret_values(item) for item in value]
    return value


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", value.replace("-", "_").lower())


def has_evidence_reference(value: Any) -> bool:
    if isinstance(value, dict):
        reference_fields = ("evidenceRef", "path", "uri", "url", "snapshotId", "artifactId", "reference")
        return any(non_empty_string(value.get(field)) for field in reference_fields)
    if isinstance(value, list):
        return bool(value) and all(has_evidence_reference(item) for item in value)
    return False


def evidence_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {"itemCount": len(value)}
    if not isinstance(value, dict):
        return {}
    return {
        "evidenceRef": value.get("evidenceRef") or value.get("reference") or value.get("url") or value.get("uri"),
        "path": value.get("path"),
        "sha256": value.get("sha256"),
        "containsSecretValues": value.get("containsSecretValues", False),
    }


def release_summary(release_gate: dict[str, Any]) -> dict[str, Any]:
    config = release_gate.get("config", {})
    return {
        "status": release_gate.get("status"),
        "mode": release_gate.get("mode"),
        "version": config.get("version"),
        "gitSha": config.get("git_sha"),
        "ciPlatform": release_gate.get("ciPlatform", {}).get("name"),
        "pushRequired": bool(config.get("push")),
        "imageCount": len(release_gate.get("images", [])) if isinstance(release_gate.get("images"), list) else 0,
    }


def evidence_manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": manifest.get("kind"),
        "schemaVersion": manifest.get("schemaVersion"),
        "ciRun": ci_run_summary(manifest.get("ciRun")),
        "secretManager": secret_manager_summary(manifest.get("secretManager")),
        "platformDifferenceCount": len(manifest.get("platformDifferences", []))
        if isinstance(manifest.get("platformDifferences"), list)
        else 0,
    }


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


def safe_config(config: ReleaseArchiveConfig) -> dict[str, str]:
    payload = asdict(config)
    return {key: str(value) for key, value in payload.items()}


def camel_to_snake(value: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", value).lower()


def non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    report = run_release_archive(config)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
