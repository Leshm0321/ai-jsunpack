from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from apps.worker.worker.core_bridge import CoreBridge
from apps.worker.worker.reconstruction import ReconstructionError, ReconstructionRunner


class ReconstructionFeedbackTest(unittest.TestCase):
    def test_runner_passes_only_review_approved_planned_low_risk_non_conflicting_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = _FakeStore(root)
            approved = store.add_parent(
                "repair_approved_artifact",
                "repair_instruction",
                {
                    "id": "repair_approved",
                    "status": "planned",
                    "riskLevel": "low",
                    "actions": [
                        {
                            "action": "add_package_script",
                            "path": "package.json:scripts.check",
                            "value": "node scripts/typecheck.mjs",
                            "reason": "Use deterministic shim.",
                        }
                    ],
                },
            )
            high_risk = store.add_parent(
                "repair_high_risk_artifact",
                "repair_instruction",
                {
                    "id": "repair_high_risk",
                    "status": "planned",
                    "riskLevel": "high",
                    "actions": [
                        {
                            "action": "mirror_original_static_entry",
                            "path": "projectRoot",
                            "value": "public/original",
                            "reason": "Unsafe approval fixture.",
                        }
                    ],
                },
            )
            unapproved = store.add_parent(
                "repair_unapproved_artifact",
                "repair_instruction",
                {
                    "id": "repair_unapproved",
                    "status": "planned",
                    "riskLevel": "low",
                    "actions": [
                        {
                            "action": "replace_package_script",
                            "path": "package.json:scripts.build",
                            "value": "node scripts/build.mjs",
                            "reason": "Not listed by Review.",
                        }
                    ],
                },
            )
            malformed = store.add_parent(
                "repair_malformed_artifact",
                "repair_instruction",
                {
                    "id": "repair_malformed",
                    "status": "planned",
                    "riskLevel": "low",
                    "actions": [{"action": "unknown", "path": "x", "value": "y", "reason": "z"}],
                },
            )
            review = store.add_parent(
                "review_artifact",
                "review_run",
                {
                    "reviewType": "agent_review",
                    "status": "pass",
                    "repairInstructionIds": [
                        "repair_approved",
                        "repair_high_risk",
                        "repair_malformed",
                    ],
                },
            )
            bridge = _CapturingBridge()
            runner = ReconstructionRunner(core_bridge=bridge)

            result = runner.run(
                job_id="job_feedback",
                input_path=root / "input",
                store=store,
                parent_artifact_ids=[approved.id, high_risk.id, unapproved.id, malformed.id, review.id],
            )

            self.assertIsNotNone(bridge.feedback)
            assert bridge.feedback is not None
            self.assertEqual(bridge.feedback["sourceReviewArtifactIds"], [review.id])
            self.assertEqual(len(bridge.feedback["approvedActions"]), 1)
            self.assertEqual(bridge.feedback["approvedActions"][0]["sourceArtifactId"], approved.id)
            rejection_by_source = {
                item["sourceArtifactId"]: item["reason"] for item in bridge.feedback["rejectedActions"]
            }
            self.assertIn("riskLevel 必须为 low", rejection_by_source[high_risk.id])
            self.assertIn("未获得", rejection_by_source[unapproved.id])
            self.assertIn("不支持的修复动作", rejection_by_source[malformed.id])
            plan_payload = json.loads(store.read_artifact("job_feedback", result.plan_artifact.id))
            self.assertEqual(plan_payload["agentFeedbackInputs"]["approvedActions"][0]["sourceArtifactId"], approved.id)

    def test_runner_rejects_conflicting_review_approved_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _FakeStore(Path(temp_dir))
            repair_a = store.add_parent(
                "repair_a",
                "repair_instruction",
                {
                    "id": "logical_a",
                    "status": "planned",
                    "riskLevel": "low",
                    "actions": [
                        {
                            "action": "replace_package_script",
                            "path": "package.json:scripts.build",
                            "value": "node scripts/build.mjs",
                            "reason": "A",
                        }
                    ],
                },
            )
            repair_b = store.add_parent(
                "repair_b",
                "repair_instruction",
                {
                    "id": "logical_b",
                    "status": "planned",
                    "riskLevel": "low",
                    "actions": [
                        {
                            "action": "replace_package_script",
                            "path": "package.json:scripts.build",
                            "value": "node scripts/typecheck.mjs",
                            "reason": "B",
                        }
                    ],
                },
            )
            review = store.add_parent(
                "review",
                "review_run",
                {
                    "reviewType": "agent_review",
                    "status": "pass",
                    "failureClass": "none",
                    "repairInstructionIds": [repair_a.id, repair_b.id],
                },
            )

            feedback = ReconstructionRunner()._writer_feedback_inputs(
                job_id="job_feedback",
                store=store,
                parent_artifact_ids=[repair_a.id, repair_b.id, review.id],
            )

            assert feedback is not None
            self.assertEqual(feedback["approvedActions"], [])
            self.assertEqual(len(feedback["rejectedActions"]), 2)
            self.assertTrue(all("存在冲突" in item["reason"] for item in feedback["rejectedActions"]))

    def test_failed_agent_review_cannot_authorize_repairs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _FakeStore(Path(temp_dir))
            repair = store.add_parent(
                "repair_artifact",
                "repair_instruction",
                {
                    "id": "RepairAgent:repair:1",
                    "status": "planned",
                    "riskLevel": "low",
                    "actions": [
                        {
                            "action": "add_package_script",
                            "path": "package.json:scripts.check",
                            "value": "node scripts/typecheck.mjs",
                            "reason": "Must remain audit-only after failed Review.",
                        }
                    ],
                },
            )
            review = store.add_parent(
                "review_artifact",
                "review_run",
                {
                    "reviewType": "agent_review",
                    "status": "fail",
                    "failureClass": "agent_failed",
                    "repairInstructionIds": [repair.id],
                },
            )

            feedback = ReconstructionRunner()._writer_feedback_inputs(
                job_id="job_feedback",
                store=store,
                parent_artifact_ids=[repair.id, review.id],
            )

            assert feedback is not None
            self.assertEqual(feedback["approvedActions"], [])
            self.assertTrue(
                any("必须以 failureClass=none 通过" in item["reason"] for item in feedback["rejectedActions"])
            )

    def test_core_bridge_serializes_feedback_to_cli_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cli_path = root / "fake_cli.py"
            cli_path.write_text(
                "\n".join(
                    [
                        "import json, pathlib, sys",
                        "feedback_path = pathlib.Path(sys.argv[sys.argv.index('--agent-feedback-file') + 1])",
                        "feedback = json.loads(feedback_path.read_text(encoding='utf-8'))",
                        "print(json.dumps({",
                        "  'reconstructionPlanPayload': {'kind': 'reconstruction_plan', 'feedback': feedback},",
                        "  'generatedProjectManifestPayload': {'kind': 'generated_project', 'manifest': {}},",
                        "  'generatedProjectPath': 'generated'",
                        "}))",
                    ]
                ),
                encoding="utf-8",
            )
            feedback = {
                "kind": "agent_feedback",
                "protocolVersion": 1,
                "sourceReviewArtifactIds": ["review"],
                "approvedActions": [],
                "rejectedActions": [],
            }

            result = CoreBridge(node_binary=sys.executable, cli_path=cli_path).reconstruct_input_package(
                job_id="job_bridge",
                input_path=root / "input",
                output_dir=root / "output",
                agent_feedback=feedback,
            )

            self.assertEqual(result.reconstruction_plan_payload["feedback"], feedback)

    def test_runner_rejects_bridge_that_cannot_accept_feedback(self):
        class UnsupportedBridge:
            def reconstruct_input_package(self, *, job_id, input_path, output_dir):
                raise AssertionError("Bridge must not run when feedback capability is missing.")

        feedback = {
            "kind": "agent_feedback",
            "protocolVersion": 1,
            "sourceReviewArtifactIds": ["review"],
            "approvedActions": [
                {
                    "sourceArtifactId": "repair",
                    "action": "add_package_script",
                    "path": "package.json:scripts.check",
                    "value": "node scripts/typecheck.mjs",
                    "reason": "Approved deterministic repair.",
                }
            ],
            "rejectedActions": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ReconstructionError, "不接受 agent_feedback"):
                ReconstructionRunner(core_bridge=UnsupportedBridge())._reconstruct_with_feedback(
                    job_id="job_feedback",
                    input_path=Path(temp_dir) / "input",
                    output_dir=Path(temp_dir) / "output",
                    feedback=feedback,
                )


class _CapturingBridge:
    def __init__(self) -> None:
        self.feedback = None

    def reconstruct_input_package(self, *, job_id, input_path, output_dir, agent_feedback=None):
        del job_id, input_path
        self.feedback = agent_feedback
        output_dir.mkdir(parents=True)
        (output_dir / "index.html").write_text("generated", encoding="utf-8")
        return SimpleNamespace(
            reconstruction_plan_payload={
                "kind": "reconstruction_plan",
                "plan": {"agentFeedback": agent_feedback},
                "limitations": [],
            },
            generated_project_manifest_payload={"kind": "generated_project", "manifest": {}},
            generated_project_path=output_dir,
        )


class _FakeStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.payloads: dict[str, bytes] = {}
        self.artifacts: dict[str, SimpleNamespace] = {}
        self.counter = 0

    def add_parent(self, artifact_id: str, kind: str, payload: dict) -> SimpleNamespace:
        artifact = SimpleNamespace(id=artifact_id, kind=kind)
        self.artifacts[artifact_id] = artifact
        self.payloads[artifact_id] = json.dumps(payload).encode("utf-8")
        return artifact

    def update_status(self, job_id: str, status: str) -> None:
        del job_id, status

    def get_artifact(self, job_id: str, artifact_id: str):
        del job_id
        return self.artifacts.get(artifact_id)

    def read_artifact(self, job_id: str, artifact_id: str) -> bytes:
        del job_id
        return self.payloads[artifact_id]

    def write_artifact(self, job_id: str, **kwargs):
        del job_id
        self.counter += 1
        artifact_id = f"written_{self.counter}"
        artifact = SimpleNamespace(id=artifact_id, kind=kwargs["kind"])
        self.artifacts[artifact_id] = artifact
        self.payloads[artifact_id] = kwargs["content"]
        return artifact

    def register_artifact_path(self, job_id: str, **kwargs):
        del job_id
        self.counter += 1
        artifact_id = f"written_{self.counter}"
        target = self.root / artifact_id
        shutil.copytree(kwargs["source_path"], target)
        artifact = SimpleNamespace(id=artifact_id, kind=kwargs["kind"], local_path=target)
        self.artifacts[artifact_id] = artifact
        return artifact

    def artifact_local_path(self, artifact) -> Path:
        return artifact.local_path


if __name__ == "__main__":
    unittest.main()
