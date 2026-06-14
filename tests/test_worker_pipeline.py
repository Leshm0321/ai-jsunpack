import unittest

from apps.worker.worker.pipeline import WorkerPipeline


class WorkerPipelineTest(unittest.TestCase):
    def test_worker_pipeline_reaches_completed(self):
        run = WorkerPipeline().run("job_test")

        self.assertEqual(run.events[0].status, "leased")
        self.assertEqual(run.events[-1].status, "completed")
        self.assertTrue(any(event.status == "runtime_smoke" for event in run.events))
        self.assertTrue(any(event.status == "agent_pass" for event in run.events))


if __name__ == "__main__":
    unittest.main()
