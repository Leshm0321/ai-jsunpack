from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .agent_runtime import AgentRuntime, AgentRuntimeError, AgentRuntimeRequest
from .build_validation import BuildValidationError, BuildValidationRunner
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
        agent_runtime: AgentRuntime | None = None,
        build_validation_runner: BuildValidationRunner | None = None,
        runtime_smoke_runner: RuntimeSmokeRunner | None = None,
    ) -> None:
        self.core_bridge = core_bridge or CoreBridge()
        self.agent_runtime = agent_runtime or AgentRuntime()
        self.build_validation_runner = build_validation_runner or BuildValidationRunner()
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
            job = store.get_job(job_id)
            if job is None:
                raise AgentRuntimeError(f"Job not found during Agent runtime setup: {job_id}")
            agent_request = AgentRuntimeRequest(
                job_id=job_id,
                project_id=job.project_id,
                cloud_mode=job.cloud_mode,
                job_config=job.config,
                inventory_artifact_id=inventory_artifact.id,
                ast_index_artifact_id=ast_artifact.id,
                inventory_payload=result.inventory_artifact_payload,
                ast_index_payload=result.ast_index_artifact_payload,
            )
            agent_result = self.agent_runtime.run(job_id=job_id, store=store, request=agent_request)
            run.transition("agent_planning", "CrewAI planner context and evidence persisted.")
            run.transition("agent_pass", agent_result.message)

            evidence_parent_ids = [
                inventory_artifact.id,
                ast_artifact.id,
                agent_result.plan_artifact.id,
                agent_result.memory_artifact.id,
                agent_result.knowledge_artifact.id,
                *[artifact.id for artifact in agent_result.inference_artifacts],
                agent_result.review_artifact.id,
                agent_result.tool_call_artifact.id,
            ]
            build_validation_result = self.build_validation_runner.run(
                job_id=job_id,
                store=store,
                parent_artifact_ids=evidence_parent_ids,
            )
            run.transition("building", build_validation_result.build.message)
            run.transition("typechecking", build_validation_result.typecheck.message)

            runtime_result = self.runtime_smoke_runner.run(
                job_id=job_id,
                input_path=input_path,
                store=store,
                parent_artifact_ids=[*evidence_parent_ids, *build_validation_result.artifact_ids],
            )
            run.transition("runtime_smoke", runtime_result.message)
        except CoreBridgeError as error:
            store.update_status(job_id, "failed", failure_reason=str(error), failure_class="parse_error")
            run.transition("failed", str(error))
        except AgentRuntimeError as error:
            store.update_status(job_id, "failed", failure_reason=str(error), failure_class="agent_failed")
            run.transition("failed", str(error))
        except BuildValidationError as error:
            store.update_status(job_id, "failed", failure_reason=str(error), failure_class="build_error")
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
            "agent_planning": "CrewAI planner context and evidence prepared.",
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
