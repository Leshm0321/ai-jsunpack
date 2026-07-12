import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.worker.worker.agent_contracts import AgentModelPolicy, CrewAgentSpec, CrewStructuredAgentOutput
from apps.worker.worker.agent_process import (
    AGENT_PROCESS_PROTOCOL_VERSION,
    BoundedCrewAIProcessPool,
    CrewAIProcessError,
    CrewAIProcessRequest,
    CrewAIProcessResult,
    IsolatedCrewAIBackend,
    SubprocessCrewAIInvoker,
)
from apps.worker.worker.agent_process_worker import main as worker_main
from apps.worker.worker.agent_providers import CrewAIExecutionAdapter


def agent_spec(name: str = "NamingAgent") -> CrewAgentSpec:
    return CrewAgentSpec(
        name=name,
        stage="specialists",
        responsibility="Recover names.",
        role="Naming specialist",
        goal="Return evidence-backed names.",
        backstory="A deterministic assistant.",
        output_kind="inference",
        allow_parallel=True,
        dependencies=["AnalysisAgent"],
    )


def model_policy(*, api_key: str = "secret-api-key", base_url: str = "https://model.example.test/v1"):
    return AgentModelPolicy(
        allowed=True,
        cloud_mode="cloud_allowed",
        model_provider="openai-compatible",
        model_name="test-model",
        prompt_version="test-v1",
        sanitized_context=False,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=2.0,
    )


def invocation_request(name: str = "NamingAgent") -> CrewAIProcessRequest:
    return CrewAIProcessRequest(
        job_id="job/unsafe path",
        spec=agent_spec(name),
        prompt_context={"jobId": "job/unsafe path", "evidenceRefs": []},
        policy=model_policy(),
    )


class FakeBackend:
    def run_agent(self, *, spec, prompt_context, policy):
        del prompt_context, policy
        return CrewStructuredAgentOutput(
            inferences=[
                {
                    "type": "naming",
                    "agentName": spec.name,
                    "confidence": 0.8,
                    "uncertaintyReasons": ["fixture"],
                    "alternatives": ["keep current"],
                }
            ]
        )


class AgentProcessWorkerTest(unittest.TestCase):
    def test_worker_module_is_a_json_stdin_stdout_process(self):
        completed = subprocess.run(
            [sys.executable, "-m", "apps.worker.worker.agent_process_worker"],
            input="{}",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)["errorKind"], "protocol_error")
        self.assertEqual(completed.stderr, "")

    def test_worker_runs_backend_with_json_only_protocol_and_isolated_root(self):
        request = invocation_request()
        payload = {
            "protocolVersion": AGENT_PROCESS_PROTOCOL_VERSION,
            "invocationId": "invocation-1",
            "jobId": request.job_id,
            "dataRoot": "D:/tmp/isolated/job/agent/invocation-1",
            "spec": {
                "name": request.spec.name,
                "stage": request.spec.stage,
                "responsibility": request.spec.responsibility,
                "role": request.spec.role,
                "goal": request.spec.goal,
                "backstory": request.spec.backstory,
                "output_kind": request.spec.output_kind,
                "allow_parallel": request.spec.allow_parallel,
                "dependencies": request.spec.dependencies,
            },
            "promptContext": request.prompt_context,
            "policy": {
                "allowed": request.policy.allowed,
                "cloud_mode": request.policy.cloud_mode,
                "model_provider": request.policy.model_provider,
                "model_name": request.policy.model_name,
                "prompt_version": request.policy.prompt_version,
                "sanitized_context": request.policy.sanitized_context,
                "denial_reason": request.policy.denial_reason,
                "base_url": request.policy.base_url,
                "api_key": request.policy.api_key,
                "timeout_seconds": request.policy.timeout_seconds,
                "temperature": request.policy.temperature,
            },
        }
        output = io.StringIO()

        captured_environment = {}

        class InspectingBackend(FakeBackend):
            def run_agent(self, *, spec, prompt_context, policy):
                captured_environment.update(
                    {
                        name: os.environ.get(name)
                        for name in (
                            "HOME",
                            "USERPROFILE",
                            "LOCALAPPDATA",
                            "APPDATA",
                            "XDG_DATA_HOME",
                            "CREWAI_STORAGE_DIR",
                        )
                    }
                )
                return super().run_agent(spec=spec, prompt_context=prompt_context, policy=policy)

        exit_status = worker_main(
            input_stream=io.StringIO(json.dumps(payload)),
            output_stream=output,
            backend_factory=InspectingBackend,
        )

        self.assertEqual(exit_status, 0)
        response = json.loads(output.getvalue())
        self.assertEqual(response["status"], "success")
        self.assertEqual(response["output"]["inferences"][0]["agentName"], "NamingAgent")
        self.assertNotIn(request.policy.api_key, output.getvalue())
        self.assertNotIn(request.policy.base_url, output.getvalue())
        for name in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "XDG_DATA_HOME"):
            self.assertEqual(Path(captured_environment[name]).resolve(), Path(payload["dataRoot"]).resolve())
        self.assertEqual(captured_environment["CREWAI_STORAGE_DIR"], "ai-jsunpack")

    def test_worker_hides_backend_exception_details_and_secrets(self):
        class FailingBackend:
            def run_agent(self, *, spec, prompt_context, policy):
                del spec, prompt_context
                raise RuntimeError(f"endpoint={policy.base_url} key={policy.api_key}")

        request = invocation_request()
        invoker = SubprocessCrewAIInvoker()
        payload = invoker._request_payload(  # noqa: SLF001 - protocol unit test
            request,
            invocation_id="invocation-1",
            data_root=Path("D:/tmp/isolated"),
        )
        response = worker_main(
            input_stream=io.StringIO(json.dumps(payload)),
            output_stream=(output := io.StringIO()),
            backend_factory=FailingBackend,
        )

        self.assertEqual(response, 0)
        self.assertEqual(json.loads(output.getvalue())["errorKind"], "backend_error")
        self.assertNotIn(request.policy.api_key, output.getvalue())
        self.assertNotIn(request.policy.base_url, output.getvalue())

    def test_worker_classifies_output_parse_failures_as_schema_errors(self):
        class InvalidOutputBackend:
            def run_agent(self, *, spec, prompt_context, policy):
                del spec, prompt_context, policy
                raise json.JSONDecodeError("invalid model JSON", "{", 0)

        request = invocation_request()
        payload = SubprocessCrewAIInvoker()._request_payload(  # noqa: SLF001 - protocol unit test
            request,
            invocation_id="invocation-1",
            data_root=Path("D:/tmp/isolated"),
        )
        output = io.StringIO()

        worker_main(
            input_stream=io.StringIO(json.dumps(payload)),
            output_stream=output,
            backend_factory=InvalidOutputBackend,
        )

        self.assertEqual(json.loads(output.getvalue())["errorKind"], "schema_error")


class SubprocessCrewAIInvokerTest(unittest.TestCase):
    def test_success_redacts_api_key_and_base_url_from_model_output(self):
        request = invocation_request()
        raw_output = {
            "inferences": [
                {
                    "type": "naming",
                    "agentName": "NamingAgent",
                    "confidence": 0.7,
                    "uncertaintyReasons": [request.policy.api_key],
                    "alternatives": [f"endpoint {request.policy.base_url}"],
                }
            ]
        }
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "protocolVersion": AGENT_PROCESS_PROTOCOL_VERSION,
                    "status": "success",
                    "output": raw_output,
                }
            ),
            stderr=f"ignored {request.policy.api_key} {request.policy.base_url}",
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "apps.worker.worker.agent_process.subprocess.run", return_value=completed
        ):
            result = SubprocessCrewAIInvoker(data_root_base=temp_dir).invoke(request)

        self.assertTrue(result.succeeded)
        serialized = json.dumps(result.output.model_dump(by_alias=True))
        self.assertNotIn(request.policy.api_key, serialized)
        self.assertNotIn(request.policy.base_url, serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertNotIn(request.policy.api_key, result.message)
        self.assertNotIn(request.policy.base_url, result.message)

    def test_each_invocation_uses_unique_safe_data_root_and_stdin_payload(self):
        captured_payloads = []

        def fake_run(command, **kwargs):
            payload = json.loads(kwargs["input"])
            captured_payloads.append((command, payload))
            data_root = Path(payload["dataRoot"])
            data_root.mkdir(parents=True, exist_ok=True)
            (data_root / "temporary-crewai-state").write_text("state", encoding="utf-8")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "protocolVersion": AGENT_PROCESS_PROTOCOL_VERSION,
                        "status": "success",
                        "output": {},
                    }
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "apps.worker.worker.agent_process.subprocess.run", side_effect=fake_run
        ):
            invoker = SubprocessCrewAIInvoker(data_root_base=temp_dir)
            first = invoker.invoke(invocation_request())
            second = invoker.invoke(invocation_request())

        self.assertNotEqual(first.data_root, second.data_root)
        self.assertNotIn("/unsafe path", first.data_root.replace("\\", "/"))
        self.assertEqual(captured_payloads[0][0][1:3], ["-m", "apps.worker.worker.agent_process_worker"])
        self.assertEqual(captured_payloads[0][1]["dataRoot"], first.data_root)
        self.assertEqual(captured_payloads[0][1]["policy"]["api_key"], "secret-api-key")
        self.assertFalse(Path(first.data_root).exists())
        self.assertFalse(Path(second.data_root).exists())

    def test_timeout_and_nonzero_exit_are_classified_without_captured_output(self):
        request = invocation_request()
        with patch(
            "apps.worker.worker.agent_process.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="worker", timeout=0.1, output="secret-api-key"),
        ):
            timeout_result = SubprocessCrewAIInvoker().invoke(
                CrewAIProcessRequest(
                    job_id=request.job_id,
                    spec=request.spec,
                    prompt_context=request.prompt_context,
                    policy=request.policy,
                    process_timeout_seconds=0.1,
                )
            )
        with patch(
            "apps.worker.worker.agent_process.subprocess.run",
            return_value=SimpleNamespace(returncode=17, stdout="secret-api-key", stderr=request.policy.base_url),
        ):
            exit_result = SubprocessCrewAIInvoker().invoke(request)

        self.assertEqual(timeout_result.status, "timeout")
        self.assertEqual(exit_result.status, "exit_error")
        self.assertEqual(exit_result.process_exit_status, 17)
        serialized = json.dumps({"timeout": timeout_result.message, "exit": exit_result.message})
        self.assertNotIn(request.policy.api_key, serialized)
        self.assertNotIn(request.policy.base_url, serialized)

    def test_backend_is_drop_in_and_surfaces_classified_failure(self):
        class FakeInvoker:
            parallel_safe = True
            isolation_mode = "process"

            def invoke(self, request):
                return CrewAIProcessResult(
                    status="child_error",
                    message="CrewAI child invocation failed.",
                    duration_ms=1.0,
                    data_root="isolated",
                    invocation_id="id",
                )

        backend = IsolatedCrewAIBackend(invoker=FakeInvoker())
        self.assertTrue(backend.parallel_safe)
        self.assertEqual(backend.isolation_mode, "process")
        with self.assertRaises(CrewAIProcessError) as raised:
            backend.run_agent(spec=agent_spec(), prompt_context={"jobId": "job"}, policy=model_policy())
        self.assertEqual(raised.exception.result.status, "child_error")

    def test_schema_invalid_child_response_is_not_cached_as_endpoint_failure(self):
        class SchemaFailingInvoker:
            parallel_safe = True
            isolation_mode = "process"

            def __init__(self):
                self.calls = 0

            def invoke(self, request):
                self.calls += 1
                return CrewAIProcessResult(
                    status="invalid_response",
                    message="role schema invalid",
                    duration_ms=1.0,
                    data_root="isolated",
                    invocation_id=str(self.calls),
                    process_exit_status=0,
                )

        invoker = SchemaFailingInvoker()
        adapter = CrewAIExecutionAdapter(backend=IsolatedCrewAIBackend(invoker=invoker))
        kwargs = {
            "spec": agent_spec(),
            "policy": model_policy(),
            "prompt_context": {"jobId": "job"},
            "input_artifact_ids": [],
            "evidence_refs": [],
        }

        self.assertEqual(adapter.execute_agent(**kwargs).status, "fail")
        self.assertEqual(adapter.execute_agent(**kwargs).status, "fail")
        self.assertEqual(invoker.calls, 2)

    def test_execution_adapter_records_process_isolation_metadata(self):
        class SuccessfulInvoker:
            parallel_safe = True
            isolation_mode = "process"

            def invoke(self, request):
                return CrewAIProcessResult(
                    status="success",
                    message="done",
                    duration_ms=1.0,
                    data_root="isolated/job/naming/id",
                    invocation_id="id",
                    output=FakeBackend().run_agent(
                        spec=request.spec,
                        prompt_context=request.prompt_context,
                        policy=request.policy,
                    ),
                    process_exit_status=0,
                )

        execution = CrewAIExecutionAdapter(
            backend=IsolatedCrewAIBackend(invoker=SuccessfulInvoker())
        ).execute_agent(
            spec=agent_spec(),
            policy=model_policy(),
            prompt_context={"jobId": "job"},
            input_artifact_ids=[],
            evidence_refs=[],
        )

        self.assertEqual(execution.status, "pass")
        self.assertEqual(execution.isolation_mode, "process")
        self.assertEqual(execution.process_exit_status, 0)
        self.assertTrue(execution.process_data_root_configured)
        self.assertTrue(execution.role_schema_validated)


class BoundedCrewAIProcessPoolTest(unittest.TestCase):
    def test_parallel_invocations_respect_bound_and_preserve_order(self):
        class TrackingInvoker:
            parallel_safe = True
            isolation_mode = "process"

            def __init__(self):
                self.active = 0
                self.peak = 0
                self.lock = threading.Lock()

            def invoke(self, request):
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                time.sleep(0.03)
                with self.lock:
                    self.active -= 1
                return CrewAIProcessResult(
                    status="success",
                    message=request.spec.name,
                    duration_ms=30,
                    data_root=request.spec.name,
                    invocation_id=request.spec.name,
                    output=CrewStructuredAgentOutput(),
                    process_exit_status=0,
                )

        tracker = TrackingInvoker()
        pool = BoundedCrewAIProcessPool(max_parallel=2, invoker=tracker)
        requests = [invocation_request(f"Agent{index}") for index in range(5)]

        results = pool.invoke_all(requests)

        self.assertEqual(tracker.peak, 2)
        self.assertEqual([result.message for result in results], [f"Agent{index}" for index in range(5)])
        self.assertTrue(pool.parallel_safe)
        self.assertEqual(pool.isolation_mode, "process")

    def test_parallel_bound_must_be_between_one_and_ten(self):
        for value in (0, 11):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BoundedCrewAIProcessPool(max_parallel=value)

    def test_real_crewai_children_run_concurrently_against_local_compatible_endpoint(self):
        content = json.dumps(
            {
                "limitations": [],
                "notes": [],
                "inferences": [
                    {
                        "type": "naming",
                        "agentName": "ForgedAgent",
                        "confidence": 0.8,
                        "uncertaintyReasons": ["fixture"],
                        "alternatives": ["keep current"],
                        "validationStatus": "needs_review",
                        "target": "symbol:a",
                        "value": "bootstrap",
                    }
                ],
            }
        )
        server = _ConcurrentModelServer(content)
        try:
            policy = AgentModelPolicy(
                allowed=True,
                cloud_mode="local_only",
                model_provider="openai-compatible",
                model_name="test-model",
                prompt_version="test-v1",
                sanitized_context=False,
                base_url=server.base_url,
                api_key=None,
                timeout_seconds=20,
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                invoker = SubprocessCrewAIInvoker(data_root_base=temp_dir)
                requests = [
                    CrewAIProcessRequest(
                        job_id=f"job-{index}",
                        spec=agent_spec(),
                        prompt_context={"jobId": f"job-{index}", "evidenceRefs": []},
                        policy=policy,
                        process_timeout_seconds=30,
                    )
                    for index in range(2)
                ]
                results = BoundedCrewAIProcessPool(max_parallel=2, invoker=invoker).invoke_all(requests)

            self.assertTrue(all(result.succeeded for result in results))
            self.assertTrue(
                all(result.output.inferences[0].agent_name == "NamingAgent" for result in results if result.output)
            )
            self.assertEqual(server.request_count, 2)
            self.assertEqual(server.peak_active, 2)
            self.assertEqual(len({result.data_root for result in results}), 2)
            self.assertTrue(all(not Path(result.data_root).exists() for result in results))
        finally:
            server.close()


class _ConcurrentModelServer:
    def __init__(self, content: str) -> None:
        self.content = content
        self.active = 0
        self.peak_active = 0
        self.request_count = 0
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                with owner.lock:
                    owner.active += 1
                    owner.request_count += 1
                    owner.peak_active = max(owner.peak_active, owner.active)
                time.sleep(0.1)
                encoded = json.dumps({"choices": [{"message": {"content": owner.content}}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                with owner.lock:
                    owner.active -= 1

            def log_message(self, format, *args):  # noqa: A002
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
