import type { AuditFilterState, NormalizedAuditRecord } from "./workbench-types";
import type { AuditRiskGroup, AuditRiskGroupId, JobEvidence, SavedAuditFilter } from "./workbench-types";
import { auditFilterStorageKey } from "./workbench-types";
import { formatDurationMs, formatPercent, isRecord } from "./workbench-format";

export function readSavedAuditFilters(): SavedAuditFilter[] {
  if (typeof window === "undefined") {
    return [];
  }
  try {
    const raw = window.localStorage.getItem(auditFilterStorageKey);
    if (!raw) {
      return [];
    }
    const payload = JSON.parse(raw);
    if (!Array.isArray(payload)) {
      return [];
    }
    return payload
      .filter(isRecord)
      .map((item): SavedAuditFilter | null => {
        if (typeof item.id !== "string" || typeof item.name !== "string" || !isRecord(item.filters)) {
          return null;
        }
        return {
          createdAt: typeof item.createdAt === "string" ? item.createdAt : new Date().toISOString(),
          filters: sanitizeAuditFilters(item.filters),
          id: item.id,
          name: item.name
        };
      })
      .filter((item): item is SavedAuditFilter => Boolean(item));
  } catch {
    return [];
  }
}

export function persistSavedAuditFilters(filters: SavedAuditFilter[]): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(auditFilterStorageKey, JSON.stringify(filters));
}

export function sanitizeAuditFilters(value: Record<string, unknown>): AuditFilterState {
  const category = value.category === "inference" || value.category === "review" || value.category === "tool" ? value.category : "all";
  const status = value.status === "attention" || value.status === "pass" || value.status === "fail" ? value.status : "all";
  return {
    category,
    query: typeof value.query === "string" ? value.query : "",
    status
  };
}

export function buildAuditRecords(evidence: JobEvidence): NormalizedAuditRecord[] {
  return [
    ...evidence.inferenceRecords.map((record) => ({
      artifactIds: [...record.inputArtifactIds, ...record.outputArtifactIds],
      category: "inference" as const,
      detail: `confidence ${formatPercent(record.confidence)} / ${record.validationStatus}`,
      evidenceRefs: record.evidenceRefs,
      failureClass: "none",
      id: record.id,
      label: record.type,
      secondary: record.agentName,
      status: record.validationStatus
    })),
    ...evidence.reviewRuns.map((run) => ({
      artifactIds: [run.logsArtifactId, ...run.repairInstructionIds].filter((artifactId): artifactId is string =>
        Boolean(artifactId)
      ),
      category: "review" as const,
      detail: run.decision,
      evidenceRefs: run.evidenceRefs,
      failureClass: run.failureClass,
      id: run.id,
      label: run.reviewType,
      secondary: `attempt ${run.attempt}`,
      status: run.status
    })),
    ...evidence.toolCalls.map((call) => ({
      artifactIds: [...call.inputArtifactIds, ...call.outputArtifactIds],
      category: "tool" as const,
      detail: `${call.caller} / ${formatDurationMs(call.duration)}`,
      evidenceRefs: [],
      failureClass: call.failureClass,
      id: call.id,
      label: call.toolName,
      secondary: call.toolVersion,
      status: call.status
    }))
  ];
}

export function auditFilterLabel(filters: AuditFilterState, t: (key: string) => string): string {
  const parts = [
    filters.category === "all" ? t("audit.label.allRecords") : filters.category,
    filters.status === "all" ? t("audit.label.allStatuses") : filters.status,
    filters.query.trim() ? `"${filters.query.trim()}"` : t("audit.label.noSearch")
  ];
  return parts.join(" / ");
}

export function groupAuditRecordsByRisk(records: NormalizedAuditRecord[]): AuditRiskGroup[] {
  const groups: AuditRiskGroup[] = [
    {
      detail: "non-none failure classes or failing decisions",
      id: "blocking",
      records: [],
      title: "Blocking risk"
    },
    {
      detail: "best-effort, retry, unverified, or needs-review records",
      id: "review",
      records: [],
      title: "Needs review"
    },
    {
      detail: "accepted or passing records",
      id: "passing",
      records: [],
      title: "Passing evidence"
    }
  ];
  const groupsById = new Map(groups.map((group) => [group.id, group]));
  for (const record of records) {
    groupsById.get(auditRecordRiskGroup(record))?.records.push(record);
  }
  return groups;
}

export function auditRecordRiskGroup(record: NormalizedAuditRecord): AuditRiskGroupId {
  if (record.failureClass !== "none" || statusTokenTone(record.status) === "fail") {
    return "blocking";
  }
  if (auditRecordNeedsAttention(record)) {
    return "review";
  }
  return "passing";
}

export function auditRecordMatches(record: NormalizedAuditRecord, filters: AuditFilterState): boolean {
  if (filters.category !== "all" && record.category !== filters.category) {
    return false;
  }
  if (filters.status === "attention" && !auditRecordNeedsAttention(record)) {
    return false;
  }
  if (filters.status === "pass" && statusTokenTone(record.status) !== "pass") {
    return false;
  }
  if (filters.status === "fail" && statusTokenTone(record.status) !== "fail") {
    return false;
  }
  const query = filters.query.trim().toLowerCase();
  if (!query) {
    return true;
  }
  const haystack = [
    record.category,
    record.detail,
    record.failureClass,
    record.id,
    record.label,
    record.secondary,
    record.status,
    ...record.artifactIds,
    ...record.evidenceRefs.flatMap((ref) => [ref.artifactId, ref.label, ref.locator ?? "", ref.excerpt ?? ""])
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query);
}

export function auditRecordNeedsAttention(record: NormalizedAuditRecord): boolean {
  return record.failureClass !== "none" || ["best_effort", "fail", "needs_review", "rejected", "retry", "unverified"].includes(record.status);
}

export function statusTokenTone(status: string): "pass" | "warn" | "fail" {
  if (status === "pass" || status === "accepted") {
    return "pass";
  }
  if (status === "fail" || status === "rejected") {
    return "fail";
  }
  return "warn";
}
