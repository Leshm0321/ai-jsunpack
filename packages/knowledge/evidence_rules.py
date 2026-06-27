from __future__ import annotations

from typing import Any

from .constants import POST_CORE_KINDS
from .models import KnowledgeHit
from .utils import artifact_id, excerpt, runner_kinds, runtime_validation_excerpt, slug


def extend_prior_evidence_hits(*, hits: list[KnowledgeHit], payloads: list[dict[str, Any]]) -> None:
    for payload in payloads:
        kind = str(payload.get("kind") or "")
        current_artifact_id = artifact_id(payload)
        failure_class = str(payload.get("failureClass") or "none")
        status = str(payload.get("status") or "")
        source_artifact_ids = [current_artifact_id] if current_artifact_id else []
        if kind in {"build_artifact", "build_log"} and status in {"fail", "retry", "best_effort"}:
            review_type = str(payload.get("reviewType") or payload.get("phase") or "build")
            slug_value = slug(f"{review_type}_{failure_class}") or "build_feedback"
            hits.append(
                KnowledgeHit(
                    id=f"knowledge_validation_build_{slug_value}",
                    category="validation_feedback",
                    label=f"Build validation feedback: {review_type}",
                    locator=f"knowledge:validation_feedback/build/{slug_value}",
                    excerpt=excerpt(
                        payload,
                        fallback="Existing build/typecheck evidence should guide low-risk repair suggestions.",
                    ),
                    confidence=0.78,
                    source_artifact_ids=source_artifact_ids,
                    source_kinds=[kind],
                )
            )
        if kind == "runtime_validation" and status in {"fail", "retry", "best_effort"}:
            target = str(payload.get("target") or "runtime")
            slug_value = slug(f"{target}_{failure_class}") or "runtime_validation"
            hits.append(
                KnowledgeHit(
                    id=f"knowledge_validation_runtime_{slug_value}",
                    category="validation_feedback",
                    label=f"Runtime validation feedback: {target}",
                    locator=f"knowledge:validation_feedback/runtime/{slug_value}",
                    excerpt=runtime_validation_excerpt(payload),
                    confidence=0.8,
                    source_artifact_ids=source_artifact_ids,
                    source_kinds=[kind],
                )
            )
        if kind == "runtime_trace":
            execution_boundary = payload.get("executionBoundary")
            source_kinds = [kind]
            if isinstance(execution_boundary, dict):
                runner_values = runner_kinds(execution_boundary)
                if runner_values:
                    slug_value = slug("_".join(runner_values)) or "runner"
                    hits.append(
                        KnowledgeHit(
                            id=f"knowledge_browser_shim_runtime_boundary_{slug_value}",
                            category="browser_shim",
                            label="Runtime execution boundary evidence",
                            locator=f"knowledge:browser_shim/runtime_boundary/{slug_value}",
                            excerpt=(
                                "Existing runtime trace captured browser execution boundary "
                                f"with runner(s): {', '.join(runner_values)}."
                            ),
                            confidence=0.77,
                            source_artifact_ids=source_artifact_ids,
                            source_kinds=source_kinds,
                        )
                    )
            if status in {"fail", "retry", "best_effort"}:
                slug_value = slug(f"{payload.get('target') or 'runtime_trace'}_{failure_class}") or "runtime_trace"
                hits.append(
                    KnowledgeHit(
                        id=f"knowledge_validation_runtime_trace_{slug_value}",
                        category="validation_feedback",
                        label="Runtime trace feedback",
                        locator=f"knowledge:validation_feedback/runtime_trace/{slug_value}",
                        excerpt=runtime_validation_excerpt(payload),
                        confidence=0.79,
                        source_artifact_ids=source_artifact_ids,
                        source_kinds=source_kinds,
                    )
                )
        if kind == "runtime_comparison" and status in {"fail", "retry", "best_effort"}:
            slug_value = slug(str(payload.get("status") or "runtime_comparison")) or "runtime_comparison"
            hits.append(
                KnowledgeHit(
                    id=f"knowledge_validation_runtime_comparison_{slug_value}",
                    category="validation_feedback",
                    label="Runtime comparison feedback",
                    locator=f"knowledge:validation_feedback/runtime_compare/{slug_value}",
                    excerpt="Existing runtime comparison differences should guide behavior-preserving repair review.",
                    confidence=0.82,
                    source_artifact_ids=source_artifact_ids,
                    source_kinds=[kind],
                )
            )
        if kind == "review_run" and status in {"fail", "retry", "best_effort"}:
            review_type = str(payload.get("reviewType") or "review")
            slug_value = slug(f"{review_type}_{failure_class}") or "review_feedback"
            hits.append(
                KnowledgeHit(
                    id=f"knowledge_review_feedback_{slug_value}",
                    category="validation_feedback",
                    label=f"Review feedback: {review_type}",
                    locator=f"knowledge:validation_feedback/review/{slug_value}",
                    excerpt=excerpt(
                        payload,
                        fallback="Existing review evidence should remain audit-only unless a deterministic repair consumes it.",
                    ),
                    confidence=0.76,
                    source_artifact_ids=source_artifact_ids,
                    source_kinds=[kind],
                )
            )
        if kind == "repair_instruction":
            target_stage = str(payload.get("targetStage") or "repair")
            risk = str(payload.get("riskLevel") or "unknown")
            slug_value = slug(f"{target_stage}_{risk}") or "repair_case"
            hits.append(
                KnowledgeHit(
                    id=f"knowledge_repair_case_{slug_value}",
                    category="repair_case",
                    label=f"Current-job repair case: {target_stage}",
                    locator=f"knowledge:repair_case/current_job/{slug_value}",
                    excerpt=excerpt(
                        payload,
                        fallback="Existing repair instruction can be used as current-job historical context only.",
                    ),
                    confidence=0.84 if risk == "low" else 0.72,
                    source_artifact_ids=source_artifact_ids,
                    source_kinds=[kind],
                )
            )


def extend_historical_repair_hits(*, hits: list[KnowledgeHit], payloads: list[dict[str, Any]]) -> None:
    for payload in payloads:
        kind = str(payload.get("kind") or "")
        current_artifact_id = artifact_id(payload)
        source_artifact_ids = [current_artifact_id] if current_artifact_id else []
        if kind == "repair_instruction":
            target_stage = str(payload.get("targetStage") or "repair")
            risk = str(payload.get("riskLevel") or "unknown")
            decision = excerpt(payload, fallback="Historical repair case available for evidence reference only.")
            slug_value = slug(f"{target_stage}_{risk}_{current_artifact_id or 'historical'}") or "historical_repair_case"
            hits.append(
                KnowledgeHit(
                    id=f"knowledge_historical_repair_case_{slug_value}",
                    category="historical_repair_case",
                    label=f"Historical repair case: {target_stage}",
                    locator=f"knowledge:repair_case/historical/{slug_value}",
                    excerpt=decision,
                    confidence=0.66 if risk == "low" else 0.58,
                    source_artifact_ids=source_artifact_ids,
                    source_kinds=[kind],
                )
            )
        elif kind == "review_run":
            review_type = str(payload.get("reviewType") or "review")
            failure_class = str(payload.get("failureClass") or "none")
            status = str(payload.get("status") or "")
            if status in {"fail", "retry", "best_effort"}:
                slug_value = slug(f"{review_type}_{failure_class}_{current_artifact_id or 'historical'}") or "historical_review"
                hits.append(
                    KnowledgeHit(
                        id=f"knowledge_historical_review_feedback_{slug_value}",
                        category="historical_validation_feedback",
                        label=f"Historical review feedback: {review_type}",
                        locator=f"knowledge:validation_feedback/historical_review/{slug_value}",
                        excerpt=excerpt(
                            payload,
                            fallback="Historical review evidence is evidence-only and same-project scoped.",
                        ),
                        confidence=0.62,
                        source_artifact_ids=source_artifact_ids,
                        source_kinds=[kind],
                    )
                )
        elif kind == "runtime_comparison":
            status = str(payload.get("status") or "")
            if status in {"fail", "retry", "best_effort"}:
                failure_class = str(payload.get("failureClass") or "unknown")
                slug_value = slug(f"{status}_{failure_class}_{current_artifact_id or 'historical'}") or "historical_runtime_compare"
                hits.append(
                    KnowledgeHit(
                        id=f"knowledge_historical_runtime_comparison_{slug_value}",
                        category="historical_validation_feedback",
                        label="Historical runtime comparison feedback",
                        locator=f"knowledge:validation_feedback/historical_runtime_compare/{slug_value}",
                        excerpt="Historical runtime comparison differences are evidence-only and same-project scoped.",
                        confidence=0.64,
                        source_artifact_ids=source_artifact_ids,
                        source_kinds=[kind],
                    )
                )


def retrieval_sources(prior_payloads: list[dict[str, Any]], historical_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    post_core_sources = [
        {
            "artifactId": artifact_id(payload),
            "kind": str(payload.get("kind") or "unknown"),
            "status": payload.get("status"),
            "failureClass": payload.get("failureClass"),
            "attempt": payload.get("attempt"),
        }
        for payload in prior_payloads
        if str(payload.get("kind") or "") in POST_CORE_KINDS
    ]
    historical_sources = [
        {
            "artifactId": artifact_id(payload),
            "kind": str(payload.get("kind") or "unknown"),
            "jobId": payload.get("jobId"),
            "status": payload.get("status"),
            "failureClass": payload.get("failureClass"),
            "attempt": payload.get("attempt"),
        }
        for payload in historical_payloads
        if str(payload.get("kind") or "") in {"repair_instruction", "review_run", "runtime_comparison"}
    ]
    return {
        "core": ["input_inventory", "ast_index"],
        "currentJobArtifacts": post_core_sources,
        "historicalProjectArtifacts": historical_sources,
        "crossJobHistory": bool(historical_sources),
    }


def dedupe_hits(hits: list[KnowledgeHit]) -> list[KnowledgeHit]:
    by_id: dict[str, KnowledgeHit] = {}
    for hit in hits:
        existing = by_id.get(hit.id)
        if existing is None or hit.confidence > existing.confidence:
            by_id[hit.id] = hit
    return list(by_id.values())
