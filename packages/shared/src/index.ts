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
  "source_input",
  "input_inventory",
  "source_index",
  "ast_index",
  "agent_plan",
  "agent_execution",
  "inference_record",
  "reconstruction_plan",
  "generated_project",
  "build_log",
  "build_artifact",
  "runtime_validation",
  "runtime_trace",
  "runtime_screenshot",
  "runtime_scenario",
  "runtime_comparison",
  "review_run",
  "tool_call",
  "tool_registry",
  "memory_record",
  "knowledge_evidence",
  "repair_instruction",
  "runtime_diagnosis",
  "report_section",
  "ops_alert_event",
  "result_package",
  "audit_report",
  "html_report",
  "evidence_index"
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

export const SENSITIVITY_CLASSES = ["public", "derived", "source_sensitive", "secret"] as const;
export type SensitivityClass = (typeof SENSITIVITY_CLASSES)[number];

export const RETENTION_CLASSES = ["ephemeral", "project", "archive"] as const;
export type RetentionClass = (typeof RETENTION_CLASSES)[number];

export const RETENTION_CATEGORIES = ["source", "derived", "package", "logs", "screenshots", "memory"] as const;
export type RetentionCategory = (typeof RETENTION_CATEGORIES)[number];

export const SANDBOX_RESOURCE_ENFORCEMENTS = [
  "local_best_effort",
  "container_enforced",
  "runtime_isolated",
  "remote_isolated"
] as const;
export type SandboxResourceEnforcement = (typeof SANDBOX_RESOURCE_ENFORCEMENTS)[number];

export const SANDBOX_RUNNER_KINDS = [
  "local",
  "container",
  "gvisor",
  "firecracker",
  "remote_browser_runner"
] as const;
export type SandboxRunnerKind = (typeof SANDBOX_RUNNER_KINDS)[number];

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
  expiresAt?: string | null;
  deletedAt?: string | null;
  deletionReason?: string | null;
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
  runAttempt: number;
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

export interface TypeScriptRelatedInformation {
  message: string;
  filePath?: string | null;
  line?: number | null;
  column?: number | null;
  code?: string | null;
}

export interface TypeScriptDiagnostic {
  source: "stdout" | "stderr";
  tool?: "tsc" | "vite" | "esbuild" | "unknown";
  category: "error" | "warning" | "message" | "suggestion" | "unknown";
  code?: string | null;
  message: string;
  filePath?: string | null;
  line?: number | null;
  column?: number | null;
  contextLines?: string[];
  relatedInformation?: TypeScriptRelatedInformation[];
}

export interface SandboxRuntimeCapability {
  name: "network" | "process" | "cpu" | "memory" | "filesystem";
  status: "enforced" | "best_effort" | "unsupported" | "unknown";
  detail: string;
}

export interface SandboxResourcePolicy {
  processLimit?: number | null;
  cpuTimeLimitMs?: number | null;
  memoryLimitBytes?: number | null;
  enforcement: SandboxResourceEnforcement;
  runnerKind: SandboxRunnerKind;
  runtimeName?: string | null;
  runtimeVersion?: string | null;
  hostPlatform: string;
  capabilities: SandboxRuntimeCapability[];
  limitations: string[];
}

export interface BuildArtifact {
  id: string;
  jobId: string;
  stage: "building" | "typechecking";
  reviewType: "build" | "typecheck" | "runtime_smoke" | "runtime_compare" | "agent_review";
  phase: "install" | "build" | "typecheck";
  attempt: number;
  status: "pass" | "retry" | "best_effort" | "fail";
  decision: string;
  command: string[];
  commandSource: "configured" | "npm_script" | "fallback_shim" | "npm_install" | "missing";
  scriptName?: string | null;
  packageManager?: string | null;
  exitCode?: number | null;
  durationMs: number;
  failureClass: FailureClass;
  timedOut: boolean;
  outputTruncated: boolean;
  workingDirectory?: string | null;
  networkPolicy: "deny" | "allow";
  resourcePolicy: SandboxResourcePolicy;
  diagnostics: TypeScriptDiagnostic[];
  logsArtifactId?: string | null;
  repairInstructionIds: string[];
  limitations: string[];
}

export interface RuntimeWaitFor {
  kind: "load_state" | "selector" | "timeout";
  selector?: string | null;
  state?: "load" | "domcontentloaded" | "networkidle" | null;
  timeoutMs?: number | null;
}

export interface RuntimeInteraction {
  action: "click" | "fill" | "press" | "wait";
  selector?: string | null;
  value?: string | null;
  key?: string | null;
  timeoutMs?: number | null;
}

export interface RuntimeAssertion {
  kind: "selector_visible" | "text_contains" | "url_contains";
  selector?: string | null;
  text?: string | null;
  value?: string | null;
}

export interface RuntimeScenario {
  id: string;
  jobId: string;
  name: string;
  entryUrl?: string | null;
  viewport?: RuntimeViewport | null;
  waitFor: RuntimeWaitFor[];
  interactions: RuntimeInteraction[];
  assertions: RuntimeAssertion[];
  networkPolicy: "deny" | "allow";
  timeoutMs: number;
}

export interface RuntimeCaptureSummary {
  target: "original" | "reconstructed";
  entryUrl: string;
  status: "pass" | "retry" | "best_effort" | "fail";
  failureClass: FailureClass;
  consoleMessages: string[];
  consoleErrors: string[];
  pageErrors: string[];
  failedRequests: string[];
  responses: string[];
  assertionFailures: string[];
  domSummary: Record<string, unknown>;
  screenshotArtifactId?: string | null;
  durationMs: number;
  limitations: string[];
}

export interface RuntimeScreenshotDiff {
  changed?: boolean | null;
  originalHash?: string | null;
  reconstructedHash?: string | null;
  originalSizeBytes?: number | null;
  reconstructedSizeBytes?: number | null;
  originalFormat?: string | null;
  reconstructedFormat?: string | null;
  pixelDiffStatus: "compared" | "unavailable";
  pixelCount?: number | null;
  changedPixelCount?: number | null;
  changedPixelRatio?: number | null;
  threshold?: number | null;
  thresholdMode?: string | null;
  maxChangedPixelRatio?: number | null;
  width?: number | null;
  height?: number | null;
  diffArtifactId?: string | null;
  reason?: string | null;
}

export interface RuntimeDomDifference {
  path: string;
  original: unknown;
  reconstructed: unknown;
  summary: string;
}

export interface RuntimeCollectionDiff {
  changed: boolean;
  originalCount: number;
  reconstructedCount: number;
  shared: string[];
  originalOnly: string[];
  reconstructedOnly: string[];
  groups: Record<string, string[]>;
}

export interface RuntimeViewport {
  name?: string | null;
  width: number;
  height: number;
}

export interface RuntimeComparisonScope {
  scenarioName: string;
  networkPolicy: "deny" | "allow";
  timeoutMs: number;
  viewport?: RuntimeViewport | null;
}

export interface RuntimeDifferenceSet {
  screenshotChanged?: boolean | null;
  domChanged: boolean;
  networkChanged: boolean;
  consoleChanged: boolean;
  originalOnlyRequests: string[];
  reconstructedOnlyRequests: string[];
  originalOnlyConsole: string[];
  reconstructedOnlyConsole: string[];
  changedDomFields: string[];
  screenshotDiff: RuntimeScreenshotDiff;
  domDifferences: RuntimeDomDifference[];
  networkDiff: RuntimeCollectionDiff;
  consoleDiff: RuntimeCollectionDiff;
  comparisonScope: RuntimeComparisonScope;
}

export interface RuntimeComparisonReport {
  id: string;
  jobId: string;
  attempt: number;
  status: "pass" | "retry" | "best_effort" | "fail";
  scenarioArtifactId: string;
  original: RuntimeCaptureSummary;
  reconstructed: RuntimeCaptureSummary;
  differences: RuntimeDifferenceSet;
  screenshotArtifactIds: string[];
  traceArtifactIds: string[];
  limitations: string[];
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

export interface BrowserRunSourceArchive {
  contentBase64: string;
  entryPath: string;
}

export interface BrowserRunRequest {
  jobId: string;
  target: "original" | "reconstructed";
  attempt: number;
  entryUrl: string;
  timeoutMs: number;
  waitForSelector?: string | null;
  scenario?: RuntimeScenario | null;
  networkPolicy: "deny" | "allow";
  viewport?: RuntimeViewport | null;
  sourceArchive?: BrowserRunSourceArchive | null;
}

export interface BrowserRunResult {
  status: "pass" | "retry" | "best_effort" | "fail";
  failureClass: FailureClass;
  consoleMessages: string[];
  consoleErrors: string[];
  pageErrors: string[];
  failedRequests: string[];
  responses: string[];
  assertionFailures: string[];
  domSummary: Record<string, unknown>;
  screenshotBase64?: string | null;
  limitations: string[];
  executionBoundary: Record<string, unknown>;
}

export interface BrowserRunSummary {
  id: string;
  status: "queued" | "running" | "pass" | "fail" | "best_effort";
  result?: BrowserRunResult | null;
  error?: string | null;
  createdAt: string;
  updatedAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  attempt?: number;
  maxAttempts?: number;
  leaseOwner?: string | null;
  leaseExpiresAt?: string | null;
  nextRunAt?: string | null;
  workerId?: string | null;
  queueBackend?: string | null;
  leaseRecovered?: boolean;
}

export interface BrowserRunnerQueueMetrics {
  checkedAt: string;
  queueBackend: string;
  backendStatus: "ok" | "degraded";
  backendError?: string | null;
  queuedCount: number;
  runningCount: number;
  terminalCount: number;
  totalCount: number;
  oldestQueuedAgeMs?: number | null;
  claimLatencyMs?: number | null;
  averageRunDurationMs?: number | null;
  retryRate: number;
  leaseRecoveryCount: number;
  expiredRunningCount: number;
}

export interface BrowserRunnerQueueAlert {
  code: string;
  severity: "warning" | "critical";
  message: string;
  field: string;
  value?: unknown;
  threshold?: unknown;
}

export interface BrowserRunnerQueueHealth {
  status: "ok" | "degraded";
  serviceRole: string;
  deploymentProfile?: string | null;
  workerId: string;
  maxWorkers: number;
  maxAttempts: number;
  leaseSeconds: number;
  retryBackoffSeconds: number;
  pollSeconds: number;
  metrics: BrowserRunnerQueueMetrics;
  alerts: BrowserRunnerQueueAlert[];
}

export interface OpsAlert {
  code: string;
  severity: "warning" | "critical";
  message: string;
  field: string;
  value?: unknown;
  threshold?: unknown;
  serviceRole?: string | null;
  instanceId?: string | null;
  checkedAt?: string | null;
}

export interface OpsHeartbeatRequest {
  serviceRole: string;
  instanceId: string;
  status: "ok" | "degraded";
  ttlSeconds: number;
  metrics: Record<string, unknown>;
  alerts: OpsAlert[];
  metadata: Record<string, unknown>;
  checkedAt?: string | null;
}

export interface OpsHeartbeatRecord {
  serviceRole: string;
  instanceId: string;
  status: "ok" | "degraded";
  checkedAt: string;
  expiresAt: string;
  metrics: Record<string, unknown>;
  alerts: OpsAlert[];
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export interface OpsMetricsSnapshot {
  checkedAt: string;
  serviceRole: string;
  deploymentProfile?: string | null;
  jobStatusCounts: Record<string, number>;
  activeHeartbeatCount: number;
  staleHeartbeatCount: number;
  serviceHeartbeatCounts: Record<string, number>;
  metrics: Record<string, unknown>;
  alerts: OpsAlert[];
}

export interface OpsAlertDelivery {
  status: "not_configured" | "delivered" | "failed";
  attempted: boolean;
  webhookUrlConfigured: boolean;
  eventId?: string | null;
  deliveredAt?: string | null;
  error?: string | null;
}

export interface OpsAlertRule {
  code: string;
  severity: "warning" | "critical";
  metricPath: string;
  operator: "gt" | "gte" | "lt" | "lte" | "eq" | "neq";
  threshold: unknown;
  message: string;
  serviceRole?: string | null;
  enabled: boolean;
  source: "default" | "env";
}

export interface OpsAlertEvent {
  id: string;
  checkedAt: string;
  status: "active" | "resolved";
  severity: "warning" | "critical";
  code: string;
  message: string;
  field: string;
  value?: unknown;
  threshold?: unknown;
  serviceRole?: string | null;
  instanceId?: string | null;
  rule?: OpsAlertRule | null;
  alerts: OpsAlert[];
  metrics: Record<string, unknown>;
  delivery: OpsAlertDelivery;
  createdAt: string;
  updatedAt: string;
}

export interface OpsAlertResponse {
  checkedAt: string;
  alerts: OpsAlert[];
  delivery: OpsAlertDelivery;
  events: OpsAlertEvent[];
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

export interface ToolRegistryEntry {
  id: string;
  jobId: string;
  toolName: string;
  toolVersion: string;
  category: "code" | "build" | "runtime" | "audit" | "knowledge" | "memory" | "model";
  caller: string;
  inputArtifactKinds: ArtifactKind[];
  outputArtifactKinds: ArtifactKind[];
  failureClasses: FailureClass[];
  description: string;
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

export interface RuntimeDiagnosis {
  id: string;
  jobId: string;
  attempt: number;
  agentName: string;
  targetStage: string;
  status: "pass" | "retry" | "best_effort" | "fail";
  failureClass: FailureClass;
  inputArtifactIds: string[];
  evidenceRefs: EvidenceRef[];
  diagnosis: string;
  recommendedActions: string[];
  confidence: number;
  uncertaintyReasons: string[];
}

export interface ReportSectionDetail {
  label: string;
  value: string;
  status?: "pass" | "retry" | "best_effort" | "fail";
  details?: Record<string, unknown>;
}

export interface ReportSection {
  id: string;
  jobId: string;
  agentName: string;
  title: string;
  anchor: string;
  summary: string;
  content: string;
  inputArtifactIds: string[];
  evidenceRefs: EvidenceRef[];
  status: "pass" | "retry" | "best_effort" | "fail";
  confidence: number;
  uncertaintyReasons: string[];
  details: ReportSectionDetail[];
}

export interface RetentionCleanupRequest {
  dryRun: boolean;
  categories: RetentionCategory[];
  retentionClasses: RetentionClass[];
  deleteExpired: boolean;
  reason: string;
  now?: string | null;
}

export interface RetentionCleanupItem {
  artifactId: string;
  kind: ArtifactKind;
  category: RetentionCategory;
  retentionClass: RetentionClass;
  storageUri: string;
  deleted: boolean;
  reason: string;
  error?: string | null;
}

export interface RetentionCleanupResult {
  jobId: string;
  dryRun: boolean;
  requestedAt: string;
  candidateCount: number;
  deletedCount: number;
  skippedCount: number;
  errorCount: number;
  items: RetentionCleanupItem[];
  errors: string[];
}

export interface RepairAction {
  action: "add_package_script" | "replace_package_script" | "mirror_original_static_entry";
  path: string;
  value: string;
  reason: string;
}

export interface RepairInstruction {
  id: string;
  jobId: string;
  attempt: number;
  targetStage: "building" | "typechecking" | "runtime_smoke" | "runtime_compare";
  failureClass: FailureClass;
  inputArtifactIds: string[];
  evidenceRefs: EvidenceRef[];
  actions: RepairAction[];
  status: "planned" | "applied" | "skipped";
  riskLevel: "low" | "medium" | "high";
  decision: string;
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

export const CONTRACT_SCHEMA_VERSION = "2026-06-14";

export type JsonSchema = {
  $schema?: string;
  $id?: string;
  title?: string;
  type?: "array" | "boolean" | "integer" | "number" | "object" | "string";
  properties?: Record<string, JsonSchema>;
  required?: readonly string[];
  enum?: readonly string[];
  items?: JsonSchema;
  additionalProperties?: boolean | JsonSchema;
  format?: string;
  minimum?: number;
  maximum?: number;
};

const stringSchema = { type: "string" } as const satisfies JsonSchema;
const numberSchema = { type: "number" } as const satisfies JsonSchema;
const booleanSchema = { type: "boolean" } as const satisfies JsonSchema;
const stringArraySchema = { type: "array", items: stringSchema } as const satisfies JsonSchema;
const retentionCategoriesArraySchema = {
  type: "array",
  items: { type: "string", enum: RETENTION_CATEGORIES }
} as const satisfies JsonSchema;
const retentionClassesArraySchema = {
  type: "array",
  items: { type: "string", enum: RETENTION_CLASSES }
} as const satisfies JsonSchema;
const evidenceRefsSchema = {
  type: "array",
  items: {
    type: "object",
    properties: {
      artifactId: stringSchema,
      label: stringSchema,
      locator: stringSchema,
      excerpt: stringSchema
    },
    required: ["artifactId", "label"],
    additionalProperties: false
  }
} as const satisfies JsonSchema;
const reportSectionDetailsSchema = {
  type: "array",
  items: {
    type: "object",
    properties: {
      label: stringSchema,
      value: stringSchema,
      status: { type: "string", enum: ["pass", "retry", "best_effort", "fail"] },
      details: { type: "object", additionalProperties: true }
    },
    required: ["label", "value"],
    additionalProperties: false
  }
} as const satisfies JsonSchema;
const repairActionsSchema = {
  type: "array",
  items: {
    type: "object",
    properties: {
      action: { type: "string", enum: ["add_package_script", "replace_package_script", "mirror_original_static_entry"] },
      path: stringSchema,
      value: stringSchema,
      reason: stringSchema
    },
    required: ["action", "path", "value", "reason"],
    additionalProperties: false
  }
} as const satisfies JsonSchema;
const artifactKindArraySchema = {
  type: "array",
  items: { type: "string", enum: ARTIFACT_KINDS }
} as const satisfies JsonSchema;
const failureClassArraySchema = {
  type: "array",
  items: { type: "string", enum: FAILURE_CLASSES }
} as const satisfies JsonSchema;
const sandboxRuntimeCapabilitiesSchema = {
  type: "array",
  items: {
    type: "object",
    properties: {
      name: { type: "string", enum: ["network", "process", "cpu", "memory", "filesystem"] },
      status: { type: "string", enum: ["enforced", "best_effort", "unsupported", "unknown"] },
      detail: stringSchema
    },
    required: ["name", "status", "detail"],
    additionalProperties: false
  }
} as const satisfies JsonSchema;
const sandboxResourcePolicySchema = {
  type: "object",
  properties: {
    processLimit: { type: "integer", minimum: 1 },
    cpuTimeLimitMs: { type: "integer", minimum: 1 },
    memoryLimitBytes: { type: "integer", minimum: 1 },
    enforcement: { type: "string", enum: SANDBOX_RESOURCE_ENFORCEMENTS },
    runnerKind: { type: "string", enum: SANDBOX_RUNNER_KINDS },
    runtimeName: stringSchema,
    runtimeVersion: stringSchema,
    hostPlatform: stringSchema,
    capabilities: sandboxRuntimeCapabilitiesSchema,
    limitations: stringArraySchema
  },
  required: ["enforcement", "runnerKind", "hostPlatform", "capabilities", "limitations"],
  additionalProperties: false
} as const satisfies JsonSchema;
const typeScriptRelatedInformationSchema = {
  type: "array",
  items: {
    type: "object",
    properties: {
      message: stringSchema,
      filePath: stringSchema,
      line: { type: "integer", minimum: 1 },
      column: { type: "integer", minimum: 1 },
      code: stringSchema
    },
    required: ["message"],
    additionalProperties: false
  }
} as const satisfies JsonSchema;
const typeScriptDiagnosticsSchema = {
  type: "array",
  items: {
    type: "object",
    properties: {
      source: { type: "string", enum: ["stdout", "stderr"] },
      tool: { type: "string", enum: ["tsc", "vite", "esbuild", "unknown"] },
      category: { type: "string", enum: ["error", "warning", "message", "suggestion", "unknown"] },
      code: stringSchema,
      message: stringSchema,
      filePath: stringSchema,
      line: { type: "integer", minimum: 1 },
      column: { type: "integer", minimum: 1 },
      contextLines: stringArraySchema,
      relatedInformation: typeScriptRelatedInformationSchema
    },
    required: ["source", "category", "message"],
    additionalProperties: false
  }
} as const satisfies JsonSchema;
const runtimeWaitForSchema = {
  type: "array",
  items: {
    type: "object",
    properties: {
      kind: { type: "string", enum: ["load_state", "selector", "timeout"] },
      selector: stringSchema,
      state: { type: "string", enum: ["load", "domcontentloaded", "networkidle"] },
      timeoutMs: { type: "integer", minimum: 1 }
    },
    required: ["kind"],
    additionalProperties: false
  }
} as const satisfies JsonSchema;
const runtimeInteractionsSchema = {
  type: "array",
  items: {
    type: "object",
    properties: {
      action: { type: "string", enum: ["click", "fill", "press", "wait"] },
      selector: stringSchema,
      value: stringSchema,
      key: stringSchema,
      timeoutMs: { type: "integer", minimum: 1 }
    },
    required: ["action"],
    additionalProperties: false
  }
} as const satisfies JsonSchema;
const runtimeAssertionsSchema = {
  type: "array",
  items: {
    type: "object",
    properties: {
      kind: { type: "string", enum: ["selector_visible", "text_contains", "url_contains"] },
      selector: stringSchema,
      text: stringSchema,
      value: stringSchema
    },
    required: ["kind"],
    additionalProperties: false
  }
} as const satisfies JsonSchema;
const runtimeDomSummarySchema = {
  type: "object",
  additionalProperties: true
} as const satisfies JsonSchema;
const runtimeViewportSchema = {
  type: "object",
  properties: {
    name: stringSchema,
    width: { type: "integer", minimum: 1 },
    height: { type: "integer", minimum: 1 }
  },
  required: ["width", "height"],
  additionalProperties: false
} as const satisfies JsonSchema;
const runtimeCaptureSummarySchema = {
  type: "object",
  properties: {
    target: { type: "string", enum: ["original", "reconstructed"] },
    entryUrl: stringSchema,
    status: { type: "string", enum: ["pass", "retry", "best_effort", "fail"] },
    failureClass: { type: "string", enum: FAILURE_CLASSES },
    consoleMessages: stringArraySchema,
    consoleErrors: stringArraySchema,
    pageErrors: stringArraySchema,
    failedRequests: stringArraySchema,
    responses: stringArraySchema,
    assertionFailures: stringArraySchema,
    domSummary: runtimeDomSummarySchema,
    screenshotArtifactId: stringSchema,
    durationMs: { type: "integer", minimum: 0 },
    limitations: stringArraySchema
  },
  required: [
    "target",
    "entryUrl",
    "status",
    "failureClass",
    "consoleMessages",
    "consoleErrors",
    "pageErrors",
    "failedRequests",
    "responses",
    "assertionFailures",
    "domSummary",
    "durationMs",
    "limitations"
  ],
  additionalProperties: false
} as const satisfies JsonSchema;
const browserRunSourceArchiveSchema = {
  type: "object",
  properties: {
    contentBase64: stringSchema,
    entryPath: stringSchema
  },
  required: ["contentBase64", "entryPath"],
  additionalProperties: false
} as const satisfies JsonSchema;
const browserRunResultSchema = {
  type: "object",
  properties: {
    status: { type: "string", enum: ["pass", "retry", "best_effort", "fail"] },
    failureClass: { type: "string", enum: FAILURE_CLASSES },
    consoleMessages: stringArraySchema,
    consoleErrors: stringArraySchema,
    pageErrors: stringArraySchema,
    failedRequests: stringArraySchema,
    responses: stringArraySchema,
    assertionFailures: stringArraySchema,
    domSummary: runtimeDomSummarySchema,
    screenshotBase64: stringSchema,
    limitations: stringArraySchema,
    executionBoundary: { type: "object", additionalProperties: true }
  },
  required: [
    "status",
    "failureClass",
    "consoleMessages",
    "consoleErrors",
    "pageErrors",
    "failedRequests",
    "responses",
    "assertionFailures",
    "domSummary",
    "limitations",
    "executionBoundary"
  ],
  additionalProperties: false
} as const satisfies JsonSchema;
const browserRunnerQueueMetricsSchema = {
  type: "object",
  properties: {
    checkedAt: { type: "string", format: "date-time" },
    queueBackend: stringSchema,
    backendStatus: { type: "string", enum: ["ok", "degraded"] },
    backendError: stringSchema,
    queuedCount: { type: "integer", minimum: 0 },
    runningCount: { type: "integer", minimum: 0 },
    terminalCount: { type: "integer", minimum: 0 },
    totalCount: { type: "integer", minimum: 0 },
    oldestQueuedAgeMs: { type: "integer", minimum: 0 },
    claimLatencyMs: { type: "integer", minimum: 0 },
    averageRunDurationMs: { type: "integer", minimum: 0 },
    retryRate: { type: "number", minimum: 0 },
    leaseRecoveryCount: { type: "integer", minimum: 0 },
    expiredRunningCount: { type: "integer", minimum: 0 }
  },
  required: [
    "checkedAt",
    "queueBackend",
    "backendStatus",
    "queuedCount",
    "runningCount",
    "terminalCount",
    "totalCount",
    "retryRate",
    "leaseRecoveryCount",
    "expiredRunningCount"
  ],
  additionalProperties: false
} as const satisfies JsonSchema;
const browserRunnerQueueAlertSchema = {
  type: "object",
  properties: {
    code: stringSchema,
    severity: { type: "string", enum: ["warning", "critical"] },
    message: stringSchema,
    field: stringSchema,
    value: {},
    threshold: {}
  },
  required: ["code", "severity", "message", "field"],
  additionalProperties: false
} as const satisfies JsonSchema;
const opsAlertSchema = {
  type: "object",
  properties: {
    code: stringSchema,
    severity: { type: "string", enum: ["warning", "critical"] },
    message: stringSchema,
    field: stringSchema,
    value: {},
    threshold: {},
    serviceRole: stringSchema,
    instanceId: stringSchema,
    checkedAt: stringSchema
  },
  required: ["code", "severity", "message", "field"],
  additionalProperties: false
} as const satisfies JsonSchema;
const opsMetricsMapSchema = {
  type: "object",
  additionalProperties: true
} as const satisfies JsonSchema;
const opsIntegerMapSchema = {
  type: "object",
  additionalProperties: {
    type: "integer",
    minimum: 0
  }
} as const satisfies JsonSchema;
const opsAlertDeliverySchema = {
  type: "object",
  properties: {
    status: { type: "string", enum: ["not_configured", "delivered", "failed"] },
    attempted: booleanSchema,
    webhookUrlConfigured: booleanSchema,
    eventId: stringSchema,
    deliveredAt: stringSchema,
    error: stringSchema
  },
  required: ["status", "attempted", "webhookUrlConfigured"],
  additionalProperties: false
} as const satisfies JsonSchema;
const opsAlertRuleSchema = {
  type: "object",
  properties: {
    code: stringSchema,
    severity: { type: "string", enum: ["warning", "critical"] },
    metricPath: stringSchema,
    operator: { type: "string", enum: ["gt", "gte", "lt", "lte", "eq", "neq"] },
    threshold: {},
    message: stringSchema,
    serviceRole: stringSchema,
    enabled: booleanSchema,
    source: { type: "string", enum: ["default", "env"] }
  },
  required: ["code", "severity", "metricPath", "operator", "threshold", "message", "enabled", "source"],
  additionalProperties: false
} as const satisfies JsonSchema;
const opsAlertEventSchema = {
  type: "object",
  properties: {
    id: stringSchema,
    checkedAt: stringSchema,
    status: { type: "string", enum: ["active", "resolved"] },
    severity: { type: "string", enum: ["warning", "critical"] },
    code: stringSchema,
    message: stringSchema,
    field: stringSchema,
    value: {},
    threshold: {},
    serviceRole: stringSchema,
    instanceId: stringSchema,
    rule: opsAlertRuleSchema,
    alerts: { type: "array", items: opsAlertSchema },
    metrics: opsMetricsMapSchema,
    delivery: opsAlertDeliverySchema,
    createdAt: stringSchema,
    updatedAt: stringSchema
  },
  required: [
    "id",
    "checkedAt",
    "status",
    "severity",
    "code",
    "message",
    "field",
    "alerts",
    "metrics",
    "delivery",
    "createdAt",
    "updatedAt"
  ],
  additionalProperties: false
} as const satisfies JsonSchema;
const runtimeScreenshotDiffSchema = {
  type: "object",
  properties: {
    changed: booleanSchema,
    originalHash: stringSchema,
    reconstructedHash: stringSchema,
    originalSizeBytes: { type: "integer", minimum: 0 },
    reconstructedSizeBytes: { type: "integer", minimum: 0 },
    originalFormat: stringSchema,
    reconstructedFormat: stringSchema,
    pixelDiffStatus: { type: "string", enum: ["compared", "unavailable"] },
    pixelCount: { type: "integer", minimum: 0 },
    changedPixelCount: { type: "integer", minimum: 0 },
    changedPixelRatio: { type: "number", minimum: 0 },
    threshold: { type: "integer", minimum: 0 },
    thresholdMode: stringSchema,
    maxChangedPixelRatio: { type: "number", minimum: 0 },
    width: { type: "integer", minimum: 1 },
    height: { type: "integer", minimum: 1 },
    diffArtifactId: stringSchema,
    reason: stringSchema
  },
  required: ["pixelDiffStatus"],
  additionalProperties: false
} as const satisfies JsonSchema;
const runtimeDomDifferencesSchema = {
  type: "array",
  items: {
    type: "object",
    properties: {
      path: stringSchema,
      original: {},
      reconstructed: {},
      summary: stringSchema
    },
    required: ["path", "original", "reconstructed", "summary"],
    additionalProperties: false
  }
} as const satisfies JsonSchema;
const runtimeCollectionDiffSchema = {
  type: "object",
  properties: {
    changed: booleanSchema,
    originalCount: { type: "integer", minimum: 0 },
    reconstructedCount: { type: "integer", minimum: 0 },
    shared: stringArraySchema,
    originalOnly: stringArraySchema,
    reconstructedOnly: stringArraySchema,
    groups: {
      type: "object",
      additionalProperties: {
        type: "array",
        items: stringSchema
      }
    }
  },
  required: ["changed", "originalCount", "reconstructedCount", "shared", "originalOnly", "reconstructedOnly", "groups"],
  additionalProperties: false
} as const satisfies JsonSchema;
const runtimeComparisonScopeSchema = {
  type: "object",
  properties: {
    scenarioName: stringSchema,
    networkPolicy: { type: "string", enum: ["deny", "allow"] },
    timeoutMs: { type: "integer", minimum: 1 },
    viewport: runtimeViewportSchema
  },
  required: ["scenarioName", "networkPolicy", "timeoutMs"],
  additionalProperties: false
} as const satisfies JsonSchema;
const runtimeDifferenceSetSchema = {
  type: "object",
  properties: {
    screenshotChanged: booleanSchema,
    domChanged: booleanSchema,
    networkChanged: booleanSchema,
    consoleChanged: booleanSchema,
    originalOnlyRequests: stringArraySchema,
    reconstructedOnlyRequests: stringArraySchema,
    originalOnlyConsole: stringArraySchema,
    reconstructedOnlyConsole: stringArraySchema,
    changedDomFields: stringArraySchema,
    screenshotDiff: runtimeScreenshotDiffSchema,
    domDifferences: runtimeDomDifferencesSchema,
    networkDiff: runtimeCollectionDiffSchema,
    consoleDiff: runtimeCollectionDiffSchema,
    comparisonScope: runtimeComparisonScopeSchema
  },
  required: [
    "domChanged",
    "networkChanged",
    "consoleChanged",
    "originalOnlyRequests",
    "reconstructedOnlyRequests",
    "originalOnlyConsole",
    "reconstructedOnlyConsole",
    "changedDomFields",
    "screenshotDiff",
    "domDifferences",
    "networkDiff",
    "consoleDiff",
    "comparisonScope"
  ],
  additionalProperties: false
} as const satisfies JsonSchema;

export const SHARED_CONTRACT_ENUMS = {
  jobStatus: JOB_STATUSES,
  cloudMode: CLOUD_MODES,
  artifactKind: ARTIFACT_KINDS,
  failureClass: FAILURE_CLASSES,
  sensitivityClass: SENSITIVITY_CLASSES,
  retentionClass: RETENTION_CLASSES,
  retentionCategory: RETENTION_CATEGORIES,
  sandboxResourceEnforcement: SANDBOX_RESOURCE_ENFORCEMENTS,
  sandboxRunnerKind: SANDBOX_RUNNER_KINDS
} as const;

export const SHARED_JSON_SCHEMAS = {
  evidenceRef: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/evidence-ref.json",
    title: "EvidenceRef",
    type: "object",
    properties: {
      artifactId: stringSchema,
      label: stringSchema,
      locator: stringSchema,
      excerpt: stringSchema
    },
    required: ["artifactId", "label"],
    additionalProperties: false
  },
  artifact: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/artifact.json",
    title: "Artifact",
    type: "object",
    properties: {
      id: stringSchema,
      jobId: stringSchema,
      kind: { type: "string", enum: ARTIFACT_KINDS },
      stage: { type: "string", enum: JOB_STATUSES },
      attempt: { type: "integer", minimum: 0 },
      schemaVersion: stringSchema,
      contentType: stringSchema,
      hash: stringSchema,
      size: { type: "integer", minimum: 0 },
      storageUri: stringSchema,
      parentArtifactIds: stringArraySchema,
      producer: stringSchema,
      sensitivityClass: { type: "string", enum: SENSITIVITY_CLASSES },
      retentionClass: { type: "string", enum: RETENTION_CLASSES },
      createdAt: { type: "string", format: "date-time" },
      expiresAt: { type: "string", format: "date-time" },
      deletedAt: { type: "string", format: "date-time" },
      deletionReason: stringSchema
    },
    required: [
      "id",
      "jobId",
      "kind",
      "stage",
      "attempt",
      "schemaVersion",
      "contentType",
      "hash",
      "size",
      "storageUri",
      "parentArtifactIds",
      "producer",
      "sensitivityClass",
      "retentionClass",
      "createdAt"
    ],
    additionalProperties: false
  },
  retentionCleanupRequest: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/retention-cleanup-request.json",
    title: "RetentionCleanupRequest",
    type: "object",
    properties: {
      dryRun: booleanSchema,
      categories: retentionCategoriesArraySchema,
      retentionClasses: retentionClassesArraySchema,
      deleteExpired: booleanSchema,
      reason: stringSchema,
      now: { type: "string", format: "date-time" }
    },
    required: ["dryRun", "categories", "retentionClasses", "deleteExpired", "reason"],
    additionalProperties: false
  },
  retentionCleanupResult: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/retention-cleanup-result.json",
    title: "RetentionCleanupResult",
    type: "object",
    properties: {
      jobId: stringSchema,
      dryRun: booleanSchema,
      requestedAt: { type: "string", format: "date-time" },
      candidateCount: { type: "integer", minimum: 0 },
      deletedCount: { type: "integer", minimum: 0 },
      skippedCount: { type: "integer", minimum: 0 },
      errorCount: { type: "integer", minimum: 0 },
      items: {
        type: "array",
        items: {
          type: "object",
          properties: {
            artifactId: stringSchema,
            kind: { type: "string", enum: ARTIFACT_KINDS },
            category: { type: "string", enum: RETENTION_CATEGORIES },
            retentionClass: { type: "string", enum: RETENTION_CLASSES },
            storageUri: stringSchema,
            deleted: booleanSchema,
            reason: stringSchema,
            error: stringSchema
          },
          required: ["artifactId", "kind", "category", "retentionClass", "storageUri", "deleted", "reason"],
          additionalProperties: false
        }
      },
      errors: stringArraySchema
    },
    required: [
      "jobId",
      "dryRun",
      "requestedAt",
      "candidateCount",
      "deletedCount",
      "skippedCount",
      "errorCount",
      "items",
      "errors"
    ],
    additionalProperties: false
  },
  job: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/job.json",
    title: "Job",
    type: "object",
    properties: {
      id: stringSchema,
      status: { type: "string", enum: JOB_STATUSES },
      ownerId: stringSchema,
      projectId: stringSchema,
      inputArtifactId: stringSchema,
      config: { type: "object", additionalProperties: true },
      cloudMode: { type: "string", enum: CLOUD_MODES },
      reviewAttempt: { type: "integer", minimum: 0 },
      runAttempt: { type: "integer", minimum: 0 },
      workerLease: {
        type: "object",
        properties: {
          workerId: stringSchema,
          expiresAt: { type: "string", format: "date-time" }
        },
        required: ["workerId", "expiresAt"],
        additionalProperties: false
      },
      failureClass: { type: "string", enum: FAILURE_CLASSES },
      failureReason: stringSchema,
      createdAt: { type: "string", format: "date-time" },
      updatedAt: { type: "string", format: "date-time" }
    },
    required: [
      "id",
      "status",
      "ownerId",
      "projectId",
      "config",
      "cloudMode",
      "reviewAttempt",
      "runAttempt",
      "failureClass",
      "createdAt",
      "updatedAt"
    ],
    additionalProperties: false
  },
  inferenceRecord: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/inference-record.json",
    title: "InferenceRecord",
    type: "object",
    properties: {
      id: stringSchema,
      jobId: stringSchema,
      type: {
        type: "string",
        enum: ["naming", "module_split", "type_inference", "framework", "dead_code", "runtime", "repair"]
      },
      agentName: stringSchema,
      modelProvider: stringSchema,
      modelName: stringSchema,
      promptVersion: stringSchema,
      inputArtifactIds: stringArraySchema,
      outputArtifactIds: stringArraySchema,
      evidenceRefs: evidenceRefsSchema,
      confidence: { type: "number", minimum: 0, maximum: 1 },
      uncertaintyReasons: stringArraySchema,
      alternatives: stringArraySchema,
      validationStatus: { type: "string", enum: ["unverified", "accepted", "rejected", "needs_review"] },
      rollbackRef: stringSchema
    },
    required: [
      "id",
      "jobId",
      "type",
      "agentName",
      "modelProvider",
      "modelName",
      "promptVersion",
      "inputArtifactIds",
      "outputArtifactIds",
      "evidenceRefs",
      "confidence",
      "uncertaintyReasons",
      "alternatives",
      "validationStatus"
    ],
    additionalProperties: false
  },
  reviewRun: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/review-run.json",
    title: "ReviewRun",
    type: "object",
    properties: {
      id: stringSchema,
      jobId: stringSchema,
      attempt: { type: "integer", minimum: 0 },
      reviewType: { type: "string", enum: ["build", "typecheck", "runtime_smoke", "runtime_compare", "agent_review"] },
      status: { type: "string", enum: ["pass", "retry", "best_effort", "fail"] },
      decision: stringSchema,
      failureClass: { type: "string", enum: FAILURE_CLASSES },
      evidenceRefs: evidenceRefsSchema,
      repairInstructionIds: stringArraySchema,
      logsArtifactId: stringSchema
    },
    required: [
      "id",
      "jobId",
      "attempt",
      "reviewType",
      "status",
      "decision",
      "failureClass",
      "evidenceRefs",
      "repairInstructionIds"
    ],
    additionalProperties: false
  },
  buildArtifact: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/build-artifact.json",
    title: "BuildArtifact",
    type: "object",
    properties: {
      id: stringSchema,
      jobId: stringSchema,
      stage: { type: "string", enum: ["building", "typechecking"] },
      reviewType: { type: "string", enum: ["build", "typecheck", "runtime_smoke", "runtime_compare", "agent_review"] },
      phase: { type: "string", enum: ["install", "build", "typecheck"] },
      attempt: { type: "integer", minimum: 0 },
      status: { type: "string", enum: ["pass", "retry", "best_effort", "fail"] },
      decision: stringSchema,
      command: stringArraySchema,
      commandSource: { type: "string", enum: ["configured", "npm_script", "fallback_shim", "npm_install", "missing"] },
      scriptName: stringSchema,
      packageManager: stringSchema,
      exitCode: { type: "integer" },
      durationMs: { type: "integer", minimum: 0 },
      failureClass: { type: "string", enum: FAILURE_CLASSES },
      timedOut: { type: "boolean" },
      outputTruncated: { type: "boolean" },
      workingDirectory: stringSchema,
      networkPolicy: { type: "string", enum: ["deny", "allow"] },
      resourcePolicy: sandboxResourcePolicySchema,
      diagnostics: typeScriptDiagnosticsSchema,
      logsArtifactId: stringSchema,
      repairInstructionIds: stringArraySchema,
      limitations: stringArraySchema
    },
    required: [
      "id",
      "jobId",
      "stage",
      "reviewType",
      "phase",
      "attempt",
      "status",
      "decision",
      "command",
      "commandSource",
      "durationMs",
      "failureClass",
      "timedOut",
      "outputTruncated",
      "networkPolicy",
      "resourcePolicy",
      "diagnostics",
      "repairInstructionIds",
      "limitations"
    ],
    additionalProperties: false
  },
  runtimeScenario: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/runtime-scenario.json",
    title: "RuntimeScenario",
    type: "object",
    properties: {
      id: stringSchema,
      jobId: stringSchema,
      name: stringSchema,
      entryUrl: stringSchema,
      viewport: runtimeViewportSchema,
      waitFor: runtimeWaitForSchema,
      interactions: runtimeInteractionsSchema,
      assertions: runtimeAssertionsSchema,
      networkPolicy: { type: "string", enum: ["deny", "allow"] },
      timeoutMs: { type: "integer", minimum: 1 }
    },
    required: [
      "id",
      "jobId",
      "name",
      "waitFor",
      "interactions",
      "assertions",
      "networkPolicy",
      "timeoutMs"
    ],
    additionalProperties: false
  },
  runtimeComparisonReport: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/runtime-comparison-report.json",
    title: "RuntimeComparisonReport",
    type: "object",
    properties: {
      id: stringSchema,
      jobId: stringSchema,
      attempt: { type: "integer", minimum: 0 },
      status: { type: "string", enum: ["pass", "retry", "best_effort", "fail"] },
      scenarioArtifactId: stringSchema,
      original: runtimeCaptureSummarySchema,
      reconstructed: runtimeCaptureSummarySchema,
      differences: runtimeDifferenceSetSchema,
      screenshotArtifactIds: stringArraySchema,
      traceArtifactIds: stringArraySchema,
      limitations: stringArraySchema
    },
    required: [
      "id",
      "jobId",
      "attempt",
      "status",
      "scenarioArtifactId",
      "original",
      "reconstructed",
      "differences",
      "screenshotArtifactIds",
      "traceArtifactIds",
      "limitations"
    ],
    additionalProperties: false
  },
  runtimeValidationRun: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/runtime-validation-run.json",
    title: "RuntimeValidationRun",
    type: "object",
    properties: {
      id: stringSchema,
      jobId: stringSchema,
      attempt: { type: "integer", minimum: 0 },
      target: { type: "string", enum: ["original", "reconstructed"] },
      entryUrl: stringSchema,
      status: { type: "string", enum: ["pass", "retry", "best_effort", "fail"] },
      consoleErrors: stringArraySchema,
      pageErrors: stringArraySchema,
      failedRequests: stringArraySchema,
      screenshotArtifactIds: stringArraySchema,
      traceArtifactId: stringSchema,
      comparisonArtifactId: stringSchema
    },
    required: [
      "id",
      "jobId",
      "attempt",
      "target",
      "entryUrl",
      "status",
      "consoleErrors",
      "pageErrors",
      "failedRequests",
      "screenshotArtifactIds"
    ],
    additionalProperties: false
  },
  browserRunRequest: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/browser-run-request.json",
    title: "BrowserRunRequest",
    type: "object",
    properties: {
      jobId: stringSchema,
      target: { type: "string", enum: ["original", "reconstructed"] },
      attempt: { type: "integer", minimum: 0 },
      entryUrl: stringSchema,
      timeoutMs: { type: "integer", minimum: 1 },
      waitForSelector: stringSchema,
      scenario: { type: "object", additionalProperties: true },
      networkPolicy: { type: "string", enum: ["deny", "allow"] },
      viewport: runtimeViewportSchema,
      sourceArchive: browserRunSourceArchiveSchema
    },
    required: ["jobId", "target", "attempt", "entryUrl", "timeoutMs", "networkPolicy"],
    additionalProperties: false
  },
  browserRunSummary: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/browser-run-summary.json",
    title: "BrowserRunSummary",
    type: "object",
    properties: {
      id: stringSchema,
      status: { type: "string", enum: ["queued", "running", "pass", "fail", "best_effort"] },
      result: browserRunResultSchema,
      error: stringSchema,
      createdAt: { type: "string", format: "date-time" },
      updatedAt: { type: "string", format: "date-time" },
      startedAt: { type: "string", format: "date-time" },
      finishedAt: { type: "string", format: "date-time" },
      attempt: { type: "integer", minimum: 0 },
      maxAttempts: { type: "integer", minimum: 1 },
      leaseOwner: stringSchema,
      leaseExpiresAt: { type: "string", format: "date-time" },
      nextRunAt: { type: "string", format: "date-time" },
      workerId: stringSchema,
      queueBackend: stringSchema,
      leaseRecovered: booleanSchema
    },
    required: ["id", "status", "createdAt", "updatedAt"],
    additionalProperties: false
  },
  browserRunnerQueueMetrics: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/browser-runner-queue-metrics.json",
    title: "BrowserRunnerQueueMetrics",
    ...browserRunnerQueueMetricsSchema
  },
  browserRunnerQueueHealth: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/browser-runner-queue-health.json",
    title: "BrowserRunnerQueueHealth",
    type: "object",
    properties: {
      status: { type: "string", enum: ["ok", "degraded"] },
      serviceRole: stringSchema,
      deploymentProfile: stringSchema,
      workerId: stringSchema,
      maxWorkers: { type: "integer", minimum: 1 },
      maxAttempts: { type: "integer", minimum: 1 },
      leaseSeconds: { type: "integer", minimum: 1 },
      retryBackoffSeconds: { type: "number", minimum: 0 },
      pollSeconds: { type: "number", minimum: 0 },
      metrics: browserRunnerQueueMetricsSchema,
      alerts: {
        type: "array",
        items: browserRunnerQueueAlertSchema
      }
    },
    required: [
      "status",
      "serviceRole",
      "workerId",
      "maxWorkers",
      "maxAttempts",
      "leaseSeconds",
      "retryBackoffSeconds",
      "pollSeconds",
      "metrics",
      "alerts"
    ],
    additionalProperties: false
  },
  opsAlert: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/ops-alert.json",
    title: "OpsAlert",
    type: "object",
    properties: {
      code: stringSchema,
      severity: { type: "string", enum: ["warning", "critical"] },
      message: stringSchema,
      field: stringSchema,
      value: {},
      threshold: {},
      serviceRole: stringSchema,
      instanceId: stringSchema,
      checkedAt: stringSchema
    },
    required: ["code", "severity", "message", "field"],
    additionalProperties: false
  },
  opsHeartbeatRequest: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/ops-heartbeat-request.json",
    title: "OpsHeartbeatRequest",
    type: "object",
    properties: {
      serviceRole: stringSchema,
      instanceId: stringSchema,
      status: { type: "string", enum: ["ok", "degraded"] },
      ttlSeconds: { type: "integer", minimum: 1 },
      metrics: opsMetricsMapSchema,
      alerts: { type: "array", items: opsAlertSchema },
      metadata: opsMetricsMapSchema,
      checkedAt: stringSchema
    },
    required: ["serviceRole", "instanceId", "status", "ttlSeconds", "metrics", "alerts", "metadata"],
    additionalProperties: false
  },
  opsHeartbeatRecord: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/ops-heartbeat-record.json",
    title: "OpsHeartbeatRecord",
    type: "object",
    properties: {
      serviceRole: stringSchema,
      instanceId: stringSchema,
      status: { type: "string", enum: ["ok", "degraded"] },
      checkedAt: stringSchema,
      expiresAt: stringSchema,
      metrics: opsMetricsMapSchema,
      alerts: { type: "array", items: opsAlertSchema },
      metadata: opsMetricsMapSchema,
      createdAt: stringSchema,
      updatedAt: stringSchema
    },
    required: ["serviceRole", "instanceId", "status", "checkedAt", "expiresAt", "metrics", "alerts", "metadata", "createdAt", "updatedAt"],
    additionalProperties: false
  },
  opsMetricsSnapshot: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/ops-metrics-snapshot.json",
    title: "OpsMetricsSnapshot",
    type: "object",
    properties: {
      checkedAt: stringSchema,
      serviceRole: stringSchema,
      deploymentProfile: stringSchema,
      jobStatusCounts: opsIntegerMapSchema,
      activeHeartbeatCount: { type: "integer", minimum: 0 },
      staleHeartbeatCount: { type: "integer", minimum: 0 },
      serviceHeartbeatCounts: opsIntegerMapSchema,
      metrics: opsMetricsMapSchema,
      alerts: { type: "array", items: opsAlertSchema }
    },
    required: [
      "checkedAt",
      "serviceRole",
      "jobStatusCounts",
      "activeHeartbeatCount",
      "staleHeartbeatCount",
      "serviceHeartbeatCounts",
      "metrics",
      "alerts"
    ],
    additionalProperties: false
  },
  opsAlertDelivery: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/ops-alert-delivery.json",
    title: "OpsAlertDelivery",
    type: "object",
    properties: {
      status: { type: "string", enum: ["not_configured", "delivered", "failed"] },
      attempted: booleanSchema,
      webhookUrlConfigured: booleanSchema,
      eventId: stringSchema,
      deliveredAt: stringSchema,
      error: stringSchema
    },
    required: ["status", "attempted", "webhookUrlConfigured"],
    additionalProperties: false
  },
  opsAlertRule: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/ops-alert-rule.json",
    title: "OpsAlertRule",
    ...opsAlertRuleSchema
  },
  opsAlertEvent: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/ops-alert-event.json",
    title: "OpsAlertEvent",
    ...opsAlertEventSchema
  },
  opsAlertResponse: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/ops-alert-response.json",
    title: "OpsAlertResponse",
    type: "object",
    properties: {
      checkedAt: stringSchema,
      alerts: { type: "array", items: opsAlertSchema },
      delivery: opsAlertDeliverySchema,
      events: { type: "array", items: opsAlertEventSchema }
    },
    required: ["checkedAt", "alerts", "delivery", "events"],
    additionalProperties: false
  },
  toolCall: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/tool-call.json",
    title: "ToolCall",
    type: "object",
    properties: {
      id: stringSchema,
      jobId: stringSchema,
      caller: stringSchema,
      toolName: stringSchema,
      toolVersion: stringSchema,
      inputArtifactIds: stringArraySchema,
      outputArtifactIds: stringArraySchema,
      status: { type: "string", enum: ["pass", "fail"] },
      duration: numberSchema,
      failureClass: { type: "string", enum: FAILURE_CLASSES }
    },
    required: [
      "id",
      "jobId",
      "caller",
      "toolName",
      "toolVersion",
      "inputArtifactIds",
      "outputArtifactIds",
      "status",
      "duration",
      "failureClass"
    ],
    additionalProperties: false
  },
  toolRegistryEntry: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/tool-registry-entry.json",
    title: "ToolRegistryEntry",
    type: "object",
    properties: {
      id: stringSchema,
      jobId: stringSchema,
      toolName: stringSchema,
      toolVersion: stringSchema,
      category: { type: "string", enum: ["code", "build", "runtime", "audit", "knowledge", "memory", "model"] },
      caller: stringSchema,
      inputArtifactKinds: artifactKindArraySchema,
      outputArtifactKinds: artifactKindArraySchema,
      failureClasses: failureClassArraySchema,
      description: stringSchema
    },
    required: [
      "id",
      "jobId",
      "toolName",
      "toolVersion",
      "category",
      "caller",
      "inputArtifactKinds",
      "outputArtifactKinds",
      "failureClasses",
      "description"
    ],
    additionalProperties: false
  },
  memoryRecord: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/memory-record.json",
    title: "MemoryRecord",
    type: "object",
    properties: {
      id: stringSchema,
      scope: { type: "string", enum: ["job", "project", "global"] },
      projectId: stringSchema,
      jobId: stringSchema,
      memoryType: { type: "string", enum: ["short_term", "long_term", "entity", "scenario"] },
      content: stringSchema,
      sourceArtifactIds: stringArraySchema,
      sensitivityClass: { type: "string", enum: SENSITIVITY_CLASSES },
      retentionClass: { type: "string", enum: RETENTION_CLASSES }
    },
    required: [
      "id",
      "scope",
      "projectId",
      "memoryType",
      "content",
      "sourceArtifactIds",
      "sensitivityClass",
      "retentionClass"
    ],
    additionalProperties: false
  },
  runtimeDiagnosis: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/runtime-diagnosis.json",
    title: "RuntimeDiagnosis",
    type: "object",
    properties: {
      id: stringSchema,
      jobId: stringSchema,
      attempt: { type: "integer", minimum: 0 },
      agentName: stringSchema,
      targetStage: stringSchema,
      status: { type: "string", enum: ["pass", "retry", "best_effort", "fail"] },
      failureClass: { type: "string", enum: FAILURE_CLASSES },
      inputArtifactIds: stringArraySchema,
      evidenceRefs: evidenceRefsSchema,
      diagnosis: stringSchema,
      recommendedActions: stringArraySchema,
      confidence: { type: "number", minimum: 0, maximum: 1 },
      uncertaintyReasons: stringArraySchema
    },
    required: [
      "id",
      "jobId",
      "attempt",
      "agentName",
      "targetStage",
      "status",
      "failureClass",
      "inputArtifactIds",
      "evidenceRefs",
      "diagnosis",
      "recommendedActions",
      "confidence",
      "uncertaintyReasons"
    ],
    additionalProperties: false
  },
  reportSection: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/report-section.json",
    title: "ReportSection",
    type: "object",
    properties: {
      id: stringSchema,
      jobId: stringSchema,
      agentName: stringSchema,
      title: stringSchema,
      anchor: stringSchema,
      summary: stringSchema,
      content: stringSchema,
      inputArtifactIds: stringArraySchema,
      evidenceRefs: evidenceRefsSchema,
      status: { type: "string", enum: ["pass", "retry", "best_effort", "fail"] },
      confidence: { type: "number", minimum: 0, maximum: 1 },
      uncertaintyReasons: stringArraySchema,
      details: reportSectionDetailsSchema
    },
    required: [
      "id",
      "jobId",
      "agentName",
      "title",
      "anchor",
      "summary",
      "content",
      "inputArtifactIds",
      "evidenceRefs",
      "status",
      "confidence",
      "uncertaintyReasons",
      "details"
    ],
    additionalProperties: false
  },
  repairInstruction: {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://ai-jsunpack.local/schemas/repair-instruction.json",
    title: "RepairInstruction",
    type: "object",
    properties: {
      id: stringSchema,
      jobId: stringSchema,
      attempt: { type: "integer", minimum: 0 },
      targetStage: { type: "string", enum: ["building", "typechecking", "runtime_smoke", "runtime_compare"] },
      failureClass: { type: "string", enum: FAILURE_CLASSES },
      inputArtifactIds: stringArraySchema,
      evidenceRefs: evidenceRefsSchema,
      actions: repairActionsSchema,
      status: { type: "string", enum: ["planned", "applied", "skipped"] },
      riskLevel: { type: "string", enum: ["low", "medium", "high"] },
      decision: stringSchema
    },
    required: [
      "id",
      "jobId",
      "attempt",
      "targetStage",
      "failureClass",
      "inputArtifactIds",
      "evidenceRefs",
      "actions",
      "status",
      "riskLevel",
      "decision"
    ],
    additionalProperties: false
  }
} as const satisfies Record<string, JsonSchema>;

const exampleTimestamp = "2026-06-14T00:00:00.000Z";

export const EXAMPLE_EVIDENCE_REF = {
  artifactId: "artifact_input_example",
  label: "contract fixture",
  locator: "file:index.html",
  excerpt: "<script src=\"/assets/app.js\"></script>"
} as const satisfies EvidenceRef;

export const EXAMPLE_ARTIFACT = {
  id: "artifact_input_example",
  jobId: "job_contract_example",
  kind: "input_inventory",
  stage: "intake",
  attempt: 0,
  schemaVersion: CONTRACT_SCHEMA_VERSION,
  contentType: "application/json",
  hash: "sha256:contract-fixture",
  size: 128,
  storageUri: "file://artifacts/job_contract_example/input-inventory.json",
  parentArtifactIds: [],
  producer: "contract.test",
  sensitivityClass: "source_sensitive",
  retentionClass: "project",
  createdAt: exampleTimestamp,
  expiresAt: null,
  deletedAt: null,
  deletionReason: null
} as const satisfies Artifact;

export const EXAMPLE_JOB = {
  id: "job_contract_example",
  status: "queued",
  ownerId: "local-user",
  projectId: "default",
  inputArtifactId: EXAMPLE_ARTIFACT.id,
  config: {
    fixture: true
  },
  cloudMode: "local_only",
  reviewAttempt: 0,
  runAttempt: 0,
  workerLease: {
    workerId: "worker_contract",
    expiresAt: exampleTimestamp
  },
  failureClass: "none",
  failureReason: "not failed",
  createdAt: exampleTimestamp,
  updatedAt: exampleTimestamp
} as const satisfies Job;

export const EXAMPLE_INFERENCE_RECORD = {
  id: "inference_contract_example",
  jobId: EXAMPLE_JOB.id,
  type: "naming",
  agentName: "NamingAgent",
  modelProvider: "stub",
  modelName: "stub-contract-model",
  promptVersion: "contract-v1",
  inputArtifactIds: [EXAMPLE_ARTIFACT.id],
  outputArtifactIds: ["artifact_inference_example"],
  evidenceRefs: [EXAMPLE_EVIDENCE_REF],
  confidence: 0.85,
  uncertaintyReasons: ["fixture uncertainty"],
  alternatives: ["keep original symbol"],
  validationStatus: "accepted",
  rollbackRef: "rollback:contract"
} as const satisfies InferenceRecord;

export const EXAMPLE_REVIEW_RUN = {
  id: "review_contract_example",
  jobId: EXAMPLE_JOB.id,
  attempt: 0,
  reviewType: "build",
  status: "pass",
  decision: "contract fixture accepted",
  failureClass: "none",
  evidenceRefs: [EXAMPLE_EVIDENCE_REF],
  repairInstructionIds: [],
  logsArtifactId: "artifact_logs_example"
} as const satisfies ReviewRun;

export const EXAMPLE_BUILD_ARTIFACT = {
  id: "build_contract_example",
  jobId: EXAMPLE_JOB.id,
  stage: "typechecking",
  reviewType: "typecheck",
  phase: "typecheck",
  attempt: 0,
  status: "fail",
  decision: "TypeScript validation found one contract fixture diagnostic.",
  command: ["npm", "run", "--ignore-scripts", "typecheck"],
  commandSource: "npm_script",
  scriptName: "typecheck",
  packageManager: "npm",
  exitCode: 2,
  durationMs: 42,
  failureClass: "type_error",
  timedOut: false,
  outputTruncated: false,
  workingDirectory: "project",
  networkPolicy: "deny",
  resourcePolicy: {
    processLimit: null,
    cpuTimeLimitMs: null,
    memoryLimitBytes: null,
    enforcement: "local_best_effort",
    runnerKind: "local",
    runtimeName: null,
    runtimeVersion: null,
    hostPlatform: "contract-test-platform",
    capabilities: [
      {
        name: "network",
        status: "best_effort",
        detail: "Local runner records network policy but does not enforce OS-level network isolation."
      },
      {
        name: "process",
        status: "best_effort",
        detail: "Local runner records process limits but does not enforce a process-count boundary."
      },
      {
        name: "cpu",
        status: "best_effort",
        detail: "Local runner enforces wall-clock timeout only; CPU time limits are audit metadata."
      },
      {
        name: "memory",
        status: "best_effort",
        detail: "Local runner records memory limits but does not enforce an OS memory boundary."
      },
      {
        name: "filesystem",
        status: "best_effort",
        detail: "Local runner executes in a temporary attempt workspace and validates relative working directories."
      }
    ],
    limitations: [
      "Local sandbox runner records process, CPU, and memory policy but does not enforce OS/container isolation."
    ]
  },
  diagnostics: [
    {
      source: "stderr",
      tool: "tsc",
      category: "error",
      code: "TS2322",
      message: "Type 'string' is not assignable to type 'number'.",
      filePath: "src/index.ts",
      line: 4,
      column: 7,
      contextLines: ["  4 const value: number = \"text\";", "        ~~~~~"],
      relatedInformation: [
        {
          message: "The expected type comes from this declaration.",
          filePath: "src/types.ts",
          line: 1,
          column: 14,
          code: null
        }
      ]
    }
  ],
  logsArtifactId: "artifact_logs_example",
  repairInstructionIds: ["repair_contract_example"],
  limitations: []
} as const satisfies BuildArtifact;

export const EXAMPLE_RUNTIME_SCENARIO = {
  id: "runtime_scenario_contract_example",
  jobId: EXAMPLE_JOB.id,
  name: "default-load",
  entryUrl: null,
  viewport: {
    name: "desktop",
    width: 1365,
    height: 768
  },
  waitFor: [
    {
      kind: "load_state",
      selector: null,
      state: "load",
      timeoutMs: 10000
    }
  ],
  interactions: [],
  assertions: [
    {
      kind: "selector_visible",
      selector: "body",
      text: null,
      value: null
    }
  ],
  networkPolicy: "deny",
  timeoutMs: 10000
} as const satisfies RuntimeScenario;

export const EXAMPLE_RUNTIME_COMPARISON_REPORT = {
  id: "runtime_comparison_contract_example",
  jobId: EXAMPLE_JOB.id,
  attempt: 0,
  status: "pass",
  scenarioArtifactId: "artifact_runtime_scenario_example",
  original: {
    target: "original",
    entryUrl: "http://127.0.0.1:4173/",
    status: "pass",
    failureClass: "none",
    consoleMessages: [],
    consoleErrors: [],
    pageErrors: [],
    failedRequests: [],
    responses: ["200 http://127.0.0.1:4173/"],
    assertionFailures: [],
    domSummary: {
      title: "Original",
      nodeCount: 4,
      textLength: 8
    },
    screenshotArtifactId: "artifact_original_screenshot_example",
    durationMs: 24,
    limitations: []
  },
  reconstructed: {
    target: "reconstructed",
    entryUrl: "http://127.0.0.1:5173/",
    status: "pass",
    failureClass: "none",
    consoleMessages: [],
    consoleErrors: [],
    pageErrors: [],
    failedRequests: [],
    responses: ["200 http://127.0.0.1:5173/"],
    assertionFailures: [],
    domSummary: {
      title: "Reconstructed",
      nodeCount: 4,
      textLength: 8
    },
    screenshotArtifactId: "artifact_reconstructed_screenshot_example",
    durationMs: 26,
    limitations: []
  },
  differences: {
    screenshotChanged: false,
    domChanged: true,
    networkChanged: true,
    consoleChanged: false,
    originalOnlyRequests: ["200 http://127.0.0.1:4173/"],
    reconstructedOnlyRequests: ["200 http://127.0.0.1:5173/"],
    originalOnlyConsole: [],
    reconstructedOnlyConsole: [],
    changedDomFields: ["title"],
    screenshotDiff: {
      changed: false,
      originalHash: "sha256-original",
      reconstructedHash: "sha256-original",
      originalSizeBytes: 2048,
      reconstructedSizeBytes: 2048,
      originalFormat: "png",
      reconstructedFormat: "png",
      pixelDiffStatus: "compared",
      pixelCount: 1048320,
      changedPixelCount: 0,
      changedPixelRatio: 0,
      threshold: 0,
      thresholdMode: "per_channel_rgba",
      maxChangedPixelRatio: 0,
      width: 1365,
      height: 768,
      diffArtifactId: "artifact_runtime_diff_screenshot_example",
      reason: null
    },
    domDifferences: [
      {
        path: "title",
        original: "Original",
        reconstructed: "Reconstructed",
        summary: "DOM summary field title changed."
      }
    ],
    networkDiff: {
      changed: true,
      originalCount: 1,
      reconstructedCount: 1,
      shared: [],
      originalOnly: ["200 http://127.0.0.1:4173/"],
      reconstructedOnly: ["200 http://127.0.0.1:5173/"],
      groups: {
        status_2xx: ["200 http://127.0.0.1:4173/", "200 http://127.0.0.1:5173/"]
      }
    },
    consoleDiff: {
      changed: false,
      originalCount: 0,
      reconstructedCount: 0,
      shared: [],
      originalOnly: [],
      reconstructedOnly: [],
      groups: {}
    },
    comparisonScope: {
      scenarioName: "default-load",
      networkPolicy: "deny",
      timeoutMs: 10000,
      viewport: {
        name: "desktop",
        width: 1365,
        height: 768
      }
    }
  },
  screenshotArtifactIds: [
    "artifact_original_screenshot_example",
    "artifact_reconstructed_screenshot_example",
    "artifact_runtime_diff_screenshot_example"
  ],
  traceArtifactIds: ["artifact_runtime_trace_example"],
  limitations: []
} as const satisfies RuntimeComparisonReport;

export const EXAMPLE_RUNTIME_VALIDATION_RUN = {
  id: "runtime_contract_example",
  jobId: EXAMPLE_JOB.id,
  attempt: 0,
  target: "reconstructed",
  entryUrl: "http://127.0.0.1:5173/",
  status: "pass",
  consoleErrors: [],
  pageErrors: [],
  failedRequests: [],
  screenshotArtifactIds: ["artifact_screenshot_example"],
  traceArtifactId: "artifact_trace_example",
  comparisonArtifactId: "artifact_comparison_example"
} as const satisfies RuntimeValidationRun;

export const EXAMPLE_BROWSER_RUN_REQUEST = {
  jobId: EXAMPLE_JOB.id,
  target: "reconstructed",
  attempt: 0,
  entryUrl: "http://127.0.0.1:5173/",
  timeoutMs: 10000,
  waitForSelector: null,
  scenario: EXAMPLE_RUNTIME_SCENARIO,
  networkPolicy: "deny",
  viewport: {
    name: "desktop",
    width: 1365,
    height: 768
  },
  sourceArchive: {
    contentBase64: "UEsDBAoAAAAAA",
    entryPath: "index.html"
  }
} as const satisfies BrowserRunRequest;

export const EXAMPLE_BROWSER_RUN_SUMMARY = {
  id: "browser_run_contract_example",
  status: "pass",
  result: {
    status: "pass",
    failureClass: "none",
    consoleMessages: [],
    consoleErrors: [],
    pageErrors: [],
    failedRequests: [],
    responses: ["200 http://127.0.0.1:5173/"],
    assertionFailures: [],
    domSummary: {
      title: "Reconstructed",
      nodeCount: 4
    },
    screenshotBase64: "iVBORw0KGgo=",
    limitations: ["Browser execution ran in the remote browser-runner service boundary."],
    executionBoundary: {
      runnerKind: "remote_browser_runner",
      enforcement: "remote_isolated",
      remoteRunId: "browser_run_contract_example",
      auth: "bearer_hmac",
      artifactExchange: "worker_request_archive_and_worker_registered_runtime_artifacts",
      queueBackend: "sqlite",
      maxWorkers: 2,
      runAttempt: 1,
      maxAttempts: 3,
      leaseSeconds: 120,
      retryBackoffSeconds: 1,
      leaseRecovered: false,
      queueLength: 0,
      runningCount: 1,
      terminalCount: 4,
      totalCount: 5,
      oldestQueuedAgeMs: null,
      claimLatencyMs: null,
      averageRunDurationMs: 250,
      retryRate: 0,
      leaseRecoveryCount: 0,
      expiredRunningCount: 0,
      backendHealthStatus: "ok",
      backendError: null,
      alerts: []
    }
  },
  error: null,
  createdAt: exampleTimestamp,
  updatedAt: exampleTimestamp,
  startedAt: exampleTimestamp,
  finishedAt: exampleTimestamp,
  attempt: 1,
  maxAttempts: 3,
  leaseOwner: null,
  leaseExpiresAt: null,
  nextRunAt: exampleTimestamp,
  workerId: "browser-runner-contract",
  queueBackend: "sqlite",
  leaseRecovered: false
} as const satisfies BrowserRunSummary;

export const EXAMPLE_BROWSER_RUNNER_QUEUE_METRICS = {
  checkedAt: exampleTimestamp,
  queueBackend: "postgresql",
  backendStatus: "ok",
  backendError: null,
  queuedCount: 2,
  runningCount: 1,
  terminalCount: 4,
  totalCount: 7,
  oldestQueuedAgeMs: 1500,
  claimLatencyMs: 1200,
  averageRunDurationMs: 3400,
  retryRate: 0.25,
  leaseRecoveryCount: 1,
  expiredRunningCount: 0
} as const satisfies BrowserRunnerQueueMetrics;

export const EXAMPLE_BROWSER_RUNNER_QUEUE_HEALTH = {
  status: "degraded",
  serviceRole: "browser-runner",
  deploymentProfile: "ok",
  workerId: "browser-runner-contract",
  maxWorkers: 2,
  maxAttempts: 3,
  leaseSeconds: 120,
  retryBackoffSeconds: 1,
  pollSeconds: 0.25,
  metrics: EXAMPLE_BROWSER_RUNNER_QUEUE_METRICS,
  alerts: [
    {
      code: "queue_backlog",
      severity: "warning",
      message: "Browser Runner 排队运行数超过本地 Worker 并发数。",
      field: "queuedCount",
      value: 2,
      threshold: 1
    }
  ]
} as const satisfies BrowserRunnerQueueHealth;

export const EXAMPLE_OPS_ALERT = {
  code: "worker_heartbeat_stale",
  severity: "critical",
  message: "Worker heartbeat is stale or expired.",
  field: "expiresAt",
  value: exampleTimestamp,
  threshold: "now",
  serviceRole: "worker",
  instanceId: "worker-contract",
  checkedAt: exampleTimestamp
} as const satisfies OpsAlert;

export const EXAMPLE_OPS_HEARTBEAT_REQUEST = {
  serviceRole: "worker",
  instanceId: "worker-contract",
  status: "ok",
  ttlSeconds: 90,
  metrics: {
    pollSeconds: 5,
    maxAttempts: 3
  },
  alerts: [],
  metadata: {
    deploymentProfile: "ok"
  },
  checkedAt: exampleTimestamp
} as const satisfies OpsHeartbeatRequest;

export const EXAMPLE_OPS_HEARTBEAT_RECORD = {
  serviceRole: "worker",
  instanceId: "worker-contract",
  status: "ok",
  checkedAt: exampleTimestamp,
  expiresAt: "2026-06-14T00:01:30.000Z",
  metrics: EXAMPLE_OPS_HEARTBEAT_REQUEST.metrics,
  alerts: [],
  metadata: EXAMPLE_OPS_HEARTBEAT_REQUEST.metadata,
  createdAt: exampleTimestamp,
  updatedAt: exampleTimestamp
} as const satisfies OpsHeartbeatRecord;

export const EXAMPLE_OPS_METRICS_SNAPSHOT = {
  checkedAt: exampleTimestamp,
  serviceRole: "api",
  deploymentProfile: "ok",
  jobStatusCounts: {
    queued: 1,
    leased: 1,
    completed: 2
  },
  activeHeartbeatCount: 1,
  staleHeartbeatCount: 1,
  serviceHeartbeatCounts: {
    worker: 1,
    "browser-runner": 1
  },
  metrics: {
    contractSchemaVersion: CONTRACT_SCHEMA_VERSION
  },
  alerts: [EXAMPLE_OPS_ALERT]
} as const satisfies OpsMetricsSnapshot;

export const EXAMPLE_OPS_ALERT_DELIVERY = {
  status: "not_configured",
  attempted: false,
  webhookUrlConfigured: false,
  eventId: null,
  deliveredAt: null,
  error: null
} as const satisfies OpsAlertDelivery;

export const EXAMPLE_OPS_ALERT_RULE = {
  code: "browser_runner_queue_backlog",
  severity: "warning",
  metricPath: "metrics.browserRunner.queuedCount",
  operator: "gte",
  threshold: 2,
  message: "Browser Runner 队列积压超过配置的阈值。",
  serviceRole: "browser-runner",
  enabled: true,
  source: "default"
} as const satisfies OpsAlertRule;

export const EXAMPLE_OPS_ALERT_EVENT = {
  id: "ops_alert_event_contract_example",
  checkedAt: exampleTimestamp,
  status: "active",
  severity: "warning",
  code: EXAMPLE_OPS_ALERT_RULE.code,
  message: EXAMPLE_OPS_ALERT_RULE.message,
  field: EXAMPLE_OPS_ALERT_RULE.metricPath,
  value: 2,
  threshold: EXAMPLE_OPS_ALERT_RULE.threshold,
  serviceRole: "browser-runner",
  instanceId: "browser-runner-contract",
  rule: EXAMPLE_OPS_ALERT_RULE,
  alerts: [EXAMPLE_OPS_ALERT],
  metrics: {
    queuedCount: 2,
    retryRate: 0.25
  },
  delivery: EXAMPLE_OPS_ALERT_DELIVERY,
  createdAt: exampleTimestamp,
  updatedAt: exampleTimestamp
} as const satisfies OpsAlertEvent;

export const EXAMPLE_OPS_ALERT_RESPONSE = {
  checkedAt: exampleTimestamp,
  alerts: [EXAMPLE_OPS_ALERT],
  delivery: EXAMPLE_OPS_ALERT_DELIVERY,
  events: [EXAMPLE_OPS_ALERT_EVENT]
} as const satisfies OpsAlertResponse;

export const EXAMPLE_TOOL_CALL = {
  id: "tool_call_contract_example",
  jobId: EXAMPLE_JOB.id,
  caller: "WorkerPipeline",
  toolName: "analyzeInputPackage",
  toolVersion: "0.1.0",
  inputArtifactIds: [EXAMPLE_ARTIFACT.id],
  outputArtifactIds: ["artifact_ast_example"],
  status: "pass",
  duration: 12.5,
  failureClass: "none"
} as const satisfies ToolCall;

export const EXAMPLE_TOOL_REGISTRY_ENTRY = {
  id: "tool_registry_contract_example",
  jobId: EXAMPLE_JOB.id,
  toolName: "crewai.agent_pass",
  toolVersion: "0.2.0",
  category: "model",
  caller: "WorkerPipeline",
  inputArtifactKinds: ["input_inventory", "ast_index", "memory_record", "knowledge_evidence"],
  outputArtifactKinds: ["agent_plan", "inference_record", "review_run", "tool_call"],
  failureClasses: ["none", "policy_denied", "agent_failed"],
  description: "Runs schema-first Agent analysis over deterministic Core evidence."
} as const satisfies ToolRegistryEntry;

export const EXAMPLE_MEMORY_RECORD = {
  id: "memory_contract_example",
  scope: "job",
  projectId: EXAMPLE_JOB.projectId,
  jobId: EXAMPLE_JOB.id,
  memoryType: "short_term",
  content: "Contract fixture memory.",
  sourceArtifactIds: [EXAMPLE_ARTIFACT.id],
  sensitivityClass: "derived",
  retentionClass: "project"
} as const satisfies MemoryRecord;

export const EXAMPLE_RUNTIME_DIAGNOSIS = {
  id: "runtime_diagnosis_contract_example",
  jobId: EXAMPLE_JOB.id,
  attempt: 0,
  agentName: "RuntimeAgent",
  targetStage: "runtime_compare",
  status: "best_effort",
  failureClass: "runtime_error",
  inputArtifactIds: [EXAMPLE_ARTIFACT.id],
  evidenceRefs: [EXAMPLE_EVIDENCE_REF],
  diagnosis: "Runtime comparison needs additional browser evidence before applying a repair.",
  recommendedActions: ["Inspect runtime_trace and runtime_comparison artifacts."],
  confidence: 0.6,
  uncertaintyReasons: ["Contract fixture does not include a real browser trace."]
} as const satisfies RuntimeDiagnosis;

export const EXAMPLE_REPORT_SECTION = {
  id: "report_section_contract_example",
  jobId: EXAMPLE_JOB.id,
  agentName: "ReportAgent",
  title: "Agent Runtime Summary",
  anchor: "agent-runtime-summary",
  summary: "Contract fixture report section.",
  content: "The Agent runtime produced schema-valid audit evidence.",
  inputArtifactIds: [EXAMPLE_ARTIFACT.id],
  evidenceRefs: [EXAMPLE_EVIDENCE_REF],
  status: "pass",
  confidence: 0.8,
  uncertaintyReasons: ["Fixture content is intentionally minimal."],
  details: [
    {
      label: "Runtime compare scope",
      value: "default-load / desktop 1280x720",
      status: "pass",
      details: {
        domDifferences: 0,
        networkChanged: false,
        consoleChanged: false
      }
    }
  ]
} as const satisfies ReportSection;

export const EXAMPLE_RETENTION_CLEANUP_REQUEST = {
  dryRun: true,
  categories: ["logs", "screenshots", "memory"],
  retentionClasses: ["ephemeral", "project"],
  deleteExpired: true,
  reason: "contract cleanup preview",
  now: exampleTimestamp
} as const satisfies RetentionCleanupRequest;

export const EXAMPLE_RETENTION_CLEANUP_RESULT = {
  jobId: EXAMPLE_JOB.id,
  dryRun: true,
  requestedAt: exampleTimestamp,
  candidateCount: 1,
  deletedCount: 0,
  skippedCount: 2,
  errorCount: 0,
  items: [
    {
      artifactId: EXAMPLE_ARTIFACT.id,
      kind: EXAMPLE_ARTIFACT.kind,
      category: "derived",
      retentionClass: EXAMPLE_ARTIFACT.retentionClass,
      storageUri: EXAMPLE_ARTIFACT.storageUri,
      deleted: false,
      reason: "dry_run",
      error: null
    }
  ],
  errors: []
} as const satisfies RetentionCleanupResult;

export const EXAMPLE_REPAIR_INSTRUCTION = {
  id: "repair_contract_example",
  jobId: EXAMPLE_JOB.id,
  attempt: 1,
  targetStage: "building",
  failureClass: "build_error",
  inputArtifactIds: [EXAMPLE_ARTIFACT.id],
  evidenceRefs: [EXAMPLE_EVIDENCE_REF],
  actions: [
    {
      action: "add_package_script",
      path: "package.json:scripts.build",
      value: "node scripts/build.mjs",
      reason: "A generated validation shim exists and the package script is missing."
    }
  ],
  status: "applied",
  riskLevel: "low",
  decision: "Added a deterministic package script for the generated project validation shim."
} as const satisfies RepairInstruction;

export const SHARED_CONTRACT_EXAMPLES = {
  job: EXAMPLE_JOB,
  artifact: EXAMPLE_ARTIFACT,
  evidenceRef: EXAMPLE_EVIDENCE_REF,
  inferenceRecord: EXAMPLE_INFERENCE_RECORD,
  reviewRun: EXAMPLE_REVIEW_RUN,
  buildArtifact: EXAMPLE_BUILD_ARTIFACT,
  runtimeScenario: EXAMPLE_RUNTIME_SCENARIO,
  runtimeComparisonReport: EXAMPLE_RUNTIME_COMPARISON_REPORT,
  runtimeValidationRun: EXAMPLE_RUNTIME_VALIDATION_RUN,
  browserRunRequest: EXAMPLE_BROWSER_RUN_REQUEST,
  browserRunSummary: EXAMPLE_BROWSER_RUN_SUMMARY,
  browserRunnerQueueMetrics: EXAMPLE_BROWSER_RUNNER_QUEUE_METRICS,
  browserRunnerQueueHealth: EXAMPLE_BROWSER_RUNNER_QUEUE_HEALTH,
  opsAlert: EXAMPLE_OPS_ALERT,
  opsHeartbeatRequest: EXAMPLE_OPS_HEARTBEAT_REQUEST,
  opsHeartbeatRecord: EXAMPLE_OPS_HEARTBEAT_RECORD,
  opsMetricsSnapshot: EXAMPLE_OPS_METRICS_SNAPSHOT,
  opsAlertDelivery: EXAMPLE_OPS_ALERT_DELIVERY,
  opsAlertRule: EXAMPLE_OPS_ALERT_RULE,
  opsAlertEvent: EXAMPLE_OPS_ALERT_EVENT,
  opsAlertResponse: EXAMPLE_OPS_ALERT_RESPONSE,
  toolCall: EXAMPLE_TOOL_CALL,
  toolRegistryEntry: EXAMPLE_TOOL_REGISTRY_ENTRY,
  memoryRecord: EXAMPLE_MEMORY_RECORD,
  runtimeDiagnosis: EXAMPLE_RUNTIME_DIAGNOSIS,
  reportSection: EXAMPLE_REPORT_SECTION,
  retentionCleanupRequest: EXAMPLE_RETENTION_CLEANUP_REQUEST,
  retentionCleanupResult: EXAMPLE_RETENTION_CLEANUP_RESULT,
  repairInstruction: EXAMPLE_REPAIR_INSTRUCTION
} as const;
