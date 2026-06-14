import assert from "node:assert/strict";
import test from "node:test";
import {
  ARTIFACT_KINDS,
  CLOUD_MODES,
  FAILURE_CLASSES,
  JOB_STATUSES,
  RETENTION_CLASSES,
  SENSITIVITY_CLASSES,
  SHARED_CONTRACT_EXAMPLES,
  SHARED_JSON_SCHEMAS
} from "./index.js";

test("shared schemas reuse the canonical enum constants", () => {
  assert.deepEqual(SHARED_JSON_SCHEMAS.job.properties?.status?.enum, JOB_STATUSES);
  assert.deepEqual(SHARED_JSON_SCHEMAS.job.properties?.cloudMode?.enum, CLOUD_MODES);
  assert.deepEqual(SHARED_JSON_SCHEMAS.job.properties?.failureClass?.enum, FAILURE_CLASSES);
  assert.deepEqual(SHARED_JSON_SCHEMAS.artifact.properties?.kind?.enum, ARTIFACT_KINDS);
  assert.deepEqual(SHARED_JSON_SCHEMAS.artifact.properties?.sensitivityClass?.enum, SENSITIVITY_CLASSES);
  assert.deepEqual(SHARED_JSON_SCHEMAS.artifact.properties?.retentionClass?.enum, RETENTION_CLASSES);
});

test("shared examples cover each schema required field without extra keys", () => {
  const examples = SHARED_CONTRACT_EXAMPLES as unknown as Record<string, Record<string, unknown>>;

  assert.deepEqual(Object.keys(examples).sort(), Object.keys(SHARED_JSON_SCHEMAS).sort());

  for (const [schemaName, schema] of Object.entries(SHARED_JSON_SCHEMAS)) {
    const example = examples[schemaName];
    assert.ok(example, `${schemaName} example is exported`);

    for (const key of schema.required ?? []) {
      assert.ok(key in example, `${schemaName} example includes required key ${key}`);
    }

    const allowedKeys = new Set(Object.keys(schema.properties ?? {}));
    for (const key of Object.keys(example)) {
      assert.ok(allowedKeys.has(key), `${schemaName} example key ${key} is declared in schema properties`);
    }
  }
});

test("job and artifact schemas expose the expected contract-required fields", () => {
  assert.deepEqual(SHARED_JSON_SCHEMAS.job.required, [
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
