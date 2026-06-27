from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class KnowledgeHit:
    id: str
    category: str
    label: str
    locator: str
    excerpt: str
    confidence: float
    source_artifact_ids: list[str] = field(default_factory=list)
    source_kinds: list[str] = field(default_factory=list)
