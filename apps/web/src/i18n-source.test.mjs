import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = (name) => readFile(new URL(name, import.meta.url), "utf8");

test("中文是首次加载和存储不可用时的默认语言", async () => {
  const i18n = await source("./i18n.ts");

  assert.match(i18n, /language: "zh"/);
  assert.match(i18n, /t: \(key\) => translate\("zh", key\)/);
  assert.equal((i18n.match(/return "zh";/g) ?? []).length, 2);
});

test("工作台直出提示通过本地化键生成", async () => {
  const [artifactLogic, auditLogic, reportLogic, runtimeLogic, graphLogic, formatLogic] = await Promise.all([
    source("./workbench-artifact-logic.ts"),
    source("./workbench-audit-logic.ts"),
    source("./workbench-report-logic.ts"),
    source("./workbench-runtime-logic.ts"),
    source("./workbench-graph-logic.ts"),
    source("./workbench-format.ts"),
  ]);
  const combined = [artifactLogic, auditLogic, reportLogic, runtimeLogic, graphLogic, formatLogic].join("\n");

  for (const key of [
    "support.generatedProject",
    "audit.group.blocking",
    "parse.evidenceIndexInvalid",
    "parse.runtimeComparisonInvalid",
    "parse.inputInventoryInvalid",
    "parse.astIndexInvalid",
    "fallback.section.completion",
    "summary.noStructuredBreakdown",
    "download.error",
  ]) {
    assert.ok(combined.includes(`t("${key}")`), `${key} 应通过 t(...) 使用`);
  }

  for (const english of [
    "Directory artifacts are available through the packaged result download.",
    "Blocking risk",
    "Artifact is not a valid evidence index.",
    "Artifact is not a valid runtime comparison report.",
    "Request failed.",
    "No structured breakdown",
  ]) {
    assert.ok(!combined.includes(english), `不应继续硬编码英文：${english}`);
  }
});
