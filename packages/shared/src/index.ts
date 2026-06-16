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
  "memory_record",
  "knowledge_evidence",
  "repair_instruction",
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
  enforcement: "local_best_effort" | "container_enforced";
  runnerKind: "local" | "container";
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
  pixelDiffStatus: "compared" | "unavailable";
  pixelCount?: number | null;
  changedPixelCount?: number | null;
  changedPixelRatio?: number | null;
  threshold?: number | null;
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

export interface RepairAction {
  action: "add_package_script";
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
const repairActionsSchema = {
  type: "array",
  items: {
    type: "object",
    properties: {
      action: { type: "string", enum: ["add_package_script"] },
      path: stringSchema,
      value: stringSchema,
      reason: stringSchema
    },
    required: ["action", "path", "value", "reason"],
    additionalProperties: false
  }
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
    enforcement: { type: "string", enum: ["local_best_effort", "container_enforced"] },
    runnerKind: { type: "string", enum: ["local", "container"] },
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
const runtimeScreenshotDiffSchema = {
  type: "object",
  properties: {
    changed: booleanSchema,
    originalHash: stringSchema,
    reconstructedHash: stringSchema,
    originalSizeBytes: { type: "integer", minimum: 0 },
    reconstructedSizeBytes: { type: "integer", minimum: 0 },
    pixelDiffStatus: { type: "string", enum: ["compared", "unavailable"] },
    pixelCount: { type: "integer", minimum: 0 },
    changedPixelCount: { type: "integer", minimum: 0 },
    changedPixelRatio: { type: "number", minimum: 0 },
    threshold: { type: "integer", minimum: 0 },
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
  retentionClass: RETENTION_CLASSES
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
      createdAt: { type: "string", format: "date-time" }
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
  createdAt: exampleTimestamp
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
      pixelDiffStatus: "compared",
      pixelCount: 1048320,
      changedPixelCount: 0,
      changedPixelRatio: 0,
      threshold: 0,
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
  toolCall: EXAMPLE_TOOL_CALL,
  memoryRecord: EXAMPLE_MEMORY_RECORD,
  repairInstruction: EXAMPLE_REPAIR_INSTRUCTION
} as const;
