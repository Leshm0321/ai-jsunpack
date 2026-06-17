import json
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import get_args

from apps.api.app import models


ROOT = Path(__file__).resolve().parents[1]


class SharedContractAlignmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        npm = shutil.which("npm")
        node = shutil.which("node")
        if npm is None or node is None:
            raise unittest.SkipTest("npm and node are required for cross-language contract checks")

        subprocess.run(
            [npm, "run", "build", "--workspace", "@ai-jsunpack/shared"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        script = """
          import {
            SHARED_CONTRACT_ENUMS,
            SHARED_CONTRACT_EXAMPLES,
            SHARED_JSON_SCHEMAS
          } from "./packages/shared/dist/index.js";

          console.log(JSON.stringify({
            enums: SHARED_CONTRACT_ENUMS,
            examples: SHARED_CONTRACT_EXAMPLES,
            schemas: SHARED_JSON_SCHEMAS
          }));
        """
        result = subprocess.run(
            [node, "--input-type=module", "-e", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        cls.contract = json.loads(result.stdout)

    def test_python_enums_match_typescript_contract_enums(self):
        expected = self.contract["enums"]

        self.assertEqual(list(get_args(models.JobStatus)), expected["jobStatus"])
        self.assertEqual(list(get_args(models.CloudMode)), expected["cloudMode"])
        self.assertEqual(list(get_args(models.ArtifactKind)), expected["artifactKind"])
        self.assertEqual(list(get_args(models.FailureClass)), expected["failureClass"])
        self.assertEqual(list(get_args(models.SensitivityClass)), expected["sensitivityClass"])
        self.assertEqual(list(get_args(models.RetentionClass)), expected["retentionClass"])

    def test_pydantic_models_validate_typescript_contract_examples(self):
        model_by_example = {
            "job": models.JobRecord,
            "artifact": models.ArtifactRecord,
            "evidenceRef": models.EvidenceRef,
            "inferenceRecord": models.InferenceRecord,
            "reviewRun": models.ReviewRun,
            "buildArtifact": models.BuildArtifact,
            "runtimeScenario": models.RuntimeScenario,
            "runtimeComparisonReport": models.RuntimeComparisonReport,
            "runtimeValidationRun": models.RuntimeValidationRun,
            "toolCall": models.ToolCall,
            "memoryRecord": models.MemoryRecord,
            "repairInstruction": models.RepairInstruction,
        }

        for example_name, model in model_by_example.items():
            with self.subTest(example=example_name):
                example = self.contract["examples"][example_name]
                validated = model.model_validate(example)
                self.assertEqual(validated.model_dump(by_alias=True), example)

    def test_pydantic_json_schema_uses_camel_case_contract_fields(self):
        job_schema = models.JobRecord.model_json_schema(by_alias=True)
        artifact_schema = models.ArtifactRecord.model_json_schema(by_alias=True)

        self.assertEqual(job_schema["required"], self.contract["schemas"]["job"]["required"])
        self.assertEqual(artifact_schema["required"], self.contract["schemas"]["artifact"]["required"])
        self.assertIn("ownerId", job_schema["properties"])
        self.assertNotIn("owner_id", job_schema["properties"])
        self.assertIn("schemaVersion", artifact_schema["properties"])
        self.assertNotIn("schema_version", artifact_schema["properties"])

    def test_repair_action_contract_accepts_runtime_static_mirror(self):
        example = dict(self.contract["examples"]["repairInstruction"])
        example["targetStage"] = "runtime_compare"
        example["failureClass"] = "runtime_error"
        example["actions"] = [
            {
                "action": "mirror_original_static_entry",
                "path": "projectRoot",
                "value": "public/original",
                "reason": "Mirror the original static entry for runtime compare retry.",
            }
        ]

        validated = models.RepairInstruction.model_validate(example)
        action_schema = self.contract["schemas"]["repairInstruction"]["properties"]["actions"]["items"]

        self.assertEqual(validated.actions[0].action, "mirror_original_static_entry")
        self.assertIn("mirror_original_static_entry", action_schema["properties"]["action"]["enum"])


if __name__ == "__main__":
    unittest.main()
