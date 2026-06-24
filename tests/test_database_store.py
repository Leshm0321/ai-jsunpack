import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from apps.api.app.artifact_store import (
    Boto3ObjectStorageClient,
    InMemoryObjectStorageClient,
    S3CompatibleArtifactStore,
    artifact_lifecycle_rules,
)
from apps.api.app.models import CreateJobRequest, OpsAlert, OpsHeartbeatRequest, RetentionCleanupRequest
from apps.api.app.store import create_artifact_store, create_store


class FakeS3Error(Exception):
    def __init__(self, code: str, status_code: int = 400) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        }


class FakeS3Paginator:
    def __init__(self, client: "FakeS3Client") -> None:
        self.client = client

    def paginate(self, *, Bucket: str, Prefix: str):
        yield {
            "Contents": [
                {"Key": key}
                for item_bucket, key in sorted(self.client.objects)
                if item_bucket == Bucket and key.startswith(Prefix)
            ]
        }


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_requests: list[dict[str, object]] = []
        self.deleted: list[tuple[str, str]] = []
        self.lifecycle_rules: list[dict[str, object]] | None = None
        self.existing_lifecycle_rules: list[dict[str, object]] | None = None

    def put_object(self, **kwargs):
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        self.objects[(bucket, key)] = body
        self.put_requests.append(kwargs)

    def get_object(self, **kwargs):
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        if (bucket, key) not in self.objects:
            raise FakeS3Error("NoSuchKey", 404)
        return {"Body": BytesIO(self.objects[(bucket, key)])}

    def head_object(self, **kwargs):
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        if (bucket, key) not in self.objects:
            raise FakeS3Error("404", 404)
        return {}

    def get_paginator(self, operation_name: str):
        self.assert_operation(operation_name, "list_objects_v2")
        return FakeS3Paginator(self)

    def delete_object(self, **kwargs):
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        self.deleted.append((bucket, key))
        self.objects.pop((bucket, key), None)

    def generate_presigned_url(self, client_method: str, *, Params: dict[str, str], ExpiresIn: int):
        self.assert_operation(client_method, "get_object")
        return f"https://signed.example/{Params['Bucket']}/{Params['Key']}?ttl={ExpiresIn}"

    def get_bucket_lifecycle_configuration(self, **kwargs):
        if self.existing_lifecycle_rules is None:
            raise FakeS3Error("NoSuchLifecycleConfiguration", 404)
        return {"Rules": self.existing_lifecycle_rules}

    def put_bucket_lifecycle_configuration(self, **kwargs):
        self.lifecycle_rules = kwargs["LifecycleConfiguration"]["Rules"]

    def assert_operation(self, actual: str, expected: str) -> None:
        if actual != expected:
            raise AssertionError(f"Expected {expected}, got {actual}")


class LifecycleInMemoryObjectStorageClient(InMemoryObjectStorageClient):
    def __init__(self) -> None:
        super().__init__()
        self.lifecycle_rules: list[dict[str, object]] = []

    def configure_lifecycle_rules(self, bucket: str, rules: list[dict[str, object]]) -> None:
        self.lifecycle_rules = rules


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

    def test_worker_queue_lease_renewal_and_guarded_status_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                skipped = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                queued = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                store.write_artifact(
                    queued.id,
                    kind="source_input",
                    stage="intake",
                    filename="input.zip",
                    content=b"source",
                    content_type="application/zip",
                    producer="test.database_store",
                )

                leased = store.lease_next_job(worker_id="worker-a", lease_seconds=60)
                self.assertIsNotNone(leased)
                self.assertEqual(leased.id, queued.id)
                self.assertEqual(leased.status, "leased")
                self.assertEqual(leased.run_attempt, 1)
                self.assertEqual(leased.worker_lease.worker_id, "worker-a")
                self.assertIsNone(store.lease_next_job(worker_id="worker-b", lease_seconds=60))
                self.assertEqual(store.get_job(skipped.id).status, "queued")

                self.assertIsNone(store.renew_lease(job_id=queued.id, worker_id="worker-b", lease_seconds=60))
                renewed = store.renew_lease(job_id=queued.id, worker_id="worker-a", lease_seconds=120)
                self.assertIsNotNone(renewed)
                self.assertEqual(renewed.worker_lease.worker_id, "worker-a")

                guarded = store.update_status(queued.id, "building", expected_worker_id="worker-b")
                self.assertEqual(guarded.status, "leased")
                updated = store.update_status(queued.id, "building", expected_worker_id="worker-a")
                self.assertEqual(updated.status, "building")
            finally:
                store.close()

    def test_list_project_artifact_payloads_scopes_by_project_and_excludes_current_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                current_job = store.create_job(CreateJobRequest(project_id="proj-a", owner_id="owner"))
                historical_job = store.create_job(CreateJobRequest(project_id="proj-a", owner_id="owner"))
                other_project_job = store.create_job(CreateJobRequest(project_id="proj-b", owner_id="owner"))

                current_repair = store.write_artifact(
                    current_job.id,
                    kind="repair_instruction",
                    stage="repairing",
                    filename="current-repair.json",
                    content=b'{"kind":"repair_instruction","status":"planned"}',
                    content_type="application/json",
                    producer="test.database_store",
                )
                historical_repair = store.write_artifact(
                    historical_job.id,
                    kind="repair_instruction",
                    stage="repairing",
                    filename="historical-repair.json",
                    content=b'{"kind":"repair_instruction","status":"planned"}',
                    content_type="application/json",
                    producer="test.database_store",
                )
                historical_review = store.write_artifact(
                    historical_job.id,
                    kind="review_run",
                    stage="reviewing",
                    filename="historical-review.json",
                    content=b'{"kind":"review_run","status":"fail"}',
                    content_type="application/json",
                    producer="test.database_store",
                )
                store.write_artifact(
                    other_project_job.id,
                    kind="repair_instruction",
                    stage="repairing",
                    filename="other-repair.json",
                    content=b'{"kind":"repair_instruction","status":"planned"}',
                    content_type="application/json",
                    producer="test.database_store",
                )

                payloads = store.list_project_artifact_payloads(
                    project_id="proj-a",
                    kinds=("repair_instruction", "review_run"),
                    exclude_job_id=current_job.id,
                )
                artifact_ids = {payload["artifactId"] for payload in payloads}

                self.assertEqual(artifact_ids, {historical_repair.id, historical_review.id})
                self.assertTrue(all(payload["projectId"] == "proj-a" for payload in payloads))
                self.assertNotIn(current_repair.id, artifact_ids)
            finally:
                store.close()

    def test_worker_queue_requeues_expired_leases_until_attempt_limit(self):
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
                    kind="source_input",
                    stage="intake",
                    filename="input.zip",
                    content=b"source",
                    content_type="application/zip",
                    producer="test.database_store",
                )
                now = datetime(2026, 6, 18, 8, 0, tzinfo=timezone.utc)

                first = store.lease_next_job(worker_id="worker-a", lease_seconds=1, max_attempts=2, now=now.isoformat())
                self.assertIsNotNone(first)
                requeued = store.requeue_expired_leases(
                    max_attempts=2,
                    now=(now + timedelta(seconds=2)).isoformat(),
                )
                self.assertEqual([item.status for item in requeued], ["queued"])
                self.assertIsNone(store.get_job(job.id).worker_lease)

                second = store.lease_next_job(
                    worker_id="worker-b",
                    lease_seconds=1,
                    max_attempts=2,
                    now=(now + timedelta(seconds=3)).isoformat(),
                )
                self.assertIsNotNone(second)
                self.assertEqual(second.run_attempt, 2)
                failed = store.requeue_expired_leases(
                    max_attempts=2,
                    now=(now + timedelta(seconds=5)).isoformat(),
                )
                self.assertEqual([item.status for item in failed], ["failed"])
                final = store.get_job(job.id)
                self.assertEqual(final.failure_class, "timeout")
                self.assertIsNone(final.worker_lease)
            finally:
                store.close()

    def test_ops_heartbeats_persist_and_filter_active_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                checked_at = datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)
                active = store.record_ops_heartbeat(
                    OpsHeartbeatRequest(
                        service_role="worker",
                        instance_id="worker-a",
                        status="ok",
                        ttl_seconds=30,
                        checked_at=checked_at.isoformat(),
                        metrics={"phase": "idle", "jobCount": 1},
                        alerts=[
                            OpsAlert(
                                code="worker_overloaded",
                                severity="warning",
                                message="Worker is approaching capacity.",
                                field="jobCount",
                                value=1,
                                threshold=0,
                            )
                        ],
                        metadata={"role": "worker"},
                    )
                )
                stale = store.record_ops_heartbeat(
                    OpsHeartbeatRequest(
                        service_role="browser-runner",
                        instance_id="runner-a",
                        status="degraded",
                        ttl_seconds=1,
                        checked_at=checked_at.isoformat(),
                        metrics={"queueBackend": "sqlite"},
                        alerts=[],
                        metadata={"role": "browser-runner"},
                    )
                )

                self.assertEqual(active.service_role, "worker")
                self.assertEqual(active.alerts[0].code, "worker_overloaded")
                self.assertEqual(stale.status, "degraded")

                all_heartbeats = store.list_ops_heartbeats()
                active_only = store.list_ops_heartbeats(
                    include_stale=False,
                    now=(checked_at + timedelta(seconds=10)).isoformat(),
                )

                self.assertEqual(len(all_heartbeats), 2)
                self.assertEqual({heartbeat.service_role for heartbeat in all_heartbeats}, {"worker", "browser-runner"})
                self.assertEqual([heartbeat.service_role for heartbeat in active_only], ["worker"])
                self.assertEqual(active_only[0].metadata["role"], "worker")
            finally:
                store.close()

    def test_cancelled_job_releases_lease_and_cannot_be_reopened_by_status_update(self):
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
                    kind="source_input",
                    stage="intake",
                    filename="input.zip",
                    content=b"source",
                    content_type="application/zip",
                    producer="test.database_store",
                )
                leased = store.lease_next_job(worker_id="worker-a")
                self.assertIsNotNone(leased)

                cancelled = store.request_cancel(job.id, "operator cancelled")
                self.assertEqual(cancelled.status, "cancelled")
                self.assertEqual(cancelled.failure_reason, "operator cancelled")
                self.assertIsNone(cancelled.worker_lease)
                self.assertIsNone(store.lease_next_job(worker_id="worker-b"))

                guarded = store.update_status(job.id, "building")
                self.assertEqual(guarded.status, "cancelled")
            finally:
                store.close()

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
                object_key = next(iter(object_client.objects))[1]
                self.assertEqual(object_client.metadata[("artifact-bucket", object_key)]["retention-class"], "archive")
                self.assertEqual(object_client.tags[("artifact-bucket", object_key)]["retentionClass"], "archive")
                self.assertEqual(object_client.tags[("artifact-bucket", object_key)]["artifactKind"], "source_input")
            finally:
                store.close()

    def test_boto3_object_storage_client_supports_io_signing_and_lifecycle_rules(self):
        fake_s3 = FakeS3Client()
        client = Boto3ObjectStorageClient(s3_client=fake_s3)

        client.put_object(
            "artifact-bucket",
            "prefix/job/artifact.json",
            b'{"ok":true}',
            metadata={"retention-class": "ephemeral"},
            tags={"retentionClass": "ephemeral", "artifactKind": "build_log"},
        )

        self.assertTrue(client.object_exists("artifact-bucket", "prefix/job/artifact.json"))
        self.assertEqual(client.get_object("artifact-bucket", "prefix/job/artifact.json"), b'{"ok":true}')
        self.assertEqual(client.list_objects("artifact-bucket", "prefix/"), ["prefix/job/artifact.json"])
        self.assertEqual(
            client.presigned_get_object_url(
                "artifact-bucket",
                "prefix/job/artifact.json",
                expires_in_seconds=120,
            ),
            "https://signed.example/artifact-bucket/prefix/job/artifact.json?ttl=120",
        )
        self.assertEqual(fake_s3.put_requests[0]["Metadata"], {"retention-class": "ephemeral"})
        self.assertEqual(
            fake_s3.put_requests[0]["Tagging"],
            "retentionClass=ephemeral&artifactKind=build_log",
        )

        fake_s3.existing_lifecycle_rules = [
            {"ID": "external-backup", "Status": "Enabled"},
            {"ID": "ai-jsunpack-old", "Status": "Enabled"},
        ]
        client.configure_lifecycle_rules(
            "artifact-bucket",
            artifact_lifecycle_rules(prefix="team/artifacts", ephemeral_days=7),
        )

        self.assertEqual(len(fake_s3.lifecycle_rules), 2)
        self.assertEqual(fake_s3.lifecycle_rules[0]["ID"], "external-backup")
        self.assertEqual(fake_s3.lifecycle_rules[1]["ID"], "ai-jsunpack-ephemeral-artifacts")
        self.assertEqual(
            fake_s3.lifecycle_rules[1]["Filter"]["And"]["Tags"],
            [{"Key": "retentionClass", "Value": "ephemeral"}],
        )

        client.delete_object("artifact-bucket", "prefix/job/artifact.json")
        self.assertFalse(client.object_exists("artifact-bucket", "prefix/job/artifact.json"))
        self.assertEqual(fake_s3.deleted, [("artifact-bucket", "prefix/job/artifact.json")])

    def test_s3_compatible_store_presigns_object_reads(self):
        object_client = InMemoryObjectStorageClient()
        artifact_store = S3CompatibleArtifactStore(
            bucket="artifact-bucket",
            prefix="signed",
            client=object_client,
            presign_ttl_seconds=90,
        )
        stored = artifact_store.write_bytes(
            job_id="job_1",
            artifact_id="artifact_1",
            filename="audit.md",
            content=b"# Audit",
        )

        self.assertEqual(
            artifact_store.presigned_read_url(stored.storage_uri),
            "https://object-storage.local/artifact-bucket/signed/job_1/artifact_1-audit.md?expires=90",
        )

    def test_create_artifact_store_uses_s3_env_configuration_and_lifecycle(self):
        object_client = LifecycleInMemoryObjectStorageClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "AI_JSUNPACK_ARTIFACT_STORE": "minio",
                    "AI_JSUNPACK_ARTIFACT_S3_BUCKET": "artifact-bucket",
                    "AI_JSUNPACK_ARTIFACT_S3_PREFIX": "deploy/prod",
                    "AI_JSUNPACK_ARTIFACT_S3_PRESIGN_TTL_SECONDS": "600",
                    "AI_JSUNPACK_ARTIFACT_S3_LIFECYCLE_ENABLED": "true",
                },
                clear=False,
            ):
                artifact_store = create_artifact_store(Path(temp_dir), object_client=object_client)

        self.assertIsInstance(artifact_store, S3CompatibleArtifactStore)
        self.assertEqual(artifact_store.bucket, "artifact-bucket")
        self.assertEqual(artifact_store.prefix, "deploy/prod")
        self.assertEqual(artifact_store.presign_ttl_seconds, 600)
        self.assertEqual(object_client.lifecycle_rules[0]["Expiration"], {"Days": 7})
        self.assertEqual(object_client.lifecycle_rules[0]["Filter"]["And"]["Prefix"], "deploy/prod/")

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
