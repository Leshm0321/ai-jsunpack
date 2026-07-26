import assert from "node:assert/strict";
import test from "node:test";
import {
  ARTIFACT_KINDS,
  CLOUD_MODES,
  FAILURE_CLASSES,
  JOB_STATUSES,
  RETENTION_CLASSES,
  RETENTION_CATEGORIES,
  SENSITIVITY_CLASSES,
  SHARED_CONTRACT_EXAMPLES,
  SHARED_JSON_SCHEMAS
} from "./index.js";

test("共享 schema 复用规范枚举常量", () => {
  assert.deepEqual(SHARED_JSON_SCHEMAS.job.properties?.status?.enum, JOB_STATUSES);
  assert.deepEqual(SHARED_JSON_SCHEMAS.job.properties?.cloudMode?.enum, CLOUD_MODES);
  assert.deepEqual(SHARED_JSON_SCHEMAS.job.properties?.failureClass?.enum, FAILURE_CLASSES);
  assert.deepEqual(SHARED_JSON_SCHEMAS.artifact.properties?.kind?.enum, ARTIFACT_KINDS);
  assert.deepEqual(SHARED_JSON_SCHEMAS.artifact.properties?.sensitivityClass?.enum, SENSITIVITY_CLASSES);
  assert.deepEqual(SHARED_JSON_SCHEMAS.artifact.properties?.retentionClass?.enum, RETENTION_CLASSES);
  assert.deepEqual(
    SHARED_JSON_SCHEMAS.retentionCleanupRequest.properties?.categories?.items?.enum,
    RETENTION_CATEGORIES
  );
});

test("shared repair and inference contract extensions stay exposed in JSON schemas", () => {
  const inferenceRequired = SHARED_JSON_SCHEMAS.inferenceRecord.required as readonly string[];

  assert.ok(SHARED_JSON_SCHEMAS.inferenceRecord.properties?.target);
  assert.ok(SHARED_JSON_SCHEMAS.inferenceRecord.properties?.value);
  assert.ok(!inferenceRequired.includes("target"));
  assert.ok(!inferenceRequired.includes("value"));

  assert.ok(SHARED_JSON_SCHEMAS.repairInstruction.properties?.targetStage?.enum?.includes("reconstructing"));
  assert.ok(
    SHARED_JSON_SCHEMAS.repairInstruction.properties?.actions?.items?.properties?.action?.enum?.includes(
      "apply_symbol_rename_map"
    )
  );
});

test("共享示例覆盖每个 schema 的必填字段且不含额外键", () => {
  const examples = SHARED_CONTRACT_EXAMPLES as unknown as Record<string, Record<string, unknown>>;

  assert.deepEqual(Object.keys(examples).sort(), Object.keys(SHARED_JSON_SCHEMAS).sort());

  for (const [schemaName, schema] of Object.entries(SHARED_JSON_SCHEMAS)) {
    const example = examples[schemaName];
    assert.ok(example, `${schemaName} 示例已导出`);

    for (const key of schema.required ?? []) {
      assert.ok(key in example, `${schemaName} 示例包含必填键 ${key}`);
    }

    const allowedKeys = new Set(Object.keys(schema.properties ?? {}));
    for (const key of Object.keys(example)) {
      assert.ok(allowedKeys.has(key), `${schemaName} 示例键 ${key} 已在 schema properties 中声明`);
    }
  }
});

test("任务和产物 schema 暴露契约要求的预期字段", () => {
  assert.deepEqual(SHARED_JSON_SCHEMAS.job.required, [
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
  ]);

  assert.deepEqual(SHARED_JSON_SCHEMAS.artifact.required, [
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
  ]);
});
