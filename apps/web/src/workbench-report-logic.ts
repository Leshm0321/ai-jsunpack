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

export function parseEvidenceIndexPayload(text: string, t: (key: string) => string): EvidenceIndexPayload {
  const payload = JSON.parse(text) as Partial<EvidenceIndexPayload>;
  if (payload.kind !== "evidence_index" || !Array.isArray(payload.attachments)) {
    throw new Error(t("parse.evidenceIndexInvalid"));
  }
  const attachments = payload.attachments.map(normalizeEvidenceAttachment);
  return {
    attachments,
    failureSummary: Array.isArray(payload.failureSummary) ? payload.failureSummary.map((item) => normalizeFailureSummary(item, t)) : [],
    includedCount: payload.includedCount ?? attachments.filter((item) => item.included).length,
    jobId: payload.jobId ?? "",
    kind: "evidence_index",
    omittedCount: payload.omittedCount ?? attachments.filter((item) => !item.included).length,
    packageContents: Array.isArray(payload.packageContents) ? payload.packageContents.map(normalizePackageContent) : [],
    reportSections: Array.isArray(payload.reportSections) ? payload.reportSections.map((item) => normalizeReportSection(item, t)) : [],
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

export function normalizeReportSection(value: ReportSectionEntry, t: (key: string) => string): ReportSectionEntry {
  const rawDetails: unknown[] = Array.isArray(value.details) ? value.details : [];
  return {
    anchor: String(value.anchor ?? ""),
    artifactIds: Array.isArray(value.artifactIds) ? value.artifactIds.map(String) : [],
    artifactKinds: Array.isArray(value.artifactKinds) ? value.artifactKinds.map(String) : [],
    details: rawDetails.filter(isRecord).map((item) => normalizeReportSectionDetail(item, t)),
    evidenceLinks: Array.isArray(value.evidenceLinks) ? value.evidenceLinks.map(String) : [],
    summary: String(value.summary ?? ""),
    title: String(value.title ?? t("fallback.reportSection"))
  };
}

export function normalizeReportSectionDetail(value: Record<string, unknown>, t: (key: string) => string): ReportSectionDetailEntry {
  return {
    details: isRecord(value.details) ? value.details : {},
    label: String(value.label ?? t("fallback.reportDetail")),
    status: typeof value.status === "string" ? value.status : undefined,
    value: String(value.value ?? "")
  };
}

export function normalizeFailureSummary(value: FailureSummaryEntry, t: (key: string) => string): FailureSummaryEntry {
  return {
    decision: String(value.decision ?? t("fallback.validationIncomplete")),
    failureClass: String(value.failureClass ?? "unknown"),
    group: String(value.group ?? "reports"),
    status: String(value.status ?? "best_effort")
  };
}

export function buildFailureSummary(
  evidence: JobEvidence,
  currentJob: Job | null,
  t: (key: string) => string
): FailureSummaryEntry[] {
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
        decision: currentJob.failureReason ?? t("fallback.jobFailure"),
        failureClass: currentJob.failureClass,
        group: "job",
        status: currentJob.status
      }
    ];
  }
  return [];
}

export function buildFallbackPackageContents(
  attachments: EvidenceAttachmentEntry[],
  t: (key: string) => string
): PackageContentEntry[] {
  const fixedEntries: PackageContentEntry[] = [
    fallbackPackageContent("audit-report.md", "audit_report", t("fallback.package.auditMd")),
    fallbackPackageContent("audit-report.html", "html_report", t("fallback.package.auditHtml")),
    fallbackPackageContent("audit.json", "audit_payload", t("fallback.package.auditJson")),
    fallbackPackageContent("evidence-index.json", "evidence_index", t("fallback.package.evidenceIndex")),
    fallbackPackageContent("artifact-manifest.json", "artifact_manifest", t("fallback.package.manifest")),
    fallbackPackageContent("runtime-report.json", "runtime_validation", t("fallback.package.runtimeReport")),
    fallbackPackageContent("review-runs.json", "review_run", t("fallback.package.reviewRuns"))
  ];
  return [
    ...fixedEntries,
    ...attachments.map((attachment): PackageContentEntry => ({
      artifactId: attachment.artifactId,
      contentType: attachment.contentType,
      description: t("fallback.package.attachment"),
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

export function buildFallbackReportSections(
  attachments: EvidenceAttachmentEntry[],
  artifacts: Artifact[],
  t: (key: string) => string
): ReportSectionEntry[] {
  const artifactIdsByKind = new Map<string, string[]>();
  artifacts.forEach((artifact) => {
    artifactIdsByKind.set(artifact.kind, [...(artifactIdsByKind.get(artifact.kind) ?? []), artifact.id]);
  });
  const attachmentIds = attachments.filter((item) => item.included).map((item) => item.artifactId);
  return [
    fallbackReportSection(
      t("fallback.section.completion"),
      "completion-decision",
      t("fallback.section.completionSummary"),
      ["audit_report", "html_report"],
      artifactIdsByKind
    ),
    fallbackReportSection(
      t("fallback.section.risk"),
      "risk-and-failure-groups",
      t("fallback.section.riskSummary"),
      ["build_artifact", "runtime_validation", "review_run"],
      artifactIdsByKind
    ),
    fallbackReportSection(
      t("fallback.section.runtimeCompare"),
      "runtime-compare-difference-summary",
      t("fallback.section.runtimeCompareSummary"),
      ["runtime_comparison", "runtime_scenario", "runtime_trace", "runtime_screenshot"],
      artifactIdsByKind
    ),
    {
      anchor: "evidence-attachment-index",
      artifactIds: attachmentIds,
      artifactKinds: [],
      details: [],
      evidenceLinks: attachmentIds.map((artifactId) => `artifact://${artifactId}`),
      summary: t("fallback.section.attachmentSummary"),
      title: t("fallback.section.attachment")
    },
    fallbackReportSection(
      t("fallback.section.reproduction"),
      "reproduction",
      t("fallback.section.reproductionSummary"),
      ["result_package", "evidence_index"],
      artifactIdsByKind
    )
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

export function filterReportSections(
  sections: ReportSectionEntry[],
  query: string,
  t: (key: string) => string
): ReportSectionEntry[] {
  const normalizedQuery = query.trim().toLowerCase();
  if (!normalizedQuery) {
    return sections;
  }
  return sections.flatMap((section) => {
    const sectionMatches = reportSectionSearchText(section).includes(normalizedQuery);
    const matchingDetails = section.details.filter((detail) => reportSectionDetailSearchText(detail, t).includes(normalizedQuery));
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

export function reportSectionDetailSearchText(detail: ReportSectionDetailEntry, t: (key: string) => string): string {
  return [detail.label, detail.value, detail.status ?? "", reportSectionDetailSummary(detail, t), safeJsonText(detail.details)]
    .join(" ")
    .toLowerCase();
}

export function reportSectionDetailSummary(detail: ReportSectionDetailEntry, t: (key: string) => string): string {
  const payload = detail.details;
  const parts: string[] = [];
  appendPayloadPart(parts, t("audit.table.type"), payload.reviewType ?? payload.phase);
  appendPayloadPart(parts, t("detail.attempt"), payload.attempt);
  appendPayloadPart(parts, t("job.failure"), payload.failureClass && payload.failureClass !== "none" ? payload.failureClass : null);
  appendPayloadPart(parts, t("detail.stage"), payload.targetStage);
  if (isRecord(payload.policy)) {
    appendPayloadPart(parts, t("summary.policy"), reviewFixPolicySummary(payload.policy, t));
  }
  if (Array.isArray(payload.automaticActions)) {
    parts.push(
      `${t("summary.automaticActions")} ${payload.automaticActions.length ? payload.automaticActions.map(String).join(", ") : t("status.noneLower")}`
    );
  }
  if (Array.isArray(payload.auditOnlyActions) && payload.auditOnlyActions.length > 0) {
    parts.push(`${t("summary.manualActions")} ${payload.auditOnlyActions.length}`);
  }
  appendPayloadPart(parts, t("summary.next"), payload.nextStep);
  appendPayloadPart(parts, t("summary.command"), payload.commandSource);
  appendPayloadPart(parts, t("summary.script"), payload.scriptName);
  if (typeof payload.diagnosticCount === "number") {
    parts.push(`${payload.diagnosticCount} ${t("summary.diagnostics")}`);
  }
  if (isRecord(payload.resourcePolicy)) {
    const runner = [payload.resourcePolicy.runnerKind, payload.resourcePolicy.enforcement].filter(Boolean).join("/");
    appendPayloadPart(parts, t("summary.runner"), runner || null);
  }
  if (Array.isArray(payload.repairInstructionIds) && payload.repairInstructionIds.length > 0) {
    parts.push(`${payload.repairInstructionIds.length} ${t("summary.repairs")}`);
  }
  if (Array.isArray(payload.domDifferences)) {
    parts.push(`${t("runtime.domDifferences")} ${payload.domDifferences.length}`);
  }
  const networkSummary = collectionDiffSummary(payload.networkDiff, t);
  if (networkSummary) {
    parts.push(`${t("runtime.networkDifferences")} ${networkSummary}`);
  }
  const consoleSummary = collectionDiffSummary(payload.consoleDiff, t);
  if (consoleSummary) {
    parts.push(`${t("runtime.consoleDifferences")} ${consoleSummary}`);
  }
  appendPayloadPart(parts, t("summary.decision"), payload.decision);
  if (Array.isArray(payload.evidenceLinks)) {
    parts.push(`${payload.evidenceLinks.length} ${t("summary.evidence")}`);
  }
  return parts.length > 0 ? parts.join(" / ") : t("summary.noStructuredBreakdown");
}

export function reviewFixPolicySummary(policy: Record<string, unknown>, t: (key: string) => string): string {
  const lowRisk = policy.allowLowRiskRepairs;
  const actions = Array.isArray(policy.allowedRepairActions) ? policy.allowedRepairActions.map(String) : [];
  return `${t("summary.lowRiskAuto")}=${formatUnknownValue(lowRisk)} ${t("summary.actions")}=${actions.length ? actions.join(",") : t("summary.default")}`;
}

export function appendPayloadPart(parts: string[], label: string, value: unknown): void {
  if (value === undefined || value === null || value === "") {
    return;
  }
  parts.push(`${label} ${formatUnknownValue(value)}`);
}

export function collectionDiffSummary(value: unknown, t: (key: string) => string): string | null {
  if (!isRecord(value)) {
    return null;
  }
  if (value.changed === false) {
    return t("summary.unchanged");
  }
  const originalCount = typeof value.originalCount === "number" ? value.originalCount : null;
  const reconstructedCount = typeof value.reconstructedCount === "number" ? value.reconstructedCount : null;
  const groups = Array.isArray(value.groups) ? value.groups.map(String).slice(0, 3) : [];
  const countSummary = originalCount !== null && reconstructedCount !== null ? `${originalCount}->${reconstructedCount}` : t("summary.changed");
  return groups.length > 0 ? `${countSummary} ${groups.join(", ")}` : countSummary;
}
