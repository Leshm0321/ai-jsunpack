import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

test("core CLI emits inventory and AST artifact payloads", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-cli-"));
  try {
    await mkdir(path.join(root, "assets"));
    await writeFile(path.join(root, "index.html"), '<div id="app"></div><script type="module" src="/assets/app.js"></script>');
    await writeFile(path.join(root, "assets", "app.js"), "function boot(){return 1} export { boot };");

    const output = await runCli(["analyze", root, "--job-id", "job_cli_test"]);
    const parsed = JSON.parse(output) as {
      jobId: string;
      inventoryArtifactPayload: { kind: string; inventory: { entries: string[]; scripts: string[] } };
      astIndexArtifactPayload: { kind: string; astIndexes: Array<{ symbols: Array<{ name: string }> }> };
    };

    assert.equal(parsed.jobId, "job_cli_test");
    assert.equal(parsed.inventoryArtifactPayload.kind, "input_inventory");
    assert.equal(parsed.inventoryArtifactPayload.inventory.entries.length, 1);
    assert.equal(parsed.inventoryArtifactPayload.inventory.scripts.length, 1);
    assert.equal(parsed.astIndexArtifactPayload.kind, "ast_index");
    assert.ok(parsed.astIndexArtifactPayload.astIndexes[0].symbols.some((symbol) => symbol.name === "boot"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("core CLI reconstruct emits plan payload and generated project manifest", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-cli-reconstruct-"));
  try {
    const outputDir = path.join(root, "generated");
    await mkdir(path.join(root, "assets"));
    await writeFile(path.join(root, "index.html"), '<div id="app"></div><script type="module" src="/assets/app.js"></script>');
    await writeFile(path.join(root, "assets", "app.js"), "function boot(){return 1} export { boot };");

    const output = await runCli(["reconstruct", root, "--job-id", "job_cli_reconstruct", "--output-dir", outputDir]);
    const parsed = JSON.parse(output) as {
      jobId: string;
      generatedProjectPath: string;
      reconstructionPlanPayload: { kind: string; plan: { strategy: string } };
      generatedProjectManifestPayload: { kind: string; manifest: { copiedSourceFiles: string[] } };
    };
    const manifest = JSON.parse(await readFile(path.join(outputDir, "src", "reconstruction-manifest.json"), "utf8")) as {
      kind: string;
    };

    assert.equal(parsed.jobId, "job_cli_reconstruct");
    assert.equal(parsed.generatedProjectPath, outputDir);
    assert.equal(parsed.reconstructionPlanPayload.kind, "reconstruction_plan");
    assert.equal(parsed.reconstructionPlanPayload.plan.strategy, "static_host_project");
    assert.equal(parsed.generatedProjectManifestPayload.kind, "generated_project");
    assert.ok(parsed.generatedProjectManifestPayload.manifest.copiedSourceFiles.includes("public/original/assets/app.js"));
    assert.equal(manifest.kind, "generated_project");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

function runCli(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [fileURLToPath(new URL("./cli.js", import.meta.url)), ...args], {
      stdio: ["ignore", "pipe", "pipe"]
    });
    let stdout = "";
    let stderr = "";

    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) {
        resolve(stdout);
        return;
      }
      reject(new Error(stderr || `Core CLI exited with code ${code}`));
    });
  });
}
