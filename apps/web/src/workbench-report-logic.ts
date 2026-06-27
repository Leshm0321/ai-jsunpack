import type { Artifact, Job } from "@ai-jsunpack/shared";
import type {
  EvidenceAttachmentEntry,
  EvidenceIndexPayload,
  FailureSummaryEntry,
  JobEvidence,
  PackageContentEntry,
  ReportSectionDetailEntry,
  ReportSectionEntry
} from "./workbench-types";
import { formatUnknownValue, isRecord, safeJsonText } from "./workbench-format";

export function parseEvidenceIndexPayload(text: string): EvidenceIndexPayload {
  const payload = JSON.parse(text) as Partial<EvidenceIndexPayload>;
  if (payload.kind !== "evidence_index" || !Array.isArray(payload.attachments)) {
    throw new Error("Artifact is not a valid evidence index.");
  }
  const attachments = payload.attachments.map(normalizeEvidenceAttachment);
  return {
    attachments,
    failureSummary: Array.isArray(payload.failureSummary) ? payload.failureSummary.map(normalizeFailureSummary) : [],
    includedCount: payload.includedCount ?? attachments.filter((item) => item.included).length,
    jobId: payload.jobId ?? "",
    kind: "evidence_index",
    omittedCount: payload.omittedCount ?? attachments.filter((item) => !item.included).length,
    packageContents: Array.isArray(payload.packageContents) ? payload.packageContents.map(normalizePackageContent) : [],
    reportSections: Array.isArray(payload.reportSections) ? payload.reportSections.map(normalizeReportSection) : [],
    schemaVersion: payload.schemaVersion ?? "unknown"
  };
}

export function normalizeEvidenceAttachment(value: EvidenceAttachmentEntry): EvidenceAttachmentEntry {
  return {
    artifactId: String(value.artifactId ?? ""),
    contentType: String(value.contentType ?? "application/octet-stream"),
    hash: String(value.hash ?? ""),
    included: Boolean(value.included),
    kind: String(value.kind ?? "unknown"),
    packagePath: typeof value.packagePath === "string" ? value.packagePath : null,
    reason: String(value.reason ?? ""),
    retentionClass: typeof value.retentionClass === "string" ? value.retentionClass : undefined,
    sensitivityClass: typeof value.sensitivityClass === "string" ? value.sensitivityClass : undefined,
    size: typeof value.size === "number" ? value.size : 0,
    sourceFilename: String(value.sourceFilename ?? ""),
    stage: String(value.stage ?? "packaging")
  };
}

export function normalizePackageContent(value: PackageContentEntry): PackageContentEntry {
  return {
    artifactId: typeof value.artifactId === "string" ? value.artifactId : null,
    contentType: String(value.contentType ?? "application/octet-stream"),
    description: String(value.description ?? ""),
    included: value.included !== false,
    path: String(value.path ?? ""),
    reason: String(value.reason ?? ""),
    size: typeof value.size === "number" ? value.size : null,
    source: String(value.source ?? "package")
  };
}

export function normalizeReportSection(value: ReportSectionEntry): ReportSectionEntry {
  const rawDetails: unknown[] = Array.isArray(value.details) ? value.details : [];
  return {
    anchor: String(value.anchor ?? ""),
    artifactIds: Array.isArray(value.artifactIds) ? value.artifactIds.map(String) : [],
    artifactKinds: Array.isArray(value.artifactKinds) ? value.artifactKinds.map(String) : [],
    details: rawDetails.filter(isRecord).map(normalizeReportSectionDetail),
    evidenceLinks: Array.isArray(value.evidenceLinks) ? value.evidenceLinks.map(String) : [],
    summary: String(value.summary ?? ""),
    title: String(value.title ?? "Report section")
  };
}

export function normalizeReportSectionDetail(value: Record<string, unknown>): ReportSectionDetailEntry {
  return {
    details: isRecord(value.details) ? value.details : {},
    label: String(value.label ?? "Detail"),
    status: typeof value.status === "string" ? value.status : undefined,
    value: String(value.value ?? "")
  };
}

export function normalizeFailureSummary(value: FailureSummaryEntry): FailureSummaryEntry {
  return {
    decision: String(value.decision ?? "Validation did not fully pass."),
    failureClass: String(value.failureClass ?? "unknown"),
    group: String(value.group ?? "reports"),
    status: String(value.status ?? "best_effort")
  };
}

export function buildFailureSummary(evidence: JobEvidence, currentJob: Job | null): FailureSummaryEntry[] {
  const reviewItems = evidence.reviewRuns
    .filter((run) => run.failureClass !== "none" || ["best_effort", "fail", "retry"].includes(run.status))
    .map((run): FailureSummaryEntry => ({
      decision: run.decision,
      failureClass: run.failureClass,
      group: run.reviewType,
      status: run.status
    }));
  if (reviewItems.length > 0) {
    return reviewItems;
  }
  if (currentJob?.failureClass && currentJob.failureClass !== "none") {
    return [
      {
        decision: currentJob.failureReason ?? "Job completed with a non-none failure class.",
        failureClass: currentJob.failureClass,
        group: "job",
        status: currentJob.status
      }
    ];
  }
  return [];
}

export function buildFallbackPackageContents(attachments: EvidenceAttachmentEntry[]): PackageContentEntry[] {
  const fixedEntries: PackageContentEntry[] = [
    fallbackPackageContent("audit-report.md", "audit_report", "Human-readable Markdown audit report."),
    fallbackPackageContent("audit-report.html", "html_report", "Offline HTML audit report."),
    fallbackPackageContent("audit.json", "audit_payload", "Structured audit payload."),
    fallbackPackageContent("evidence-index.json", "evidence_index", "Evidence attachment index."),
    fallbackPackageContent("artifact-manifest.json", "artifact_manifest", "Artifact manifest."),
    fallbackPackageContent("runtime-report.json", "runtime_validation", "Runtime validation records."),
    fallbackPackageContent("review-runs.json", "review_run", "Review records.")
  ];
  return [
    ...fixedEntries,
    ...attachments.map((attachment): PackageContentEntry => ({
      artifactId: attachment.artifactId,
      contentType: attachment.contentType,
      description: "Evidence attachment collected into the result package.",
      included: attachment.included,
      path: attachment.packagePath ?? `evidence/${attachment.kind}/${attachment.artifactId}`,
      reason: attachment.reason,
      size: attachment.size,
      source: String(attachment.kind)
    }))
  ];
}

export function fallbackPackageContent(path: string, source: string, description: string): PackageContentEntry {
  return {
    artifactId: null,
    contentType: path.endsWith(".html") ? "text/html" : "application/json",
    description,
    included: true,
    path,
    reason: "included",
    size: null,
    source
  };
}

export function buildFallbackReportSections(attachments: EvidenceAttachmentEntry[], artifacts: Artifact[]): ReportSectionEntry[] {
  const artifactIdsByKind = new Map<string, string[]>();
  artifacts.forEach((artifact) => {
    artifactIdsByKind.set(artifact.kind, [...(artifactIdsByKind.get(artifact.kind) ?? []), artifact.id]);
  });
  const attachmentIds = attachments.filter((item) => item.included).map((item) => item.artifactId);
  return [
    fallbackReportSection("Completion Decision", "completion-decision", "Final packaging decision.", ["audit_report", "html_report"], artifactIdsByKind),
    fallbackReportSection(
      "Risk And Failure Groups",
      "risk-and-failure-groups",
      "Failing or best-effort observations.",
      ["build_artifact", "runtime_validation", "review_run"],
      artifactIdsByKind
    ),
    fallbackReportSection(
      "Runtime Compare Difference Summary",
      "runtime-compare-difference-summary",
      "Runtime comparison differences and related evidence.",
      ["runtime_comparison", "runtime_scenario", "runtime_trace", "runtime_screenshot"],
      artifactIdsByKind
    ),
    {
      anchor: "evidence-attachment-index",
      artifactIds: attachmentIds,
      artifactKinds: [],
      details: [],
      evidenceLinks: attachmentIds.map((artifactId) => `artifact://${artifactId}`),
      summary: "Evidence files included in or omitted from the result package.",
      title: "Evidence Attachment Index"
    },
    fallbackReportSection("Reproduction", "reproduction", "Offline inspection commands.", ["result_package", "evidence_index"], artifactIdsByKind)
  ];
}

export function fallbackReportSection(
  title: string,
  anchor: string,
  summary: string,
  kinds: string[],
  artifactIdsByKind: Map<string, string[]>
): ReportSectionEntry {
  const artifactIds = kinds.flatMap((kind) => artifactIdsByKind.get(kind) ?? []);
  return {
    anchor,
    artifactIds,
    artifactKinds: kinds,
    details: [],
    evidenceLinks: artifactIds.map((artifactId) => `artifact://${artifactId}`),
    summary,
    title
  };
}

export function filterReportSections(sections: ReportSectionEntry[], query: string): ReportSectionEntry[] {
  const normalizedQuery = query.trim().toLowerCase();
  if (!normalizedQuery) {
    return sections;
  }
  return sections.flatMap((section) => {
    const sectionMatches = reportSectionSearchText(section).includes(normalizedQuery);
    const matchingDetails = section.details.filter((detail) => reportSectionDetailSearchText(detail).includes(normalizedQuery));
    if (sectionMatches) {
      return [{ ...section, details: matchingDetails.length > 0 ? matchingDetails : section.details }];
    }
    if (matchingDetails.length > 0) {
      return [{ ...section, details: matchingDetails }];
    }
    return [];
  });
}

export function reportSectionSearchText(section: ReportSectionEntry): string {
  return [
    section.anchor,
    section.summary,
    section.title,
    ...section.artifactIds,
    ...section.artifactKinds,
    ...section.evidenceLinks
  ]
    .join(" ")
    .toLowerCase();
}

export function reportSectionDetailSearchText(detail: ReportSectionDetailEntry): string {
  return [detail.label, detail.value, detail.status ?? "", reportSectionDetailSummary(detail), safeJsonText(detail.details)].join(" ").toLowerCase();
}

export function reportSectionDetailSummary(detail: ReportSectionDetailEntry): string {
  const payload = detail.details;
  const parts: string[] = [];
  appendPayloadPart(parts, "type", payload.reviewType ?? payload.phase);
  appendPayloadPart(parts, "attempt", payload.attempt);
  appendPayloadPart(parts, "failure", payload.failureClass && payload.failureClass !== "none" ? payload.failureClass : null);
  appendPayloadPart(parts, "stage", payload.targetStage);
  if (isRecord(payload.policy)) {
    appendPayloadPart(parts, "policy", reviewFixPolicySummary(payload.policy));
  }
  if (Array.isArray(payload.automaticActions)) {
    parts.push(`auto ${payload.automaticActions.length ? payload.automaticActions.map(String).join(", ") : "none"}`);
  }
  if (Array.isArray(payload.auditOnlyActions) && payload.auditOnlyActions.length > 0) {
    parts.push(`manual ${payload.auditOnlyActions.length}`);
  }
  appendPayloadPart(parts, "next", payload.nextStep);
  appendPayloadPart(parts, "command", payload.commandSource);
  appendPayloadPart(parts, "script", payload.scriptName);
  if (typeof payload.diagnosticCount === "number") {
    parts.push(`${payload.diagnosticCount} diagnostics`);
  }
  if (isRecord(payload.resourcePolicy)) {
    const runner = [payload.resourcePolicy.runnerKind, payload.resourcePolicy.enforcement].filter(Boolean).join("/");
    appendPayloadPart(parts, "runner", runner || null);
  }
  if (Array.isArray(payload.repairInstructionIds) && payload.repairInstructionIds.length > 0) {
    parts.push(`${payload.repairInstructionIds.length} repairs`);
  }
  if (Array.isArray(payload.domDifferences)) {
    parts.push(`DOM ${payload.domDifferences.length}`);
  }
  const networkSummary = collectionDiffSummary(payload.networkDiff);
  if (networkSummary) {
    parts.push(`network ${networkSummary}`);
  }
  const consoleSummary = collectionDiffSummary(payload.consoleDiff);
  if (consoleSummary) {
    parts.push(`console ${consoleSummary}`);
  }
  appendPayloadPart(parts, "decision", payload.decision);
  if (Array.isArray(payload.evidenceLinks)) {
    parts.push(`${payload.evidenceLinks.length} evidence`);
  }
  return parts.length > 0 ? parts.join(" / ") : "No structured breakdown";
}

export function reviewFixPolicySummary(policy: Record<string, unknown>): string {
  const lowRisk = policy.allowLowRiskRepairs;
  const actions = Array.isArray(policy.allowedRepairActions) ? policy.allowedRepairActions.map(String) : [];
  return `lowRiskAuto=${formatUnknownValue(lowRisk)} actions=${actions.length ? actions.join(",") : "default"}`;
}

export function appendPayloadPart(parts: string[], label: string, value: unknown): void {
  if (value === undefined || value === null || value === "") {
    return;
  }
  parts.push(`${label} ${formatUnknownValue(value)}`);
}

export function collectionDiffSummary(value: unknown): string | null {
  if (!isRecord(value)) {
    return null;
  }
  if (value.changed === false) {
    return "unchanged";
  }
  const originalCount = typeof value.originalCount === "number" ? value.originalCount : null;
  const reconstructedCount = typeof value.reconstructedCount === "number" ? value.reconstructedCount : null;
  const groups = Array.isArray(value.groups) ? value.groups.map(String).slice(0, 3) : [];
  const countSummary = originalCount !== null && reconstructedCount !== null ? `${originalCount}->${reconstructedCount}` : "changed";
  return groups.length > 0 ? `${countSummary} ${groups.join(", ")}` : countSummary;
}
