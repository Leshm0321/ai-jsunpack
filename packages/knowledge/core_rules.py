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
                label="已发现 HTML 入口",
                locator="knowledge:runtime_entry/html",
                excerpt="HTML 入口通常会提供脚本、样式表、公共路径和运行时加载证据。",
                confidence=0.8,
                source_kinds=["input_inventory"],
            )
        )
    else:
        hits.append(
            KnowledgeHit(
                id="knowledge_browser_shim_missing_html_entry",
                category="browser_shim",
                label="未发现 HTML 入口",
                locator="knowledge:browser_shim/html_entry",
                excerpt="缺少 HTML 入口时，通常需要先生成确定性宿主页，才能运行浏览器验证。",
                confidence=0.72,
                source_kinds=["input_inventory"],
            )
        )
    if source_maps:
        hits.append(
            KnowledgeHit(
                id="knowledge_source_map_available",
                category="source_map",
                label="已发现 source map 候选",
                locator="knowledge:source_map/available",
                excerpt="source map 可用于生成源文件候选，但不能替代当前 artifact 证据。",
                confidence=0.7,
                source_kinds=["input_inventory"],
            )
        )
    if has_export(ast_indexes):
        hits.append(
            KnowledgeHit(
                id="knowledge_esm_exports",
                category="module_pattern",
                label="ES 模块导出证据",
                locator="knowledge:module_pattern/esm",
                excerpt="具名导出可提示模块边界，并指导重建规划。",
                confidence=0.65,
                source_kinds=["ast_index"],
            )
        )
    if len(scripts) > 1 or "multi_chunk" in detected_runtime:
        hits.append(
            KnowledgeHit(
                id="knowledge_runtime_multi_chunk",
                category="build_runtime",
                label="多 chunk 运行时模式",
                locator="knowledge:build_runtime/multi_chunk",
                excerpt="多个 JavaScript chunk 表明应保留加载器顺序和公共路径证据。",
                confidence=0.74,
                source_kinds=["input_inventory", "ast_index"],
            )
        )
    if inventory.get("isSingleBundle") or "single_bundle_best_effort" in detected_runtime:
        hits.append(
            KnowledgeHit(
                id="knowledge_runtime_single_bundle",
                category="build_runtime",
                label="单 bundle 尽力而为模式",
                locator="knowledge:build_runtime/single_bundle",
                excerpt="缺少完整源码图的单个 bundle 应让重建更倾向于保守的静态托管。",
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
                excerpt="Manifest 文件可能包含 asset、route 或 Service Worker 元数据，这些信息应继续受证据约束。",
                confidence=0.64,
                source_kinds=["input_inventory"],
            )
        )
    if styles:
        hits.append(
            KnowledgeHit(
                id="knowledge_runtime_stylesheet_assets",
                category="build_runtime",
                label="已发现样式表资源",
                locator="knowledge:build_runtime/stylesheets",
                excerpt="样式表资源属于运行时可见证据，应纳入浏览器对比场景。",
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
                    label=f"检测到的运行时：{runtime}",
                    locator=f"knowledge:build_runtime/{runtime_id}",
                    excerpt=f"Core 根据本地 Manifest 和 AST 证据检测到 {runtime}。",
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
            "React 框架特征",
            "React 导入、JSX helper 或 React Hook 符号应指导组件边界推断。",
            0.72,
        ),
        (
            "vue",
            ("vue", "createapp", "definecomponent", "__vue", ".vue"),
            "Vue 框架特征",
            "Vue 运行时或组件符号应指导组件与模板重建假设。",
            0.72,
        ),
        (
            "vite_rollup",
            ("vite", "import.meta", "vite_or_rollup", "assets/"),
            "Vite/Rollup 构建特征",
            "Vite 或 Rollup 的资源约定应保留模块脚本和公共资源路径假设。",
            0.68,
        ),
        (
            "webpack",
            ("__webpack_require__", "webpack", "webpackchunk"),
            "Webpack runtime 特征",
            "分析期间应依据 Webpack runtime 标记保留 chunk loader 和 public path 证据。",
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
                label="短符号混淆模式",
                locator="knowledge:obfuscation_pattern/short_symbols",
                excerpt="在没有 source map 的情况下，大量一至两个字符的符号表明输入可能经过混淆或压缩。",
                confidence=0.7,
                source_kinds=["input_inventory", "ast_index"],
            )
        )
    if warnings:
        hits.append(
            KnowledgeHit(
                id="knowledge_obfuscation_parse_warnings",
                category="obfuscation_pattern",
                label="解析警告模式",
                locator="knowledge:obfuscation_pattern/parse_warnings",
                excerpt="Manifest 或 AST parse warning 应降低置信度，并让转换保持保守。",
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
            "浏览器全局对象使用情况",
            "浏览器全局符号表明运行时冒烟测试应在类浏览器环境中执行。",
            0.66,
        ),
        (
            "node_globals",
            ("process", "buffer", "__dirname", "__filename", "global"),
            "Node 全局对象垫片候选",
            "浏览器 Artifact 中的 Node 风格全局对象可能需要明确审查 shim，而不是静默重写。",
            0.65,
        ),
        (
            "global_this",
            ("globalthis",),
            "globalThis 运行时假设",
            "应保留 globalThis 引用，将其作为浏览器验证的运行时环境证据。",
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
                label="生成宿主页候选",
                locator="knowledge:browser_shim/generated_host",
                excerpt="仅含脚本的输入需要先生成宿主页，浏览器运行时证据才具有权威性。",
                confidence=0.73,
                source_kinds=["input_inventory"],
            )
        )
