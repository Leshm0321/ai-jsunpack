from __future__ import annotations

from typing import Any


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def has_export(ast_indexes: list[Any]) -> bool:
    for ast_index in ast_indexes:
        if isinstance(ast_index, dict) and ast_index.get("exports"):
            return True
    return False


def symbols(ast_indexes: list[Any]) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for ast_index in ast_indexes:
        raw_symbols = mapping(ast_index).get("symbols")
        if isinstance(raw_symbols, list):
            collected.extend(symbol for symbol in raw_symbols if isinstance(symbol, dict))
    return collected


def ast_text(ast_indexes: list[Any]) -> list[str]:
    chunks: list[str] = []
    for ast_index in ast_indexes:
        payload = mapping(ast_index)
        chunks.extend(string_list(payload.get("imports")))
        chunks.extend(string_list(payload.get("exports")))
        chunks.extend(str(symbol.get("name") or "") for symbol in symbols([payload]))
        chunks.extend(str(symbol.get("kind") or "") for symbol in symbols([payload]))
        chunks.append(str(payload.get("filePath") or ""))
    return chunks


def runtime_validation_excerpt(payload: dict[str, Any]) -> str:
    console_errors = string_list(payload.get("consoleErrors"))
    page_errors = string_list(payload.get("pageErrors"))
    failed_requests = string_list(payload.get("failedRequests"))
    details = [*console_errors[:2], *page_errors[:2], *failed_requests[:2]]
    if details:
        return "Runtime validation reported: " + "; ".join(details)
    return "Existing runtime validation evidence should guide Runtime and Repair Agent review."


def excerpt(payload: dict[str, Any], *, fallback: str) -> str:
    for key in ("decision", "diagnosis", "summary", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    return fallback


def artifact_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("artifactId") or payload.get("artifact_id")
    return value if isinstance(value, str) and value else None


def runner_kinds(execution_boundary: dict[str, Any]) -> list[str]:
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


def slug(value: str) -> str:
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
