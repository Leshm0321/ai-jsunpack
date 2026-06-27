from __future__ import annotations

from dataclasses import dataclass

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
