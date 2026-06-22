import json
import os
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from apps.api.app import main as api_main
from apps.api.app.artifact_store import InMemoryObjectStorageClient, S3CompatibleArtifactStore
from apps.api.app.auth import AUTH_SECRET_ENV, create_auth_token
from apps.api.app.store import create_store


class ApiEndpointTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.auth_secret = "test-auth-secret"
        self.original_auth_secret = os.environ.get(AUTH_SECRET_ENV)
        os.environ[AUTH_SECRET_ENV] = self.auth_secret
        self.access_headers = self.auth_headers("owner", {"proj": "owner"})
        self.viewer_headers = self.auth_headers("viewer", {"proj": "viewer"})
        self.other_access_headers = self.auth_headers("other-owner", {"other-proj": "owner"})
        self.worker_service_headers = self.auth_headers("worker-service", {}, kind="service", service_roles=["worker"])
        self.store = create_store(
            database_url=f"sqlite:///{(self.root / 'metadata.db').as_posix()}",
            artifact_root=self.root / "artifacts",
        )
        self.original_store = api_main.store
        api_main.store = self.store
        self.client = TestClient(api_main.app)

    def tearDown(self):
        api_main.store = self.original_store
        self.store.close()
        if self.original_auth_secret is None:
            os.environ.pop(AUTH_SECRET_ENV, None)
        else:
            os.environ[AUTH_SECRET_ENV] = self.original_auth_secret
        self.temp_dir.cleanup()

    def auth_headers(
        self,
        subject: str,
        projects: dict[str, str],
        *,
        kind: str = "user",
        service_roles: list[str] | None = None,
        ttl_seconds: int = 3600,
        secret: str | None = None,
    ) -> dict[str, str]:
        token = create_auth_token(
            subject=subject,
            projects=projects,
            kind=kind,
            service_roles=service_roles,
            ttl_seconds=ttl_seconds,
            secret=secret or self.auth_secret,
        )
        return {"Authorization": f"Bearer {token}"}

    def test_create_upload_and_get_job_summary(self):
        created = self.client.post(
            "/jobs",
            json={
                "projectId": "proj",
                "ownerId": "owner",
                "cloudMode": "local_only",
                "config": {"source": "api-test"},
            },
            headers=self.access_headers,
        )

        self.assertEqual(created.status_code, 200)
        created_body = created.json()
        job_id = created_body["job"]["id"]
        self.assertEqual(created_body["job"]["status"], "queued")
        self.assertEqual(created_body["job"]["projectId"], "proj")
        self.assertEqual(created_body["artifacts"], [])

        uploaded = self.client.post(
            f"/jobs/{job_id}/upload",
            files={"file": ("dist.zip", b"zip-bytes", "application/zip")},
            headers=self.access_headers,
        )

        self.assertEqual(uploaded.status_code, 200)
        uploaded_body = uploaded.json()
        self.assertEqual(uploaded_body["job"]["status"], "intake")
        self.assertEqual(len(uploaded_body["artifacts"]), 1)
        self.assertEqual(uploaded_body["artifacts"][0]["kind"], "source_input")
        self.assertEqual(uploaded_body["artifacts"][0]["size"], len(b"zip-bytes"))
        self.assertEqual(uploaded_body["job"]["inputArtifactId"], uploaded_body["artifacts"][0]["id"])
        self.assertTrue(Path(uploaded_body["artifacts"][0]["storageUri"]).exists())

        fetched = self.client.get(f"/jobs/{job_id}", headers=self.access_headers)

        self.assertEqual(fetched.status_code, 200)
        fetched_body = fetched.json()
        self.assertEqual(fetched_body["job"]["id"], job_id)
        self.assertEqual(fetched_body["job"]["status"], "intake")
        self.assertEqual(fetched_body["artifacts"][0]["contentType"], "application/zip")

    def test_missing_job_returns_404(self):
        fetched = self.client.get("/jobs/job_missing", headers=self.access_headers)
        uploaded = self.client.post(
            "/jobs/job_missing/upload",
            files={"file": ("dist.zip", b"zip-bytes", "application/zip")},
            headers=self.access_headers,
        )

        self.assertEqual(fetched.status_code, 404)
        self.assertEqual(uploaded.status_code, 404)

    def test_runtime_validation_report_and_artifact_download_endpoints(self):
        created = self.client.post("/jobs", json={"projectId": "proj", "ownerId": "owner"}, headers=self.access_headers)
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]
        payload = {
            "id": "runtime_api_test",
            "jobId": job_id,
            "attempt": 0,
            "target": "reconstructed",
            "entryUrl": "http://127.0.0.1:5173/",
            "status": "pass",
            "consoleErrors": [],
            "pageErrors": [],
            "failedRequests": [],
            "screenshotArtifactIds": [],
            "traceArtifactId": None,
            "comparisonArtifactId": None,
        }
        report_artifact = self.store.write_artifact(
            job_id,
            kind="runtime_validation",
            stage="runtime_smoke",
            filename="runtime-validation.json",
            content=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            producer="test.api",
        )

        listed = self.client.get(f"/jobs/{job_id}/runtime-validations", headers=self.access_headers)
        latest = self.client.get(f"/jobs/{job_id}/runtime-validations/latest", headers=self.access_headers)
        downloaded = self.client.get(f"/jobs/{job_id}/artifacts/{report_artifact.id}/download", headers=self.access_headers)

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(listed.json(), [payload])
        self.assertEqual(latest.json(), payload)
        self.assertEqual(downloaded.json(), payload)

    def test_agent_audit_record_endpoints(self):
        created = self.client.post("/jobs", json={"projectId": "proj", "ownerId": "owner"}, headers=self.access_headers)
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]
        evidence_ref = {
            "artifactId": "artifact_input",
            "label": "Core input inventory",
            "locator": "artifact:input_inventory",
            "excerpt": "entries=['index.html']",
        }
        inference_payload = {
            "id": "inference_api_test",
            "jobId": job_id,
            "type": "module_split",
            "agentName": "AnalysisAgent",
            "modelProvider": "crewai_stub",
            "modelName": "stub-v0",
            "promptVersion": "agent-stub-v1",
            "inputArtifactIds": ["artifact_inventory", "artifact_ast"],
            "outputArtifactIds": ["artifact_plan"],
            "evidenceRefs": [evidence_ref],
            "confidence": 0.35,
            "uncertaintyReasons": ["stub output"],
            "alternatives": ["real provider"],
            "validationStatus": "needs_review",
            "rollbackRef": None,
        }
        review_payload = {
            "id": "review_api_test",
            "jobId": job_id,
            "attempt": 0,
            "reviewType": "agent_review",
            "status": "best_effort",
            "decision": "stub accepted",
            "failureClass": "none",
            "evidenceRefs": [evidence_ref],
            "repairInstructionIds": [],
            "logsArtifactId": None,
        }
        tool_call_payload = {
            "id": "tool_call_api_test",
            "jobId": job_id,
            "caller": "WorkerPipeline",
            "toolName": "crewai_stub.agent_pass",
            "toolVersion": "0.1.0",
            "inputArtifactIds": ["artifact_inventory", "artifact_ast"],
            "outputArtifactIds": ["artifact_plan", "artifact_inference", "artifact_review"],
            "status": "pass",
            "duration": 3.25,
            "failureClass": "none",
        }
        self.store.write_artifact(
            job_id,
            kind="inference_record",
            stage="agent_pass",
            filename="inference-record.json",
            content=json.dumps(inference_payload).encode("utf-8"),
            content_type="application/json",
            producer="test.api",
        )
        self.store.write_artifact(
            job_id,
            kind="review_run",
            stage="agent_pass",
            filename="review-run.json",
            content=json.dumps(review_payload).encode("utf-8"),
            content_type="application/json",
            producer="test.api",
        )
        self.store.write_artifact(
            job_id,
            kind="tool_call",
            stage="agent_pass",
            filename="tool-call.json",
            content=json.dumps(tool_call_payload).encode("utf-8"),
            content_type="application/json",
            producer="test.api",
        )

        inference_response = self.client.get(f"/jobs/{job_id}/inference-records", headers=self.access_headers)
        review_response = self.client.get(f"/jobs/{job_id}/review-runs", headers=self.access_headers)
        tool_call_response = self.client.get(f"/jobs/{job_id}/tool-calls", headers=self.access_headers)
        audit_records_response = self.client.get(f"/jobs/{job_id}/audit-records", headers=self.access_headers)
        audit_review_response = self.client.get(f"/jobs/{job_id}/audit-records?category=review", headers=self.access_headers)
        unsupported_audit_response = self.client.get(
            f"/jobs/{job_id}/audit-records?category=runtime",
            headers=self.access_headers,
        )

        self.assertEqual(inference_response.status_code, 200)
        self.assertEqual(review_response.status_code, 200)
        self.assertEqual(tool_call_response.status_code, 200)
        self.assertEqual(audit_records_response.status_code, 200)
        self.assertEqual(audit_review_response.status_code, 200)
        self.assertEqual(unsupported_audit_response.status_code, 400)
        self.assertEqual(inference_response.json(), [inference_payload])
        self.assertEqual(review_response.json(), [review_payload])
        self.assertEqual(tool_call_response.json(), [tool_call_payload])
        self.assertEqual(
            audit_records_response.json(),
            {
                "jobId": job_id,
                "inferenceRecords": [inference_payload],
                "reviewRuns": [review_payload],
                "toolCalls": [tool_call_payload],
            },
        )
        self.assertEqual(
            audit_review_response.json(),
            {
                "jobId": job_id,
                "inferenceRecords": [],
                "reviewRuns": [review_payload],
                "toolCalls": [],
            },
        )

    def test_report_package_download_and_rerun_endpoints(self):
        created = self.client.post(
            "/jobs",
            json={
                "projectId": "proj",
                "ownerId": "owner",
                "cloudMode": "local_only",
                "config": {"source": "api-test", "nested": {"enabled": True}},
            },
            headers=self.access_headers,
        )
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]
        uploaded = self.client.post(
            f"/jobs/{job_id}/upload",
            files={"file": ("dist.zip", b"zip-bytes", "application/zip")},
            headers=self.access_headers,
        )
        self.assertEqual(uploaded.status_code, 200)
        audit_artifact = self.store.write_artifact(
            job_id,
            kind="audit_report",
            stage="packaging",
            filename="audit-report.md",
            content=b"# Audit\n",
            content_type="text/markdown; charset=utf-8",
            producer="test.api",
        )
        html_artifact = self.store.write_artifact(
            job_id,
            kind="html_report",
            stage="packaging",
            filename="audit-report.html",
            content=b"<!doctype html><title>Audit</title>\n",
            content_type="text/html; charset=utf-8",
            producer="test.api",
            parent_artifact_ids=[audit_artifact.id],
        )
        evidence_index_artifact = self.store.write_artifact(
            job_id,
            kind="evidence_index",
            stage="packaging",
            filename="evidence-index.json",
            content=b'{"kind":"evidence_index","attachments":[]}\n',
            content_type="application/json",
            producer="test.api",
            parent_artifact_ids=[audit_artifact.id],
        )
        package_buffer = BytesIO()
        with zipfile.ZipFile(package_buffer, "w") as archive:
            archive.writestr("audit-report.md", "# Audit\n")
            archive.writestr("audit-report.html", "<!doctype html><title>Audit</title>\n")
            archive.writestr("evidence-index.json", '{"kind":"evidence_index","attachments":[]}\n')
        package_artifact = self.store.write_artifact(
            job_id,
            kind="result_package",
            stage="packaging",
            filename="result-package.zip",
            content=package_buffer.getvalue(),
            content_type="application/zip",
            producer="test.api",
            parent_artifact_ids=[audit_artifact.id],
        )

        audit_download = self.client.get(f"/jobs/{job_id}/reports/audit", headers=self.access_headers)
        package_download = self.client.get(f"/jobs/{job_id}/result-package", headers=self.access_headers)
        html_download = self.client.get(f"/jobs/{job_id}/artifacts/{html_artifact.id}/download", headers=self.access_headers)
        evidence_index_download = self.client.get(
            f"/jobs/{job_id}/artifacts/{evidence_index_artifact.id}/download",
            headers=self.access_headers,
        )
        report_list = self.client.get(f"/jobs/{job_id}/reports", headers=self.access_headers)
        evidence_report_list = self.client.get(
            f"/jobs/{job_id}/reports?kind=evidence-index",
            headers=self.access_headers,
        )
        generic_audit_download = self.client.get(f"/jobs/{job_id}/reports/audit_report", headers=self.access_headers)
        generic_html_download = self.client.get(f"/jobs/{job_id}/reports/html", headers=self.access_headers)
        generic_evidence_index_download = self.client.get(
            f"/jobs/{job_id}/reports/evidence-index",
            headers=self.access_headers,
        )
        unsupported_report_list = self.client.get(
            f"/jobs/{job_id}/reports?kind=result_package",
            headers=self.access_headers,
        )
        unsupported_report_download = self.client.get(
            f"/jobs/{job_id}/reports/result-package",
            headers=self.access_headers,
        )
        rerun = self.client.post(f"/jobs/{job_id}/rerun", headers=self.access_headers)

        self.assertEqual(audit_download.status_code, 200)
        self.assertEqual(package_download.status_code, 200)
        self.assertEqual(html_download.status_code, 200)
        self.assertEqual(evidence_index_download.status_code, 200)
        self.assertEqual(report_list.status_code, 200)
        self.assertEqual(evidence_report_list.status_code, 200)
        self.assertEqual(generic_audit_download.status_code, 200)
        self.assertEqual(generic_html_download.status_code, 200)
        self.assertEqual(generic_evidence_index_download.status_code, 200)
        self.assertEqual(unsupported_report_list.status_code, 400)
        self.assertEqual(unsupported_report_download.status_code, 400)
        self.assertEqual(audit_download.text, "# Audit\n")
        self.assertEqual(generic_audit_download.text, "# Audit\n")
        self.assertEqual(package_download.content, self.store.read_artifact_record(package_artifact))
        self.assertIn("<!doctype html>", html_download.text)
        self.assertIn("<!doctype html>", generic_html_download.text)
        self.assertEqual(evidence_index_download.json()["kind"], "evidence_index")
        self.assertEqual(generic_evidence_index_download.json()["kind"], "evidence_index")
        self.assertEqual(
            [artifact["kind"] for artifact in report_list.json()],
            ["audit_report", "html_report", "evidence_index"],
        )
        self.assertEqual([artifact["id"] for artifact in evidence_report_list.json()], [evidence_index_artifact.id])
        self.assertEqual(rerun.status_code, 200)
        rerun_body = rerun.json()
        rerun_job = rerun_body["job"]
        rerun_artifact = rerun_body["artifacts"][0]
        self.assertNotEqual(rerun_job["id"], job_id)
        self.assertEqual(rerun_job["status"], "intake")
        self.assertEqual(rerun_job["projectId"], "proj")
        self.assertEqual(rerun_job["config"]["rerunOfJobId"], job_id)
        self.assertEqual(rerun_job["config"]["nested"], {"enabled": True})
        self.assertEqual(rerun_artifact["kind"], "source_input")
        self.assertEqual(Path(rerun_artifact["storageUri"]).read_bytes(), b"zip-bytes")

    def test_artifact_download_streams_non_local_object_artifacts(self):
        object_client = InMemoryObjectStorageClient()
        object_store = S3CompatibleArtifactStore(bucket="artifact-bucket", prefix="api", client=object_client)
        object_backed_store = create_store(
            database_url=f"sqlite:///{(self.root / 'object-metadata.db').as_posix()}",
            artifact_root=self.root / "object-artifacts",
            artifact_store=object_store,
        )
        self.store.close()
        self.store = object_backed_store
        api_main.store = self.store

        created = self.client.post("/jobs", json={"projectId": "proj", "ownerId": "owner"}, headers=self.access_headers)
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]
        artifact = self.store.write_artifact(
            job_id,
            kind="audit_report",
            stage="packaging",
            filename="audit-report.md",
            content=b"# Object Audit\n",
            content_type="text/markdown; charset=utf-8",
            producer="test.api",
        )

        downloaded = self.client.get(f"/jobs/{job_id}/artifacts/{artifact.id}/download", headers=self.access_headers)

        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, b"# Object Audit\n")
        self.assertEqual(downloaded.headers["content-type"], "text/markdown; charset=utf-8")

    def test_retention_cleanup_endpoint_previews_and_deletes_artifacts(self):
        created = self.client.post("/jobs", json={"projectId": "proj", "ownerId": "owner"}, headers=self.access_headers)
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]
        artifact = self.store.write_artifact(
            job_id,
            kind="build_log",
            stage="building",
            filename="build.log",
            content=b"build log",
            content_type="text/plain",
            producer="test.api",
        )

        preview = self.client.post(
            f"/jobs/{job_id}/retention/cleanup",
            json={
                "dryRun": True,
                "categories": ["logs"],
                "retentionClasses": [],
                "deleteExpired": False,
                "reason": "preview logs",
            },
            headers=self.access_headers,
        )
        still_downloadable = self.client.get(
            f"/jobs/{job_id}/artifacts/{artifact.id}/download",
            headers=self.access_headers,
        )
        deleted = self.client.post(
            f"/jobs/{job_id}/retention/cleanup",
            json={
                "dryRun": False,
                "categories": ["logs"],
                "retentionClasses": [],
                "deleteExpired": False,
                "reason": "delete logs",
            },
            headers=self.access_headers,
        )
        missing_download = self.client.get(
            f"/jobs/{job_id}/artifacts/{artifact.id}/download",
            headers=self.access_headers,
        )
        fetched = self.client.get(f"/jobs/{job_id}", headers=self.access_headers)

        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["candidateCount"], 1)
        self.assertEqual(preview.json()["deletedCount"], 0)
        self.assertEqual(still_downloadable.status_code, 200)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["candidateCount"], 1)
        self.assertEqual(deleted.json()["deletedCount"], 1)
        self.assertEqual(missing_download.status_code, 404)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["artifacts"], [])

    def test_missing_runtime_validation_and_artifact_download_return_404(self):
        created = self.client.post("/jobs", json={"projectId": "proj", "ownerId": "owner"}, headers=self.access_headers)
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]

        latest = self.client.get(f"/jobs/{job_id}/runtime-validations/latest", headers=self.access_headers)
        downloaded = self.client.get(f"/jobs/{job_id}/artifacts/artifact_missing/download", headers=self.access_headers)
        missing_inferences = self.client.get("/jobs/job_missing/inference-records", headers=self.access_headers)
        missing_reviews = self.client.get("/jobs/job_missing/review-runs", headers=self.access_headers)
        missing_tool_calls = self.client.get("/jobs/job_missing/tool-calls", headers=self.access_headers)
        missing_audit_records = self.client.get("/jobs/job_missing/audit-records", headers=self.access_headers)
        missing_reports = self.client.get("/jobs/job_missing/reports", headers=self.access_headers)
        missing_audit = self.client.get("/jobs/job_missing/reports/audit", headers=self.access_headers)
        missing_evidence_index = self.client.get("/jobs/job_missing/reports/evidence-index", headers=self.access_headers)
        missing_package = self.client.get("/jobs/job_missing/result-package", headers=self.access_headers)
        missing_rerun = self.client.post("/jobs/job_missing/rerun", headers=self.access_headers)
        missing_cleanup = self.client.post(
            "/jobs/job_missing/retention/cleanup",
            json={"dryRun": True, "categories": [], "retentionClasses": [], "deleteExpired": True, "reason": "test"},
            headers=self.access_headers,
        )

        self.assertEqual(latest.status_code, 404)
        self.assertEqual(downloaded.status_code, 404)
        self.assertEqual(missing_inferences.status_code, 404)
        self.assertEqual(missing_reviews.status_code, 404)
        self.assertEqual(missing_tool_calls.status_code, 404)
        self.assertEqual(missing_audit_records.status_code, 404)
        self.assertEqual(missing_reports.status_code, 404)
        self.assertEqual(missing_audit.status_code, 404)
        self.assertEqual(missing_evidence_index.status_code, 404)
        self.assertEqual(missing_package.status_code, 404)
        self.assertEqual(missing_rerun.status_code, 404)
        self.assertEqual(missing_cleanup.status_code, 404)

    def test_rerun_without_source_input_returns_400(self):
        created = self.client.post("/jobs", json={"projectId": "proj", "ownerId": "owner"}, headers=self.access_headers)
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]

        rerun = self.client.post(f"/jobs/{job_id}/rerun", headers=self.access_headers)

        self.assertEqual(rerun.status_code, 400)

    def test_directory_artifact_download_returns_400_until_packaged(self):
        created = self.client.post("/jobs", json={"projectId": "proj", "ownerId": "owner"}, headers=self.access_headers)
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]
        generated = self.root / "generated"
        generated.mkdir()
        (generated / "index.html").write_text("<h1>Generated</h1>", encoding="utf-8")
        artifact = self.store.register_artifact_path(
            job_id,
            kind="generated_project",
            stage="reconstructing",
            filename="generated-project",
            source_path=generated,
            content_type="application/vnd.ai-jsunpack.generated-project+directory",
            producer="test.api",
        )

        downloaded = self.client.get(f"/jobs/{job_id}/artifacts/{artifact.id}/download", headers=self.access_headers)

        self.assertEqual(downloaded.status_code, 400)

    def test_maintainer_can_cancel_non_terminal_job(self):
        created = self.client.post("/jobs", json={"projectId": "proj", "ownerId": "owner"}, headers=self.access_headers)
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]
        self.store.write_artifact(
            job_id,
            kind="source_input",
            stage="intake",
            filename="dist.zip",
            content=b"zip-bytes",
            content_type="application/zip",
            producer="test.api",
        )
        leased = self.store.lease_next_job(worker_id="worker-api-test")
        self.assertIsNotNone(leased)

        cancelled = self.client.post(
            f"/jobs/{job_id}/cancel",
            json={"reason": "operator stopped job"},
            headers=self.access_headers,
        )

        self.assertEqual(cancelled.status_code, 200)
        body = cancelled.json()
        self.assertEqual(body["job"]["status"], "cancelled")
        self.assertEqual(body["job"]["failureReason"], "operator stopped job")
        self.assertIsNone(body["job"]["workerLease"])
        self.assertIsNone(self.store.lease_next_job(worker_id="worker-after-cancel"))

    def test_local_vite_origin_is_allowed_for_development(self):
        response = self.client.options(
            "/jobs",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://127.0.0.1:5173")

    def test_job_access_requires_project_membership_and_owner_matches_user_create(self):
        created = self.client.post("/jobs", json={"projectId": "proj", "ownerId": "owner"}, headers=self.access_headers)
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]
        artifact = self.store.write_artifact(
            job_id,
            kind="audit_report",
            stage="packaging",
            filename="audit-report.md",
            content=b"# Audit\n",
            content_type="text/markdown; charset=utf-8",
            producer="test.api",
        )

        create_mismatched_owner = self.client.post(
            "/jobs",
            json={"projectId": "proj", "ownerId": "owner"},
            headers=self.auth_headers("other-owner", {"proj": "owner"}),
        )
        fetched = self.client.get(f"/jobs/{job_id}", headers=self.other_access_headers)
        uploaded = self.client.post(
            f"/jobs/{job_id}/upload",
            files={"file": ("dist.zip", b"zip-bytes", "application/zip")},
            headers=self.other_access_headers,
        )
        downloaded = self.client.get(
            f"/jobs/{job_id}/artifacts/{artifact.id}/download",
            headers=self.other_access_headers,
        )
        audit_download = self.client.get(f"/jobs/{job_id}/reports/audit", headers=self.other_access_headers)
        rerun = self.client.post(f"/jobs/{job_id}/rerun", headers=self.other_access_headers)
        cancel = self.client.post(f"/jobs/{job_id}/cancel", headers=self.other_access_headers)
        inferences = self.client.get(f"/jobs/{job_id}/inference-records", headers=self.other_access_headers)
        reviews = self.client.get(f"/jobs/{job_id}/review-runs", headers=self.other_access_headers)
        tools = self.client.get(f"/jobs/{job_id}/tool-calls", headers=self.other_access_headers)
        audit_records = self.client.get(f"/jobs/{job_id}/audit-records", headers=self.other_access_headers)
        runtime = self.client.get(f"/jobs/{job_id}/runtime-validations", headers=self.other_access_headers)
        reports = self.client.get(f"/jobs/{job_id}/reports", headers=self.other_access_headers)
        report = self.client.get(f"/jobs/{job_id}/reports/audit_report", headers=self.other_access_headers)
        cleanup = self.client.post(
            f"/jobs/{job_id}/retention/cleanup",
            json={"dryRun": True, "categories": [], "retentionClasses": [], "deleteExpired": True, "reason": "test"},
            headers=self.other_access_headers,
        )

        self.assertEqual(create_mismatched_owner.status_code, 403)
        self.assertEqual(fetched.status_code, 403)
        self.assertEqual(uploaded.status_code, 403)
        self.assertEqual(downloaded.status_code, 403)
        self.assertEqual(audit_download.status_code, 403)
        self.assertEqual(rerun.status_code, 403)
        self.assertEqual(cancel.status_code, 403)
        self.assertEqual(inferences.status_code, 403)
        self.assertEqual(reviews.status_code, 403)
        self.assertEqual(tools.status_code, 403)
        self.assertEqual(audit_records.status_code, 403)
        self.assertEqual(runtime.status_code, 403)
        self.assertEqual(reports.status_code, 403)
        self.assertEqual(report.status_code, 403)
        self.assertEqual(cleanup.status_code, 403)

    def test_authentication_rejects_missing_bad_and_expired_tokens(self):
        missing = self.client.get("/jobs/job_missing")
        bad_signature = self.client.get(
            "/jobs/job_missing",
            headers=self.auth_headers("owner", {"proj": "owner"}, secret="wrong-secret"),
        )
        expired = self.client.get(
            "/jobs/job_missing",
            headers=self.auth_headers("owner", {"proj": "owner"}, ttl_seconds=-60),
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(bad_signature.status_code, 401)
        self.assertEqual(expired.status_code, 401)

    def test_viewer_can_read_but_cannot_write_job_resources(self):
        created = self.client.post(
            "/jobs",
            json={"projectId": "proj", "ownerId": "owner"},
            headers=self.access_headers,
        )
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]

        fetched = self.client.get(f"/jobs/{job_id}", headers=self.viewer_headers)
        uploaded = self.client.post(
            f"/jobs/{job_id}/upload",
            files={"file": ("dist.zip", b"zip-bytes", "application/zip")},
            headers=self.viewer_headers,
        )
        rerun = self.client.post(f"/jobs/{job_id}/rerun", headers=self.viewer_headers)
        cancel = self.client.post(f"/jobs/{job_id}/cancel", headers=self.viewer_headers)
        cleanup = self.client.post(
            f"/jobs/{job_id}/retention/cleanup",
            json={"dryRun": True, "categories": [], "retentionClasses": [], "deleteExpired": True, "reason": "test"},
            headers=self.viewer_headers,
        )

        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(uploaded.status_code, 403)
        self.assertEqual(rerun.status_code, 403)
        self.assertEqual(cancel.status_code, 403)
        self.assertEqual(cleanup.status_code, 403)

    def test_service_token_requires_service_role_for_project_access(self):
        service_without_role = self.auth_headers(
            "worker-service",
            {"proj": "maintainer"},
            kind="service",
        )
        service_with_role = self.auth_headers(
            "worker-service",
            {"proj": "maintainer"},
            kind="service",
            service_roles=["worker"],
        )

        rejected = self.client.post(
            "/jobs",
            json={"projectId": "proj", "ownerId": "owner"},
            headers=service_without_role,
        )
        created = self.client.post(
            "/jobs",
            json={"projectId": "proj", "ownerId": "owner"},
            headers=service_with_role,
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(created.status_code, 200)

        job_id = created.json()["job"]["id"]
        uploaded = self.client.post(
            f"/jobs/{job_id}/upload",
            files={"file": ("dist.zip", b"zip-bytes", "application/zip")},
            headers=service_with_role,
        )

        self.assertEqual(uploaded.status_code, 200)

    def test_ops_endpoints_record_heartbeats_and_deliver_alerts(self):
        webhook_response = MagicMock()
        webhook_response.__enter__.return_value = webhook_response
        webhook_response.__exit__.return_value = False
        webhook_response.read.return_value = b"{}"

        with patch.dict(
            os.environ,
            {
                "AI_JSUNPACK_ALERT_WEBHOOK_URL": "https://ops.example/webhook",
                "AI_JSUNPACK_ALERT_WEBHOOK_TIMEOUT_SECONDS": "1",
            },
            clear=False,
        ), patch("apps.api.app.main.urlopen", return_value=webhook_response) as mocked_urlopen:
            heartbeat = self.client.post(
                "/ops/heartbeats",
                json={
                    "serviceRole": "worker",
                    "instanceId": "worker-service-a",
                    "status": "degraded",
                    "ttlSeconds": 30,
                    "metrics": {"phase": "running", "jobCount": 1},
                    "alerts": [
                        {
                            "code": "worker_overloaded",
                            "severity": "warning",
                            "message": "Worker is approaching capacity.",
                            "field": "jobCount",
                            "value": 1,
                            "threshold": 0,
                        }
                    ],
                    "metadata": {"queue": "shared"},
                },
                headers=self.worker_service_headers,
            )
            metrics = self.client.get("/ops/metrics", headers=self.access_headers)
            heartbeats = self.client.get("/ops/heartbeats?service_role=worker", headers=self.access_headers)
            alerts = self.client.get("/ops/alerts", headers=self.access_headers)

        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(metrics.status_code, 200)
        self.assertEqual(heartbeats.status_code, 200)
        self.assertEqual(alerts.status_code, 200)
        self.assertEqual(heartbeat.json()["serviceRole"], "worker")
        self.assertGreaterEqual(metrics.json()["activeHeartbeatCount"], 2)
        self.assertEqual(metrics.json()["serviceHeartbeatCounts"]["worker"], 1)
        self.assertIn("api", metrics.json()["serviceHeartbeatCounts"])
        self.assertEqual(heartbeats.json()[0]["status"], "degraded")
        self.assertEqual(alerts.json()["delivery"]["status"], "delivered")
        self.assertTrue(alerts.json()["delivery"]["attempted"])
        self.assertTrue(alerts.json()["delivery"]["webhookUrlConfigured"])
        self.assertTrue(mocked_urlopen.called)


if __name__ == "__main__":
    unittest.main()
