from __future__ import annotations

from typing import Any

from .models import KnowledgeHit
from .utils import ast_text, has_export, mapping, slug, string_list, symbols


def extend_core_hits(
    *,
    hits: list[KnowledgeHit],
    inventory: dict[str, Any],
    ast_indexes: list[Any],
    detected_runtime: list[str],
) -> None:
    entries = string_list(inventory.get("entries"))
    scripts = string_list(inventory.get("scripts"))
    styles = string_list(inventory.get("styles"))
    source_maps = string_list(inventory.get("sourceMaps"))
    manifests = string_list(inventory.get("manifests"))

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
    if has_export(ast_indexes):
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
        runtime_id = slug(runtime)
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


def extend_framework_hits(
    *,
    hits: list[KnowledgeHit],
    inventory: dict[str, Any],
    ast_indexes: list[Any],
) -> None:
    haystack = "\n".join(
        [
            *string_list(inventory.get("scripts")),
            *string_list(inventory.get("styles")),
            *string_list(inventory.get("manifests")),
            *ast_text(ast_indexes),
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
    for slug_value, tokens, label, excerpt, confidence in framework_rules:
        if any(token in haystack for token in tokens):
            hits.append(
                KnowledgeHit(
                    id=f"knowledge_framework_{slug_value}",
                    category="framework_feature",
                    label=label,
                    locator=f"knowledge:framework_feature/{slug_value}",
                    excerpt=excerpt,
                    confidence=confidence,
                    source_kinds=["input_inventory", "ast_index"],
                )
            )


def extend_obfuscation_hits(
    *,
    hits: list[KnowledgeHit],
    inventory: dict[str, Any],
    ast_indexes: list[Any],
) -> None:
    names = [str(symbol.get("name") or "") for symbol in symbols(ast_indexes) if isinstance(symbol, dict)]
    short_names = [name for name in names if 1 <= len(name) <= 2]
    source_maps = string_list(inventory.get("sourceMaps"))
    warnings = [
        *string_list(inventory.get("warnings")),
        *[
            warning
            for ast_index in ast_indexes
            for warning in string_list(mapping(ast_index).get("warnings"))
        ],
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


def extend_browser_shim_hits(
    *,
    hits: list[KnowledgeHit],
    inventory: dict[str, Any],
    ast_indexes: list[Any],
) -> None:
    ast_blob = "\n".join(ast_text(ast_indexes)).lower()
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
    for slug_value, tokens, label, excerpt, confidence in shim_rules:
        if any(token in ast_blob for token in tokens):
            hits.append(
                KnowledgeHit(
                    id=f"knowledge_browser_shim_{slug_value}",
                    category="browser_shim",
                    label=label,
                    locator=f"knowledge:browser_shim/{slug_value}",
                    excerpt=excerpt,
                    confidence=confidence,
                    source_kinds=["ast_index"],
                )
            )
    if not string_list(inventory.get("entries")) and string_list(inventory.get("scripts")):
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
