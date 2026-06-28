from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from apps.api.app.models import (
    ArtifactRecord,
    InferenceRecord,
    MemoryRecord,
    RepairInstruction,
    ReportSection,
    ReportSectionDetail,
    ReviewRun,
    RuntimeDiagnosis,
    ToolCall,
    ToolRegistryEntry,
)
from packages.knowledge import KnowledgeHit, StaticKnowledgeRetriever

from .agent_contracts import (
    AgentProviderDraft,
    AgentRunSummary,
    AgentRuntimeRequest,
    CrewConflictRecord,
    CrewStageExecution,
)


@dataclass(frozen=True)
class PersistedAgentOutputs:
    plan_artifact: ArtifactRecord
    agent_execution_artifacts: list[ArtifactRecord]
    inference_artifacts: list[ArtifactRecord]
    runtime_diagnosis_artifacts: list[ArtifactRecord]
    report_section_artifacts: list[ArtifactRecord]
    repair_instruction_artifacts: list[ArtifactRecord]
    review_artifact: ArtifactRecord
    tool_call_artifact: ArtifactRecord


class AgentArtifactWriter:
    """Persists Agent Runtime artifacts with stable lineage and schema boundaries."""

    def __init__(self, knowledge_retriever: StaticKnowledgeRetriever | None = None) -> None:
        self.knowledge_retriever = knowledge_retriever or StaticKnowledgeRetriever()

    def write_memory_artifact(
        self,
        *,
        job_id: str,
        store,
        memory_record: MemoryRecord,
        parent_artifact_ids: list[str],
    ) -> ArtifactRecord:
        return store.write_artifact(
            job_id,
            kind="memory_record",
            stage="agent_planning",
            filename=f"memory-record-{memory_record.memory_type}.json",
            content=memory_record.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.memory",
            parent_artifact_ids=parent_artifact_ids,
        )

    def write_tool_registry_artifact(
        self,
        *,
        job_id: str,
        store,
        entries: list[ToolRegistryEntry],
        parent_artifact_ids: list[str],
    ) -> ArtifactRecord:
        payload = {
            "kind": "tool_registry",
            "jobId": job_id,
            "entries": [entry.model_dump(by_alias=True) for entry in entries],
        }
        return store.write_artifact(
            job_id,
            kind="tool_registry",
            stage="agent_planning",
            filename="tool-registry.json",
            content=self._json_bytes(payload),
            content_type="application/json",
            producer="worker.tool_registry",
            parent_artifact_ids=parent_artifact_ids,
            sensitivity_class="derived",
        )

    def write_knowledge_artifact(
        self,
        *,
        job_id: str,
        store,
        hits: list[KnowledgeHit],
        parent_artifact_ids: list[str],
        prior_artifact_payloads: list[dict[str, Any]] | None = None,
        historical_artifact_payloads: list[dict[str, Any]] | None = None,
    ) -> ArtifactRecord:
        payload = self.knowledge_retriever.artifact_payload(
            job_id=job_id,
            input_artifact_ids=parent_artifact_ids,
            hits=hits,
            prior_artifact_payloads=prior_artifact_payloads,
            historical_artifact_payloads=historical_artifact_payloads,
        )
        return store.write_artifact(
            job_id,
            kind="knowledge_evidence",
            stage="agent_planning",
            filename="knowledge-evidence.json",
            content=self._json_bytes(payload),
            content_type="application/json",
            producer="worker.knowledge",
            parent_artifact_ids=parent_artifact_ids,
        )

    def persist_runtime_outputs(
        self,
        *,
        job_id: str,
        store,
        request: AgentRuntimeRequest,
        provider_draft: AgentProviderDraft,
        stages: list[CrewStageExecution],
        memory_artifact_ids: list[str],
        knowledge_artifact: ArtifactRecord,
        tool_registry_artifact: ArtifactRecord,
        started_at: float,
    ) -> PersistedAgentOutputs:
        base_input_artifact_ids = [
            *request.input_artifact_ids,
            *memory_artifact_ids,
            knowledge_artifact.id,
            tool_registry_artifact.id,
        ]
        plan_artifact = store.write_artifact(
            job_id,
            kind="agent_plan",
            stage="agent_planning",
            filename="agent-plan.json",
            content=self._json_bytes({**provider_draft.plan_payload, "toolRegistryArtifactId": tool_registry_artifact.id}),
            content_type="application/json",
            producer="worker.agent_runtime",
            parent_artifact_ids=base_input_artifact_ids,
        )

        store.update_status(job_id, "agent_pass")
        agent_execution_artifacts = self._write_agent_execution_artifacts(
            job_id=job_id,
            store=store,
            stages=stages,
            plan_artifact=plan_artifact,
            tool_registry_artifact=tool_registry_artifact,
            knowledge_artifact=knowledge_artifact,
            base_input_artifact_ids=base_input_artifact_ids,
            model_provider=provider_draft.model_provider,
            model_name=provider_draft.model_name,
        )
        stage_artifact_ids = [artifact.id for artifact in agent_execution_artifacts]
        inference_artifacts: list[ArtifactRecord] = []
        runtime_diagnosis_artifacts: list[ArtifactRecord] = []
        report_section_artifacts: list[ArtifactRecord] = []
        repair_instruction_artifacts: list[ArtifactRecord] = []
        agent_input_artifact_ids = base_input_artifact_ids
        for index, draft in enumerate(provider_draft.inferences, start=1):
            record = InferenceRecord(
                id=f"inference_{uuid4().hex[:12]}",
                job_id=job_id,
                type=draft.type,
                agent_name=draft.agent_name,
                model_provider=provider_draft.model_provider,
                model_name=provider_draft.model_name,
                prompt_version=provider_draft.prompt_version,
                input_artifact_ids=agent_input_artifact_ids,
                output_artifact_ids=[plan_artifact.id],
                evidence_refs=provider_draft.evidence_refs,
                confidence=draft.confidence,
                uncertainty_reasons=draft.uncertainty_reasons,
                alternatives=draft.alternatives,
                validation_status=draft.validation_status,
                rollback_ref=draft.rollback_ref,
            )
            inference_artifacts.append(
                store.write_artifact(
                    job_id,
                    kind="inference_record",
                    stage="agent_pass",
                    filename=f"inference-record-{index}.json",
                    content=record.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                    content_type="application/json",
                    producer="worker.agent_runtime",
                    parent_artifact_ids=[*agent_input_artifact_ids, plan_artifact.id, *stage_artifact_ids],
                )
            )

        for index, draft in enumerate(provider_draft.runtime_diagnoses, start=1):
            diagnosis = RuntimeDiagnosis(
                id=f"runtime_diagnosis_{uuid4().hex[:12]}",
                job_id=job_id,
                attempt=0,
                agent_name=draft.agent_name,
                target_stage=draft.target_stage,
                status=draft.status,
                failure_class=draft.failure_class,
                input_artifact_ids=agent_input_artifact_ids,
                evidence_refs=provider_draft.evidence_refs,
                diagnosis=draft.diagnosis,
                recommended_actions=draft.recommended_actions,
                confidence=max(0, min(1, draft.confidence)),
                uncertainty_reasons=draft.uncertainty_reasons,
            )
            runtime_diagnosis_artifacts.append(
                store.write_artifact(
                    job_id,
                    kind="runtime_diagnosis",
                    stage="agent_pass",
                    filename=f"runtime-diagnosis-{index}.json",
                    content=diagnosis.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                    content_type="application/json",
                    producer="worker.agent_runtime",
                    parent_artifact_ids=[*agent_input_artifact_ids, plan_artifact.id, *stage_artifact_ids],
                )
            )

        runtime_diagnosis_artifact_ids = [artifact.id for artifact in runtime_diagnosis_artifacts]
        for index, draft in enumerate(provider_draft.report_sections, start=1):
            report_section = ReportSection(
                id=f"report_section_{uuid4().hex[:12]}",
                job_id=job_id,
                agent_name=draft.agent_name,
                title=draft.title,
                anchor=draft.anchor,
                summary=draft.summary,
                content=draft.content,
                input_artifact_ids=agent_input_artifact_ids,
                evidence_refs=provider_draft.evidence_refs,
                status=draft.status,
                confidence=max(0, min(1, draft.confidence)),
                uncertainty_reasons=draft.uncertainty_reasons,
                details=[ReportSectionDetail(label=label, value=value) for label, value in draft.details],
            )
            report_section_artifacts.append(
                store.write_artifact(
                    job_id,
                    kind="report_section",
                    stage="agent_pass",
                    filename=f"report-section-{index}.json",
                    content=report_section.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                    content_type="application/json",
                    producer="worker.agent_runtime",
                    parent_artifact_ids=[
                        *agent_input_artifact_ids,
                        plan_artifact.id,
                        *stage_artifact_ids,
                        *runtime_diagnosis_artifact_ids,
                    ],
                )
            )

        report_section_artifact_ids = [artifact.id for artifact in report_section_artifacts]
        for index, draft in enumerate(provider_draft.repair_instructions, start=1):
            repair_instruction = RepairInstruction(
                id=f"repair_{uuid4().hex[:12]}",
                job_id=job_id,
                attempt=0,
                target_stage=draft.target_stage,  # type: ignore[arg-type]
                failure_class=draft.failure_class,
                input_artifact_ids=agent_input_artifact_ids,
                evidence_refs=provider_draft.evidence_refs,
                actions=draft.actions,
                status=draft.status,  # type: ignore[arg-type]
                risk_level=draft.risk_level,  # type: ignore[arg-type]
                decision=draft.decision,
            )
            repair_instruction_artifacts.append(
                store.write_artifact(
                    job_id,
                    kind="repair_instruction",
                    stage="agent_pass",
                    filename=f"agent-repair-instruction-{index}.json",
                    content=repair_instruction.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                    content_type="application/json",
                    producer="worker.agent_runtime",
                    parent_artifact_ids=[
                        *agent_input_artifact_ids,
                        plan_artifact.id,
                        *stage_artifact_ids,
                        *runtime_diagnosis_artifact_ids,
                        *report_section_artifact_ids,
                    ],
                )
            )

        repair_instruction_artifact_ids = [artifact.id for artifact in repair_instruction_artifacts]
        review_artifact_payload = ReviewRun(
            id=f"review_{uuid4().hex[:12]}",
            job_id=job_id,
            attempt=0,
            review_type="agent_review",
            status=provider_draft.review.status,
            decision=provider_draft.review.decision,
            failure_class=provider_draft.review.failure_class,
            evidence_refs=provider_draft.evidence_refs,
            repair_instruction_ids=[*provider_draft.review.repair_instruction_ids, *repair_instruction_artifact_ids],
            logs_artifact_id=provider_draft.review.logs_artifact_id,
        )

        review_artifact = store.write_artifact(
            job_id,
            kind="review_run",
            stage="agent_pass",
            filename="agent-review-run.json",
            content=review_artifact_payload.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.agent_runtime",
            parent_artifact_ids=[
                plan_artifact.id,
                *stage_artifact_ids,
                *[artifact.id for artifact in inference_artifacts],
                *[artifact.id for artifact in runtime_diagnosis_artifacts],
                *[artifact.id for artifact in report_section_artifacts],
                *[artifact.id for artifact in repair_instruction_artifacts],
            ],
        )

        output_artifact_ids = [
            plan_artifact.id,
            *stage_artifact_ids,
            *[artifact.id for artifact in inference_artifacts],
            *[artifact.id for artifact in runtime_diagnosis_artifacts],
            *[artifact.id for artifact in report_section_artifacts],
            *[artifact.id for artifact in repair_instruction_artifacts],
            review_artifact.id,
        ]
        tool_call = ToolCall(
            id=f"tool_call_{uuid4().hex[:12]}",
            job_id=job_id,
            caller="WorkerPipeline",
            tool_name=provider_draft.tool_name,
            tool_version=provider_draft.tool_version,
            input_artifact_ids=base_input_artifact_ids,
            output_artifact_ids=output_artifact_ids,
            status=provider_draft.tool_status,
            duration=self._duration_ms(started_at),
            failure_class=provider_draft.tool_failure_class,
        )
        tool_call_artifact = store.write_artifact(
            job_id,
            kind="tool_call",
            stage="agent_pass",
            filename="agent-tool-call.json",
            content=tool_call.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.agent_runtime",
            parent_artifact_ids=output_artifact_ids,
        )

        return PersistedAgentOutputs(
            plan_artifact=plan_artifact,
            agent_execution_artifacts=agent_execution_artifacts,
            inference_artifacts=inference_artifacts,
            runtime_diagnosis_artifacts=runtime_diagnosis_artifacts,
            report_section_artifacts=report_section_artifacts,
            repair_instruction_artifacts=repair_instruction_artifacts,
            review_artifact=review_artifact,
            tool_call_artifact=tool_call_artifact,
        )

    def _write_agent_execution_artifacts(
        self,
        *,
        job_id: str,
        store,
        stages: list[CrewStageExecution],
        plan_artifact: ArtifactRecord,
        tool_registry_artifact: ArtifactRecord,
        knowledge_artifact: ArtifactRecord,
        base_input_artifact_ids: list[str],
        model_provider: str,
        model_name: str,
    ) -> list[ArtifactRecord]:
        artifacts: list[ArtifactRecord] = []
        for stage_index, stage in enumerate(stages, start=1):
            stage_payload = {
                "kind": "agent_execution",
                "jobId": job_id,
                "stage": stage.stage,
                "status": stage.status,
                "failureClass": stage.failure_class,
                "durationMs": stage.duration_ms,
                "notes": list(stage.notes),
                "conflicts": [self._conflict_payload(conflict) for conflict in stage.conflict_summary],
                "agents": [],
            }
            for execution in stage.agent_executions:
                execution_payload = {
                    "name": execution.spec.name,
                    "responsibility": execution.spec.responsibility,
                    "outputKind": execution.spec.output_kind,
                    "allowParallel": execution.spec.allow_parallel,
                    "dependencies": execution.spec.dependencies,
                    "status": execution.status,
                    "failureClass": execution.failure_class,
                    "attempt": execution.attempt,
                    "durationMs": execution.duration_ms,
                    "modelProvider": execution.model_provider or model_provider,
                    "modelName": execution.model_name or model_name,
                    "inputArtifactIds": execution.input_artifact_ids,
                    "evidenceRefs": [ref.model_dump(by_alias=True, exclude_none=True) for ref in execution.evidence_refs],
                    "message": execution.message,
                    "rawOutput": execution.raw_output,
                    "inferenceCount": len(execution.inferences),
                    "runtimeDiagnosisCount": len(execution.runtime_diagnoses),
                    "reportSectionCount": len(execution.report_sections),
                    "repairInstructionCount": len(execution.repair_instructions),
                    "reviewStatus": execution.review.status if execution.review is not None else None,
                }
                stage_payload["agents"].append(execution_payload)
                artifacts.append(
                    store.write_artifact(
                        job_id,
                        kind="agent_execution",
                        stage="agent_pass",
                        filename=f"agent-execution-{execution.spec.name.lower()}.json",
                        content=self._json_bytes(
                            {
                                "kind": "agent_execution",
                                "jobId": job_id,
                                "stage": stage.stage,
                                "stageStatus": stage.status,
                                "stageFailureClass": stage.failure_class,
                                "stageDurationMs": stage.duration_ms,
                                "toolRegistryArtifactId": tool_registry_artifact.id,
                                "knowledgeEvidenceArtifactId": knowledge_artifact.id,
                                **execution_payload,
                            }
                        ),
                        content_type="application/json",
                        producer="worker.agent_runtime",
                        parent_artifact_ids=[
                            *base_input_artifact_ids,
                            plan_artifact.id,
                            tool_registry_artifact.id,
                            knowledge_artifact.id,
                        ],
                    )
                )
            artifacts.append(
                store.write_artifact(
                    job_id,
                    kind="agent_execution",
                    stage="agent_pass",
                    filename=f"agent-stage-{stage_index}-{stage.stage}.json",
                    content=self._json_bytes(stage_payload),
                    content_type="application/json",
                    producer="worker.agent_runtime",
                    parent_artifact_ids=[
                        *base_input_artifact_ids,
                        plan_artifact.id,
                        tool_registry_artifact.id,
                        knowledge_artifact.id,
                    ],
                )
            )
        return artifacts

    def _conflict_payload(self, conflict: CrewConflictRecord) -> dict[str, Any]:
        return {
            "key": conflict.key,
            "severity": conflict.severity,
            "agents": list(conflict.agents),
            "summary": conflict.summary,
            "evidenceRefs": list(conflict.evidence_refs),
        }

    def _duration_ms(self, started_at: float) -> float:
        return round((time.perf_counter() - started_at) * 1000, 3)

    def _json_bytes(self, payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


__all__ = [
    "AgentArtifactWriter",
    "AgentRunSummary",
    "PersistedAgentOutputs",
]
