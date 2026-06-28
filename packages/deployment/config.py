from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Literal, Mapping


ServiceRole = Literal["api", "worker", "browser-runner", "web", "db", "artifact-store"]
SERVICE_ROLE_ENV = "AI_JSUNPACK_SERVICE_ROLE"

_VALID_ROLES: set[str] = {"api", "worker", "browser-runner", "web", "db", "artifact-store"}

_API_FORBIDDEN_EXACT: dict[str, tuple[str, str]] = {
    "AI_JSUNPACK_AGENT_MODEL": ("worker", "cloud model selection belongs to the Worker/Agent runtime"),
    "AI_JSUNPACK_AGENT_PROVIDER": ("worker", "cloud model provider selection belongs to the Worker/Agent runtime"),
    "AI_JSUNPACK_LOCAL_AGENT_MODEL": ("worker", "local model selection belongs to the Worker/Agent runtime"),
    "AI_JSUNPACK_LOCAL_AGENT_PROVIDER": ("worker", "local model provider selection belongs to the Worker/Agent runtime"),
    "AI_JSUNPACK_CORE_CLI_PATH": ("worker", "Core CLI execution belongs to the Worker"),
    "AI_JSUNPACK_NODE_BINARY": ("worker", "Node/Core execution belongs to the Worker"),
    "AI_JSUNPACK_CREWAI_DATA_ROOT": ("worker", "CrewAI storage belongs to the Worker/Agent runtime"),
    "CREWAI_STORAGE_DIR": ("worker", "CrewAI storage belongs to the Worker/Agent runtime"),
    "OPENAI_API_KEY": ("worker", "model provider credentials must not be present in the API service"),
    "ANTHROPIC_API_KEY": ("worker", "model provider credentials must not be present in the API service"),
    "GOOGLE_API_KEY": ("worker", "model provider credentials must not be present in the API service"),
    "AZURE_OPENAI_API_KEY": ("worker", "model provider credentials must not be present in the API service"),
    "OLLAMA_ENDPOINT": ("worker", "model provider endpoints belong to the Worker/Agent runtime"),
}

_API_FORBIDDEN_PREFIXES: dict[str, tuple[str, str]] = {
    "AI_JSUNPACK_SANDBOX_": ("worker", "sandbox execution configuration belongs to the Worker or Browser Runner"),
    "AI_JSUNPACK_BROWSER_RUNNER_": ("browser-runner", "browser execution configuration belongs to the Browser Runner"),
    "AI_JSUNPACK_AGENT_": ("worker", "Agent endpoint configuration belongs to the Worker/Agent runtime"),
    "AI_JSUNPACK_LOCAL_AGENT_": ("worker", "local Agent endpoint configuration belongs to the Worker/Agent runtime"),
}

_WORKER_EXECUTION_PREFIXES: tuple[str, ...] = (
    "AI_JSUNPACK_SANDBOX_",
    "AI_JSUNPACK_AGENT_",
    "AI_JSUNPACK_LOCAL_AGENT_",
    "AI_JSUNPACK_CORE_",
)


class DeploymentConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentViolation:
    name: str
    owner_role: str
    reason: str


@dataclass(frozen=True)
class DeploymentProfile:
    role: str
    expected_role: str | None
    strict: bool
    violations: tuple[DeploymentViolation, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.violations:
            return "invalid" if self.strict else "warning"
        if self.warnings:
            return "warning"
        return "ok"

    def summary(self) -> str:
        if not self.violations:
            return f"{self.role} deployment profile is {self.status}."
        details = "; ".join(
            f"{violation.name} is owned by {violation.owner_role}: {violation.reason}"
            for violation in self.violations
        )
        return f"{self.role} deployment profile is invalid: {details}"


def validate_current_environment(
    expected_role: ServiceRole,
    *,
    environ: Mapping[str, str | None] | None = None,
    strict: bool | None = None,
) -> DeploymentProfile:
    env = os.environ if environ is None else environ
    configured_role = _clean(env.get(SERVICE_ROLE_ENV))
    effective_role = configured_role or expected_role
    profile = validate_service_environment(effective_role, environ=env)
    violations = list(profile.violations)

    if configured_role and configured_role != expected_role:
        violations.insert(
            0,
            DeploymentViolation(
                name=SERVICE_ROLE_ENV,
                owner_role=expected_role,
                reason=f"expected {expected_role!r}, got {configured_role!r}",
            ),
        )

    effective_strict = bool(configured_role) if strict is None else strict
    result = replace(profile, expected_role=expected_role, strict=effective_strict, violations=tuple(violations))
    if result.strict and result.violations:
        raise DeploymentConfigurationError(result.summary())
    return result


def validate_service_environment(
    role: str,
    *,
    environ: Mapping[str, str | None] | None = None,
) -> DeploymentProfile:
    env = os.environ if environ is None else environ
    normalized_role = _normalize_role(role)
    active_names = _active_env_names(env)

    violations: list[DeploymentViolation] = []
    warnings: list[str] = []
    if normalized_role not in _VALID_ROLES:
        violations.append(
            DeploymentViolation(
                name=SERVICE_ROLE_ENV,
                owner_role="deployment",
                reason=f"unknown service role {role!r}",
            )
        )
        return DeploymentProfile(
            role=normalized_role,
            expected_role=None,
            strict=True,
            violations=tuple(violations),
        )

    if normalized_role == "api":
        violations.extend(_api_execution_violations(active_names))
    elif normalized_role == "worker":
        warnings.extend(_worker_warnings(active_names))

    return DeploymentProfile(
        role=normalized_role,
        expected_role=None,
        strict=True,
        violations=tuple(sorted(violations, key=lambda item: item.name)),
        warnings=tuple(warnings),
    )


def _api_execution_violations(active_names: set[str]) -> list[DeploymentViolation]:
    violations: list[DeploymentViolation] = []
    for name in active_names:
        if name in _API_FORBIDDEN_EXACT:
            owner_role, reason = _API_FORBIDDEN_EXACT[name]
            violations.append(DeploymentViolation(name=name, owner_role=owner_role, reason=reason))
            continue
        for prefix, (owner_role, reason) in _API_FORBIDDEN_PREFIXES.items():
            if name.startswith(prefix):
                violations.append(DeploymentViolation(name=name, owner_role=owner_role, reason=reason))
                break
    return violations


def _worker_warnings(active_names: set[str]) -> list[str]:
    sandbox_runner = "AI_JSUNPACK_SANDBOX_RUNNER" in active_names
    execution_configured = any(
        name.startswith(prefix) for prefix in _WORKER_EXECUTION_PREFIXES for name in active_names
    )
    if not sandbox_runner and execution_configured:
        return ["Worker execution configuration is present without AI_JSUNPACK_SANDBOX_RUNNER; local runner remains the default."]
    return []


def _active_env_names(environ: Mapping[str, str | None]) -> set[str]:
    return {name for name, value in environ.items() if _clean(value)}


def _clean(value: str | None) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_role(role: str) -> str:
    return role.strip().lower().replace("_", "-")
