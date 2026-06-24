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
    assert.equal(result.sourceMapAnalysis.bundleCount, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage builds analysis graphs and low-risk transform evidence", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-graphs-"));
  try {
    await mkdir(path.join(root, "assets"));
    await writeFile(
      path.join(root, "index.html"),
      '<div id="app"></div><script type="module" src="/assets/app.js"></script><link rel="stylesheet" href="/assets/app.css">'
    );
    await writeFile(
      path.join(root, "assets", "app.js"),
      [
        "import { helper } from './helper.js';",
        "const value = 1 + 2;",
        "const api = window['document'];",
        "helper(), console.log(value, api);",
        "export { value };",
        "//# sourceMappingURL=app.js.map"
      ].join("\n")
    );
    await writeFile(path.join(root, "assets", "helper.js"), "export function helper(){return 'ok'}");
    await writeFile(path.join(root, "assets", "app.css"), "#app{display:block}");
    await writeFile(
      path.join(root, "assets", "app.js.map"),
      JSON.stringify({
        version: 3,
        file: "app.js",
        sources: ["../src/main.ts"],
        sourcesContent: ["export const main = 1;"],
        names: [],
        mappings: ""
      })
    );

    const result = await analyzeInputPackage(root);

    assert.ok(result.graphAnalysis.chunkGraph.edges.some((edge) => edge.kind === "entry_includes_script"));
    assert.ok(result.graphAnalysis.moduleCandidateGraph.edges.some((edge) => edge.kind === "static_import" && edge.to === "script:assets/helper.js"));
    assert.ok(result.graphAnalysis.moduleCandidateGraph.nodes.some((node) => node.id === "source:src/main.ts"));
    assert.ok(result.transformAnalysis.transformLog.some((entry) => entry.kind === "computed_property_literal_restore" && entry.status === "applied"));
    assert.ok(result.transformAnalysis.transformLog.some((entry) => entry.kind === "low_risk_constant_fold" && entry.status === "applied"));
    assert.ok(result.transformAnalysis.transformLog.some((entry) => entry.kind === "sequence_expression_expand" && entry.status === "applied"));
    assert.ok(result.transformAnalysis.rollbackMap.length >= 3);
    assert.ok(result.transformAnalysis.scriptTransforms[0].transformedSource.includes("window.document"));
    assert.ok(result.transformAnalysis.scriptTransforms[0].transformedSource.includes("const value = 3"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage maps bundles to source candidates from source maps", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-sourcemap-"));
  try {
    await mkdir(path.join(root, "assets"));
    await writeFile(path.join(root, "index.html"), '<script type="module" src="/assets/app.js"></script>');
    await writeFile(path.join(root, "assets", "app.js"), "console.log('bundle');\n//# sourceMappingURL=app.js.map\n");
    await writeFile(
      path.join(root, "assets", "app.js.map"),
      JSON.stringify({
        version: 3,
        file: "app.js",
        sources: ["../src/main.ts", "webpack:///./src/util.ts"],
        sourcesContent: ["export const main = 1;", "export const util = 1;"],
        names: [],
        mappings: ""
      })
    );

    const result = await analyzeInputPackage(root);

    assert.equal(result.sourceMapAnalysis.bundleCount, 1);
    assert.equal(result.sourceMapAnalysis.bundleAnalyses[0].bundlePath, "assets/app.js");
    assert.deepEqual(result.sourceMapAnalysis.bundleAnalyses[0].sourcesContentAvailable, ["../src/main.ts", "src/util.ts"]);
    assert.ok(result.sourceMapAnalysis.sourceCandidates.includes("src/main.ts"));
    assert.ok(result.sourceMapAnalysis.sourceCandidates.includes("src/util.ts"));
    assert.equal(result.sourceMapAnalysis.warnings.length, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage records source map sourcesContent gaps", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-sourcemap-warning-"));
  try {
    await mkdir(path.join(root, "assets"));
    await writeFile(path.join(root, "index.html"), '<script type="module" src="/assets/app.js"></script>');
    await writeFile(path.join(root, "assets", "app.js"), "console.log('bundle');\n//# sourceMappingURL=app.js.map\n");
    await writeFile(
      path.join(root, "assets", "app.js.map"),
      JSON.stringify({
        version: 3,
        file: "app.js",
        sources: ["../src/main.ts", "../src/missing.ts"],
        sourcesContent: ["export const main = 1;", null],
        names: [],
        mappings: ""
      })
    );

    const result = await analyzeInputPackage(root);

    assert.deepEqual(result.sourceMapAnalysis.bundleAnalyses[0].missingSourcesContent, ["../src/missing.ts"]);
    assert.ok(result.sourceMapAnalysis.warnings.some((warning) => warning.includes("missing sourcesContent for 1 source")));
    assert.ok(result.inventory.warnings.some((warning) => warning.includes("missing sourcesContent for 1 source")));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage resolves local HTML entry references", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-html-refs-"));
  try {
    await mkdir(path.join(root, "nested", "assets"), { recursive: true });
    await writeFile(
      path.join(root, "nested", "index.html"),
      [
        '<link rel="modulepreload" href="/nested/assets/preload.chunk">',
        '<link rel="stylesheet" href="./assets/app.style?v=1">',
        '<link rel="icon" href="./assets/icon.hash">',
        '<img src="./assets/logo.hash?rev=2" srcset="./assets/logo@2x.hash 2x, https://cdn.example/logo@3x.png 3x">',
        '<video poster="./assets/poster.hash"><source src="./assets/movie.hash" /></video>',
        '<script type="module" src="./assets/main.chunk"></script>',
        '<script src="/nested/assets/main.chunk"></script>',
        '<script src="https://cdn.example/app.js"></script>',
        "<script>import './inline-ignored.js';</script>"
      ].join("")
    );
    await writeFile(path.join(root, "nested", "assets", "main.chunk"), "function main(){return 1} export { main };");
    await writeFile(path.join(root, "nested", "assets", "preload.chunk"), "function preload(){return 1} export { preload };");
    await writeFile(path.join(root, "nested", "assets", "app.style"), "#app{display:block}");
    await writeFile(path.join(root, "nested", "assets", "icon.hash"), "icon");
    await writeFile(path.join(root, "nested", "assets", "logo.hash"), "logo");
    await writeFile(path.join(root, "nested", "assets", "logo@2x.hash"), "logo2x");
    await writeFile(path.join(root, "nested", "assets", "poster.hash"), "poster");
    await writeFile(path.join(root, "nested", "assets", "movie.hash"), "movie");

    const result = await analyzeInputPackage(root);

    assert.deepEqual(result.inventory.entries, ["nested/index.html"]);
    assert.deepEqual(new Set(result.inventory.scripts), new Set(["nested/assets/main.chunk", "nested/assets/preload.chunk"]));
    assert.deepEqual(result.inventory.styles, ["nested/assets/app.style"]);
    assert.deepEqual(
      new Set(result.inventory.assets),
      new Set([
        "nested/assets/icon.hash",
        "nested/assets/logo.hash",
        "nested/assets/logo@2x.hash",
        "nested/assets/movie.hash",
        "nested/assets/poster.hash"
      ])
    );
    assert.equal(result.astIndexes.length, 2);
    assert.ok(result.astIndexes.some((index) => index.symbols.some((symbol) => symbol.name === "main")));
    assert.ok(result.astIndexes.some((index) => index.symbols.some((symbol) => symbol.name === "preload")));
    assert.ok(!result.inventory.scripts.some((script) => script.includes("cdn.example")));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage records missing local HTML entry references", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-html-missing-"));
  try {
    await writeFile(
      path.join(root, "index.html"),
      '<script src="./missing.js"></script><link rel="stylesheet" href="/missing.css">'
    );

    const result = await analyzeInputPackage(root);

    assert.ok(result.inventory.warnings.some((warning) => warning.includes("HTML references 2 missing files")));
    assert.ok(result.inventory.warnings.some((warning) => warning.includes("script:missing.js")));
    assert.ok(result.inventory.warnings.some((warning) => warning.includes("style:missing.css")));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage continues scanning after self-closing script tags", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-html-self-close-"));
  try {
    await mkdir(path.join(root, "assets"));
    await writeFile(
      path.join(root, "index.html"),
      '<script src="./assets/app.chunk" /><link rel="stylesheet" href="./assets/app.style">'
    );
    await writeFile(path.join(root, "assets", "app.chunk"), "function boot(){return 1} export { boot };");
    await writeFile(path.join(root, "assets", "app.style"), "#app{display:block}");

    const result = await analyzeInputPackage(root);

    assert.deepEqual(result.inventory.scripts, ["assets/app.chunk"]);
    assert.deepEqual(result.inventory.styles, ["assets/app.style"]);
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
        {
          path: "assets/app.js.map",
          content: JSON.stringify({
            version: 3,
            file: "app.js",
            sources: ["../src/from-zip.ts"],
            sourcesContent: ["export const fromZip = 1;"],
            names: [],
            mappings: ""
          })
        },
        { path: "assets/app.css", content: "#app{display:block}" }
      ])
    );

    const result = await analyzeInputPackage(archivePath);

    assert.deepEqual(result.inventory.entries, ["index.html"]);
    assert.deepEqual(result.inventory.scripts, ["assets/app.js"]);
    assert.deepEqual(result.inventory.styles, ["assets/app.css"]);
    assert.ok(result.inventory.warnings.some((warning) => warning.includes("zip archive")));
    assert.ok(result.astIndexes[0].symbols.some((symbol) => symbol.name === "fromZip"));
    assert.equal(result.sourceMapAnalysis.bundleAnalyses[0].bundlePath, "assets/app.js");
    assert.ok(result.sourceMapAnalysis.sourceCandidates.includes("src/from-zip.ts"));
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
      transformedSourceFiles: string[];
      analysisFiles: string[];
    };

    assert.equal(result.projectPath, outputDir);
    assert.equal(manifest.kind, "generated_project");
    assert.ok(manifest.copiedSourceFiles.includes("public/original/assets/app.js"));
    assert.ok(manifest.transformedSourceFiles.includes("src/transformed/assets/app.js"));
    assert.ok(manifest.analysisFiles.includes("src/analysis/graph-analysis.json"));
    const graphAnalysis = JSON.parse(await readFile(path.join(outputDir, "src", "analysis", "graph-analysis.json"), "utf8")) as {
      graphAnalysis: { chunkGraph: { edges: unknown[] } };
    };
    const transformLog = JSON.parse(await readFile(path.join(outputDir, "src", "analysis", "transform-log.json"), "utf8")) as {
      transformLog: unknown[];
    };
    assert.ok(graphAnalysis.graphAnalysis.chunkGraph.edges.length > 0);
    assert.ok(Array.isArray(transformLog.transformLog));
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
