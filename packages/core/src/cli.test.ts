import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
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
