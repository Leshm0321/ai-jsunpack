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
