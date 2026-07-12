from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.api.app.models import CreateJobRequest
from apps.api.app.store import create_store
from apps.worker.worker.agent_runtime import AgentRuntime, AgentRuntimeRequest
from apps.worker.worker.core_bridge import CoreBridge
from apps.worker.worker.reconstruction import ReconstructionRunner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run repeatable live-model Agent runtime evaluations.")
    parser.add_argument("input", type=Path, help="Authorized JavaScript package, archive, or script input.")
    parser.add_argument("--iterations", type=int, default=1, choices=range(1, 21))
    parser.add_argument("--cloud-mode", choices=("cloud_allowed", "local_only", "desensitized"), default="local_only")
    parser.add_argument("--model", help="Cloud/desensitized Agent model name.")
    parser.add_argument("--provider", default="openai-compatible", help="Cloud Agent provider name.")
    parser.add_argument("--local-model", help="Local/desensitized Agent model name.")
    parser.add_argument("--local-provider", default="openai-compatible", help="Local Agent provider name.")
    parser.add_argument("--max-parallel", type=int, default=5, choices=range(1, 11))
    parser.add_argument("--context-budget", type=int, default=16_000)
    parser.add_argument("--output", type=Path, help="Optional JSON report path; stdout is always written.")
    return parser.parse_args()


def job_config(args: argparse.Namespace) -> dict[str, Any]:
    config: dict[str, Any] = {
        "agents": {
            "maxParallel": args.max_parallel,
            "contextBudget": args.context_budget,
        }
    }
    if args.model:
        config.update({"agentModel": args.model, "agentModelProvider": args.provider})
    if args.local_model:
        config.update({"localAgentModel": args.local_model, "localAgentProvider": args.local_provider})
    return config


def load_json_artifact(store, job_id: str, artifact_id: str) -> dict[str, Any]:
    payload = json.loads(store.read_artifact(job_id, artifact_id))
    return payload if isinstance(payload, dict) else {}


def evaluate_once(*, store, input_path: Path, analysis, args: argparse.Namespace, iteration: int) -> dict[str, Any]:
    job = store.create_job(
        CreateJobRequest(
            project_id="agent-evaluation",
            owner_id="agent-evaluator",
            cloud_mode=args.cloud_mode,
            config=job_config(args),
        )
    )
    inventory = store.write_artifact(
        job.id,
        kind="input_inventory",
        stage="intake",
        filename=f"evaluation-{iteration}-input-inventory.json",
        content=json.dumps(analysis.inventory_artifact_payload).encode("utf-8"),
        content_type="application/json",
        producer="scripts.evaluate_agents",
    )
    ast_index = store.write_artifact(
        job.id,
        kind="ast_index",
        stage="indexing",
        filename=f"evaluation-{iteration}-ast-index.json",
        content=json.dumps(analysis.ast_index_artifact_payload).encode("utf-8"),
        content_type="application/json",
        producer="scripts.evaluate_agents",
        parent_artifact_ids=[inventory.id],
    )
    request = AgentRuntimeRequest(
        job_id=job.id,
        project_id=job.project_id,
        cloud_mode=job.cloud_mode,
        job_config=job.config,
        inventory_artifact_id=inventory.id,
        ast_index_artifact_id=ast_index.id,
        inventory_payload=analysis.inventory_artifact_payload,
        ast_index_payload=analysis.ast_index_artifact_payload,
    )
    started = time.perf_counter()
    result = AgentRuntime().run(job_id=job.id, store=store, request=request)
    reconstruction = ReconstructionRunner().run(
        job_id=job.id,
        input_path=input_path,
        store=store,
        parent_artifact_ids=[
            result.review_artifact.id,
            *[artifact.id for artifact in result.repair_instruction_artifacts],
        ],
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    plan = load_json_artifact(store, job.id, result.plan_artifact.id)
    review = load_json_artifact(store, job.id, result.review_artifact.id)
    reconstruction_plan = load_json_artifact(store, job.id, reconstruction.plan_artifact.id)
    agent_feedback = reconstruction_plan.get("plan", {}).get("agentFeedback", {})
    executions = [
        load_json_artifact(store, job.id, artifact.id)
        for artifact in result.agent_execution_artifacts
    ]
    agent_executions = [payload for payload in executions if isinstance(payload.get("name"), str)]
    isolated_process_invocations = [
        payload for payload in agent_executions if payload.get("processDataRootConfigured")
    ]
    schema_success = [
        payload
        for payload in isolated_process_invocations
        if payload.get("roleSchemaValidated") is True
    ]
    approved_repairs = review.get("repairInstructionIds", [])
    repair_count = len(result.repair_instruction_artifacts)
    return {
        "iteration": iteration,
        "jobId": job.id,
        "durationMs": duration_ms,
        "selectedAgents": plan.get("selectedAgents", []),
        "plannerFallbackReason": plan.get("fallbackReason"),
        "requestedMaxParallel": plan.get("requestedMaxParallel"),
        "effectiveMaxParallel": plan.get("effectiveMaxParallel"),
        "schedulerMode": plan.get("schedulerMode"),
        "agentExecutionCount": len(agent_executions),
        "isolatedProcessInvocationCount": len(isolated_process_invocations),
        "roleSchemaValidationRate": (
            round(len(schema_success) / len(isolated_process_invocations), 4)
            if isolated_process_invocations
            else 0
        ),
        "conflictCount": sum(len(payload.get("conflicts", [])) for payload in executions),
        "inferenceCount": len(result.inference_artifacts),
        "repairInstructionCount": repair_count,
        "approvedRepairInstructionCount": len(approved_repairs),
        "repairApprovalRate": round(len(approved_repairs) / repair_count, 4) if repair_count else 0,
        "appliedRepairActionCount": len(agent_feedback.get("appliedActions", [])),
        "rejectedRepairActionCount": len(agent_feedback.get("rejectedActions", [])),
        "reviewStatus": review.get("status"),
        "reviewFailureClass": review.get("failureClass"),
        "message": result.message,
        "input": str(input_path.resolve()),
    }


def main() -> int:
    args = parse_args()
    if not args.input.exists():
        raise SystemExit(f"Input does not exist: {args.input}")
    if args.cloud_mode == "cloud_allowed" and not args.model:
        raise SystemExit("--model is required for cloud_allowed evaluation.")
    if args.cloud_mode == "local_only" and not args.local_model:
        raise SystemExit("--local-model is required for local_only evaluation.")
    if args.context_budget < 1_000 or args.context_budget > 1_000_000:
        raise SystemExit("--context-budget must be between 1000 and 1000000.")

    analysis = CoreBridge().analyze_input_package(job_id="agent-evaluation-input", input_path=args.input)
    with tempfile.TemporaryDirectory(prefix="ai-jsunpack-agent-evaluation-") as temp_dir:
        root = Path(temp_dir)
        store = create_store(
            database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
            artifact_root=root / "artifacts",
        )
        try:
            runs = [
                evaluate_once(store=store, input_path=args.input, analysis=analysis, args=args, iteration=index)
                for index in range(1, args.iterations + 1)
            ]
        finally:
            store.close()

    report = {
        "kind": "agent_evaluation",
        "iterations": args.iterations,
        "cloudMode": args.cloud_mode,
        "model": args.model or args.local_model,
        "provider": args.provider if args.model else args.local_provider,
        "summary": {
            "meanDurationMs": round(statistics.fmean(run["durationMs"] for run in runs), 3),
            "meanIsolatedProcessInvocationCount": round(
                statistics.fmean(run["isolatedProcessInvocationCount"] for run in runs),
                3,
            ),
            "meanRoleSchemaValidationRate": round(
                statistics.fmean(run["roleSchemaValidationRate"] for run in runs),
                4,
            ),
            "meanRepairApprovalRate": round(statistics.fmean(run["repairApprovalRate"] for run in runs), 4),
            "totalAppliedRepairActions": sum(run["appliedRepairActionCount"] for run in runs),
            "totalConflicts": sum(run["conflictCount"] for run in runs),
            "reviewStatuses": [run["reviewStatus"] for run in runs],
        },
        "runs": runs,
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    print(serialized)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
