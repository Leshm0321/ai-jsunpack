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

REPORT_COLUMN_LABELS = {
    "agentName": "Agent 名称",
    "alerts": "告警",
    "area": "检查领域",
    "artifactExchange": "Artifact 交换",
    "artifactId": "Artifact ID",
    "auth": "认证",
    "averageRunDurationMs": "平均运行时长（毫秒）",
    "backendHealthStatus": "后端健康状态",
    "caller": "调用方",
    "category": "分类",
    "checkedAt": "检查时间",
    "claimLatencyMs": "领取延迟（毫秒）",
    "cloudContextAllowed": "是否允许云端上下文",
    "cloudMode": "云端模式",
    "code": "代码",
    "confidence": "置信度",
    "content": "内容",
    "contextHandling": "上下文处理方式",
    "count": "数量",
    "decision": "判定",
    "deliveryStatus": "投递状态",
    "diagnosis": "诊断",
    "enforcement": "实施状态",
    "entryUrl": "入口 URL",
    "expiredRunningCount": "已过期运行数",
    "failureClass": "失败类别",
    "field": "字段",
    "group": "分组",
    "id": "ID",
    "importer": "导入方",
    "included": "是否包含",
    "instanceId": "实例 ID",
    "kind": "类型",
    "leaseRecoveryCount": "租约恢复次数",
    "limitation": "限制",
    "memoryType": "记忆类型",
    "message": "消息",
    "modelContextScope": "模型上下文范围",
    "name": "名称",
    "oldestQueuedAgeMs": "最早排队时长（毫秒）",
    "packagePath": "包内路径",
    "placeholderExports": "占位导出",
    "producer": "生成方",
    "queueBackend": "队列后端",
    "queueLength": "队列长度",
    "reason": "原因",
    "remoteRunId": "远程运行 ID",
    "resolvedPath": "解析路径",
    "retentionClass": "保留级别",
    "retryRate": "重试率",
    "reviewType": "检查类型",
    "runnerKind": "运行器类型",
    "runningCount": "运行中数量",
    "runtimeContract": "运行时约定",
    "scenarioArtifactId": "Scenario Artifact ID",
    "scope": "范围",
    "sensitivityClass": "敏感级别",
    "serviceRole": "服务角色",
    "severity": "严重级别",
    "size": "大小",
    "specifier": "引用路径",
    "stage": "阶段",
    "status": "状态",
    "summary": "摘要",
    "target": "目标",
    "targetStage": "目标阶段",
    "terminalCount": "已结束数量",
    "threshold": "阈值",
    "title": "标题",
    "toolName": "工具名称",
    "toolVersion": "工具版本",
    "totalCount": "总数",
    "traceArtifactId": "Trace Artifact ID",
    "traceArtifactIds": "Trace Artifact ID 列表",
    "screenshotArtifactIds": "Screenshot Artifact ID 列表",
    "value": "值",
}


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
            raise PackagingError(f"打包时未找到任务：{job_id}")

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
                "打包已生成 audit_report、html_report、evidence_index 和 result_package，"
                f"最终状态为 {decision['status']}。"
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
            "agentExecutions": self._load_json_artifacts(job.id, artifacts, store, "agent_execution"),
            "inferenceRecords": self._load_json_artifacts(job.id, artifacts, store, "inference_record"),
            "toolCalls": self._load_json_artifacts(job.id, artifacts, store, "tool_call"),
            "toolRegistry": self._load_tool_registry(job.id, artifacts, store),
            "memoryRecords": self._load_json_artifacts(job.id, artifacts, store, "memory_record"),
            "runtimeDiagnoses": self._load_json_artifacts(job.id, artifacts, store, "runtime_diagnosis"),
            "reportSections": self._load_json_artifacts(job.id, artifacts, store, "report_section"),
            "buildArtifacts": self._load_json_artifacts(job.id, artifacts, store, "build_artifact"),
            "repairInstructions": self._load_json_artifacts(job.id, artifacts, store, "repair_instruction"),
            "reconstructionPlans": self._load_json_artifacts(job.id, artifacts, store, "reconstruction_plan"),
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
                if group == "reviewRuns" and self._runtime_uncertainty_review_is_superseded(
                    record=record,
                    decision_records=decision_records,
                ):
                    continue
                if status in {"fail", "retry", "best_effort"}:
                    observations.append(
                        {
                            "group": group,
                            "status": str(status),
                            "failureClass": str(record.get("failureClass") or "unknown"),
                            "decision": str(record.get("decision") or record.get("entryUrl") or "验证未完全通过。"),
                        }
                    )

        if not observations:
            return {"status": "completed", "failureClass": "none", "reason": None, "observations": []}

        first_non_none = next(
            (item["failureClass"] for item in observations if item["failureClass"] != "none"),
            "unknown",
        )
        reason = "；".join(
            f"{item['group']} 状态为 {item['status']}：{item['decision']}" for item in observations[:3]
        )
        return {
            "status": "completed_best_effort",
            "failureClass": first_non_none,
            "reason": reason,
            "observations": observations,
        }

    def _runtime_uncertainty_review_is_superseded(
        self,
        *,
        record: dict[str, Any],
        decision_records: dict[str, list[dict[str, Any]]],
    ) -> bool:
        if record.get("reviewType") != "agent_review" or record.get("status") != "best_effort":
            return False
        if str(record.get("failureClass") or "unknown") not in {"none", "unknown"}:
            return False
        decision = str(record.get("decision") or "").lower()
        runtime_uncertainty = (
            "runtime evidence" in decision
            and any(
                phrase in decision
                for phrase in ("inconclusive", "without current runtime", "no current runtime", "runtime comparison evidence")
            )
        ) or (
            "运行时证据" in decision
            and any(phrase in decision for phrase in ("无法确认", "缺少当前运行时", "没有当前运行时", "运行时对比证据"))
        )
        if not runtime_uncertainty:
            return False
        runtime_reviews = [
            item
            for item in decision_records.get("reviewRuns", [])
            if item.get("reviewType") == "runtime_compare"
        ]
        runtime_reports = decision_records.get("runtimeReports", [])
        return (
            bool(runtime_reviews)
            and all(item.get("status") == "pass" for item in runtime_reviews)
            and bool(runtime_reports)
            and all(item.get("status") == "pass" for item in runtime_reports)
        )

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
        agent_executions = audit_payload["agentExecutions"]
        inference_records = audit_payload["inferenceRecords"]
        tool_calls = audit_payload["toolCalls"]
        tool_registry = audit_payload["toolRegistry"]
        memory_records = audit_payload["memoryRecords"]
        runtime_diagnoses = audit_payload["runtimeDiagnoses"]
        report_sections = audit_payload["reportSections"]
        build_artifacts = audit_payload["buildArtifacts"]
        dependency_placeholders = self._dependency_placeholder_rows(audit_payload["reconstructionPlans"])
        review_fix_summary = audit_payload["reviewFixSummary"]
        ops_alert_events = self._ops_alert_event_rows(audit_payload["opsAlertEvents"])
        attachments = evidence_index["attachments"]

        lines = [
            "# AI JS Unpack 审计报告",
            "",
            f"- 任务：`{job['id']}`",
            f"- 最终状态：`{decision['status']}`",
            f"- 云端模式：`{job['cloudMode']}`",
            f"- 访问边界：所有者 `{policy_summary['accessBoundary']['ownerId']}` / 项目 `{policy_summary['accessBoundary']['projectId']}`",
            f"- 已纳入的 Artifact：{len(artifact_manifest)}",
            f"- 运行时验证：{len(runtime_reports)}",
            f"- 运行时对比：{len(runtime_comparisons)}",
            f"- 审查运行：{len(review_runs)}",
            f"- Agent 执行：{len(agent_executions)}",
            f"- 推理记录：{len(inference_records)}",
            f"- 工具调用：{len(tool_calls)}",
            f"- 工具注册表条目：{len(tool_registry)}",
            f"- 记忆记录：{len(memory_records)}",
            "",
            "## 完成判定",
            "",
            decision["reason"] or "收集到的构建、审查和运行时验证证据均已通过。",
            "",
            "## Review/Fix 收敛情况",
            "",
            self._review_fix_markdown(review_fix_summary),
            "",
            "## 策略摘要",
            "",
            self._policy_summary_markdown(policy_summary),
            "",
            "## 风险与失败分组",
            "",
            self._status_table(decision["observations"], ("group", "status", "failureClass", "decision"))
            if decision["observations"]
            else "未收集到失败或 best_effort 观察项。",
            "",
            "## 依赖占位摘要",
            "",
            (
                "此处列出的缺失依赖仅提供加载连续性，未恢复其真实语义行为；调用生成的命名导出或默认导出时，"
                "会抛出代码为 `AI_JSUNPACK_MISSING_DEPENDENCY` 的 `MissingDependencyPlaceholderError`。"
            ),
            "",
            self._status_table(
                dependency_placeholders,
                ("status", "importer", "specifier", "resolvedPath", "placeholderExports", "runtimeContract", "limitation"),
            )
            if dependency_placeholders
            else "没有缺失的静态相对 ESM 依赖需要占位文件。",
            "",
            "## 构建与类型检查",
            "",
            self._status_table(build_artifacts, ("reviewType", "status", "failureClass", "decision")),
            "",
            "## 运行时证据",
            "",
            self._status_table(runtime_reports, ("target", "status", "entryUrl", "traceArtifactId")),
            "",
            "## Browser Runner 执行边界",
            "",
            self._status_table(runtime_boundaries, ("stage", "runnerKind", "enforcement", "remoteRunId", "auth", "artifactExchange")),
            "",
            "## Browser Runner 运行情况",
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
            "## 运维告警摘要",
            "",
            self._status_table(
                ops_alert_events,
                ("checkedAt", "severity", "code", "serviceRole", "instanceId", "field", "value", "threshold", "deliveryStatus"),
            ),
            "",
            "## 运行时对比",
            "",
            self._status_table(runtime_comparisons, ("status", "scenarioArtifactId", "screenshotArtifactIds", "traceArtifactIds")),
            "",
            "## 运行时对比差异摘要",
            "",
            self._runtime_compare_diff_markdown([*runtime_comparisons, *audit_payload["runtimeTraces"]]),
            "",
            "## Agent 运行时审计",
            "",
            f"- 工具注册表条目：{len(tool_registry)}",
            f"- 记忆记录：{self._memory_record_summary(memory_records)}",
            f"- Agent 执行：{self._agent_execution_summary(agent_executions)}",
            f"- 运行时诊断：{len(runtime_diagnoses)}",
            f"- 报告章节：{len(report_sections)}",
            "",
            self._status_table(agent_executions, ("stage", "name", "status", "failureClass", "message")),
            "",
            self._status_table(runtime_diagnoses, ("agentName", "targetStage", "status", "failureClass", "diagnosis")),
            "",
            "## 审查证据",
            "",
            self._status_table(review_runs, ("reviewType", "status", "failureClass", "decision")),
            "",
            "## Artifact Manifest",
            "",
            "| 类型 | 阶段 | Artifact | 生成方 | 大小 | deep link |",
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
                "## 证据附件索引",
                "",
                self._status_table(
                    attachments,
                    ("kind", "artifactId", "included", "packagePath", "reason"),
                ),
                "",
                "## 复现方法",
                "",
                "```bash",
                "unzip result-package.zip -d ai-jsunpack-result",
                "open ai-jsunpack-result/audit-report.html",
                "cat ai-jsunpack-result/evidence-index.json",
                "```",
                "",
                "如果 shell 环境不提供 `open` 命令，请在浏览器中打开 `audit-report.html`，或直接查看 `audit-report.md`。",
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
        agent_executions = audit_payload["agentExecutions"]
        build_artifacts = audit_payload["buildArtifacts"]
        dependency_placeholders = self._dependency_placeholder_rows(audit_payload["reconstructionPlans"])
        tool_registry = audit_payload["toolRegistry"]
        memory_records = audit_payload["memoryRecords"]
        runtime_diagnoses = audit_payload["runtimeDiagnoses"]
        report_sections = audit_payload["reportSections"]
        review_fix_summary = audit_payload["reviewFixSummary"]
        ops_alert_events = self._ops_alert_event_rows(audit_payload["opsAlertEvents"])
        attachments = evidence_index["attachments"]
        decision_text = decision["reason"] or "收集到的构建、审查和运行时验证证据均已通过。"

        return "\n".join(
            [
                "<!doctype html>",
                '<html lang="zh-CN">',
                "<head>",
                '<meta charset="utf-8">',
                '<meta name="viewport" content="width=device-width, initial-scale=1">',
                "<title>AI JS Unpack 审计报告</title>",
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
                "<h1>AI JS Unpack 审计报告</h1>",
                f"<p>任务 <code>{escape(str(job['id']))}</code> 的离线报告。</p>",
                '<section class="summary" aria-label="报告摘要">',
                self._metric_html("最终状态", str(decision["status"]), f"status-{decision['status']}"),
                self._metric_html("云端模式", str(job["cloudMode"])),
                self._metric_html("所有者", str(policy_summary["accessBoundary"]["ownerId"])),
                self._metric_html("项目", str(policy_summary["accessBoundary"]["projectId"])),
                self._metric_html("Artifact 数量", str(len(artifact_manifest))),
                self._metric_html("运行时验证", str(len(runtime_reports))),
                self._metric_html("运行时对比", str(len(runtime_comparisons))),
                self._metric_html("审查运行", str(len(review_runs))),
                self._metric_html("Agent 执行", str(len(agent_executions))),
                self._metric_html("工具注册表", str(len(tool_registry))),
                self._metric_html("记忆记录", str(len(memory_records))),
                self._metric_html("证据附件", str(sum(1 for item in attachments if item["included"]))),
                self._metric_html("依赖占位项", str(len(dependency_placeholders))),
                "</section>",
                "<h2>完成判定</h2>",
                f'<div class="notice">{escape(decision_text)}</div>',
                "<h2>Review/Fix 收敛情况</h2>",
                self._review_fix_html(review_fix_summary),
                "<h2>策略摘要</h2>",
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
                "<h2>风险与失败分组</h2>",
                self._html_table(decision["observations"], ("group", "status", "failureClass", "decision"))
                if decision["observations"]
                else '<div class="notice">未收集到失败或 best_effort 观察项。</div>',
                "<h2>依赖占位摘要</h2>",
                (
                    '<div class="notice">此处列出的缺失依赖仅提供加载连续性，未恢复其真实语义行为；调用生成的命名导出或默认导出时，'
                    '会抛出代码为 <code>AI_JSUNPACK_MISSING_DEPENDENCY</code> 的 '
                    '<code>MissingDependencyPlaceholderError</code>。</div>'
                ),
                self._html_table(
                    dependency_placeholders,
                    ("status", "importer", "specifier", "resolvedPath", "placeholderExports", "runtimeContract", "limitation"),
                ),
                "<h2>构建与类型检查</h2>",
                self._html_table(build_artifacts, ("reviewType", "status", "failureClass", "decision")),
                "<h2>运行时证据</h2>",
                self._html_table(runtime_reports, ("target", "status", "entryUrl", "traceArtifactId")),
                "<h2>Browser Runner 执行边界</h2>",
                self._html_table(runtime_boundaries, ("stage", "runnerKind", "enforcement", "remoteRunId", "auth", "artifactExchange")),
                "<h2>Browser Runner 运行情况</h2>",
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
                "<h2>运维告警摘要</h2>",
                self._html_table(
                    ops_alert_events,
                    ("checkedAt", "severity", "code", "serviceRole", "instanceId", "field", "value", "threshold", "deliveryStatus"),
                ),
                "<h2>运行时对比</h2>",
                self._html_table(runtime_comparisons, ("status", "scenarioArtifactId", "screenshotArtifactIds", "traceArtifactIds")),
                "<h2>运行时对比差异摘要</h2>",
                self._runtime_compare_diff_html([*runtime_comparisons, *audit_payload["runtimeTraces"]]),
                "<h2>Agent 运行时审计</h2>",
                self._html_table(tool_registry, ("toolName", "toolVersion", "category", "caller")),
                self._html_table(memory_records, ("memoryType", "scope", "sensitivityClass", "retentionClass", "content")),
                self._html_table(agent_executions, ("stage", "name", "status", "failureClass", "message")),
                self._html_table(runtime_diagnoses, ("agentName", "targetStage", "status", "failureClass", "diagnosis")),
                self._html_table(report_sections, ("title", "status", "confidence", "summary")),
                "<h2>审查证据</h2>",
                self._html_table(review_runs, ("reviewType", "status", "failureClass", "decision")),
                "<h2>证据附件索引</h2>",
                self._html_table(attachments, ("kind", "artifactId", "included", "packagePath", "reason")),
                "<h2>Artifact Manifest</h2>",
                self._html_table(artifact_manifest, ("kind", "stage", "id", "producer", "size")),
                "<h2>复现方法</h2>",
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
            return '<div class="notice">无记录。</div>'
        header = "".join(f"<th>{escape(self._report_column_label(column))}</th>" for column in columns)
        rows = [f"<tr>{header}</tr>"]
        for record in records:
            cells = "".join(f"<td>{self._html_cell(record.get(column))}</td>" for column in columns)
            rows.append(f"<tr>{cells}</tr>")
        return f"<table>{''.join(rows)}</table>"

    def _policy_summary_markdown(self, policy_summary: dict[str, Any]) -> str:
        model_policy = policy_summary["modelPolicy"]
        return "\n".join(
            [
                "| 项目 | 值 |",
                "| --- | --- |",
                f"| 所有者 | `{policy_summary['accessBoundary']['ownerId']}` |",
                f"| 项目 | `{policy_summary['accessBoundary']['projectId']}` |",
                f"| 云端模式 | `{model_policy['cloudMode']}` |",
                f"| 模型上下文范围 | `{model_policy['modelContextScope']}` |",
                f"| 模型上下文处理方式 | `{model_policy['contextHandling']}` |",
                f"| 是否允许云端上下文 | `{model_policy['cloudContextAllowed']}` |",
                f"| 策略限制 | {model_policy['limitation']} |",
                f"| 敏感级别计数 | `{self._count_summary(policy_summary['sensitivityCounts'])}` |",
                f"| 保留级别计数 | `{self._count_summary(policy_summary['retentionCounts'])}` |",
            ]
        )

    def _policy_summary_rows(self, counts: dict[str, int], key: str) -> list[dict[str, Any]]:
        return [{key: name, "count": count} for name, count in sorted(counts.items())]

    def _count_summary(self, counts: dict[str, int]) -> str:
        return ", ".join(f"{name}={count}" for name, count in sorted(counts.items())) or "无"

    def _dependency_placeholder_rows(self, plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for plan_payload in plans:
            plan = plan_payload.get("plan") if isinstance(plan_payload.get("plan"), dict) else plan_payload
            records = plan.get("dependencyPlaceholders")
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                exports = self._string_list(record.get("importedNames"))
                if record.get("defaultImport") is True and "default" not in exports:
                    exports.insert(0, "default")
                if record.get("namespaceImport") is True:
                    exports.append("namespace（仅加载）")
                if record.get("exportAll") is True:
                    exports.append("export-all（仅加载）")
                status = self._optional_string(record.get("status")) or "unsupported"
                rows.append(
                    {
                        "status": status,
                        "importer": self._optional_string(record.get("importerPath")) or "unknown",
                        "specifier": self._optional_string(record.get("specifier")) or "unknown",
                        "resolvedPath": self._optional_string(record.get("resolvedPath")) or "not-written",
                        "placeholderExports": ", ".join(dict.fromkeys(exports)) or "仅副作用/仅加载",
                        "runtimeContract": (
                            "加载时发出警告；调用生成的导出时抛出 AI_JSUNPACK_MISSING_DEPENDENCY"
                            if status == "generated"
                            else "仅记录报告，不写入文件"
                        ),
                        "semanticBehaviorAvailable": False,
                        "limitation": self._optional_string(record.get("limitation")) or "无法提供语义行为。",
                    }
                )
        return rows

    def _memory_record_summary(self, records: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for record in records:
            memory_type = str(record.get("memoryType") or "unknown")
            counts[memory_type] = counts.get(memory_type, 0) + 1
        return self._count_summary(counts)

    def _agent_execution_summary(self, records: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for record in records:
            stage = str(record.get("stage") or "unknown")
            counts[stage] = counts.get(stage, 0) + 1
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
                "decision": "打包时没有可用的 Review/Fix 收敛摘要证据。",
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
            return "Review/Fix 流程已应用确定性修复证据，最终验证通过。"
        if final_outcome == "passed_without_repair":
            return "构建、类型检查和运行时对比均已通过，无需确定性修复。"
        if final_outcome == "budget_exhausted_best_effort":
            return (
                "Review/Fix 流程在使用 "
                f"{runtime_compare.get('attemptsUsed')}/{runtime_compare.get('maxAttempts')} 次运行时对比尝试后停止。"
            )
        if final_outcome == "no_deterministic_repair":
            return "Review/Fix 流程未找到适用于剩余失败的低风险确定性修复。"
        if build_typecheck.get("needsAttention") or runtime_compare.get("finalReviewStatus") in {"fail", "best_effort", "retry"}:
            return "Review/Fix 流程已完成，但仍存在 best_effort 限制。"
        return str(summary.get("decision") or "Review/Fix 流程已完成收敛。")

    def _review_fix_markdown(self, summary: dict[str, Any]) -> str:
        runtime_compare = summary.get("runtimeCompare") if isinstance(summary.get("runtimeCompare"), dict) else {}
        build_typecheck = summary.get("buildTypecheck") if isinstance(summary.get("buildTypecheck"), dict) else {}
        next_steps = self._string_list(summary.get("nextSteps"))
        policy = summary.get("policy") if isinstance(summary.get("policy"), dict) else {}
        failure_action_map = summary.get("failureActionMap") if isinstance(summary.get("failureActionMap"), list) else []
        rows = [
            {"area": "最终结果", "value": summary.get("finalOutcome")},
            {"area": "状态", "value": summary.get("status")},
            {"area": "判定", "value": self._review_fix_decision(summary)},
            {
                "area": "策略",
                "value": (
                    f"低风险自动修复={policy.get('allowLowRiskRepairs')}；"
                    f"动作={', '.join(self._string_list(policy.get('allowedRepairActions'))) or '默认'}"
                ),
            },
            {
                "area": "构建/类型检查修复",
                "value": (
                    f"指令={build_typecheck.get('repairInstructionCount')}；"
                    f"已应用={build_typecheck.get('appliedRepairCount')}；"
                    f"全部通过={build_typecheck.get('allPassed')}"
                ),
            },
            {
                "area": "运行时对比预算",
                "value": (
                    f"attempts={runtime_compare.get('attemptsUsed')}/{runtime_compare.get('maxAttempts')}; "
                    f"stoppedReason={runtime_compare.get('stoppedReason')}; "
                    f"finalReviewStatus={runtime_compare.get('finalReviewStatus')}"
                ),
            },
            {
                "area": "运行时修复",
                "value": (
                    f"已规划={runtime_compare.get('plannedRepairCount')}；"
                    f"已应用={runtime_compare.get('appliedRepairCount')}；"
                    f"最终项目={runtime_compare.get('finalProjectArtifactId')}"
                ),
            },
            {
                "area": "失败/动作映射",
                "value": self._failure_action_map_summary(failure_action_map),
            },
            {
                "area": "后续步骤",
                "value": " | ".join(next_steps) if next_steps else "无需后续操作。",
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
            {"area": "最终结果", "value": summary.get("finalOutcome")},
            {"area": "状态", "value": summary.get("status")},
            {"area": "判定", "value": self._review_fix_decision(summary)},
            {
                "area": "策略",
                "value": (
                    f"低风险自动修复={policy.get('allowLowRiskRepairs')}；"
                    f"动作={', '.join(self._string_list(policy.get('allowedRepairActions'))) or '默认'}"
                ),
            },
            {
                "area": "构建/类型检查修复",
                "value": (
                    f"指令={build_typecheck.get('repairInstructionCount')}；"
                    f"已应用={build_typecheck.get('appliedRepairCount')}；"
                    f"全部通过={build_typecheck.get('allPassed')}"
                ),
            },
            {
                "area": "运行时对比预算",
                "value": (
                    f"attempts={runtime_compare.get('attemptsUsed')}/{runtime_compare.get('maxAttempts')}; "
                    f"stoppedReason={runtime_compare.get('stoppedReason')}; "
                    f"finalReviewStatus={runtime_compare.get('finalReviewStatus')}"
                ),
            },
            {
                "area": "运行时修复",
                "value": (
                    f"已规划={runtime_compare.get('plannedRepairCount')}；"
                    f"已应用={runtime_compare.get('appliedRepairCount')}；"
                    f"最终项目={runtime_compare.get('finalProjectArtifactId')}"
                ),
            },
            {
                "area": "失败/动作映射",
                "value": self._failure_action_map_summary(failure_action_map),
            },
            {
                "area": "后续步骤",
                "value": " | ".join(next_steps) if next_steps else "无需后续操作。",
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
                f"{record.get('failureClass') or 'unknown'}->{', '.join(automatic_actions) or '仅审计'}"
            )
        return "；".join(parts) or "无"

    def _runtime_compare_diff_markdown(self, records: list[dict[str, Any]]) -> str:
        comparison_records = self._runtime_compare_comparison_records(records)
        matrix_summaries = self._runtime_compare_matrix_summaries(records)
        retry_summary = self._runtime_compare_retry_summary(records)
        if not comparison_records and not matrix_summaries and not retry_summary:
            return "没有运行时对比差异记录。"
        rows = []
        if matrix_summaries or retry_summary:
            rows.extend(
                [
                    "| 摘要 | 值 |",
                    "| --- | --- |",
                    *[
                        "| 矩阵尝试 "
                        + self._cell(summary.get("attempt"))
                        + " | "
                        + self._cell(
                            f"已选择={summary.get('selectedRunCount')}/{summary.get('requestedRunCount')}；"
                            f"已省略={summary.get('omittedRunCount')}；maxMatrixRuns={summary.get('maxMatrixRuns')}；"
                            f"选择方式={summary.get('matrixSelection')}"
                        )
                        + " |"
                        for summary in matrix_summaries
                    ],
                ]
            )
            if retry_summary:
                rows.append(
                    "| 重试预算 | "
                    + self._cell(
                        f"attemptsUsed={retry_summary.get('attemptsUsed')}/{retry_summary.get('maxAttempts')}；"
                        f"budgetExhausted={retry_summary.get('budgetExhausted')}；"
                        f"stoppedReason={retry_summary.get('stoppedReason')}"
                    )
                    + " |"
                )
            rows.append("")
        rows.extend(
            [
                "| 对比 | 状态 | 范围 | 截图 | DOM | 网络 | Console | 证据 |",
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
            return '<div class="notice">没有运行时对比差异记录。</div>'
        summary_html = ""
        if matrix_summaries or retry_summary:
            summary_rows = []
            for summary in matrix_summaries:
                value = (
                    f"已选择={summary.get('selectedRunCount')}/{summary.get('requestedRunCount')}；"
                    f"已省略={summary.get('omittedRunCount')}；maxMatrixRuns={summary.get('maxMatrixRuns')}；"
                    f"选择方式={summary.get('matrixSelection')}"
                )
                summary_rows.append(
                    f"<tr><td>矩阵尝试 {self._html_cell(summary.get('attempt'))}</td><td>{self._html_cell(value)}</td></tr>"
                )
            if retry_summary:
                value = (
                    f"attemptsUsed={retry_summary.get('attemptsUsed')}/{retry_summary.get('maxAttempts')}；"
                    f"budgetExhausted={retry_summary.get('budgetExhausted')}；"
                    f"stoppedReason={retry_summary.get('stoppedReason')}"
                )
                summary_rows.append(f"<tr><td>重试预算</td><td>{self._html_cell(value)}</td></tr>")
            summary_html = "<table><tr><th>摘要</th><th>值</th></tr>" + "".join(summary_rows) + "</table>"
        rows = [
            "<tr><th>对比</th><th>状态</th><th>范围</th><th>截图</th><th>DOM</th><th>网络</th><th>Console</th><th>证据</th></tr>"
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
            return "无"
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
            labels.append(f"另有 {remaining} 项")
        return ", ".join(labels) if labels else "无"

    def _runtime_compare_scope_label(self, differences: dict[str, Any]) -> str:
        scope = differences.get("comparisonScope") or {}
        viewport = scope.get("viewport") or {}
        viewport_name = viewport.get("name")
        viewport_size = ""
        if viewport.get("width") and viewport.get("height"):
            viewport_size = f"{viewport.get('width')}x{viewport.get('height')}"
        viewport_label = " ".join(str(part) for part in (viewport_name, viewport_size) if part) or "默认视口"
        return f"{scope.get('scenarioName') or '未知场景'} / {viewport_label}"

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
            percent = f"{float(ratio) * 100:.3f}%" if isinstance(ratio, (int, float)) else "未知"
            pixels = f"；像素={changed_pixels}/{pixel_count}（{percent}）"
        diff_artifact = f"；diff Artifact={screenshot.get('diffArtifactId')}" if screenshot.get("diffArtifactId") else ""
        return f"已变化={changed}；像素状态={pixel_status}{pixels}{diff_artifact}{sizes}"

    def _dom_diff_label(self, differences: dict[str, Any]) -> str:
        dom_differences = differences.get("domDifferences") or []
        changed_fields = differences.get("changedDomFields") or []
        if dom_differences:
            return f"{len(dom_differences)} 处路径变化：{', '.join(str(item.get('path')) for item in dom_differences[:4])}"
        if changed_fields:
            return f"{len(changed_fields)} 个顶层字段：{', '.join(map(str, changed_fields[:4]))}"
        return "无"

    def _collection_diff_label(self, diff: Any) -> str:
        if not isinstance(diff, dict):
            return "不可用"
        original_only = len(diff.get("originalOnly") or [])
        reconstructed_only = len(diff.get("reconstructedOnly") or [])
        shared = len(diff.get("shared") or [])
        groups = ", ".join(sorted((diff.get("groups") or {}).keys())[:4]) or "无"
        return f"仅原始={original_only}；仅重建={reconstructed_only}；共有={shared}；分组={groups}"

    def _runtime_compare_evidence_links(self, record: dict[str, Any]) -> str:
        links = self._runtime_compare_evidence_link_list(record)
        return ", ".join(links) if links else "无"

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
                "limitation": "云端模型能否使用仍取决于是否已配置模型提供方凭据。",
            }
        if job.cloud_mode == "desensitized":
            return {
                "cloudMode": job.cloud_mode,
                "modelContextScope": "sanitized_cloud_or_local",
                "contextHandling": "deterministic_context_redaction",
                "cloudContextAllowed": True,
                "limitation": (
                    "Agent 模型上下文会对源代码摘录、证据引用、记忆、路径和符号进行确定性脱敏；"
                    "原始 Artifact 仍受 Artifact 访问策略和证据附件策略约束。"
                ),
            }
        return {
            "cloudMode": job.cloud_mode,
            "modelContextScope": "local",
            "contextHandling": "local_context_only",
            "cloudContextAllowed": False,
            "limitation": "除非任务以 cloud_allowed 或 desensitized 模式创建，否则禁止使用云端模型上下文。",
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
                description="面向用户的 Markdown 审计报告。",
            ),
            self._package_content(
                path="audit-report.html",
                content_type="text/html; charset=utf-8",
                source="html_report",
                description="可离线查看的 HTML 审计报告。",
            ),
            self._package_content(
                path="audit.json",
                content_type="application/json",
                source="audit_payload",
                description="包含报告输入和完成判定的结构化审计数据。",
            ),
            self._package_content(
                path="evidence-index.json",
                content_type="application/json",
                source="evidence_index",
                description="结果包、报告和证据附件的结构化索引。",
            ),
            self._package_content(
                path="artifact-manifest.json",
                content_type="application/json",
                source="artifact_manifest",
                description="打包时记录的 Artifact 元数据 Manifest。",
            ),
            self._package_content(
                path="build-artifacts.json",
                content_type="application/json",
                source="build_artifact",
                description="构建和类型检查证据记录。",
            ),
            self._package_content(
                path="agent-executions.json",
                content_type="application/json",
                source="agent_execution",
                description="按 Agent 和阶段记录的 CrewAI 执行审计数据。",
            ),
            self._package_content(
                path="inference-records.json",
                content_type="application/json",
                source="inference_record",
                description="Agent 推理审计记录。",
            ),
            self._package_content(
                path="runtime-report.json",
                content_type="application/json",
                source="runtime_validation",
                description="浏览器运行时验证记录。",
            ),
            self._package_content(
                path="runtime-traces.json",
                content_type="application/json",
                source="runtime_trace",
                description="浏览器 runtime trace 记录，包括 remote Browser Runner 执行边界。",
            ),
            self._package_content(
                path="runtime-comparisons.json",
                content_type="application/json",
                source="runtime_comparison",
                description="原始版本与重建版本的运行时对比记录。",
            ),
            self._package_content(
                path="review-runs.json",
                content_type="application/json",
                source="review_run",
                description="审查和门禁判定记录。",
            ),
            self._package_content(
                path="tool-calls.json",
                content_type="application/json",
                source="tool_call",
                description="工具调用审计记录。",
            ),
            self._package_content(
                path="tool-registry.json",
                content_type="application/json",
                source="tool_registry",
                description="Agent runtime 可用的工具注册表条目。",
            ),
            self._package_content(
                path="memory-records.json",
                content_type="application/json",
                source="memory_record",
                description="短期、长期、实体和场景记忆记录。",
            ),
            self._package_content(
                path="runtime-diagnoses.json",
                content_type="application/json",
                source="runtime_diagnosis",
                description="Runtime Agent 的结构化诊断记录。",
            ),
            self._package_content(
                path="report-sections.json",
                content_type="application/json",
                source="report_section",
                description="Report Agent 的结构化章节记录。",
            ),
            self._package_content(
                path="repair-instructions.json",
                content_type="application/json",
                source="repair_instruction",
                description="结构化修复指令和已应用的修复证据。",
            ),
            self._package_content(
                path="review-fix-summary.json",
                content_type="application/json",
                source="review_fix_summary",
                description="统一记录 Review/Fix 收敛结果、重试预算和修复应用情况的摘要。",
            ),
            self._package_content(
                path="ops-alert-events.json",
                content_type="application/json",
                source="ops_alert_event",
                description="打包时可见的近期运维告警事件和 webhook 投递审计记录。",
            ),
        ]
        if generated_project is None:
            contents.append(
                self._package_content(
                    path="generated_project/README.md",
                    content_type="text/markdown; charset=utf-8",
                    source="generated_project",
                    description="说明没有可用生成项目的占位文件。",
                    included=False,
                    reason="打包时没有可用的 generated_project Artifact。",
                )
            )
        else:
            contents.append(
                self._package_content(
                    path="generated_project/",
                    content_type="inode/directory",
                    source="generated_project",
                    description="生成的 TypeScript 项目目录。",
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
                    description="已收集到结果包中的证据附件。",
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
        reason: str = "已包含",
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
        dependency_placeholder_details = self._dependency_placeholder_rows(
            self._load_json_artifacts(job_id, artifacts, store, "reconstruction_plan")
        )
        ops_alert_details = self._ops_alert_report_details(store)
        risk_details = self._risk_report_details([*build_details, *review_details, *runtime_compare_details, *review_fix_details, *ops_alert_details])
        return [
            self._report_section(
                title="完成判定",
                anchor="completion-decision",
                summary="最终 completed 或 best_effort 判定及其主要原因。",
                artifact_kinds=("audit_report", "html_report"),
                artifact_ids_by_kind=artifact_ids_by_kind,
            ),
            self._report_section(
                title="风险与失败分组",
                anchor="risk-and-failure-groups",
                summary="构建、运行时和审查中的失败或 best_effort 观察项。",
                artifact_kinds=("build_artifact", "runtime_validation", "review_run", "runtime_trace"),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=risk_details,
            ),
            self._report_section(
                title="依赖占位摘要",
                anchor="dependency-placeholder-summary",
                summary=(
                    "缺失的静态相对 ESM 依赖、引用它们的导入方、生成的导出项以及仅加载运行时约定。"
                ),
                artifact_kinds=("reconstruction_plan", "generated_project"),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=dependency_placeholder_details,
            ),
            self._report_section(
                title="Review/Fix 收敛情况",
                anchor="review-fix-convergence",
                summary="统一汇总构建、runtime comparison、Agent review、修复应用情况和停止原因。",
                artifact_kinds=("build_artifact", "review_run", "repair_instruction", "runtime_trace", "generated_project"),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=review_fix_details,
            ),
            self._report_section(
                title="构建与类型检查",
                anchor="build-and-typecheck",
                summary="沙箱中的构建和类型检查结果。",
                artifact_kinds=("build_artifact", "build_log"),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=build_details,
            ),
            self._report_section(
                title="运行时证据",
                anchor="runtime-evidence",
                summary="浏览器验证报告、trace、截图和请求证据。",
                artifact_kinds=("runtime_validation", "runtime_trace", "runtime_screenshot"),
                artifact_ids_by_kind=artifact_ids_by_kind,
            ),
            self._report_section(
                title="运行时对比差异摘要",
                anchor="runtime-compare-difference-summary",
                summary="场景和视口对比差异及其证据深层链接。",
                artifact_kinds=("runtime_comparison", "runtime_scenario", "runtime_trace", "runtime_screenshot"),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=runtime_compare_details,
            ),
            self._report_section(
                title="Browser Runner 运行情况",
                anchor="browser-runner-operations",
                summary="remote Browser Runner 的队列长度、延迟、重试、租约恢复、后端健康状态和告警证据。",
                artifact_kinds=("runtime_trace",),
                artifact_ids_by_kind=artifact_ids_by_kind,
            ),
            self._report_section(
                title="运维告警摘要",
                anchor="ops-alert-summary",
                summary="近期已配置的运维告警事件、服务范围和 webhook 投递审计状态。",
                artifact_kinds=("ops_alert_event",),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=ops_alert_details,
            ),
            self._report_section(
                title="Agent 运行时审计",
                anchor="agent-runtime-audit",
                summary="Agent 输出约定、工具注册表条目、记忆记录、runtime diagnosis 和报告章节。",
                artifact_kinds=(
                    "agent_plan",
                    "agent_execution",
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
                title="审查证据",
                anchor="review-evidence",
                summary="审查、修复门禁和 best_effort 判定证据。",
                artifact_kinds=("review_run", "repair_instruction"),
                artifact_ids_by_kind=artifact_ids_by_kind,
                details=review_details,
            ),
            self._report_section(
                title="Artifact Manifest",
                anchor="artifact-manifest",
                summary="已打包的 Artifact Manifest 和 Artifact deep link。",
                artifact_kinds=tuple(sorted(artifact_ids_by_kind)),
                artifact_ids_by_kind=artifact_ids_by_kind,
            ),
            self._report_section(
                title="证据附件索引",
                anchor="evidence-attachment-index",
                summary="结果包中包含或省略的证据文件。",
                artifact_kinds=(),
                artifact_ids_by_kind=artifact_ids_by_kind,
                evidence_artifact_ids=attachment_ids,
            ),
            self._report_section(
                title="复现方法",
                anchor="reproduction",
                summary="离线检查结果包所需的命令和文件。",
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
                    "label": "运维告警事件",
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
                    "label": "构建验证",
                    "value": f"{review_type}，第 {attempt} 次尝试",
                    "status": status,
                    "details": {
                        "artifactId": artifact.id,
                        "reviewType": review_type,
                        "phase": payload.get("phase"),
                        "attempt": attempt,
                        "failureClass": str(payload.get("failureClass") or "unknown"),
                        "decision": str(payload.get("decision") or "构建验证未提供判定。"),
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
                    "label": "审查运行",
                    "value": f"{review_type}，第 {attempt} 次尝试",
                    "status": status,
                    "details": {
                        "artifactId": artifact.id,
                        "reviewType": review_type,
                        "attempt": attempt,
                        "failureClass": str(payload.get("failureClass") or "unknown"),
                        "decision": str(payload.get("decision") or "审查未提供判定。"),
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
                    "label": "Review/Fix 收敛摘要",
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
                        "label": "Review/Fix 策略",
                        "value": (
                            f"低风险自动修复={policy.get('allowLowRiskRepairs')} / "
                            f"动作={', '.join(self._string_list(policy.get('allowedRepairActions'))) or '默认'}"
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
                        "label": "Review/Fix 失败动作映射",
                        "value": (
                            f"{mapping.get('failureClass') or 'unknown'} -> "
                            f"{', '.join(automatic_actions) or '仅审计'}"
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
                        "label": "Review/Fix 后续步骤",
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
                "decision": f"无法读取 {kind} Artifact：{error}",
            }
        if not isinstance(payload, dict):
            return {
                "artifactId": artifact.id,
                "kind": kind,
                "status": "best_effort",
                "failureClass": "unknown",
                "decision": f"{kind} Artifact 不包含 JSON 对象。",
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
            scope_label = f"{scope.get('scenarioName') or payload.get('scenarioArtifactId') or '未知'} / {viewport_label}"
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
                "label": "运行时对比范围",
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
        value = f"矩阵已选择 {selected if selected is not None else 0}/{requested if requested is not None else 0}"
        if retry_summary:
            value += f"；尝试 {retry_summary.get('attemptsUsed')}/{retry_summary.get('maxAttempts')} 次"
        return [
            {
                "label": "运行时对比矩阵摘要",
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
            return "默认视口"
        viewport_name = viewport.get("name")
        viewport_size = ""
        if viewport.get("width") and viewport.get("height"):
            viewport_size = f"{viewport['width']}x{viewport['height']}"
        return " ".join(str(part) for part in (viewport_name, viewport_size) if part) or "默认视口"

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
            return False, "Artifact kind 不在已配置的 includeKinds 范围内。"
        if artifact.kind in policy.exclude_kinds:
            return False, "Artifact kind 已被配置的 excludeKinds 排除。"
        if (
            policy.include_sensitivity_classes is not None
            and artifact.sensitivity_class not in policy.include_sensitivity_classes
        ):
            return False, "Artifact 的 sensitivityClass 不在已配置的 includeSensitivityClasses 范围内。"
        if policy.include_retention_classes is not None and artifact.retention_class not in policy.include_retention_classes:
            return False, "Artifact 的 retentionClass 不在已配置的 includeRetentionClasses 范围内。"
        if policy.max_bytes_per_artifact is not None and artifact.size > policy.max_bytes_per_artifact:
            return False, "Artifact 大小超过已配置的 maxBytesPerArtifact。"
        if not store.artifact_exists(artifact) or not store.artifact_is_file(artifact):
            return False, "Artifact 内容缺失或不是文件。"
        return True, "已包含"

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
            return "无记录。"
        header = "| " + " | ".join(self._report_column_label(column) for column in columns) + " |"
        divider = "| " + " | ".join("---" for _ in columns) + " |"
        rows = [header, divider]
        for record in records:
            rows.append("| " + " | ".join(self._cell(record.get(column)) for column in columns) + " |")
        return "\n".join(rows)

    def _report_column_label(self, column: str) -> str:
        return REPORT_COLUMN_LABELS.get(column, column)

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
            archive.writestr("agent-executions.json", self._json_text(audit_payload["agentExecutions"]))
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
                    "打包时没有可用的 generated_project Artifact。\n",
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
                archive.writestr(f"{root_name}/README.md", f"目录 Artifact 不可用：{error}\n")
                return
            self._write_directory(archive, materialized, root_name)

    def _write_directory(self, archive: zipfile.ZipFile, source: Path, root_name: str) -> None:
        if not source.exists() or not source.is_dir():
            archive.writestr(f"{root_name}/README.md", f"目录 Artifact 不可用：{source}\n")
            return
        for file_path in sorted(path for path in source.rglob("*") if path.is_file()):
            archive.write(file_path, f"{root_name}/{file_path.relative_to(source).as_posix()}")

    def _latest_artifact(self, artifacts: list[ArtifactRecord], kind: str) -> ArtifactRecord | None:
        matches = [artifact for artifact in artifacts if artifact.kind == kind]
        return matches[-1] if matches else None

    def _json_text(self, payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
