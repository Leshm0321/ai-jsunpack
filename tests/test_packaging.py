import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from apps.api.app.artifact_store import InMemoryObjectStorageClient, S3CompatibleArtifactStore
from apps.api.app.models import CreateJobRequest
from apps.api.app.store import artifacts_table, create_store
from apps.worker.worker.packaging import PackagingRunner


class PackagingRunnerTest(unittest.TestCase):
    def test_completion_decision_uses_latest_attempt_per_validation_group(self):
        decision = PackagingRunner()._completion_decision(
            {
                "buildArtifacts": [],
                "runtimeReports": [
                    {
                        "target": "reconstructed",
                        "attempt": 0,
                        "status": "fail",
                        "failureClass": "runtime_error",
                        "decision": "first runtime compare failed",
                        "comparisonArtifactId": "comparison_0",
                    },
                    {
                        "target": "reconstructed",
                        "attempt": 1,
                        "status": "pass",
                        "failureClass": "none",
                        "decision": "runtime compare passed after repair",
                        "comparisonArtifactId": "comparison_1",
                    },
                ],
                "reviewRuns": [
                    {
                        "reviewType": "runtime_compare",
                        "attempt": 0,
                        "status": "fail",
                        "failureClass": "runtime_error",
                        "decision": "planned runtime repair",
                    },
                    {
                        "reviewType": "runtime_compare",
                        "attempt": 1,
                        "status": "pass",
                        "failureClass": "none",
                        "decision": "runtime compare repair passed",
                    },
                ],
            }
        )

        self.assertEqual(decision["status"], "completed")
        self.assertEqual(decision["failureClass"], "none")
        self.assertEqual(decision["observations"], [])

    def test_evidence_attachment_include_kinds_filter_controls_zip_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(
                    CreateJobRequest(
                        config={
                            "packaging": {
                                "evidenceAttachments": {
                                    "includeKinds": ["runtime_trace"],
                                }
                            }
                        }
                    )
                )
                build_log = store.write_artifact(
                    job.id,
                    kind="build_log",
                    stage="building",
                    filename="build-log.json",
                    content=b'{"status":"pass"}',
                    content_type="application/json",
                    producer="test",
                )
                runtime_trace = store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="runtime_compare",
                    filename="trace.json",
                    content=b'{"trace":true}',
                    content_type="application/json",
                    producer="test",
                )
                runtime_screenshot = store.write_artifact(
                    job.id,
                    kind="runtime_screenshot",
                    stage="runtime_compare",
                    filename="screenshot.png",
                    content=b"\x89PNG\r\n\x1a\nfake",
                    content_type="image/png",
                    producer="test",
                )
                review_run = store.write_artifact(
                    job.id,
                    kind="review_run",
                    stage="reviewing",
                    filename="review.json",
                    content=json.dumps(
                        {
                            "id": "review_test",
                            "jobId": job.id,
                            "attempt": 0,
                            "reviewType": "runtime_compare",
                            "status": "fail",
                            "decision": "runtime compare needs repair",
                            "failureClass": "runtime_error",
                            "evidenceRefs": [],
                            "repairInstructionIds": [],
                            "logsArtifactId": None,
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test",
                )

                result = PackagingRunner().run(job_id=job.id, store=store)
                evidence_index = json.loads(Path(result.evidence_index_artifact.storage_uri).read_text(encoding="utf-8"))
                audit_payload = self._read_zip_json(result.result_package_artifact.storage_uri, "audit.json")
                attachments = {item["artifactId"]: item for item in evidence_index["attachments"]}

                self.assertEqual(evidence_index["includedCount"], 1)
                self.assertEqual(evidence_index["omittedCount"], 2)
                package_contents = {item["path"]: item for item in evidence_index["packageContents"]}
                report_sections = {item["anchor"]: item for item in evidence_index["reportSections"]}
                self.assertEqual(evidence_index["failureSummary"][0]["failureClass"], "runtime_error")
                self.assertEqual(evidence_index["policySummary"]["accessBoundary"]["ownerId"], "local-user")
                self.assertEqual(evidence_index["policySummary"]["accessBoundary"]["projectId"], "default")
                self.assertEqual(evidence_index["policySummary"]["modelPolicy"]["cloudMode"], "local_only")
                self.assertFalse(evidence_index["policySummary"]["modelPolicy"]["cloudContextAllowed"])
                self.assertEqual(audit_payload["policySummary"], evidence_index["policySummary"])
                self.assertTrue(attachments[runtime_trace.id]["included"])
                self.assertFalse(attachments[build_log.id]["included"])
                self.assertFalse(attachments[runtime_screenshot.id]["included"])
                self.assertEqual(
                    attachments[build_log.id]["reason"],
                    "Artifact kind is outside configured includeKinds.",
                )
                self.assertIn("audit-report.md", package_contents)
                self.assertIn("evidence-index.json", package_contents)
                self.assertIn("generated_project/README.md", package_contents)
                self.assertIn(f"evidence/runtime_trace/{runtime_trace.id}.json", package_contents)
                self.assertTrue(package_contents[f"evidence/runtime_trace/{runtime_trace.id}.json"]["included"])
                self.assertFalse(package_contents[f"evidence/build_log/{build_log.id}"]["included"])
                self.assertEqual(package_contents[f"evidence/runtime_trace/{runtime_trace.id}.json"]["artifactId"], runtime_trace.id)
                self.assertIn("risk-and-failure-groups", report_sections)
                self.assertIn("evidence-attachment-index", report_sections)
                self.assertIn(f"artifact://{review_run.id}", report_sections["risk-and-failure-groups"]["evidenceLinks"])
                self.assertIn(f"artifact://{runtime_trace.id}", report_sections["evidence-attachment-index"]["evidenceLinks"])

                with zipfile.ZipFile(result.result_package_artifact.storage_uri) as archive:
                    names = set(archive.namelist())
                self.assertIn(f"evidence/runtime_trace/{runtime_trace.id}.json", names)
                self.assertNotIn(f"evidence/build_log/{build_log.id}.json", names)
                self.assertNotIn(f"evidence/runtime_screenshot/{runtime_screenshot.id}.png", names)
                report_text = Path(result.audit_report_artifact.storage_uri).read_text(encoding="utf-8")
                self.assertIn("## Policy Summary", report_text)
                self.assertIn("Cloud context allowed", report_text)
            finally:
                store.close()

    def test_packaging_reports_remote_browser_runner_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="runtime_smoke",
                    filename="runtime-trace.json",
                    content=json.dumps(
                        {
                            "kind": "runtime_trace",
                            "jobId": job.id,
                            "target": "reconstructed",
                            "status": "pass",
                            "executionBoundary": {
                                "runnerKind": "remote_browser_runner",
                                "enforcement": "remote_isolated",
                                "remoteRunId": "browser_run_test",
                                "auth": "bearer_hmac",
                                "artifactExchange": "worker_request_archive_and_worker_registered_runtime_artifacts",
                                "queueBackend": "postgresql",
                                "queueLength": 3,
                                "runningCount": 2,
                                "terminalCount": 8,
                                "totalCount": 13,
                                "oldestQueuedAgeMs": 5400,
                                "claimLatencyMs": 1200,
                                "averageRunDurationMs": 4500,
                                "retryRate": 0.25,
                                "leaseRecoveryCount": 1,
                                "expiredRunningCount": 1,
                                "backendHealthStatus": "ok",
                                "alerts": [
                                    {
                                        "code": "queue_backlog",
                                        "severity": "warning",
                                        "message": "Browser Runner queued run count exceeds local worker concurrency.",
                                        "field": "queuedCount",
                                        "value": 3,
                                        "threshold": 2,
                                    }
                                ],
                            },
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test.packaging",
                )

                result = PackagingRunner().run(job_id=job.id, store=store)
                report = Path(result.audit_report_artifact.storage_uri).read_text(encoding="utf-8")
                audit_payload = self._read_zip_json(result.result_package_artifact.storage_uri, "audit.json")
                runtime_traces = self._read_zip_json(result.result_package_artifact.storage_uri, "runtime-traces.json")
                evidence_index = json.loads(Path(result.evidence_index_artifact.storage_uri).read_text(encoding="utf-8"))
                report_sections = {item["anchor"]: item for item in evidence_index["reportSections"]}

                self.assertIn("Browser Runner Boundary", report)
                self.assertIn("Browser Runner Operations", report)
                self.assertIn("browser_run_test", report)
                self.assertIn("postgresql", report)
                self.assertIn("runningCount", report)
                self.assertIn("terminalCount", report)
                self.assertIn("totalCount", report)
                self.assertIn("oldestQueuedAgeMs", report)
                self.assertIn("expiredRunningCount", report)
                self.assertIn("queue_backlog", report)
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["runnerKind"], "remote_browser_runner")
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["queueLength"], 3)
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["runningCount"], 2)
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["terminalCount"], 8)
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["totalCount"], 13)
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["oldestQueuedAgeMs"], 5400)
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["expiredRunningCount"], 1)
                self.assertEqual(runtime_traces[0]["executionBoundary"]["remoteRunId"], "browser_run_test")
                self.assertIn("browser-runner-operations", report_sections)
            finally:
                store.close()

    def test_evidence_attachment_policy_records_sensitivity_retention_and_size_omissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(
                    CreateJobRequest(
                        config={
                            "packaging": {
                                "evidenceAttachments": {
                                    "includeKinds": [
                                        "build_log",
                                        "runtime_scenario",
                                        "runtime_screenshot",
                                        "runtime_trace",
                                    ],
                                    "includeSensitivityClasses": ["derived"],
                                    "includeRetentionClasses": ["archive"],
                                    "maxBytesPerArtifact": 4,
                                }
                            }
                        }
                    )
                )
                trace = store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="runtime_compare",
                    filename="trace.json",
                    content=b"ok",
                    content_type="application/json",
                    producer="test",
                )
                screenshot = store.write_artifact(
                    job.id,
                    kind="runtime_screenshot",
                    stage="runtime_compare",
                    filename="screenshot.png",
                    content=b"ok",
                    content_type="image/png",
                    producer="test",
                )
                build_log = store.write_artifact(
                    job.id,
                    kind="build_log",
                    stage="building",
                    filename="build-log.json",
                    content=b"12345",
                    content_type="application/json",
                    producer="test",
                )
                scenario = store.write_artifact(
                    job.id,
                    kind="runtime_scenario",
                    stage="runtime_compare",
                    filename="scenario.json",
                    content=b"ok",
                    content_type="application/json",
                    producer="test",
                )
                self._set_artifact_policy_metadata(store, trace.id, sensitivity_class="source_sensitive", retention_class="archive")
                self._set_artifact_policy_metadata(store, screenshot.id, sensitivity_class="derived", retention_class="ephemeral")
                self._set_artifact_policy_metadata(store, build_log.id, sensitivity_class="derived", retention_class="archive")
                self._set_artifact_policy_metadata(store, scenario.id, sensitivity_class="derived", retention_class="archive")

                result = PackagingRunner().run(job_id=job.id, store=store)
                evidence_index = json.loads(Path(result.evidence_index_artifact.storage_uri).read_text(encoding="utf-8"))
                attachments = {item["artifactId"]: item for item in evidence_index["attachments"]}

                self.assertEqual(evidence_index["includedCount"], 1)
                self.assertEqual(evidence_index["omittedCount"], 3)
                self.assertEqual(
                    attachments[trace.id]["reason"],
                    "Artifact sensitivityClass is outside configured includeSensitivityClasses.",
                )
                self.assertEqual(
                    attachments[screenshot.id]["reason"],
                    "Artifact retentionClass is outside configured includeRetentionClasses.",
                )
                self.assertEqual(attachments[build_log.id]["reason"], "Artifact exceeds configured maxBytesPerArtifact.")
                self.assertTrue(attachments[scenario.id]["included"])
                self.assertEqual(attachments[scenario.id]["sensitivityClass"], "derived")
                self.assertEqual(attachments[scenario.id]["retentionClass"], "archive")
                self.assertEqual(evidence_index["policySummary"]["sensitivityCounts"]["derived"], 3)
                self.assertEqual(evidence_index["policySummary"]["sensitivityCounts"]["source_sensitive"], 1)
                self.assertEqual(evidence_index["policySummary"]["retentionCounts"]["archive"], 3)
                self.assertEqual(evidence_index["policySummary"]["retentionCounts"]["ephemeral"], 1)

                with zipfile.ZipFile(result.result_package_artifact.storage_uri) as archive:
                    names = set(archive.namelist())
                self.assertIn(f"evidence/runtime_scenario/{scenario.id}.json", names)
                self.assertNotIn(f"evidence/runtime_trace/{trace.id}.json", names)
                self.assertNotIn(f"evidence/runtime_screenshot/{screenshot.id}.png", names)
                self.assertNotIn(f"evidence/build_log/{build_log.id}.json", names)
            finally:
                store.close()

    def test_packaging_policy_summary_records_desensitized_context_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(cloud_mode="desensitized"))
                store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="runtime_compare",
                    filename="trace.json",
                    content=b'{"trace":true}',
                    content_type="application/json",
                    producer="test",
                    sensitivity_class="derived",
                )

                result = PackagingRunner().run(job_id=job.id, store=store)
                evidence_index = json.loads(Path(result.evidence_index_artifact.storage_uri).read_text(encoding="utf-8"))
                model_policy = evidence_index["policySummary"]["modelPolicy"]

                self.assertEqual(model_policy["cloudMode"], "desensitized")
                self.assertEqual(model_policy["modelContextScope"], "sanitized_cloud_or_local")
                self.assertEqual(model_policy["contextHandling"], "deterministic_context_redaction")
                self.assertTrue(model_policy["cloudContextAllowed"])
                self.assertIn("deterministic source excerpt", model_policy["limitation"])
            finally:
                store.close()

    def test_evidence_attachment_can_package_non_local_object_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            object_client = InMemoryObjectStorageClient()
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
                artifact_store=S3CompatibleArtifactStore(bucket="artifact-bucket", prefix="packaging", client=object_client),
            )
            try:
                job = store.create_job(
                    CreateJobRequest(
                        config={
                            "packaging": {
                                "evidenceAttachments": {
                                    "includeKinds": ["runtime_trace"],
                                }
                            }
                        }
                    )
                )
                runtime_trace = store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="runtime_compare",
                    filename="trace.json",
                    content=b'{"trace":"object"}',
                    content_type="application/json",
                    producer="test",
                )

                result = PackagingRunner().run(job_id=job.id, store=store)
                evidence_index = json.loads(store.read_artifact_record(result.evidence_index_artifact).decode("utf-8"))
                package_bytes = store.read_artifact_record(result.result_package_artifact)

                self.assertTrue(evidence_index["attachments"][0]["included"])
                with zipfile.ZipFile(self._bytes_zip_path(root, package_bytes)) as archive:
                    self.assertEqual(
                        archive.read(f"evidence/runtime_trace/{runtime_trace.id}.json"),
                        b'{"trace":"object"}',
                    )
            finally:
                store.close()

    def _set_artifact_policy_metadata(self, store, artifact_id: str, *, sensitivity_class: str, retention_class: str) -> None:
        with store.engine.begin() as connection:
            connection.execute(
                artifacts_table.update()
                .where(artifacts_table.c.id == artifact_id)
                .values(sensitivity_class=sensitivity_class, retention_class=retention_class)
            )

    def _read_zip_json(self, package_path: str, name: str):
        with zipfile.ZipFile(package_path) as archive:
            return json.loads(archive.read(name).decode("utf-8"))

    def _bytes_zip_path(self, root: Path, content: bytes) -> Path:
        package_path = root / "package.zip"
        package_path.write_bytes(content)
        return package_path


if __name__ == "__main__":
    unittest.main()
