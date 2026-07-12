from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Callable
from typing import Any, Protocol, TextIO
from pathlib import Path

from pydantic import ValidationError

from .agent_contracts import CREWAI_DATA_ROOT_ENV, AgentModelPolicy, CrewAgentSpec
from .agent_process import AGENT_PROCESS_PROTOCOL_VERSION
from .agent_providers import CrewAIBackend


class AgentBackend(Protocol):
    def run_agent(
        self,
        *,
        spec: CrewAgentSpec,
        prompt_context: dict[str, Any],
        policy: AgentModelPolicy,
    ) -> Any:
        ...


def run_request(payload: Any, *, backend_factory: Callable[[], AgentBackend] = CrewAIBackend) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("protocolVersion") != AGENT_PROCESS_PROTOCOL_VERSION:
        return _error_response("protocol_error")
    try:
        data_root = str(payload["dataRoot"])
        spec_payload = payload["spec"]
        policy_payload = payload["policy"]
        prompt_context = payload["promptContext"]
        if not isinstance(spec_payload, dict) or not isinstance(policy_payload, dict):
            raise TypeError("Invalid structured request.")
        if not isinstance(prompt_context, dict):
            raise TypeError("Invalid prompt context.")
        spec = CrewAgentSpec(**spec_payload)
        policy = AgentModelPolicy(**policy_payload)
    except (KeyError, TypeError, ValueError):
        return _error_response("invalid_request")

    previous_environment = _configure_isolated_environment(data_root)
    try:
        # CrewAI and provider libraries may write diagnostic text. The protocol deliberately
        # discards it so stdout remains JSON-only and secrets cannot enter parent artifacts.
        with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(
            sink
        ), contextlib.redirect_stderr(sink):
            output = backend_factory().run_agent(spec=spec, prompt_context=prompt_context, policy=policy)
        return {
            "protocolVersion": AGENT_PROCESS_PROTOCOL_VERSION,
            "status": "success",
            "output": output.model_dump(by_alias=True, exclude_none=True),
        }
    except (ValidationError, json.JSONDecodeError):
        return _error_response("schema_error")
    except Exception:  # noqa: BLE001 - child boundary must translate every backend failure.
        return _error_response("backend_error")
    finally:
        _restore_environment(previous_environment)


def main(
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    backend_factory: Callable[[], AgentBackend] = CrewAIBackend,
) -> int:
    source = input_stream or sys.stdin
    destination = output_stream or sys.stdout
    try:
        payload = json.loads(source.read())
    except json.JSONDecodeError:
        response = _error_response("invalid_json")
    else:
        response = run_request(payload, backend_factory=backend_factory)
    destination.write(json.dumps(response, ensure_ascii=False) + "\n")
    destination.flush()
    return 0


def _error_response(kind: str) -> dict[str, Any]:
    return {
        "protocolVersion": AGENT_PROCESS_PROTOCOL_VERSION,
        "status": "error",
        "errorKind": kind,
        "message": "CrewAI child invocation failed.",
    }


def _configure_isolated_environment(data_root: str) -> dict[str, str | None]:
    root = Path(data_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    names = (
        CREWAI_DATA_ROOT_ENV,
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "XDG_DATA_HOME",
        "CREWAI_STORAGE_DIR",
    )
    previous = {name: os.environ.get(name) for name in names}
    for name in names[:-1]:
        os.environ[name] = str(root)
    os.environ["CREWAI_STORAGE_DIR"] = "ai-jsunpack"
    return previous


def _restore_environment(previous: dict[str, str | None]) -> None:
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


if __name__ == "__main__":
    raise SystemExit(main())
