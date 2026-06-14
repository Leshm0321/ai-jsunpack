import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile, mkdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { analyzeInputPackage } from "./index.js";

test("analyzeInputPackage inventories dist assets and indexes bundle symbols", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-"));
  try {
    await mkdir(path.join(root, "assets"));
    await writeFile(path.join(root, "index.html"), '<div id="app"></div><script type="module" src="/assets/app.js"></script>');
    await writeFile(path.join(root, "assets", "app.js"), "function n(){return 1} const value=n(); export { value };");
    await writeFile(path.join(root, "assets", "app.css"), "#app{color:#0369a1}");

    const result = await analyzeInputPackage(root);

    assert.equal(result.inventory.entries.length, 1);
    assert.equal(result.inventory.scripts.length, 1);
    assert.equal(result.inventory.styles.length, 1);
    assert.equal(result.astIndexes.length, 1);
    assert.ok(result.astIndexes[0].symbols.some((symbol) => symbol.name === "n"));
    assert.ok(result.detectedRuntime.includes("vite_or_rollup"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

