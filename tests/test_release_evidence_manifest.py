import json
import tempfile
import unittest
from pathlib import Path

from deploy.release_archive import ReleaseArchiveConfig, run_release_archive
from deploy.release_evidence_manifest import ReleaseEvidenceManifestConfig, run_release_evidence_manifest
from deploy.release_gate import ReleaseGateConfig, run_release_gate


DIGESTS = {
    "api": "sha256:" + "a" * 64,
    "worker": "sha256:" + "b" * 64,
    "browser-runner": "sha256:" + "c" * 64,
    "web": "sha256:" + "d" * 64,
}


class ReleaseEvidenceManifestTest(unittest.TestCase):
    def test_generates_github_actions_manifest_template_from_release_gate_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            release_gate_path = root / "release-gate.json"
            run_release_gate(
                ReleaseGateConfig(
                    registry="ghcr.io",
                    repository_prefix="owner/ai-jsunpack",
                    version="2026.06.26",
                    git_sha="abcdef1234567890",
                    previous_version="2026.06.25",
                    ci_platform="github_actions",
                    secret_environment="production",
                    output_path=release_gate_path,
                    push=True,
                )
            )

            manifest = run_release_evidence_manifest(
                ReleaseEvidenceManifestConfig(
                    release_gate_report=release_gate_path,
                    output_path=root / "production-evidence-manifest.json",
                    compose_smoke_report=root / "compose-smoke.json",
                    deployment_smoke_report=root / "deployment-smoke.json",
                )
            )

            self.assertEqual(manifest["kind"], "production_release_evidence_manifest")
            self.assertEqual(manifest["ciRun"]["environment"], "production")
            self.assertIn("release-gate-report", manifest["ciRun"]["artifacts"])
            self.assertEqual({item["service"] for item in manifest["registryDigests"]}, set(DIGESTS))
            self.assertEqual(manifest["registryDigests"][0]["digest"], "sha256:<replace-with-ghcr-digest>")
            self.assertEqual(manifest["secretManager"]["provider"], "github_environments")
            self.assertEqual(manifest["secretManager"]["containsSecretValues"], False)
            self.assertEqual(manifest["rollbackEvidence"]["previousVersion"], "2026.06.25")

            serialized = json.dumps(manifest, ensure_ascii=False)
            self.assertNotIn("${{ secrets.", serialized)
            self.assertNotIn("replace-with-a-shared-hmac-secret", serialized)

    def test_archive_rejects_unfilled_manifest_template_placeholders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _write_release_inputs(root)
            manifest = run_release_evidence_manifest(
                ReleaseEvidenceManifestConfig(
                    release_gate_report=paths["release_gate"],
                    output_path=paths["manifest"],
                )
            )
            self.assertIn("<replace-with-ghcr-digest>", json.dumps(manifest))

            report = run_release_archive(
                ReleaseArchiveConfig(
                    release_gate_report=paths["release_gate"],
                    compose_smoke_report=paths["compose_smoke"],
                    deployment_smoke_report=paths["deployment_smoke"],
                    evidence_manifest=paths["manifest"],
                    output_path=root / "release-archive.json",
                )
            )

            self.assertEqual(report["status"], "fail")
            self.assertIn("placeholder_values_replaced", report["failedChecks"])
            self.assertIn("placeholderValuesReplaced", report["missingEvidence"])

    def test_archive_passes_after_manifest_template_is_filled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _write_release_inputs(root)
            manifest = run_release_evidence_manifest(
                ReleaseEvidenceManifestConfig(
                    release_gate_report=paths["release_gate"],
                    output_path=paths["manifest"],
                    database_snapshot_ref="s3://release-archive/2026.06.26/db.dump",
                    artifact_store_export_ref="s3://release-archive/2026.06.26/artifacts/",
                    service_logs_ref="s3://release-archive/2026.06.26/service-logs.txt",
                    secret_revision_ref="environment-revision-42",
                    secret_approval_record="https://github.com/owner/ai-jsunpack/actions/runs/123456",
                )
            )
            for item in manifest["registryDigests"]:
                item["digest"] = DIGESTS[item["service"]]
                item["digestReference"] = item["digestReference"].replace(
                    "sha256:<replace-with-ghcr-digest>",
                    DIGESTS[item["service"]],
                )
            manifest["databaseSnapshot"]["sha256"] = "dbsha"
            manifest["artifactStoreExport"]["sha256"] = "artifactsha"
            paths["manifest"].write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            report = run_release_archive(
                ReleaseArchiveConfig(
                    release_gate_report=paths["release_gate"],
                    compose_smoke_report=paths["compose_smoke"],
                    deployment_smoke_report=paths["deployment_smoke"],
                    evidence_manifest=paths["manifest"],
                    output_path=root / "release-archive.json",
                )
            )

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["failedChecks"], [])
            self.assertEqual(len(report["matchedRegistryDigests"]), 4)


def _write_release_inputs(root: Path) -> dict[str, Path]:
    paths = {
        "release_gate": root / "release-gate.json",
        "compose_smoke": root / "compose-smoke.json",
        "deployment_smoke": root / "deployment-smoke.json",
        "manifest": root / "production-evidence-manifest.json",
    }
    paths["release_gate"].write_text(json.dumps(_release_gate_report(), ensure_ascii=False), encoding="utf-8")
    paths["compose_smoke"].write_text(json.dumps(_compose_smoke_report(), ensure_ascii=False), encoding="utf-8")
    paths["deployment_smoke"].write_text(
        json.dumps(_deployment_smoke_report(), ensure_ascii=False),
        encoding="utf-8",
    )
    return paths


def _release_gate_report() -> dict[str, object]:
    return {
        "kind": "release_gate_report",
        "schemaVersion": "1",
        "status": "pass",
        "mode": "execute",
        "config": {
            "execute": True,
            "push": True,
            "version": "2026.06.26",
            "git_sha": "abcdef123456",
            "previous_version": "2026.06.25",
            "secret_environment": "production",
        },
        "ciPlatform": {
            "name": "github_actions",
            "secretEnvironment": "production",
            "runContext": {
                "runUrl": "https://github.com/owner/ai-jsunpack/actions/runs/123456",
                "runId": "123456",
                "commit": "abcdef123456",
                "environment": "production",
            },
        },
        "archivePlan": {
            "githubActionsArtifacts": [
                {"name": "release-gate-report", "path": "release-gate.json"},
                {"name": "release-gate-sbom", "path": "sbom"},
                {"name": "release-gate-scans", "path": "scans"},
                {"name": "compose-smoke-report", "path": "compose-smoke.json"},
                {"name": "deployment-smoke-report", "path": "deployment-smoke.json"},
            ]
        },
        "images": [_image("api"), _image("worker"), _image("browser-runner"), _image("web")],
        "rollback": {
            "imageTagMapping": [
                {
                    "service": "api",
                    "current": "ghcr.io/owner/ai-jsunpack/api:2026.06.26",
                    "rollback": "ghcr.io/owner/ai-jsunpack/api:2026.06.25",
                }
            ]
        },
        "failedChecks": [],
    }


def _image(service: str) -> dict[str, str]:
    repository = f"ghcr.io/owner/ai-jsunpack/{service}"
    return {
        "service": service,
        "repository": repository,
        "versionTag": f"{repository}:2026.06.26",
    }


def _compose_smoke_report() -> dict[str, object]:
    return {
        "kind": "compose_smoke_report",
        "schemaVersion": "1",
        "status": "pass",
        "deploymentSmoke": {"archive_manifest": {"archiveReady": True}},
        "failedChecks": [],
    }


def _deployment_smoke_report() -> dict[str, object]:
    return {
        "kind": "deployment_smoke_report",
        "schemaVersion": "1",
        "status": "pass",
        "archive_manifest": {
            "kind": "deployment_smoke_archive_manifest",
            "archiveReady": True,
            "retainedEvidence": {"resultPackageSha256": "abc123", "prometheusScraped": True},
        },
        "failedChecks": [],
    }


if __name__ == "__main__":
    unittest.main()
