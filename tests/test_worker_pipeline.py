import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from apps.api.app.models import CreateJobRequest
from apps.api.app.store import create_store
from apps.worker.worker.pipeline import WorkerPipeline


ROOT = Path(__file__).resolve().parents[1]


class WorkerPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        npm = shutil.which("npm")
        node = shutil.which("node")
        if npm is None or node is None:
            raise unittest.SkipTest("npm and node are required for worker Core integration checks")

        subprocess.run(
            [npm, "run", "build", "--workspace", "@ai-jsunpack/shared"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [npm, "run", "build", "--workspace", "@ai-jsunpack/core"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_worker_pipeline_reaches_completed(self):
        run = WorkerPipeline().run("job_test")

        self.assertEqual(run.events[0].status, "leased")
        self.assertEqual(run.events[-1].status, "completed")
        self.assertTrue(any(event.status == "runtime_smoke" for event in run.events))
        self.assertTrue(any(event.status == "agent_pass" for event in run.events))

    def test_worker_pipeline_persists_core_inventory_and_ast_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "dist"
            asset_root = input_root / "assets"
            asset_root.mkdir(parents=True)
            (input_root / "index.html").write_text(
                '<div id="app"></div><script type="module" src="/assets/app.js"></script>',
                encoding="utf-8",
            )
            (asset_root / "app.js").write_text("function boot(){return 1} export { boot };", encoding="utf-8")

            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                run = WorkerPipeline().run(job.id, input_path=input_root, store=store)
                artifacts = store.list_artifacts(job.id)
                artifact_by_kind = {artifact.kind: artifact for artifact in artifacts}
                persisted_job = store.get_job(job.id)

                self.assertEqual(run.events[0].status, "leased")
                self.assertTrue(any(event.status == "intake" for event in run.events))
                self.assertTrue(any(event.status == "indexing" for event in run.events))
                self.assertIsNotNone(persisted_job)
                self.assertEqual(persisted_job.status, "indexing")
                self.assertIn("input_inventory", artifact_by_kind)
                self.assertIn("ast_index", artifact_by_kind)

                inventory_artifact = artifact_by_kind["input_inventory"]
                ast_index_artifact = artifact_by_kind["ast_index"]
                inventory_payload = json.loads(Path(inventory_artifact.storage_uri).read_text(encoding="utf-8"))
                ast_index_payload = json.loads(Path(ast_index_artifact.storage_uri).read_text(encoding="utf-8"))

                self.assertEqual(inventory_payload["kind"], "input_inventory")
                self.assertEqual(inventory_payload["inventory"]["entries"], ["index.html"])
                self.assertEqual(inventory_payload["inventory"]["scripts"], ["assets/app.js"])
                self.assertEqual(ast_index_payload["kind"], "ast_index")
                self.assertEqual(ast_index_artifact.parent_artifact_ids, [inventory_artifact.id])
                self.assertTrue(
                    any(symbol["name"] == "boot" for symbol in ast_index_payload["astIndexes"][0]["symbols"])
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
