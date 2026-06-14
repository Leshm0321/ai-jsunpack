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

export const SHARED_CONTRACT_EXAMPLES = {
  job: EXAMPLE_JOB,
  artifact: EXAMPLE_ARTIFACT,
  evidenceRef: EXAMPLE_EVIDENCE_REF,
  inferenceRecord: EXAMPLE_INFERENCE_RECORD,
  reviewRun: EXAMPLE_REVIEW_RUN,
  runtimeValidationRun: EXAMPLE_RUNTIME_VALIDATION_RUN,
  toolCall: EXAMPLE_TOOL_CALL,
  memoryRecord: EXAMPLE_MEMORY_RECORD
} as const;
