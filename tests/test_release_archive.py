import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from deploy.release_archive import ReleaseArchiveConfig, main, run_release_archive


DIGESTS = {
    "api": "sha256:" + "a" * 64,
    "worker": "sha256:" + "b" * 64,
    "browser-runner": "sha256:" + "c" * 64,
    "web": "sha256:" + "d" * 64,
}


class ReleaseArchiveTest(unittest.TestCase):
    def test_release_archive_passes_with_complete_external_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _write_inputs(Path(temp_dir))
            report = run_release_archive(
                ReleaseArchiveConfig(
                    release_gate_report=paths["release_gate"],
                    compose_smoke_report=paths["compose_smoke"],
                    deployment_smoke_report=paths["deployment_smoke"],
                    evidence_manifest=paths["manifest"],
                    output_path=Path(temp_dir) / "release-archive.json",
                )
            )

            persisted = json.loads((Path(temp_dir) / "release-archive.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass")
            self.assertEqual(persisted["kind"], "production_release_archive_report")
            self.assertEqual(persisted["failedChecks"], [])
            self.assertEqual(persisted["missingEvidence"], [])
            self.assertEqual(len(persisted["matchedRegistryDigests"]), 4)
            self.assertIn("ghcr.io/owner/ai-jsunpack/api@sha256:", persisted["matchedRegistryDigests"][0]["digestReference"])
            self.assertEqual(persisted["evidenceManifest"]["secretManager"]["provider"], "github_environments")

    def test_release_archive_fails_when_required_registry_digest_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _write_inputs(Path(temp_dir), missing_service="worker")
            report = run_release_archive(
                ReleaseArchiveConfig(
                    release_gate_report=paths["release_gate"],
                    compose_smoke_report=paths["compose_smoke"],
                    deployment_smoke_report=paths["deployment_smoke"],
                    evidence_manifest=paths["manifest"],
                    output_path=Path(temp_dir) / "release-archive.json",
                )
            )

            self.assertEqual(report["status"], "fail")
            self.assertIn("registry_digest_evidence", report["failedChecks"])
            self.assertIn("registryDigests.worker", report["missingEvidence"])

    def test_release_archive_rejects_secret_values_in_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _write_inputs(Path(temp_dir), secret_value="do-not-record-this")
            report = run_release_archive(
                ReleaseArchiveConfig(
                    release_gate_report=paths["release_gate"],
                    compose_smoke_report=paths["compose_smoke"],
                    deployment_smoke_report=paths["deployment_smoke"],
                    evidence_manifest=paths["manifest"],
                    output_path=Path(temp_dir) / "release-archive.json",
                )
            )

            self.assertEqual(report["status"], "fail")
            self.assertIn("secret_values_absent", report["failedChecks"])
            serialized = json.dumps(report, ensure_ascii=False)
            self.assertIn("$.secretManager.secret", serialized)
            self.assertNotIn("do-not-record-this", serialized)

    def test_release_archive_records_platform_differences_without_failing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _write_inputs(
                Path(temp_dir),
                platform_differences=[
                    {
                        "area": "secret_injection",
                        "expected": "GitHub Environment secrets",
                        "actual": "Vault dynamic secrets",
                        "docUpdateRequired": True,
                    }
                ],
            )
            report = run_release_archive(
                ReleaseArchiveConfig(
                    release_gate_report=paths["release_gate"],
                    compose_smoke_report=paths["compose_smoke"],
                    deployment_smoke_report=paths["deployment_smoke"],
                    evidence_manifest=paths["manifest"],
                    output_path=Path(temp_dir) / "release-archive.json",
                )
            )

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["platformDifferences"][0]["actual"], "Vault dynamic secrets")
            self.assertEqual(report["evidenceManifest"]["platformDifferenceCount"], 1)

    def test_release_archive_cli_reads_files_and_writes_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _write_inputs(root)
            output_path = root / "release-archive.json"
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--release-gate-report",
                        str(paths["release_gate"]),
                        "--compose-smoke-report",
                        str(paths["compose_smoke"]),
                        "--deployment-smoke-report",
                        str(paths["deployment_smoke"]),
                        "--evidence-manifest",
                        str(paths["manifest"]),
                        "--output",
                        str(output_path),
                    ]
                )

            persisted = json.loads(output_path.read_text(encoding="utf-8"))
            printed = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(persisted["status"], "pass")
            self.assertEqual(printed["kind"], "production_release_archive_report")


def _write_inputs(
    root: Path,
    *,
    missing_service: str | None = None,
    secret_value: str | None = None,
    platform_differences: list[dict[str, object]] | None = None,
) -> dict[str, Path]:
    release_gate = _release_gate_report()
    compose_smoke = {
        "kind": "compose_smoke_report",
        "schemaVersion": "1",
        "status": "pass",
        "deploymentSmoke": {"archive_manifest": {"archiveReady": True}},
        "failedChecks": [],
    }
    deployment_smoke = {
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
    manifest = _evidence_manifest(missing_service=missing_service, secret_value=secret_value)
    if platform_differences:
        manifest["platformDifferences"] = platform_differences

    paths = {
        "release_gate": root / "release-gate.json",
        "compose_smoke": root / "compose-smoke.json",
        "deployment_smoke": root / "deployment-smoke.json",
        "manifest": root / "evidence-manifest.json",
    }
    paths["release_gate"].write_text(json.dumps(release_gate, ensure_ascii=False), encoding="utf-8")
    paths["compose_smoke"].write_text(json.dumps(compose_smoke, ensure_ascii=False), encoding="utf-8")
    paths["deployment_smoke"].write_text(json.dumps(deployment_smoke, ensure_ascii=False), encoding="utf-8")
    paths["manifest"].write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
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
        },
        "ciPlatform": {"name": "github_actions"},
        "images": [
            _image("api"),
            _image("worker"),
            _image("browser-runner"),
            _image("web"),
        ],
        "failedChecks": [],
    }


def _image(service: str) -> dict[str, str]:
    repository = f"ghcr.io/owner/ai-jsunpack/{service}"
    return {
        "service": service,
        "repository": repository,
        "versionTag": f"{repository}:2026.06.26",
    }


def _evidence_manifest(*, missing_service: str | None, secret_value: str | None) -> dict[str, object]:
    registry_digests = [
        {
            "service": service,
            "tag": f"ghcr.io/owner/ai-jsunpack/{service}:2026.06.26",
            "digest": digest,
        }
        for service, digest in DIGESTS.items()
        if service != missing_service
    ]
    secret_manager = {
        "provider": "github_environments",
        "environment": "production",
        "revision": "environment-revision-42",
        "approvalRecord": "https://github.com/owner/ai-jsunpack/actions/runs/123456",
        "containsSecretValues": False,
    }
    if secret_value:
        secret_manager["secret"] = secret_value
    return {
        "kind": "production_release_evidence_manifest",
        "schemaVersion": "1",
        "ciRun": {
            "runUrl": "https://github.com/owner/ai-jsunpack/actions/runs/123456",
            "runId": "123456",
            "commit": "abcdef123456",
            "environment": "production",
            "artifacts": [
                "release-gate-report",
                "release-gate-sbom",
                "release-gate-scans",
                "compose-smoke-report",
                "deployment-smoke-report",
            ],
        },
        "registryDigests": registry_digests,
        "secretManager": secret_manager,
        "databaseSnapshot": {
            "evidenceRef": "s3://release-archive/2026.06.26/db.dump",
            "sha256": "dbsha",
            "containsSecretValues": False,
        },
        "artifactStoreExport": {
            "evidenceRef": "s3://release-archive/2026.06.26/artifacts/",
            "sha256": "artifacts-sha",
            "containsSecretValues": False,
        },
        "serviceLogs": {"evidenceRef": "s3://release-archive/2026.06.26/compose-logs.txt"},
        "rollbackEvidence": {
            "evidenceRef": "ghcr.io/owner/ai-jsunpack/api:2026.06.25",
            "previousVersion": "2026.06.25",
        },
        "platformDifferences": [],
    }


if __name__ == "__main__":
    unittest.main()
