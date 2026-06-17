import tempfile
import unittest
from pathlib import Path

from apps.api.app.artifact_store import InMemoryObjectStorageClient, S3CompatibleArtifactStore
from apps.api.app.models import CreateJobRequest, RetentionCleanupRequest
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
                    kind="source_input",
                    stage="intake",
                    filename="../input.json",
                    content=b'{"ok":true}',
                    content_type="application/json",
                    producer="test.database_store",
                    sensitivity_class="secret",
                    retention_class="archive",
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
                self.assertEqual(persisted_artifacts[0].sensitivity_class, "secret")
                self.assertEqual(persisted_artifacts[0].retention_class, "archive")
            finally:
                store.close()
                if reopened is not None:
                    reopened.close()

    def test_register_artifact_path_persists_directory_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_url = f"sqlite:///{(root / 'metadata.db').as_posix()}"
            artifact_root = root / "artifacts"
            source_dir = root / "generated"
            (source_dir / "src").mkdir(parents=True)
            (source_dir / "index.html").write_text("<h1>Generated</h1>", encoding="utf-8")
            (source_dir / "src" / "main.ts").write_text("export const ok = true;", encoding="utf-8")

            store = create_store(database_url=database_url, artifact_root=artifact_root)
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                artifact = store.register_artifact_path(
                    job.id,
                    kind="generated_project",
                    stage="reconstructing",
                    filename="generated-project",
                    source_path=source_dir,
                    content_type="application/vnd.ai-jsunpack.generated-project+directory",
                    producer="test.database_store",
                    sensitivity_class="derived",
                    retention_class="archive",
                )
                repeated = store.register_artifact_path(
                    job.id,
                    kind="generated_project",
                    stage="reconstructing",
                    filename="generated-project-copy",
                    source_path=source_dir,
                    content_type="application/vnd.ai-jsunpack.generated-project+directory",
                    producer="test.database_store",
                )

                self.assertTrue(Path(artifact.storage_uri).is_dir())
                self.assertTrue((Path(artifact.storage_uri) / "src" / "main.ts").exists())
                self.assertEqual(artifact.hash, repeated.hash)
                self.assertEqual(artifact.size, len("<h1>Generated</h1>".encode("utf-8")) + len("export const ok = true;".encode("utf-8")))
                self.assertEqual(artifact.sensitivity_class, "derived")
                self.assertEqual(artifact.retention_class, "archive")
            finally:
                store.close()

    def test_database_store_can_delegate_bytes_to_s3_compatible_artifact_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_url = f"sqlite:///{(root / 'metadata.db').as_posix()}"
            object_client = InMemoryObjectStorageClient()
            artifact_store = S3CompatibleArtifactStore(bucket="artifact-bucket", prefix="team", client=object_client)

            store = create_store(
                database_url=database_url,
                artifact_root=root / "artifacts",
                artifact_store=artifact_store,
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                artifact = store.write_artifact(
                    job.id,
                    kind="source_input",
                    stage="intake",
                    filename="input.json",
                    content=b'{"object":true}',
                    content_type="application/json",
                    producer="test.database_store",
                )

                self.assertTrue(artifact.storage_uri.startswith("s3://artifact-bucket/team/"))
                self.assertEqual(store.read_artifact(job.id, artifact.id), b'{"object":true}')
                self.assertTrue(store.artifact_exists(artifact))
                self.assertTrue(store.artifact_is_file(artifact))
                self.assertIsNone(store.artifact_local_path(artifact))
                self.assertEqual(store.artifact_suffix(artifact), ".json")
            finally:
                store.close()

    def test_retention_cleanup_previews_and_deletes_local_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_url = f"sqlite:///{(root / 'metadata.db').as_posix()}"
            artifact_root = root / "artifacts"

            store = create_store(database_url=database_url, artifact_root=artifact_root)
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                source = store.write_artifact(
                    job.id,
                    kind="source_input",
                    stage="intake",
                    filename="input.zip",
                    content=b"source",
                    content_type="application/zip",
                    producer="test.database_store",
                )
                log = store.write_artifact(
                    job.id,
                    kind="build_log",
                    stage="building",
                    filename="build.log",
                    content=b"log",
                    content_type="text/plain",
                    producer="test.database_store",
                )

                self.assertEqual(source.retention_class, "archive")
                self.assertIsNone(source.expires_at)
                self.assertEqual(log.retention_class, "ephemeral")
                self.assertIsNotNone(log.expires_at)

                preview = store.cleanup_retention(
                    job.id,
                    RetentionCleanupRequest(
                        dry_run=True,
                        categories=["logs"],
                        delete_expired=False,
                        reason="preview logs",
                    ),
                )
                self.assertEqual(preview.candidate_count, 1)
                self.assertEqual(preview.deleted_count, 0)
                self.assertTrue(Path(log.storage_uri).exists())
                self.assertIsNotNone(store.get_artifact(job.id, log.id))

                deleted = store.cleanup_retention(
                    job.id,
                    RetentionCleanupRequest(
                        dry_run=False,
                        categories=["logs"],
                        delete_expired=False,
                        reason="delete logs",
                    ),
                )

                self.assertEqual(deleted.candidate_count, 1)
                self.assertEqual(deleted.deleted_count, 1)
                self.assertFalse(Path(log.storage_uri).exists())
                self.assertIsNone(store.get_artifact(job.id, log.id))
                self.assertEqual([artifact.id for artifact in store.list_artifacts(job.id)], [source.id])
                deleted_record = store.get_artifact(job.id, log.id, include_deleted=True)
                self.assertIsNotNone(deleted_record)
                self.assertEqual(deleted_record.deletion_reason, "delete logs")
                self.assertIsNotNone(deleted_record.deleted_at)
            finally:
                store.close()

    def test_retention_cleanup_deletes_directory_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_url = f"sqlite:///{(root / 'metadata.db').as_posix()}"
            artifact_root = root / "artifacts"
            source_dir = root / "generated"
            source_dir.mkdir()
            (source_dir / "index.html").write_text("<h1>Generated</h1>", encoding="utf-8")

            store = create_store(database_url=database_url, artifact_root=artifact_root)
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                artifact = store.register_artifact_path(
                    job.id,
                    kind="generated_project",
                    stage="reconstructing",
                    filename="generated-project",
                    source_path=source_dir,
                    content_type="application/vnd.ai-jsunpack.generated-project+directory",
                    producer="test.database_store",
                )

                result = store.cleanup_retention(
                    job.id,
                    RetentionCleanupRequest(
                        dry_run=False,
                        categories=["derived"],
                        delete_expired=False,
                        reason="delete derived",
                    ),
                )

                self.assertEqual(result.deleted_count, 1)
                self.assertFalse(Path(artifact.storage_uri).exists())
                self.assertIsNone(store.get_artifact(job.id, artifact.id))
            finally:
                store.close()

    def test_retention_cleanup_deletes_s3_compatible_objects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_url = f"sqlite:///{(root / 'metadata.db').as_posix()}"
            object_client = InMemoryObjectStorageClient()
            artifact_store = S3CompatibleArtifactStore(bucket="artifact-bucket", prefix="retention", client=object_client)

            store = create_store(
                database_url=database_url,
                artifact_root=root / "artifacts",
                artifact_store=artifact_store,
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                artifact = store.write_artifact(
                    job.id,
                    kind="build_log",
                    stage="building",
                    filename="build.log",
                    content=b"log",
                    content_type="text/plain",
                    producer="test.database_store",
                )
                self.assertTrue(store.artifact_exists(artifact))

                result = store.cleanup_retention(
                    job.id,
                    RetentionCleanupRequest(
                        dry_run=False,
                        categories=["logs"],
                        delete_expired=False,
                        reason="delete object logs",
                    ),
                )

                self.assertEqual(result.deleted_count, 1)
                self.assertFalse(object_client.objects)
                self.assertIsNone(store.get_artifact(job.id, artifact.id))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
