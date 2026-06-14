import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.app import main as api_main
from apps.api.app.store import create_store


class ApiEndpointTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
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
        )

        self.assertEqual(uploaded.status_code, 200)
        uploaded_body = uploaded.json()
        self.assertEqual(uploaded_body["job"]["status"], "intake")
        self.assertEqual(len(uploaded_body["artifacts"]), 1)
        self.assertEqual(uploaded_body["artifacts"][0]["kind"], "source_input")
        self.assertEqual(uploaded_body["artifacts"][0]["size"], len(b"zip-bytes"))
        self.assertEqual(uploaded_body["job"]["inputArtifactId"], uploaded_body["artifacts"][0]["id"])
        self.assertTrue(Path(uploaded_body["artifacts"][0]["storageUri"]).exists())

        fetched = self.client.get(f"/jobs/{job_id}")

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
        created = self.client.post("/jobs", json={"projectId": "proj", "ownerId": "owner"})
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

        listed = self.client.get(f"/jobs/{job_id}/runtime-validations")
        latest = self.client.get(f"/jobs/{job_id}/runtime-validations/latest")
        downloaded = self.client.get(f"/jobs/{job_id}/artifacts/{report_artifact.id}/download")

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(listed.json(), [payload])
        self.assertEqual(latest.json(), payload)
        self.assertEqual(downloaded.json(), payload)

    def test_agent_audit_record_endpoints(self):
        created = self.client.post("/jobs", json={"projectId": "proj", "ownerId": "owner"})
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

        inference_response = self.client.get(f"/jobs/{job_id}/inference-records")
        review_response = self.client.get(f"/jobs/{job_id}/review-runs")
        tool_call_response = self.client.get(f"/jobs/{job_id}/tool-calls")

        self.assertEqual(inference_response.status_code, 200)
        self.assertEqual(review_response.status_code, 200)
        self.assertEqual(tool_call_response.status_code, 200)
        self.assertEqual(inference_response.json(), [inference_payload])
        self.assertEqual(review_response.json(), [review_payload])
        self.assertEqual(tool_call_response.json(), [tool_call_payload])

    def test_missing_runtime_validation_and_artifact_download_return_404(self):
        created = self.client.post("/jobs", json={"projectId": "proj", "ownerId": "owner"})
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job"]["id"]

        latest = self.client.get(f"/jobs/{job_id}/runtime-validations/latest")
        downloaded = self.client.get(f"/jobs/{job_id}/artifacts/artifact_missing/download")
        missing_inferences = self.client.get("/jobs/job_missing/inference-records")
        missing_reviews = self.client.get("/jobs/job_missing/review-runs")
        missing_tool_calls = self.client.get("/jobs/job_missing/tool-calls")

        self.assertEqual(latest.status_code, 404)
        self.assertEqual(downloaded.status_code, 404)
        self.assertEqual(missing_inferences.status_code, 404)
        self.assertEqual(missing_reviews.status_code, 404)
        self.assertEqual(missing_tool_calls.status_code, 404)

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


if __name__ == "__main__":
    unittest.main()
