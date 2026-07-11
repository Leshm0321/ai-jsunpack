import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.app import main as api_main
from apps.api.app.auth import AUTH_SECRET_ENV, create_auth_token
from apps.api.app.store import create_store


class SettingsApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.secret = "settings-api-test-secret"
        self.original_secret = os.environ.get(AUTH_SECRET_ENV)
        os.environ[AUTH_SECRET_ENV] = self.secret
        self.store = create_store(
            database_url=f"sqlite:///{(self.root / 'metadata.db').as_posix()}",
            artifact_root=self.root / "artifacts",
        )
        self.original_store = api_main.store
        api_main.store = self.store
        self.client = TestClient(api_main.app)
        self.owner_headers = self.auth_headers("owner", {"proj": "owner"})
        self.viewer_headers = self.auth_headers("viewer", {"proj": "viewer"})

    def tearDown(self):
        api_main.store = self.original_store
        self.store.close()
        if self.original_secret is None:
            os.environ.pop(AUTH_SECRET_ENV, None)
        else:
            os.environ[AUTH_SECRET_ENV] = self.original_secret
        self.temp_dir.cleanup()

    def auth_headers(self, subject: str, projects: dict[str, str]) -> dict[str, str]:
        token = create_auth_token(subject=subject, projects=projects, secret=self.secret)
        return {"Authorization": f"Bearer {token}"}

    def test_system_and_project_settings_revision_merge_into_new_job(self):
        system = self.client.put(
            "/v1/settings/system",
            json={
                "expectedRevision": 0,
                "reason": "system defaults",
                "settings": {
                    "ai": {"cloud": {"model": "system-model", "apiKeySecretRef": "ai/cloud"}},
                    "agents": {"maxParallel": 3},
                },
            },
            headers=self.owner_headers,
        )
        project = self.client.put(
            "/v1/projects/proj/settings",
            json={
                "expectedRevision": 0,
                "reason": "project validation",
                "settings": {"validation": {"minimumConfidence": 0.85}},
            },
            headers=self.owner_headers,
        )
        created = self.client.post(
            "/jobs",
            json={
                "projectId": "proj",
                "ownerId": "owner",
                "config": {"source": "legacy-compatible", "agents": {"maxParallel": 2}},
            },
            headers=self.owner_headers,
        )

        self.assertEqual(system.status_code, 200, system.text)
        self.assertEqual(project.status_code, 200, project.text)
        self.assertEqual(created.status_code, 200, created.text)
        config = created.json()["job"]["config"]
        self.assertEqual(config["source"], "legacy-compatible")
        self.assertEqual(config["ai"]["cloud"]["model"], "system-model")
        self.assertEqual(config["agents"]["maxParallel"], 2)
        self.assertEqual(config["validation"]["minimumConfidence"], 0.85)

        effective = self.client.get("/v1/projects/proj/settings/effective", headers=self.viewer_headers)
        self.assertEqual(effective.status_code, 200)
        self.assertEqual(effective.json()["systemRevision"], 1)
        self.assertEqual(effective.json()["projectRevision"], 1)

    def test_owner_permissions_conflict_and_rollback_preserve_history(self):
        denied = self.client.put(
            "/v1/settings/system",
            json={"settings": {}, "expectedRevision": 0},
            headers=self.viewer_headers,
        )
        first = self.client.put(
            "/v1/settings/system",
            json={"settings": {"agents": {"maxParallel": 2}}, "expectedRevision": 0},
            headers=self.owner_headers,
        )
        conflict = self.client.put(
            "/v1/settings/system",
            json={"settings": {"agents": {"maxParallel": 4}}, "expectedRevision": 0},
            headers=self.owner_headers,
        )
        second = self.client.put(
            "/v1/settings/system",
            json={"settings": {"agents": {"maxParallel": 4}}, "expectedRevision": 1},
            headers=self.owner_headers,
        )
        rolled_back = self.client.post(
            "/v1/settings/system/rollback",
            json={"revision": 1, "expectedRevision": 2, "reason": "restore revision one"},
            headers=self.owner_headers,
        )
        revisions = self.client.get("/v1/settings/system/revisions", headers=self.owner_headers)

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(rolled_back.status_code, 200)
        self.assertEqual(rolled_back.json()["revision"], 3)
        self.assertEqual(rolled_back.json()["settings"]["agents"]["maxParallel"], 2)
        self.assertEqual([item["revision"] for item in revisions.json()], [3, 2, 1])

    def test_config_schema_effective_config_and_readiness_do_not_return_secrets(self):
        config_path = self.root / "app.yaml"
        config_path.write_text(
            "version: 1\nworker:\n  agent:\n    cloud:\n"
            "      model: cloud-model\n      baseUrl: https://ai.example/v1\n"
            "      apiKeySecretRef: ai/cloud\n",
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"AI_JSUNPACK_CONFIG_FILE": str(config_path), "AI_JSUNPACK_AGENT_API_KEY": "must-not-leak"},
            clear=False,
        ):
            effective = self.client.get("/v1/config/effective", headers=self.owner_headers)
            schema = self.client.get("/v1/config/schema", headers=self.owner_headers)
            readiness = self.client.get("/v1/providers/readiness", headers=self.owner_headers)

        self.assertEqual(effective.status_code, 200, effective.text)
        self.assertEqual(schema.status_code, 200, schema.text)
        self.assertEqual(readiness.status_code, 200, readiness.text)
        serialized = json.dumps([effective.json(), schema.json(), readiness.json()])
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn(str(config_path.resolve()), serialized)
        cloud = readiness.json()[0]
        self.assertEqual(cloud["status"], "ready")
        self.assertTrue(cloud["credentialConfigured"])
        self.assertEqual(cloud["endpointType"], "remote_https")


if __name__ == "__main__":
    unittest.main()
