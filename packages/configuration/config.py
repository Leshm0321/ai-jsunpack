from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


CONFIG_FILE_ENV = "AI_JSUNPACK_CONFIG_FILE"


def _to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


class ConfigurationError(ValueError):
    pass


class ConfigModel(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel, extra="forbid", populate_by_name=True)


class SharedConfig(ConfigModel):
    deployment_profile: Literal["development", "test", "production"] = "development"
    log_level: Literal["debug", "info", "warning", "error"] = "info"


class DatabaseConfig(ConfigModel):
    url_secret_ref: str | None = None


class ApiConfig(ConfigModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://127.0.0.1:5173", "http://localhost:5173"]
    )
    max_upload_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    artifact_root: str = "artifacts"
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)


class ProviderConfig(ConfigModel):
    provider: str = "openai-compatible"
    model: str | None = None
    base_url: str | None = None
    api_key_secret_ref: str | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return _validated_base_url(value)


class AgentConfig(ConfigModel):
    cloud: ProviderConfig = Field(default_factory=ProviderConfig)
    local: ProviderConfig = Field(
        default_factory=lambda: ProviderConfig(base_url="http://127.0.0.1:11434/v1")
    )


class SandboxConfig(ConfigModel):
    runner: Literal["local", "container", "gvisor", "firecracker"] = "local"
    allow_local_in_development: bool = True


class WorkerConfig(ConfigModel):
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)


class BrowserRunnerConfig(ConfigModel):
    mode: Literal["local", "remote"] = "local"
    base_url: str | None = None
    token_secret_ref: str | None = None


class WebConfig(ConfigModel):
    api_base_url: str = "http://127.0.0.1:8000"


class ApplicationConfig(ConfigModel):
    version: Literal[1] = 1
    shared: SharedConfig = Field(default_factory=SharedConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    browser_runner: BrowserRunnerConfig = Field(default_factory=BrowserRunnerConfig)
    web: WebConfig = Field(default_factory=WebConfig)


class RuntimeProviderSettings(ConfigModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_secret_ref: str | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return _validated_base_url(value)


class RuntimeAISettings(ConfigModel):
    cloud: RuntimeProviderSettings = Field(default_factory=RuntimeProviderSettings)
    local: RuntimeProviderSettings = Field(default_factory=RuntimeProviderSettings)


class RuntimeAgentSettings(ConfigModel):
    enabled: bool = True
    max_parallel: int = Field(default=5, ge=1, le=10)
    context_budget: int = Field(default=16_000, ge=1_000, le=1_000_000)


class RuntimeValidationSettings(ConfigModel):
    run_typecheck: bool = True
    run_runtime_compare: bool = True
    minimum_confidence: float = Field(default=0.7, ge=0, le=1)


class RuntimeSettings(ConfigModel):
    ai: RuntimeAISettings = Field(default_factory=RuntimeAISettings)
    agents: RuntimeAgentSettings = Field(default_factory=RuntimeAgentSettings)
    validation: RuntimeValidationSettings = Field(default_factory=RuntimeValidationSettings)


class RuntimeProviderSettingsPatch(ConfigModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_secret_ref: str | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return _validated_base_url(value)


class RuntimeAISettingsPatch(ConfigModel):
    cloud: RuntimeProviderSettingsPatch | None = None
    local: RuntimeProviderSettingsPatch | None = None


class RuntimeAgentSettingsPatch(ConfigModel):
    enabled: bool | None = None
    max_parallel: int | None = Field(default=None, ge=1, le=10)
    context_budget: int | None = Field(default=None, ge=1_000, le=1_000_000)


class RuntimeValidationSettingsPatch(ConfigModel):
    run_typecheck: bool | None = None
    run_runtime_compare: bool | None = None
    minimum_confidence: float | None = Field(default=None, ge=0, le=1)


class RuntimeSettingsPatch(ConfigModel):
    ai: RuntimeAISettingsPatch | None = None
    agents: RuntimeAgentSettingsPatch | None = None
    validation: RuntimeValidationSettingsPatch | None = None


class LoadedConfiguration(ConfigModel):
    config: ApplicationConfig
    source: Literal["defaults", "file", "environment"]
    config_file: str | None = None
    fingerprint: str


ENVIRONMENT_OVERRIDES: dict[str, tuple[str, ...]] = {
    "AI_JSUNPACK_DEPLOYMENT_PROFILE": ("shared", "deploymentProfile"),
    "AI_JSUNPACK_LOG_LEVEL": ("shared", "logLevel"),
    "AI_JSUNPACK_API_HOST": ("api", "host"),
    "AI_JSUNPACK_API_PORT": ("api", "port"),
    "AI_JSUNPACK_CORS_ORIGINS": ("api", "corsOrigins"),
    "AI_JSUNPACK_MAX_UPLOAD_BYTES": ("api", "maxUploadBytes"),
    "AI_JSUNPACK_ARTIFACT_ROOT": ("api", "artifactRoot"),
    "AI_JSUNPACK_AGENT_PROVIDER": ("worker", "agent", "cloud", "provider"),
    "AI_JSUNPACK_AGENT_MODEL": ("worker", "agent", "cloud", "model"),
    "AI_JSUNPACK_AGENT_BASE_URL": ("worker", "agent", "cloud", "baseUrl"),
    "AI_JSUNPACK_AGENT_API_KEY_SECRET_REF": ("worker", "agent", "cloud", "apiKeySecretRef"),
    "AI_JSUNPACK_LOCAL_AGENT_PROVIDER": ("worker", "agent", "local", "provider"),
    "AI_JSUNPACK_LOCAL_AGENT_MODEL": ("worker", "agent", "local", "model"),
    "AI_JSUNPACK_LOCAL_AGENT_BASE_URL": ("worker", "agent", "local", "baseUrl"),
    "AI_JSUNPACK_LOCAL_AGENT_API_KEY_SECRET_REF": ("worker", "agent", "local", "apiKeySecretRef"),
}


def load_application_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> LoadedConfiguration:
    env = os.environ if environ is None else environ
    selected_path = Path(path) if path is not None else _optional_path(env.get(CONFIG_FILE_ENV))
    payload: dict[str, Any] = {}
    source: Literal["defaults", "file", "environment"] = "defaults"
    if selected_path is not None:
        payload = _load_config_file(selected_path)
        source = "file"
    overridden = _environment_payload(env)
    if overridden:
        payload = _deep_merge(payload, overridden)
        source = "environment"
    try:
        config = ApplicationConfig.model_validate(payload)
    except ValidationError as error:
        raise ConfigurationError(str(error)) from error
    redacted = redact_secrets(config.model_dump(mode="json", by_alias=True))
    serialized = json.dumps(redacted, separators=(",", ":"), sort_keys=True)
    return LoadedConfiguration(
        config=config,
        source=source,
        config_file=str(selected_path.resolve()) if selected_path is not None else None,
        fingerprint=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )


def apply_application_config_to_environment(
    service_role: Literal["api", "worker", "browser-runner", "web"],
    path: str | Path | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> LoadedConfiguration:
    target = os.environ if environ is None else environ
    loaded = load_application_config(path, environ=target)
    selected_path = path is not None or bool(target.get(CONFIG_FILE_ENV, "").strip())
    if not selected_path:
        return loaded
    config = loaded.config
    values: dict[str, str] = {
        "AI_JSUNPACK_DEPLOYMENT_PROFILE": config.shared.deployment_profile,
        "AI_JSUNPACK_LOG_LEVEL": config.shared.log_level,
    }
    if service_role == "api":
        values.update(
            {
                "AI_JSUNPACK_API_HOST": config.api.host,
                "AI_JSUNPACK_API_PORT": str(config.api.port),
                "AI_JSUNPACK_CORS_ORIGINS": ",".join(config.api.cors_origins),
                "AI_JSUNPACK_MAX_UPLOAD_BYTES": str(config.api.max_upload_bytes),
                "AI_JSUNPACK_ARTIFACT_ROOT": config.api.artifact_root,
            }
        )
    elif service_role == "worker":
        values.update(_worker_environment(config))
    elif service_role == "browser-runner":
        if config.browser_runner.base_url:
            values["AI_JSUNPACK_BROWSER_RUNNER_URL"] = config.browser_runner.base_url
    elif service_role == "web":
        values["VITE_API_BASE_URL"] = config.web.api_base_url
    for name, value in values.items():
        target.setdefault(name, value)
    return loaded


def merge_runtime_settings(*patches: RuntimeSettingsPatch | Mapping[str, Any] | None) -> RuntimeSettings:
    payload = RuntimeSettings().model_dump(mode="json", by_alias=True)
    for patch in patches:
        if patch is None:
            continue
        if isinstance(patch, RuntimeSettingsPatch):
            update = patch.model_dump(mode="json", by_alias=True, exclude_none=True)
        else:
            try:
                validated = RuntimeSettingsPatch.model_validate(patch)
            except ValidationError as error:
                raise ConfigurationError(str(error)) from error
            update = validated.model_dump(mode="json", by_alias=True, exclude_none=True)
        payload = _deep_merge(payload, update)
    return RuntimeSettings.model_validate(payload)


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = key.lower().replace("_", "")
            if any(marker in normalized for marker in ("apikey", "password", "credential", "token", "secret")):
                if normalized.endswith("secretref") or normalized.endswith("tokensecretref"):
                    redacted[key] = item
                elif item not in (None, "", False):
                    redacted[key] = "[redacted]"
                else:
                    redacted[key] = item
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def _load_config_file(path: Path) -> dict[str, Any]:
    if path.suffix.lower() not in {".json", ".yaml", ".yml"}:
        raise ConfigurationError("配置文件必须使用 .json、.yaml 或 .yml 扩展名")
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"无法加载配置文件 {path}：{error}") from error
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigurationError("配置文件根节点必须是对象")
    return parsed


def _validated_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("baseUrl 必须是绝对 HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("baseUrl 不得包含用户信息")
    return value.rstrip("/")


def _environment_payload(environ: Mapping[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name, path in ENVIRONMENT_OVERRIDES.items():
        raw = environ.get(name)
        if raw is None or not raw.strip():
            continue
        value: Any = raw.strip()
        if name in {"AI_JSUNPACK_API_PORT", "AI_JSUNPACK_MAX_UPLOAD_BYTES"}:
            try:
                value = int(value)
            except ValueError as error:
                raise ConfigurationError(f"{name} 必须是整数") from error
        elif name == "AI_JSUNPACK_CORS_ORIGINS":
            value = [item.strip() for item in value.split(",") if item.strip()]
        _set_path(payload, path, value)
    return payload


def _set_path(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target = payload
    for part in path[:-1]:
        target = target.setdefault(part, {})
    target[path[-1]] = value


def _deep_merge(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value and value.strip() else None


def _worker_environment(config: ApplicationConfig) -> dict[str, str]:
    values = {
        "AI_JSUNPACK_SANDBOX_RUNNER": config.worker.sandbox.runner,
        "AI_JSUNPACK_AGENT_PROVIDER": config.worker.agent.cloud.provider,
        "AI_JSUNPACK_LOCAL_AGENT_PROVIDER": config.worker.agent.local.provider,
    }
    optional_values = {
        "AI_JSUNPACK_AGENT_MODEL": config.worker.agent.cloud.model,
        "AI_JSUNPACK_AGENT_BASE_URL": config.worker.agent.cloud.base_url,
        "AI_JSUNPACK_LOCAL_AGENT_MODEL": config.worker.agent.local.model,
        "AI_JSUNPACK_LOCAL_AGENT_BASE_URL": config.worker.agent.local.base_url,
        "AI_JSUNPACK_BROWSER_RUNNER_URL": config.browser_runner.base_url,
    }
    values.update({name: value for name, value in optional_values.items() if value})
    return values
