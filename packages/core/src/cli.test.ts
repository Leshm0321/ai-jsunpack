import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

test("Core CLI 输出 inventory 和 AST Artifact payload", async () => {
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

test("Core CLI 分析归档输入", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-cli-zip-"));
  try {
    const archivePath = path.join(root, "dist.zip");
    await writeFile(
      archivePath,
      makeZipArchive([
        { path: "index.html", content: '<script type="module" src="/assets/app.js"></script>' },
        { path: "assets/app.js", content: "function cliZip(){return 1} export { cliZip };" }
      ])
    );

    const output = await runCli(["analyze", archivePath, "--job-id", "job_cli_zip"]);
    const parsed = JSON.parse(output) as {
      inventoryArtifactPayload: { inventory: { entries: string[]; scripts: string[]; warnings: string[] } };
      astIndexArtifactPayload: { astIndexes: Array<{ symbols: Array<{ name: string }> }> };
    };

    assert.deepEqual(parsed.inventoryArtifactPayload.inventory.entries, ["index.html"]);
    assert.deepEqual(parsed.inventoryArtifactPayload.inventory.scripts, ["assets/app.js"]);
    assert.ok(parsed.inventoryArtifactPayload.inventory.warnings.some((warning) => warning.includes("zip 归档")));
    assert.ok(parsed.astIndexArtifactPayload.astIndexes[0].symbols.some((symbol) => symbol.name === "cliZip"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Core CLI 分析单个 JavaScript 输入", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-cli-single-js-"));
  try {
    const inputPath = path.join(root, "agentApi.js");
    await writeFile(inputPath, "function cliSingle(){return 1} const value = cliSingle();");

    const output = await runCli(["analyze", inputPath, "--job-id", "job_cli_single_js"]);
    const parsed = JSON.parse(output) as {
      inventoryArtifactPayload: { inventory: { entries: string[]; scripts: string[]; warnings: string[]; isSingleBundle: boolean } };
      astIndexArtifactPayload: { astIndexes: Array<{ symbols: Array<{ name: string }> }>; detectedRuntime: string[] };
    };

    assert.deepEqual(parsed.inventoryArtifactPayload.inventory.entries, ["index.html"]);
    assert.deepEqual(parsed.inventoryArtifactPayload.inventory.scripts, ["agentApi.js"]);
    assert.equal(parsed.inventoryArtifactPayload.inventory.isSingleBundle, true);
    assert.ok(parsed.inventoryArtifactPayload.inventory.warnings.some((warning) => warning.includes("single_script 文件已封装")));
    assert.ok(parsed.astIndexArtifactPayload.detectedRuntime.includes("single_bundle_best_effort"));
    assert.ok(parsed.astIndexArtifactPayload.astIndexes[0].symbols.some((symbol) => symbol.name === "cliSingle"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Core CLI 重建命令输出 plan payload 和 generated project Manifest", async () => {
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

test("Core CLI 重建命令接受归档输入", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-cli-reconstruct-zip-"));
  try {
    const archivePath = path.join(root, "dist.zip");
    const outputDir = path.join(root, "generated");
    await writeFile(
      archivePath,
      makeZipArchive([
        { path: "index.html", content: '<script type="module" src="/assets/app.js"></script>' },
        { path: "assets/app.js", content: "function cliReconstructZip(){return 1} export { cliReconstructZip };" }
      ])
    );

    const output = await runCli(["reconstruct", archivePath, "--job-id", "job_cli_reconstruct_zip", "--output-dir", outputDir]);
    const parsed = JSON.parse(output) as {
      generatedProjectManifestPayload: { manifest: { copiedSourceFiles: string[]; limitations: string[] } };
    };
    const copiedSource = await readFile(path.join(outputDir, "public", "original", "assets", "app.js"), "utf8");

    assert.ok(parsed.generatedProjectManifestPayload.manifest.copiedSourceFiles.includes("public/original/assets/app.js"));
    assert.ok(parsed.generatedProjectManifestPayload.manifest.limitations.some((limitation) => limitation.includes("zip 归档")));
    assert.equal(copiedSource, "function cliReconstructZip(){return 1} export { cliReconstructZip };");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Core CLI 重建命令接受单个 JavaScript 输入", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-cli-reconstruct-single-js-"));
  try {
    const inputPath = path.join(root, "agentApi.js");
    const outputDir = path.join(root, "generated");
    await writeFile(inputPath, "function cliReconstructSingle(){return 1} const value = cliReconstructSingle();");

    const output = await runCli(["reconstruct", inputPath, "--job-id", "job_cli_reconstruct_single_js", "--output-dir", outputDir]);
    const parsed = JSON.parse(output) as {
      generatedProjectManifestPayload: { manifest: { copiedSourceFiles: string[]; limitations: string[] } };
    };
    const copiedScript = await readFile(path.join(outputDir, "public", "original", "agentApi.js"), "utf8");
    const hostHtml = await readFile(path.join(outputDir, "public", "original", "index.html"), "utf8");

    assert.ok(parsed.generatedProjectManifestPayload.manifest.copiedSourceFiles.includes("public/original/agentApi.js"));
    assert.ok(parsed.generatedProjectManifestPayload.manifest.copiedSourceFiles.includes("public/original/index.html"));
    assert.ok(parsed.generatedProjectManifestPayload.manifest.limitations.some((limitation) => limitation.includes("single_script 文件已封装")));
    assert.equal(copiedScript, "function cliReconstructSingle(){return 1} const value = cliReconstructSingle();");
    assert.ok(hostHtml.includes('<script src="./agentApi.js"></script>'));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Core CLI 拒绝不安全的归档条目", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-cli-unsafe-"));
  try {
    const archivePath = path.join(root, "unsafe.zip");
    await writeFile(archivePath, makeZipArchive([{ path: "../evil.js", content: "alert(1)" }]));

    await assert.rejects(
      () => runCli(["analyze", archivePath, "--job-id", "job_cli_unsafe"]),
      /不安全的归档条目路径/
    );
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
      reject(new Error(stderr || `Core CLI 已退出，退出码 ${code}`));
    });
  });
}

interface ArchiveEntry {
  path: string;
  content: string | Buffer;
}

function makeZipArchive(entries: ArchiveEntry[]): Buffer {
  const localParts: Buffer[] = [];
  const centralParts: Buffer[] = [];
  let offset = 0;

  for (const entry of entries) {
    const name = Buffer.from(entry.path, "utf8");
    const content = Buffer.isBuffer(entry.content) ? entry.content : Buffer.from(entry.content, "utf8");
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 6);
    local.writeUInt16LE(0, 8);
    local.writeUInt32LE(0, 14);
    local.writeUInt32LE(content.length, 18);
    local.writeUInt32LE(content.length, 22);
    local.writeUInt16LE(name.length, 26);
    local.writeUInt16LE(0, 28);
    localParts.push(local, name, content);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0, 8);
    central.writeUInt16LE(0, 10);
    central.writeUInt32LE(0, 16);
    central.writeUInt32LE(content.length, 20);
    central.writeUInt32LE(content.length, 24);
    central.writeUInt16LE(name.length, 28);
    central.writeUInt16LE(0, 30);
    central.writeUInt16LE(0, 32);
    central.writeUInt32LE(0, 38);
    central.writeUInt32LE(offset, 42);
    centralParts.push(central, name);

    offset += local.length + name.length + content.length;
  }

  const localSection = Buffer.concat(localParts);
  const centralSection = Buffer.concat(centralParts);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralSection.length, 12);
  end.writeUInt32LE(localSection.length, 16);
  end.writeUInt16LE(0, 20);
  return Buffer.concat([localSection, centralSection, end]);
}
