import tempfile
import unittest
from pathlib import Path

from apps.api.app.models import CreateJobRequest
from apps.api.app.store import create_store


class DatabaseStoreTest(unittest.TestCase):
    def test_database_store_persists_jobs_and_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_url = f"sqlite:///{(root / 'metadata.db').as_posix()}"
            artifact_root = root / "artifacts"

            store = create_store(database_url=database_url, artifact_root=artifact_root)
            reopened = None
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                updated = store.update_status(job.id, "intake")
                artifact = store.write_artifact(
                    job.id,
                    kind="input_inventory",
                    stage="intake",
                    filename="../input.json",
                    content=b'{"ok":true}',
                    content_type="application/json",
                    producer="test.database_store",
                )

                self.assertEqual(updated.status, "intake")
                self.assertTrue(Path(artifact.storage_uri).exists())
                self.assertEqual(Path(artifact.storage_uri).read_bytes(), b'{"ok":true}')
                self.assertEqual(Path(artifact.storage_uri).parent, artifact_root / job.id)
                self.assertTrue(Path(artifact.storage_uri).name.endswith("-input.json"))

                reopened = create_store(database_url=database_url, artifact_root=artifact_root)
                persisted_job = reopened.get_job(job.id)
                persisted_artifacts = reopened.list_artifacts(job.id)

                self.assertIsNotNone(persisted_job)
                self.assertEqual(persisted_job.input_artifact_id, artifact.id)
                self.assertEqual(persisted_job.status, "intake")
                self.assertEqual(len(persisted_artifacts), 1)
                self.assertEqual(persisted_artifacts[0].hash, artifact.hash)
            finally:
                store.close()
                if reopened is not None:
                    reopened.close()


if __name__ == "__main__":
    unittest.main()
