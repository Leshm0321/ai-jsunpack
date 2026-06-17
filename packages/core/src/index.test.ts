import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile, mkdir, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { gzipSync } from "node:zlib";
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

test("analyzeInputPackage extracts zip archives into a safe inventory root", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-zip-"));
  try {
    const archivePath = path.join(root, "dist.zip");
    await writeFile(
      archivePath,
      makeZipArchive([
        { path: "index.html", content: '<div id="app"></div><script type="module" src="/assets/app.js"></script>' },
        { path: "assets/app.js", content: "function fromZip(){return 1} export { fromZip };" },
        { path: "assets/app.css", content: "#app{display:block}" }
      ])
    );

    const result = await analyzeInputPackage(archivePath);

    assert.deepEqual(result.inventory.entries, ["index.html"]);
    assert.deepEqual(result.inventory.scripts, ["assets/app.js"]);
    assert.deepEqual(result.inventory.styles, ["assets/app.css"]);
    assert.ok(result.inventory.warnings.some((warning) => warning.includes("zip archive")));
    assert.ok(result.astIndexes[0].symbols.some((symbol) => symbol.name === "fromZip"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage extracts tar and compressed tar archives", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-tar-"));
  try {
    const entries = [
      { path: "dist/index.html", content: '<script type="module" src="./app.js"></script>' },
      { path: "dist/app.js", content: "function fromTar(){return 1} export { fromTar };" }
    ];
    const tarArchive = makeTarArchive(entries);
    const tarPath = path.join(root, "dist.tar");
    const tgzPath = path.join(root, "dist.tgz");
    await writeFile(tarPath, tarArchive);
    await writeFile(tgzPath, gzipSync(tarArchive));

    const tarResult = await analyzeInputPackage(tarPath);
    const tgzResult = await analyzeInputPackage(tgzPath);

    assert.deepEqual(tarResult.inventory.entries, ["dist/index.html"]);
    assert.deepEqual(tarResult.inventory.scripts, ["dist/app.js"]);
    assert.ok(tarResult.astIndexes[0].symbols.some((symbol) => symbol.name === "fromTar"));
    assert.deepEqual(tgzResult.inventory.entries, ["dist/index.html"]);
    assert.deepEqual(tgzResult.inventory.scripts, ["dist/app.js"]);
    assert.ok(tgzResult.inventory.warnings.some((warning) => warning.includes("tar_gz archive")));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage rejects archive paths that escape the extraction root", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-unsafe-"));
  try {
    const unsafeArchives = [
      makeZipArchive([{ path: "../evil.js", content: "alert(1)" }]),
      makeZipArchive([{ path: "assets\\..\\evil.js", content: "alert(1)" }]),
      makeZipArchive([{ path: "C:/tmp/evil.js", content: "alert(1)" }]),
      makeTarArchive([{ path: "/tmp/evil.js", content: "alert(1)" }]),
      makeTarArchive([{ path: "assets/../../evil.js", content: "alert(1)" }])
    ];

    for (const [index, archive] of unsafeArchives.entries()) {
      const archivePath = path.join(root, `unsafe-${index}${index < 3 ? ".zip" : ".tar"}`);
      await writeFile(archivePath, archive);
      await assert.rejects(() => analyzeInputPackage(archivePath), /Unsafe archive entry path/);
    }
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

test("writeProject accepts archive input and copies extracted sources", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-writer-zip-"));
  try {
    const archivePath = path.join(root, "dist.zip");
    await writeFile(
      archivePath,
      makeZipArchive([
        { path: "index.html", content: '<div id="app"></div><script type="module" src="/assets/app.js"></script>' },
        { path: "assets/app.js", content: "function archivedBoot(){return 1} export { archivedBoot };" }
      ])
    );

    const analysis = await analyzeInputPackage(archivePath, { jobId: "job_archive_writer_test" });
    const plan = planReconstruction(analysis, { jobId: "job_archive_writer_test" });
    const outputDir = path.join(root, "generated_project");
    const result = await writeProject(plan, { inputPath: archivePath, outputDir });
    const manifest = JSON.parse(await readFile(path.join(outputDir, "src", "reconstruction-manifest.json"), "utf8")) as {
      copiedSourceFiles: string[];
    };

    assert.equal(result.projectPath, outputDir);
    assert.ok(manifest.copiedSourceFiles.includes("public/original/assets/app.js"));
    assert.equal(
      await readFile(path.join(outputDir, "public", "original", "assets", "app.js"), "utf8"),
      "function archivedBoot(){return 1} export { archivedBoot };"
    );
    await runScript(outputDir, ["scripts/typecheck.mjs"]);
    await runScript(outputDir, ["scripts/build.mjs"]);
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

function makeTarArchive(entries: ArchiveEntry[]): Buffer {
  const parts: Buffer[] = [];
  for (const entry of entries) {
    const name = Buffer.from(entry.path, "utf8");
    assert.ok(name.length <= 100, "test tar helper only supports short names");
    const content = Buffer.isBuffer(entry.content) ? entry.content : Buffer.from(entry.content, "utf8");
    const header = Buffer.alloc(512, 0);
    name.copy(header, 0);
    writeTarOctal(header, 100, 8, 0o644);
    writeTarOctal(header, 108, 8, 0);
    writeTarOctal(header, 116, 8, 0);
    writeTarOctal(header, 124, 12, content.length);
    writeTarOctal(header, 136, 12, 0);
    header.fill(0x20, 148, 156);
    header.write("0", 156, "ascii");
    header.write("ustar", 257, "ascii");
    header.write("00", 263, "ascii");
    const checksum = header.reduce((sum, byte) => sum + byte, 0);
    writeTarOctal(header, 148, 8, checksum);
    parts.push(header, content, Buffer.alloc((512 - (content.length % 512)) % 512, 0));
  }
  parts.push(Buffer.alloc(1024, 0));
  return Buffer.concat(parts);
}

function writeTarOctal(buffer: Buffer, offset: number, length: number, value: number): void {
  const text = value.toString(8).padStart(length - 1, "0");
  buffer.write(`${text}\0`, offset, length, "ascii");
}
