from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from apps.api.app.models import MemoryRecord


@dataclass(frozen=True)
class JobMemoryContext:
    records: list[MemoryRecord]

    @property
    def prompt_excerpt(self) -> str:
        return "\n".join(f"{record.memory_type}: {record.content}" for record in self.records)

    @property
    def short_term_record(self) -> MemoryRecord:
        return self.records[0]


class JobMemoryService:
    """基于确定性 Artifact 构建可审计的 Job 与项目记忆上下文。"""

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
            f"Job short-term memory: cloudMode={cloud_mode}; "
            f"entries={entries}; scripts={scripts}; styles={styles}; "
            f"symbolCount={len(symbol_names)}; symbolSample={symbol_names[:8]}"
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
        source_map_state = "present" if source_maps else "missing"
        bundle_shape = "multi-script" if len(scripts) > 1 else "single-script"
        return (
            "Project long-term memory candidate: "
            f"bundleShape={bundle_shape}; sourceMaps={source_map_state}; "
            f"runtimePatterns={runtime_patterns}; cloudMode={cloud_mode}; "
            "retain as project-scoped derived pattern evidence only."
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
            "Project entity memory: "
            f"entries={entries}; scripts={scripts}; styles={styles}; assets={assets[:5]}; "
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
            "Project scenario memory: "
            f"validationFocus={validation_focus}; scriptCount={len(scripts)}; "
            f"styleCount={len(styles)}; sourceMapCount={len(source_maps)}; "
            "reuse for similar build artifact analysis and repair triage."
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
