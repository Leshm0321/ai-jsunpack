from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class KnowledgeHit:
    id: str
    category: str
    label: str
    locator: str
    excerpt: str
    confidence: float


class StaticKnowledgeRetriever:
    """Deterministic first-pass knowledge retrieval for build/runtime evidence."""

    def retrieve(
        self,
        *,
        inventory_payload: dict[str, Any],
        ast_index_payload: dict[str, Any],
    ) -> list[KnowledgeHit]:
        inventory = inventory_payload.get("inventory", {})
        hits: list[KnowledgeHit] = []
        if inventory.get("entries"):
            hits.append(
                KnowledgeHit(
                    id="knowledge_html_entry",
                    category="runtime_entry",
                    label="HTML entry discovered",
                    locator="knowledge:runtime_entry/html",
                    excerpt="HTML entries usually provide script, stylesheet, public path, and runtime loading evidence.",
                    confidence=0.8,
                )
            )
        if inventory.get("sourceMaps"):
            hits.append(
                KnowledgeHit(
                    id="knowledge_source_map_available",
                    category="source_map",
                    label="Source map candidate discovered",
                    locator="knowledge:source_map/available",
                    excerpt="Source maps can seed source-file candidates but must not replace current artifact evidence.",
                    confidence=0.7,
                )
            )
        if self._has_export(ast_index_payload):
            hits.append(
                KnowledgeHit(
                    id="knowledge_esm_exports",
                    category="module_pattern",
                    label="ES module export evidence",
                    locator="knowledge:module_pattern/esm",
                    excerpt="Named exports suggest module boundaries and can guide reconstruction planning.",
                    confidence=0.65,
                )
            )
        if not hits:
            hits.append(
                KnowledgeHit(
                    id="knowledge_minimal_input",
                    category="input_limitation",
                    label="Limited build evidence",
                    locator="knowledge:input_limitation/minimal",
                    excerpt="Missing entry, source map, or export evidence should lower Agent confidence.",
                    confidence=0.55,
                )
            )
        return hits

    def artifact_payload(
        self,
        *,
        job_id: str,
        input_artifact_ids: list[str],
        hits: list[KnowledgeHit],
    ) -> dict[str, Any]:
        return {
            "kind": "knowledge_evidence",
            "jobId": job_id,
            "inputArtifactIds": input_artifact_ids,
            "hits": [
                {
                    "id": hit.id,
                    "category": hit.category,
                    "label": hit.label,
                    "locator": hit.locator,
                    "excerpt": hit.excerpt,
                    "confidence": hit.confidence,
                }
                for hit in hits
            ],
            "limitations": [
                "Static retriever only emits deterministic local knowledge hints.",
                "Knowledge hits are evidence references and do not override current input artifacts.",
            ],
        }

    def _has_export(self, payload: dict[str, Any]) -> bool:
        ast_indexes = payload.get("astIndexes", [])
        if not isinstance(ast_indexes, list):
            return False
        for ast_index in ast_indexes:
            if isinstance(ast_index, dict) and ast_index.get("exports"):
                return True
        return False
