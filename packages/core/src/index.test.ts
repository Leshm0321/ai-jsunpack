import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile, mkdir, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";
import { deflateRawSync, gzipSync } from "node:zlib";
import { analyzeInputPackage, planReconstruction, writeProject } from "./index.js";

test("analyzeInputPackage 清点 dist 资源并索引 bundle 符号", async () => {
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

test("analyzeInputPackage 接受单个 JavaScript 文件作为封装后的静态输入", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-single-js-"));
  try {
    const inputPath = path.join(root, "agentApi.js");
    await writeFile(inputPath, "function singleUploadBoot(){return 1} const value = singleUploadBoot();");

    const result = await analyzeInputPackage(inputPath);

    assert.deepEqual(result.inventory.entries, ["index.html"]);
    assert.deepEqual(result.inventory.scripts, ["agentApi.js"]);
    assert.equal(result.inventory.isSingleBundle, true);
    assert.ok(result.inventory.warnings.some((warning) => warning.includes("single_script 文件已封装")));
    assert.ok(result.detectedRuntime.includes("single_bundle_best_effort"));
    assert.ok(result.astIndexes[0].symbols.some((symbol) => symbol.name === "singleUploadBoot"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage 构建分析图和低风险转换证据", async () => {
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
    assert.ok(result.transformAnalysis.transformLog.every((entry) => entry.kind === "parse" || entry.riskLevel));
    assert.ok(result.transformAnalysis.transformLog.some((entry) => entry.evidenceRefs?.includes("script:assets/app.js")));
    assert.ok(result.transformAnalysis.rollbackMap.length >= 3);
    assert.ok(result.transformAnalysis.rollbackMap.every((entry) => entry.riskLevel && entry.reversible === true));
    assert.ok(result.transformAnalysis.scriptTransforms[0].transformedSource.includes("window.document"));
    assert.ok(result.transformAnalysis.scriptTransforms[0].transformedSource.includes("const value = 3"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage 静态恢复轮转字符串数组解码器调用", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-string-decoder-"));
  try {
    const inputPath = path.join(root, "bundle.js");
    await writeFile(
      inputPath,
      [
        "const lookup = decode;",
        "(function(factory, target){",
        "  const d = decode, values = factory();",
        "  while (!![]) {",
        "    try {",
        "      const checksum = parseInt(d(0x10)) + parseInt(d(0x11));",
        "      if (checksum === target) break;",
        "      values.push(values.shift());",
        "    } catch (error) { values.push(values.shift()); }",
        "  }",
        "})(strings, 30);",
        "export const message = lookup(0x12) + ' ' + lookup(0x13);",
        "function decode(value){",
        "  const values = strings();",
        "  return decode = function(index){ index = index - 0x10; return values[index]; }, decode(value);",
        "}",
        "function strings(){",
        "  const values = ['20', 'hello', 'world', '10'];",
        "  strings = function(){ return values; };",
        "  return strings();",
        "}"
      ].join("\n")
    );

    const result = await analyzeInputPackage(inputPath);
    const transformed = result.transformAnalysis.scriptTransforms[0];

    assert.ok(result.transformAnalysis.transformLog.some((entry) => entry.kind === "string_array_decoder_restore" && entry.status === "applied"));
    assert.ok(transformed.transforms.includes("string_array_decoder_restore"));
    assert.ok(transformed.transformedSource.includes("hello"));
    assert.ok(transformed.transformedSource.includes("world"));
    assert.ok(!transformed.transformedSource.includes("lookup(0x12)"));
    assert.ok(!transformed.transformedSource.includes("lookup(0x13)"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage 记录 webpack runtime wrapper 和 module table 候选", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-webpack-"));
  try {
    await mkdir(path.join(root, "assets"));
    await writeFile(path.join(root, "index.html"), '<script src="./assets/bundle.js"></script>');
    await writeFile(
      path.join(root, "assets", "bundle.js"),
      [
        "(function(modules){",
        "  function __webpack_require__(id){ return modules[id]({}, {}, __webpack_require__); }",
        "  return __webpack_require__(0);",
        "})({",
        "  0: function(module, exports, __webpack_require__){ const dep = __webpack_require__(1); module.exports = dep; },",
        "  1: function(module){ module.exports = { value: 1 }; }",
        "});"
      ].join("\n")
    );

    const result = await analyzeInputPackage(root);

    assert.ok(result.moduleRecoveryAnalysis.runtimeWrappers.some((wrapper) => wrapper.runtimeKind === "webpack"));
    assert.ok(result.moduleRecoveryAnalysis.moduleBoundaries.some((boundary) => boundary.boundaryKind === "runtime_module_table" && boundary.moduleId === "0"));
    assert.ok(result.moduleRecoveryAnalysis.moduleBoundaries.some((boundary) => boundary.moduleId === "1"));
    assert.ok(result.moduleRecoveryAnalysis.importExportCandidates.some((candidate) => candidate.candidateKind === "runtime_dependency" && candidate.runtimeKind === "webpack"));
    assert.ok(result.moduleRecoveryAnalysis.runtimeWrappers.every((wrapper) => wrapper.sourceRange || wrapper.loc || wrapper.evidenceRefs.length > 0));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage 记录 UMD 和 SystemJS 包装器证据", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-umd-system-"));
  try {
    await writeFile(
      path.join(root, "umd.js"),
      [
        "(function(root, factory){",
        "  if (typeof exports === 'object') module.exports = factory();",
        "  else if (typeof define === 'function' && define.amd) define([], factory);",
        "  else root.Lib = factory();",
        "})(this, function(){ return { boot: function(){ return 1; } }; });"
      ].join("\n")
    );
    await writeFile(
      path.join(root, "system.js"),
      "System.register('app', [], function(exports){ return { execute: function(){ exports('value', 1); } }; });"
    );

    const result = await analyzeInputPackage(root);

    assert.ok(result.moduleRecoveryAnalysis.runtimeWrappers.some((wrapper) => wrapper.runtimeKind === "umd"));
    assert.ok(result.moduleRecoveryAnalysis.runtimeWrappers.some((wrapper) => wrapper.runtimeKind === "systemjs"));
    assert.ok(result.moduleRecoveryAnalysis.moduleBoundaries.some((boundary) => boundary.boundaryKind === "system_register"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage 记录 ESM 导入导出候选和 source map 恢复模块", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-module-recovery-"));
  try {
    await mkdir(path.join(root, "assets"));
    await writeFile(path.join(root, "index.html"), '<script type="module" src="/assets/app.js"></script>');
    await writeFile(
      path.join(root, "assets", "app.js"),
      [
        "import { helper } from './helper.js';",
        "export const value = helper();",
        "export { value as renamed };",
        "import('./lazy.js').then((mod) => mod.run());",
        "//# sourceMappingURL=app.js.map"
      ].join("\n")
    );
    await writeFile(path.join(root, "assets", "helper.js"), "export function helper(){return 1}");
    await writeFile(path.join(root, "assets", "lazy.js"), "export function run(){return 2}");
    await writeFile(
      path.join(root, "assets", "app.js.map"),
      JSON.stringify({
        version: 3,
        file: "app.js",
        sources: ["../src/main.ts", "../src/lazy.ts"],
        sourcesContent: ["export const main = 1;", "export const lazy = 2;"],
        names: [],
        mappings: ""
      })
    );

    const result = await analyzeInputPackage(root);

    assert.equal(result.sourceMapAnalysis.bundleAnalyses[0].recoveredSources.length, 2);
    assert.ok(result.moduleRecoveryAnalysis.generatedModules.some((module) => module.sourceKind === "source_map_sources_content"));
    assert.ok(result.moduleRecoveryAnalysis.importExportCandidates.some((candidate) => candidate.candidateKind === "static_import" && candidate.source === "./helper.js"));
    assert.ok(result.moduleRecoveryAnalysis.importExportCandidates.some((candidate) => candidate.candidateKind === "dynamic_import" && candidate.source === "./lazy.js"));
    assert.ok(result.moduleRecoveryAnalysis.importExportCandidates.some((candidate) => candidate.candidateKind === "named_export"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage 通过 source map 将 bundle 映射到源候选", async () => {
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

test("analyzeInputPackage 记录 source map 的 sourcesContent 缺口", async () => {
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
    assert.ok(result.sourceMapAnalysis.warnings.some((warning) => warning.includes("缺少 1 个源的 sourcesContent")));
    assert.ok(result.inventory.warnings.some((warning) => warning.includes("缺少 1 个源的 sourcesContent")));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage 解析本地 HTML 入口引用", async () => {
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

test("analyzeInputPackage 记录缺失的本地 HTML 入口引用", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-html-missing-"));
  try {
    await writeFile(
      path.join(root, "index.html"),
      '<script src="./missing.js"></script><link rel="stylesheet" href="/missing.css">'
    );

    const result = await analyzeInputPackage(root);

    assert.ok(result.inventory.warnings.some((warning) => warning.includes("HTML 引用了 2 个缺失文件")));
    assert.ok(result.inventory.warnings.some((warning) => warning.includes("script:missing.js")));
    assert.ok(result.inventory.warnings.some((warning) => warning.includes("style:missing.css")));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage 在自闭合 script 标签后继续扫描", async () => {
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

test("analyzeInputPackage 将 zip 归档解压到安全的 inventory root", async () => {
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
    assert.ok(result.inventory.warnings.some((warning) => warning.includes("zip 归档")));
    assert.ok(result.astIndexes[0].symbols.some((symbol) => symbol.name === "fromZip"));
    assert.equal(result.sourceMapAnalysis.bundleAnalyses[0].bundlePath, "assets/app.js");
    assert.ok(result.sourceMapAnalysis.sourceCandidates.includes("src/from-zip.ts"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage 解压 tar 和压缩 tar 归档", async () => {
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
    assert.ok(tgzResult.inventory.warnings.some((warning) => warning.includes("tar_gz 归档")));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage 拒绝逃逸解压根目录的归档路径", async () => {
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
      await assert.rejects(() => analyzeInputPackage(archivePath), /不安全的归档条目路径/);
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("analyzeInputPackage 拒绝超出解压资源限制的归档条目", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-archive-limits-"));
  try {
    const zipBombPath = path.join(root, "ratio.zip");
    await writeFile(
      zipBombPath,
      makeZipArchive([{ path: "large.txt", content: Buffer.alloc(1024 * 1024, 0x41), compress: true }])
    );
    await assert.rejects(analyzeInputPackage(zipBombPath), /归档资源限制已超出.*压缩比/i);

    const oversizedTarPath = path.join(root, "oversized.tar");
    const oversizedHeader = makeTarArchive([{ path: "large.js", content: "" }]);
    writeTarOctal(oversizedHeader, 124, 12, 64 * 1024 * 1024 + 1);
    await writeFile(oversizedTarPath, oversizedHeader);
    await assert.rejects(analyzeInputPackage(oversizedTarPath), /归档资源限制已超出.*最多/i);

    const tooManyZipEntriesPath = path.join(root, "entries.zip");
    const tooManyEntries = makeZipArchive([]);
    tooManyEntries.writeUInt16LE(10_001, tooManyEntries.length - 12);
    await writeFile(tooManyZipEntriesPath, tooManyEntries);
    await assert.rejects(analyzeInputPackage(tooManyZipEntriesPath), /归档资源限制已超出.*条目/i);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("writeProject 输出可构建的生成项目外壳", async () => {
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
      generatedModuleFiles: string[];
      entrypointFiles: string[];
      typeDefinitionFiles: string[];
      runtimeShimFiles: string[];
      analysisFiles: string[];
    };

    assert.equal(result.projectPath, outputDir);
    assert.equal(manifest.kind, "generated_project");
    assert.ok(manifest.copiedSourceFiles.includes("public/original/assets/app.js"));
    assert.ok(manifest.transformedSourceFiles.includes("src/transformed/assets/app.js"));
    assert.ok(manifest.generatedModuleFiles.includes("src/modules/module-index.ts"));
    assert.ok(manifest.entrypointFiles.includes("src/entrypoints/reconstruction-entry.ts"));
    assert.ok(manifest.typeDefinitionFiles.includes("src/types/reconstruction.d.ts"));
    assert.ok(manifest.runtimeShimFiles.includes("src/runtime-shims/browser-globals.ts"));
    assert.ok(manifest.analysisFiles.includes("src/analysis/graph-analysis.json"));
    assert.ok(manifest.analysisFiles.includes("src/analysis/runtime-wrappers.json"));
    assert.ok(manifest.analysisFiles.includes("src/analysis/module-boundaries.json"));
    assert.ok(manifest.analysisFiles.includes("src/analysis/import-export-candidates.json"));
    assert.ok(manifest.analysisFiles.includes("src/analysis/generated-modules.json"));
    const graphAnalysis = JSON.parse(await readFile(path.join(outputDir, "src", "analysis", "graph-analysis.json"), "utf8")) as {
      graphAnalysis: { chunkGraph: { edges: unknown[] } };
    };
    const transformLog = JSON.parse(await readFile(path.join(outputDir, "src", "analysis", "transform-log.json"), "utf8")) as {
      transformLog: unknown[];
    };
    assert.ok(graphAnalysis.graphAnalysis.chunkGraph.edges.length > 0);
    assert.ok(Array.isArray(transformLog.transformLog));
    const moduleBoundaries = JSON.parse(await readFile(path.join(outputDir, "src", "analysis", "module-boundaries.json"), "utf8")) as {
      moduleBoundaries: unknown[];
    };
    assert.ok(Array.isArray(moduleBoundaries.moduleBoundaries));
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

test("writeProject 接受归档输入并复制解压后的源文件", async () => {
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

test("writeProject 接受单个 JavaScript 输入并复制封装后的静态源文件", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-writer-single-js-"));
  try {
    const inputPath = path.join(root, "agentApi.js");
    await writeFile(inputPath, "function singleWriterBoot(){return 1} const value = singleWriterBoot();");

    const analysis = await analyzeInputPackage(inputPath, { jobId: "job_single_writer_test" });
    const plan = planReconstruction(analysis, { jobId: "job_single_writer_test" });
    const outputDir = path.join(root, "generated_project");
    const result = await writeProject(plan, { inputPath, outputDir });
    const manifest = JSON.parse(await readFile(path.join(outputDir, "src", "reconstruction-manifest.json"), "utf8")) as {
      copiedSourceFiles: string[];
      limitations: string[];
    };

    assert.equal(result.projectPath, outputDir);
    assert.equal(plan.entryHtml, "index.html");
    assert.ok(manifest.copiedSourceFiles.includes("public/original/agentApi.js"));
    assert.ok(manifest.copiedSourceFiles.includes("public/original/index.html"));
    assert.ok(manifest.limitations.some((limitation) => limitation.includes("single_script 文件已封装")));
    assert.equal(
      await readFile(path.join(outputDir, "public", "original", "agentApi.js"), "utf8"),
      "function singleWriterBoot(){return 1} const value = singleWriterBoot();"
    );
    assert.ok(
      (await readFile(path.join(outputDir, "public", "original", "index.html"), "utf8")).includes(
        '<script src="./agentApi.js"></script>'
      )
    );
    await runScript(outputDir, ["scripts/typecheck.mjs"]);
    await runScript(outputDir, ["scripts/build.mjs"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("writeProject 将使用 ESM 语法的单个 .js 输入作为模块加载", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-writer-single-js-esm-"));
  try {
    const inputPath = path.join(root, "agentApi.js");
    await writeFile(inputPath, "import { generateText } from './aiTextApi.js'; export const plan = generateText();");

    const analysis = await analyzeInputPackage(inputPath, { jobId: "job_single_writer_esm_test" });
    const plan = planReconstruction(analysis, { jobId: "job_single_writer_esm_test" });
    const outputDir = path.join(root, "generated_project");
    await writeProject(plan, { inputPath, outputDir });

    const hostHtml = await readFile(path.join(outputDir, "public", "original", "index.html"), "utf8");
    assert.ok(hostHtml.includes('<script type="module" src="./agentApi.js"></script>'));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("缺失的静态相对 ESM 依赖生成经过审计且会抛错的占位模块", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-missing-dependency-"));
  try {
    await writeFile(
      path.join(root, "index.html"),
      '<script type="module" src="./main.js"></script>'
    );
    await writeFile(
      path.join(root, "main.js"),
      [
        "import missingDefault, { generateText as text } from './aiTextApi.js';",
        "import * as missingNamespace from './aiTextApi.js';",
        "import './aiTextApi.js';",
        "export { generateText as forwardedText } from './aiTextApi.js';",
        "export * from './aiTextApi.js';",
        "globalThis.__placeholderBindings = [missingDefault, text, missingNamespace];"
      ].join("\n")
    );

    const analysis = await analyzeInputPackage(root, { jobId: "job_missing_dependency" });
    assert.equal(analysis.dependencyPlaceholders.length, 1);
    assert.deepEqual(analysis.dependencyPlaceholders[0], {
      importerPath: "main.js",
      specifier: "./aiTextApi.js",
      resolvedPath: "aiTextApi.js",
      importedNames: ["generateText"],
      reExportedNames: ["forwardedText"],
      defaultImport: true,
      namespaceImport: true,
      sideEffectOnly: true,
      exportAll: true,
      reason: "missing_static_relative_dependency",
      status: "generated",
      limitation: "仅提供加载连续性；依赖的语义行为不可用，生成的导出在调用时会抛出异常。"
    });

    const plan = planReconstruction(analysis, { jobId: "job_missing_dependency" });
    assert.equal(plan.evidenceSummary.dependencyPlaceholderCount, 1);
    assert.ok(plan.limitations.some((limitation) => limitation.includes("仅加载占位模块")));
    const outputDir = path.join(root, "generated_project");
    await writeProject(plan, { inputPath: root, outputDir });

    const manifest = JSON.parse(await readFile(path.join(outputDir, "src", "reconstruction-manifest.json"), "utf8")) as {
      dependencyPlaceholderFiles: string[];
      dependencyPlaceholders: Array<{ resolvedPath: string; status: string }>;
      analysisFiles: string[];
    };
    assert.ok(manifest.dependencyPlaceholderFiles.includes("aiTextApi.js"));
    assert.ok(manifest.dependencyPlaceholderFiles.includes("public/original/aiTextApi.js"));
    assert.deepEqual(manifest.dependencyPlaceholders, plan.dependencyPlaceholders);
    assert.ok(manifest.analysisFiles.includes("src/analysis/dependency-placeholders.json"));
    const analysisPayload = JSON.parse(
      await readFile(path.join(outputDir, "src", "analysis", "dependency-placeholders.json"), "utf8")
    ) as { contract: { errorCode: string; semanticBehaviorAvailable: boolean } };
    assert.equal(analysisPayload.contract.errorCode, "AI_JSUNPACK_MISSING_DEPENDENCY");
    assert.equal(analysisPayload.contract.semanticBehaviorAvailable, false);

    const placeholderUrl = `${pathToFileURL(path.join(outputDir, "aiTextApi.js")).href}?test=${Date.now()}`;
    const placeholder = await import(placeholderUrl) as {
      default: () => never;
      generateText: () => never;
    };
    for (const invoke of [placeholder.default, placeholder.generateText]) {
      assert.throws(invoke, (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.name, "MissingDependencyPlaceholderError");
        assert.equal((error as Error & { code?: string }).code, "AI_JSUNPACK_MISSING_DEPENDENCY");
        return true;
      });
    }
    await assert.rejects(readFile(path.join(root, "aiTextApi.js"), "utf8"), /ENOENT/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("现有相对 ESM 依赖不会生成占位模块", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-existing-dependency-"));
  try {
    await writeFile(path.join(root, "main.js"), "import { helper } from './helper.js'; helper();");
    await writeFile(path.join(root, "helper.js"), "export function helper(){ return 'ok'; }");
    const analysis = await analyzeInputPackage(root);
    assert.deepEqual(analysis.dependencyPlaceholders, []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("逃逸输入根目录的相对依赖仅保留在报告中", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-unsafe-dependency-"));
  try {
    const inputPath = path.join(root, "main.js");
    await writeFile(inputPath, "import { outside } from '../outside.js'; outside();");
    const analysis = await analyzeInputPackage(inputPath);
    assert.equal(analysis.dependencyPlaceholders.length, 1);
    assert.equal(analysis.dependencyPlaceholders[0].status, "unsupported");
    assert.equal(analysis.dependencyPlaceholders[0].reason, "unsafe_relative_dependency");
    assert.equal(analysis.dependencyPlaceholders[0].resolvedPath, null);
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
      reject(new Error(stderr || `生成项目脚本已退出，退出码 ${code}`));
    });
  });
}

interface ArchiveEntry {
  path: string;
  content: string | Buffer;
  compress?: boolean;
}

function makeZipArchive(entries: ArchiveEntry[]): Buffer {
  const localParts: Buffer[] = [];
  const centralParts: Buffer[] = [];
  let offset = 0;

  for (const entry of entries) {
    const name = Buffer.from(entry.path, "utf8");
    const content = Buffer.isBuffer(entry.content) ? entry.content : Buffer.from(entry.content, "utf8");
    const compressed = entry.compress ? deflateRawSync(content) : content;
    const compressionMethod = entry.compress ? 8 : 0;
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 6);
    local.writeUInt16LE(compressionMethod, 8);
    local.writeUInt32LE(0, 14);
    local.writeUInt32LE(compressed.length, 18);
    local.writeUInt32LE(content.length, 22);
    local.writeUInt16LE(name.length, 26);
    local.writeUInt16LE(0, 28);
    localParts.push(local, name, compressed);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0, 8);
    central.writeUInt16LE(compressionMethod, 10);
    central.writeUInt32LE(0, 16);
    central.writeUInt32LE(compressed.length, 20);
    central.writeUInt32LE(content.length, 24);
    central.writeUInt16LE(name.length, 28);
    central.writeUInt16LE(0, 30);
    central.writeUInt16LE(0, 32);
    central.writeUInt32LE(0, 38);
    central.writeUInt32LE(offset, 42);
    centralParts.push(central, name);

    offset += local.length + name.length + compressed.length;
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
    assert.ok(name.length <= 100, "测试用 tar 辅助函数仅支持短名称");
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
