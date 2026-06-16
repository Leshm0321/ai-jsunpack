from __future__ import annotations

import io
import json
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
import zipfile

from apps.api.app.models import ArtifactRecord, FailureClass, JobRecord, RunStatus

EVIDENCE_ATTACHMENT_KINDS = {
    "build_log",
    "runtime_comparison",
    "runtime_scenario",
    "runtime_screenshot",
    "runtime_trace",
}


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
        evidence_index = self._evidence_index(job=job, artifacts=artifacts)
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
            "artifactManifest": [artifact.model_dump(by_alias=True) for artifact in artifacts],
            "runtimeReports": self._load_json_artifacts(job.id, artifacts, store, "runtime_validation"),
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
        for group in ("buildArtifacts", "runtimeReports", "reviewRuns"):
            for record in audit_payload[group]:
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

    def _audit_markdown(
        self,
        audit_payload: dict[str, Any],
        decision: dict[str, Any],
        evidence_index: dict[str, Any],
    ) -> str:
        job = audit_payload["job"]
        artifact_manifest = audit_payload["artifactManifest"]
        runtime_reports = audit_payload["runtimeReports"]
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
        artifact_manifest = audit_payload["artifactManifest"]
        runtime_reports = audit_payload["runtimeReports"]
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
                self._metric_html("Artifacts", str(len(artifact_manifest))),
                self._metric_html("Runtime validations", str(len(runtime_reports))),
                self._metric_html("Runtime comparisons", str(len(runtime_comparisons))),
                self._metric_html("Review runs", str(len(review_runs))),
                self._metric_html("Evidence attachments", str(sum(1 for item in attachments if item["included"]))),
                "</section>",
                "<h2>Completion Decision</h2>",
                f'<div class="notice">{escape(decision_text)}</div>',
                "<h2>Risk And Failure Groups</h2>",
                self._html_table(decision["observations"], ("group", "status", "failureClass", "decision"))
                if decision["observations"]
                else '<div class="notice">No failing or best-effort observations were collected.</div>',
                "<h2>Build And Typecheck</h2>",
                self._html_table(build_artifacts, ("reviewType", "status", "failureClass", "decision")),
                "<h2>Runtime Evidence</h2>",
                self._html_table(runtime_reports, ("target", "status", "entryUrl", "traceArtifactId")),
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

    def _evidence_index(self, *, job: JobRecord, artifacts: list[ArtifactRecord]) -> dict[str, Any]:
        attachments = [self._evidence_attachment(artifact) for artifact in artifacts if artifact.kind in EVIDENCE_ATTACHMENT_KINDS]
        return {
            "schemaVersion": "2026-06-14",
            "kind": "evidence_index",
            "jobId": job.id,
            "attachments": attachments,
            "includedCount": sum(1 for item in attachments if item["included"]),
            "omittedCount": sum(1 for item in attachments if not item["included"]),
        }

    def _evidence_attachment(self, artifact: ArtifactRecord) -> dict[str, Any]:
        source = Path(artifact.storage_uri)
        package_path = f"evidence/{artifact.kind}/{artifact.id}{self._artifact_suffix(artifact)}"
        included = source.exists() and source.is_file()
        reason = "included" if included else "Artifact content is missing or not a file."
        return {
            "artifactId": artifact.id,
            "kind": artifact.kind,
            "stage": artifact.stage,
            "contentType": artifact.content_type,
            "hash": artifact.hash,
            "size": artifact.size,
            "sourceFilename": source.name,
            "packagePath": package_path if included else None,
            "included": included,
            "reason": reason,
        }

    def _artifact_suffix(self, artifact: ArtifactRecord) -> str:
        suffix = Path(artifact.storage_uri).suffix
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
            archive.writestr("runtime-comparisons.json", self._json_text(audit_payload["runtimeComparisons"]))
            archive.writestr("review-runs.json", self._json_text(audit_payload["reviewRuns"]))
            archive.writestr("tool-calls.json", self._json_text(audit_payload["toolCalls"]))
            archive.writestr("repair-instructions.json", self._json_text(audit_payload["repairInstructions"]))
            self._write_evidence_attachments(archive, artifacts, evidence_index)
            if generated_project is None:
                archive.writestr(
                    "generated_project/README.md",
                    "No generated_project artifact was available when packaging ran.\n",
                )
            else:
                self._write_directory(archive, Path(generated_project.storage_uri), "generated_project")
        return buffer.getvalue()

    def _write_evidence_attachments(
        self,
        archive: zipfile.ZipFile,
        artifacts: list[ArtifactRecord],
        evidence_index: dict[str, Any],
    ) -> None:
        artifacts_by_id = {artifact.id: artifact for artifact in artifacts}
        for attachment in evidence_index["attachments"]:
            if not attachment["included"] or not attachment["packagePath"]:
                continue
            artifact = artifacts_by_id.get(attachment["artifactId"])
            if artifact is None:
                continue
            source = Path(artifact.storage_uri)
            if source.exists() and source.is_file():
                archive.write(source, attachment["packagePath"])

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
