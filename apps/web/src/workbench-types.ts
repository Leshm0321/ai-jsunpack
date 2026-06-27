import type { Artifact, AstIndex, CloudMode, EvidenceRef, InferenceRecord, InputInventory, Job, JobStatus, ReviewRun, RuntimeComparisonReport, RuntimeValidationRun, ToolCall } from "@ai-jsunpack/shared";

export type StageState = "done" | "active" | "pending" | "warning" | "fail";
export type MetricStatus = "pass" | "warn" | "fail";

export interface StageDefinition {
  status: JobStatus;
  labelKey: string;
}

export interface StageItem extends StageDefinition {
  label: string;
  state: StageState;
}

export interface RuntimeMetric {
  label: string;
  value: string;
  status: MetricStatus;
}

export interface WorkbenchData {
  stages: StageItem[];
  latestRuntime: RuntimeValidationRun | null;
  reportArtifacts: Artifact[];
  runtimeMetrics: RuntimeMetric[];
}

export interface JobEvidence {
  runtimeValidations: RuntimeValidationRun[];
  inferenceRecords: InferenceRecord[];
  reviewRuns: ReviewRun[];
  toolCalls: ToolCall[];
}

export interface EvidenceAttachmentEntry {
  artifactId: string;
  contentType: string;
  hash: string;
  included: boolean;
  kind: Artifact["kind"] | string;
  packagePath: string | null;
  reason: string;
  retentionClass?: Artifact["retentionClass"] | string;
  sensitivityClass?: Artifact["sensitivityClass"] | string;
  size: number;
  sourceFilename: string;
  stage: JobStatus | string;
}

export interface PackageContentEntry {
  artifactId: string | null;
  contentType: string;
  description: string;
  included: boolean;
  path: string;
  reason: string;
  size: number | null;
  source: string;
}

export interface ReportSectionDetailEntry {
  details: Record<string, unknown>;
  label: string;
  status?: string;
  value: string;
}

export interface ReportSectionEntry {
  anchor: string;
  artifactIds: string[];
  artifactKinds: string[];
  details: ReportSectionDetailEntry[];
  evidenceLinks: string[];
  summary: string;
  title: string;
}

export interface FailureSummaryEntry {
  decision: string;
  failureClass: string;
  group: string;
  status: string;
}

export interface EvidenceIndexPayload {
  attachments: EvidenceAttachmentEntry[];
  failureSummary: FailureSummaryEntry[];
  includedCount: number;
  jobId: string;
  kind: "evidence_index";
  omittedCount: number;
  packageContents: PackageContentEntry[];
  reportSections: ReportSectionEntry[];
  schemaVersion: string;
}

export interface JobWorkspace {
  summary: {
    job: Job;
    artifacts: Artifact[];
  };
  evidence: JobEvidence;
}

export type ArtifactPreviewStatus = "idle" | "loading" | "ready" | "unsupported" | "error";
export type AuditCategory = "all" | "inference" | "review" | "tool";
export type AuditStatusFilter = "all" | "attention" | "pass" | "fail";

export interface ArtifactPreview {
  artifactId: string | null;
  error: string | null;
  reason: string | null;
  status: ArtifactPreviewStatus;
  text: string | null;
}

export interface ArtifactPreviewSupport {
  reason: string | null;
  supported: boolean;
}

export interface AuditFilterState {
  category: AuditCategory;
  query: string;
  status: AuditStatusFilter;
}

export type AuditRiskGroupId = "blocking" | "review" | "passing";

export interface SavedAuditFilter {
  createdAt: string;
  filters: AuditFilterState;
  id: string;
  name: string;
}

export interface AuditRiskGroup {
  detail: string;
  id: AuditRiskGroupId;
  records: NormalizedAuditRecord[];
  title: string;
}

export interface NormalizedAuditRecord {
  artifactIds: string[];
  category: Exclude<AuditCategory, "all">;
  detail: string;
  evidenceRefs: EvidenceRef[];
  failureClass: string;
  id: string;
  label: string;
  secondary: string;
  status: string;
}

export interface RuntimeComparisonState {
  error: string | null;
  reports: RuntimeComparisonLoaded[];
  status: "idle" | "loading" | "ready" | "error";
}

export interface RuntimeComparisonLoaded {
  artifactId: string;
  report: RuntimeComparisonReport;
}

export interface RuntimeComparisonFilters {
  scenario: string;
  status: "all" | RuntimeComparisonReport["status"];
  viewport: string;
}

export type EvidenceGraphMode = "lineage" | "chunks" | "agents";
export type EvidenceGraphNodeKind = "artifact" | "resource" | "analysis" | "agent" | "review" | "tool";
export type EvidenceGraphTone = "neutral" | "pass" | "warn" | "fail" | "active";

export interface EvidenceGraphNode {
  artifactId?: string;
  column: number;
  detail: string;
  id: string;
  kind: EvidenceGraphNodeKind;
  title: string;
  tone?: EvidenceGraphTone;
}

export interface EvidenceGraphEdge {
  from: string;
  id: string;
  label: string;
  to: string;
}

export interface EvidenceGraph {
  edges: EvidenceGraphEdge[];
  emptyDetail: string;
  nodes: EvidenceGraphNode[];
  summary: string;
  title: string;
}

export interface EvidenceGraphSourceState {
  astIndexes: AstIndex[] | null;
  error: string | null;
  inventory: InputInventory | null;
  status: "idle" | "loading" | "ready" | "error";
}

export const previewMaxBytes = 256 * 1024;
export const auditFilterStorageKey = "ai-jsunpack.auditFilters.v1";
export const defaultAuditFilters: AuditFilterState = { category: "all", query: "", status: "all" };
export const runtimeComparisonRowHeight = 50;
export const runtimeComparisonListHeight = 280;

export const stageDefinitions: StageDefinition[] = [
  { status: "queued", labelKey: "stage.queued" },
  { status: "intake", labelKey: "stage.intake" },
  { status: "indexing", labelKey: "stage.indexing" },
  { status: "agent_pass", labelKey: "stage.agent_pass" },
  { status: "reconstructing", labelKey: "stage.reconstructing" },
  { status: "runtime_smoke", labelKey: "stage.runtime_smoke" },
  { status: "runtime_compare", labelKey: "stage.runtime_compare" },
  { status: "reviewing", labelKey: "stage.reviewing" },
  { status: "completed", labelKey: "stage.completed" }
];

export const reportArtifactKinds = new Set<Artifact["kind"]>([
  "audit_report",
  "html_report",
  "evidence_index",
  "result_package",
  "runtime_validation",
  "runtime_trace",
  "runtime_screenshot",
  "runtime_scenario",
  "runtime_comparison",
  "review_run",
  "build_artifact",
  "tool_call",
  "inference_record",
  "build_log"
]);

export const textualArtifactKinds = new Set<Artifact["kind"]>([
  "input_inventory",
  "source_index",
  "ast_index",
  "agent_plan",
  "inference_record",
  "reconstruction_plan",
  "build_log",
  "build_artifact",
  "runtime_validation",
  "runtime_trace",
  "runtime_scenario",
  "runtime_comparison",
  "review_run",
  "tool_call",
  "memory_record",
  "knowledge_evidence",
  "repair_instruction",
  "evidence_index",
  "audit_report"
]);

const orderedStatuses = [
  "queued",
  "intake",
  "indexing",
  "agent_pass",
  "reconstructing",
  "runtime_smoke",
  "runtime_compare",
  "reviewing",
  "completed",
  "completed_best_effort",
  "failed",
  "cancelled"
] as const satisfies readonly JobStatus[];

export const statusOrder = new Map<JobStatus, number>(orderedStatuses.map((status, index) => [status, index] as const));
