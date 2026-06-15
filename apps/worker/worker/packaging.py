from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.api.app.models import ArtifactRecord, FailureClass, JobRecord, RunStatus


class PackagingError(RuntimeError):
    pass


@dataclass(frozen=True)
class PackagingResult:
    audit_report_artifact: ArtifactRecord
    result_package_artifact: ArtifactRecord
    final_status: str
    failure_class: FailureClass
    failure_reason: str | None
    message: str

    @property
    def artifact_ids(self) -> list[str]:
        return [self.audit_report_artifact.id, self.result_package_artifact.id]


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
        report_markdown = self._audit_markdown(audit_payload, decision)

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

        package_bytes = self._package_bytes(
            audit_payload=audit_payload,
            report_markdown=report_markdown,
            generated_project=self._latest_artifact(artifacts, "generated_project"),
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
            parent_artifact_ids=[*parents, audit_report_artifact.id],
        )

        return PackagingResult(
            audit_report_artifact=audit_report_artifact,
            result_package_artifact=result_package_artifact,
            final_status=decision["status"],
            failure_class=decision["failureClass"],
            failure_reason=decision["reason"],
            message=f"Packaging produced audit_report and result_package with final status {decision['status']}.",
        )

    def _audit_payload(self, *, job: JobRecord, artifacts: list[ArtifactRecord], store) -> dict[str, Any]:
        return {
            "schemaVersion": "2026-06-14",
            "kind": "audit_report",
            "job": job.model_dump(by_alias=True),
            "artifactManifest": [artifact.model_dump(by_alias=True) for artifact in artifacts],
            "runtimeReports": self._load_json_artifacts(job.id, artifacts, store, "runtime_validation"),
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
                records.append(json.loads(store.read_artifact(job_id, artifact.id).decode("utf-8")))
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

    def _audit_markdown(self, audit_payload: dict[str, Any], decision: dict[str, Any]) -> str:
        job = audit_payload["job"]
        artifact_manifest = audit_payload["artifactManifest"]
        runtime_reports = audit_payload["runtimeReports"]
        review_runs = audit_payload["reviewRuns"]
        inference_records = audit_payload["inferenceRecords"]
        tool_calls = audit_payload["toolCalls"]
        build_artifacts = audit_payload["buildArtifacts"]

        lines = [
            f"# AI JS Unpack Audit Report",
            "",
            f"- Job: `{job['id']}`",
            f"- Final status: `{decision['status']}`",
            f"- Cloud mode: `{job['cloudMode']}`",
            f"- Artifacts included: {len(artifact_manifest)}",
            f"- Runtime validations: {len(runtime_reports)}",
            f"- Review runs: {len(review_runs)}",
            f"- Inference records: {len(inference_records)}",
            f"- Tool calls: {len(tool_calls)}",
            "",
            "## Completion Decision",
            "",
            decision["reason"] or "All collected build, review, and runtime validation evidence passed.",
            "",
            "## Build And Typecheck",
            "",
            self._status_table(build_artifacts, ("reviewType", "status", "failureClass", "decision")),
            "",
            "## Runtime Evidence",
            "",
            self._status_table(runtime_reports, ("target", "status", "entryUrl", "traceArtifactId")),
            "",
            "## Review Evidence",
            "",
            self._status_table(review_runs, ("reviewType", "status", "failureClass", "decision")),
            "",
            "## Artifact Manifest",
            "",
            "| Kind | Stage | Artifact | Producer | Size |",
            "| --- | --- | --- | --- | ---: |",
        ]
        for artifact in artifact_manifest:
            lines.append(
                f"| `{artifact['kind']}` | `{artifact['stage']}` | `{artifact['id']}` | "
                f"`{artifact['producer']}` | {artifact['size']} |"
            )
        lines.extend(
            [
                "",
                "## Reproduction",
                "",
                "Download `result-package.zip`, inspect `generated_project/`, then review the JSON evidence files in this package.",
                "",
            ]
        )
        return "\n".join(lines)

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
        generated_project: ArtifactRecord | None,
        store,
    ) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("audit-report.md", report_markdown)
            archive.writestr("audit.json", self._json_text(audit_payload))
            archive.writestr("artifact-manifest.json", self._json_text(audit_payload["artifactManifest"]))
            archive.writestr("runtime-report.json", self._json_text(audit_payload["runtimeReports"]))
            archive.writestr("review-runs.json", self._json_text(audit_payload["reviewRuns"]))
            if generated_project is None:
                archive.writestr(
                    "generated_project/README.md",
                    "No generated_project artifact was available when packaging ran.\n",
                )
            else:
                self._write_directory(archive, Path(generated_project.storage_uri), "generated_project")
        return buffer.getvalue()

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
