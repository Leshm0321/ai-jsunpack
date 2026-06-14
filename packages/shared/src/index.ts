export const JOB_STATUSES = [
  "queued",
  "leased",
  "intake",
  "planning",
  "parsing",
  "indexing",
  "analyzing",
  "agent_planning",
  "agent_pass",
  "reconstructing",
  "building",
  "typechecking",
  "runtime_smoke",
  "runtime_compare",
  "reviewing",
  "repairing",
  "packaging",
  "completed",
  "completed_best_effort",
  "failed",
  "cancelled"
] as const;

export type JobStatus = (typeof JOB_STATUSES)[number];

export const CLOUD_MODES = ["cloud_allowed", "local_only", "desensitized"] as const;
export type CloudMode = (typeof CLOUD_MODES)[number];

export const ARTIFACT_KINDS = [
  "input_inventory",
  "source_index",
  "ast_index",
  "agent_plan",
  "inference_record",
  "reconstruction_plan",
  "generated_project",
  "build_log",
  "runtime_validation",
  "review_run",
  "repair_instruction",
  "result_package",
  "audit_report"
] as const;

export type ArtifactKind = (typeof ARTIFACT_KINDS)[number];

export const FAILURE_CLASSES = [
  "none",
  "invalid_input",
  "parse_error",
  "agent_failed",
  "dependency_missing",
  "install_failed",
  "type_error",
  "build_error",
  "runtime_error",
  "sandbox_denied",
  "policy_denied",
  "timeout",
  "resource_limit",
  "unknown"
] as const;

export type FailureClass = (typeof FAILURE_CLASSES)[number];

export type SensitivityClass = "public" | "derived" | "source_sensitive" | "secret";
export type RetentionClass = "ephemeral" | "project" | "archive";

export interface Artifact {
  id: string;
  jobId: string;
  kind: ArtifactKind;
  stage: JobStatus;
  attempt: number;
  schemaVersion: string;
  contentType: string;
  hash: string;
  size: number;
  storageUri: string;
  parentArtifactIds: string[];
  producer: string;
  sensitivityClass: SensitivityClass;
  retentionClass: RetentionClass;
  createdAt: string;
}

export interface Job {
  id: string;
  status: JobStatus;
  ownerId: string;
  projectId: string;
  inputArtifactId?: string;
  config: Record<string, unknown>;
  cloudMode: CloudMode;
  reviewAttempt: number;
  workerLease?: {
    workerId: string;
    expiresAt: string;
  };
  failureClass: FailureClass;
  failureReason?: string;
  createdAt: string;
  updatedAt: string;
}

export interface EvidenceRef {
  artifactId: string;
  label: string;
  locator?: string;
  excerpt?: string;
}

export interface InferenceRecord {
  id: string;
  jobId: string;
  type: "naming" | "module_split" | "type_inference" | "framework" | "dead_code" | "runtime" | "repair";
  agentName: string;
  modelProvider: string;
  modelName: string;
  promptVersion: string;
  inputArtifactIds: string[];
  outputArtifactIds: string[];
  evidenceRefs: EvidenceRef[];
  confidence: number;
  uncertaintyReasons: string[];
  alternatives: string[];
  validationStatus: "unverified" | "accepted" | "rejected" | "needs_review";
  rollbackRef?: string;
}

export interface ReviewRun {
  id: string;
  jobId: string;
  attempt: number;
  reviewType: "build" | "typecheck" | "runtime_smoke" | "runtime_compare" | "agent_review";
  status: "pass" | "retry" | "best_effort" | "fail";
  decision: string;
  failureClass: FailureClass;
  evidenceRefs: EvidenceRef[];
  repairInstructionIds: string[];
  logsArtifactId?: string;
}

export interface RuntimeValidationRun {
  id: string;
  jobId: string;
  attempt: number;
  target: "original" | "reconstructed";
  entryUrl: string;
  status: "pass" | "retry" | "best_effort" | "fail";
  consoleErrors: string[];
  pageErrors: string[];
  failedRequests: string[];
  screenshotArtifactIds: string[];
  traceArtifactId?: string;
  comparisonArtifactId?: string;
}

export interface ToolCall {
  id: string;
  jobId: string;
  caller: string;
  toolName: string;
  toolVersion: string;
  inputArtifactIds: string[];
  outputArtifactIds: string[];
  status: "pass" | "fail";
  duration: number;
  failureClass: FailureClass;
}

export interface MemoryRecord {
  id: string;
  scope: "job" | "project" | "global";
  projectId: string;
  jobId?: string;
  memoryType: "short_term" | "long_term" | "entity" | "scenario";
  content: string;
  sourceArtifactIds: string[];
  sensitivityClass: SensitivityClass;
  retentionClass: RetentionClass;
}

export interface InputFileRecord {
  path: string;
  kind: "html" | "script" | "style" | "asset" | "source_map" | "manifest" | "unknown";
  size: number;
  hash: string;
}

export interface InputInventory {
  files: InputFileRecord[];
  entries: string[];
  scripts: string[];
  styles: string[];
  assets: string[];
  sourceMaps: string[];
  manifests: string[];
  isSingleBundle: boolean;
  warnings: string[];
}

export interface AstSymbolRecord {
  name: string;
  kind: string;
  loc?: string;
  references: number;
}

export interface AstIndex {
  filePath: string;
  sourceHash: string;
  symbols: AstSymbolRecord[];
  imports: string[];
  exports: string[];
  warnings: string[];
}

export interface HeadlessAnalysisResult {
  inventory: InputInventory;
  astIndexes: AstIndex[];
  detectedRuntime: string[];
  artifacts: Artifact[];
}

