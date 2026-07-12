from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

from pydantic import BaseModel, ValidationError

from apps.api.app.models import (
    CloudMode,
    EvidenceRef,
    FailureClass,
    InferenceType,
    InferenceValidationStatus,
    RepairAction,
    RunStatus,
)
from packages.knowledge import KnowledgeHit
from packages.memory import JobMemoryContext
from packages.sandbox import is_production_profile

from .agent_context import AgentContextBuilder, AgentContextRedactor
from .agent_contracts import (
    AGENT_API_KEY_ENV,
    AGENT_BASE_URL_ENV,
    AGENT_MODEL_ENV,
    AGENT_PROMPT_VERSION,
    AGENT_PROVIDER_ENV,
    AGENT_TEMPERATURE_ENV,
    AGENT_TIMEOUT_SECONDS_ENV,
    AGENT_TOOL_VERSION,
    CREWAI_DATA_ROOT_ENV,
    CREW_AGENT_NAMES,
    CrewAgentExecution,
    CrewAgentSpec,
    CrewExecutionStatus,
    CrewInferenceOutput,
    CrewRepairInstructionOutput,
    CrewReportSectionOutput,
    CrewReviewOutput,
    CrewRuntimeDiagnosisOutput,
    CrewStructuredAgentOutput,
    LOCAL_AGENT_API_KEY_ENV,
    LOCAL_AGENT_BASE_URL_ENV,
    LOCAL_AGENT_MODEL_ENV,
    LOCAL_AGENT_PROVIDER_ENV,
    AgentInferenceDraft,
    AgentModelPolicy,
    AgentRepairInstructionDraft,
    AgentReportSectionDraft,
    AgentReviewDraft,
    AgentRuntimeDiagnosisDraft,
    AgentRuntimeRequest,
    crew_output_model_for_agent,
    validate_crew_output_for_agent,
)
from .agent_feedback import AgentFeedbackRefiner


OPENAI_COMPATIBLE_PROVIDER = "openai-compatible"
DEFAULT_AGENT_TIMEOUT_SECONDS = 30.0
DEFAULT_BACKEND_FAILURE_CACHE_SECONDS = 30.0
BACKEND_FAILURE_CACHE_SECONDS_ENV = "AI_JSUNPACK_AGENT_FAILURE_CACHE_SECONDS"


class ModelPolicyResolver:
    def resolve(self, request: AgentRuntimeRequest) -> AgentModelPolicy:
        if request.cloud_mode == "cloud_allowed":
            return self._cloud_allowed(request)
        if request.cloud_mode == "desensitized":
            return self._desensitized(request)
        return self._local_only(request)

    def _cloud_allowed(self, request: AgentRuntimeRequest) -> AgentModelPolicy:
        model_name = self._config_or_env(request.job_config, "agentModel", AGENT_MODEL_ENV)
        provider = self._config_or_env(request.job_config, "agentModelProvider", AGENT_PROVIDER_ENV) or "cloud"
        base_url = self._env_value(AGENT_BASE_URL_ENV)
        api_key = self._env_value(AGENT_API_KEY_ENV)
        return self._policy(
            cloud_mode=request.cloud_mode,
            model_provider=provider,
            model_name=model_name,
            sanitized_context=False,
            denial_reason="cloud_allowed mode requires config.agentModel or AI_JSUNPACK_AGENT_MODEL.",
            base_url=base_url,
            api_key=api_key,
            endpoint_is_cloud=True,
        )

    def _local_only(self, request: AgentRuntimeRequest) -> AgentModelPolicy:
        model_name = self._config_or_env(request.job_config, "localAgentModel", LOCAL_AGENT_MODEL_ENV)
        provider = self._config_or_env(request.job_config, "localAgentProvider", LOCAL_AGENT_PROVIDER_ENV) or "local"
        base_url = self._env_value(LOCAL_AGENT_BASE_URL_ENV)
        api_key = self._env_value(LOCAL_AGENT_API_KEY_ENV)
        return self._policy(
            cloud_mode=request.cloud_mode,
            model_provider=provider,
            model_name=model_name,
            sanitized_context=False,
            denial_reason="local_only mode requires config.localAgentModel or AI_JSUNPACK_LOCAL_AGENT_MODEL.",
            base_url=base_url,
            api_key=api_key,
            endpoint_is_cloud=False,
        )

    def _desensitized(self, request: AgentRuntimeRequest) -> AgentModelPolicy:
        cloud_model_name = self._config_or_env(request.job_config, "agentModel", AGENT_MODEL_ENV)
        local_model_name = self._config_or_env(request.job_config, "localAgentModel", LOCAL_AGENT_MODEL_ENV)
        model_name = cloud_model_name or local_model_name
        cloud_provider = self._config_or_env(request.job_config, "agentModelProvider", AGENT_PROVIDER_ENV)
        local_provider = self._config_or_env(request.job_config, "localAgentProvider", LOCAL_AGENT_PROVIDER_ENV)
        cloud_base_url = self._env_value(AGENT_BASE_URL_ENV)
        local_base_url = self._env_value(LOCAL_AGENT_BASE_URL_ENV)
        cloud_api_key = self._env_value(AGENT_API_KEY_ENV)
        local_api_key = self._env_value(LOCAL_AGENT_API_KEY_ENV)
        use_cloud_endpoint = bool(cloud_model_name)
        provider = (cloud_provider if use_cloud_endpoint else local_provider) or "desensitized"
        return self._policy(
            cloud_mode=request.cloud_mode,
            model_provider=provider,
            model_name=model_name,
            sanitized_context=True,
            denial_reason=(
                "desensitized mode requires config.agentModel, config.localAgentModel, "
                "AI_JSUNPACK_AGENT_MODEL, or AI_JSUNPACK_LOCAL_AGENT_MODEL."
            ),
            base_url=cloud_base_url if use_cloud_endpoint else local_base_url,
            api_key=cloud_api_key if use_cloud_endpoint else local_api_key,
            endpoint_is_cloud=use_cloud_endpoint,
        )

    def _policy(
        self,
        *,
        cloud_mode: CloudMode,
        model_provider: str,
        model_name: str | None,
        sanitized_context: bool,
        denial_reason: str,
        base_url: str | None,
        api_key: str | None,
        endpoint_is_cloud: bool,
    ) -> AgentModelPolicy:
        timeout_seconds = self._positive_float_or_default(
            self._env_value(AGENT_TIMEOUT_SECONDS_ENV),
            default=DEFAULT_AGENT_TIMEOUT_SECONDS,
        )
        temperature = self._optional_float(self._env_value(AGENT_TEMPERATURE_ENV))
        endpoint_error = self._endpoint_policy_error(
            base_url=base_url,
            endpoint_is_cloud=endpoint_is_cloud,
        )
        if model_name and endpoint_error is None:
            return AgentModelPolicy(
                allowed=True,
                cloud_mode=cloud_mode,
                model_provider=model_provider,
                model_name=model_name,
                prompt_version=AGENT_PROMPT_VERSION,
                sanitized_context=sanitized_context,
                base_url=base_url,
                api_key=api_key,
                timeout_seconds=timeout_seconds,
                temperature=temperature,
            )
        return AgentModelPolicy(
            allowed=False,
            cloud_mode=cloud_mode,
            model_provider=model_provider,
            model_name="unconfigured",
            prompt_version=AGENT_PROMPT_VERSION,
            sanitized_context=sanitized_context,
            denial_reason=endpoint_error or denial_reason,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
        )

    def _endpoint_policy_error(self, *, base_url: str | None, endpoint_is_cloud: bool) -> str | None:
        if not base_url:
            return None
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            return "OpenAI-compatible endpoint must use http or https."
        if not parsed.hostname:
            return "OpenAI-compatible endpoint must include a hostname."
        if parsed.username or parsed.password:
            return "OpenAI-compatible endpoint must not include credentials in the URL."
        if endpoint_is_cloud and is_production_profile() and parsed.scheme != "https":
            return "Production cloud AI endpoints must use https."
        if endpoint_is_cloud and self._hostname_is_private(parsed.hostname, parsed.port):
            return "Cloud AI endpoints must not resolve to loopback, private, link-local, or reserved addresses."
        return None

    def _hostname_is_private(self, hostname: str, port: int | None) -> bool:
        normalized = hostname.rstrip(".").lower()
        if normalized == "localhost" or normalized.endswith(".localhost"):
            return True
        addresses: set[str] = set()
        with contextlib.suppress(ValueError):
            addresses.add(str(ipaddress.ip_address(normalized)))
        if not addresses:
            with contextlib.suppress(OSError):
                addresses.update(
                    info[4][0]
                    for info in socket.getaddrinfo(normalized, port or 443, type=socket.SOCK_STREAM)
                )
        for address in addresses:
            with contextlib.suppress(ValueError):
                parsed = ipaddress.ip_address(address)
                if not parsed.is_global:
                    return True
        return False

    def _config_or_env(self, config: dict[str, Any], key: str, env_name: str) -> str | None:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        env_value = os.getenv(env_name)
        if env_value and env_value.strip():
            return env_value.strip()
        return None

    def _env_value(self, env_name: str) -> str | None:
        env_value = os.getenv(env_name)
        if env_value and env_value.strip():
            return env_value.strip()
        return None

    def _positive_float_or_default(self, value: str | None, *, default: float) -> float:
        if value is None:
            return default
        with contextlib.suppress(ValueError):
            parsed = float(value)
            if parsed > 0:
                return parsed
        return default

    def _optional_float(self, value: str | None) -> float | None:
        if value is None:
            return None
        with contextlib.suppress(ValueError):
            return float(value)
        return None


class OpenAICompatibleLLMError(RuntimeError):
    pass


class OpenAICompatibleCrewAILLM:
    """CrewAI BaseLLM adapter for OpenAI Chat Completions-compatible endpoints."""

    def __new__(
        cls,
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float,
        temperature: float | None = None,
    ):
        prepare_crewai_storage()
        from crewai.llm import BaseLLM

        class _OpenAICompatibleCrewAILLM(BaseLLM):
            llm_type: str = OPENAI_COMPATIBLE_PROVIDER

            def __init__(self) -> None:
                super().__init__(
                    model=model,
                    provider=OPENAI_COMPATIBLE_PROVIDER,
                    base_url=base_url,
                    api_key=api_key,
                    temperature=temperature,
                )
                self._endpoint = self._chat_completions_url(base_url)
                self._request_timeout_seconds = timeout_seconds

            def call(
                self,
                messages: str | list[dict[str, Any]],
                tools: list[dict[str, Any]] | None = None,
                callbacks: list[Any] | None = None,
                available_functions: dict[str, Any] | None = None,
                from_task: Any | None = None,
                from_agent: Any | None = None,
                response_model: type[BaseModel] | None = None,
            ) -> str | Any:
                del callbacks, available_functions
                formatted_messages = self._format_messages(messages)
                payload: dict[str, Any] = {
                    "model": self.model,
                    "messages": formatted_messages,
                }
                if self.temperature is not None:
                    payload["temperature"] = self.temperature
                if tools:
                    payload["tools"] = tools
                content = self._post_chat_completions(payload)
                return self._validate_structured_output(content, response_model)

            def _post_chat_completions(self, payload: dict[str, Any]) -> str:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                headers = {"Content-Type": "application/json", "Accept": "application/json"}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"
                # The endpoint is normalized and restricted to HTTP(S) before the adapter is constructed.
                chat_request = request.Request(  # noqa: S310
                    self._endpoint,
                    data=body,
                    headers=headers,
                    method="POST",
                )
                try:
                    with request.urlopen(  # nosec B310  # noqa: S310
                        chat_request,
                        timeout=self._request_timeout_seconds,
                    ) as response:
                        raw = response.read()
                except TimeoutError as exc:
                    raise OpenAICompatibleLLMError(
                        f"OpenAI-compatible endpoint timed out after {self._request_timeout_seconds:g}s."
                    ) from exc
                except error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")[:500]
                    raise OpenAICompatibleLLMError(
                        f"OpenAI-compatible endpoint returned HTTP {exc.code}: {detail}"
                    ) from exc
                except error.URLError as exc:
                    reason = getattr(exc, "reason", exc)
                    if isinstance(reason, TimeoutError):
                        raise OpenAICompatibleLLMError(
                            f"OpenAI-compatible endpoint timed out after {self._request_timeout_seconds:g}s."
                        ) from exc
                    raise OpenAICompatibleLLMError(f"OpenAI-compatible endpoint request failed: {reason}") from exc

                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise OpenAICompatibleLLMError(
                        "OpenAI-compatible endpoint returned non-JSON response."
                    ) from exc
                return self._extract_content(data)

            def _extract_content(self, data: Any) -> str:
                if not isinstance(data, dict):
                    raise OpenAICompatibleLLMError("OpenAI-compatible endpoint response must be a JSON object.")
                choices = data.get("choices")
                if not isinstance(choices, list) or not choices:
                    raise OpenAICompatibleLLMError(
                        "OpenAI-compatible endpoint response is missing choices[0].message.content."
                    )
                first_choice = choices[0]
                if not isinstance(first_choice, dict):
                    raise OpenAICompatibleLLMError(
                        "OpenAI-compatible endpoint response has invalid choices[0]."
                    )
                message = first_choice.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                    raise OpenAICompatibleLLMError(
                        "OpenAI-compatible endpoint response is missing choices[0].message.content."
                    )
                content = message["content"]
                return self._apply_stop_words(content)

            def _format_messages(self, messages: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
                formatted = super()._format_messages(messages)
                return [self._message_payload(message) for message in formatted]

            def _message_payload(self, message: dict[str, Any]) -> dict[str, Any]:
                return {
                    key: value
                    for key, value in message.items()
                    if key in {"role", "content", "name", "tool_call_id", "tool_calls"}
                }

            @staticmethod
            def _chat_completions_url(raw_base_url: str) -> str:
                normalized = raw_base_url.strip().rstrip("/")
                if not normalized:
                    raise ValueError("OpenAI-compatible base URL cannot be empty.")
                parsed = urlparse(normalized)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    raise ValueError("OpenAI-compatible base URL must use http or https and include a hostname.")
                if parsed.username or parsed.password:
                    raise ValueError("OpenAI-compatible base URL must not include credentials.")
                if normalized.endswith("/chat/completions"):
                    return normalized
                if normalized.endswith("/v1"):
                    return f"{normalized}/chat/completions"
                return f"{normalized}/v1/chat/completions"

        return _OpenAICompatibleCrewAILLM()


def prepare_crewai_storage() -> None:
    data_root = Path(os.getenv(CREWAI_DATA_ROOT_ENV, Path.cwd() / ".crewai-data")).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    os.environ["LOCALAPPDATA"] = str(data_root)
    os.environ["APPDATA"] = str(data_root)
    os.environ["XDG_DATA_HOME"] = str(data_root)
    os.environ["CREWAI_STORAGE_DIR"] = "ai-jsunpack"
    try:
        import appdirs

        def project_user_data_dir(
            appname: str | None = None,
            appauthor: str | None = None,
            version: str | None = None,
            roaming: bool = False,
        ) -> str:
            parts = [part for part in (appauthor, appname, version) if part]
            target = data_root.joinpath(*parts) if parts else data_root
            target.mkdir(parents=True, exist_ok=True)
            return str(target)

        appdirs.user_data_dir = project_user_data_dir
    except Exception:
        # CrewAI can still use the environment-backed storage path when appdirs is unavailable.
        return


class CrewAIBackend:
    """Encapsulates raw CrewAI calls for a single agent execution."""

    def __init__(self) -> None:
        self._prepared = False

    def run_agent(
        self,
        *,
        spec: CrewAgentSpec,
        prompt_context: dict[str, Any],
        policy: AgentModelPolicy,
    ) -> CrewStructuredAgentOutput:
        self._prepare_crewai_storage()
        from crewai import Agent, Crew, Process, Task

        output_model = crew_output_model_for_agent(spec.name)
        agent = Agent(
            role=spec.role,
            goal=spec.goal,
            backstory=spec.backstory,
            llm=self._llm_for_policy(policy),
            allow_delegation=False,
            verbose=False,
        )
        context_json = json.dumps(prompt_context, ensure_ascii=False, indent=2, sort_keys=True)
        task = Task(
            description=(
                "Read the structured, audited runtime context and return only schema-valid JSON for your role. "
                "Preserve uncertainty. Do not request source text outside provided evidence references.\n\n{agent_context}"
            ),
            expected_output=f"Structured {spec.output_kind} records for {spec.name}.",
            agent=agent,
            output_pydantic=output_model,
        )
        crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
        result = crew.kickoff(inputs={"agent_context": context_json})
        return self._parse_crewai_result(result, agent_name=spec.name)

    def _prepare_crewai_storage(self) -> None:
        if self._prepared:
            return
        prepare_crewai_storage()
        self._prepared = True

    def _parse_crewai_result(self, result: Any, *, agent_name: str) -> CrewStructuredAgentOutput:
        structured = getattr(result, "pydantic", None)
        if isinstance(structured, BaseModel):
            return validate_crew_output_for_agent(agent_name, structured)
        if isinstance(result, BaseModel | dict):
            return validate_crew_output_for_agent(agent_name, result)
        raw = getattr(result, "raw", None) or str(result)
        return validate_crew_output_for_agent(agent_name, json.loads(raw))

    def _llm_for_policy(self, policy: AgentModelPolicy) -> str | Any:
        if not policy.custom_endpoint_enabled:
            return policy.model_name
        if not policy.base_url:
            return policy.model_name
        return OpenAICompatibleCrewAILLM(
            model=policy.model_name,
            base_url=policy.base_url,
            api_key=policy.api_key,
            timeout_seconds=policy.timeout_seconds,
            temperature=policy.temperature,
        )


class CrewAIExecutionAdapter:
    """Converts policy-checked prompt context into isolated CrewAI agent executions."""

    tool_name = "crewai.agent_pass"
    tool_version = AGENT_TOOL_VERSION

    def __init__(
        self,
        *,
        policy_resolver: ModelPolicyResolver | None = None,
        redactor: AgentContextRedactor | None = None,
        feedback_refiner: AgentFeedbackRefiner | None = None,
        context_builder: AgentContextBuilder | None = None,
        backend: Any | None = None,
    ) -> None:
        self.policy_resolver = policy_resolver or ModelPolicyResolver()
        self.redactor = redactor or AgentContextRedactor()
        self.feedback_refiner = feedback_refiner or AgentFeedbackRefiner()
        self.context_builder = context_builder or AgentContextBuilder()
        if backend is None:
            from .agent_process import IsolatedCrewAIBackend

            backend = IsolatedCrewAIBackend()
        self.backend = backend
        self._cached_backend_failures: dict[tuple[str, str, str], tuple[float, str]] = {}
        self._failure_cache_lock = threading.Lock()
        self._failure_cache_seconds = self._positive_float_env(
            BACKEND_FAILURE_CACHE_SECONDS_ENV,
            DEFAULT_BACKEND_FAILURE_CACHE_SECONDS,
        )

    @property
    def parallel_safe(self) -> bool:
        return bool(getattr(self.backend, "parallel_safe", False))

    @property
    def isolation_mode(self) -> str:
        return str(getattr(self.backend, "isolation_mode", "in_process"))

    def prepare_context(
        self,
        *,
        request: AgentRuntimeRequest,
        memory_context: JobMemoryContext,
        memory_artifact_ids: list[str],
        knowledge_hits: list[KnowledgeHit],
        knowledge_artifact_id: str,
        evidence_refs: list[EvidenceRef],
    ) -> tuple[AgentModelPolicy, dict[str, Any], dict[str, Any], Any, Any]:
        policy = self.policy_resolver.resolve(request)
        redaction = self.redactor.redact(
            policy=policy,
            input_summary=self.context_builder.input_summary(request),
            memory_excerpt=memory_context.prompt_excerpt,
            evidence_refs=evidence_refs,
        )
        plan_payload = self._plan_payload(
            request=request,
            policy=policy,
            memory_artifact_ids=memory_artifact_ids,
            knowledge_artifact_id=knowledge_artifact_id,
            knowledge_hits=knowledge_hits,
            input_summary=redaction.input_summary,
            evidence_refs=redaction.evidence_refs,
            redaction_metadata=redaction.metadata,
        )
        feedback = self.feedback_refiner.refine(knowledge_hits=knowledge_hits)
        prompt_context = self._prompt_context(
            request=request,
            policy=policy,
            memory_excerpt=redaction.memory_excerpt,
            input_summary=redaction.input_summary,
            knowledge_hits=knowledge_hits,
            evidence_refs=redaction.evidence_refs,
            redaction_metadata=redaction.metadata,
        )
        return policy, prompt_context, plan_payload, feedback, redaction

    def execute_agent(
        self,
        *,
        spec: CrewAgentSpec,
        policy: AgentModelPolicy,
        prompt_context: dict[str, Any],
        input_artifact_ids: list[str],
        evidence_refs: list[EvidenceRef],
    ) -> CrewAgentExecution:
        if not policy.allowed:
            return replace(
                self._policy_denied_execution(
                    spec=spec,
                    policy=policy,
                    input_artifact_ids=input_artifact_ids,
                    evidence_refs=evidence_refs,
                ),
                isolation_mode=self.isolation_mode,
            )
        cache_key = self._failure_cache_key(policy)
        cached_failure = self._cached_failure(cache_key)
        if cached_failure:
            return replace(
                self._failed_execution(
                    spec=spec,
                    policy=policy,
                    input_artifact_ids=input_artifact_ids,
                    evidence_refs=evidence_refs,
                    error=RuntimeError(cached_failure),
                ),
                isolation_mode=self.isolation_mode,
            )

        try:
            output = self.backend.run_agent(spec=spec, prompt_context=prompt_context, policy=policy)
            output = validate_crew_output_for_agent(spec.name, output)
        except Exception as error:
            if self._should_cache_backend_failure(error):
                self._cache_failure(cache_key, str(error))
            metadata = self._process_metadata(error)
            return replace(
                self._failed_execution(
                    spec=spec,
                    policy=policy,
                    input_artifact_ids=input_artifact_ids,
                    evidence_refs=evidence_refs,
                    error=error,
                ),
                **metadata,
            )
        with self._failure_cache_lock:
            self._cached_backend_failures.pop(cache_key, None)
        metadata = self._process_metadata()
        return replace(
            self._execution_from_output(
                spec=spec,
                policy=policy,
                input_artifact_ids=input_artifact_ids,
                evidence_refs=evidence_refs,
                output=output,
            ),
            **metadata,
        )

    def _process_metadata(self, error: Exception | None = None) -> dict[str, Any]:
        result = getattr(error, "result", None)
        consume = getattr(self.backend, "consume_last_result", None)
        consumed = consume() if callable(consume) else None
        if result is None:
            result = consumed
        return {
            "isolation_mode": self.isolation_mode,
            "process_exit_status": getattr(result, "process_exit_status", None),
            "process_data_root_configured": bool(getattr(result, "data_root", None)),
        }

    def _should_cache_backend_failure(self, error: Exception) -> bool:
        if isinstance(error, ValidationError):
            return False
        result = getattr(error, "result", None)
        if getattr(result, "status", None) == "invalid_response":
            return False
        return True

    def _failure_cache_key(self, policy: AgentModelPolicy) -> tuple[str, str, str]:
        return (
            policy.model_provider.strip().lower(),
            policy.model_name.strip(),
            (policy.base_url or "").strip().rstrip("/").lower(),
        )

    def _cached_failure(self, key: tuple[str, str, str]) -> str | None:
        now = time.monotonic()
        with self._failure_cache_lock:
            cached = self._cached_backend_failures.get(key)
            if cached is None:
                return None
            expires_at, message = cached
            if expires_at <= now:
                self._cached_backend_failures.pop(key, None)
                return None
            return message

    def _cache_failure(self, key: tuple[str, str, str], message: str) -> None:
        with self._failure_cache_lock:
            self._cached_backend_failures[key] = (time.monotonic() + self._failure_cache_seconds, message)

    def _positive_float_env(self, name: str, default: float) -> float:
        with contextlib.suppress(ValueError):
            value = float(os.getenv(name, str(default)))
            if value > 0:
                return value
        return default

    def _execution_from_output(
        self,
        *,
        spec: CrewAgentSpec,
        policy: AgentModelPolicy,
        input_artifact_ids: list[str],
        evidence_refs: list[EvidenceRef],
        output: CrewStructuredAgentOutput,
    ) -> CrewAgentExecution:
        review = self._review_from_output(output.review)
        inferences = [self._draft_from_crewai(inference) for inference in output.inferences]
        runtime_diagnoses = [self._runtime_diagnosis_from_output(item) for item in output.runtime_diagnoses]
        report_sections = [self._report_section_from_output(item) for item in output.report_sections]
        repair_instructions = [self._repair_instruction_from_output(item) for item in output.repair_instructions]
        status = self._execution_status(review.status if review is not None else "pass")
        failure_class = review.failure_class if review is not None else "none"
        message = review.decision if review is not None else f"{spec.name} completed with no explicit review decision."
        return CrewAgentExecution(
            spec=spec,
            status=status,
            failure_class=failure_class,
            attempt=0,
            duration_ms=0.0,
            input_artifact_ids=input_artifact_ids,
            evidence_refs=evidence_refs,
            message=message,
            raw_output=output.model_dump(by_alias=True, exclude_none=True),
            inferences=inferences,
            runtime_diagnoses=runtime_diagnoses,
            report_sections=report_sections,
            repair_instructions=repair_instructions,
            review=review,
            model_provider=policy.model_provider,
            model_name=policy.model_name,
            model_base_url_configured=policy.base_url_configured,
            model_api_key_configured=policy.api_key_configured,
            model_custom_endpoint_enabled=policy.custom_endpoint_enabled,
            model_timeout_seconds=policy.timeout_seconds,
            model_temperature=policy.temperature,
            role_schema_validated=True,
        )

    def _policy_denied_execution(
        self,
        *,
        spec: CrewAgentSpec,
        policy: AgentModelPolicy,
        input_artifact_ids: list[str],
        evidence_refs: list[EvidenceRef],
    ) -> CrewAgentExecution:
        reason = policy.denial_reason or "Agent model policy denied execution."
        review = AgentReviewDraft(
            status="best_effort",
            decision=f"{spec.name} skipped because model policy denied execution: {reason}",
            failure_class="policy_denied",
        ) if spec.name == "ReviewAgent" else None
        return CrewAgentExecution(
            spec=spec,
            status="best_effort",
            failure_class="policy_denied",
            attempt=0,
            duration_ms=0.0,
            input_artifact_ids=input_artifact_ids,
            evidence_refs=evidence_refs,
            message=f"{spec.name} skipped because model policy denied execution: {reason}",
            raw_output={"limitations": [reason], "policyDenied": True},
            review=review,
            model_provider=policy.model_provider,
            model_name=policy.model_name,
            model_base_url_configured=policy.base_url_configured,
            model_api_key_configured=policy.api_key_configured,
            model_custom_endpoint_enabled=policy.custom_endpoint_enabled,
            model_timeout_seconds=policy.timeout_seconds,
            model_temperature=policy.temperature,
        )

    def _failed_execution(
        self,
        *,
        spec: CrewAgentSpec,
        policy: AgentModelPolicy,
        input_artifact_ids: list[str],
        evidence_refs: list[EvidenceRef],
        error: Exception,
    ) -> CrewAgentExecution:
        detail = f"CrewAI runtime failed: {error}"
        review = AgentReviewDraft(
            status="fail",
            decision=detail,
            failure_class="agent_failed",
        ) if spec.name == "ReviewAgent" else None
        return CrewAgentExecution(
            spec=spec,
            status="fail",
            failure_class="agent_failed",
            attempt=0,
            duration_ms=0.0,
            input_artifact_ids=input_artifact_ids,
            evidence_refs=evidence_refs,
            message=detail,
            raw_output={"limitations": [detail], "agentFailed": True},
            review=review,
            model_provider=policy.model_provider,
            model_name=policy.model_name,
            model_base_url_configured=policy.base_url_configured,
            model_api_key_configured=policy.api_key_configured,
            model_custom_endpoint_enabled=policy.custom_endpoint_enabled,
            model_timeout_seconds=policy.timeout_seconds,
            model_temperature=policy.temperature,
        )

    def _execution_status(self, value: str) -> CrewExecutionStatus:
        allowed = {"pass", "retry", "best_effort", "fail", "skipped"}
        return value if value in allowed else "best_effort"  # type: ignore[return-value]

    def _review_from_output(self, review: CrewReviewOutput | None) -> AgentReviewDraft | None:
        if review is None:
            return None
        return AgentReviewDraft(
            status=self._run_status(review.status, default="best_effort"),
            decision=review.decision,
            failure_class=self._failure_class(review.failure_class),
            repair_instruction_ids=list(review.repair_instruction_ids),
        )

    def _runtime_diagnosis_from_output(self, output: CrewRuntimeDiagnosisOutput) -> AgentRuntimeDiagnosisDraft:
        return AgentRuntimeDiagnosisDraft(
            target_stage=output.target_stage,
            status=self._run_status(output.status, default="best_effort"),
            failure_class=self._failure_class(output.failure_class),
            diagnosis=output.diagnosis,
            recommended_actions=list(output.recommended_actions),
            confidence=max(0, min(1, output.confidence)),
            uncertainty_reasons=list(output.uncertainty_reasons) or ["CrewAI output did not include uncertainty details."],
            agent_name=output.agent_name,
        )

    def _report_section_from_output(self, output: CrewReportSectionOutput) -> AgentReportSectionDraft:
        return AgentReportSectionDraft(
            title=output.title,
            anchor=output.anchor,
            summary=output.summary,
            content=output.content,
            status=self._run_status(output.status, default="best_effort"),
            confidence=max(0, min(1, output.confidence)),
            uncertainty_reasons=list(output.uncertainty_reasons) or ["CrewAI output did not include uncertainty details."],
            details=[(detail.label, detail.value) for detail in output.details],
            agent_name=output.agent_name,
        )

    def _repair_instruction_from_output(self, output: CrewRepairInstructionOutput) -> AgentRepairInstructionDraft:
        actions = [
            RepairAction(action=item.action, path=item.path, value=item.value, reason=item.reason)
            for item in output.actions
        ]
        return AgentRepairInstructionDraft(
            target_stage=output.target_stage,
            failure_class=self._failure_class(output.failure_class),
            decision=output.decision,
            status=output.status,
            risk_level=output.risk_level,
            actions=actions,
        )

    def _draft_from_crewai(self, inference: CrewInferenceOutput) -> AgentInferenceDraft:
        return AgentInferenceDraft(
            type=self._inference_type(inference.type),
            agent_name=inference.agent_name,
            confidence=max(0, min(1, inference.confidence)),
            uncertainty_reasons=inference.uncertainty_reasons or ["CrewAI output did not include uncertainty details."],
            alternatives=inference.alternatives or ["keep deterministic Core evidence unchanged"],
            validation_status=self._validation_status(inference.validation_status),
        )

    def _plan_payload(
        self,
        *,
        request: AgentRuntimeRequest,
        policy: AgentModelPolicy,
        memory_artifact_ids: list[str],
        knowledge_artifact_id: str,
        knowledge_hits: list[KnowledgeHit],
        input_summary: dict[str, Any],
        evidence_refs: list[EvidenceRef],
        redaction_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "kind": "agent_plan",
            "jobId": request.job_id,
            "provider": "crewai",
            "modelProvider": policy.model_provider,
            "modelName": policy.model_name,
            "promptVersion": policy.prompt_version,
            "cloudMode": policy.cloud_mode,
            "inputArtifactIds": request.input_artifact_ids,
            "memoryRecordArtifactId": memory_artifact_ids[0] if memory_artifact_ids else None,
            "memoryRecordArtifactIds": memory_artifact_ids,
            "knowledgeEvidenceArtifactId": knowledge_artifact_id,
            "plannedAgents": list(CREW_AGENT_NAMES),
            "inputSummary": input_summary,
            "knowledgeHitIds": [hit.id for hit in knowledge_hits],
            "evidenceRefs": [ref.model_dump(by_alias=True, exclude_none=True) for ref in evidence_refs],
            "modelPolicy": {
                "allowed": policy.allowed,
                "sanitizedContext": policy.sanitized_context,
                "denialReason": policy.denial_reason,
                "provider": policy.model_provider,
                "model": policy.model_name,
                "baseUrlConfigured": policy.base_url_configured,
                "apiKeyConfigured": policy.api_key_configured,
                "customEndpointEnabled": policy.custom_endpoint_enabled,
                "timeoutSeconds": policy.timeout_seconds,
                "temperature": policy.temperature,
                "redaction": redaction_metadata,
            },
            "runtimeStatus": "planned",
            "limitations": [],
        }

    def _prompt_context(
        self,
        *,
        request: AgentRuntimeRequest,
        policy: AgentModelPolicy,
        memory_excerpt: str,
        input_summary: dict[str, Any],
        knowledge_hits: list[KnowledgeHit],
        evidence_refs: list[EvidenceRef],
        redaction_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "jobId": request.job_id,
            "projectId": request.project_id,
            "cloudMode": policy.cloud_mode,
            "sanitizedContext": policy.sanitized_context,
            "redactionPolicy": redaction_metadata,
            "inputSummary": input_summary,
            "memory": memory_excerpt,
            "knowledgeHits": [
                {
                    "id": hit.id,
                    "category": hit.category,
                    "label": hit.label,
                    "locator": hit.locator,
                    "excerpt": hit.excerpt,
                    "confidence": hit.confidence,
                }
                for hit in knowledge_hits
            ],
            "evidenceRefs": [ref.model_dump(by_alias=True, exclude_none=True) for ref in evidence_refs],
        }

    def _inference_type(self, value: str) -> InferenceType:
        allowed = {"naming", "module_split", "type_inference", "framework", "dead_code", "runtime", "repair"}
        return value if value in allowed else "module_split"  # type: ignore[return-value]

    def _validation_status(self, value: str) -> InferenceValidationStatus:
        allowed = {"unverified", "accepted", "rejected", "needs_review"}
        return value if value in allowed else "needs_review"  # type: ignore[return-value]

    def _run_status(self, value: str, *, default: RunStatus) -> RunStatus:
        allowed = {"pass", "retry", "best_effort", "fail"}
        return value if value in allowed else default  # type: ignore[return-value]

    def _failure_class(self, value: str | FailureClass) -> FailureClass:
        allowed = {
            "none",
            "invalid_input",
            "parse_error",
            "agent_failed",
            "dependency_missing",
            "install_failed",
            "type_error",
            "build_error",
            "runtime_error",
            "sandbox_denied",
            "policy_denied",
            "timeout",
            "resource_limit",
            "unknown",
        }
        normalized = str(value)
        return normalized if normalized in allowed else "unknown"  # type: ignore[return-value]


__all__ = [
    "CrewAIBackend",
    "CrewAIExecutionAdapter",
    "ModelPolicyResolver",
    "OpenAICompatibleCrewAILLM",
    "OpenAICompatibleLLMError",
]
