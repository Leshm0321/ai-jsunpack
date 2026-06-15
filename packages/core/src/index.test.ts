import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile, mkdir, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { analyzeInputPackage, planReconstruction, writeProject } from "./index.js";

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

test("writeProject emits a buildable generated project shell", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-writer-"));
  try {
    await mkdir(path.join(root, "assets"));
    await writeFile(path.join(root, "index.html"), '<div id="app"></div><script type="module" src="/assets/app.js"></script>');
    await writeFile(path.join(root, "assets", "app.js"), "function boot(){return 1} export { boot };");
    await writeFile(path.join(root, "assets", "app.css"), "#app{color:#0369a1}");

    const analysis = await analyzeInputPackage(root, { jobId: "job_writer_test" });
    const plan = planReconstruction(analysis, { jobId: "job_writer_test" });
    const outputDir = path.join(root, "generated_project");
    const result = await writeProject(plan, { inputPath: root, outputDir });
    const manifest = JSON.parse(await readFile(path.join(outputDir, "src", "reconstruction-manifest.json"), "utf8")) as {
      kind: string;
      copiedSourceFiles: string[];
    };

    assert.equal(result.projectPath, outputDir);
    assert.equal(manifest.kind, "generated_project");
    assert.ok(manifest.copiedSourceFiles.includes("public/original/assets/app.js"));
    await runScript(outputDir, ["scripts/typecheck.mjs"]);
    await runScript(outputDir, ["scripts/build.mjs"]);
    const buildManifest = JSON.parse(await readFile(path.join(outputDir, "dist", "build-manifest.json"), "utf8")) as {
      status: string;
    };
    assert.equal(buildManifest.status, "pass");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

function runScript(cwd: string, args: string[]): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, { cwd, stdio: ["ignore", "pipe", "pipe"] });
    let stderr = "";
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) {
        resolve();
        return;
      }
      reject(new Error(stderr || `Generated project script exited with code ${code}`));
    });
  });
}
