from __future__ import annotations

from typing import Any
from uuid import uuid4

from apps.api.app.models import MemoryRecord

from .context import JobMemoryContext


class JobMemoryService:
    """根据确定性 artifact 构建可审计的任务与项目记忆上下文。"""

    def create_context(
        self,
        *,
        job_id: str,
        project_id: str,
        source_artifact_ids: list[str],
        inventory_payload: dict[str, Any],
        ast_index_payload: dict[str, Any],
        cloud_mode: str,
    ) -> JobMemoryContext:
        memory_inputs = self._memory_inputs(
            inventory_payload=inventory_payload,
            ast_index_payload=ast_index_payload,
            cloud_mode=cloud_mode,
        )
        return JobMemoryContext(
            records=[
                self._record(
                    project_id=project_id,
                    job_id=job_id,
                    memory_type="short_term",
                    scope="job",
                    content=self._short_term_content(**memory_inputs),
                    source_artifact_ids=source_artifact_ids,
                ),
                self._record(
                    project_id=project_id,
                    job_id=job_id,
                    memory_type="long_term",
                    scope="project",
                    content=self._long_term_content(**memory_inputs),
                    source_artifact_ids=source_artifact_ids,
                ),
                self._record(
                    project_id=project_id,
                    job_id=job_id,
                    memory_type="entity",
                    scope="project",
                    content=self._entity_content(**memory_inputs),
                    source_artifact_ids=source_artifact_ids,
                ),
                self._record(
                    project_id=project_id,
                    job_id=job_id,
                    memory_type="scenario",
                    scope="project",
                    content=self._scenario_content(**memory_inputs),
                    source_artifact_ids=source_artifact_ids,
                ),
            ]
        )

    def _record(
        self,
        *,
        project_id: str,
        job_id: str,
        memory_type: str,
        scope: str,
        content: str,
        source_artifact_ids: list[str],
    ) -> MemoryRecord:
        return MemoryRecord(
            id=f"memory_{uuid4().hex[:12]}",
            scope=scope,  # type: ignore[arg-type]
            project_id=project_id,
            job_id=job_id,
            memory_type=memory_type,  # type: ignore[arg-type]
            content=content,
            source_artifact_ids=source_artifact_ids,
            sensitivity_class="derived",
            retention_class="project",
        )

    def _memory_inputs(
        self,
        *,
        inventory_payload: dict[str, Any],
        ast_index_payload: dict[str, Any],
        cloud_mode: str,
    ) -> dict[str, Any]:
        inventory = inventory_payload.get("inventory", {})
        ast_indexes = ast_index_payload.get("astIndexes", [])
        entries = self._list_excerpt(inventory.get("entries"), limit=8)
        scripts = self._list_excerpt(inventory.get("scripts"), limit=8)
        styles = self._list_excerpt(inventory.get("styles"), limit=8)
        assets = self._list_excerpt(inventory.get("assets"), limit=8)
        source_maps = self._list_excerpt(inventory.get("sourceMaps"), limit=8)
        symbol_names = self._symbol_names(ast_indexes)
        runtime_patterns = self._list_excerpt(ast_index_payload.get("detectedRuntime"), limit=8)
        return {
            "cloud_mode": cloud_mode,
            "entries": entries,
            "scripts": scripts,
            "styles": styles,
            "assets": assets,
            "source_maps": source_maps,
            "symbol_names": symbol_names,
            "runtime_patterns": runtime_patterns,
        }

    def _short_term_content(
        self,
        *,
        cloud_mode: str,
        entries: list[str],
        scripts: list[str],
        styles: list[str],
        assets: list[str],
        source_maps: list[str],
        symbol_names: list[str],
        runtime_patterns: list[str],
    ) -> str:
        return (
            f"任务短期记忆：cloudMode={cloud_mode}；"
            f"entries={entries}；scripts={scripts}；styles={styles}；"
            f"symbolCount={len(symbol_names)}；symbolSample={symbol_names[:8]}"
        )

    def _long_term_content(
        self,
        *,
        cloud_mode: str,
        entries: list[str],
        scripts: list[str],
        styles: list[str],
        assets: list[str],
        source_maps: list[str],
        symbol_names: list[str],
        runtime_patterns: list[str],
    ) -> str:
        source_map_state = "存在" if source_maps else "缺失"
        bundle_shape = "多脚本" if len(scripts) > 1 else "单脚本"
        return (
            "项目长期记忆候选："
            f"bundleShape={bundle_shape}；sourceMaps={source_map_state}；"
            f"runtimePatterns={runtime_patterns}；cloudMode={cloud_mode}；"
            "仅作为项目范围内的派生模式证据保留。"
        )

    def _entity_content(
        self,
        *,
        cloud_mode: str,
        entries: list[str],
        scripts: list[str],
        styles: list[str],
        assets: list[str],
        source_maps: list[str],
        symbol_names: list[str],
        runtime_patterns: list[str],
    ) -> str:
        return (
            "项目实体记忆："
            f"entries={entries}；scripts={scripts}；styles={styles}；assets={assets[:5]}；"
            f"symbolFamilies={symbol_names[:12]}"
        )

    def _scenario_content(
        self,
        *,
        cloud_mode: str,
        entries: list[str],
        scripts: list[str],
        styles: list[str],
        assets: list[str],
        source_maps: list[str],
        symbol_names: list[str],
        runtime_patterns: list[str],
    ) -> str:
        validation_focus = "runtime_compare" if entries else "minimal_host_assumption"
        return (
            "项目场景记忆："
            f"validationFocus={validation_focus}；scriptCount={len(scripts)}；"
            f"styleCount={len(styles)}；sourceMapCount={len(source_maps)}；"
            "可复用于相似构建 artifact 的分析与修复分诊。"
        )

    def _list_excerpt(self, value: Any, *, limit: int = 8) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value[:limit] if isinstance(item, str)]

    def _symbol_names(self, ast_indexes: Any) -> list[str]:
        if not isinstance(ast_indexes, list):
            return []
        names: list[str] = []
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
