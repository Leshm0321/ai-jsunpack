from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from apps.api.app.models import ArtifactKind, FailureClass, ToolRegistryCategory, ToolRegistryEntry

from .agent_contracts import AGENT_TOOL_VERSION


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    category: ToolRegistryCategory
    caller: str
    input_artifact_kinds: list[ArtifactKind]
    output_artifact_kinds: list[ArtifactKind]
    failure_classes: list[FailureClass]
    description: str
    stateful: bool = False
    parallel_safe: bool = True
    side_effects: list[str] = field(default_factory=list)

    def to_registry_entry(self, job_id: str) -> ToolRegistryEntry:
        behavior: list[str] = []
        if self.stateful:
            behavior.append("stateful")
        if not self.parallel_safe:
            behavior.append("not parallel-safe")
        if self.side_effects:
            behavior.append(f"side effects: {', '.join(self.side_effects)}")
        description = self.description
        if behavior:
            description = f"{description} ({'; '.join(behavior)})."
        return ToolRegistryEntry(
            id=f"tool_registry_{uuid4().hex[:12]}",
            job_id=job_id,
            tool_name=self.name,
            tool_version=self.version,
            category=self.category,
            caller=self.caller,
            input_artifact_kinds=self.input_artifact_kinds,
            output_artifact_kinds=self.output_artifact_kinds,
            failure_classes=self.failure_classes,
            description=description,
        )


class AgentToolRegistryBuilder:
    """Creates auditable tool registry entries from declarative tool specs."""

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="crewai.agent_pass",
                version=AGENT_TOOL_VERSION,
                category="model",
                caller="WorkerPipeline",
                input_artifact_kinds=["input_inventory", "ast_index", "memory_record", "knowledge_evidence"],
                output_artifact_kinds=[
                    "agent_plan",
                    "agent_execution",
                    "inference_record",
                    "runtime_diagnosis",
                    "report_section",
                    "repair_instruction",
                    "review_run",
                    "tool_call",
                ],
                failure_classes=["none", "policy_denied", "agent_failed", "resource_limit"],
                description=(
                    "Runs schema-first Agent analysis over deterministic Core evidence in an isolated "
                    "child process with a per-invocation CrewAI storage root."
                ),
                stateful=True,
                parallel_safe=True,
                side_effects=["model_call", "isolated_crewai_storage", "child_process"],
            ),
            ToolSpec(
                name="memory.context",
                version="0.1.0",
                category="memory",
                caller="AgentRuntime",
                input_artifact_kinds=["input_inventory", "ast_index"],
                output_artifact_kinds=["memory_record"],
                failure_classes=["none", "unknown"],
                description="Builds short-term, long-term, entity, and scenario memory records for the current project.",
                side_effects=["artifact_write"],
            ),
            ToolSpec(
                name="knowledge.static_retrieval",
                version="0.1.0",
                category="knowledge",
                caller="AgentRuntime",
                input_artifact_kinds=[
                    "input_inventory",
                    "ast_index",
                    "memory_record",
                    "build_artifact",
                    "build_log",
                    "runtime_trace",
                    "runtime_validation",
                    "runtime_comparison",
                    "review_run",
                    "repair_instruction",
                ],
                output_artifact_kinds=["knowledge_evidence"],
                failure_classes=["none", "unknown"],
                description="Retrieves static build, framework, runtime, repair, current-job validation, and same-project historical evidence hints.",
                side_effects=["artifact_write"],
            ),
        ]

    def entries(self, job_id: str) -> list[ToolRegistryEntry]:
        return [spec.to_registry_entry(job_id) for spec in self.specs()]
