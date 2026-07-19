from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from apps.api.app.models import ArtifactRecord, EvidenceRef, MemoryRecord
from packages.knowledge import KnowledgeHit

from .agent_contracts import AgentModelPolicy, AgentRuntimeRequest


@dataclass(frozen=True)
class AgentContextRedactionResult:
    input_summary: dict[str, Any]
    memory_excerpt: str
    evidence_refs: list[EvidenceRef]
    metadata: dict[str, Any]


class AgentContextRedactor:
    strategy = "deterministic_context_redaction_v1"

    def redact(
        self,
        *,
        policy: AgentModelPolicy,
        input_summary: dict[str, Any],
        memory_excerpt: str,
        evidence_refs: list[EvidenceRef],
    ) -> AgentContextRedactionResult:
        if not policy.sanitized_context:
            return AgentContextRedactionResult(
                input_summary=input_summary,
                memory_excerpt=memory_excerpt,
                evidence_refs=evidence_refs,
                metadata={
                    "applied": False,
                    "strategy": "none",
                    "scope": [],
                    "placeholderFormat": None,
                    "replacementCount": 0,
                    "replacementCounts": {},
                    "limitations": [],
                },
            )

        counts: dict[str, int] = {}
        redacted_summary = self._redact_input_summary(input_summary, counts)
        redacted_memory = self._redact_text(memory_excerpt, "memory", counts) if memory_excerpt else memory_excerpt
        redacted_refs = [self._redact_evidence_ref(ref, counts) for ref in evidence_refs]
        return AgentContextRedactionResult(
            input_summary=redacted_summary,
            memory_excerpt=redacted_memory,
            evidence_refs=redacted_refs,
            metadata={
                "applied": True,
                "strategy": self.strategy,
                "scope": ["inputSummary", "memory", "evidenceRefs"],
                "placeholderFormat": "redacted:<kind>:<sha256-12>",
                "replacementCount": sum(counts.values()),
                "replacementCounts": dict(sorted(counts.items())),
                "limitations": [
                    "原始 artifact 保持不变；脱敏仅作用于模型上下文和审计证据摘录。",
                    "确定性占位符会保留稳定引用，同时避免暴露源码文本或由源码派生的名称。",
                ],
            },
        )

    def _redact_input_summary(self, input_summary: dict[str, Any], counts: dict[str, int]) -> dict[str, Any]:
        redacted: dict[str, Any] = {}
        for key, value in input_summary.items():
            if key in {"entries", "scripts", "styles", "sourceMaps"}:
                redacted[key] = self._redact_string_list(value, "path", counts)
            elif key == "symbolSample":
                redacted[key] = self._redact_string_list(value, "symbol", counts)
            else:
                redacted[key] = value
        return redacted

    def _redact_evidence_ref(self, ref: EvidenceRef, counts: dict[str, int]) -> EvidenceRef:
        return EvidenceRef(
            artifact_id=ref.artifact_id,
            label=ref.label,
            locator=self._redact_locator(ref.locator, counts),
            excerpt=self._redact_text(ref.excerpt, "source", counts) if ref.excerpt else ref.excerpt,
        )

    def _redact_locator(self, locator: str | None, counts: dict[str, int]) -> str | None:
        if locator is None:
            return None
        if locator.startswith(("artifact:", "memory:", "knowledge:")):
            return locator
        if ":" in locator:
            prefix, value = locator.split(":", 1)
            return f"{prefix}:{self._redact_text(value, 'locator', counts)}"
        return self._redact_text(locator, "locator", counts)

    def _redact_string_list(self, value: Any, kind: str, counts: dict[str, int]) -> list[str]:
        if not isinstance(value, list):
            return []
        return [self._redact_text(item, kind, counts) for item in value if isinstance(item, str)]

    def _redact_text(self, value: str | None, kind: str, counts: dict[str, int]) -> str:
        if value is None:
            return ""
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        counts[kind] = counts.get(kind, 0) + 1
        return f"redacted:{kind}:{digest}"



class AgentContextBuilder:
    """根据确定性 artifact 构建有边界且关联证据的模型上下文。"""

    def input_summary(self, request: AgentRuntimeRequest) -> dict[str, Any]:
        inventory = request.inventory_payload.get("inventory", {})
        ast_indexes = request.ast_index_payload.get("astIndexes", [])
        symbol_names = self._symbol_names(request.ast_index_payload)
        return {
            "entries": self._list_excerpt(inventory.get("entries")),
            "scripts": self._list_excerpt(inventory.get("scripts")),
            "styles": self._list_excerpt(inventory.get("styles")),
            "sourceMaps": self._list_excerpt(inventory.get("sourceMaps")),
            "astIndexCount": len(ast_indexes) if isinstance(ast_indexes, list) else 0,
            "symbolCount": len(symbol_names),
            "symbolSample": symbol_names[:8],
        }

    def evidence_refs(
        self,
        *,
        request: AgentRuntimeRequest,
        memory_artifacts: list[ArtifactRecord],
        memory_records: list[MemoryRecord],
        knowledge_artifact: ArtifactRecord,
        knowledge_hits: list[KnowledgeHit],
    ) -> list[EvidenceRef]:
        return [
            EvidenceRef(
                artifact_id=request.inventory_artifact_id,
                label="Core input inventory",
                locator="artifact:input_inventory",
                excerpt=self.inventory_excerpt(request.inventory_payload),
            ),
            EvidenceRef(
                artifact_id=request.ast_index_artifact_id,
                label="Core AST 索引",
                locator="artifact:ast_index",
                excerpt=self.ast_excerpt(request.ast_index_payload),
            ),
            *[
                EvidenceRef(
                    artifact_id=artifact.id,
                    label=f"记忆：{record.memory_type}",
                    locator=f"memory:{record.memory_type}",
                    excerpt=record.content[:240],
                )
                for artifact, record in zip(memory_artifacts, memory_records, strict=True)
            ],
            *[
                EvidenceRef(
                    artifact_id=knowledge_artifact.id,
                    label=f"知识：{hit.label}",
                    locator=hit.locator,
                    excerpt=hit.excerpt,
                )
                for hit in knowledge_hits
            ],
        ]

    def inventory_excerpt(self, payload: dict[str, Any]) -> str:
        inventory = payload.get("inventory", {})
        entries = inventory.get("entries", [])
        scripts = inventory.get("scripts", [])
        return f"entries={list(entries)[:3]}; scripts={list(scripts)[:3]}"

    def ast_excerpt(self, payload: dict[str, Any]) -> str:
        symbols = self._symbol_names(payload)
        return f"symbols={symbols[:5]}"

    def _list_excerpt(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value[:8] if isinstance(item, str)]

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
