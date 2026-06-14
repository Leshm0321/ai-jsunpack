from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from apps.api.app.models import ArtifactRecord, EvidenceRef, InferenceRecord, ReviewRun, ToolCall


STUB_PROMPT_VERSION = "agent-stub-v1"
STUB_TOOL_VERSION = "0.1.0"


class AgentRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentRuntimeRequest:
    job_id: str
    inventory_artifact_id: str
    ast_index_artifact_id: str
    inventory_payload: dict[str, Any]
    ast_index_payload: dict[str, Any]

    @property
    def input_artifact_ids(self) -> list[str]:
        return [self.inventory_artifact_id, self.ast_index_artifact_id]


@dataclass(frozen=True)
class AgentRuntimeResult:
    plan_artifact: ArtifactRecord
    inference_artifacts: list[ArtifactRecord]
    review_artifact: ArtifactRecord
    tool_call_artifact: ArtifactRecord
    message: str


class AgentProvider(Protocol):
    provider_name: str
    model_name: str
    prompt_version: str
    tool_name: str
    tool_version: str

    def create_plan(self, request: AgentRuntimeRequest) -> dict[str, Any]:
        ...

    def create_inference_records(self, request: AgentRuntimeRequest, plan_artifact_id: str) -> list[InferenceRecord]:
        ...

    def create_review_run(self, request: AgentRuntimeRequest, inference_artifact_ids: list[str]) -> ReviewRun:
        ...


class CrewAIStubProvider:
    """Deterministic CrewAI-compatible provider used until real model providers are wired."""

    provider_name = "crewai_stub"
    model_name = "stub-v0"
    prompt_version = STUB_PROMPT_VERSION
    tool_name = "crewai_stub.agent_pass"
    tool_version = STUB_TOOL_VERSION

    def create_plan(self, request: AgentRuntimeRequest) -> dict[str, Any]:
        inventory = request.inventory_payload.get("inventory", {})
        ast_indexes = request.ast_index_payload.get("astIndexes", [])
        return {
            "kind": "agent_plan",
            "jobId": request.job_id,
            "provider": self.provider_name,
            "modelName": self.model_name,
            "promptVersion": self.prompt_version,
            "inputArtifactIds": request.input_artifact_ids,
            "plannedAgents": ["PlannerAgent", "AnalysisAgent", "FrameworkAgent", "RuntimeAgent", "ReviewAgent"],
            "inputSummary": {
                "entries": list(inventory.get("entries", [])),
                "scripts": list(inventory.get("scripts", [])),
                "styles": list(inventory.get("styles", [])),
                "astIndexCount": len(ast_indexes) if isinstance(ast_indexes, list) else 0,
                "symbolCount": len(self._symbol_names(request.ast_index_payload)),
            },
            "limitations": [
                "CrewAI stub provider produced deterministic schema fixtures only.",
                "No model call was made and no generated project files were modified.",
            ],
        }

    def create_inference_records(self, request: AgentRuntimeRequest, plan_artifact_id: str) -> list[InferenceRecord]:
        symbols = self._symbol_names(request.ast_index_payload)
        evidence_refs = self._evidence_refs(request)
        return [
            InferenceRecord(
                id=self._new_id("inference"),
                job_id=request.job_id,
                type="module_split",
                agent_name="AnalysisAgent",
                model_provider=self.provider_name,
                model_name=self.model_name,
                prompt_version=self.prompt_version,
                input_artifact_ids=request.input_artifact_ids,
                output_artifact_ids=[plan_artifact_id],
                evidence_refs=evidence_refs,
                confidence=0.35 if symbols else 0.2,
                uncertainty_reasons=[
                    "Stub provider only summarizes deterministic Core artifacts.",
                    "Module boundaries require a real Agent/model pass.",
                ],
                alternatives=["defer module split until real CrewAI provider is available"],
                validation_status="needs_review",
            ),
            InferenceRecord(
                id=self._new_id("inference"),
                job_id=request.job_id,
                type="framework",
                agent_name="FrameworkAgent",
                model_provider=self.provider_name,
                model_name=self.model_name,
                prompt_version=self.prompt_version,
                input_artifact_ids=request.input_artifact_ids,
                output_artifact_ids=[plan_artifact_id],
                evidence_refs=evidence_refs,
                confidence=0.2,
                uncertainty_reasons=[
                    "Stub provider does not inspect framework semantics.",
                    "Framework identification remains unverified.",
                ],
                alternatives=["classify framework after semantic Agent pass"],
                validation_status="needs_review",
            ),
            InferenceRecord(
                id=self._new_id("inference"),
                job_id=request.job_id,
                type="runtime",
                agent_name="RuntimeAgent",
                model_provider=self.provider_name,
                model_name=self.model_name,
                prompt_version=self.prompt_version,
                input_artifact_ids=request.input_artifact_ids,
                output_artifact_ids=[plan_artifact_id],
                evidence_refs=evidence_refs,
                confidence=0.4,
                uncertainty_reasons=[
                    "Stub provider records runtime assumptions before sandbox/runtime evidence is reviewed.",
                ],
                alternatives=["re-evaluate after runtime smoke and compare artifacts are available"],
                validation_status="needs_review",
            ),
        ]

    def create_review_run(self, request: AgentRuntimeRequest, inference_artifact_ids: list[str]) -> ReviewRun:
        return ReviewRun(
            id=self._new_id("review"),
            job_id=request.job_id,
            attempt=0,
            review_type="agent_review",
            status="best_effort",
            decision="CrewAI stub provider produced schema-valid audit records; semantic conclusions require a real provider.",
            failure_class="none",
            evidence_refs=self._evidence_refs(request),
            repair_instruction_ids=[],
            logs_artifact_id=None,
        )

    def _evidence_refs(self, request: AgentRuntimeRequest) -> list[EvidenceRef]:
        return [
            EvidenceRef(
                artifact_id=request.inventory_artifact_id,
                label="Core input inventory",
                locator="artifact:input_inventory",
                excerpt=self._inventory_excerpt(request.inventory_payload),
            ),
            EvidenceRef(
                artifact_id=request.ast_index_artifact_id,
                label="Core AST index",
                locator="artifact:ast_index",
                excerpt=self._ast_excerpt(request.ast_index_payload),
            ),
        ]

    def _inventory_excerpt(self, payload: dict[str, Any]) -> str:
        inventory = payload.get("inventory", {})
        entries = inventory.get("entries", [])
        scripts = inventory.get("scripts", [])
        return f"entries={list(entries)[:3]}; scripts={list(scripts)[:3]}"

    def _ast_excerpt(self, payload: dict[str, Any]) -> str:
        symbols = self._symbol_names(payload)
        return f"symbols={symbols[:5]}"

    def _symbol_names(self, payload: dict[str, Any]) -> list[str]:
        names: list[str] = []
        ast_indexes = payload.get("astIndexes", [])
        if not isinstance(ast_indexes, list):
            return names
        for ast_index in ast_indexes:
            if not isinstance(ast_index, dict):
                continue
            symbols = ast_index.get("symbols", [])
            if not isinstance(symbols, list):
                continue
            for symbol in symbols:
                if isinstance(symbol, dict) and isinstance(symbol.get("name"), str):
                    names.append(symbol["name"])
        return names

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid4().hex[:12]}"


class AgentRuntime:
    def __init__(self, provider: AgentProvider | None = None) -> None:
        self.provider = provider or CrewAIStubProvider()

    def run(self, *, job_id: str, store, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        started_at = time.perf_counter()
        try:
            store.update_status(job_id, "agent_planning")
            plan_artifact = store.write_artifact(
                job_id,
                kind="agent_plan",
                stage="agent_planning",
                filename="agent-plan.json",
                content=self._json_bytes(self.provider.create_plan(request)),
                content_type="application/json",
                producer="worker.agent_runtime",
                parent_artifact_ids=request.input_artifact_ids,
            )

            store.update_status(job_id, "agent_pass")
            inference_artifacts: list[ArtifactRecord] = []
            for index, record in enumerate(self.provider.create_inference_records(request, plan_artifact.id), start=1):
                inference_artifacts.append(
                    store.write_artifact(
                        job_id,
                        kind="inference_record",
                        stage="agent_pass",
                        filename=f"inference-record-{index}.json",
                        content=record.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                        content_type="application/json",
                        producer="worker.agent_runtime",
                        parent_artifact_ids=[*request.input_artifact_ids, plan_artifact.id],
                    )
                )

            inference_artifact_ids = [artifact.id for artifact in inference_artifacts]
            review_run = self.provider.create_review_run(request, inference_artifact_ids)
            review_artifact = store.write_artifact(
                job_id,
                kind="review_run",
                stage="agent_pass",
                filename="agent-review-run.json",
                content=review_run.model_dump_json(by_alias=True, indent=2).encode("utf-8"),
                content_type="application/json",
                producer="worker.agent_runtime",
                parent_artifact_ids=[plan_artifact.id, *inference_artifact_ids],
            )

            output_artifact_ids = [plan_artifact.id, *inference_artifact_ids, review_artifact.id]
            tool_call = ToolCall(
                id=f"tool_call_{uuid4().hex[:12]}",
                job_id=job_id,
                caller="WorkerPipeline",
                tool_name=self.provider.tool_name,
                tool_version=self.provider.tool_version,
                input_artifact_ids=request.input_artifact_ids,
                output_artifact_ids=output_artifact_ids,
                status="pass",
                duration=self._duration_ms(started_at),
                failure_class="none",
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
        except Exception as error:
            raise AgentRuntimeError(f"Agent runtime failed: {error}") from error

        return AgentRuntimeResult(
            plan_artifact=plan_artifact,
            inference_artifacts=inference_artifacts,
            review_artifact=review_artifact,
            tool_call_artifact=tool_call_artifact,
            message=(
                "CrewAI stub provider produced "
                f"{len(inference_artifacts)} inference records, one review run, and one tool call audit record."
            ),
        )

    def _duration_ms(self, started_at: float) -> float:
        return round((time.perf_counter() - started_at) * 1000, 3)

    def _json_bytes(self, payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
