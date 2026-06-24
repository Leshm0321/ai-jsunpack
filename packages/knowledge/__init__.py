from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


POST_CORE_KINDS = {
    "build_artifact",
    "build_log",
    "runtime_trace",
    "runtime_validation",
    "runtime_comparison",
    "review_run",
    "repair_instruction",
}


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


class StaticKnowledgeRetriever:
    """Deterministic knowledge retrieval for Core, current-job, and same-project historical evidence."""

    def retrieve(
        self,
        *,
        inventory_payload: dict[str, Any],
        ast_index_payload: dict[str, Any],
        prior_artifact_payloads: list[dict[str, Any]] | None = None,
        historical_artifact_payloads: list[dict[str, Any]] | None = None,
    ) -> list[KnowledgeHit]:
        inventory = self._mapping(inventory_payload.get("inventory"))
        ast_indexes = self._list(ast_index_payload.get("astIndexes"))
        detected_runtime = self._string_list(ast_index_payload.get("detectedRuntime"))
        prior_payloads = [payload for payload in (prior_artifact_payloads or []) if isinstance(payload, dict)]
        historical_payloads = [payload for payload in (historical_artifact_payloads or []) if isinstance(payload, dict)]

        hits: list[KnowledgeHit] = []
        self._extend_core_hits(
            hits=hits,
            inventory=inventory,
            ast_indexes=ast_indexes,
            detected_runtime=detected_runtime,
        )
        self._extend_framework_hits(hits=hits, inventory=inventory, ast_indexes=ast_indexes)
        self._extend_obfuscation_hits(hits=hits, inventory=inventory, ast_indexes=ast_indexes)
        self._extend_browser_shim_hits(hits=hits, inventory=inventory, ast_indexes=ast_indexes)
        self._extend_prior_evidence_hits(hits=hits, payloads=prior_payloads)
        self._extend_historical_repair_hits(hits=hits, payloads=historical_payloads)

        if not hits:
            hits.append(
                KnowledgeHit(
                    id="knowledge_minimal_input",
                    category="input_limitation",
                    label="Limited build evidence",
                    locator="knowledge:input_limitation/minimal",
                    excerpt="Missing entry, source map, export, runtime, or validation evidence should lower Agent confidence.",
                    confidence=0.55,
                    source_kinds=["input_inventory", "ast_index"],
                )
            )
        return self._dedupe_hits(hits)

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
            "retrievalSources": self._retrieval_sources(prior_payloads, historical_payloads),
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
                "Retriever emits deterministic local knowledge hints from Core, current-job, and same-project historical artifacts.",
                "Current-job validation and repair artifacts are used only when they already exist before Agent planning.",
                "Historical repair cases are limited to same-project evidence and remain evidence references only.",
                "Knowledge hits are evidence references and do not override current input artifacts.",
            ],
        }

    def _extend_core_hits(
        self,
        *,
        hits: list[KnowledgeHit],
        inventory: dict[str, Any],
        ast_indexes: list[Any],
        detected_runtime: list[str],
    ) -> None:
        entries = self._string_list(inventory.get("entries"))
        scripts = self._string_list(inventory.get("scripts"))
        styles = self._string_list(inventory.get("styles"))
        source_maps = self._string_list(inventory.get("sourceMaps"))
        manifests = self._string_list(inventory.get("manifests"))

        if entries:
            hits.append(
                KnowledgeHit(
                    id="knowledge_html_entry",
                    category="runtime_entry",
                    label="HTML entry discovered",
                    locator="knowledge:runtime_entry/html",
                    excerpt="HTML entries usually provide script, stylesheet, public path, and runtime loading evidence.",
                    confidence=0.8,
                    source_kinds=["input_inventory"],
                )
            )
        else:
            hits.append(
                KnowledgeHit(
                    id="knowledge_browser_shim_missing_html_entry",
                    category="browser_shim",
                    label="No HTML entry discovered",
                    locator="knowledge:browser_shim/html_entry",
                    excerpt="A missing HTML entry usually requires a deterministic host page before browser validation can run.",
                    confidence=0.72,
                    source_kinds=["input_inventory"],
                )
            )
        if source_maps:
            hits.append(
                KnowledgeHit(
                    id="knowledge_source_map_available",
                    category="source_map",
                    label="Source map candidate discovered",
                    locator="knowledge:source_map/available",
                    excerpt="Source maps can seed source-file candidates but must not replace current artifact evidence.",
                    confidence=0.7,
                    source_kinds=["input_inventory"],
                )
            )
        if self._has_export(ast_indexes):
            hits.append(
                KnowledgeHit(
                    id="knowledge_esm_exports",
                    category="module_pattern",
                    label="ES module export evidence",
                    locator="knowledge:module_pattern/esm",
                    excerpt="Named exports suggest module boundaries and can guide reconstruction planning.",
                    confidence=0.65,
                    source_kinds=["ast_index"],
                )
            )
        if len(scripts) > 1 or "multi_chunk" in detected_runtime:
            hits.append(
                KnowledgeHit(
                    id="knowledge_runtime_multi_chunk",
                    category="build_runtime",
                    label="Multi-chunk runtime pattern",
                    locator="knowledge:build_runtime/multi_chunk",
                    excerpt="Multiple JavaScript chunks indicate loader ordering and public path evidence should be preserved.",
                    confidence=0.74,
                    source_kinds=["input_inventory", "ast_index"],
                )
            )
        if inventory.get("isSingleBundle") or "single_bundle_best_effort" in detected_runtime:
            hits.append(
                KnowledgeHit(
                    id="knowledge_runtime_single_bundle",
                    category="build_runtime",
                    label="Single-bundle best-effort pattern",
                    locator="knowledge:build_runtime/single_bundle",
                    excerpt="A single bundle without a full source graph should bias reconstruction toward conservative static hosting.",
                    confidence=0.68,
                    source_kinds=["input_inventory", "ast_index"],
                )
            )
        if manifests:
            hits.append(
                KnowledgeHit(
                    id="knowledge_runtime_manifest",
                    category="build_runtime",
                    label="Manifest runtime metadata",
                    locator="knowledge:build_runtime/manifest",
                    excerpt="Manifest files may contain asset, route, or service worker metadata that should remain evidence-bound.",
                    confidence=0.64,
                    source_kinds=["input_inventory"],
                )
            )
        if styles:
            hits.append(
                KnowledgeHit(
                    id="knowledge_runtime_stylesheet_assets",
                    category="build_runtime",
                    label="Stylesheet assets discovered",
                    locator="knowledge:build_runtime/stylesheets",
                    excerpt="Stylesheet assets are runtime-visible evidence and should be included in browser comparison scenarios.",
                    confidence=0.62,
                    source_kinds=["input_inventory"],
                )
            )
        for runtime in detected_runtime:
            runtime_id = self._slug(runtime)
            if runtime_id:
                hits.append(
                    KnowledgeHit(
                        id=f"knowledge_detected_runtime_{runtime_id}",
                        category="build_runtime",
                        label=f"Detected runtime: {runtime}",
                        locator=f"knowledge:build_runtime/{runtime_id}",
                        excerpt=f"Core detected {runtime} from local inventory and AST evidence.",
                        confidence=0.66,
                        source_kinds=["ast_index"],
                    )
                )

    def _extend_framework_hits(
        self,
        *,
        hits: list[KnowledgeHit],
        inventory: dict[str, Any],
        ast_indexes: list[Any],
    ) -> None:
        haystack = "\n".join(
            [
                *self._string_list(inventory.get("scripts")),
                *self._string_list(inventory.get("styles")),
                *self._string_list(inventory.get("manifests")),
                *self._ast_text(ast_indexes),
            ]
        ).lower()
        framework_rules = [
            (
                "react",
                ("react", "react-dom", "jsx", "createelement", "usestate", "useeffect"),
                "React framework signature",
                "React imports, JSX helpers, or React hook symbols should guide component-boundary inference.",
                0.72,
            ),
            (
                "vue",
                ("vue", "createapp", "definecomponent", "__vue", ".vue"),
                "Vue framework signature",
                "Vue runtime or component symbols should guide component and template reconstruction assumptions.",
                0.72,
            ),
            (
                "vite_rollup",
                ("vite", "import.meta", "vite_or_rollup", "assets/"),
                "Vite/Rollup build signature",
                "Vite or Rollup asset conventions should preserve module-script and public asset path assumptions.",
                0.68,
            ),
            (
                "webpack",
                ("__webpack_require__", "webpack", "webpackchunk"),
                "Webpack runtime signature",
                "Webpack runtime symbols should preserve chunk loader and public path evidence during analysis.",
                0.7,
            ),
        ]
        for slug, tokens, label, excerpt, confidence in framework_rules:
            if any(token in haystack for token in tokens):
                hits.append(
                    KnowledgeHit(
                        id=f"knowledge_framework_{slug}",
                        category="framework_feature",
                        label=label,
                        locator=f"knowledge:framework_feature/{slug}",
                        excerpt=excerpt,
                        confidence=confidence,
                        source_kinds=["input_inventory", "ast_index"],
                    )
                )

    def _extend_obfuscation_hits(
        self,
        *,
        hits: list[KnowledgeHit],
        inventory: dict[str, Any],
        ast_indexes: list[Any],
    ) -> None:
        symbols = self._symbols(ast_indexes)
        names = [str(symbol.get("name") or "") for symbol in symbols if isinstance(symbol, dict)]
        short_names = [name for name in names if 1 <= len(name) <= 2]
        source_maps = self._string_list(inventory.get("sourceMaps"))
        warnings = [
            *self._string_list(inventory.get("warnings")),
            *[warning for ast_index in ast_indexes for warning in self._string_list(self._mapping(ast_index).get("warnings"))],
        ]
        if names and len(short_names) / max(1, len(names)) >= 0.5 and not source_maps:
            hits.append(
                KnowledgeHit(
                    id="knowledge_obfuscation_short_symbols",
                    category="obfuscation_pattern",
                    label="Short symbol obfuscation pattern",
                    locator="knowledge:obfuscation_pattern/short_symbols",
                    excerpt="A high ratio of one- or two-character symbols without source maps suggests obfuscated or minified input.",
                    confidence=0.7,
                    source_kinds=["input_inventory", "ast_index"],
                )
            )
        if warnings:
            hits.append(
                KnowledgeHit(
                    id="knowledge_obfuscation_parse_warnings",
                    category="obfuscation_pattern",
                    label="Parse warning pattern",
                    locator="knowledge:obfuscation_pattern/parse_warnings",
                    excerpt="Inventory or AST parse warnings should reduce confidence and keep transformations conservative.",
                    confidence=0.62,
                    source_kinds=["input_inventory", "ast_index"],
                )
            )

    def _extend_browser_shim_hits(
        self,
        *,
        hits: list[KnowledgeHit],
        inventory: dict[str, Any],
        ast_indexes: list[Any],
    ) -> None:
        ast_text = "\n".join(self._ast_text(ast_indexes)).lower()
        shim_rules = [
            (
                "dom_globals",
                ("window", "document", "navigator", "location"),
                "Browser global usage",
                "Browser global symbols suggest runtime smoke should execute inside a browser-like environment.",
                0.66,
            ),
            (
                "node_globals",
                ("process", "buffer", "__dirname", "__filename", "global"),
                "Node global shim candidate",
                "Node-style globals in browser artifacts may require explicit shim review instead of silent rewriting.",
                0.65,
            ),
            (
                "global_this",
                ("globalthis",),
                "globalThis runtime assumption",
                "globalThis references should be preserved as runtime environment evidence for browser validation.",
                0.6,
            ),
        ]
        for slug, tokens, label, excerpt, confidence in shim_rules:
            if any(token in ast_text for token in tokens):
                hits.append(
                    KnowledgeHit(
                        id=f"knowledge_browser_shim_{slug}",
                        category="browser_shim",
                        label=label,
                        locator=f"knowledge:browser_shim/{slug}",
                        excerpt=excerpt,
                        confidence=confidence,
                        source_kinds=["ast_index"],
                    )
                )
        if not self._string_list(inventory.get("entries")) and self._string_list(inventory.get("scripts")):
            hits.append(
                KnowledgeHit(
                    id="knowledge_browser_shim_generated_host",
                    category="browser_shim",
                    label="Generated host page candidate",
                    locator="knowledge:browser_shim/generated_host",
                    excerpt="Script-only inputs need a generated host page before browser runtime evidence is authoritative.",
                    confidence=0.73,
                    source_kinds=["input_inventory"],
                )
            )

    def _extend_prior_evidence_hits(self, *, hits: list[KnowledgeHit], payloads: list[dict[str, Any]]) -> None:
        for payload in payloads:
            kind = str(payload.get("kind") or "")
            artifact_id = self._artifact_id(payload)
            failure_class = str(payload.get("failureClass") or "none")
            status = str(payload.get("status") or "")
            source_artifact_ids = [artifact_id] if artifact_id else []
            if kind in {"build_artifact", "build_log"} and status in {"fail", "retry", "best_effort"}:
                review_type = str(payload.get("reviewType") or payload.get("phase") or "build")
                slug = self._slug(f"{review_type}_{failure_class}") or "build_feedback"
                hits.append(
                    KnowledgeHit(
                        id=f"knowledge_validation_build_{slug}",
                        category="validation_feedback",
                        label=f"Build validation feedback: {review_type}",
                        locator=f"knowledge:validation_feedback/build/{slug}",
                        excerpt=self._excerpt(
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
                slug = self._slug(f"{target}_{failure_class}") or "runtime_validation"
                hits.append(
                    KnowledgeHit(
                        id=f"knowledge_validation_runtime_{slug}",
                        category="validation_feedback",
                        label=f"Runtime validation feedback: {target}",
                        locator=f"knowledge:validation_feedback/runtime/{slug}",
                        excerpt=self._runtime_validation_excerpt(payload),
                        confidence=0.8,
                        source_artifact_ids=source_artifact_ids,
                        source_kinds=[kind],
                    )
                )
            if kind == "runtime_trace":
                execution_boundary = payload.get("executionBoundary")
                source_kinds = [kind]
                if isinstance(execution_boundary, dict):
                    runner_kinds = self._runner_kinds(execution_boundary)
                    if runner_kinds:
                        slug = self._slug("_".join(runner_kinds)) or "runner"
                        hits.append(
                            KnowledgeHit(
                                id=f"knowledge_browser_shim_runtime_boundary_{slug}",
                                category="browser_shim",
                                label="Runtime execution boundary evidence",
                                locator=f"knowledge:browser_shim/runtime_boundary/{slug}",
                                excerpt=(
                                    "Existing runtime trace captured browser execution boundary "
                                    f"with runner(s): {', '.join(runner_kinds)}."
                                ),
                                confidence=0.77,
                                source_artifact_ids=source_artifact_ids,
                                source_kinds=source_kinds,
                            )
                        )
                if status in {"fail", "retry", "best_effort"}:
                    slug = self._slug(f"{payload.get('target') or 'runtime_trace'}_{failure_class}") or "runtime_trace"
                    hits.append(
                        KnowledgeHit(
                            id=f"knowledge_validation_runtime_trace_{slug}",
                            category="validation_feedback",
                            label="Runtime trace feedback",
                            locator=f"knowledge:validation_feedback/runtime_trace/{slug}",
                            excerpt=self._runtime_validation_excerpt(payload),
                            confidence=0.79,
                            source_artifact_ids=source_artifact_ids,
                            source_kinds=source_kinds,
                        )
                    )
            if kind == "runtime_comparison" and status in {"fail", "retry", "best_effort"}:
                slug = self._slug(str(payload.get("status") or "runtime_comparison")) or "runtime_comparison"
                hits.append(
                    KnowledgeHit(
                        id=f"knowledge_validation_runtime_comparison_{slug}",
                        category="validation_feedback",
                        label="Runtime comparison feedback",
                        locator=f"knowledge:validation_feedback/runtime_compare/{slug}",
                        excerpt="Existing runtime comparison differences should guide behavior-preserving repair review.",
                        confidence=0.82,
                        source_artifact_ids=source_artifact_ids,
                        source_kinds=[kind],
                    )
                )
            if kind == "review_run" and status in {"fail", "retry", "best_effort"}:
                review_type = str(payload.get("reviewType") or "review")
                slug = self._slug(f"{review_type}_{failure_class}") or "review_feedback"
                hits.append(
                    KnowledgeHit(
                        id=f"knowledge_review_feedback_{slug}",
                        category="validation_feedback",
                        label=f"Review feedback: {review_type}",
                        locator=f"knowledge:validation_feedback/review/{slug}",
                        excerpt=self._excerpt(
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
                slug = self._slug(f"{target_stage}_{risk}") or "repair_case"
                hits.append(
                    KnowledgeHit(
                        id=f"knowledge_repair_case_{slug}",
                        category="repair_case",
                        label=f"Current-job repair case: {target_stage}",
                        locator=f"knowledge:repair_case/current_job/{slug}",
                        excerpt=self._excerpt(
                            payload,
                            fallback="Existing repair instruction can be used as current-job historical context only.",
                        ),
                        confidence=0.84 if risk == "low" else 0.72,
                        source_artifact_ids=source_artifact_ids,
                        source_kinds=[kind],
                    )
                )

    def _extend_historical_repair_hits(self, *, hits: list[KnowledgeHit], payloads: list[dict[str, Any]]) -> None:
        for payload in payloads:
            kind = str(payload.get("kind") or "")
            artifact_id = self._artifact_id(payload)
            source_artifact_ids = [artifact_id] if artifact_id else []
            if kind == "repair_instruction":
                target_stage = str(payload.get("targetStage") or "repair")
                risk = str(payload.get("riskLevel") or "unknown")
                decision = self._excerpt(payload, fallback="Historical repair case available for evidence reference only.")
                slug = self._slug(f"{target_stage}_{risk}_{artifact_id or 'historical'}") or "historical_repair_case"
                hits.append(
                    KnowledgeHit(
                        id=f"knowledge_historical_repair_case_{slug}",
                        category="historical_repair_case",
                        label=f"Historical repair case: {target_stage}",
                        locator=f"knowledge:repair_case/historical/{slug}",
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
                    slug = self._slug(f"{review_type}_{failure_class}_{artifact_id or 'historical'}") or "historical_review"
                    hits.append(
                        KnowledgeHit(
                            id=f"knowledge_historical_review_feedback_{slug}",
                            category="historical_validation_feedback",
                            label=f"Historical review feedback: {review_type}",
                            locator=f"knowledge:validation_feedback/historical_review/{slug}",
                            excerpt=self._excerpt(
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
                    slug = self._slug(f"{status}_{failure_class}_{artifact_id or 'historical'}") or "historical_runtime_compare"
                    hits.append(
                        KnowledgeHit(
                            id=f"knowledge_historical_runtime_comparison_{slug}",
                            category="historical_validation_feedback",
                            label="Historical runtime comparison feedback",
                            locator=f"knowledge:validation_feedback/historical_runtime_compare/{slug}",
                            excerpt="Historical runtime comparison differences are evidence-only and same-project scoped.",
                            confidence=0.64,
                            source_artifact_ids=source_artifact_ids,
                            source_kinds=[kind],
                        )
                    )

    def _retrieval_sources(self, prior_payloads: list[dict[str, Any]], historical_payloads: list[dict[str, Any]]) -> dict[str, Any]:
        post_core_sources = [
            {
                "artifactId": self._artifact_id(payload),
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
                "artifactId": self._artifact_id(payload),
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

    def _dedupe_hits(self, hits: list[KnowledgeHit]) -> list[KnowledgeHit]:
        by_id: dict[str, KnowledgeHit] = {}
        for hit in hits:
            existing = by_id.get(hit.id)
            if existing is None or hit.confidence > existing.confidence:
                by_id[hit.id] = hit
        return list(by_id.values())

    def _has_export(self, ast_indexes: list[Any]) -> bool:
        for ast_index in ast_indexes:
            if isinstance(ast_index, dict) and ast_index.get("exports"):
                return True
        return False

    def _symbols(self, ast_indexes: list[Any]) -> list[dict[str, Any]]:
        symbols: list[dict[str, Any]] = []
        for ast_index in ast_indexes:
            raw_symbols = self._mapping(ast_index).get("symbols")
            if isinstance(raw_symbols, list):
                symbols.extend(symbol for symbol in raw_symbols if isinstance(symbol, dict))
        return symbols

    def _ast_text(self, ast_indexes: list[Any]) -> list[str]:
        chunks: list[str] = []
        for ast_index in ast_indexes:
            payload = self._mapping(ast_index)
            chunks.extend(self._string_list(payload.get("imports")))
            chunks.extend(self._string_list(payload.get("exports")))
            chunks.extend(str(symbol.get("name") or "") for symbol in self._symbols([payload]))
            chunks.extend(str(symbol.get("kind") or "") for symbol in self._symbols([payload]))
            chunks.append(str(payload.get("filePath") or ""))
        return chunks

    def _runtime_validation_excerpt(self, payload: dict[str, Any]) -> str:
        console_errors = self._string_list(payload.get("consoleErrors"))
        page_errors = self._string_list(payload.get("pageErrors"))
        failed_requests = self._string_list(payload.get("failedRequests"))
        details = [*console_errors[:2], *page_errors[:2], *failed_requests[:2]]
        if details:
            return "Runtime validation reported: " + "; ".join(details)
        return "Existing runtime validation evidence should guide Runtime and Repair Agent review."

    def _excerpt(self, payload: dict[str, Any], *, fallback: str) -> str:
        for key in ("decision", "diagnosis", "summary", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
        return fallback

    def _artifact_id(self, payload: dict[str, Any]) -> str | None:
        value = payload.get("artifactId") or payload.get("artifact_id")
        return value if isinstance(value, str) and value else None

    def _runner_kinds(self, execution_boundary: dict[str, Any]) -> list[str]:
        values: list[str] = []
        direct = execution_boundary.get("runnerKind")
        if isinstance(direct, str) and direct:
            values.append(direct)
        for nested_key in ("original", "reconstructed"):
            nested = execution_boundary.get(nested_key)
            if isinstance(nested, dict):
                nested_runner = nested.get("runnerKind")
                if isinstance(nested_runner, str) and nested_runner:
                    values.append(nested_runner)
        return list(dict.fromkeys(values))

    def _mapping(self, value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _list(self, value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    def _slug(self, value: str) -> str:
        normalized = []
        previous_separator = False
        for character in value.lower():
            if character.isalnum():
                normalized.append(character)
                previous_separator = False
            elif not previous_separator:
                normalized.append("_")
                previous_separator = True
        return "".join(normalized).strip("_")
