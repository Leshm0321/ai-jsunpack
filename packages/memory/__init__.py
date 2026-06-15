from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from apps.api.app.models import MemoryRecord


@dataclass(frozen=True)
class JobMemoryContext:
    record: MemoryRecord

    @property
    def prompt_excerpt(self) -> str:
        return self.record.content


class JobMemoryService:
    """Builds job-local memory context from deterministic artifacts."""

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
        content = self._content(
            inventory_payload=inventory_payload,
            ast_index_payload=ast_index_payload,
            cloud_mode=cloud_mode,
        )
        return JobMemoryContext(
            record=MemoryRecord(
                id=f"memory_{uuid4().hex[:12]}",
                scope="job",
                project_id=project_id,
                job_id=job_id,
                memory_type="short_term",
                content=content,
                source_artifact_ids=source_artifact_ids,
                sensitivity_class="derived",
                retention_class="project",
            )
        )

    def _content(
        self,
        *,
        inventory_payload: dict[str, Any],
        ast_index_payload: dict[str, Any],
        cloud_mode: str,
    ) -> str:
        inventory = inventory_payload.get("inventory", {})
        ast_indexes = ast_index_payload.get("astIndexes", [])
        entries = self._list_excerpt(inventory.get("entries"))
        scripts = self._list_excerpt(inventory.get("scripts"))
        styles = self._list_excerpt(inventory.get("styles"))
        symbol_names = self._symbol_names(ast_indexes)
        return (
            f"Job short-term memory: cloudMode={cloud_mode}; "
            f"entries={entries}; scripts={scripts}; styles={styles}; "
            f"symbolCount={len(symbol_names)}; symbolSample={symbol_names[:8]}"
        )

    def _list_excerpt(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value[:8] if isinstance(item, str)]

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
