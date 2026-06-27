from __future__ import annotations

import io
import json
import tempfile
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
import zipfile

from apps.api.app.models import ArtifactRecord, FailureClass, JobRecord

DEFAULT_EVIDENCE_ATTACHMENT_KINDS = {
    "build_log",
    "runtime_comparison",
    "runtime_scenario",
    "runtime_screenshot",
    "runtime_trace",
}
SENSITIVITY_CLASSES = {"public", "derived", "source_sensitive", "secret"}
RETENTION_CLASSES = {"ephemeral", "project", "archive"}


@dataclass(frozen=True)
class EvidenceAttachmentPolicy:
    include_kinds: set[str] | None
    exclude_kinds: set[str]
    include_sensitivity_classes: set[str] | None
    include_retention_classes: set[str] | None
    max_bytes_per_artifact: int | None

    @property
    def candidate_kinds(self) -> set[str]:
        if self.include_kinds is None:
            return set(DEFAULT_EVIDENCE_ATTACHMENT_KINDS)
        return set(DEFAULT_EVIDENCE_ATTACHMENT_KINDS) | set(self.include_kinds)


class PackagingError(RuntimeError):
    pass


@dataclass(frozen=True)
class PackagingResult:
    audit_report_artifact: ArtifactRecord
    html_report_artifact: ArtifactRecord
    evidence_index_artifact: ArtifactRecord
    result_package_artifact: ArtifactRecord
    final_status: str
    failure_class: FailureClass
    failure_reason: str | None
    message: str

    @property
    def artifact_ids(self) -> list[str]:
        return [
            self.audit_report_artifact.id,
            self.html_report_artifact.id,
            self.evidence_index_artifact.id,
            self.result_package_artifact.id,
        ]


class PackagingRunner:
    """构建最终可下载包和可读审计报告。"""

    def run(
        self,
        *,
        job_id: str,
        store,
        parent_artifact_ids: list[str] | None = None,
    ) -> PackagingResult:
        store.update_status(job_id, "packaging")
        job = store.get_job(job_id)
        if job is None:
            raise PackagingError(f"Job not found during packaging: {job_id}")

        artifacts = store.list_artifacts(job_id)
        parents = parent_artifact_ids or [artifact.id for artifact in artifacts]
        audit_payload = self._audit_payload(job=job, artifacts=artifacts, store=store)
        decision = self._completion_decision(audit_payload)
        evidence_index = self._evidence_index(job=job, artifacts=artifacts, decision=decision, store=store)
        audit_payload["completionDecision"] = decision
        audit_payload["evidenceIndex"] = evidence_index
        report_markdown = self._audit_markdown(audit_payload, decision, evidence_index)
        report_html = self._audit_html(audit_payload, decision, evidence_index)

        audit_report_artifact = store.write_artifact(
            job_id,
            kind="audit_report",
            stage="packaging",
            filename="audit-report.md",
            content=report_markdown.encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
            producer="worker.packaging",
            parent_artifact_ids=parents,
        )

        html_report_artifact = store.write_artifact(
            job_id,
            kind="html_report",
            stage="packaging",
            filename="audit-report.html",
            content=report_html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
            producer="worker.packaging",
            parent_artifact_ids=[*parents, audit_report_artifact.id],
        )

        evidence_index_artifact = store.write_artifact(
            job_id,
            kind="evidence_index",
            stage="packaging",
            filename="evidence-index.json",
            content=self._json_text(evidence_index).encode("utf-8"),
            content_type="application/json",
            producer="worker.packaging",
            parent_artifact_ids=parents,
        )

        package_bytes = self._package_bytes(
            audit_payload=audit_payload,
            report_markdown=report_markdown,
            report_html=report_html,
            evidence_index=evidence_index,
            generated_project=self._latest_artifact(artifacts, "generated_project"),
            artifacts=artifacts,
            store=store,
        )
        result_package_artifact = store.write_artifact(
            job_id,
            kind="result_package",
            stage="packaging",
            filename="result-package.zip",
            content=package_bytes,
            content_type="application/zip",
            producer="worker.packaging",
            parent_artifact_ids=[*parents, audit_report_artifact.id, html_report_artifact.id, evidence_index_artifact.id],
        )

        return PackagingResult(
            audit_report_artifact=audit_report_artifact,
            html_report_artifact=html_report_artifact,
            evidence_index_artifact=evidence_index_artifact,
            result_package_artifact=result_package_artifact,
            final_status=decision["status"],
            failure_class=decision["failureClass"],
            failure_reason=decision["reason"],
            message=(
                "Packaging produced audit_report, html_report, evidence_index, "
                f"and result_package with final status {decision['status']}."
            ),
        )

    def _audit_payload(self, *, job: JobRecord, artifacts: list[ArtifactRecord], store) -> dict[str, Any]:
        payload = {
            "schemaVersion": "2026-06-14",
            "kind": "audit_report",
            "job": job.model_dump(by_alias=True),
            "policySummary": self._policy_summary(job=job, artifacts=artifacts),
            "artifactManifest": [artifact.model_dump(by_alias=True) for artifact in artifacts],
            "runtimeReports": self._load_json_artifacts(job.id, artifacts, store, "runtime_validation"),
            "runtimeTraces": self._load_json_artifacts(job.id, artifacts, store, "runtime_trace"),
            "runtimeComparisons": self._load_json_artifacts(job.id, artifacts, store, "runtime_comparison"),
            "reviewRuns": self._load_json_artifacts(job.id, artifacts, store, "review_run"),
            "inferenceRecords": self._load_json_artifacts(job.id, artifacts, store, "inference_record"),
            "toolCalls": self._load_json_artifacts(job.id, artifacts, store, "tool_call"),
            "toolRegistry": self._load_tool_registry(job.id, artifacts, store),
            "memoryRecords": self._load_json_artifacts(job.id, artifacts, store, "memory_record"),
            "runtimeDiagnoses": self._load_json_artifacts(job.id, artifacts, store, "runtime_diagnosis"),
            "reportSections": self._load_json_artifacts(job.id, artifacts, store, "report_section"),
            "buildArtifacts": self._load_json_artifacts(job.id, artifacts, store, "build_artifact"),
            "repairInstructions": self._load_json_artifacts(job.id, artifacts, store, "repair_instruction"),
            "opsAlertEvents": self._load_ops_alert_events(store),
        }
        payload["reviewFixSummary"] = self._review_fix_summary(payload["runtimeTraces"])
        return payload

    def _load_ops_alert_events(self, store) -> list[dict[str, Any]]:
        list_events = getattr(store, "list_ops_alert_events", None)
        if not callable(list_events):
            return []
        try:
            return [event.model_dump(by_alias=True) for event in list_events(limit=50)]
        except Exception:
            return []

    def _load_tool_registry(self, job_id: str, artifacts: list[ArtifactRecord], store) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for payload in self._load_json_artifacts(job_id, artifacts, store, "tool_registry"):
            raw_entries = payload.get("entries")
            if isinstance(raw_entries, list):
                entries.extend(entry for entry in raw_entries if isinstance(entry, dict))
        return entries

    def _load_json_artifacts(
        self,
        job_id: str,
        artifacts: list[ArtifactRecord],
        store,
        kind: str,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for artifact in artifacts:
            if artifact.kind != kind:
                continue
            try:
                record = json.loads(store.read_artifact(job_id, artifact.id).decode("utf-8"))
                if isinstance(record, dict):
                    record.setdefault("artifactId", artifact.id)
                records.append(record)
            except Exception as error:
                records.append(
                    {
                        "artifactId": artifact.id,
                        "kind": kind,
                        "status": "unreadable",
                        "error": str(error),
                    }
                )
        return records

    def _completion_decision(self, audit_payload: dict[str, Any]) -> dict[str, Any]:
        observations: list[dict[str, str]] = []
        decision_records = {
            "buildArtifacts": self._latest_decision_records(
                audit_payload["buildArtifacts"],
                key_name="reviewType",
            ),
            "runtimeReports": self._latest_decision_records(
                audit_payload["runtimeReports"],
                key_name=None,
            ),
            "reviewRuns": self._latest_decision_records(
                audit_payload["reviewRuns"],
                key_name="reviewType",
            ),
        }
        review_fix_summary = audit_payload.get("reviewFixSummary")
        if (
            isinstance(review_fix_summary, dict)
            and review_fix_summary.get("target") == "review_fix_convergence_summary"
            and review_fix_summary.get("available", True)
        ):
            status = review_fix_summary.get("status")
            if status in {"fail", "retry", "best_effort"}:
                observations.append(
                    {
                        "group": "reviewFixSummary",
                        "status": str(status),
                        "failureClass": str(review_fix_summary.get("failureClass") or "unknown"),
                        "decision": self._review_fix_decision(review_fix_summary),
                    }
                )
        for group, records in decision_records.items():
            for record in records:
                status = record.get("status")
                if status in {"fail", "retry", "best_effort"}:
                    observations.append(
                        {
                            "group": group,
                            "status": str(status),
                            "failureClass": str(record.get("failureClass") or "unknown"),
                            "decision": str(record.get("decision") or record.get("entryUrl") or "Validation did not fully pass."),
                        }
                    )

        if not observations:
            return {"status": "completed", "failureClass": "none", "reason": None, "observations": []}

        first_non_none = next(
            (item["failureClass"] for item in observations if item["failureClass"] != "none"),
            "unknown",
        )
        reason = "; ".join(
            f"{item['group']} {item['status']}: {item['decision']}" for item in observations[:3]
        )
        return {
            "status": "completed_best_effort",
            "failureClass": first_non_none,
            "reason": reason,
            "observations": observations,
        }

    def _latest_decision_records(self, records: list[dict[str, Any]], *, key_name: str | None) -> list[dict[str, Any]]:
        latest_attempt_by_key: dict[str, int] = {}
        for record in records:
            key = self._decision_record_key(record, key_name=key_name)
            attempt = self._record_attempt(record)
            latest_attempt_by_key[key] = max(attempt, latest_attempt_by_key.get(key, attempt))
        return [
            record
            for record in records
            if self._record_attempt(record) == latest_attempt_by_key[self._decision_record_key(record, key_name=key_name)]
        ]

    def _decision_record_key(self, record: dict[str, Any], *, key_name: str | None) -> str:
        if key_name is not None:
            return str(record.get(key_name) or "unknown")
        phase = "runtime_compare" if record.get("comparisonArtifactId") else "runtime_smoke"
        target = str(record.get("target") or "unknown")
        return f"{phase}:{target}"

    def _record_attempt(self, record: dict[str, Any]) -> int:
        attempt = record.get("attempt")
        if isinstance(attempt, int) and not isinstance(attempt, bool):
            return max(0, attempt)
        return 0

    def _audit_markdown(
        self,
        audit_payload: dict[str, Any],
        decision: dict[str, Any],
        evidence_index: dict[str, Any],
    ) -> str:
        job = audit_payload["job"]
        policy_summary = audit_payload["policySummary"]
        artifact_manifest = audit_payload["artifactManifest"]
        runtime_reports = audit_payload["runtimeReports"]
        runtime_boundaries = self._runtime_execution_boundaries(audit_payload["runtimeTraces"])
        runtime_operations = self._browser_runner_operations(runtime_boundaries)
        runtime_comparisons = audit_payload["runtimeComparisons"]
        review_runs = audit_payload["reviewRuns"]
        inference_records = audit_payload["inferenceRecords"]
        tool_calls = audit_payload["toolCalls"]
        tool_registry = audit_payload["toolRegistry"]
        memory_records = audit_payload["memoryRecords"]
        runtime_diagnoses = audit_payload["runtimeDiagnoses"]
        report_sections = audit_payload["reportSections"]
        build_artifacts = audit_payload["buildArtifacts"]
        review_fix_summary = audit_payload["reviewFixSummary"]
        ops_alert_events = self._ops_alert_event_rows(audit_payload["opsAlertEvents"])
        attachments = evidence_index["attachments"]

        lines = [
            "# AI JS Unpack Audit Report",
            "",
            f"- Job: `{job['id']}`",
            f"- Final status: `{decision['status']}`",
            f"- Cloud mode: `{job['cloudMode']}`",
            f"- Access boundary: owner `{policy_summary['accessBoundary']['ownerId']}` / project `{policy_summary['accessBoundary']['projectId']}`",
            f"- Artifacts included: {len(artifact_manifest)}",
            f"- Runtime validations: {len(runtime_reports)}",
            f"- Runtime comparisons: {len(runtime_comparisons)}",
            f"- Review runs: {len(review_runs)}",
            f"- Inference records: {len(inference_records)}",
            f"- Tool calls: {len(tool_calls)}",
            f"- Tool registry entries: {len(tool_registry)}",
            f"- Memory records: {len(memory_records)}",
            "",
            "## Completion Decision",
            "",
            decision["reason"] or "All collected build, review, and runtime validation evidence passed.",
            "",
            "## Review/Fix Convergence",
            "",
            self._review_fix_markdown(review_fix_summary),
            "",
            "## Policy Summary",
            "",
            self._policy_summary_markdown(policy_summary),
            "",
            "## Risk And Failure Groups",
            "",
            self._status_table(decision["observations"], ("group", "status", "failureClass", "decision"))
            if decision["observations"]
            else "No failing or best-effort observations were collected.",
            "",
            "## Build And Typecheck",
            "",
            self._status_table(build_artifacts, ("reviewType", "status", "failureClass", "decision")),
            "",
            "## Runtime Evidence",
            "",
            self._status_table(runtime_reports, ("target", "status", "entryUrl", "traceArtifactId")),
            "",
            "## Browser Runner Boundary",
            "",
            self._status_table(runtime_boundaries, ("stage", "runnerKind", "enforcement", "remoteRunId", "auth", "artifactExchange")),
            "",
            "## Browser Runner Operations",
            "",
            self._status_table(
                runtime_operations,
                (
                    "stage",
                    "remoteRunId",
                    "queueBackend",
                    "queueLength",
                    "runningCount",
                    "terminalCount",
                    "totalCount",
                    "oldestQueuedAgeMs",
                    "claimLatencyMs",
                    "averageRunDurationMs",
                    "retryRate",
                    "leaseRecoveryCount",
                    "expiredRunningCount",
                    "backendHealthStatus",
                    "alerts",
                ),
            ),
            "",
            "## Ops Alert Summary",
            "",
            self._status_table(
                ops_alert_events,
                ("checkedAt", "severity", "code", "serviceRole", "instanceId", "field", "value", "threshold", "deliveryStatus"),
            ),
            "",
            "## Runtime Compare",
            "",
            self._status_table(runtime_comparisons, ("status", "scenarioArtifactId", "screenshotArtifactIds", "traceArtifactIds")),
            "",
            "## Runtime Compare Difference Summary",
            "",
            self._runtime_compare_diff_markdown([*runtime_comparisons, *audit_payload["runtimeTraces"]]),
            "",
            "## Agent Runtime Audit",
            "",
            f"- Tool registry entries: {len(tool_registry)}",
            f"- Memory records: {self._memory_record_summary(memory_records)}",
            f"- Runtime diagnoses: {len(runtime_diagnoses)}",
            f"- Report sections: {len(report_sections)}",
            "",
            self._status_table(runtime_diagnoses, ("agentName", "targetStage", "status", "failureClass", "diagnosis")),
            "",
            "## Review Evidence",
            "",
            self._status_table(review_runs, ("reviewType", "status", "failureClass", "decision")),
            "",
            "## Artifact Manifest",
            "",
            "| Kind | Stage | Artifact | Producer | Size | Deep link |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
        for artifact in artifact_manifest:
            lines.append(
                f"| `{artifact['kind']}` | `{artifact['stage']}` | `{artifact['id']}` | "
                f"`{artifact['producer']}` | {artifact['size']} | `artifact://{artifact['id']}` |"
            )
        lines.extend(
            [
                "",
                "## Evidence Attachment Index",
                "",
                self._status_table(
                    attachments,
                    ("kind", "artifactId", "included", "packagePath", "reason"),
                ),
                "",
                "## Reproduction",
                "",
                "```bash",
                "unzip result-package.zip -d ai-jsunpack-result",
                "open ai-jsunpack-result/audit-report.html",
                "cat ai-jsunpack-result/evidence-index.json",
                "```",
                "",
                "For shell environments without `open`, load `audit-report.html` in a browser or inspect `audit-report.md`.",
                "",
            ]
        )
        return "\n".join(lines)

    def _audit_html(
        self,
        audit_payload: dict[str, Any],
        decision: dict[str, Any],
        evidence_index: dict[str, Any],
    ) -> str:
        job = audit_payload["job"]
        policy_summary = audit_payload["policySummary"]
        artifact_manifest = audit_payload["artifactManifest"]
        runtime_reports = audit_payload["runtimeReports"]
        runtime_boundaries = self._runtime_execution_boundaries(audit_payload["runtimeTraces"])
        runtime_operations = self._browser_runner_operations(runtime_boundaries)
        runtime_comparisons = audit_payload["runtimeComparisons"]
        review_runs = audit_payload["reviewRuns"]
        build_artifacts = audit_payload["buildArtifacts"]
        tool_registry = audit_payload["toolRegistry"]
        memory_records = audit_payload["memoryRecords"]
        runtime_diagnoses = audit_payload["runtimeDiagnoses"]
        report_sections = audit_payload["reportSections"]
        review_fix_summary = audit_payload["reviewFixSummary"]
        ops_alert_events = self._ops_alert_event_rows(audit_payload["opsAlertEvents"])
        attachments = evidence_index["attachments"]
        decision_text = decision["reason"] or "All collected build, review, and runtime validation evidence passed."

        return "\n".join(
            [
                "<!doctype html>",
                '<html lang="en">',
                "<head>",
                '<meta charset="utf-8">',
                '<meta name="viewport" content="width=device-width, initial-scale=1">',
                "<title>AI JS Unpack Audit Report</title>",
                "<style>",
                ":root{color-scheme:light;--ink:#0f172a;--muted:#475569;--primary:#0369a1;--border:#cbd5e1;--surface:#fff;--bg:#f0f9ff;--warn:#92400e;--fail:#991b1b;--pass:#166534}",
                "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Arial,sans-serif;line-height:1.5}",
                "main{max-width:1180px;margin:0 auto;padding:32px 20px 48px}h1,h2{font-family:Consolas,monospace;letter-spacing:0}h1{margin:0 0 8px;font-size:30px}h2{margin:26px 0 10px;font-size:18px}",
                ".summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:18px 0}.metric{border:1px solid var(--border);border-radius:8px;background:var(--surface);padding:12px}.metric span{display:block;color:var(--muted);font-size:12px}.metric strong{display:block;margin-top:6px;font-family:Consolas,monospace;font-size:18px;overflow-wrap:anywhere}",
                ".notice{border:1px solid var(--border);border-left:4px solid var(--primary);border-radius:8px;background:var(--surface);padding:12px;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden}th,td{border-bottom:1px solid var(--border);padding:8px;text-align:left;vertical-align:top;font-size:13px}th{color:var(--muted);font-size:12px}code{font-family:Consolas,monospace;font-size:12px;overflow-wrap:anywhere}pre{white-space:pre-wrap;border:1px solid var(--border);border-radius:8px;background:var(--surface);padding:12px}",
                ".status-completed{color:var(--pass)}.status-completed_best_effort{color:var(--warn)}.status-failed{color:var(--fail)}",
                "</style>",
                "</head>",
                "<body>",
                "<main>",
                "<h1>AI JS Unpack Audit Report</h1>",
                f"<p>Offline report for job <code>{escape(str(job['id']))}</code>.</p>",
                '<section class="summary" aria-label="Report summary">',
                self._metric_html("Final status", str(decision["status"]), f"status-{decision['status']}"),
                self._metric_html("Cloud mode", str(job["cloudMode"])),
                self._metric_html("Owner", str(policy_summary["accessBoundary"]["ownerId"])),
                self._metric_html("Project", str(policy_summary["accessBoundary"]["projectId"])),
                self._metric_html("Artifacts", str(len(artifact_manifest))),
                self._metric_html("Runtime validations", str(len(runtime_reports))),
                self._metric_html("Runtime comparisons", str(len(runtime_comparisons))),
                self._metric_html("Review runs", str(len(review_runs))),
                self._metric_html("Tool registry", str(len(tool_registry))),
                self._metric_html("Memory records", str(len(memory_records))),
                self._metric_html("Evidence attachments", str(sum(1 for item in attachments if item["included"]))),
                "</section>",
                "<h2>Completion Decision</h2>",
                f'<div class="notice">{escape(decision_text)}</div>',
                "<h2>Review/Fix Convergence</h2>",
                self._review_fix_html(review_fix_summary),
                "<h2>Policy Summary</h2>",
                self._html_table(
                    [policy_summary["modelPolicy"]],
                    ("cloudMode", "modelContextScope", "contextHandling", "cloudContextAllowed", "limitation"),
                ),
                self._html_table(
                    self._policy_summary_rows(policy_summary["sensitivityCounts"], "sensitivityClass"),
                    ("sensitivityClass", "count"),
                ),
                self._html_table(
                    self._policy_summary_rows(policy_summary["retentionCounts"], "retentionClass"),
                    ("retentionClass", "count"),
                ),
                "<h2>Risk And Failure Groups</h2>",
                self._html_table(decision["observations"], ("group", "status", "failureClass", "decision"))
                if decision["observations"]
                else '<div class="notice">No failing or best-effort observations were collected.</div>',
                "<h2>Build And Typecheck</h2>",
                self._html_table(build_artifacts, ("reviewType", "status", "failureClass", "decision")),
                "<h2>Runtime Evidence</h2>",
                self._html_table(runtime_reports, ("target", "status", "entryUrl", "traceArtifactId")),
                "<h2>Browser Runner Boundary</h2>",
                self._html_table(runtime_boundaries, ("stage", "runnerKind", "enforcement", "remoteRunId", "auth", "artifactExchange")),
                "<h2>Browser Runner Operations</h2>",
                self._html_table(
                    runtime_operations,
                    (
                        "stage",
                        "remoteRunId",
                        "queueBackend",
                        "queueLength",
                        "runningCount",
                        "terminalCount",
                        "totalCount",
                        "oldestQueuedAgeMs",
                        "claimLatencyMs",
                        "averageRunDurationMs",
                        "retryRate",
                        "leaseRecoveryCount",
                        "expiredRunningCount",
                        "backendHealthStatus",
                        "alerts",
                    ),
                ),
                "<h2>Ops Alert Summary</h2>",
                self._html_table(
                    ops_alert_events,
                    ("checkedAt", "severity", "code", "serviceRole", "instanceId", "field", "value", "threshold", "deliveryStatus"),
                ),
                "<h2>Runtime Compare</h2>",
                self._html_table(runtime_comparisons, ("status", "scenarioArtifactId", "screenshotArtifactIds", "traceArtifactIds")),
                "<h2>Runtime Compare Difference Summary</h2>",
                self._runtime_compare_diff_html([*runtime_comparisons, *audit_payload["runtimeTraces"]]),
                "<h2>Agent Runtime Audit</h2>",
                self._html_table(tool_registry, ("toolName", "toolVersion", "category", "caller")),
                self._html_table(memory_records, ("memoryType", "scope", "sensitivityClass", "retentionClass", "content")),
                self._html_table(runtime_diagnoses, ("agentName", "targetStage", "status", "failureClass", "diagnosis")),
                self._html_table(report_sections, ("title", "status", "confidence", "summary")),
                "<h2>Review Evidence</h2>",
                self._html_table(review_runs, ("reviewType", "status", "failureClass", "decision")),
                "<h2>Evidence Attachment Index</h2>",
                self._html_table(attachments, ("kind", "artifactId", "included", "packagePath", "reason")),
                "<h2>Artifact Manifest</h2>",
                self._html_table(artifact_manifest, ("kind", "stage", "id", "producer", "size")),
                "<h2>Reproduction</h2>",
                "<pre>unzip result-package.zip -d ai-jsunpack-result\nopen ai-jsunpack-result/audit-report.html\ncat ai-jsunpack-result/evidence-index.json</pre>",
                "</main>",
                "</body>",
                "</html>",
            ]
        )

    def _metric_html(self, label: str, value: str, class_name: str = "") -> str:
        class_attr = f' class="{escape(class_name)}"' if class_name else ""
        return f"<div class=\"metric\"><span>{escape(label)}</span><strong{class_attr}>{escape(value)}</strong></div>"

    def _html_table(self, records: list[dict[str, Any]], columns: tuple[str, ...]) -> str:
        if not records:
            return '<div class="notice">No records.</div>'
        header = "".join(f"<th>{escape(column)}</th>" for column in columns)
        rows = [f"<tr>{header}</tr>"]
        for record in records:
            cells = "".join(f"<td>{self._html_cell(record.get(column))}</td>" for column in columns)
            rows.append(f"<tr>{cells}</tr>")
        return f"<table>{''.join(rows)}</table>"

    def _policy_summary_markdown(self, policy_summary: dict[str, Any]) -> str:
        model_policy = policy_summary["modelPolicy"]
        return "\n".join(
            [
                "| Area | Value |",
                "| --- | --- |",
                f"| Owner | `{policy_summary['accessBoundary']['ownerId']}` |",
                f"| Project | `{policy_summary['accessBoundary']['projectId']}` |",
                f"| Cloud mode | `{model_policy['cloudMode']}` |",
                f"| Model context scope | `{model_policy['modelContextScope']}` |",
                f"| Model context handling | `{model_policy['contextHandling']}` |",
                f"| Cloud context allowed | `{model_policy['cloudContextAllowed']}` |",
                f"| Policy limitation | {model_policy['limitation']} |",
                f"| Sensitivity counts | `{self._count_summary(policy_summary['sensitivityCounts'])}` |",
                f"| Retention counts | `{self._count_summary(policy_summary['retentionCounts'])}` |",
            ]
        )

    def _policy_summary_rows(self, counts: dict[str, int], key: str) -> list[dict[str, Any]]:
        return [{key: name, "count": count} for name, count in sorted(counts.items())]

    def _count_summary(self, counts: dict[str, int]) -> str:
        return ", ".join(f"{name}={count}" for name, count in sorted(counts.items())) or "none"

    def _memory_record_summary(self, records: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for record in records:
            memory_type = str(record.get("memoryType") or "unknown")
            counts[memory_type] = counts.get(memory_type, 0) + 1
        return self._count_summary(counts)

    def _html_cell(self, value: Any) -> str:
        if value is None:
            return ""
        text = str(value)
        if len(text) > 240:
            text = f"{text[:237]}..."
        return escape(text)

    def _runtime_compare_comparison_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [record for record in records if isinstance(record.get("differences"), dict)]

    def _runtime_compare_matrix_summaries(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summaries = [record for record in records if record.get("target") == "runtime_compare_matrix"]
        return sorted(summaries, key=self._record_attempt)

    def _runtime_compare_retry_summary(self, records: list[dict[str, Any]]) -> dict[str, Any] | None:
        summaries = [record for record in records if record.get("target") == "runtime_compare_retry_summary"]
        if not summaries:
            return None
        return max(summaries, key=lambda record: int(record.get("attemptsUsed") or 0))

    def _review_fix_summary(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        summaries = [record for record in records if record.get("target") == "review_fix_convergence_summary"]
        if not summaries:
            return {
                "target": "review_fix_convergence_summary",
                "status": "best_effort",
                "failureClass": "unknown",
                "finalOutcome": "best_effort_with_limitations",
                "available": False,
                "decision": "Review/Fix convergence summary evidence was not available at packaging time.",
                "evidenceArtifactIds": [],
                "evidenceLinks": [],
            }
        summary = summaries[-1]
        summary.setdefault("available", True)
        return summary

    def _review_fix_decision(self, summary: dict[str, Any]) -> str:
        final_outcome = str(summary.get("finalOutcome") or "best_effort_with_limitations")
        runtime_compare = summary.get("runtimeCompare") if isinstance(summary.get("runtimeCompare"), dict) else {}
        build_typecheck = summary.get("buildTypecheck") if isinstance(summary.get("buildTypecheck"), dict) else {}
        if final_outcome == "repaired_passed":
            return "Review/Fix applied deterministic repair evidence and final validation passed."
        if final_outcome == "passed_without_repair":
            return "Build/typecheck and runtime compare passed without deterministic repair."
        if final_outcome == "budget_exhausted_best_effort":
            return (
                "Review/Fix stopped after using "
                f"{runtime_compare.get('attemptsUsed')}/{runtime_compare.get('maxAttempts')} runtime compare attempts."
            )
        if final_outcome == "no_deterministic_repair":
            return "Review/Fix found no applicable low-risk deterministic repair for the remaining failure."
        if build_typecheck.get("needsAttention") or runtime_compare.get("finalReviewStatus") in {"fail", "best_effort", "retry"}:
            return "Review/Fix completed with remaining best-effort limitations."
        return str(summary.get("decision") or "Review/Fix convergence completed.")

    def _review_fix_markdown(self, summary: dict[str, Any]) -> str:
        runtime_compare = summary.get("runtimeCompare") if isinstance(summary.get("runtimeCompare"), dict) else {}
        build_typecheck = summary.get("buildTypecheck") if isinstance(summary.get("buildTypecheck"), dict) else {}
        next_steps = self._string_list(summary.get("nextSteps"))
        policy = summary.get("policy") if isinstance(summary.get("policy"), dict) else {}
        failure_action_map = summary.get("failureActionMap") if isinstance(summary.get("failureActionMap"), list) else []
        rows = [
            {"area": "Final outcome", "value": summary.get("finalOutcome")},
            {"area": "Status", "value": summary.get("status")},
            {"area": "Decision", "value": self._review_fix_decision(summary)},
            {
                "area": "Policy",
                "value": (
                    f"lowRiskAuto={policy.get('allowLowRiskRepairs')}; "
                    f"actions={', '.join(self._string_list(policy.get('allowedRepairActions'))) or 'default'}"
                ),
            },
            {
                "area": "Build/typecheck repairs",
                "value": (
                    f"instructions={build_typecheck.get('repairInstructionCount')}; "
                    f"applied={build_typecheck.get('appliedRepairCount')}; "
                    f"allPassed={build_typecheck.get('allPassed')}"
                ),
            },
            {
                "area": "Runtime compare budget",
                "value": (
                    f"attempts={runtime_compare.get('attemptsUsed')}/{runtime_compare.get('maxAttempts')}; "
                    f"stoppedReason={runtime_compare.get('stoppedReason')}; "
                    f"finalReviewStatus={runtime_compare.get('finalReviewStatus')}"
                ),
            },
            {
                "area": "Runtime repairs",
                "value": (
                    f"planned={runtime_compare.get('plannedRepairCount')}; "
                    f"applied={runtime_compare.get('appliedRepairCount')}; "
                    f"finalProject={runtime_compare.get('finalProjectArtifactId')}"
                ),
            },
            {
                "area": "Failure/action mapping",
                "value": self._failure_action_map_summary(failure_action_map),
            },
            {
                "area": "Next steps",
                "value": " | ".join(next_steps) if next_steps else "No follow-up action required.",
            },
        ]
        return self._status_table(rows, ("area", "value"))

    def _review_fix_html(self, summary: dict[str, Any]) -> str:
        runtime_compare = summary.get("runtimeCompare") if isinstance(summary.get("runtimeCompare"), dict) else {}
        build_typecheck = summary.get("buildTypecheck") if isinstance(summary.get("buildTypecheck"), dict) else {}
        next_steps = self._string_list(summary.get("nextSteps"))
        policy = summary.get("policy") if isinstance(summary.get("policy"), dict) else {}
        failure_action_map = summary.get("failureActionMap") if isinstance(summary.get("failureActionMap"), list) else []
        rows = [
            {"area": "Final outcome", "value": summary.get("finalOutcome")},
            {"area": "Status", "value": summary.get("status")},
            {"area": "Decision", "value": self._review_fix_decision(summary)},
            {
                "area": "Policy",
                "value": (
                    f"lowRiskAuto={policy.get('allowLowRiskRepairs')}; "
                    f"actions={', '.join(self._string_list(policy.get('allowedRepairActions'))) or 'default'}"
                ),
            },
            {
                "area": "Build/typecheck repairs",
                "value": (
                    f"instructions={build_typecheck.get('repairInstructionCount')}; "
                    f"applied={build_typecheck.get('appliedRepairCount')}; "
                    f"allPassed={build_typecheck.get('allPassed')}"
                ),
            },
            {
                "area": "Runtime compare budget",
                "value": (
                    f"attempts={runtime_compare.get('attemptsUsed')}/{runtime_compare.get('maxAttempts')}; "
                    f"stoppedReason={runtime_compare.get('stoppedReason')}; "
                    f"finalReviewStatus={runtime_compare.get('finalReviewStatus')}"
                ),
            },
            {
                "area": "Runtime repairs",
                "value": (
                    f"planned={runtime_compare.get('plannedRepairCount')}; "
                    f"applied={runtime_compare.get('appliedRepairCount')}; "
                    f"finalProject={runtime_compare.get('finalProjectArtifactId')}"
                ),
            },
            {
                "area": "Failure/action mapping",
                "value": self._failure_action_map_summary(failure_action_map),
            },
            {
                "area": "Next steps",
                "value": " | ".join(next_steps) if next_steps else "No follow-up action required.",
            },
        ]
        return self._html_table(rows, ("area", "value"))

    def _failure_action_map_summary(self, records: list[Any]) -> str:
        active = [record for record in records if isinstance(record, dict) and record.get("status") == "active"]
        scoped = active or [record for record in records if isinstance(record, dict)]
        parts = []
        for record in scoped[:4]:
            automatic_actions = self._string_list(record.get("automaticActions"))
            parts.append(
                f"{record.get('failureClass') or 'unknown'}->{', '.join(automatic_actions) or 'audit-only'}"
            )
        return "; ".join(parts) or "none"

    def _runtime_compare_diff_markdown(self, records: list[dict[str, Any]]) -> str:
        comparison_records = self._runtime_compare_comparison_records(records)
        matrix_summaries = self._runtime_compare_matrix_summaries(records)
        retry_summary = self._runtime_compare_retry_summary(records)
        if not comparison_records and not matrix_summaries and not retry_summary:
            return "No runtime comparison difference records."
        rows = []
        if matrix_summaries or retry_summary:
            rows.extend(
                [
                    "| Summary | Value |",
                    "| --- | --- |",
                    *[
                        "| Matrix attempt "
                        + self._cell(summary.get("attempt"))
                        + " | "
                        + self._cell(
                            f"selected={summary.get('selectedRunCount')}/{summary.get('requestedRunCount')}; "
                            f"omitted={summary.get('omittedRunCount')}; maxMatrixRuns={summary.get('maxMatrixRuns')}; "
                            f"selection={summary.get('matrixSelection')}"
                        )
                        + " |"
                        for summary in matrix_summaries
                    ],
                ]
            )
            if retry_summary:
                rows.append(
                    "| Retry budget | "
                    + self._cell(
                        f"attempts={retry_summary.get('attemptsUsed')}/{retry_summary.get('maxAttempts')}; "
                        f"budgetExhausted={retry_summary.get('budgetExhausted')}; "
                        f"stoppedReason={retry_summary.get('stoppedReason')}"
                    )
                    + " |"
                )
            rows.append("")
        rows.extend(
            [
                "| Comparison | Status | Scope | Screenshot | DOM | Network | Console | Evidence |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for record in comparison_records:
            differences = record.get("differences") or {}
            rows.append(
                "| "
                + " | ".join(
                    [
                        self._cell(record.get("artifactId") or record.get("id")),
                        self._cell(record.get("status")),
                        self._cell(self._runtime_compare_scope_label(differences)),
                        self._cell(self._screenshot_diff_label(differences)),
                        self._cell(self._dom_diff_label(differences)),
                        self._cell(self._collection_diff_label(differences.get("networkDiff"))),
                        self._cell(self._collection_diff_label(differences.get("consoleDiff"))),
                        self._cell(self._runtime_compare_evidence_links(record)),
                    ]
                )
                + " |"
            )
        return "\n".join(rows)

    def _runtime_compare_diff_html(self, records: list[dict[str, Any]]) -> str:
        comparison_records = self._runtime_compare_comparison_records(records)
        matrix_summaries = self._runtime_compare_matrix_summaries(records)
        retry_summary = self._runtime_compare_retry_summary(records)
        if not comparison_records and not matrix_summaries and not retry_summary:
            return '<div class="notice">No runtime comparison difference records.</div>'
        summary_html = ""
        if matrix_summaries or retry_summary:
            summary_rows = []
            for summary in matrix_summaries:
                value = (
                    f"selected={summary.get('selectedRunCount')}/{summary.get('requestedRunCount')}; "
                    f"omitted={summary.get('omittedRunCount')}; maxMatrixRuns={summary.get('maxMatrixRuns')}; "
                    f"selection={summary.get('matrixSelection')}"
                )
                summary_rows.append(
                    f"<tr><td>Matrix attempt {self._html_cell(summary.get('attempt'))}</td><td>{self._html_cell(value)}</td></tr>"
                )
            if retry_summary:
                value = (
                    f"attempts={retry_summary.get('attemptsUsed')}/{retry_summary.get('maxAttempts')}; "
                    f"budgetExhausted={retry_summary.get('budgetExhausted')}; "
                    f"stoppedReason={retry_summary.get('stoppedReason')}"
                )
                summary_rows.append(f"<tr><td>Retry budget</td><td>{self._html_cell(value)}</td></tr>")
            summary_html = "<table><tr><th>Summary</th><th>Value</th></tr>" + "".join(summary_rows) + "</table>"
        rows = [
            "<tr><th>Comparison</th><th>Status</th><th>Scope</th><th>Screenshot</th><th>DOM</th><th>Network</th><th>Console</th><th>Evidence</th></tr>"
        ]
        for record in comparison_records:
            differences = record.get("differences") or {}
            cells = [
                record.get("artifactId") or record.get("id"),
                record.get("status"),
                self._runtime_compare_scope_label(differences),
                self._screenshot_diff_label(differences),
                self._dom_diff_label(differences),
                self._collection_diff_label(differences.get("networkDiff")),
                self._collection_diff_label(differences.get("consoleDiff")),
                self._runtime_compare_evidence_links(record),
            ]
            rows.append("<tr>" + "".join(f"<td>{self._html_cell(value)}</td>" for value in cells) + "</tr>")
        return summary_html + f"<table>{''.join(rows)}</table>"

    def _runtime_execution_boundaries(self, traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for trace in traces:
            boundary = trace.get("executionBoundary")
            if not isinstance(boundary, dict):
                continue
            if any(key in boundary for key in ("runnerKind", "remoteRunId")):
                rows.append(self._runtime_boundary_row(trace, boundary, stage=str(trace.get("target") or "runtime")))
                continue
            for stage, nested in boundary.items():
                if isinstance(nested, dict) and any(key in nested for key in ("runnerKind", "remoteRunId")):
                    rows.append(self._runtime_boundary_row(trace, nested, stage=str(stage)))
        return rows

    def _runtime_boundary_row(self, trace: dict[str, Any], boundary: dict[str, Any], *, stage: str) -> dict[str, Any]:
        return {
            "stage": stage,
            "runnerKind": boundary.get("runnerKind") or "",
            "enforcement": boundary.get("enforcement") or "",
            "remoteRunId": boundary.get("remoteRunId") or "",
            "auth": boundary.get("auth") or "",
            "artifactExchange": boundary.get("artifactExchange") or "",
            "queueBackend": boundary.get("queueBackend") or "",
            "queueLength": boundary.get("queueLength"),
            "runningCount": boundary.get("runningCount"),
            "terminalCount": boundary.get("terminalCount"),
            "totalCount": boundary.get("totalCount"),
            "oldestQueuedAgeMs": boundary.get("oldestQueuedAgeMs"),
            "claimLatencyMs": boundary.get("claimLatencyMs"),
            "averageRunDurationMs": boundary.get("averageRunDurationMs"),
            "retryRate": boundary.get("retryRate"),
            "leaseRecoveryCount": boundary.get("leaseRecoveryCount"),
            "expiredRunningCount": boundary.get("expiredRunningCount"),
            "backendHealthStatus": boundary.get("backendHealthStatus") or "",
            "alerts": self._browser_runner_alert_label(boundary.get("alerts")),
            "traceArtifactId": trace.get("artifactId") or "",
        }

    def _browser_runner_operations(self, boundary_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            row
            for row in boundary_rows
            if row.get("runnerKind") == "remote_browser_runner"
            and any(
                row.get(field) not in (None, "")
                for field in (
                    "queueBackend",
                    "queueLength",
                    "runningCount",
                    "claimLatencyMs",
                    "oldestQueuedAgeMs",
                    "terminalCount",
                    "totalCount",
                    "averageRunDurationMs",
                    "retryRate",
                    "leaseRecoveryCount",
                    "expiredRunningCount",
                    "backendHealthStatus",
                    "alerts",
                )
            )
        ]

    def _browser_runner_alert_label(self, alerts: Any) -> str:
        if not isinstance(alerts, list) or not alerts:
            return "none"
        labels: list[str] = []
        for alert in alerts[:4]:
            if isinstance(alert, dict):
                labels.append(
                    ":".join(
                        str(part)
                        for part in (
                            alert.get("severity") or "warning",
                            alert.get("code") or "unknown",
                            alert.get("field") or "field",
                        )
                    )
                )
        remaining = len(alerts) - len(labels)
        if remaining > 0:
            labels.append(f"+{remaining} more")
        return ", ".join(labels) if labels else "none"

    def _runtime_compare_scope_label(self, differences: dict[str, Any]) -> str:
        scope = differences.get("comparisonScope") or {}
        viewport = scope.get("viewport") or {}
        viewport_name = viewport.get("name")
        viewport_size = ""
        if viewport.get("width") and viewport.get("height"):
            viewport_size = f"{viewport.get('width')}x{viewport.get('height')}"
        viewport_label = " ".join(str(part) for part in (viewport_name, viewport_size) if part) or "default viewport"
        return f"{scope.get('scenarioName') or 'unknown scenario'} / {viewport_label}"

    def _screenshot_diff_label(self, differences: dict[str, Any]) -> str:
        screenshot = differences.get("screenshotDiff") or {}
        changed = screenshot.get("changed", differences.get("screenshotChanged"))
        pixel_status = screenshot.get("pixelDiffStatus", "unknown")
        original_size = screenshot.get("originalSizeBytes")
        reconstructed_size = screenshot.get("reconstructedSizeBytes")
        changed_pixels = screenshot.get("changedPixelCount")
        pixel_count = screenshot.get("pixelCount")
        ratio = screenshot.get("changedPixelRatio")
        sizes = ""
        if original_size is not None or reconstructed_size is not None:
            sizes = f" ({original_size or 0}B -> {reconstructed_size or 0}B)"
        pixels = ""
        if changed_pixels is not None and pixel_count is not None:
            percent = f"{float(ratio) * 100:.3f}%" if isinstance(ratio, (int, float)) else "unknown"
            pixels = f"; pixels={changed_pixels}/{pixel_count} ({percent})"
        diff_artifact = f"; diff={screenshot.get('diffArtifactId')}" if screenshot.get("diffArtifactId") else ""
        return f"changed={changed}; pixel={pixel_status}{pixels}{diff_artifact}{sizes}"

    def _dom_diff_label(self, differences: dict[str, Any]) -> str:
        dom_differences = differences.get("domDifferences") or []
        changed_fields = differences.get("changedDomFields") or []
        if dom_differences:
            return f"{len(dom_differences)} path changes: {', '.join(str(item.get('path')) for item in dom_differences[:4])}"
        if changed_fields:
            return f"{len(changed_fields)} top-level fields: {', '.join(map(str, changed_fields[:4]))}"
        return "none"

    def _collection_diff_label(self, diff: Any) -> str:
        if not isinstance(diff, dict):
            return "unavailable"
        original_only = len(diff.get("originalOnly") or [])
        reconstructed_only = len(diff.get("reconstructedOnly") or [])
        shared = len(diff.get("shared") or [])
        groups = ", ".join(sorted((diff.get("groups") or {}).keys())[:4]) or "none"
        return f"original-only={original_only}; reconstructed-only={reconstructed_only}; shared={shared}; groups={groups}"

    def _runtime_compare_evidence_links(self, record: dict[str, Any]) -> str:
        links = self._runtime_compare_evidence_link_list(record)
        return ", ".join(links) if links else "none"

    def _runtime_compare_evidence_link_list(
        self,
        record: dict[str, Any],
        *,
        comparison_artifact_id: str | None = None,
    ) -> list[str]:
        artifact_ids = [
            comparison_artifact_id or record.get("artifactId"),
            record.get("scenarioArtifactId"),
            *(record.get("traceArtifactIds") or []),
            *(record.get("screenshotArtifactIds") or []),
        ]
        return [f"artifact://{artifact_id}" for artifact_id in dict.fromkeys(artifact_ids) if artifact_id]

    def _evidence_index(
        self,
        *,
        job: JobRecord,
        artifacts: list[ArtifactRecord],
        decision: dict[str, Any],
        store,
    ) -> dict[str, Any]:
        policy = self._evidence_attachment_policy(job)
        attachments = [
            self._evidence_attachment(artifact, policy, store)
            for artifact in artifacts
            if artifact.kind in policy.candidate_kinds
        ]
        package_contents = self._package_contents(
            attachments=attachments,
            generated_project=self._latest_artifact(artifacts, "generated_project"),
        )
        report_sections = self._report_sections(attachments=attachments, artifacts=artifacts, job_id=job.id, store=store)
        return {
            "schemaVersion": "2026-06-14",
            "kind": "evidence_index",
            "jobId": job.id,
            "policySummary": self._policy_summary(job=job, artifacts=artifacts),
            "attachmentPolicy": self._evidence_attachment_policy_payload(policy),
            "attachments": attachments,
            "packageContents": package_contents,
            "reportSections": report_sections,
            "failureSummary": decision["observations"],
            "includedCount": sum(1 for item in attachments if item["included"]),
            "omittedCount": sum(1 for item in attachments if not item["included"]),
        }

    def _policy_summary(self, *, job: JobRecord, artifacts: list[ArtifactRecord]) -> dict[str, Any]:
        return {
            "accessBoundary": {
                "ownerId": job.owner_id,
                "projectId": job.project_id,
                "enforcement": "api_request_context",
            },
            "modelPolicy": self._model_policy_summary(job),
            "sensitivityCounts": self._artifact_count_by(artifacts, "sensitivity_class"),
            "retentionCounts": self._artifact_count_by(artifacts, "retention_class"),
        }

    def _model_policy_summary(self, job: JobRecord) -> dict[str, Any]:
        if job.cloud_mode == "cloud_allowed":
            return {
                "cloudMode": job.cloud_mode,
                "modelContextScope": "cloud",
                "contextHandling": "full_context_allowed_when_configured",
                "cloudContextAllowed": True,
                "limitation": "Cloud model use still depends on configured model provider credentials.",
            }
        if job.cloud_mode == "desensitized":
            return {
                "cloudMode": job.cloud_mode,
                "modelContextScope": "sanitized_cloud_or_local",
                "contextHandling": "deterministic_context_redaction",
                "cloudContextAllowed": True,
                "limitation": (
                    "Agent model context uses deterministic source excerpt, evidence reference, memory, "
                    "path, and symbol redaction; original artifacts remain governed by artifact access "
                    "and evidence attachment policy."
                ),
            }
        return {
            "cloudMode": job.cloud_mode,
            "modelContextScope": "local",
            "contextHandling": "local_context_only",
            "cloudContextAllowed": False,
            "limitation": "Cloud model context is denied unless the job is created with cloud_allowed or desensitized mode.",
        }

    def _artifact_count_by(self, artifacts: list[ArtifactRecord], field_name: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for artifact in artifacts:
            value = str(getattr(artifact, field_name))
            counts[value] = counts.get(value, 0) + 1
        return counts

    def _package_contents(
        self,
        *,
        attachments: list[dict[str, Any]],
        generated_project: ArtifactRecord | None,
    ) -> list[dict[str, Any]]:
        contents = [
            self._package_content(
                path="audit-report.md",
                content_type="text/markdown; charset=utf-8",
                source="audit_report",
                description="Human-readable Markdown audit report.",
            ),
            self._package_content(
                path="audit-report.html",
                content_type="text/html; charset=utf-8",
                source="html_report",
                description="Offline HTML audit report.",
            ),
            self._package_content(
                path="audit.json",
                content_type="application/json",
                source="audit_payload",
                description="Structured audit payload with report inputs and completion decision.",
            ),
            self._package_content(
                path="evidence-index.json",
                content_type="application/json",
                source="evidence_index",
                description="Structured package, report, and evidence attachment index.",
            ),
            self._package_content(
                path="artifact-manifest.json",
                content_type="application/json",
                source="artifact_manifest",
                description="Artifact metadata manifest captured at packaging time.",
            ),
            self._package_content(
                path="build-artifacts.json",
                content_type="application/json",
                source="build_artifact",
                description="Build and typecheck evidence records.",
            ),
            self._package_content(
                path="inference-records.json",
                content_type="application/json",
                source="inference_record",
                description="Agent inference audit records.",
            ),
            self._package_content(
                path="runtime-report.json",
                content_type="application/json",
                source="runtime_validation",
                description="Browser runtime validation records.",
            ),
            self._package_content(
                path="runtime-traces.json",
                content_type="application/json",
                source="runtime_trace",
                description="Browser runtime trace records, including remote Browser Runner execution boundaries.",
            ),
            self._package_content(
                path="runtime-comparisons.json",
                content_type="application/json",
                source="runtime_comparison",
                description="Original-vs-reconstructed runtime comparison records.",
            ),
            self._package_content(
                path="review-runs.json",
                content_type="application/json",
                source="review_run",
                description="Review and gate decision records.",
            ),
            self._package_content(
                path="tool-calls.json",
                content_type="application/json",
                source="tool_call",
                description="Tool call audit records.",
            ),
            self._package_content(
                path="tool-registry.json",
                content_type="application/json",
                source="tool_registry",
                description="Tool Registry entries available to the Agent runtime.",
            ),
            self._package_content(
                path="memory-records.json",
                content_type="application/json",
                source="memory_record",
                description="Short-term, long-term, entity, and scenario memory records.",
            ),
            self._package_content(
                path="runtime-diagnoses.json",
                content_type="application/json",
                source="runtime_diagnosis",
                description="Runtime Agent structured diagnosis records.",
            ),
            self._package_content(
                path="report-sections.json",
                content_type="application/json",
                source="report_section",
                description="Report Agent structured section records.",
            ),
            self._package_content(
                path="repair-instructions.json",
                content_type="application/json",
                source="repair_instruction",
                description="Structured repair instructions and applied repair evidence.",
            ),
            self._package_content(
                path="review-fix-summary.json",
                content_type="application/json",
                source="review_fix_summary",
                description="Unified Review/Fix convergence outcome, retry budget, and repair application summary.",
            ),
            self._package_content(
                path="ops-alert-events.json",
                content_type="application/json",
                source="ops_alert_event",
                description="Recent Ops alert events and webhook delivery audit records visible at packaging time.",
            ),
        ]
        if generated_project is None:
            contents.append(
                self._package_content(
                    path="generated_project/README.md",
                    content_type="text/markdown; charset=utf-8",
                    source="generated_project",
                    description="Placeholder explaining that no generated project was available.",
                    included=False,
                    reason="No generated_project artifact was available when packaging ran.",
                )
            )
        else:
            contents.append(
                self._package_content(
                    path="generated_project/",
                    content_type="inode/directory",
                    source="generated_project",
                    description="Generated TypeScript-oriented project directory.",
                    artifact_id=generated_project.id,
                    size=generated_project.size,
                )
            )
        for attachment in attachments:
            contents.append(
                self._package_content(
                    path=attachment["packagePath"] or f"evidence/{attachment['kind']}/{attachment['artifactId']}",
                    content_type=str(attachment["contentType"]),
                    source=str(attachment["kind"]),
                    description="Evidence attachment collected into the result package.",
                    artifact_id=str(attachment["artifactId"]),
                    included=bool(attachment["included"]),
                    reason=str(attachment["reason"]),
                    size=int(attachment["size"]),
                )
            )
        return contents

    def _package_content(
        self,
        *,
        path: str,
        content_type: str,
        source: str,
        description: str,
        artifact_id: str | None = None,
        included: bool = True,
        reason: str = "included",
        size: int | None = None,
    ) -> dict[str, Any]:
        return {
            "path": path,
            "contentType": content_type,
            "source": source,
            "artifactId": artifact_id,
            "included": included,
            "reason": reason,
            "size": size,
            "description": description,
        }

    def _report_sections(
        self,
        *,
        attachments: list[dict[str, Any]],
        artifacts: list[ArtifactRecord],
        job_id: str,
        store,
    ) -> list[dict[str, Any]]:
        artifact_ids_by_kind: dict[str, list[str]] = {}
        for artifact in artifacts:
            artifact_ids_by_kind.setdefault(artifact.kind, []).append(artifact.id)
        attachment_ids = [str(item["artifactId"]) for item in attachments if item["included"]]
        build_details = self._build_typecheck_report_details(job_id=job_id, store=store, artifacts=artifacts)
        review_details = self._review_report_details(job_id=job_id, store=store, artifacts=artifacts)
        runtime_compare_details = self._runtime_compare_report_details(job_id=job_id, store=store, artifacts=artifacts)
        review_fix_details = self._review_fix_report_details(job_id=job_id, store=store, artifacts=artifacts)
        ops_alert_details = self._ops_alert_report_details(store)
        risk_details = self._risk_report_details([*build_details, *review_details, *runtime_compare_details, *review_fix_details, *ops_alert_details])
        return [
            self._report_section(
                title="Completion Decision",
                anchor="completion-decision",
                summary="Final completed or best-effort decision and the primary reason.",
                artifact_kinds=("audit_report", "html_report"),
                artifact_ids_by_kind=artifact_ids_by_kind,
            ),
            self._report_section(
                title="Risk And Failure Groups",
                anchor="risk-and-failure-groups",
                summary="Failing or best-effort build, runtime, and review observations.",
                artifact_kinds=("build_artifact", "runtime_validation", "review_run", "runtime_trace"),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=risk_details,
            ),
            self._report_section(
                title="Review/Fix Convergence",
                anchor="review-fix-convergence",
                summary="Unified build, runtime compare, Agent review, repair application, and stop reason summary.",
                artifact_kinds=("build_artifact", "review_run", "repair_instruction", "runtime_trace", "generated_project"),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=review_fix_details,
            ),
            self._report_section(
                title="Build And Typecheck",
                anchor="build-and-typecheck",
                summary="Sandboxed build and typecheck results.",
                artifact_kinds=("build_artifact", "build_log"),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=build_details,
            ),
            self._report_section(
                title="Runtime Evidence",
                anchor="runtime-evidence",
                summary="Browser validation reports, traces, screenshots, and request evidence.",
                artifact_kinds=("runtime_validation", "runtime_trace", "runtime_screenshot"),
                artifact_ids_by_kind=artifact_ids_by_kind,
            ),
            self._report_section(
                title="Runtime Compare Difference Summary",
                anchor="runtime-compare-difference-summary",
                summary="Scenario and viewport comparison differences with evidence deep links.",
                artifact_kinds=("runtime_comparison", "runtime_scenario", "runtime_trace", "runtime_screenshot"),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=runtime_compare_details,
            ),
            self._report_section(
                title="Browser Runner Operations",
                anchor="browser-runner-operations",
                summary="Remote Browser Runner queue length, latency, retry, lease recovery, backend health, and alert evidence.",
                artifact_kinds=("runtime_trace",),
                artifact_ids_by_kind=artifact_ids_by_kind,
            ),
            self._report_section(
                title="Ops Alert Summary",
                anchor="ops-alert-summary",
                summary="Recent configured Ops alert events, service scope, and webhook delivery audit status.",
                artifact_kinds=("ops_alert_event",),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=ops_alert_details,
            ),
            self._report_section(
                title="Agent Runtime Audit",
                anchor="agent-runtime-audit",
                summary="Agent output contracts, Tool Registry entries, memory records, runtime diagnoses, and report sections.",
                artifact_kinds=(
                    "agent_plan",
                    "inference_record",
                    "tool_registry",
                    "memory_record",
                    "runtime_diagnosis",
                    "report_section",
                    "tool_call",
                ),
                artifact_ids_by_kind=artifact_ids_by_kind,
            ),
            self._report_section(
                title="Review Evidence",
                anchor="review-evidence",
                summary="Review, repair gate, and best-effort decision evidence.",
                artifact_kinds=("review_run", "repair_instruction"),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=review_details,
            ),
            self._report_section(
                title="Artifact Manifest",
                anchor="artifact-manifest",
                summary="Packaged artifact manifest and artifact deep links.",
                artifact_kinds=tuple(sorted(artifact_ids_by_kind)),
                artifact_ids_by_kind=artifact_ids_by_kind,
            ),
            self._report_section(
                title="Evidence Attachment Index",
                anchor="evidence-attachment-index",
                summary="Evidence files included in or omitted from the result package.",
                artifact_kinds=(),
                artifact_ids_by_kind=artifact_ids_by_kind,
                evidence_artifact_ids=attachment_ids,
            ),
            self._report_section(
                title="Reproduction",
                anchor="reproduction",
                summary="Offline commands and files needed to inspect the package.",
                artifact_kinds=("result_package", "evidence_index"),
                artifact_ids_by_kind=artifact_ids_by_kind,
            ),
        ]

    def _report_section(
        self,
        *,
        title: str,
        anchor: str,
        summary: str,
        artifact_kinds: tuple[str, ...],
        artifact_ids_by_kind: dict[str, list[str]],
        evidence_artifact_ids: list[str] | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        artifact_ids: list[str] = []
        for kind in artifact_kinds:
            artifact_ids.extend(artifact_ids_by_kind.get(kind, []))
        if evidence_artifact_ids:
            artifact_ids.extend(evidence_artifact_ids)
        return {
            "title": title,
            "anchor": anchor,
            "summary": summary,
            "artifactKinds": list(artifact_kinds),
            "artifactIds": list(dict.fromkeys(artifact_ids)),
            "evidenceLinks": [f"artifact://{artifact_id}" for artifact_id in dict.fromkeys(artifact_ids)],
            "details": details or [],
        }

    def _ops_alert_event_rows(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            delivery = event.get("delivery") if isinstance(event.get("delivery"), dict) else {}
            rows.append(
                {
                    "id": event.get("id"),
                    "checkedAt": event.get("checkedAt"),
                    "severity": event.get("severity"),
                    "code": event.get("code"),
                    "serviceRole": event.get("serviceRole"),
                    "instanceId": event.get("instanceId"),
                    "field": event.get("field"),
                    "value": event.get("value"),
                    "threshold": event.get("threshold"),
                    "deliveryStatus": delivery.get("status"),
                    "deliveryAttempted": delivery.get("attempted"),
                    "deliveryError": delivery.get("error"),
                }
            )
        return rows

    def _ops_alert_report_details(self, store) -> list[dict[str, Any]]:
        events = self._load_ops_alert_events(store)
        details: list[dict[str, Any]] = []
        for row in self._ops_alert_event_rows(events):
            severity = str(row.get("severity") or "warning")
            status = "fail" if severity == "critical" else "best_effort"
            details.append(
                {
                    "label": "Ops alert event",
                    "value": f"{row.get('code') or 'unknown'} / {row.get('serviceRole') or 'global'}",
                    "status": status,
                    "details": {
                        **row,
                        "failureClass": "resource_limit" if status == "fail" else "unknown",
                        "evidenceLinks": [],
                    },
                }
            )
        return details

    def _build_typecheck_report_details(self, *, job_id: str, store, artifacts: list[ArtifactRecord]) -> list[dict[str, Any]]:
        details: list[dict[str, Any]] = []
        for artifact in artifacts:
            if artifact.kind != "build_artifact":
                continue
            payload = self._read_report_payload(job_id=job_id, store=store, artifact=artifact, kind="build_artifact")
            status = self._report_detail_status(payload.get("status"))
            review_type = str(payload.get("reviewType") or payload.get("phase") or artifact.stage)
            attempt = self._record_attempt(payload)
            diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), list) else []
            logs_artifact_id = self._optional_string(payload.get("logsArtifactId"))
            repair_instruction_ids = self._string_list(payload.get("repairInstructionIds"))
            evidence_links = self._artifact_evidence_links(
                artifact.id,
                logs_artifact_id,
                *repair_instruction_ids,
            )
            details.append(
                {
                    "label": "Build validation",
                    "value": f"{review_type} attempt {attempt}",
                    "status": status,
                    "details": {
                        "artifactId": artifact.id,
                        "reviewType": review_type,
                        "phase": payload.get("phase"),
                        "attempt": attempt,
                        "failureClass": str(payload.get("failureClass") or "unknown"),
                        "decision": str(payload.get("decision") or "Build validation did not include a decision."),
                        "command": self._string_list(payload.get("command")),
                        "commandSource": payload.get("commandSource"),
                        "scriptName": payload.get("scriptName"),
                        "packageManager": payload.get("packageManager"),
                        "exitCode": payload.get("exitCode"),
                        "durationMs": payload.get("durationMs"),
                        "diagnosticCount": len(diagnostics),
                        "diagnostics": self._compact_diagnostics(diagnostics),
                        "logsArtifactId": logs_artifact_id,
                        "repairInstructionIds": repair_instruction_ids,
                        "networkPolicy": payload.get("networkPolicy"),
                        "resourcePolicy": self._compact_resource_policy(payload.get("resourcePolicy")),
                        "limitations": self._string_list(payload.get("limitations")),
                        "evidenceLinks": evidence_links,
                    },
                }
            )
        return details

    def _review_report_details(self, *, job_id: str, store, artifacts: list[ArtifactRecord]) -> list[dict[str, Any]]:
        details: list[dict[str, Any]] = []
        for artifact in artifacts:
            if artifact.kind != "review_run":
                continue
            payload = self._read_report_payload(job_id=job_id, store=store, artifact=artifact, kind="review_run")
            status = self._report_detail_status(payload.get("status"))
            review_type = str(payload.get("reviewType") or artifact.stage)
            attempt = self._record_attempt(payload)
            logs_artifact_id = self._optional_string(payload.get("logsArtifactId"))
            repair_instruction_ids = self._string_list(payload.get("repairInstructionIds"))
            evidence_refs = payload.get("evidenceRefs") if isinstance(payload.get("evidenceRefs"), list) else []
            evidence_links = self._artifact_evidence_links(
                artifact.id,
                logs_artifact_id,
                *repair_instruction_ids,
                *self._evidence_ref_artifact_ids(evidence_refs),
            )
            details.append(
                {
                    "label": "Review run",
                    "value": f"{review_type} attempt {attempt}",
                    "status": status,
                    "details": {
                        "artifactId": artifact.id,
                        "reviewType": review_type,
                        "attempt": attempt,
                        "failureClass": str(payload.get("failureClass") or "unknown"),
                        "decision": str(payload.get("decision") or "Review did not include a decision."),
                        "logsArtifactId": logs_artifact_id,
                        "repairInstructionIds": repair_instruction_ids,
                        "evidenceRefs": self._compact_evidence_refs(evidence_refs),
                        "evidenceRefCount": len(evidence_refs),
                        "evidenceLinks": evidence_links,
                    },
                }
            )
        return details

    def _review_fix_report_details(self, *, job_id: str, store, artifacts: list[ArtifactRecord]) -> list[dict[str, Any]]:
        details: list[dict[str, Any]] = []
        for artifact in artifacts:
            if artifact.kind != "runtime_trace":
                continue
            payload = self._read_report_payload(job_id=job_id, store=store, artifact=artifact, kind="runtime_trace")
            if payload.get("target") != "review_fix_convergence_summary":
                continue
            payload.setdefault("artifactId", artifact.id)
            runtime_compare = payload.get("runtimeCompare") if isinstance(payload.get("runtimeCompare"), dict) else {}
            build_typecheck = payload.get("buildTypecheck") if isinstance(payload.get("buildTypecheck"), dict) else {}
            policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
            failure_action_map = payload.get("failureActionMap") if isinstance(payload.get("failureActionMap"), list) else []
            next_steps = self._string_list(payload.get("nextSteps"))
            evidence_ids = self._string_list(payload.get("evidenceArtifactIds"))
            evidence_links = payload.get("evidenceLinks") if isinstance(payload.get("evidenceLinks"), list) else []
            if not evidence_links:
                evidence_links = self._artifact_evidence_links(artifact.id, *evidence_ids)
            details.append(
                {
                    "label": "Review/Fix convergence summary",
                    "value": str(payload.get("finalOutcome") or "best_effort_with_limitations"),
                    "status": self._report_detail_status(payload.get("status")),
                    "details": {
                        "artifactId": artifact.id,
                        "finalOutcome": payload.get("finalOutcome"),
                        "decision": self._review_fix_decision(payload),
                        "failureClass": str(payload.get("failureClass") or "unknown"),
                        "policy": policy,
                        "failureActionMap": failure_action_map,
                        "activeFailureActionMap": [
                            item for item in failure_action_map if isinstance(item, dict) and item.get("status") == "active"
                        ],
                        "nextSteps": next_steps,
                        "buildTypecheck": {
                            "allPassed": build_typecheck.get("allPassed"),
                            "needsAttention": build_typecheck.get("needsAttention"),
                            "repairInstructionCount": build_typecheck.get("repairInstructionCount"),
                            "appliedRepairCount": build_typecheck.get("appliedRepairCount"),
                            "latestStatusByReviewType": build_typecheck.get("latestStatusByReviewType"),
                        },
                        "runtimeCompare": {
                            "maxAttempts": runtime_compare.get("maxAttempts"),
                            "attemptsUsed": runtime_compare.get("attemptsUsed"),
                            "budgetExhausted": runtime_compare.get("budgetExhausted"),
                            "stoppedReason": runtime_compare.get("stoppedReason"),
                            "finalReviewStatus": runtime_compare.get("finalReviewStatus"),
                            "plannedRepairCount": runtime_compare.get("plannedRepairCount"),
                            "appliedRepairCount": runtime_compare.get("appliedRepairCount"),
                            "finalProjectArtifactId": runtime_compare.get("finalProjectArtifactId"),
                        },
                        "agentReview": payload.get("agentReview") if isinstance(payload.get("agentReview"), dict) else {},
                        "evidenceLinks": [str(item) for item in evidence_links],
                    },
                }
            )
            if policy:
                details.append(
                    {
                        "label": "Review/Fix policy",
                        "value": (
                            f"lowRiskAuto={policy.get('allowLowRiskRepairs')} / "
                            f"actions={', '.join(self._string_list(policy.get('allowedRepairActions'))) or 'default'}"
                        ),
                        "status": self._report_detail_status(payload.get("status")),
                        "details": {
                            "artifactId": artifact.id,
                            "failureClass": str(payload.get("failureClass") or "unknown"),
                            "policy": policy,
                            "evidenceLinks": [str(item) for item in evidence_links],
                        },
                    }
                )
            for mapping in failure_action_map:
                if not isinstance(mapping, dict):
                    continue
                status = (
                    "best_effort"
                    if mapping.get("status") == "active" and self._report_detail_status(payload.get("status")) != "pass"
                    else "pass"
                )
                automatic_actions = self._string_list(mapping.get("automaticActions"))
                audit_actions = self._string_list(mapping.get("auditOnlyActions"))
                details.append(
                    {
                        "label": "Review/Fix failure action map",
                        "value": (
                            f"{mapping.get('failureClass') or 'unknown'} -> "
                            f"{', '.join(automatic_actions) or 'audit-only'}"
                        ),
                        "status": status,
                        "details": {
                            "artifactId": artifact.id,
                            "failureClass": str(mapping.get("failureClass") or "unknown"),
                            "targetStage": mapping.get("targetStage"),
                            "automaticActions": automatic_actions,
                            "auditOnlyActions": audit_actions,
                            "mappingStatus": mapping.get("status"),
                            "evidenceLinks": self._artifact_evidence_links(
                                artifact.id,
                                *self._string_list(mapping.get("evidenceArtifactIds")),
                            ),
                        },
                    }
                )
            for index, step in enumerate(next_steps, start=1):
                details.append(
                    {
                        "label": "Review/Fix next step",
                        "value": step,
                        "status": self._report_detail_status(payload.get("status")),
                        "details": {
                            "artifactId": artifact.id,
                            "stepIndex": index,
                            "failureClass": str(payload.get("failureClass") or "unknown"),
                            "nextStep": step,
                            "evidenceLinks": [str(item) for item in evidence_links],
                        },
                    }
                )
        return details

    def _risk_report_details(self, details: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [detail for detail in details if self._report_detail_needs_attention(detail)]

    def _report_detail_needs_attention(self, detail: dict[str, Any]) -> bool:
        status = str(detail.get("status") or "")
        detail_payload = detail.get("details") if isinstance(detail.get("details"), dict) else {}
        failure_class = str(detail_payload.get("failureClass") or "unknown")
        return failure_class != "none" or status in {"best_effort", "fail", "retry"}

    def _read_report_payload(self, *, job_id: str, store, artifact: ArtifactRecord, kind: str) -> dict[str, Any]:
        try:
            payload = json.loads(store.read_artifact(job_id, artifact.id).decode("utf-8"))
        except Exception as error:
            return {
                "artifactId": artifact.id,
                "kind": kind,
                "status": "best_effort",
                "failureClass": "unknown",
                "decision": f"{kind} artifact could not be read: {error}",
            }
        if not isinstance(payload, dict):
            return {
                "artifactId": artifact.id,
                "kind": kind,
                "status": "best_effort",
                "failureClass": "unknown",
                "decision": f"{kind} artifact did not contain a JSON object.",
            }
        payload.setdefault("artifactId", artifact.id)
        return payload

    def _report_detail_status(self, value: Any) -> str:
        status = str(value or "best_effort")
        if status in {"pass", "retry", "best_effort", "fail"}:
            return status
        return "best_effort"

    def _optional_string(self, value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    def _string_list(self, value: Any) -> list[str]:
        return [str(item) for item in value] if isinstance(value, list) else []

    def _artifact_evidence_links(self, *artifact_ids: str | None) -> list[str]:
        return [f"artifact://{artifact_id}" for artifact_id in dict.fromkeys(artifact_ids) if artifact_id]

    def _evidence_ref_artifact_ids(self, evidence_refs: list[Any]) -> list[str]:
        artifact_ids: list[str] = []
        for item in evidence_refs:
            if isinstance(item, dict) and isinstance(item.get("artifactId"), str):
                artifact_ids.append(item["artifactId"])
        return artifact_ids

    def _compact_evidence_refs(self, evidence_refs: list[Any]) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for item in evidence_refs[:8]:
            if not isinstance(item, dict):
                continue
            compact.append(
                {
                    "artifactId": item.get("artifactId"),
                    "label": item.get("label"),
                    "locator": item.get("locator"),
                }
            )
        return compact

    def _compact_diagnostics(self, diagnostics: list[Any]) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for item in diagnostics[:8]:
            if not isinstance(item, dict):
                continue
            compact.append(
                {
                    "source": item.get("source"),
                    "tool": item.get("tool"),
                    "category": item.get("category"),
                    "code": item.get("code"),
                    "message": self._truncate_text(item.get("message"), max_length=180),
                    "filePath": item.get("filePath"),
                    "line": item.get("line"),
                    "column": item.get("column"),
                    "relatedInformationCount": len(item.get("relatedInformation") or []),
                }
            )
        return compact

    def _compact_resource_policy(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        capabilities = value.get("capabilities") if isinstance(value.get("capabilities"), list) else []
        return {
            "enforcement": value.get("enforcement"),
            "runnerKind": value.get("runnerKind"),
            "runtimeName": value.get("runtimeName"),
            "runtimeVersion": value.get("runtimeVersion"),
            "hostPlatform": value.get("hostPlatform"),
            "capabilities": [
                {
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "detail": self._truncate_text(item.get("detail"), max_length=180),
                }
                for item in capabilities[:8]
                if isinstance(item, dict)
            ],
            "limitations": self._string_list(value.get("limitations")),
        }

    def _truncate_text(self, value: Any, *, max_length: int) -> str | None:
        if value is None:
            return None
        text = str(value)
        return text if len(text) <= max_length else f"{text[: max_length - 3]}..."
    def _runtime_compare_report_details(self, *, job_id: str, store, artifacts: list[ArtifactRecord]) -> list[dict[str, Any]]:
        comparison_entries = [artifact for artifact in artifacts if artifact.kind == "runtime_comparison"]
        trace_entries = [artifact for artifact in artifacts if artifact.kind == "runtime_trace"]
        comparison_payloads: list[dict[str, Any]] = []
        trace_payloads: list[dict[str, Any]] = []
        for artifact in comparison_entries:
            payload = self._read_report_payload(job_id=job_id, store=store, artifact=artifact, kind="runtime_comparison")
            if isinstance(payload.get("differences"), dict):
                payload["artifactId"] = artifact.id
                comparison_payloads.append(payload)
        for artifact in trace_entries:
            payload = self._read_report_payload(job_id=job_id, store=store, artifact=artifact, kind="runtime_trace")
            if payload.get("target") in {"runtime_compare_matrix", "runtime_compare_retry_summary"}:
                payload["artifactId"] = artifact.id
                trace_payloads.append(payload)
        if not comparison_payloads and not trace_payloads:
            return []

        matrix_summaries = self._runtime_compare_matrix_summaries(trace_payloads)
        retry_summary = self._runtime_compare_retry_summary(trace_payloads)
        latest_by_scope: dict[str, dict[str, Any]] = {}
        history_by_scope: dict[str, list[dict[str, Any]]] = {}
        for payload in sorted(comparison_payloads, key=self._record_attempt):
            differences = payload.get("differences") or {}
            scope = differences.get("comparisonScope") or {}
            viewport = scope.get("viewport") or {}
            viewport_label = self._viewport_label(viewport)
            scope_label = f"{scope.get('scenarioName') or payload.get('scenarioArtifactId') or 'unknown'} / {viewport_label}"
            key = f"{scope.get('scenarioName') or 'unknown'}::{viewport_label}"
            attempt_history = history_by_scope.setdefault(key, [])
            attempt_history.append(
                {
                    "attempt": self._record_attempt(payload),
                    "status": str(payload.get("status") or "unknown"),
                    "comparisonArtifactId": payload.get("artifactId"),
                    "scenarioArtifactId": payload.get("scenarioArtifactId"),
                    "traceArtifactIds": self._string_list(payload.get("traceArtifactIds")),
                    "screenshotArtifactIds": self._string_list(payload.get("screenshotArtifactIds")),
                    "domChanged": bool(differences.get("domChanged")),
                    "networkChanged": bool(differences.get("networkChanged")),
                    "consoleChanged": bool(differences.get("consoleChanged")),
                    "screenshotChanged": differences.get("screenshotChanged"),
                }
            )
            dom_differences = differences.get("domDifferences") or []
            network_diff = differences.get("networkDiff") or {}
            console_diff = differences.get("consoleDiff") or {}
            latest_by_scope[key] = {
                "label": "Runtime compare scope",
                "value": scope_label,
                "status": self._report_detail_status(payload.get("status")),
                "details": {
                    "attempt": payload.get("attempt"),
                    "comparisonArtifactId": payload.get("artifactId"),
                    "scenarioArtifactId": payload.get("scenarioArtifactId"),
                    "failureClass": "runtime_error" if str(payload.get("status")) in {"fail", "retry", "best_effort"} else "none",
                    "comparisonScope": {
                        "scenarioName": scope.get("scenarioName"),
                        "networkPolicy": scope.get("networkPolicy"),
                        "timeoutMs": scope.get("timeoutMs"),
                        "viewport": viewport,
                    },
                    "domDifferences": [self._dom_diff_summary(item) for item in dom_differences if isinstance(item, dict)],
                    "networkDiff": self._compact_collection_diff(network_diff),
                    "consoleDiff": self._compact_collection_diff(console_diff),
                    "attemptHistory": attempt_history,
                    "evidenceLinks": self._runtime_compare_evidence_link_list(
                        payload,
                        comparison_artifact_id=str(payload.get("artifactId")),
                    ),
                },
            }
        for key, detail in latest_by_scope.items():
            detail["details"]["attemptHistory"] = history_by_scope.get(key, [])

        details = self._runtime_compare_summary_details(
            comparison_payloads=comparison_payloads,
            matrix_summaries=matrix_summaries,
            retry_summary=retry_summary,
        )
        details.extend(latest_by_scope.values())
        return details

    def _runtime_compare_summary_details(
        self,
        *,
        comparison_payloads: list[dict[str, Any]],
        matrix_summaries: list[dict[str, Any]],
        retry_summary: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not comparison_payloads and not matrix_summaries and retry_summary is None:
            return []
        status_counts = self._status_counts(comparison_payloads)
        latest_matrix = matrix_summaries[-1] if matrix_summaries else {}
        selected = latest_matrix.get("selectedRunCount")
        requested = latest_matrix.get("requestedRunCount")
        omitted = latest_matrix.get("omittedRunCount")
        budget_exhausted = bool(retry_summary and retry_summary.get("budgetExhausted"))
        final_review_status = str(retry_summary.get("finalReviewStatus")) if retry_summary else ""
        comparison_needs_attention = any(item.get("status") in {"fail", "retry", "best_effort"} for item in comparison_payloads)
        status = (
            "fail"
            if budget_exhausted or final_review_status == "fail"
            else "best_effort"
            if comparison_needs_attention or final_review_status in {"retry", "best_effort"}
            else "pass"
        )
        evidence_ids = [
            *(str(item.get("artifactId")) for item in matrix_summaries if item.get("artifactId")),
            *(str(item.get("artifactId")) for item in comparison_payloads if item.get("artifactId")),
        ]
        if retry_summary and retry_summary.get("artifactId"):
            evidence_ids.append(str(retry_summary["artifactId"]))
        details = {
            "matrix": {
                "requestedRunCount": requested,
                "selectedRunCount": selected,
                "omittedRunCount": omitted,
                "maxMatrixRuns": latest_matrix.get("maxMatrixRuns"),
                "matrixSelection": latest_matrix.get("matrixSelection"),
                "attemptCount": len(matrix_summaries),
                "attempts": [self._compact_matrix_summary(item) for item in matrix_summaries],
            },
            "retryBudget": self._compact_retry_summary(retry_summary),
            "statusCounts": status_counts,
            "scopeCount": len({self._runtime_compare_scope_key(item) for item in comparison_payloads}),
            "comparisonCount": len(comparison_payloads),
            "failureClass": "runtime_error" if status in {"fail", "best_effort", "retry"} else "none",
            "evidenceLinks": self._artifact_evidence_links(*evidence_ids),
        }
        value = f"matrix {selected if selected is not None else 0}/{requested if requested is not None else 0} selected"
        if retry_summary:
            value += f"; attempts {retry_summary.get('attemptsUsed')}/{retry_summary.get('maxAttempts')}"
        return [
            {
                "label": "Runtime compare matrix summary",
                "value": value,
                "status": status,
                "details": details,
            }
        ]

    def _compact_matrix_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        return {
            "artifactId": summary.get("artifactId"),
            "attempt": self._record_attempt(summary),
            "requestedRunCount": summary.get("requestedRunCount"),
            "selectedRunCount": summary.get("selectedRunCount"),
            "omittedRunCount": summary.get("omittedRunCount"),
            "maxMatrixRuns": summary.get("maxMatrixRuns"),
            "matrixSelection": summary.get("matrixSelection"),
            "pruned": summary.get("pruned"),
        }

    def _compact_retry_summary(self, summary: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(summary, dict):
            return {}
        attempts = summary.get("attempts") if isinstance(summary.get("attempts"), list) else []
        return {
            "artifactId": summary.get("artifactId"),
            "maxAttempts": summary.get("maxAttempts"),
            "attemptsUsed": summary.get("attemptsUsed"),
            "budgetExhausted": summary.get("budgetExhausted"),
            "stoppedReason": summary.get("stoppedReason"),
            "finalProjectArtifactId": summary.get("finalProjectArtifactId"),
            "finalReviewStatus": summary.get("finalReviewStatus"),
            "attempts": [self._compact_retry_attempt(item) for item in attempts if isinstance(item, dict)],
        }

    def _compact_retry_attempt(self, attempt: dict[str, Any]) -> dict[str, Any]:
        return {
            "attempt": attempt.get("attempt"),
            "reviewGateTriggered": attempt.get("reviewGateTriggered"),
            "comparisonArtifactIds": self._string_list(attempt.get("comparisonArtifactIds")),
            "reviewArtifactId": attempt.get("reviewArtifactId"),
            "plannedRepairArtifactId": attempt.get("plannedRepairArtifactId"),
            "repairArtifactId": attempt.get("repairArtifactId"),
            "appliedProjectArtifactId": attempt.get("appliedProjectArtifactId"),
        }

    def _status_counts(self, payloads: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for payload in payloads:
            status = str(payload.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _runtime_compare_scope_key(self, payload: dict[str, Any]) -> str:
        differences = payload.get("differences") if isinstance(payload.get("differences"), dict) else {}
        scope = differences.get("comparisonScope") if isinstance(differences.get("comparisonScope"), dict) else {}
        return f"{scope.get('scenarioName') or 'unknown'}::{self._viewport_label(scope.get('viewport'))}"

    def _viewport_label(self, viewport: Any) -> str:
        if not isinstance(viewport, dict):
            return "default viewport"
        viewport_name = viewport.get("name")
        viewport_size = ""
        if viewport.get("width") and viewport.get("height"):
            viewport_size = f"{viewport['width']}x{viewport['height']}"
        return " ".join(str(part) for part in (viewport_name, viewport_size) if part) or "default viewport"

    def _dom_diff_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "path": item.get("path"),
            "summary": item.get("summary"),
        }

    def _compact_collection_diff(self, diff: Any) -> dict[str, Any]:
        if not isinstance(diff, dict):
            return {}
        return {
            "changed": diff.get("changed"),
            "originalCount": diff.get("originalCount"),
            "reconstructedCount": diff.get("reconstructedCount"),
            "originalOnly": diff.get("originalOnly") or [],
            "reconstructedOnly": diff.get("reconstructedOnly") or [],
            "groups": sorted((diff.get("groups") or {}).keys()),
        }

    def _evidence_attachment(self, artifact: ArtifactRecord, policy: EvidenceAttachmentPolicy, store) -> dict[str, Any]:
        package_path = f"evidence/{artifact.kind}/{artifact.id}{self._artifact_suffix(artifact, store)}"
        included, reason = self._evidence_attachment_decision(artifact, policy, store)
        return {
            "artifactId": artifact.id,
            "kind": artifact.kind,
            "stage": artifact.stage,
            "contentType": artifact.content_type,
            "hash": artifact.hash,
            "size": artifact.size,
            "sensitivityClass": artifact.sensitivity_class,
            "retentionClass": artifact.retention_class,
            "sourceFilename": store.artifact_filename(artifact),
            "packagePath": package_path if included else None,
            "included": included,
            "reason": reason,
        }

    def _evidence_attachment_decision(
        self,
        artifact: ArtifactRecord,
        policy: EvidenceAttachmentPolicy,
        store,
    ) -> tuple[bool, str]:
        if policy.include_kinds is not None and artifact.kind not in policy.include_kinds:
            return False, "Artifact kind is outside configured includeKinds."
        if artifact.kind in policy.exclude_kinds:
            return False, "Artifact kind is excluded by configured excludeKinds."
        if (
            policy.include_sensitivity_classes is not None
            and artifact.sensitivity_class not in policy.include_sensitivity_classes
        ):
            return False, "Artifact sensitivityClass is outside configured includeSensitivityClasses."
        if policy.include_retention_classes is not None and artifact.retention_class not in policy.include_retention_classes:
            return False, "Artifact retentionClass is outside configured includeRetentionClasses."
        if policy.max_bytes_per_artifact is not None and artifact.size > policy.max_bytes_per_artifact:
            return False, "Artifact exceeds configured maxBytesPerArtifact."
        if not store.artifact_exists(artifact) or not store.artifact_is_file(artifact):
            return False, "Artifact content is missing or not a file."
        return True, "included"

    def _evidence_attachment_policy(self, job: JobRecord) -> EvidenceAttachmentPolicy:
        config = job.config if isinstance(job.config, dict) else {}
        packaging = config.get("packaging") if isinstance(config.get("packaging"), dict) else {}
        raw_policy = packaging.get("evidenceAttachments") if isinstance(packaging.get("evidenceAttachments"), dict) else {}
        include_kinds = self._optional_string_set(raw_policy.get("includeKinds")) if "includeKinds" in raw_policy else None
        return EvidenceAttachmentPolicy(
            include_kinds=include_kinds,
            exclude_kinds=self._optional_string_set(raw_policy.get("excludeKinds")) or set(),
            include_sensitivity_classes=self._limited_optional_string_set(
                raw_policy.get("includeSensitivityClasses"),
                SENSITIVITY_CLASSES,
            )
            if "includeSensitivityClasses" in raw_policy
            else None,
            include_retention_classes=self._limited_optional_string_set(
                raw_policy.get("includeRetentionClasses"),
                RETENTION_CLASSES,
            )
            if "includeRetentionClasses" in raw_policy
            else None,
            max_bytes_per_artifact=self._positive_int(raw_policy.get("maxBytesPerArtifact")),
        )

    def _evidence_attachment_policy_payload(self, policy: EvidenceAttachmentPolicy) -> dict[str, Any]:
        return {
            "defaultKinds": sorted(DEFAULT_EVIDENCE_ATTACHMENT_KINDS),
            "includeKinds": sorted(policy.include_kinds) if policy.include_kinds is not None else None,
            "excludeKinds": sorted(policy.exclude_kinds),
            "includeSensitivityClasses": sorted(policy.include_sensitivity_classes)
            if policy.include_sensitivity_classes is not None
            else None,
            "includeRetentionClasses": sorted(policy.include_retention_classes)
            if policy.include_retention_classes is not None
            else None,
            "maxBytesPerArtifact": policy.max_bytes_per_artifact,
        }

    def _optional_string_set(self, value: Any) -> set[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return {value}
        if isinstance(value, list):
            return {item for item in value if isinstance(item, str)}
        return None

    def _limited_optional_string_set(self, value: Any, allowed: set[str]) -> set[str] | None:
        values = self._optional_string_set(value)
        if values is None:
            return None
        return {value for value in values if value in allowed}

    def _positive_int(self, value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        return None

    def _artifact_suffix(self, artifact: ArtifactRecord, store) -> str:
        suffix = store.artifact_suffix(artifact)
        if suffix:
            return suffix
        content_type = artifact.content_type.lower()
        if "json" in content_type:
            return ".json"
        if "png" in content_type:
            return ".png"
        if content_type.startswith("text/"):
            return ".txt"
        return ".bin"

    def _status_table(self, records: list[dict[str, Any]], columns: tuple[str, ...]) -> str:
        if not records:
            return "No records."
        header = "| " + " | ".join(columns) + " |"
        divider = "| " + " | ".join("---" for _ in columns) + " |"
        rows = [header, divider]
        for record in records:
            rows.append("| " + " | ".join(self._cell(record.get(column)) for column in columns) + " |")
        return "\n".join(rows)

    def _cell(self, value: Any) -> str:
        if value is None:
            return ""
        text = str(value).replace("\n", " ").replace("|", "\\|")
        if len(text) > 160:
            text = f"{text[:157]}..."
        return text

    def _package_bytes(
        self,
        *,
        audit_payload: dict[str, Any],
        report_markdown: str,
        report_html: str,
        evidence_index: dict[str, Any],
        generated_project: ArtifactRecord | None,
        artifacts: list[ArtifactRecord],
        store,
    ) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("audit-report.md", report_markdown)
            archive.writestr("audit-report.html", report_html)
            archive.writestr("audit.json", self._json_text(audit_payload))
            archive.writestr("evidence-index.json", self._json_text(evidence_index))
            archive.writestr("artifact-manifest.json", self._json_text(audit_payload["artifactManifest"]))
            archive.writestr("build-artifacts.json", self._json_text(audit_payload["buildArtifacts"]))
            archive.writestr("inference-records.json", self._json_text(audit_payload["inferenceRecords"]))
            archive.writestr("runtime-report.json", self._json_text(audit_payload["runtimeReports"]))
            archive.writestr("runtime-traces.json", self._json_text(audit_payload["runtimeTraces"]))
            archive.writestr("runtime-comparisons.json", self._json_text(audit_payload["runtimeComparisons"]))
            archive.writestr("review-runs.json", self._json_text(audit_payload["reviewRuns"]))
            archive.writestr("tool-calls.json", self._json_text(audit_payload["toolCalls"]))
            archive.writestr("tool-registry.json", self._json_text(audit_payload["toolRegistry"]))
            archive.writestr("memory-records.json", self._json_text(audit_payload["memoryRecords"]))
            archive.writestr("runtime-diagnoses.json", self._json_text(audit_payload["runtimeDiagnoses"]))
            archive.writestr("report-sections.json", self._json_text(audit_payload["reportSections"]))
            archive.writestr("repair-instructions.json", self._json_text(audit_payload["repairInstructions"]))
            archive.writestr("review-fix-summary.json", self._json_text(audit_payload["reviewFixSummary"]))
            archive.writestr("ops-alert-events.json", self._json_text(audit_payload["opsAlertEvents"]))
            self._write_evidence_attachments(archive, artifacts, evidence_index, store)
            if generated_project is None:
                archive.writestr(
                    "generated_project/README.md",
                    "No generated_project artifact was available when packaging ran.\n",
                )
            else:
                self._write_artifact_directory(archive, generated_project, "generated_project", store)
        return buffer.getvalue()

    def _write_evidence_attachments(
        self,
        archive: zipfile.ZipFile,
        artifacts: list[ArtifactRecord],
        evidence_index: dict[str, Any],
        store,
    ) -> None:
        artifacts_by_id = {artifact.id: artifact for artifact in artifacts}
        for attachment in evidence_index["attachments"]:
            if not attachment["included"] or not attachment["packagePath"]:
                continue
            artifact = artifacts_by_id.get(attachment["artifactId"])
            if artifact is None:
                continue
            if store.artifact_exists(artifact) and store.artifact_is_file(artifact):
                local_path = store.artifact_local_path(artifact)
                if local_path is not None:
                    archive.write(local_path, attachment["packagePath"])
                else:
                    archive.writestr(attachment["packagePath"], store.read_artifact_record(artifact))

    def _write_artifact_directory(
        self,
        archive: zipfile.ZipFile,
        artifact: ArtifactRecord,
        root_name: str,
        store,
    ) -> None:
        local_path = store.artifact_local_path(artifact)
        if local_path is not None and local_path.is_dir():
            self._write_directory(archive, local_path, root_name)
            return
        with tempfile.TemporaryDirectory(prefix="ai-jsunpack-package-artifact-") as temp_dir:
            target = Path(temp_dir) / "artifact"
            try:
                materialized = store.materialize_artifact_directory(artifact, target)
            except Exception as error:
                archive.writestr(f"{root_name}/README.md", f"Directory artifact was unavailable: {error}\n")
                return
            self._write_directory(archive, materialized, root_name)

    def _write_directory(self, archive: zipfile.ZipFile, source: Path, root_name: str) -> None:
        if not source.exists() or not source.is_dir():
            archive.writestr(f"{root_name}/README.md", f"Directory artifact was unavailable: {source}\n")
            return
        for file_path in sorted(path for path in source.rglob("*") if path.is_file()):
            archive.write(file_path, f"{root_name}/{file_path.relative_to(source).as_posix()}")

    def _latest_artifact(self, artifacts: list[ArtifactRecord], kind: str) -> ArtifactRecord | None:
        matches = [artifact for artifact in artifacts if artifact.kind == kind]
        return matches[-1] if matches else None

    def _json_text(self, payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
