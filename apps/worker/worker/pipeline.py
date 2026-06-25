from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .agent_runtime import AgentRuntime, AgentRuntimeError, AgentRuntimeRequest
from .build_validation import BuildValidationError, BuildValidationRunner
from .core_bridge import CoreBridge, CoreBridgeError
from .reconstruction import ReconstructionError, ReconstructionRunner
from .runtime_smoke import RuntimeCompareRepairRunner, RuntimeCompareReviewGate, RuntimeCompareRunner, RuntimeSmokeRunner
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


class PipelineCancelled(RuntimeError):
    pass


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
        runtime_compare_repair_runner: RuntimeCompareRepairRunner | None = None,
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
        self.runtime_compare_repair_runner = runtime_compare_repair_runner or RuntimeCompareRepairRunner()
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
            self._raise_if_cancelled(job_id=job_id, store=store)
            store.update_status(job_id, "intake")
            run.transition("intake", "Core input inventory generation started.")
            result = self.core_bridge.analyze_input_package(job_id=job_id, input_path=input_path)
            self._raise_if_cancelled(job_id=job_id, store=store)
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
            self._raise_if_cancelled(job_id=job_id, store=store)
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
            self._raise_if_cancelled(job_id=job_id, store=store)

            evidence_parent_ids = [
                inventory_artifact.id,
                ast_artifact.id,
                agent_result.plan_artifact.id,
                *[artifact.id for artifact in agent_result.memory_artifacts],
                agent_result.knowledge_artifact.id,
                agent_result.tool_registry_artifact.id,
                *[artifact.id for artifact in agent_result.inference_artifacts],
                *[artifact.id for artifact in agent_result.runtime_diagnosis_artifacts],
                *[artifact.id for artifact in agent_result.report_section_artifacts],
                *[artifact.id for artifact in agent_result.repair_instruction_artifacts],
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
            self._raise_if_cancelled(job_id=job_id, store=store)

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
            self._raise_if_cancelled(job_id=job_id, store=store)

            current_project_artifact = (
                self._latest_artifact(store=store, job_id=job_id, kind="generated_project")
                or reconstruction_result.generated_project_artifact
            )
            packaging_parent_ids = self._unique_ids([*validation_parent_ids, *build_validation_result.artifact_ids])
            runtime_compare_config = self._runtime_compare_config(job.config)
            runtime_compare_max_attempts = self._runtime_compare_max_attempts(job.config)

            runtime_attempt = 0
            runtime_compare_attempts: list[dict] = []
            runtime_compare_stop_reason = "unknown"
            while True:
                with self._artifact_directory_path(store=store, artifact=current_project_artifact) as current_project_path:
                    runtime_result = self.runtime_smoke_runner.run(
                        job_id=job_id,
                        input_path=current_project_path,
                        store=store,
                        parent_artifact_ids=packaging_parent_ids,
                        attempt=runtime_attempt,
                    )
                    run.transition("runtime_smoke", runtime_result.message)
                    self._raise_if_cancelled(job_id=job_id, store=store)
                    packaging_parent_ids = self._unique_ids([*packaging_parent_ids, *runtime_result.artifact_ids])
                    runtime_compare_result = self.runtime_compare_runner.run_compare(
                        job_id=job_id,
                        store=store,
                        original_input_path=input_path,
                        reconstructed_input_path=current_project_path,
                        scenario_config=runtime_compare_config,
                        parent_artifact_ids=packaging_parent_ids,
                        attempt=runtime_attempt,
                    )
                    run.transition("runtime_compare", runtime_compare_result.message)
                    self._raise_if_cancelled(job_id=job_id, store=store)
                    packaging_parent_ids = self._unique_ids([*packaging_parent_ids, *runtime_compare_result.artifact_ids])
                    attempt_summary = {
                        "attempt": runtime_attempt,
                        "comparisonArtifactIds": [artifact.id for artifact in runtime_compare_result.comparison_artifacts],
                        "validationArtifactIds": [artifact.id for artifact in runtime_compare_result.report_artifacts],
                        "traceArtifactIds": [artifact.id for artifact in runtime_compare_result.trace_artifacts],
                        "scenarioArtifactIds": [artifact.id for artifact in runtime_compare_result.scenario_artifacts],
                        "screenshotArtifactIds": [artifact.id for artifact in runtime_compare_result.screenshot_artifacts],
                        "compareMessage": runtime_compare_result.message,
                    }
                    runtime_compare_gate_result = self.runtime_compare_review_gate.run(
                        job_id=job_id,
                        store=store,
                        comparison_artifacts=runtime_compare_result.comparison_artifacts,
                        job_config=job.config,
                        parent_artifact_ids=runtime_compare_result.artifact_ids,
                        attempt=runtime_attempt,
                    )
                    if runtime_compare_gate_result.enabled:
                        run.transition("reviewing", runtime_compare_gate_result.message)
                        if runtime_compare_gate_result.triggered and runtime_attempt + 1 < runtime_compare_max_attempts:
                            run.transition(
                                "repairing",
                                "Runtime compare review produced repair evidence for follow-up Review/Fix.",
                            )
                    attempt_summary.update(
                        {
                            "reviewGateEnabled": runtime_compare_gate_result.enabled,
                            "reviewGateTriggered": runtime_compare_gate_result.triggered,
                            "reviewArtifactId": runtime_compare_gate_result.review_artifact.id
                            if runtime_compare_gate_result.review_artifact is not None
                            else None,
                            "plannedRepairArtifactId": runtime_compare_gate_result.repair_artifact.id
                            if runtime_compare_gate_result.repair_artifact is not None
                            else None,
                            "reviewMessage": runtime_compare_gate_result.message,
                            "repairArtifactId": None,
                            "appliedProjectArtifactId": None,
                            "repairMessage": None,
                        }
                    )
                    runtime_compare_attempts.append(attempt_summary)
                    self._raise_if_cancelled(job_id=job_id, store=store)
                    packaging_parent_ids = self._unique_ids([*packaging_parent_ids, *runtime_compare_gate_result.artifact_ids])

                if not (runtime_compare_gate_result.enabled and runtime_compare_gate_result.triggered):
                    runtime_compare_stop_reason = "review_gate_passed_or_disabled"
                    break
                if runtime_attempt + 1 >= runtime_compare_max_attempts:
                    runtime_compare_stop_reason = "retry_budget_exhausted"
                    break

                runtime_repair_result = self.runtime_compare_repair_runner.run(
                    job_id=job_id,
                    store=store,
                    generated_project_artifact=current_project_artifact,
                    planned_repair_artifact=runtime_compare_gate_result.repair_artifact,
                    job_config=job.config,
                    parent_artifact_ids=packaging_parent_ids,
                    attempt=runtime_attempt + 1,
                )
                run.transition("repairing", runtime_repair_result.message)
                self._raise_if_cancelled(job_id=job_id, store=store)
                packaging_parent_ids = self._unique_ids([*packaging_parent_ids, *runtime_repair_result.artifact_ids])
                runtime_compare_attempts[-1].update(
                    {
                        "repairArtifactId": runtime_repair_result.repair_artifact.id
                        if runtime_repair_result.repair_artifact is not None
                        else None,
                        "appliedProjectArtifactId": runtime_repair_result.applied_project_artifact.id
                        if runtime_repair_result.applied_project_artifact is not None
                        else None,
                        "repairMessage": runtime_repair_result.message,
                    }
                )

                if runtime_repair_result.applied_project_artifact is None:
                    runtime_compare_stop_reason = "repair_not_applied"
                    break

                current_project_artifact = runtime_repair_result.applied_project_artifact
                runtime_attempt += 1

            retry_summary_artifact = self._write_runtime_compare_retry_summary(
                job_id=job_id,
                store=store,
                max_attempts=runtime_compare_max_attempts,
                attempts=runtime_compare_attempts,
                stop_reason=runtime_compare_stop_reason,
                current_project_artifact_id=current_project_artifact.id if current_project_artifact is not None else None,
                parent_artifact_ids=packaging_parent_ids,
            )
            packaging_parent_ids = self._unique_ids([*packaging_parent_ids, retry_summary_artifact.id])
            convergence_summary_artifact = self._write_review_fix_convergence_summary(
                job_id=job_id,
                store=store,
                build_validation_result=build_validation_result,
                agent_review_artifact_id=agent_result.review_artifact.id,
                agent_repair_artifact_ids=[artifact.id for artifact in agent_result.repair_instruction_artifacts],
                runtime_retry_summary_artifact_id=retry_summary_artifact.id,
                runtime_compare_attempts=runtime_compare_attempts,
                runtime_compare_stop_reason=runtime_compare_stop_reason,
                runtime_compare_max_attempts=runtime_compare_max_attempts,
                current_project_artifact_id=current_project_artifact.id if current_project_artifact is not None else None,
                job_config=job.config,
                parent_artifact_ids=packaging_parent_ids,
            )
            packaging_parent_ids = self._unique_ids([*packaging_parent_ids, convergence_summary_artifact.id])
            self._raise_if_cancelled(job_id=job_id, store=store)
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
        except PipelineCancelled as error:
            run.transition("cancelled", str(error))
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

    def _write_runtime_compare_retry_summary(
        self,
        *,
        job_id: str,
        store,
        max_attempts: int,
        attempts: list[dict],
        stop_reason: str,
        current_project_artifact_id: str | None,
        parent_artifact_ids: list[str],
    ):
        statuses = [
            "fail" if attempt.get("reviewGateTriggered") else "pass"
            for attempt in attempts
            if attempt.get("reviewGateEnabled")
        ]
        payload = {
            "kind": "runtime_trace",
            "jobId": job_id,
            "target": "runtime_compare_retry_summary",
            "maxAttempts": max_attempts,
            "attemptsUsed": len(attempts),
            "budgetExhausted": stop_reason == "retry_budget_exhausted",
            "stoppedReason": stop_reason,
            "finalProjectArtifactId": current_project_artifact_id,
            "finalReviewStatus": statuses[-1] if statuses else "best_effort",
            "attempts": attempts,
        }
        return store.write_artifact(
            job_id,
            kind="runtime_trace",
            stage="runtime_compare",
            filename="runtime-compare-retry-summary.json",
            content=self._json_bytes(payload),
            content_type="application/json",
            producer="worker.pipeline",
            parent_artifact_ids=parent_artifact_ids,
        )

    def _write_review_fix_convergence_summary(
        self,
        *,
        job_id: str,
        store,
        build_validation_result,
        agent_review_artifact_id: str,
        agent_repair_artifact_ids: list[str],
        runtime_retry_summary_artifact_id: str,
        runtime_compare_attempts: list[dict],
        runtime_compare_stop_reason: str,
        runtime_compare_max_attempts: int,
        current_project_artifact_id: str | None,
        job_config: dict | None,
        parent_artifact_ids: list[str],
    ):
        build_summary = self._build_validation_convergence_summary(build_validation_result)
        runtime_summary = self._runtime_compare_convergence_summary(
            max_attempts=runtime_compare_max_attempts,
            attempts=runtime_compare_attempts,
            stop_reason=runtime_compare_stop_reason,
            current_project_artifact_id=current_project_artifact_id,
            retry_summary_artifact_id=runtime_retry_summary_artifact_id,
        )
        final_outcome = self._review_fix_final_outcome(build_summary=build_summary, runtime_summary=runtime_summary)
        policy = self._review_fix_policy(job_config=job_config, runtime_compare_max_attempts=runtime_compare_max_attempts)
        failure_mapping = self._review_fix_failure_mapping(build_summary=build_summary, runtime_summary=runtime_summary)
        next_steps = self._review_fix_next_steps(
            final_outcome=final_outcome,
            build_summary=build_summary,
            runtime_summary=runtime_summary,
            failure_mapping=failure_mapping,
        )
        evidence_links = self._unique_ids(
            [
                *build_summary["evidenceArtifactIds"],
                agent_review_artifact_id,
                *agent_repair_artifact_ids,
                runtime_retry_summary_artifact_id,
                *runtime_summary["evidenceArtifactIds"],
                current_project_artifact_id,
            ]
        )
        payload = {
            "kind": "runtime_trace",
            "jobId": job_id,
            "target": "review_fix_convergence_summary",
            "finalOutcome": final_outcome,
            "status": "pass" if final_outcome in {"passed_without_repair", "repaired_passed"} else "best_effort",
            "failureClass": "none" if final_outcome in {"passed_without_repair", "repaired_passed"} else "runtime_error",
            "policy": policy,
            "failureActionMap": failure_mapping,
            "nextSteps": next_steps,
            "buildTypecheck": build_summary,
            "runtimeCompare": runtime_summary,
            "agentReview": {
                "reviewArtifactId": agent_review_artifact_id,
                "repairInstructionIds": agent_repair_artifact_ids,
                "repairInstructionCount": len(agent_repair_artifact_ids),
            },
            "evidenceArtifactIds": evidence_links,
            "evidenceLinks": [f"artifact://{artifact_id}" for artifact_id in evidence_links],
        }
        return store.write_artifact(
            job_id,
            kind="runtime_trace",
            stage="reviewing",
            filename="review-fix-convergence-summary.json",
            content=self._json_bytes(payload),
            content_type="application/json",
            producer="worker.pipeline",
            parent_artifact_ids=parent_artifact_ids,
        )

    def _build_validation_convergence_summary(self, build_validation_result) -> dict:
        stage_results = [build_validation_result.build, build_validation_result.typecheck]
        repair_artifacts = build_validation_result.repair_artifacts
        applied_project_ids = [
            artifact.id for artifact in repair_artifacts if artifact.kind == "generated_project"
        ]
        repair_instruction_ids = [
            artifact.id for artifact in repair_artifacts if artifact.kind == "repair_instruction"
        ]
        statuses = [stage.review_run.status for stage in stage_results]
        failed_stages = [stage for stage in stage_results if stage.review_run.status in {"fail", "retry", "best_effort"}]
        return {
            "maxAttempt": max(stage.review_run.attempt for stage in stage_results),
            "latestStatusByReviewType": {
                stage.review_run.review_type: stage.review_run.status for stage in stage_results
            },
            "allPassed": all(status == "pass" for status in statuses),
            "needsAttention": any(status in {"retry", "best_effort", "fail"} for status in statuses),
            "failedReviewTypes": [stage.review_run.review_type for stage in failed_stages],
            "failureClasses": sorted(
                {
                    stage.review_run.failure_class
                    for stage in failed_stages
                    if stage.review_run.failure_class != "none"
                }
            ),
            "repairInstructionIds": repair_instruction_ids,
            "repairInstructionCount": len(repair_instruction_ids),
            "appliedProjectArtifactIds": applied_project_ids,
            "appliedRepairCount": len(applied_project_ids),
            "evidenceArtifactIds": self._unique_ids(
                [
                    *build_validation_result.artifact_ids,
                ]
            ),
        }

    def _runtime_compare_convergence_summary(
        self,
        *,
        max_attempts: int,
        attempts: list[dict],
        stop_reason: str,
        current_project_artifact_id: str | None,
        retry_summary_artifact_id: str,
    ) -> dict:
        triggered = [attempt for attempt in attempts if attempt.get("reviewGateTriggered")]
        applied_repairs = [attempt for attempt in attempts if attempt.get("appliedProjectArtifactId")]
        planned_repairs = [attempt for attempt in attempts if attempt.get("plannedRepairArtifactId")]
        final_review_status = "fail" if attempts and attempts[-1].get("reviewGateTriggered") else "pass"
        if attempts and not attempts[-1].get("reviewGateEnabled"):
            final_review_status = "best_effort"
        return {
            "maxAttempts": max_attempts,
            "attemptsUsed": len(attempts),
            "budgetExhausted": stop_reason == "retry_budget_exhausted",
            "stoppedReason": stop_reason,
            "finalReviewStatus": final_review_status,
            "finalProjectArtifactId": current_project_artifact_id,
            "plannedRepairCount": len(planned_repairs),
            "appliedRepairCount": len(applied_repairs),
            "triggeredReviewCount": len(triggered),
            "failureClasses": ["runtime_error"] if triggered else [],
            "attempts": attempts,
            "evidenceArtifactIds": self._unique_ids(
                [
                    retry_summary_artifact_id,
                    *[
                        artifact_id
                        for attempt in attempts
                        for artifact_id in (
                            *attempt.get("comparisonArtifactIds", []),
                            *attempt.get("validationArtifactIds", []),
                            *attempt.get("traceArtifactIds", []),
                            *attempt.get("scenarioArtifactIds", []),
                            *attempt.get("screenshotArtifactIds", []),
                            attempt.get("reviewArtifactId"),
                            attempt.get("plannedRepairArtifactId"),
                            attempt.get("repairArtifactId"),
                            attempt.get("appliedProjectArtifactId"),
                        )
                        if artifact_id
                    ],
                ]
            ),
        }

    def _review_fix_final_outcome(self, *, build_summary: dict, runtime_summary: dict) -> str:
        if runtime_summary["budgetExhausted"]:
            return "budget_exhausted_best_effort"
        if runtime_summary["stoppedReason"] == "repair_not_applied":
            return "no_deterministic_repair"
        if runtime_summary["appliedRepairCount"] > 0 and runtime_summary["finalReviewStatus"] == "pass":
            return "repaired_passed"
        if (
            build_summary["appliedRepairCount"] > 0
            and build_summary["allPassed"]
            and runtime_summary["finalReviewStatus"] == "pass"
        ):
            return "repaired_passed"
        if (
            build_summary["repairInstructionCount"] == 0
            and runtime_summary["plannedRepairCount"] == 0
            and runtime_summary["finalReviewStatus"] == "pass"
            and build_summary["allPassed"]
        ):
            return "passed_without_repair"
        return "best_effort_with_limitations"

    def _review_fix_policy(self, *, job_config: dict | None, runtime_compare_max_attempts: int) -> dict:
        review_fix = job_config.get("reviewFix") if isinstance(job_config, dict) else None
        review_fix = review_fix if isinstance(review_fix, dict) else {}
        build_validation = job_config.get("buildValidation") if isinstance(job_config, dict) else None
        build_validation = build_validation if isinstance(build_validation, dict) else {}
        runtime_compare = job_config.get("runtimeCompare") if isinstance(job_config, dict) else None
        runtime_compare = runtime_compare if isinstance(runtime_compare, dict) else {}
        build_review_fix = review_fix.get("buildValidation") if isinstance(review_fix.get("buildValidation"), dict) else {}
        runtime_review_fix = review_fix.get("runtimeCompare") if isinstance(review_fix.get("runtimeCompare"), dict) else {}
        allowed_actions = self._review_fix_allowed_actions(review_fix, build_review_fix, runtime_review_fix, build_validation)
        allow_low_risk = self._bool_config(
            review_fix.get("allowLowRiskRepairs"),
            default=True,
        )
        return {
            "source": "job.config.reviewFix",
            "allowLowRiskRepairs": allow_low_risk,
            "allowedRepairActions": allowed_actions,
            "buildValidation": {
                "maxAttempts": self._int_config(
                    build_validation.get("maxAttempts", build_review_fix.get("maxAttempts", review_fix.get("maxAttempts"))),
                    default=2,
                    minimum=1,
                    maximum=5,
                ),
                "allowLowRiskRepairs": self._bool_config(
                    build_validation.get(
                        "allowLowRiskRepairs",
                        build_review_fix.get("allowLowRiskRepairs", allow_low_risk),
                    ),
                    default=True,
                ),
                "allowedRepairActions": self._review_fix_allowed_actions(
                    review_fix,
                    build_review_fix,
                    {},
                    build_validation,
                ),
            },
            "runtimeCompare": {
                "maxAttempts": runtime_compare_max_attempts,
                "allowLowRiskRepairs": self._bool_config(
                    runtime_review_fix.get("allowLowRiskRepairs", allow_low_risk),
                    default=True,
                ),
                "allowedRepairActions": self._review_fix_allowed_actions(review_fix, {}, runtime_review_fix, {}),
                "reviewGate": runtime_compare.get("reviewGate") if isinstance(runtime_compare.get("reviewGate"), dict) else {},
            },
            "auditOnlyRiskLevels": ["medium", "high"],
            "consumptionPolicy": "Only low-risk supported repair actions are applied automatically; medium and high risk actions remain audit-only next-step guidance.",
        }

    def _review_fix_allowed_actions(self, *configs: dict) -> list[str]:
        for config in configs:
            value = config.get("allowedRepairActions") if isinstance(config, dict) else None
            if isinstance(value, list):
                actions = [
                    item
                    for item in value
                    if item in {"add_package_script", "replace_package_script", "mirror_original_static_entry"}
                ]
                return list(dict.fromkeys(actions))
        return ["add_package_script", "replace_package_script", "mirror_original_static_entry"]

    def _review_fix_failure_mapping(self, *, build_summary: dict, runtime_summary: dict) -> list[dict]:
        mappings = [
            {
                "failureClass": "build_error",
                "targetStage": "building",
                "automaticActions": ["add_package_script", "replace_package_script"],
                "auditOnlyActions": ["inspect build log, generated package.json, and deterministic build shim output"],
                "status": "active" if "build_error" in build_summary.get("failureClasses", []) else "available",
                "evidenceArtifactIds": build_summary.get("evidenceArtifactIds", []),
            },
            {
                "failureClass": "type_error",
                "targetStage": "typechecking",
                "automaticActions": ["add_package_script", "replace_package_script"],
                "auditOnlyActions": ["inspect TypeScript diagnostics and generated typecheck shim output"],
                "status": "active" if "type_error" in build_summary.get("failureClasses", []) else "available",
                "evidenceArtifactIds": build_summary.get("evidenceArtifactIds", []),
            },
            {
                "failureClass": "dependency_missing",
                "targetStage": "building",
                "automaticActions": [],
                "auditOnlyActions": ["enable buildValidation.installDependencies only in an approved isolated runner"],
                "status": "active" if "dependency_missing" in build_summary.get("failureClasses", []) else "available",
                "evidenceArtifactIds": build_summary.get("evidenceArtifactIds", []),
            },
            {
                "failureClass": "runtime_error",
                "targetStage": "runtime_compare",
                "automaticActions": ["mirror_original_static_entry"],
                "auditOnlyActions": ["review DOM, network, console, and screenshot diffs by scenario/viewport"],
                "status": "active" if runtime_summary.get("triggeredReviewCount", 0) else "available",
                "evidenceArtifactIds": runtime_summary.get("evidenceArtifactIds", []),
            },
            {
                "failureClass": "invalid_input",
                "targetStage": "runtime_smoke",
                "automaticActions": [],
                "auditOnlyActions": ["provide an HTML entry or configure a runtime scenario entry"],
                "status": "available",
                "evidenceArtifactIds": [],
            },
        ]
        return mappings

    def _review_fix_next_steps(
        self,
        *,
        final_outcome: str,
        build_summary: dict,
        runtime_summary: dict,
        failure_mapping: list[dict],
    ) -> list[str]:
        if final_outcome in {"passed_without_repair", "repaired_passed"}:
            return ["No user action is required; retain the review-fix summary with the result package for audit."]
        steps: list[str] = []
        if runtime_summary.get("budgetExhausted"):
            steps.append(
                "Runtime compare retry budget was exhausted; inspect the last scenario/viewport diff and raise runtimeCompare.maxAttempts only after confirming the repair is converging."
            )
        if runtime_summary.get("stoppedReason") == "repair_not_applied":
            steps.append(
                "No low-risk runtime repair was applied; review planned repair instructions and keep medium/high risk changes manual."
            )
        if build_summary.get("needsAttention"):
            steps.append(
                "Build/typecheck still needs attention; inspect build_artifact diagnostics and repair instructions before rerun."
            )
        active_mappings = [item for item in failure_mapping if item.get("status") == "active"]
        for mapping in active_mappings[:3]:
            automatic_actions = mapping.get("automaticActions") if isinstance(mapping.get("automaticActions"), list) else []
            audit_actions = mapping.get("auditOnlyActions") if isinstance(mapping.get("auditOnlyActions"), list) else []
            steps.append(
                f"{mapping.get('failureClass')} at {mapping.get('targetStage')}: automatic={', '.join(automatic_actions) or 'none'}; next={'; '.join(audit_actions) or 'review evidence'}."
            )
        if not steps:
            steps.append("Review best-effort limitations in the evidence index before treating the result as complete.")
        return steps

    def _bool_config(self, value, *, default: bool) -> bool:
        return value if isinstance(value, bool) else default

    def _int_config(self, value, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

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

    def _runtime_compare_max_attempts(self, job_config: dict | None) -> int:
        if not isinstance(job_config, dict):
            return 2
        runtime_compare = job_config.get("runtimeCompare")
        review_fix = job_config.get("reviewFix")
        review_fix = review_fix if isinstance(review_fix, dict) else {}
        review_fix_runtime = review_fix.get("runtimeCompare") if isinstance(review_fix.get("runtimeCompare"), dict) else {}
        if isinstance(runtime_compare, dict):
            max_attempts_value = runtime_compare.get(
                "maxAttempts",
                review_fix_runtime.get("maxAttempts", review_fix.get("maxAttempts", 2)),
            )
        else:
            max_attempts_value = review_fix_runtime.get("maxAttempts", review_fix.get("maxAttempts", 2))
        try:
            max_attempts = int(max_attempts_value)
        except (TypeError, ValueError):
            max_attempts = 2
        return max(1, min(max_attempts, 5))

    def _raise_if_cancelled(self, *, job_id: str, store) -> None:
        job = store.get_job(job_id)
        if job is not None and job.status == "cancelled":
            raise PipelineCancelled(job.failure_reason or "Job cancelled.")

    def _latest_artifact(self, *, store, job_id: str, kind: str):
        artifacts = store.list_artifacts(job_id, kind=kind)
        return artifacts[-1] if artifacts else None

    @contextmanager
    def _artifact_directory_path(self, *, store, artifact) -> Iterator[Path]:
        local_path = store.artifact_local_path(artifact)
        if local_path is not None and local_path.is_dir():
            yield local_path
            return
        with tempfile.TemporaryDirectory(prefix="ai-jsunpack-pipeline-artifact-") as temp_dir:
            yield store.materialize_artifact_directory(artifact, Path(temp_dir) / "artifact")

    def _unique_ids(self, artifact_ids: list[str]) -> list[str]:
        return list(dict.fromkeys(artifact_ids))

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
