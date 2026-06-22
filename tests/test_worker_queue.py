import tempfile
import unittest
from pathlib import Path

from apps.api.app.models import CreateJobRequest
from apps.api.app.store import create_store
from apps.worker.worker.pipeline import PipelineRun
from apps.worker.worker.queue import WorkerQueueRunner


class RecordingPipeline:
    def __init__(self, *, raise_error: Exception | None = None, cancel: bool = False) -> None:
        self.raise_error = raise_error
        self.cancel = cancel
        self.runs: list[tuple[str, bytes]] = []

    def run(self, job_id: str, input_path: Path, store) -> PipelineRun:
        self.runs.append((job_id, Path(input_path).read_bytes()))
        if self.cancel:
            store.request_cancel(job_id, "cancelled during pipeline")
            store.update_status(job_id, "building")
            run = PipelineRun(job_id)
            run.transition("cancelled", "cancelled during pipeline")
            return run
        if self.raise_error is not None:
            raise self.raise_error
        store.update_status(job_id, "completed")
        run = PipelineRun(job_id)
        run.transition("completed", "recording pipeline completed")
        return run


class WorkerQueueRunnerTest(unittest.TestCase):
    def test_run_once_leases_source_job_and_runs_pipeline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                queued = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                store.write_artifact(
                    queued.id,
                    kind="source_input",
                    stage="intake",
                    filename="input.bin",
                    content=b"queued-source",
                    content_type="application/octet-stream",
                    producer="test.worker_queue",
                )
                pipeline = RecordingPipeline()
                runner = WorkerQueueRunner(
                    store=store,
                    pipeline=pipeline,
                    worker_id="queue-worker",
                    lease_seconds=60,
                    poll_seconds=0.1,
                    max_attempts=2,
                )

                result = runner.run_once()

                self.assertIsNotNone(result)
                self.assertEqual(result.job_id, queued.id)
                self.assertEqual(result.status, "completed")
                self.assertEqual(pipeline.runs, [(queued.id, b"queued-source")])
                persisted = store.get_job(queued.id)
                self.assertEqual(persisted.status, "completed")
                self.assertEqual(persisted.run_attempt, 1)
                self.assertIsNone(persisted.worker_lease)
                heartbeats = store.list_ops_heartbeats(service_role="worker")
                self.assertEqual(len(heartbeats), 1)
                self.assertEqual(heartbeats[0].instance_id, "queue-worker")
                self.assertEqual(heartbeats[0].status, "ok")
                self.assertEqual(heartbeats[0].metrics["jobId"], queued.id)
                self.assertEqual(heartbeats[0].metrics["phase"], "completed")
            finally:
                store.close()

    def test_run_once_marks_pipeline_errors_with_failure_class(self):
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
                    filename="input.bin",
                    content=b"queued-source",
                    content_type="application/octet-stream",
                    producer="test.worker_queue",
                )
                runner = WorkerQueueRunner(
                    store=store,
                    pipeline=RecordingPipeline(raise_error=FileNotFoundError("missing source")),
                    worker_id="queue-worker",
                    lease_seconds=60,
                    max_attempts=2,
                )

                result = runner.run_once()

                self.assertIsNotNone(result)
                self.assertEqual(result.status, "failed")
                persisted = store.get_job(job.id)
                self.assertEqual(persisted.status, "failed")
                self.assertEqual(persisted.failure_class, "invalid_input")
                self.assertIsNone(persisted.worker_lease)
                heartbeats = store.list_ops_heartbeats(service_role="worker")
                self.assertEqual(len(heartbeats), 1)
                self.assertEqual(heartbeats[0].status, "degraded")
                self.assertEqual(heartbeats[0].metrics["phase"], "failed")
                self.assertEqual(heartbeats[0].alerts[0].code, "worker_pipeline_failed")
            finally:
                store.close()

    def test_run_once_preserves_cancelled_status_from_pipeline(self):
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
                    filename="input.bin",
                    content=b"queued-source",
                    content_type="application/octet-stream",
                    producer="test.worker_queue",
                )
                runner = WorkerQueueRunner(
                    store=store,
                    pipeline=RecordingPipeline(cancel=True),
                    worker_id="queue-worker",
                    lease_seconds=60,
                    max_attempts=2,
                )

                result = runner.run_once()

                self.assertIsNotNone(result)
                self.assertEqual(result.status, "cancelled")
                persisted = store.get_job(job.id)
                self.assertEqual(persisted.status, "cancelled")
                self.assertEqual(persisted.failure_reason, "cancelled during pipeline")
                self.assertIsNone(persisted.worker_lease)
                heartbeats = store.list_ops_heartbeats(service_role="worker")
                self.assertEqual(len(heartbeats), 1)
                self.assertEqual(heartbeats[0].status, "degraded")
                self.assertEqual(heartbeats[0].metrics["phase"], "completed")
                self.assertEqual(heartbeats[0].metrics["jobStatus"], "cancelled")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
