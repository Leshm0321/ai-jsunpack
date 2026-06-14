from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


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

    def run(self, job_id: str) -> PipelineRun:
        run = PipelineRun(job_id=job_id)
        for status in PIPELINE_ORDER:
            run.transition(status, self._message_for(status))
        return run

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

