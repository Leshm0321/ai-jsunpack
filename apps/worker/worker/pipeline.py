from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .core_bridge import CoreBridge, CoreBridgeError
from .runtime_smoke import RuntimeSmokeRunner


PipelineStatus = Literal[
    "queued",
    "leased",
    "intake",
    "planning",
    "parsing",
    "indexing",
    "analyzing",
    "agent_planning",
    "agent_pass",
    "reconstructing",
    "building",
    "typechecking",
    "runtime_smoke",
    "runtime_compare",
    "reviewing",
    "repairing",
    "packaging",
    "completed",
    "completed_best_effort",
    "failed",
    "cancelled",
]


PIPELINE_ORDER: list[PipelineStatus] = [
    "leased",
    "intake",
    "planning",
    "parsing",
    "indexing",
    "analyzing",
    "agent_planning",
    "agent_pass",
    "reconstructing",
    "building",
    "typechecking",
    "runtime_smoke",
    "runtime_compare",
    "reviewing",
    "packaging",
    "completed",
]


@dataclass
class PipelineEvent:
    status: PipelineStatus
    message: str


@dataclass
class PipelineRun:
    job_id: str
    events: list[PipelineEvent] = field(default_factory=list)

    def transition(self, status: PipelineStatus, message: str) -> None:
        self.events.append(PipelineEvent(status=status, message=message))


class WorkerPipeline:
    """Deterministic pipeline shell that later hosts core, Agent, and sandbox calls."""

    def __init__(
        self,
        core_bridge: CoreBridge | None = None,
        runtime_smoke_runner: RuntimeSmokeRunner | None = None,
    ) -> None:
        self.core_bridge = core_bridge or CoreBridge()
        self.runtime_smoke_runner = runtime_smoke_runner or RuntimeSmokeRunner()

    def run(self, job_id: str, input_path: Path | str | None = None, store=None) -> PipelineRun:
        run = PipelineRun(job_id=job_id)
        if input_path is not None and store is not None:
            self._run_core_analysis(job_id=job_id, input_path=input_path, store=store, run=run)
            return run

        for status in PIPELINE_ORDER:
            run.transition(status, self._message_for(status))
        return run

    def _run_core_analysis(self, *, job_id: str, input_path: Path | str, store, run: PipelineRun) -> None:
        run.transition("leased", self._message_for("leased"))
        try:
            store.update_status(job_id, "intake")
            run.transition("intake", "Core input inventory generation started.")
            result = self.core_bridge.analyze_input_package(job_id=job_id, input_path=input_path)
            inventory_artifact = store.write_artifact(
                job_id,
                kind="input_inventory",
                stage="intake",
                filename="input-inventory.json",
                content=self._json_bytes(result.inventory_artifact_payload),
                content_type="application/json",
                producer="worker.core",
            )

            store.update_status(job_id, "indexing")
            run.transition("indexing", "Core AST index generation completed.")
            ast_artifact = store.write_artifact(
                job_id,
                kind="ast_index",
                stage="indexing",
                filename="ast-index.json",
                content=self._json_bytes(result.ast_index_artifact_payload),
                content_type="application/json",
                producer="worker.core",
                parent_artifact_ids=[inventory_artifact.id],
            )
            runtime_result = self.runtime_smoke_runner.run(
                job_id=job_id,
                input_path=input_path,
                store=store,
                parent_artifact_ids=[inventory_artifact.id, ast_artifact.id],
            )
            run.transition("runtime_smoke", runtime_result.message)
        except CoreBridgeError as error:
            store.update_status(job_id, "failed", failure_reason=str(error), failure_class="parse_error")
            run.transition("failed", str(error))

    def _json_bytes(self, payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")

    def _message_for(self, status: PipelineStatus) -> str:
        messages = {
            "leased": "Worker lease acquired.",
            "intake": "Input inventory and sensitivity classification prepared.",
            "planning": "Task plan prepared from product and runtime constraints.",
            "parsing": "HTML, JS, CSS, source map, and manifest parsing scheduled.",
            "indexing": "AST, symbol, source range, and resource indexes scheduled.",
            "analyzing": "Runtime and bundle pattern analysis scheduled.",
            "agent_planning": "CrewAI planner context prepared.",
            "agent_pass": "Semantic inference pass scheduled.",
            "reconstructing": "Deterministic writer scheduled.",
            "building": "Sandbox build scheduled.",
            "typechecking": "TypeScript check scheduled.",
            "runtime_smoke": "Playwright runtime smoke scheduled.",
            "runtime_compare": "Original vs reconstructed runtime comparison scheduled.",
            "reviewing": "Review Agent scheduled.",
            "packaging": "Result package and audit report scheduled.",
            "completed": "Pipeline completed.",
        }
        return messages.get(status, status)
