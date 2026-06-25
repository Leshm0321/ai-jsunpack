import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from apps.api.app.artifact_store import InMemoryObjectStorageClient, S3CompatibleArtifactStore
from apps.api.app.models import CreateJobRequest, OpsAlert, OpsAlertDelivery, OpsAlertEvent, OpsAlertRule
from apps.api.app.store import artifacts_table, create_store
from apps.worker.worker.packaging import PackagingRunner


class PackagingRunnerTest(unittest.TestCase):
    def test_packaging_includes_ops_alert_event_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                checked_at = "2026-06-22T08:00:00+00:00"
                rule = OpsAlertRule(
                    code="sandbox_runner_degraded",
                    severity="critical",
                    metric_path="worker.sandboxRunnerDegraded",
                    operator="eq",
                    threshold=True,
                    message="Sandbox runner degraded to a weaker boundary.",
                    service_role="worker",
                    enabled=True,
                    source="env",
                )
                alert = OpsAlert(
                    code=rule.code,
                    severity=rule.severity,
                    message=rule.message,
                    field=rule.metric_path,
                    value=True,
                    threshold=True,
                    service_role="worker",
                    instance_id="worker-a",
                    checked_at=checked_at,
                )
                store.record_ops_alert_event(
                    OpsAlertEvent(
                        id="ops_alert_event_packaging",
                        checked_at=checked_at,
                        status="active",
                        severity="critical",
                        code=rule.code,
                        message=rule.message,
                        field=rule.metric_path,
                        value=True,
                        threshold=True,
                        service_role="worker",
                        instance_id="worker-a",
                        rule=rule,
                        alerts=[alert],
                        metrics={"worker": {"sandboxRunnerDegraded": True}},
                        delivery=OpsAlertDelivery(
                            status="failed",
                            attempted=True,
                            webhook_url_configured=True,
                            event_id="ops_alert_event_packaging",
                            error="timeout",
                        ),
                        created_at=checked_at,
                        updated_at=checked_at,
                    )
                )

                result = PackagingRunner().run(job_id=job.id, store=store)
                report = Path(result.audit_report_artifact.storage_uri).read_text(encoding="utf-8")
                evidence_index = json.loads(Path(result.evidence_index_artifact.storage_uri).read_text(encoding="utf-8"))
                sections = {item["anchor"]: item for item in evidence_index["reportSections"]}
                with zipfile.ZipFile(result.result_package_artifact.storage_uri) as archive:
                    ops_events = json.loads(archive.read("ops-alert-events.json").decode("utf-8"))
                    audit_payload = json.loads(archive.read("audit.json").decode("utf-8"))

                self.assertIn("## Ops Alert Summary", report)
                self.assertIn("sandbox_runner_degraded", report)
                self.assertIn("ops-alert-summary", sections)
                self.assertEqual(sections["ops-alert-summary"]["details"][0]["status"], "fail")
                self.assertEqual(ops_events[0]["id"], "ops_alert_event_packaging")
                self.assertEqual(audit_payload["opsAlertEvents"][0]["delivery"]["status"], "failed")
            finally:
                store.close()

    def test_completion_decision_uses_latest_attempt_per_validation_group(self):
        decision = PackagingRunner()._completion_decision(
            {
                "buildArtifacts": [],
                "runtimeReports": [
                    {
                        "target": "reconstructed",
                        "attempt": 0,
                        "status": "fail",
                        "failureClass": "runtime_error",
                        "decision": "first runtime compare failed",
                        "comparisonArtifactId": "comparison_0",
                    },
                    {
                        "target": "reconstructed",
                        "attempt": 1,
                        "status": "pass",
                        "failureClass": "none",
                        "decision": "runtime compare passed after repair",
                        "comparisonArtifactId": "comparison_1",
                    },
                ],
                "reviewRuns": [
                    {
                        "reviewType": "runtime_compare",
                        "attempt": 0,
                        "status": "fail",
                        "failureClass": "runtime_error",
                        "decision": "planned runtime repair",
                    },
                    {
                        "reviewType": "runtime_compare",
                        "attempt": 1,
                        "status": "pass",
                        "failureClass": "none",
                        "decision": "runtime compare repair passed",
                    },
                ],
            }
        )

        self.assertEqual(decision["status"], "completed")
        self.assertEqual(decision["failureClass"], "none")
        self.assertEqual(decision["observations"], [])

    def test_evidence_attachment_include_kinds_filter_controls_zip_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(
                    CreateJobRequest(
                        config={
                            "packaging": {
                                "evidenceAttachments": {
                                    "includeKinds": ["runtime_trace"],
                                }
                            }
                        }
                    )
                )
                build_log = store.write_artifact(
                    job.id,
                    kind="build_log",
                    stage="building",
                    filename="build-log.json",
                    content=b'{"status":"pass"}',
                    content_type="application/json",
                    producer="test",
                )
                build_artifact = store.write_artifact(
                    job.id,
                    kind="build_artifact",
                    stage="building",
                    filename="build-artifact.json",
                    content=json.dumps(
                        {
                            "kind": "build_artifact",
                            "id": "build_artifact_test",
                            "jobId": job.id,
                            "stage": "building",
                            "reviewType": "build",
                            "phase": "build",
                            "attempt": 1,
                            "status": "fail",
                            "decision": "npm build failed",
                            "command": ["npm", "run", "build"],
                            "commandSource": "package_script",
                            "scriptName": "build",
                            "packageManager": "npm",
                            "exitCode": 1,
                            "durationMs": 123,
                            "failureClass": "build_error",
                            "timedOut": False,
                            "outputTruncated": False,
                            "workingDirectory": "generated_project",
                            "networkPolicy": "deny",
                            "resourcePolicy": {
                                "processLimit": 64,
                                "cpuTimeLimitMs": 120000,
                                "memoryLimitBytes": 536870912,
                                "enforcement": "local_best_effort",
                                "runnerKind": "local",
                                "runtimeName": "node",
                                "runtimeVersion": "24-test",
                                "hostPlatform": "win32",
                                "capabilities": [
                                    {
                                        "name": "network",
                                        "status": "best_effort",
                                        "detail": "deny policy recorded for local runner",
                                    }
                                ],
                                "limitations": ["local runner records resource policy without OS enforcement"],
                            },
                            "diagnostics": [
                                {
                                    "source": "typescript",
                                    "tool": "tsc",
                                    "category": "error",
                                    "code": "TS1005",
                                    "message": "Expected semicolon.",
                                    "filePath": "src/main.ts",
                                    "line": 1,
                                    "column": 12,
                                    "contextLines": [],
                                    "relatedInformation": [],
                                }
                            ],
                            "logsArtifactId": build_log.id,
                            "repairInstructionIds": ["repair_test"],
                            "limitations": ["test limitation"],
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test",
                )
                runtime_trace = store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="runtime_compare",
                    filename="trace.json",
                    content=b'{"trace":true}',
                    content_type="application/json",
                    producer="test",
                )
                matrix_trace = store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="runtime_compare",
                    filename="runtime-compare-matrix-attempt-2.json",
                    content=json.dumps(
                        {
                            "kind": "runtime_trace",
                            "jobId": job.id,
                            "target": "runtime_compare_matrix",
                            "attempt": 2,
                            "pruned": False,
                            "requestedRunCount": 1,
                            "selectedRunCount": 1,
                            "omittedRunCount": 0,
                            "maxMatrixRuns": 24,
                            "matrixSelection": "balanced",
                            "selectedRuns": [
                                {
                                    "requestedIndex": 0,
                                    "scenarioId": "scenario_test",
                                    "scenarioName": "default-load",
                                    "viewport": {"name": "desktop", "width": 1280, "height": 720},
                                }
                            ],
                            "omittedRuns": [],
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test",
                )
                retry_summary_trace = store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="runtime_compare",
                    filename="runtime-compare-retry-summary.json",
                    content=json.dumps(
                        {
                            "kind": "runtime_trace",
                            "jobId": job.id,
                            "target": "runtime_compare_retry_summary",
                            "maxAttempts": 3,
                            "attemptsUsed": 3,
                            "budgetExhausted": True,
                            "stoppedReason": "retry_budget_exhausted",
                            "finalProjectArtifactId": "generated_project_attempt_2",
                            "finalReviewStatus": "fail",
                            "attempts": [
                                {"attempt": 0, "reviewGateTriggered": True, "comparisonArtifactIds": ["comparison_0"]},
                                {"attempt": 1, "reviewGateTriggered": True, "comparisonArtifactIds": ["comparison_1"]},
                                {"attempt": 2, "reviewGateTriggered": True, "comparisonArtifactIds": []},
                            ],
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test",
                )
                convergence_summary_trace = store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="reviewing",
                    filename="review-fix-convergence-summary.json",
                    content=json.dumps(
                        {
                            "kind": "runtime_trace",
                            "jobId": job.id,
                            "target": "review_fix_convergence_summary",
                            "finalOutcome": "budget_exhausted_best_effort",
                            "status": "best_effort",
                            "failureClass": "runtime_error",
                            "buildTypecheck": {
                                "maxAttempt": 0,
                                "latestStatusByReviewType": {"build": "fail"},
                                "allPassed": False,
                                "needsAttention": True,
                                "repairInstructionIds": ["repair_test"],
                                "repairInstructionCount": 1,
                                "appliedProjectArtifactIds": [],
                                "appliedRepairCount": 0,
                                "evidenceArtifactIds": [],
                            },
                            "runtimeCompare": {
                                "maxAttempts": 3,
                                "attemptsUsed": 3,
                                "budgetExhausted": True,
                                "stoppedReason": "retry_budget_exhausted",
                                "finalReviewStatus": "fail",
                                "finalProjectArtifactId": "generated_project_attempt_2",
                                "plannedRepairCount": 2,
                                "appliedRepairCount": 2,
                                "triggeredReviewCount": 3,
                                "attempts": [],
                                "evidenceArtifactIds": [retry_summary_trace.id],
                            },
                            "agentReview": {
                                "reviewArtifactId": "review_test",
                                "repairInstructionIds": ["repair_test"],
                                "repairInstructionCount": 1,
                            },
                            "evidenceArtifactIds": [retry_summary_trace.id],
                            "evidenceLinks": [f"artifact://{retry_summary_trace.id}"],
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test",
                )
                runtime_screenshot = store.write_artifact(
                    job.id,
                    kind="runtime_screenshot",
                    stage="runtime_compare",
                    filename="screenshot.png",
                    content=b"\x89PNG\r\n\x1a\nfake",
                    content_type="image/png",
                    producer="test",
                )
                runtime_scenario = store.write_artifact(
                    job.id,
                    kind="runtime_scenario",
                    stage="runtime_compare",
                    filename="runtime-scenario.json",
                    content=json.dumps(
                        {
                            "kind": "runtime_scenario",
                            "jobId": job.id,
                            "name": "default-load",
                            "entry": "index.html",
                            "networkPolicy": "deny",
                            "timeoutMs": 2500,
                            "viewport": {"name": "desktop", "width": 1280, "height": 720},
                            "waitForSelector": "#app",
                            "interactions": [],
                            "assertions": [],
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test",
                )
                runtime_comparison = store.write_artifact(
                    job.id,
                    kind="runtime_comparison",
                    stage="runtime_compare",
                    filename="runtime-comparison.json",
                    content=json.dumps(
                        {
                            "kind": "runtime_comparison",
                            "jobId": job.id,
                            "attempt": 2,
                            "scenarioArtifactId": runtime_scenario.id,
                            "status": "fail",
                            "differences": {
                                "comparisonScope": {
                                    "scenarioName": "default-load",
                                    "networkPolicy": "deny",
                                    "timeoutMs": 2500,
                                    "viewport": {"name": "desktop", "width": 1280, "height": 720},
                                },
                                "domDifferences": [
                                    {"path": "html/body[1]/div[1]", "summary": "text changed"}
                                ],
                                "networkDiff": {
                                    "changed": True,
                                    "originalCount": 3,
                                    "reconstructedCount": 4,
                                    "originalOnly": ["/api/a"],
                                    "reconstructedOnly": ["/api/b"],
                                    "groups": {"api": ["/api/a", "/api/b"]},
                                },
                                "consoleDiff": {
                                    "changed": True,
                                    "originalCount": 1,
                                    "reconstructedCount": 2,
                                    "originalOnly": ["warn:old"],
                                    "reconstructedOnly": ["warn:new"],
                                    "groups": {"warn": ["warn:old", "warn:new"]},
                                },
                            },
                            "traceArtifactIds": [runtime_trace.id],
                            "screenshotArtifactIds": [],
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test",
                )
                review_run = store.write_artifact(
                    job.id,
                    kind="review_run",
                    stage="reviewing",
                    filename="review.json",
                    content=json.dumps(
                        {
                            "id": "review_test",
                            "jobId": job.id,
                            "attempt": 0,
                            "reviewType": "runtime_compare",
                            "status": "fail",
                            "decision": "runtime compare needs repair",
                            "failureClass": "runtime_error",
                            "evidenceRefs": [
                                {
                                    "artifactId": runtime_comparison.id,
                                    "label": "Runtime comparison evidence",
                                    "locator": "artifact:runtime_comparison",
                                }
                            ],
                            "repairInstructionIds": ["repair_test"],
                            "logsArtifactId": build_log.id,
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test",
                )
                memory_record = store.write_artifact(
                    job.id,
                    kind="memory_record",
                    stage="agent_planning",
                    filename="memory-record-short-term.json",
                    content=json.dumps(
                        {
                            "id": "memory_test",
                            "scope": "job",
                            "projectId": job.project_id,
                            "jobId": job.id,
                            "memoryType": "short_term",
                            "content": "Packaging test memory.",
                            "sourceArtifactIds": [runtime_trace.id],
                            "sensitivityClass": "derived",
                            "retentionClass": "project",
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test",
                )
                tool_registry = store.write_artifact(
                    job.id,
                    kind="tool_registry",
                    stage="agent_planning",
                    filename="tool-registry.json",
                    content=json.dumps(
                        {
                            "kind": "tool_registry",
                            "jobId": job.id,
                            "entries": [
                                {
                                    "id": "tool_registry_test",
                                    "jobId": job.id,
                                    "toolName": "crewai.agent_pass",
                                    "toolVersion": "0.2.0",
                                    "category": "model",
                                    "caller": "WorkerPipeline",
                                    "inputArtifactKinds": ["input_inventory", "ast_index"],
                                    "outputArtifactKinds": ["agent_plan", "inference_record"],
                                    "failureClasses": ["none", "policy_denied", "agent_failed"],
                                    "description": "Packaging test registry.",
                                }
                            ],
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test",
                )

                result = PackagingRunner().run(job_id=job.id, store=store)
                evidence_index = json.loads(Path(result.evidence_index_artifact.storage_uri).read_text(encoding="utf-8"))
                audit_payload = self._read_zip_json(result.result_package_artifact.storage_uri, "audit.json")
                attachments = {item["artifactId"]: item for item in evidence_index["attachments"]}

                self.assertEqual(evidence_index["includedCount"], 4)
                self.assertEqual(evidence_index["omittedCount"], 4)
                package_contents = {item["path"]: item for item in evidence_index["packageContents"]}
                report_sections = {item["anchor"]: item for item in evidence_index["reportSections"]}
                self.assertIn(
                    "build_error",
                    {item["failureClass"] for item in evidence_index["failureSummary"]},
                )
                self.assertIn(
                    "runtime_error",
                    {item["failureClass"] for item in evidence_index["failureSummary"]},
                )
                self.assertEqual(evidence_index["policySummary"]["accessBoundary"]["ownerId"], "local-user")
                self.assertEqual(evidence_index["policySummary"]["accessBoundary"]["projectId"], "default")
                self.assertEqual(evidence_index["policySummary"]["modelPolicy"]["cloudMode"], "local_only")
                self.assertFalse(evidence_index["policySummary"]["modelPolicy"]["cloudContextAllowed"])
                self.assertEqual(audit_payload["policySummary"], evidence_index["policySummary"])
                self.assertTrue(attachments[runtime_trace.id]["included"])
                self.assertFalse(attachments[build_log.id]["included"])
                self.assertFalse(attachments[runtime_screenshot.id]["included"])
                self.assertEqual(
                    attachments[build_log.id]["reason"],
                    "Artifact kind is outside configured includeKinds.",
                )
                self.assertIn("audit-report.md", package_contents)
                self.assertIn("evidence-index.json", package_contents)
                self.assertIn("generated_project/README.md", package_contents)
                self.assertIn(f"evidence/runtime_trace/{runtime_trace.id}.json", package_contents)
                self.assertIn(f"evidence/runtime_trace/{convergence_summary_trace.id}.json", package_contents)
                self.assertTrue(package_contents[f"evidence/runtime_trace/{runtime_trace.id}.json"]["included"])
                self.assertTrue(package_contents[f"evidence/runtime_trace/{convergence_summary_trace.id}.json"]["included"])
                self.assertFalse(package_contents[f"evidence/build_log/{build_log.id}"]["included"])
                self.assertEqual(package_contents[f"evidence/runtime_trace/{runtime_trace.id}.json"]["artifactId"], runtime_trace.id)
                self.assertIn("risk-and-failure-groups", report_sections)
                self.assertIn("agent-runtime-audit", report_sections)
                self.assertIn("evidence-attachment-index", report_sections)
                self.assertIn(f"artifact://{review_run.id}", report_sections["risk-and-failure-groups"]["evidenceLinks"])
                self.assertIn(f"artifact://{memory_record.id}", report_sections["agent-runtime-audit"]["evidenceLinks"])
                self.assertIn(f"artifact://{tool_registry.id}", report_sections["agent-runtime-audit"]["evidenceLinks"])
                self.assertIn(f"artifact://{runtime_trace.id}", report_sections["evidence-attachment-index"]["evidenceLinks"])
                self.assertIn(f"artifact://{matrix_trace.id}", report_sections["runtime-compare-difference-summary"]["evidenceLinks"])
                self.assertIn(f"artifact://{retry_summary_trace.id}", report_sections["runtime-compare-difference-summary"]["evidenceLinks"])
                self.assertEqual(audit_payload["reviewFixSummary"]["finalOutcome"], "budget_exhausted_best_effort")
                self.assertIn("review-fix-summary.json", package_contents)
                build_section = report_sections["build-and-typecheck"]
                self.assertTrue(build_section["details"])
                build_detail = build_section["details"][0]
                self.assertEqual(build_detail["label"], "Build validation")
                self.assertEqual(build_detail["status"], "fail")
                self.assertEqual(build_detail["details"]["artifactId"], build_artifact.id)
                self.assertEqual(build_detail["details"]["reviewType"], "build")
                self.assertEqual(build_detail["details"]["diagnosticCount"], 1)
                self.assertEqual(build_detail["details"]["diagnostics"][0]["code"], "TS1005")
                self.assertEqual(build_detail["details"]["logsArtifactId"], build_log.id)
                self.assertEqual(build_detail["details"]["resourcePolicy"]["enforcement"], "local_best_effort")
                self.assertIn(f"artifact://{build_artifact.id}", build_detail["details"]["evidenceLinks"])
                self.assertIn(f"artifact://{build_log.id}", build_detail["details"]["evidenceLinks"])
                review_section = report_sections["review-evidence"]
                self.assertTrue(review_section["details"])
                review_detail = review_section["details"][0]
                self.assertEqual(review_detail["label"], "Review run")
                self.assertEqual(review_detail["status"], "fail")
                self.assertEqual(review_detail["details"]["reviewType"], "runtime_compare")
                self.assertEqual(review_detail["details"]["evidenceRefCount"], 1)
                self.assertEqual(review_detail["details"]["logsArtifactId"], build_log.id)
                self.assertIn(f"artifact://{runtime_comparison.id}", review_detail["details"]["evidenceLinks"])
                risk_section = report_sections["risk-and-failure-groups"]
                self.assertGreaterEqual(len(risk_section["details"]), 2)
                self.assertTrue(any(item["details"]["failureClass"] == "build_error" for item in risk_section["details"]))
                self.assertTrue(any(item["details"]["failureClass"] == "runtime_error" for item in risk_section["details"]))
                runtime_compare_section = report_sections["runtime-compare-difference-summary"]
                self.assertTrue(runtime_compare_section["details"])
                matrix_detail = runtime_compare_section["details"][0]
                runtime_compare_detail = runtime_compare_section["details"][1]
                self.assertEqual(matrix_detail["label"], "Runtime compare matrix summary")
                self.assertEqual(matrix_detail["status"], "fail")
                self.assertEqual(matrix_detail["details"]["matrix"]["selectedRunCount"], 1)
                self.assertTrue(matrix_detail["details"]["retryBudget"]["budgetExhausted"])
                self.assertEqual(matrix_detail["details"]["retryBudget"]["attemptsUsed"], 3)
                self.assertIn(f"artifact://{retry_summary_trace.id}", matrix_detail["details"]["evidenceLinks"])
                self.assertEqual(runtime_compare_detail["label"], "Runtime compare scope")
                self.assertEqual(runtime_compare_detail["status"], "fail")
                self.assertIn("default-load", runtime_compare_detail["value"])
                self.assertEqual(runtime_compare_detail["details"]["comparisonScope"]["scenarioName"], "default-load")
                self.assertEqual(runtime_compare_detail["details"]["domDifferences"][0]["path"], "html/body[1]/div[1]")
                self.assertEqual(runtime_compare_detail["details"]["networkDiff"]["groups"], ["api"])
                self.assertEqual(runtime_compare_detail["details"]["consoleDiff"]["groups"], ["warn"])
                self.assertEqual(runtime_compare_detail["details"]["attemptHistory"][0]["attempt"], 2)
                self.assertIn(f"artifact://{runtime_comparison.id}", runtime_compare_detail["details"]["evidenceLinks"])
                self.assertTrue(
                    any(item["label"] == "Runtime compare matrix summary" for item in risk_section["details"])
                )
                review_fix_section = report_sections["review-fix-convergence"]
                self.assertIn(f"artifact://{convergence_summary_trace.id}", review_fix_section["evidenceLinks"])
                review_fix_detail = review_fix_section["details"][0]
                self.assertEqual(review_fix_detail["label"], "Review/Fix convergence summary")
                self.assertEqual(review_fix_detail["status"], "best_effort")
                self.assertEqual(review_fix_detail["details"]["finalOutcome"], "budget_exhausted_best_effort")
                self.assertEqual(review_fix_detail["details"]["runtimeCompare"]["attemptsUsed"], 3)
                self.assertTrue(
                    any(item["label"] == "Review/Fix convergence summary" for item in risk_section["details"])
                )

                with zipfile.ZipFile(result.result_package_artifact.storage_uri) as archive:
                    names = set(archive.namelist())
                    packaged_summary = json.loads(archive.read("review-fix-summary.json").decode("utf-8"))
                self.assertIn(f"evidence/runtime_trace/{runtime_trace.id}.json", names)
                self.assertIn(f"evidence/runtime_trace/{convergence_summary_trace.id}.json", names)
                self.assertIn("tool-registry.json", names)
                self.assertIn("memory-records.json", names)
                self.assertIn("runtime-diagnoses.json", names)
                self.assertIn("report-sections.json", names)
                self.assertIn("review-fix-summary.json", names)
                self.assertEqual(packaged_summary["finalOutcome"], "budget_exhausted_best_effort")
                self.assertNotIn(f"evidence/build_log/{build_log.id}.json", names)
                self.assertNotIn(f"evidence/runtime_screenshot/{runtime_screenshot.id}.png", names)
                report_text = Path(result.audit_report_artifact.storage_uri).read_text(encoding="utf-8")
                self.assertIn("## Policy Summary", report_text)
                self.assertIn("Cloud context allowed", report_text)
            finally:
                store.close()

    def test_packaging_reports_remote_browser_runner_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(project_id="proj", owner_id="owner"))
                store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="runtime_smoke",
                    filename="runtime-trace.json",
                    content=json.dumps(
                        {
                            "kind": "runtime_trace",
                            "jobId": job.id,
                            "target": "reconstructed",
                            "status": "pass",
                            "executionBoundary": {
                                "runnerKind": "remote_browser_runner",
                                "enforcement": "remote_isolated",
                                "remoteRunId": "browser_run_test",
                                "auth": "bearer_hmac",
                                "artifactExchange": "worker_request_archive_and_worker_registered_runtime_artifacts",
                                "queueBackend": "postgresql",
                                "queueLength": 3,
                                "runningCount": 2,
                                "terminalCount": 8,
                                "totalCount": 13,
                                "oldestQueuedAgeMs": 5400,
                                "claimLatencyMs": 1200,
                                "averageRunDurationMs": 4500,
                                "retryRate": 0.25,
                                "leaseRecoveryCount": 1,
                                "expiredRunningCount": 1,
                                "backendHealthStatus": "ok",
                                "alerts": [
                                    {
                                        "code": "queue_backlog",
                                        "severity": "warning",
                                        "message": "Browser Runner queued run count exceeds local worker concurrency.",
                                        "field": "queuedCount",
                                        "value": 3,
                                        "threshold": 2,
                                    }
                                ],
                            },
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test.packaging",
                )

                result = PackagingRunner().run(job_id=job.id, store=store)
                report = Path(result.audit_report_artifact.storage_uri).read_text(encoding="utf-8")
                audit_payload = self._read_zip_json(result.result_package_artifact.storage_uri, "audit.json")
                runtime_traces = self._read_zip_json(result.result_package_artifact.storage_uri, "runtime-traces.json")
                evidence_index = json.loads(Path(result.evidence_index_artifact.storage_uri).read_text(encoding="utf-8"))
                report_sections = {item["anchor"]: item for item in evidence_index["reportSections"]}

                self.assertIn("Browser Runner Boundary", report)
                self.assertIn("Browser Runner Operations", report)
                self.assertIn("browser_run_test", report)
                self.assertIn("postgresql", report)
                self.assertIn("runningCount", report)
                self.assertIn("terminalCount", report)
                self.assertIn("totalCount", report)
                self.assertIn("oldestQueuedAgeMs", report)
                self.assertIn("expiredRunningCount", report)
                self.assertIn("queue_backlog", report)
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["runnerKind"], "remote_browser_runner")
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["queueLength"], 3)
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["runningCount"], 2)
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["terminalCount"], 8)
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["totalCount"], 13)
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["oldestQueuedAgeMs"], 5400)
                self.assertEqual(audit_payload["runtimeTraces"][0]["executionBoundary"]["expiredRunningCount"], 1)
                self.assertEqual(runtime_traces[0]["executionBoundary"]["remoteRunId"], "browser_run_test")
                self.assertIn("browser-runner-operations", report_sections)
            finally:
                store.close()

    def test_evidence_attachment_policy_records_sensitivity_retention_and_size_omissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(
                    CreateJobRequest(
                        config={
                            "packaging": {
                                "evidenceAttachments": {
                                    "includeKinds": [
                                        "build_log",
                                        "runtime_scenario",
                                        "runtime_screenshot",
                                        "runtime_trace",
                                    ],
                                    "includeSensitivityClasses": ["derived"],
                                    "includeRetentionClasses": ["archive"],
                                    "maxBytesPerArtifact": 4,
                                }
                            }
                        }
                    )
                )
                trace = store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="runtime_compare",
                    filename="trace.json",
                    content=b"ok",
                    content_type="application/json",
                    producer="test",
                )
                screenshot = store.write_artifact(
                    job.id,
                    kind="runtime_screenshot",
                    stage="runtime_compare",
                    filename="screenshot.png",
                    content=b"ok",
                    content_type="image/png",
                    producer="test",
                )
                build_log = store.write_artifact(
                    job.id,
                    kind="build_log",
                    stage="building",
                    filename="build-log.json",
                    content=b"12345",
                    content_type="application/json",
                    producer="test",
                )
                scenario = store.write_artifact(
                    job.id,
                    kind="runtime_scenario",
                    stage="runtime_compare",
                    filename="scenario.json",
                    content=b"ok",
                    content_type="application/json",
                    producer="test",
                )
                self._set_artifact_policy_metadata(store, trace.id, sensitivity_class="source_sensitive", retention_class="archive")
                self._set_artifact_policy_metadata(store, screenshot.id, sensitivity_class="derived", retention_class="ephemeral")
                self._set_artifact_policy_metadata(store, build_log.id, sensitivity_class="derived", retention_class="archive")
                self._set_artifact_policy_metadata(store, scenario.id, sensitivity_class="derived", retention_class="archive")

                result = PackagingRunner().run(job_id=job.id, store=store)
                evidence_index = json.loads(Path(result.evidence_index_artifact.storage_uri).read_text(encoding="utf-8"))
                attachments = {item["artifactId"]: item for item in evidence_index["attachments"]}

                self.assertEqual(evidence_index["includedCount"], 1)
                self.assertEqual(evidence_index["omittedCount"], 3)
                self.assertEqual(
                    attachments[trace.id]["reason"],
                    "Artifact sensitivityClass is outside configured includeSensitivityClasses.",
                )
                self.assertEqual(
                    attachments[screenshot.id]["reason"],
                    "Artifact retentionClass is outside configured includeRetentionClasses.",
                )
                self.assertEqual(attachments[build_log.id]["reason"], "Artifact exceeds configured maxBytesPerArtifact.")
                self.assertTrue(attachments[scenario.id]["included"])
                self.assertEqual(attachments[scenario.id]["sensitivityClass"], "derived")
                self.assertEqual(attachments[scenario.id]["retentionClass"], "archive")
                self.assertEqual(evidence_index["policySummary"]["sensitivityCounts"]["derived"], 3)
                self.assertEqual(evidence_index["policySummary"]["sensitivityCounts"]["source_sensitive"], 1)
                self.assertEqual(evidence_index["policySummary"]["retentionCounts"]["archive"], 3)
                self.assertEqual(evidence_index["policySummary"]["retentionCounts"]["ephemeral"], 1)

                with zipfile.ZipFile(result.result_package_artifact.storage_uri) as archive:
                    names = set(archive.namelist())
                self.assertIn(f"evidence/runtime_scenario/{scenario.id}.json", names)
                self.assertNotIn(f"evidence/runtime_trace/{trace.id}.json", names)
                self.assertNotIn(f"evidence/runtime_screenshot/{screenshot.id}.png", names)
                self.assertNotIn(f"evidence/build_log/{build_log.id}.json", names)
            finally:
                store.close()

    def test_packaging_policy_summary_records_desensitized_context_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
            )
            try:
                job = store.create_job(CreateJobRequest(cloud_mode="desensitized"))
                store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="runtime_compare",
                    filename="trace.json",
                    content=b'{"trace":true}',
                    content_type="application/json",
                    producer="test",
                    sensitivity_class="derived",
                )

                result = PackagingRunner().run(job_id=job.id, store=store)
                evidence_index = json.loads(Path(result.evidence_index_artifact.storage_uri).read_text(encoding="utf-8"))
                model_policy = evidence_index["policySummary"]["modelPolicy"]

                self.assertEqual(model_policy["cloudMode"], "desensitized")
                self.assertEqual(model_policy["modelContextScope"], "sanitized_cloud_or_local")
                self.assertEqual(model_policy["contextHandling"], "deterministic_context_redaction")
                self.assertTrue(model_policy["cloudContextAllowed"])
                self.assertIn("deterministic source excerpt", model_policy["limitation"])
            finally:
                store.close()

    def test_evidence_attachment_can_package_non_local_object_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            object_client = InMemoryObjectStorageClient()
            store = create_store(
                database_url=f"sqlite:///{(root / 'metadata.db').as_posix()}",
                artifact_root=root / "artifacts",
                artifact_store=S3CompatibleArtifactStore(bucket="artifact-bucket", prefix="packaging", client=object_client),
            )
            try:
                job = store.create_job(
                    CreateJobRequest(
                        config={
                            "packaging": {
                                "evidenceAttachments": {
                                    "includeKinds": ["runtime_trace"],
                                }
                            }
                        }
                    )
                )
                runtime_trace = store.write_artifact(
                    job.id,
                    kind="runtime_trace",
                    stage="runtime_compare",
                    filename="trace.json",
                    content=b'{"trace":"object"}',
                    content_type="application/json",
                    producer="test",
                )
                runtime_scenario = store.write_artifact(
                    job.id,
                    kind="runtime_scenario",
                    stage="runtime_compare",
                    filename="scenario.json",
                    content=b'{"kind":"runtime_scenario","name":"object-scenario"}',
                    content_type="application/json",
                    producer="test",
                )
                runtime_comparison = store.write_artifact(
                    job.id,
                    kind="runtime_comparison",
                    stage="runtime_compare",
                    filename="comparison.json",
                    content=json.dumps(
                        {
                            "kind": "runtime_comparison",
                            "jobId": job.id,
                            "attempt": 0,
                            "scenarioArtifactId": runtime_scenario.id,
                            "status": "fail",
                            "differences": {
                                "comparisonScope": {
                                    "scenarioName": "object-scenario",
                                    "networkPolicy": "deny",
                                    "timeoutMs": 1000,
                                    "viewport": {"name": "mobile", "width": 390, "height": 844},
                                },
                                "domDifferences": [{"path": "title", "summary": "title changed"}],
                                "networkDiff": {"changed": False, "originalCount": 1, "reconstructedCount": 1},
                                "consoleDiff": {"changed": False, "originalCount": 0, "reconstructedCount": 0},
                            },
                            "traceArtifactIds": [runtime_trace.id],
                            "screenshotArtifactIds": [],
                        }
                    ).encode("utf-8"),
                    content_type="application/json",
                    producer="test",
                )

                result = PackagingRunner().run(job_id=job.id, store=store)
                evidence_index = json.loads(store.read_artifact_record(result.evidence_index_artifact).decode("utf-8"))
                package_bytes = store.read_artifact_record(result.result_package_artifact)
                report_sections = {item["anchor"]: item for item in evidence_index["reportSections"]}
                runtime_details = report_sections["runtime-compare-difference-summary"]["details"]

                self.assertTrue(evidence_index["attachments"][0]["included"])
                runtime_scope_detail = next(item for item in runtime_details if item["label"] == "Runtime compare scope")
                self.assertEqual(runtime_scope_detail["details"]["comparisonArtifactId"], runtime_comparison.id)
                self.assertEqual(runtime_scope_detail["details"]["domDifferences"][0]["path"], "title")
                with zipfile.ZipFile(self._bytes_zip_path(root, package_bytes)) as archive:
                    self.assertEqual(
                        archive.read(f"evidence/runtime_trace/{runtime_trace.id}.json"),
                        b'{"trace":"object"}',
                    )
            finally:
                store.close()

    def _set_artifact_policy_metadata(self, store, artifact_id: str, *, sensitivity_class: str, retention_class: str) -> None:
        with store.engine.begin() as connection:
            connection.execute(
                artifacts_table.update()
                .where(artifacts_table.c.id == artifact_id)
                .values(sensitivity_class=sensitivity_class, retention_class=retention_class)
            )

    def _read_zip_json(self, package_path: str, name: str):
        with zipfile.ZipFile(package_path) as archive:
            return json.loads(archive.read(name).decode("utf-8"))

    def _bytes_zip_path(self, root: Path, content: bytes) -> Path:
        package_path = root / "package.zip"
        package_path.write_bytes(content)
        return package_path


if __name__ == "__main__":
    unittest.main()
