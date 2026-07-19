from __future__ import annotations

from typing import Any

from .core_rules import (
    extend_browser_shim_hits,
    extend_core_hits,
    extend_framework_hits,
    extend_obfuscation_hits,
)
from .evidence_rules import (
    dedupe_hits,
    extend_historical_repair_hits,
    extend_prior_evidence_hits,
    retrieval_sources,
)
from .models import KnowledgeHit
from .utils import list_value, mapping, string_list


class StaticKnowledgeRetriever:
    """面向 Core、当前任务和历史证据执行确定性知识检索。"""

    def retrieve(
        self,
        *,
        inventory_payload: dict[str, Any],
        ast_index_payload: dict[str, Any],
        prior_artifact_payloads: list[dict[str, Any]] | None = None,
        historical_artifact_payloads: list[dict[str, Any]] | None = None,
    ) -> list[KnowledgeHit]:
        inventory = mapping(inventory_payload.get("inventory"))
        ast_indexes = list_value(ast_index_payload.get("astIndexes"))
        detected_runtime = string_list(ast_index_payload.get("detectedRuntime"))
        prior_payloads = [payload for payload in (prior_artifact_payloads or []) if isinstance(payload, dict)]
        historical_payloads = [payload for payload in (historical_artifact_payloads or []) if isinstance(payload, dict)]

        hits: list[KnowledgeHit] = []
        extend_core_hits(
            hits=hits,
            inventory=inventory,
            ast_indexes=ast_indexes,
            detected_runtime=detected_runtime,
        )
        extend_framework_hits(hits=hits, inventory=inventory, ast_indexes=ast_indexes)
        extend_obfuscation_hits(hits=hits, inventory=inventory, ast_indexes=ast_indexes)
        extend_browser_shim_hits(hits=hits, inventory=inventory, ast_indexes=ast_indexes)
        extend_prior_evidence_hits(hits=hits, payloads=prior_payloads)
        extend_historical_repair_hits(hits=hits, payloads=historical_payloads)

        if not hits:
            hits.append(
                KnowledgeHit(
                    id="knowledge_minimal_input",
                    category="input_limitation",
                    label="构建证据有限",
                    locator="knowledge:input_limitation/minimal",
                    excerpt="缺少入口、source map、导出、runtime 或验证证据时，应降低 Agent 置信度。",
                    confidence=0.55,
                    source_kinds=["input_inventory", "ast_index"],
                )
            )
        return dedupe_hits(hits)

    def artifact_payload(
        self,
        *,
        job_id: str,
        input_artifact_ids: list[str],
        hits: list[KnowledgeHit],
        prior_artifact_payloads: list[dict[str, Any]] | None = None,
        historical_artifact_payloads: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        prior_payloads = [payload for payload in (prior_artifact_payloads or []) if isinstance(payload, dict)]
        historical_payloads = [payload for payload in (historical_artifact_payloads or []) if isinstance(payload, dict)]
        return {
            "kind": "knowledge_evidence",
            "jobId": job_id,
            "inputArtifactIds": input_artifact_ids,
            "retrievalSources": retrieval_sources(prior_payloads, historical_payloads),
            "hits": [
                {
                    "id": hit.id,
                    "category": hit.category,
                    "label": hit.label,
                    "locator": hit.locator,
                    "excerpt": hit.excerpt,
                    "confidence": hit.confidence,
                    "sourceArtifactIds": hit.source_artifact_ids,
                    "sourceKinds": hit.source_kinds,
                }
                for hit in hits
            ],
            "limitations": [
                "检索器根据 Core、当前 Job 和同 project 历史 Artifact 生成确定性的本地知识线索。",
                "只有在 Agent planning 前已经存在时，才会使用当前 Job 的 validation 和 repair Artifact。",
                "历史修复案例仅限同项目证据，并且只作为证据引用。",
                "知识命中属于 evidence ref，不会覆盖当前 input Artifact。",
            ],
        }
