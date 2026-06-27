from __future__ import annotations

import json
import time
from typing import Any

from packages.knowledge import POST_CORE_KINDS, StaticKnowledgeRetriever
from packages.memory import JobMemoryService

from .agent_artifacts import AgentArtifactWriter
from .agent_context import AgentContextBuilder, AgentContextRedactor
from .agent_contracts import (
    AGENT_PROMPT_VERSION,
    AGENT_TOOL_VERSION,
    AgentFeedbackRefinement,
    AgentInferenceDraft,
    AgentModelPolicy,
    AgentProvider,
    AgentProviderDraft,
    AgentRepairInstructionDraft,
    AgentReportSectionDraft,
    AgentReviewDraft,
    AgentRuntimeDiagnosisDraft,
    AgentRuntimeError,
    AgentRuntimeRequest,
    AgentRuntimeResult,
    CrewAgentPassOutput,
    CrewInferenceOutput,
)
from .agent_feedback import AgentFeedbackRefiner
from .agent_providers import CrewAIAgentProvider, ModelPolicyResolver
from .agent_tools import AgentToolRegistryBuilder, ToolSpec


class AgentRuntime:
    def __init__(
        self,
        provider: AgentProvider | None = None,
        memory_service: JobMemoryService | None = None,
        knowledge_retriever: StaticKnowledgeRetriever | None = None,
        context_builder: AgentContextBuilder | None = None,
        tool_registry_builder: AgentToolRegistryBuilder | None = None,
        artifact_writer: AgentArtifactWriter | None = None,
    ) -> None:
        self.provider = provider or CrewAIAgentProvider()
        self.memory_service = memory_service or JobMemoryService()
        self.knowledge_retriever = knowledge_retriever or StaticKnowledgeRetriever()
        self.context_builder = context_builder or AgentContextBuilder()
        self.tool_registry_builder = tool_registry_builder or AgentToolRegistryBuilder()
        self.artifact_writer = artifact_writer or AgentArtifactWriter(self.knowledge_retriever)

    def run(self, *, job_id: str, store, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        started_at = time.perf_counter()
        try:
            store.update_status(job_id, "agent_planning")
            prior_artifact_payloads = self._current_job_artifact_payloads(job_id=job_id, store=store)
            historical_artifact_payloads = self._historical_project_artifact_payloads(
                job_id=job_id,
                project_id=request.project_id,
                store=store,
            )
            historical_artifact_ids = [
                artifact_id
                for artifact_id in (payload.get("artifactId") for payload in historical_artifact_payloads)
                if isinstance(artifact_id, str) and artifact_id
            ]
            memory_context = self.memory_service.create_context(
                job_id=job_id,
                project_id=request.project_id,
                source_artifact_ids=request.input_artifact_ids,
                inventory_payload=request.inventory_payload,
                ast_index_payload=request.ast_index_payload,
                cloud_mode=request.cloud_mode,
            )
            memory_artifacts = [
                self.artifact_writer.write_memory_artifact(
                    job_id=job_id,
                    store=store,
                    memory_record=memory_record,
                    parent_artifact_ids=request.input_artifact_ids,
                )
                for memory_record in memory_context.records
            ]
            memory_artifact_ids = [artifact.id for artifact in memory_artifacts]
            tool_registry_entries = self.tool_registry_builder.entries(job_id)
            tool_registry_artifact = self.artifact_writer.write_tool_registry_artifact(
                job_id=job_id,
                store=store,
                entries=tool_registry_entries,
                parent_artifact_ids=request.input_artifact_ids,
            )
            knowledge_hits = self.knowledge_retriever.retrieve(
                inventory_payload=request.inventory_payload,
                ast_index_payload=request.ast_index_payload,
                prior_artifact_payloads=prior_artifact_payloads,
                historical_artifact_payloads=historical_artifact_payloads,
            )
            knowledge_artifact = self.artifact_writer.write_knowledge_artifact(
                job_id=job_id,
                store=store,
                hits=knowledge_hits,
                parent_artifact_ids=[
                    *request.input_artifact_ids,
                    *memory_artifact_ids,
                    tool_registry_artifact.id,
                    *historical_artifact_ids,
                ],
                prior_artifact_payloads=prior_artifact_payloads,
                historical_artifact_payloads=historical_artifact_payloads,
            )
            evidence_refs = self.context_builder.evidence_refs(
                request=request,
                memory_artifacts=memory_artifacts,
                memory_records=memory_context.records,
                knowledge_artifact=knowledge_artifact,
                knowledge_hits=knowledge_hits,
            )
            provider_draft = self.provider.run(
                request=request,
                memory_context=memory_context,
                memory_artifact_ids=memory_artifact_ids,
                knowledge_hits=knowledge_hits,
                knowledge_artifact_id=knowledge_artifact.id,
                evidence_refs=evidence_refs,
            )
            outputs = self.artifact_writer.persist_provider_outputs(
                job_id=job_id,
                store=store,
                request=request,
                provider_draft=provider_draft,
                memory_artifact_ids=memory_artifact_ids,
                knowledge_artifact=knowledge_artifact,
                tool_registry_artifact=tool_registry_artifact,
                started_at=started_at,
            )
        except Exception as error:
            raise AgentRuntimeError(f"Agent runtime failed: {error}") from error

        return AgentRuntimeResult(
            plan_artifact=outputs.plan_artifact,
            memory_artifacts=memory_artifacts,
            knowledge_artifact=knowledge_artifact,
            tool_registry_artifact=tool_registry_artifact,
            inference_artifacts=outputs.inference_artifacts,
            runtime_diagnosis_artifacts=outputs.runtime_diagnosis_artifacts,
            report_section_artifacts=outputs.report_section_artifacts,
            repair_instruction_artifacts=outputs.repair_instruction_artifacts,
            review_artifact=outputs.review_artifact,
            tool_call_artifact=outputs.tool_call_artifact,
            message=provider_draft.message,
        )

    def _current_job_artifact_payloads(self, *, job_id: str, store) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for artifact in store.list_artifacts(job_id):
            if artifact.kind not in POST_CORE_KINDS:
                continue
            try:
                payload = json.loads(store.read_artifact(job_id, artifact.id).decode("utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict):
                payload.setdefault("artifactId", artifact.id)
                payload.setdefault("kind", artifact.kind)
                payloads.append(payload)
        return payloads

    def _historical_project_artifact_payloads(
        self,
        *,
        job_id: str,
        project_id: str,
        store,
    ) -> list[dict[str, Any]]:
        return store.list_project_artifact_payloads(
            project_id=project_id,
            kinds=("repair_instruction", "review_run", "runtime_comparison"),
            exclude_job_id=job_id,
            limit=12,
        )


__all__ = [
    "AGENT_PROMPT_VERSION",
    "AGENT_TOOL_VERSION",
    "AgentContextBuilder",
    "AgentContextRedactor",
    "AgentFeedbackRefinement",
    "AgentFeedbackRefiner",
    "AgentInferenceDraft",
    "AgentModelPolicy",
    "AgentProvider",
    "AgentProviderDraft",
    "AgentRepairInstructionDraft",
    "AgentReportSectionDraft",
    "AgentReviewDraft",
    "AgentRuntime",
    "AgentRuntimeDiagnosisDraft",
    "AgentRuntimeError",
    "AgentRuntimeRequest",
    "AgentRuntimeResult",
    "AgentToolRegistryBuilder",
    "CrewAIAgentProvider",
    "CrewAgentPassOutput",
    "CrewInferenceOutput",
    "ModelPolicyResolver",
    "ToolSpec",
]
