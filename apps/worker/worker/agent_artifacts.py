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

from .agent_contracts import AgentProviderDraft, AgentRuntimeRequest


@dataclass(frozen=True)
class PersistedAgentOutputs:
    plan_artifact: ArtifactRecord
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

    def persist_provider_outputs(
        self,
        *,
        job_id: str,
        store,
        request: AgentRuntimeRequest,
        provider_draft: AgentProviderDraft,
        memory_artifact_ids: list[str],
        knowledge_artifact: ArtifactRecord,
        tool_registry_artifact: ArtifactRecord,
        started_at: float,
    ) -> PersistedAgentOutputs:
        provider_plan_payload = {
            **provider_draft.plan_payload,
            "toolRegistryArtifactId": tool_registry_artifact.id,
        }
        plan_artifact = store.write_artifact(
            job_id,
            kind="agent_plan",
            stage="agent_planning",
            filename="agent-plan.json",
            content=self._json_bytes(provider_plan_payload),
            content_type="application/json",
            producer="worker.agent_runtime",
            parent_artifact_ids=[
                *request.input_artifact_ids,
                *memory_artifact_ids,
                knowledge_artifact.id,
                tool_registry_artifact.id,
            ],
        )

        store.update_status(job_id, "agent_pass")
        agent_input_artifact_ids = [
            *request.input_artifact_ids,
            *memory_artifact_ids,
            knowledge_artifact.id,
            tool_registry_artifact.id,
        ]
        inference_artifacts: list[ArtifactRecord] = []
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
                    parent_artifact_ids=[*agent_input_artifact_ids, plan_artifact.id],
                )
            )

        inference_artifact_ids = [artifact.id for artifact in inference_artifacts]
        runtime_diagnosis_artifacts: list[ArtifactRecord] = []
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
                    parent_artifact_ids=[*agent_input_artifact_ids, plan_artifact.id],
                )
            )
        runtime_diagnosis_artifact_ids = [artifact.id for artifact in runtime_diagnosis_artifacts]

        report_section_artifacts: list[ArtifactRecord] = []
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
                    parent_artifact_ids=[*agent_input_artifact_ids, plan_artifact.id, *runtime_diagnosis_artifact_ids],
                )
            )
        report_section_artifact_ids = [artifact.id for artifact in report_section_artifacts]

        repair_instruction_artifacts: list[ArtifactRecord] = []
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
                        *runtime_diagnosis_artifact_ids,
                        *report_section_artifact_ids,
                    ],
                )
            )
        repair_instruction_artifact_ids = [artifact.id for artifact in repair_instruction_artifacts]

        review_run = ReviewRun(
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
            content=review_run.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
            content_type="application/json",
            producer="worker.agent_runtime",
            parent_artifact_ids=[
                plan_artifact.id,
                *inference_artifact_ids,
                *runtime_diagnosis_artifact_ids,
                *report_section_artifact_ids,
                *repair_instruction_artifact_ids,
            ],
        )

        output_artifact_ids = [
            plan_artifact.id,
            *inference_artifact_ids,
            *runtime_diagnosis_artifact_ids,
            *report_section_artifact_ids,
            *repair_instruction_artifact_ids,
            review_artifact.id,
        ]
        tool_call = ToolCall(
            id=f"tool_call_{uuid4().hex[:12]}",
            job_id=job_id,
            caller="WorkerPipeline",
            tool_name=provider_draft.tool_name,
            tool_version=provider_draft.tool_version,
            input_artifact_ids=agent_input_artifact_ids,
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
            inference_artifacts=inference_artifacts,
            runtime_diagnosis_artifacts=runtime_diagnosis_artifacts,
            report_section_artifacts=report_section_artifacts,
            repair_instruction_artifacts=repair_instruction_artifacts,
            review_artifact=review_artifact,
            tool_call_artifact=tool_call_artifact,
        )

    def _duration_ms(self, started_at: float) -> float:
        return round((time.perf_counter() - started_at) * 1000, 3)

    def _json_bytes(self, payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
