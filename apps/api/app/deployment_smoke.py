from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

from fastapi.testclient import TestClient

from apps.api.app import main as api_main
from apps.api.app.auth import AUTH_SECRET_ENV, create_auth_token
from apps.api.app.store import create_store
from apps.browser_runner.benchmark import BrowserRunnerSoakConfig, run_browser_runner_soak
from apps.worker.worker.pipeline import WorkerPipeline
from apps.worker.worker.runtime_smoke import BrowserSmokeCapture, BrowserSmokeRequest, RuntimeSmokeRunner


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\xf8\x0f"
    b"\x00\x01\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)
DEFAULT_AUTH_SECRET = "deployment-smoke-local-secret"
DEFAULT_PROJECT_ID = "deployment-smoke"
DEFAULT_OWNER_ID = "deployment-smoke-user"


@dataclass(frozen=True)
class DeploymentSmokeConfig:
    database_url: str | None = None
    artifact_root: str | None = None
    output_path: str | None = None
    auth_secret: str = DEFAULT_AUTH_SECRET
    project_id: str = DEFAULT_PROJECT_ID
    owner_id: str = DEFAULT_OWNER_ID
    soak_instances: int = 1
    soak_workers_per_instance: int = 1
    soak_runs: int = 2
    soak_capture_delay_ms: int = 0
    soak_fail_every: int = 0
    soak_timeout_seconds: float = 10.0


class SmokeBrowserAdapter:
    def capture(self, request: BrowserSmokeRequest) -> BrowserSmokeCapture:
        request.screenshot_path.write_bytes(PNG_1X1)
        return BrowserSmokeCapture(
            console_messages=[f"部署冒烟捕获 {request.target}:{request.attempt}"],
            responses=[f"200 {request.source_entry_path or request.entry_url}"],
            dom_summary={
                "title": "deployment-smoke",
                "nodeCount": 4,
                "textLength": 20,
                "textSample": "部署冒烟测试",
            },
        )


class _FakeWebhookResponse:
    def __enter__(self) -> "_FakeWebhookResponse":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    def read(self) -> bytes:
        return b"{}"


def run_deployment_smoke(config: DeploymentSmokeConfig) -> dict[str, Any]:
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    limitations: list[str] = []
    webhook_payloads: list[dict[str, Any]] = []

    with _smoke_workspace(config) as workspace:
        artifact_root = Path(config.artifact_root) if config.artifact_root else workspace / "artifacts"
        database_url = config.database_url or f"sqlite:///{(workspace / 'metadata.db').as_posix()}"
        if config.artifact_root is None:
            limitations.append("Artifact 文件已写入临时工作区；如需保留，请使用 --artifact-root。")
        store = create_store(database_url=database_url, artifact_root=artifact_root)
        input_root = _write_fixture_input(workspace)
        original_store = api_main.store
        original_urlopen = api_main.urlopen
        try:
            api_main.store = store
            api_main.urlopen = _fake_urlopen(webhook_payloads)
            with _temporary_environment(
                {
                    AUTH_SECRET_ENV: config.auth_secret,
                    api_main.OPS_WEBHOOK_URL_ENV: "https://deployment-smoke.invalid/webhook",
                    api_main.OPS_WEBHOOK_TIMEOUT_SECONDS_ENV: "1",
                    api_main.OPS_ALERT_RULES_JSON_ENV: json.dumps(
                        [
                            {
                                "code": "deployment_smoke_worker_job_count",
                                "severity": "warning",
                                "metricPath": "worker.jobCount",
                                "operator": "gte",
                                "threshold": 1,
                                "message": "部署冒烟测试中的 Worker 心跳超过了测试阈值。",
                                "serviceRole": "worker",
                                "enabled": True,
                            }
                        ]
                    ),
                }
            ):
                with TestClient(api_main.app) as client:
                    headers = _auth_headers(config, kind="user", projects={config.project_id: "owner"})
                    service_headers = _auth_headers(config, kind="service", projects={}, service_roles=["worker"])
                    health = _request_check(
                        checks,
                        "api_health",
                        lambda: client.get("/health"),
                        lambda response: response.status_code == 200 and response.json()["status"] == "ok",
                    )
                    created = _request_check(
                        checks,
                        "api_create_job",
                        lambda: client.post(
                            "/jobs",
                            json={
                                "projectId": config.project_id,
                                "ownerId": config.owner_id,
                                "cloudMode": "local_only",
                                "config": {"source": "deployment-smoke"},
                            },
                            headers=headers,
                        ),
                        lambda response: response.status_code == 200,
                    )
                    job_id = created.get("json", {}).get("job", {}).get("id")
                    if not isinstance(job_id, str):
                        _add_check(checks, "job_id_available", False, error="创建任务后未返回任务 ID。")
                        report = _final_report(config, checks, limitations, webhook_payloads, started, workspace)
                        _write_report(config, report)
                        return report
                    _request_check(
                        checks,
                        "api_upload_source",
                        lambda: client.post(
                            f"/jobs/{job_id}/upload",
                            files={"file": ("deployment-smoke.zip", _zip_input(input_root), "application/zip")},
                            headers=headers,
                        ),
                        lambda response: response.status_code == 200
                        and response.json()["artifacts"][0]["kind"] == "source_input",
                    )
                    pipeline_result = WorkerPipeline(
                        runtime_smoke_runner=RuntimeSmokeRunner(browser_adapter=SmokeBrowserAdapter())
                    ).run(job_id, input_path=input_root, store=store)
                    events = [event.status for event in pipeline_result.events]
                    artifacts = store.list_artifacts(job_id)
                    artifacts_by_kind = _artifacts_by_kind(artifacts)
                    _add_check(
                        checks,
                        "worker_pipeline_packaged_result",
                        bool(job_id and "result_package" in artifacts_by_kind and "packaging" in events),
                        evidence={
                            "jobId": job_id,
                            "finalEvent": events[-1] if events else None,
                            "artifactKinds": sorted(artifacts_by_kind),
                        },
                    )
                    latest_runtime = _request_check(
                        checks,
                        "api_latest_runtime_validation",
                        lambda: client.get(f"/jobs/{job_id}/runtime-validations/latest", headers=headers),
                        lambda response: response.status_code == 200
                        and response.json().get("status") in {"pass", "best_effort", "fail"},
                    )
                    reports = _request_check(
                        checks,
                        "api_reports_list",
                        lambda: client.get(f"/jobs/{job_id}/reports", headers=headers),
                        lambda response: response.status_code == 200
                        and {"audit_report", "html_report", "evidence_index"}.issubset(
                            {item["kind"] for item in response.json()}
                        ),
                    )
                    package_download = _request_check(
                        checks,
                        "api_result_package_download",
                        lambda: client.get(f"/jobs/{job_id}/result-package", headers=headers),
                        lambda response: response.status_code == 200 and len(response.content) > 0,
                        content_summary=True,
                    )
                    _record_ops_heartbeats(client, service_headers, job_id)
                    metrics = _request_check(
                        checks,
                        "ops_metrics",
                        lambda: client.get("/ops/metrics", headers=headers),
                        lambda response: response.status_code == 200
                        and response.json().get("serviceHeartbeatCounts", {}).get("worker") == 1
                        and response.json().get("serviceHeartbeatCounts", {}).get("browser-runner") == 1,
                    )
                    prometheus = _request_check(
                        checks,
                        "ops_prometheus",
                        lambda: client.get("/ops/prometheus", headers=headers),
                        lambda response: response.status_code == 200
                        and "ai_jsunpack_ops_active_heartbeats" in response.text
                        and "ai_jsunpack_ops_alerts" in response.text,
                    )
                    alerts = _request_check(
                        checks,
                        "ops_alert_events_and_webhook",
                        lambda: client.get("/ops/alerts", headers=headers),
                        lambda response: response.status_code == 200
                        and response.json().get("delivery", {}).get("status") == "delivered"
                        and len(response.json().get("events", [])) >= 1,
                    )
                    alert_events = _request_check(
                        checks,
                        "ops_alert_event_history",
                        lambda: client.get("/ops/alert-events", headers=headers),
                        lambda response: response.status_code == 200 and len(response.json()) >= 1,
                    )
                    retention_dry_run = _request_check(
                        checks,
                        "retention_cleanup_dry_run",
                        lambda: client.post(
                            f"/jobs/{job_id}/retention/cleanup",
                            json={
                                "dryRun": True,
                                "categories": ["logs", "screenshots"],
                                "retentionClasses": [],
                                "deleteExpired": False,
                                "reason": "部署冒烟测试试运行",
                            },
                            headers=headers,
                        ),
                        lambda response: response.status_code == 200 and response.json().get("candidateCount", 0) > 0,
                    )
                    retention_execute = _request_check(
                        checks,
                        "retention_cleanup_execute",
                        lambda: client.post(
                            f"/jobs/{job_id}/retention/cleanup",
                            json={
                                "dryRun": False,
                                "categories": ["logs", "screenshots"],
                                "retentionClasses": [],
                                "deleteExpired": False,
                                "reason": "部署冒烟测试清理",
                            },
                            headers=headers,
                        ),
                        lambda response: response.status_code == 200
                        and response.json().get("deletedCount", 0) == response.json().get("candidateCount", -1)
                        and response.json().get("deletedCount", 0) > 0,
                    )
                    deleted_artifact_id = _first_deleted_artifact_id(retention_execute.get("json", {}))
                    if deleted_artifact_id:
                        _request_check(
                            checks,
                            "retention_deleted_artifact_hidden",
                            lambda: client.get(
                                f"/jobs/{job_id}/artifacts/{deleted_artifact_id}/download",
                                headers=headers,
                            ),
                            lambda response: response.status_code == 404,
                        )
                    else:
                        _add_check(
                            checks,
                            "retention_deleted_artifact_hidden",
                            False,
                            error="保留策略清理未报告已删除的 Artifact ID。",
                        )

                    soak_result = run_browser_runner_soak(
                        BrowserRunnerSoakConfig(
                            instances=config.soak_instances,
                            workers_per_instance=config.soak_workers_per_instance,
                            runs=config.soak_runs,
                            capture_delay_ms=config.soak_capture_delay_ms,
                            fail_every=config.soak_fail_every,
                            timeout_seconds=config.soak_timeout_seconds,
                        )
                    )
                    _add_check(
                        checks,
                        "browser_runner_soak_baseline",
                        _soak_passed(soak_result),
                        evidence={
                            "submittedCount": soak_result.get("submittedCount"),
                            "completedCount": soak_result.get("completedCount"),
                            "statusCounts": soak_result.get("statusCounts"),
                            "backendAssessment": soak_result.get("backendAssessment"),
                        },
                    )
                    archive_manifest = _build_archive_manifest(
                        config=config,
                        workspace=workspace,
                        job_id=job_id,
                        artifacts=artifacts,
                        checks=checks,
                        health=health,
                        latest_runtime=latest_runtime,
                        reports=reports,
                        result_package=package_download,
                        metrics=metrics,
                        prometheus=prometheus,
                        alerts=alerts,
                        alert_events=alert_events,
                        retention_dry_run=retention_dry_run,
                        retention_execute=retention_execute,
                        soak_result=soak_result,
                    )
                    _add_check(
                        checks,
                        "archive_manifest_complete",
                        _archive_manifest_complete(archive_manifest),
                        evidence={
                            "topologyMode": archive_manifest["topologyMode"],
                            "archiveReady": archive_manifest["archiveReady"],
                            "artifactCount": archive_manifest["artifactCount"],
                            "retainedEvidence": archive_manifest["retainedEvidence"],
                        },
                    )
                    report = _final_report(
                        config,
                        checks,
                        limitations,
                        webhook_payloads,
                        started,
                        workspace,
                        job_id=job_id,
                        health=health,
                        latest_runtime=latest_runtime,
                        reports=reports,
                        result_package=package_download,
                        metrics=metrics,
                        prometheus=prometheus,
                        alerts=alerts,
                        alert_events=alert_events,
                        retention_dry_run=retention_dry_run,
                        retention_execute=retention_execute,
                        soak_result=soak_result,
                        archive_manifest=archive_manifest,
                    )
                    _write_report(config, report)
                    return report
        finally:
            api_main.store = original_store
            api_main.urlopen = original_urlopen
            store.close()


def parse_args(argv: list[str] | None = None) -> DeploymentSmokeConfig:
    parser = argparse.ArgumentParser(description="运行本地生产部署的 smoke/soak test 验收检查。")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--artifact-root", default=None)
    parser.add_argument("--output", dest="output_path", default=None)
    parser.add_argument("--auth-secret", default=DEFAULT_AUTH_SECRET)
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--owner-id", default=DEFAULT_OWNER_ID)
    parser.add_argument("--soak-instances", type=int, default=DeploymentSmokeConfig.soak_instances)
    parser.add_argument("--soak-workers-per-instance", type=int, default=DeploymentSmokeConfig.soak_workers_per_instance)
    parser.add_argument("--soak-runs", type=int, default=DeploymentSmokeConfig.soak_runs)
    parser.add_argument("--soak-capture-delay-ms", type=int, default=DeploymentSmokeConfig.soak_capture_delay_ms)
    parser.add_argument("--soak-fail-every", type=int, default=DeploymentSmokeConfig.soak_fail_every)
    parser.add_argument("--soak-timeout-seconds", type=float, default=DeploymentSmokeConfig.soak_timeout_seconds)
    return DeploymentSmokeConfig(**vars(parser.parse_args(argv)))


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    try:
        report = run_deployment_smoke(config)
    except Exception as error:
        report = _exception_report(config, error)
        _write_report(config, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("status") == "pass" else 1


@contextmanager
def _smoke_workspace(config: DeploymentSmokeConfig) -> Iterator[Path]:
    if config.artifact_root:
        root = Path(config.artifact_root).resolve().parent / ".deployment-smoke-work"
        root.mkdir(parents=True, exist_ok=True)
        yield root
        return
    with tempfile.TemporaryDirectory(prefix="ai-jsunpack-deployment-smoke-") as temp_dir:
        yield Path(temp_dir)


@contextmanager
def _temporary_environment(updates: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in updates}
    try:
        os.environ.update(updates)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _fake_urlopen(payloads: list[dict[str, Any]]):
    def fake(request, timeout=None):
        data = getattr(request, "data", None)
        if data:
            payloads.append(json.loads(data.decode("utf-8")))
        return _FakeWebhookResponse()

    return fake


def _write_fixture_input(workspace: Path) -> Path:
    input_root = workspace / "dist"
    asset_root = input_root / "assets"
    asset_root.mkdir(parents=True, exist_ok=True)
    (input_root / "index.html").write_text(
        '<!doctype html><title>部署冒烟测试</title><div id="app">部署冒烟测试</div>'
        '<script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (asset_root / "app.js").write_text("export function boot(){ return 'deployment-smoke'; } boot();", encoding="utf-8")
    return input_root


def _zip_input(input_root: Path) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(input_root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(input_root).as_posix())
    return buffer.getvalue()


def _auth_headers(
    config: DeploymentSmokeConfig,
    *,
    kind: str,
    projects: dict[str, str],
    service_roles: list[str] | None = None,
) -> dict[str, str]:
    subject = config.owner_id if kind == "user" else f"deployment-smoke-{kind}"
    token = create_auth_token(
        subject=subject,
        projects=projects,
        kind=kind,
        service_roles=service_roles,
        secret=config.auth_secret,
    )
    return {"Authorization": f"Bearer {token}"}


def _record_ops_heartbeats(client: TestClient, headers: dict[str, str], job_id: str) -> None:
    client.post(
        "/ops/heartbeats",
        json={
            "serviceRole": "worker",
            "instanceId": "deployment-smoke-worker",
            "status": "degraded",
            "ttlSeconds": 60,
            "metrics": {"phase": "completed", "jobCount": 1, "jobId": job_id},
            "alerts": [],
            "metadata": {"source": "deployment-smoke"},
        },
        headers=headers,
    )
    client.post(
        "/ops/heartbeats",
        json={
            "serviceRole": "browser-runner",
            "instanceId": "deployment-smoke-browser-runner",
            "status": "degraded",
            "ttlSeconds": 60,
            "metrics": {
                "queueBackend": "postgresql",
                "backendStatus": "ok",
                "queuedCount": 11,
                "runningCount": 0,
                "terminalCount": 1,
                "oldestQueuedAgeMs": 120000,
                "claimLatencyMs": 25,
                "averageRunDurationMs": 50,
                "retryRate": 0.5,
                "leaseRecoveryCount": 1,
                "expiredRunningCount": 1,
            },
            "alerts": [],
            "metadata": {"source": "deployment-smoke"},
        },
        headers=headers,
    )


def _request_check(
    checks: list[dict[str, Any]],
    name: str,
    request_fn,
    predicate,
    *,
    content_summary: bool = False,
) -> dict[str, Any]:
    try:
        response = request_fn()
        passed = bool(predicate(response))
        evidence = {
            "statusCode": response.status_code,
            "contentType": response.headers.get("content-type"),
        }
        if content_summary:
            evidence["bytes"] = len(response.content)
            evidence["sha256"] = hashlib.sha256(response.content).hexdigest()
        else:
            evidence.update(_response_body(response))
        _add_check(checks, name, passed, evidence=evidence)
        return evidence
    except Exception as error:
        _add_check(checks, name, False, error=str(error))
        return {"error": str(error)}


def _response_body(response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return {"json": response.json()}
    text = response.text
    return {"text": text[:4000]}


def _add_check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    *,
    evidence: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    checks.append(
        {
            "name": name,
            "status": "pass" if passed else "fail",
            "evidence": evidence or {},
            "error": error,
        }
    )


def _artifacts_by_kind(artifacts) -> dict[str, list[str]]:
    by_kind: dict[str, list[str]] = {}
    for artifact in artifacts:
        by_kind.setdefault(artifact.kind, []).append(artifact.id)
    return by_kind


def _first_deleted_artifact_id(payload: dict[str, Any]) -> str | None:
    for item in payload.get("items", []):
        if item.get("deleted") and isinstance(item.get("artifactId"), str):
            return item["artifactId"]
    return None


def _soak_passed(result: dict[str, Any]) -> bool:
    status_counts = result.get("statusCounts", {})
    assessment = result.get("backendAssessment", {})
    return (
        result.get("completedCount") == result.get("submittedCount")
        and status_counts.get("pass") == result.get("submittedCount")
        and assessment.get("messageQueueMigrationRequired") is False
        and assessment.get("recommendation") == "continue_shared_db_backend"
        and result.get("queueHealth", {}).get("status") == "ok"
    )


def _final_report(
    config: DeploymentSmokeConfig,
    checks: list[dict[str, Any]],
    limitations: list[str],
    webhook_payloads: list[dict[str, Any]],
    started: float,
    workspace: Path,
    **sections: Any,
) -> dict[str, Any]:
    failed = [check for check in checks if check["status"] != "pass"]
    return {
        "kind": "deployment_smoke_report",
        "schemaVersion": "1",
        "status": "pass" if not failed else "fail",
        "generatedAt": _utc_timestamp(),
        "durationMs": int((time.perf_counter() - started) * 1000),
        "config": _safe_config(config),
        "workspace": str(workspace),
        "checks": checks,
        "failedChecks": [check["name"] for check in failed],
        "webhookPayloadCount": len(webhook_payloads),
        "webhookPayloads": webhook_payloads,
        "limitations": limitations,
        **sections,
    }


def _build_archive_manifest(
    *,
    config: DeploymentSmokeConfig,
    workspace: Path,
    job_id: str,
    artifacts,
    checks: list[dict[str, Any]],
    health,
    latest_runtime,
    reports,
    result_package,
    metrics,
    prometheus,
    alerts,
    alert_events,
    retention_dry_run,
    retention_execute,
    soak_result,
) -> dict[str, Any]:
    artifact_items = list(artifacts)
    artifact_kinds = sorted({artifact.kind for artifact in artifact_items})
    checks_by_name = {check["name"]: check for check in checks}
    topology_mode = _topology_mode(config)
    archive_ready = bool(config.output_path and config.artifact_root)
    return {
        "kind": "deployment_smoke_archive_manifest",
        "schemaVersion": "1",
        "jobId": job_id,
        "topologyMode": topology_mode,
        "archiveReady": archive_ready,
        "workspace": str(workspace),
        "outputPath": config.output_path,
        "artifactRoot": config.artifact_root,
        "databaseUrlConfigured": bool(config.database_url),
        "artifactCount": len(artifact_items),
        "artifactKinds": artifact_kinds,
        "retainedArtifactKinds": artifact_kinds,
        "retainedEvidence": {
            "resultPackageBytes": result_package["bytes"] if "bytes" in result_package else None,
            "resultPackageSha256": result_package.get("sha256"),
            "resultPackageStatusCode": result_package.get("statusCode"),
            "runtimeStatus": latest_runtime.get("json", {}).get("status") if isinstance(latest_runtime, dict) else None,
            "reportKinds": [item["kind"] for item in reports.get("json", [])] if isinstance(reports, dict) else [],
            "healthStatus": health.get("json", {}).get("status") if isinstance(health, dict) else None,
            "metricsStatus": metrics.get("json", {}).get("status") if isinstance(metrics, dict) else None,
            "prometheusScraped": "ai_jsunpack_ops_active_heartbeats" in prometheus.get("text", "") if isinstance(prometheus, dict) else False,
            "alertDeliveryStatus": alerts.get("json", {}).get("delivery", {}).get("status") if isinstance(alerts, dict) else None,
            "alertEventCount": len(alert_events.get("json", [])) if isinstance(alert_events, dict) else None,
            "retentionDryRunCandidates": retention_dry_run.get("json", {}).get("candidateCount") if isinstance(retention_dry_run, dict) else None,
            "retentionDeletedCount": retention_execute.get("json", {}).get("deletedCount") if isinstance(retention_execute, dict) else None,
            "browserRunnerSoakRecommendation": soak_result.get("backendAssessment", {}).get("recommendation") if isinstance(soak_result, dict) else None,
        },
        "checkStatuses": {name: item["status"] for name, item in checks_by_name.items()},
    }


def _archive_manifest_complete(manifest: dict[str, Any]) -> bool:
    retained = manifest.get("retainedEvidence", {})
    required_kinds = {"audit_report", "evidence_index", "html_report", "result_package"}
    return (
        manifest.get("artifactCount", 0) > 0
        and required_kinds.issubset(set(manifest.get("artifactKinds", [])))
        and retained.get("resultPackageBytes", 0) > 0
        and bool(retained.get("resultPackageSha256"))
        and retained.get("alertDeliveryStatus") == "delivered"
        and retained.get("browserRunnerSoakRecommendation") == "continue_shared_db_backend"
        and retained.get("prometheusScraped") is True
    )


def _topology_mode(config: DeploymentSmokeConfig) -> str:
    if config.artifact_root and config.database_url and not config.database_url.startswith("sqlite"):
        return "production_like"
    if config.artifact_root:
        return "retained_local"
    return "ephemeral_local"


def _exception_report(config: DeploymentSmokeConfig, error: Exception) -> dict[str, Any]:
    return {
        "kind": "deployment_smoke_report",
        "schemaVersion": "1",
        "status": "fail",
        "generatedAt": _utc_timestamp(),
        "durationMs": 0,
        "config": _safe_config(config),
        "checks": [
            {
                "name": "deployment_smoke_exception",
                "status": "fail",
                "evidence": {},
                "error": str(error),
            }
        ],
        "failedChecks": ["deployment_smoke_exception"],
        "webhookPayloadCount": 0,
        "webhookPayloads": [],
        "limitations": [],
    }


def _safe_config(config: DeploymentSmokeConfig) -> dict[str, Any]:
    payload = asdict(config)
    payload["auth_secret"] = "<redacted>"
    return payload


def _write_report(config: DeploymentSmokeConfig, report: dict[str, Any]) -> None:
    if not config.output_path:
        return
    path = Path(config.output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    sys.exit(main())
