import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.app import main as api_main
from apps.api.app.artifact_store import InMemoryObjectStorageClient, S3CompatibleArtifactStore
from apps.api.app.store import create_store


class ApiEndpointTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.access_headers = {
            "X-AI-JSUNPACK-USER-ID": "owner",
            "X-AI-JSUNPACK-PROJECT-ID": "proj",
        }
        self.other_access_headers = {
            "X-AI-JSUNPACK-USER-ID": "other-owner",
            "X-AI-JSUNPACK-PROJECT-ID": "proj",
        }
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
        self.temp_dir.cleanup()

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
        fetched = self.client.get("/jobs/job_missing")
        uploaded = self.client.post(
            "/jobs/job_missing/upload",
            files={"file": ("dist.zip", b"zip-bytes", "application/zip")},
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

        self.assertEqual(inference_response.status_code, 200)
        self.assertEqual(review_response.status_code, 200)
        self.assertEqual(tool_call_response.status_code, 200)
        self.assertEqual(inference_response.json(), [inference_payload])
        self.assertEqual(review_response.json(), [review_payload])
        self.assertEqual(tool_call_response.json(), [tool_call_payload])

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
        rerun = self.client.post(f"/jobs/{job_id}/rerun", headers=self.access_headers)

        self.assertEqual(audit_download.status_code, 200)
        self.assertEqual(package_download.status_code, 200)
        self.assertEqual(html_download.status_code, 200)
        self.assertEqual(evidence_index_download.status_code, 200)
        self.assertEqual(audit_download.text, "# Audit\n")
        self.assertEqual(package_download.content, self.store.read_artifact_record(package_artifact))
        self.assertIn("<!doctype html>", html_download.text)
        self.assertEqual(evidence_index_download.json()["kind"], "evidence_index")
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
        missing_inferences = self.client.get("/jobs/job_missing/inference-records")
        missing_reviews = self.client.get("/jobs/job_missing/review-runs")
        missing_tool_calls = self.client.get("/jobs/job_missing/tool-calls")
        missing_audit = self.client.get("/jobs/job_missing/reports/audit")
        missing_package = self.client.get("/jobs/job_missing/result-package")
        missing_rerun = self.client.post("/jobs/job_missing/rerun")
        missing_cleanup = self.client.post(
            "/jobs/job_missing/retention/cleanup",
            json={"dryRun": True, "categories": [], "retentionClasses": [], "deleteExpired": True, "reason": "test"},
        )

        self.assertEqual(latest.status_code, 404)
        self.assertEqual(downloaded.status_code, 404)
        self.assertEqual(missing_inferences.status_code, 404)
        self.assertEqual(missing_reviews.status_code, 404)
        self.assertEqual(missing_tool_calls.status_code, 404)
        self.assertEqual(missing_audit.status_code, 404)
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

    def test_job_access_requires_matching_owner_and_project_headers(self):
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
            headers=self.other_access_headers,
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
        inferences = self.client.get(f"/jobs/{job_id}/inference-records", headers=self.other_access_headers)
        reviews = self.client.get(f"/jobs/{job_id}/review-runs", headers=self.other_access_headers)
        tools = self.client.get(f"/jobs/{job_id}/tool-calls", headers=self.other_access_headers)
        runtime = self.client.get(f"/jobs/{job_id}/runtime-validations", headers=self.other_access_headers)
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
        self.assertEqual(inferences.status_code, 403)
        self.assertEqual(reviews.status_code, 403)
        self.assertEqual(tools.status_code, 403)
        self.assertEqual(runtime.status_code, 403)
        self.assertEqual(cleanup.status_code, 403)

    def test_create_allows_missing_project_header_but_read_uses_project_boundary(self):
        created = self.client.post(
            "/jobs",
            json={"projectId": "proj", "ownerId": "owner"},
            headers={"X-AI-JSUNPACK-USER-ID": "owner"},
        )
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]

        missing_project = self.client.get(f"/jobs/{job_id}", headers={"X-AI-JSUNPACK-USER-ID": "owner"})
        matching_project = self.client.get(f"/jobs/{job_id}", headers=self.access_headers)

        self.assertEqual(missing_project.status_code, 403)
        self.assertEqual(matching_project.status_code, 200)


if __name__ == "__main__":
    unittest.main()
