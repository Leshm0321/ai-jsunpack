from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .agent_runtime import AgentRuntime, AgentRuntimeError, AgentRuntimeRequest
from .build_validation import BuildValidationError, BuildValidationRunner
from .core_bridge import CoreBridge, CoreBridgeError
from .reconstruction import ReconstructionError, ReconstructionRunner
from .runtime_smoke import RuntimeCompareReviewGate, RuntimeCompareRunner, RuntimeSmokeRunner
from .packaging import PackagingError, PackagingRunner


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
        reconstruction_runner: ReconstructionRunner | None = None,
        build_validation_runner: BuildValidationRunner | None = None,
        runtime_smoke_runner: RuntimeSmokeRunner | None = None,
        runtime_compare_runner: RuntimeCompareRunner | None = None,
        runtime_compare_review_gate: RuntimeCompareReviewGate | None = None,
        packaging_runner: PackagingRunner | None = None,
    ) -> None:
        self.core_bridge = core_bridge or CoreBridge()
        self.agent_runtime = agent_runtime or AgentRuntime()
        self.reconstruction_runner = reconstruction_runner or ReconstructionRunner(self.core_bridge)
        self.build_validation_runner = build_validation_runner or BuildValidationRunner()
        self.runtime_smoke_runner = runtime_smoke_runner or RuntimeSmokeRunner()
        self.runtime_compare_runner = runtime_compare_runner or RuntimeCompareRunner(
            browser_adapter=self.runtime_smoke_runner.browser_adapter,
            timeout_ms=self.runtime_smoke_runner.timeout_ms,
            sandbox_runner=self.runtime_smoke_runner.sandbox_runner,
        )
        self.runtime_compare_review_gate = runtime_compare_review_gate or RuntimeCompareReviewGate()
        self.packaging_runner = packaging_runner or PackagingRunner()

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
            reconstruction_result = self.reconstruction_runner.run(
                job_id=job_id,
                input_path=input_path,
                store=store,
                parent_artifact_ids=evidence_parent_ids,
            )
            run.transition("reconstructing", reconstruction_result.message)

            validation_parent_ids = [*evidence_parent_ids, *reconstruction_result.artifact_ids]
            build_validation_result = self.build_validation_runner.run(
                job_id=job_id,
                store=store,
                parent_artifact_ids=validation_parent_ids,
            )
            run.transition("building", build_validation_result.build.message)
            run.transition("typechecking", build_validation_result.typecheck.message)
            if build_validation_result.repair_artifacts:
                run.transition("repairing", "Build/typecheck review produced repair evidence for validation retry.")

            runtime_result = self.runtime_smoke_runner.run(
                job_id=job_id,
                input_path=reconstruction_result.project_path,
                store=store,
                parent_artifact_ids=[*validation_parent_ids, *build_validation_result.artifact_ids],
            )
            run.transition("runtime_smoke", runtime_result.message)
            runtime_compare_result = self.runtime_compare_runner.run_compare(
                job_id=job_id,
                store=store,
                original_input_path=input_path,
                reconstructed_input_path=reconstruction_result.project_path,
                scenario_config=self._runtime_compare_config(job.config),
                parent_artifact_ids=[
                    *validation_parent_ids,
                    *build_validation_result.artifact_ids,
                    runtime_result.trace_artifact.id,
                    runtime_result.report_artifact.id,
                    *([runtime_result.screenshot_artifact.id] if runtime_result.screenshot_artifact else []),
                ],
            )
            run.transition("runtime_compare", runtime_compare_result.message)
            runtime_compare_gate_result = self.runtime_compare_review_gate.run(
                job_id=job_id,
                store=store,
                comparison_artifacts=runtime_compare_result.comparison_artifacts,
                job_config=job.config,
                parent_artifact_ids=runtime_compare_result.artifact_ids,
            )
            if runtime_compare_gate_result.enabled:
                run.transition("reviewing", runtime_compare_gate_result.message)
                if runtime_compare_gate_result.triggered:
                    run.transition(
                        "repairing",
                        "Runtime compare review produced repair evidence for follow-up Review/Fix.",
                    )
            packaging_parent_ids = [
                *validation_parent_ids,
                *build_validation_result.artifact_ids,
                runtime_result.trace_artifact.id,
                runtime_result.report_artifact.id,
                *runtime_compare_result.artifact_ids,
                *runtime_compare_gate_result.artifact_ids,
            ]
            if runtime_result.screenshot_artifact is not None:
                packaging_parent_ids.append(runtime_result.screenshot_artifact.id)
            packaging_result = self.packaging_runner.run(
                job_id=job_id,
                store=store,
                parent_artifact_ids=packaging_parent_ids,
            )
            run.transition("packaging", packaging_result.message)
            store.update_status(
                job_id,
                packaging_result.final_status,
                failure_reason=packaging_result.failure_reason,
                failure_class=packaging_result.failure_class,
            )
            run.transition(packaging_result.final_status, self._message_for(packaging_result.final_status))
        except CoreBridgeError as error:
            store.update_status(job_id, "failed", failure_reason=str(error), failure_class="parse_error")
            run.transition("failed", str(error))
        except AgentRuntimeError as error:
            store.update_status(job_id, "failed", failure_reason=str(error), failure_class="agent_failed")
            run.transition("failed", str(error))
        except ReconstructionError as error:
            store.update_status(job_id, "failed", failure_reason=str(error), failure_class="unknown")
            run.transition("failed", str(error))
        except BuildValidationError as error:
            store.update_status(job_id, "failed", failure_reason=str(error), failure_class="build_error")
            run.transition("failed", str(error))
        except PackagingError as error:
            store.update_status(job_id, "failed", failure_reason=str(error), failure_class="unknown")
            run.transition("failed", str(error))

    def _runtime_compare_config(self, job_config: dict | None) -> dict | None:
        if not isinstance(job_config, dict):
            return None
        runtime_compare = job_config.get("runtimeCompare")
        if isinstance(runtime_compare, dict):
            config = dict(runtime_compare)
            if "scenarios" not in config and "runtimeScenarios" not in config and isinstance(job_config.get("runtimeScenario"), dict):
                config["runtimeScenario"] = job_config["runtimeScenario"]
            return config
        runtime_scenario = job_config.get("runtimeScenario")
        return runtime_scenario if isinstance(runtime_scenario, dict) else None

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
            "completed_best_effort": "Pipeline completed with best-effort limitations.",
        }
        return messages.get(status, status)
