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
    """Builds the final downloadable package and human-readable audit report."""

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
        return {
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
            "buildArtifacts": self._load_json_artifacts(job.id, artifacts, store, "build_artifact"),
            "repairInstructions": self._load_json_artifacts(job.id, artifacts, store, "repair_instruction"),
        }

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
        build_artifacts = audit_payload["buildArtifacts"]
        attachments = evidence_index["attachments"]

        lines = [
            f"# AI JS Unpack Audit Report",
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
            "",
            "## Completion Decision",
            "",
            decision["reason"] or "All collected build, review, and runtime validation evidence passed.",
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
            "## Runtime Compare",
            "",
            self._status_table(runtime_comparisons, ("status", "scenarioArtifactId", "screenshotArtifactIds", "traceArtifactIds")),
            "",
            "## Runtime Compare Difference Summary",
            "",
            self._runtime_compare_diff_markdown(runtime_comparisons),
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
                self._metric_html("Evidence attachments", str(sum(1 for item in attachments if item["included"]))),
                "</section>",
                "<h2>Completion Decision</h2>",
                f'<div class="notice">{escape(decision_text)}</div>',
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
                "<h2>Runtime Compare</h2>",
                self._html_table(runtime_comparisons, ("status", "scenarioArtifactId", "screenshotArtifactIds", "traceArtifactIds")),
                "<h2>Runtime Compare Difference Summary</h2>",
                self._runtime_compare_diff_html(runtime_comparisons),
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

    def _html_cell(self, value: Any) -> str:
        if value is None:
            return ""
        text = str(value)
        if len(text) > 240:
            text = f"{text[:237]}..."
        return escape(text)

    def _runtime_compare_diff_markdown(self, records: list[dict[str, Any]]) -> str:
        if not records:
            return "No runtime comparison difference records."
        rows = [
            "| Comparison | Status | Scope | Screenshot | DOM | Network | Console | Evidence |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for record in records:
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
        if not records:
            return '<div class="notice">No runtime comparison difference records.</div>'
        rows = [
            "<tr><th>Comparison</th><th>Status</th><th>Scope</th><th>Screenshot</th><th>DOM</th><th>Network</th><th>Console</th><th>Evidence</th></tr>"
        ]
        for record in records:
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
        return f"<table>{''.join(rows)}</table>"

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
        artifact_ids = [
            record.get("artifactId"),
            record.get("scenarioArtifactId"),
            *(record.get("traceArtifactIds") or []),
            *(record.get("screenshotArtifactIds") or []),
        ]
        links = [f"artifact://{artifact_id}" for artifact_id in artifact_ids if artifact_id]
        return ", ".join(links) if links else "none"

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
        report_sections = self._report_sections(attachments=attachments, artifacts=artifacts)
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
                path="repair-instructions.json",
                content_type="application/json",
                source="repair_instruction",
                description="Structured repair instructions and applied repair evidence.",
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
    ) -> list[dict[str, Any]]:
        artifact_ids_by_kind: dict[str, list[str]] = {}
        for artifact in artifacts:
            artifact_ids_by_kind.setdefault(artifact.kind, []).append(artifact.id)
        attachment_ids = [str(item["artifactId"]) for item in attachments if item["included"]]
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
                artifact_kinds=("build_artifact", "runtime_validation", "review_run"),
                artifact_ids_by_kind=artifact_ids_by_kind,
            ),
            self._report_section(
                title="Build And Typecheck",
                anchor="build-and-typecheck",
                summary="Sandboxed build and typecheck results.",
                artifact_kinds=("build_artifact", "build_log"),
                artifact_ids_by_kind=artifact_ids_by_kind,
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
            ),
            self._report_section(
                title="Browser Runner Operations",
                anchor="browser-runner-operations",
                summary="Remote Browser Runner queue length, latency, retry, lease recovery, backend health, and alert evidence.",
                artifact_kinds=("runtime_trace",),
                artifact_ids_by_kind=artifact_ids_by_kind,
            ),
            self._report_section(
                title="Review Evidence",
                anchor="review-evidence",
                summary="Review, repair gate, and best-effort decision evidence.",
                artifact_kinds=("review_run", "repair_instruction"),
                artifact_ids_by_kind=artifact_ids_by_kind,
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
            archive.writestr("repair-instructions.json", self._json_text(audit_payload["repairInstructions"]))
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
