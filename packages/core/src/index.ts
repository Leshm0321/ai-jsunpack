import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { gunzipSync, inflateRawSync } from "node:zlib";
import generate from "@babel/generator";
import { TraceMap, sourceContentFor } from "@jridgewell/trace-mapping";
import type { SourceMapInput } from "@jridgewell/trace-mapping";
import { parse } from "@babel/parser";
import traverseModule, { type NodePath, type TraverseOptions } from "@babel/traverse";
import * as t from "@babel/types";
import type { AstIndex, HeadlessAnalysisResult, InputFileRecord, InputInventory } from "@ai-jsunpack/shared";

export interface AnalyzeInputConfig {
  jobId?: string;
  rootDir?: string;
  inputSourceKind?: NormalizedInputPackage["sourceKind"];
}

export interface CoreAnalysisResult extends HeadlessAnalysisResult {
  sourceMapAnalysis: SourceMapArtifactAnalysis;
  graphAnalysis: GraphAnalysis;
  transformAnalysis: TransformAnalysis;
  moduleRecoveryAnalysis: ModuleRecoveryAnalysis;
  dependencyPlaceholders: DependencyPlaceholderRecord[];
}

export interface NormalizedInputPackage {
  rootDir: string;
  sourcePath: string;
  sourceKind: "directory" | "zip" | "tar" | "tar_gz" | "single_script";
  cleanup: () => Promise<void>;
}

type ArchiveInputKind = Extract<NormalizedInputPackage["sourceKind"], "zip" | "tar" | "tar_gz">;

export interface ReconstructionPlan {
  kind: "reconstruction_plan";
  jobId?: string;
  strategy: "static_host_project";
  entryHtml: string | null;
  sourceFiles: string[];
  scripts: string[];
  styles: string[];
  assets: string[];
  sourceMaps: string[];
  manifests: string[];
  detectedRuntime: string[];
  generatedFiles: string[];
  inputInventory: InputInventory;
  astIndexes: AstIndex[];
  sourceMapAnalysis: SourceMapArtifactAnalysis;
  graphAnalysis: GraphAnalysis;
  moduleRecoveryAnalysis: ModuleRecoveryAnalysis;
  runtimeWrappers: RuntimeWrapperCandidate[];
  moduleBoundaries: ModuleBoundaryCandidate[];
  importExportCandidates: ImportExportCandidate[];
  generatedModules: GeneratedModuleRecord[];
  dependencyPlaceholders: DependencyPlaceholderRecord[];
  scriptTransforms: ScriptTransformRecord[];
  transformLog: TransformLogEntry[];
  rollbackMap: RollbackMapEntry[];
  evidenceSummary: {
    astIndexFiles: string[];
    chunkGraphEdgeCount: number;
    resourceGraphEdgeCount: number;
    moduleCandidateCount: number;
    runtimeWrapperCount: number;
    moduleBoundaryCount: number;
    importExportCandidateCount: number;
    generatedModuleCount: number;
    dependencyPlaceholderCount: number;
    transformCount: number;
    symbolCount: number;
  };
  limitations: string[];
  agentFeedback?: AgentFeedbackResult;
}

export interface WriteProjectConfig {
  inputPath: string;
  outputDir: string;
  packageName?: string;
  agentFeedback?: unknown;
}

export type AgentFeedbackActionName =
  | "add_package_script"
  | "replace_package_script"
  | "mirror_original_static_entry";

export interface AgentFeedbackAction {
  sourceArtifactId: string;
  repairInstructionId?: string;
  action: AgentFeedbackActionName;
  path: string;
  value: string;
  reason: string;
}

export interface AgentFeedbackRejection {
  sourceArtifactId?: string;
  repairInstructionId?: string;
  action?: string;
  path?: string;
  reason: string;
}

export interface AgentFeedbackInput {
  kind: "agent_feedback";
  protocolVersion: 1;
  sourceReviewArtifactIds: string[];
  approvedActions: AgentFeedbackAction[];
  rejectedActions: AgentFeedbackRejection[];
}

export interface AppliedAgentFeedbackAction extends AgentFeedbackAction {
  changed: boolean;
  detail: string;
}

export interface AgentFeedbackResult {
  sourceReviewArtifactIds: string[];
  approvedActions: AgentFeedbackAction[];
  appliedActions: AppliedAgentFeedbackAction[];
  rejectedActions: AgentFeedbackRejection[];
}

export interface GeneratedProjectManifest {
  kind: "generated_project";
  jobId?: string;
  projectPath: string;
  entrypoint: string;
  generatedFiles: string[];
  copiedSourceFiles: string[];
  transformedSourceFiles: string[];
  generatedModuleFiles: string[];
  entrypointFiles: string[];
  typeDefinitionFiles: string[];
  runtimeShimFiles: string[];
  dependencyPlaceholderFiles: string[];
  dependencyPlaceholders: DependencyPlaceholderRecord[];
  analysisFiles: string[];
  sourceRoot: string;
  limitations: string[];
  agentFeedback?: AgentFeedbackResult;
}

export interface WriteProjectResult {
  projectPath: string;
  manifest: GeneratedProjectManifest;
}

interface HtmlSourceRecord {
  path: string;
  content: string;
}

interface HtmlReferenceRecord {
  path: string;
  kind: HtmlReferenceKind;
  htmlPath: string;
  tagName: string;
  attributeName: string;
}

export interface SourceMapBundleAnalysis {
  bundlePath: string;
  sourceMapPath: string;
  sourceMapFile: string | null;
  sourceRoot: string | null;
  sources: string[];
  recoveredSources: SourceMapRecoveredSource[];
  sourceCandidates: string[];
  sourcesContentAvailable: string[];
  missingSourcesContent: string[];
  warnings: string[];
}

export interface SourceMapRecoveredSource {
  source: string;
  candidatePath: string;
  contentHash: string;
  content: string;
}

export interface SourceMapArtifactAnalysis {
  bundleCount: number;
  bundleAnalyses: SourceMapBundleAnalysis[];
  sourceCandidates: string[];
  warnings: string[];
}

export interface GraphNodeRecord {
  id: string;
  kind: string;
  label: string;
}

export interface GraphEdgeRecord {
  from: string;
  to: string;
  kind: string;
}

export interface GraphAnalysis {
  chunkGraph: {
    nodes: GraphNodeRecord[];
    edges: GraphEdgeRecord[];
    entryPoints: string[];
    warnings: string[];
  };
  resourceGraph: {
    nodes: GraphNodeRecord[];
    edges: GraphEdgeRecord[];
    warnings: string[];
  };
  moduleCandidateGraph: {
    nodes: GraphNodeRecord[];
    edges: GraphEdgeRecord[];
    sourceCandidates: string[];
    warnings: string[];
  };
}

export type EvidenceRiskLevel = "low" | "medium" | "high";

export interface SourceRange {
  start: number;
  end: number;
}

export type RuntimeKind =
  | "webpack"
  | "vite_or_rollup"
  | "rollup"
  | "esbuild"
  | "umd"
  | "iife"
  | "browserify"
  | "systemjs"
  | "parcel"
  | "commonjs"
  | "unknown";

export interface RuntimeWrapperCandidate {
  id: string;
  filePath: string;
  runtimeKind: RuntimeKind;
  wrapperKind: string;
  confidence: number;
  riskLevel: EvidenceRiskLevel;
  loc?: string;
  sourceRange?: SourceRange;
  astPath?: string;
  detail: string;
  evidenceRefs: string[];
}

export interface ModuleBoundaryCandidate {
  id: string;
  filePath: string;
  moduleId: string;
  boundaryKind: "source_map_source" | "runtime_module_table" | "static_es_module" | "system_register" | "single_bundle";
  runtimeKind: RuntimeKind;
  confidence: number;
  riskLevel: EvidenceRiskLevel;
  sourcePath?: string;
  loc?: string;
  sourceRange?: SourceRange;
  astPath?: string;
  detail: string;
  evidenceRefs: string[];
}

export interface ImportExportCandidate {
  id: string;
  filePath: string;
  candidateKind:
    | "static_import"
    | "dynamic_import"
    | "re_export"
    | "named_export"
    | "default_export"
    | "commonjs_export"
    | "runtime_dependency"
    | "external_module";
  source?: string;
  importedName?: string;
  exportedName?: string;
  runtimeKind?: RuntimeKind;
  confidence: number;
  riskLevel: EvidenceRiskLevel;
  loc?: string;
  sourceRange?: SourceRange;
  astPath?: string;
  detail: string;
  evidenceRefs: string[];
}

export interface GeneratedModuleRecord {
  modulePath: string;
  sourceKind: "source_map_sources_content" | "script_metadata" | "module_index";
  sourceFilePath?: string;
  sourceMapPath?: string;
  boundaryId?: string;
  sourceHash: string;
  riskLevel: EvidenceRiskLevel;
  evidenceRefs: string[];
}

export interface DependencyPlaceholderRecord {
  importerPath: string;
  specifier: string;
  resolvedPath: string | null;
  importedNames: string[];
  reExportedNames: string[];
  defaultImport: boolean;
  namespaceImport: boolean;
  sideEffectOnly: boolean;
  exportAll: boolean;
  reason: "missing_static_relative_dependency" | "unsafe_relative_dependency" | "dependency_path_conflict";
  status: "generated" | "unsupported";
  limitation: string;
}

export interface ModuleRecoveryAnalysis {
  runtimeWrappers: RuntimeWrapperCandidate[];
  moduleBoundaries: ModuleBoundaryCandidate[];
  importExportCandidates: ImportExportCandidate[];
  generatedModules: GeneratedModuleRecord[];
  warnings: string[];
}

export interface ScriptTransformRecord {
  filePath: string;
  originalHash: string;
  transformedHash: string;
  transformedSource: string;
  transforms: string[];
  wrapperMarks: string[];
}

export interface TransformLogEntry {
  filePath: string;
  kind: string;
  status: "applied" | "skipped";
  originalLoc?: string;
  sourceRange?: SourceRange;
  astPath?: string;
  riskLevel?: EvidenceRiskLevel;
  reversible?: boolean;
  evidenceRefs?: string[];
  originalSnippet?: string;
  transformedSnippet?: string;
  detail?: string;
}

export interface RollbackMapEntry {
  filePath: string;
  kind: string;
  originalLoc?: string;
  transformedLoc?: string;
  sourceRange?: SourceRange;
  astPath?: string;
  riskLevel?: EvidenceRiskLevel;
  reversible?: boolean;
  evidenceRefs?: string[];
  originalSnippet?: string;
  transformedSnippet?: string;
}

export interface TransformAnalysis {
  scriptTransforms: ScriptTransformRecord[];
  transformLog: TransformLogEntry[];
  rollbackMap: RollbackMapEntry[];
}

type HtmlReferenceKind = "asset" | "manifest" | "script" | "style";

const TEXT_EXTENSIONS = new Set([".html", ".htm", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css", ".json", ".map"]);
const HTML_RAW_TEXT_TAGS = new Set(["script", "style", "textarea", "title"]);
const HTML_REFERENCE_PRIORITY: Record<HtmlReferenceKind, number> = {
  manifest: 4,
  style: 3,
  script: 2,
  asset: 1
};
const GENERATED_PROJECT_FILES = [
  "package.json",
  "tsconfig.json",
  "index.html",
  "src/main.ts",
  "src/reconstruction-manifest.json",
  "src/analysis/input-inventory.json",
  "src/analysis/ast-indexes.json",
  "src/analysis/source-map-analysis.json",
  "src/analysis/graph-analysis.json",
  "src/analysis/runtime-wrappers.json",
  "src/analysis/module-boundaries.json",
  "src/analysis/import-export-candidates.json",
  "src/analysis/generated-modules.json",
  "src/analysis/dependency-placeholders.json",
  "src/analysis/transform-log.json",
  "src/analysis/rollback-map.json",
  "src/entrypoints/reconstruction-entry.ts",
  "src/modules/module-index.ts",
  "src/runtime-shims/browser-globals.ts",
  "src/types/reconstruction.d.ts",
  "scripts/build.mjs",
  "scripts/typecheck.mjs"
];
const LOCAL_SOURCE_SCHEMES = new Set(["webpack", "rollup", "vite", "parcel", "browserify", "ng", "esbuild", "turbopack", "snowpack"]);
const SUPPORTED_RUNTIME_MARKERS: Array<{ runtimeKind: RuntimeKind; markers: string[] }> = [
  { runtimeKind: "webpack", markers: ["__webpack_require__", "webpackJsonp", "webpackChunk"] },
  { runtimeKind: "vite_or_rollup", markers: ["__vitePreload", "import.meta", "modulepreload"] },
  { runtimeKind: "rollup", markers: ["Object.freeze", "__esModule"] },
  { runtimeKind: "esbuild", markers: ["__defProp", "__export", "__toESM", "__commonJS"] },
  { runtimeKind: "umd", markers: ["typeof exports", "define.amd", "factory"] },
  { runtimeKind: "browserify", markers: ["function r(e,n,t)", "function o(i,f)", "browserify"] },
  { runtimeKind: "systemjs", markers: ["System.register", "System.import"] },
  { runtimeKind: "parcel", markers: ["parcelRequire", "newRequire"] }
];

const ARCHIVE_MAX_ENTRIES = 10_000;
const ARCHIVE_MAX_FILE_BYTES = 64 * 1024 * 1024;
const ARCHIVE_MAX_TOTAL_BYTES = 256 * 1024 * 1024;
const ARCHIVE_MAX_COMPRESSION_RATIO = 200;

export async function analyzeInputPackage(inputPath: string, config: AnalyzeInputConfig = {}): Promise<CoreAnalysisResult> {
  const normalized = config.rootDir ? undefined : await normalizeInputPackage(inputPath);
  try {
    const rootDir = path.resolve(config.rootDir ?? normalized?.rootDir ?? inputPath);
    const sourceKind = config.inputSourceKind ?? normalized?.sourceKind;
    const inventory = await buildInputInventory(rootDir);
    if (sourceKind === "single_script") {
      inventory.isSingleBundle = inventory.scripts.length === 1;
      inventory.warnings.unshift("输入的 single_script 文件已封装到经过验证的临时静态宿主页中。");
    } else if (sourceKind && sourceKind !== "directory") {
      inventory.warnings.unshift(`输入的 ${sourceKind} 归档已解压到经过验证的临时 workspace。`);
    }
    const sourceMapAnalysis = await analyzeSourceMaps(rootDir, inventory);
    if (sourceMapAnalysis.warnings.length > 0) {
      inventory.warnings.push(...sourceMapAnalysis.warnings);
    }
    const astIndexes = await Promise.all(
      inventory.scripts.map(async (scriptPath) => buildAstIndexForFile(path.join(rootDir, scriptPath), rootDir))
    );
    const detectedRuntime = detectBundleRuntime(inventory, astIndexes);
    const graphAnalysis = buildGraphAnalysis(inventory, astIndexes, sourceMapAnalysis);
    const transformAnalysis = await buildTransformAnalysis(rootDir, astIndexes);
    const moduleRecoveryAnalysis = await buildModuleRecoveryAnalysis(rootDir, inventory, astIndexes, sourceMapAnalysis, detectedRuntime);
    const dependencyPlaceholders = await analyzeDependencyPlaceholders(rootDir, inventory);

    return {
      inventory,
      astIndexes,
      detectedRuntime,
      sourceMapAnalysis,
      graphAnalysis,
      transformAnalysis,
      moduleRecoveryAnalysis,
      dependencyPlaceholders,
      artifacts: []
    };
  } finally {
    await normalized?.cleanup();
  }
}

export async function normalizeInputPackage(inputPath: string): Promise<NormalizedInputPackage> {
  const sourcePath = path.resolve(inputPath);
  const stat = await fs.stat(sourcePath);

  if (stat.isDirectory()) {
    return {
      rootDir: sourcePath,
      sourcePath,
      sourceKind: "directory",
      cleanup: async () => {}
    };
  }

  if (!stat.isFile()) {
    throw new Error(`输入路径必须是目录、受支持的归档或 JavaScript 文件：${inputPath}`);
  }

  const sourceKind = archiveKindForPath(sourcePath);
  if (!sourceKind) {
    if (isSupportedSingleScriptPath(sourcePath)) {
      return normalizeSingleScriptInput(sourcePath);
    }
    throw new Error(`不支持的输入文件类型。预期为目录、.js、.mjs、.cjs、.zip、.tar、.tar.gz 或 .tgz：${inputPath}`);
  }

  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-input-"));
  let cleaned = false;
  const cleanup = async () => {
    if (cleaned) {
      return;
    }
    cleaned = true;
    await fs.rm(tempRoot, { recursive: true, force: true });
  };

  try {
    const archive = await fs.readFile(sourcePath);
    if (sourceKind === "zip") {
      await extractZipArchive(archive, tempRoot);
    } else {
      const tarBuffer = sourceKind === "tar_gz"
        ? gunzipSync(archive, { maxOutputLength: ARCHIVE_MAX_TOTAL_BYTES })
        : archive;
      await extractTarArchive(tarBuffer, tempRoot);
    }
  } catch (error) {
    await cleanup();
    throw error;
  }

  return {
    rootDir: tempRoot,
    sourcePath,
    sourceKind,
    cleanup
  };
}

async function normalizeSingleScriptInput(sourcePath: string): Promise<NormalizedInputPackage> {
  const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-input-"));
  let cleaned = false;
  const cleanup = async () => {
    if (cleaned) {
      return;
    }
    cleaned = true;
    await fs.rm(tempRoot, { recursive: true, force: true });
  };

  const scriptName = safeSingleScriptFilename(sourcePath);
  try {
    const scriptSource = await fs.readFile(sourcePath, "utf8");
    await fs.copyFile(sourcePath, path.join(tempRoot, scriptName));
    await fs.writeFile(path.join(tempRoot, "index.html"), singleScriptHostHtml(scriptName, scriptSource), "utf8");
  } catch (error) {
    await cleanup();
    throw error;
  }

  return {
    rootDir: tempRoot,
    sourcePath,
    sourceKind: "single_script",
    cleanup
  };
}

export async function buildInputInventory(rootDir: string): Promise<InputInventory> {
  const absoluteRoot = path.resolve(rootDir);
  const filePaths = await listFiles(absoluteRoot);
  const files: InputFileRecord[] = [];
  const htmlSources: HtmlSourceRecord[] = [];

  for (const absolutePath of filePaths) {
    const relativePath = toPosix(path.relative(absoluteRoot, absolutePath));
    const buffer = await fs.readFile(absolutePath);
    const kind = classifyPath(relativePath);
    files.push({
      path: relativePath,
      kind,
      size: buffer.byteLength,
      hash: sha256(buffer)
    });
    if (kind === "html") {
      htmlSources.push({
        path: relativePath,
        content: buffer.toString("utf8")
      });
    }
  }

  const { referenceKindsByPath, missingReferences } = collectHtmlReferenceKinds(htmlSources, new Set(files.map((file) => file.path)));
  const normalizedFiles = files.map((file) => normalizeInventoryFileKind(file, referenceKindsByPath.get(file.path)));
  const entries = normalizedFiles.filter((file) => file.kind === "html").map((file) => file.path);
  const scripts = normalizedFiles.filter((file) => file.kind === "script").map((file) => file.path);
  const styles = normalizedFiles.filter((file) => file.kind === "style").map((file) => file.path);
  const assets = normalizedFiles.filter((file) => file.kind === "asset").map((file) => file.path);
  const sourceMaps = normalizedFiles.filter((file) => file.kind === "source_map").map((file) => file.path);
  const manifests = normalizedFiles.filter((file) => file.kind === "manifest").map((file) => file.path);
  const warnings: string[] = [];

  if (entries.length === 0) {
    warnings.push("未找到 HTML 入口；浏览器验证需要一个最小宿主页。");
  }
  if (scripts.length === 0) {
    warnings.push("未找到 JavaScript bundle。");
  }
  if (missingReferences.length > 0) {
    warnings.push(formatMissingHtmlReferenceWarning(missingReferences));
  }

  return {
    files: normalizedFiles,
    entries,
    scripts,
    styles,
    assets,
    sourceMaps,
    manifests,
    isSingleBundle: entries.length === 0 && scripts.length === 1,
    warnings
  };
}

export async function buildAstIndexForFile(filePath: string, rootDir = path.dirname(filePath)): Promise<AstIndex> {
  const source = await fs.readFile(filePath, "utf8");
  const relativePath = toPosix(path.relative(path.resolve(rootDir), path.resolve(filePath)));
  const warnings: string[] = [];
  const symbols = new Map<string, { kind: string; loc?: string; references: number }>();
  const imports = new Set<string>();
  const exports = new Set<string>();

  try {
    const ast = parse(source, {
      sourceType: "unambiguous",
      plugins: ["jsx", "typescript", "dynamicImport", "classProperties", "optionalChaining", "nullishCoalescingOperator"],
      errorRecovery: true,
      tokens: true
    });
    indexProgramDeclarations(ast.program.body, symbols, imports, exports);

    const traverseAst = getTraverse();

    traverseAst(ast, {
      ImportDeclaration(path: NodePath<t.ImportDeclaration>) {
        imports.add(path.node.source.value);
      },
      ExportNamedDeclaration(path: NodePath<t.ExportNamedDeclaration>) {
        if (path.node.source?.value) {
          exports.add(path.node.source.value);
        }
        for (const specifier of path.node.specifiers) {
          exports.add(specifier.exported.type === "Identifier" ? specifier.exported.name : specifier.exported.value);
        }
      },
      ExportDefaultDeclaration() {
        exports.add("default");
      },
      FunctionDeclaration(path: NodePath<t.FunctionDeclaration>) {
        if (path.node.id?.name) {
          upsertSymbol(symbols, path.node.id.name, "function", formatLoc(path));
        }
      },
      ClassDeclaration(path: NodePath<t.ClassDeclaration>) {
        if (path.node.id?.name) {
          upsertSymbol(symbols, path.node.id.name, "class", formatLoc(path));
        }
      },
      VariableDeclarator(path: NodePath<t.VariableDeclarator>) {
        if (path.node.id.type === "Identifier") {
          upsertSymbol(symbols, path.node.id.name, "variable", formatLoc(path));
        }
      },
      Identifier(path: NodePath<t.Identifier>) {
        const existing = symbols.get(path.node.name);
        if (existing) {
          existing.references += 1;
        }
      }
    });
  } catch (error) {
    warnings.push(error instanceof Error ? error.message : "未知的 Babel 解析错误。");
  }

  return {
    filePath: relativePath,
    sourceHash: sha256(Buffer.from(source)),
    symbols: [...symbols.entries()].map(([name, value]) => ({ name, ...value })),
    imports: [...imports],
    exports: [...exports],
    warnings
  };
}

interface StaticDependencyUsage {
  importerPath: string;
  specifier: string;
  importedNames: Set<string>;
  reExportedNames: Set<string>;
  defaultImport: boolean;
  namespaceImport: boolean;
  sideEffectOnly: boolean;
  exportAll: boolean;
}

async function analyzeDependencyPlaceholders(
  rootDir: string,
  inventory: InputInventory
): Promise<DependencyPlaceholderRecord[]> {
  const absoluteRoot = path.resolve(rootDir);
  const usages = new Map<string, StaticDependencyUsage>();

  for (const importerPath of inventory.scripts) {
    const source = await fs.readFile(path.join(absoluteRoot, safeRelativePath(importerPath)), "utf8");
    let ast: ReturnType<typeof parse>;
    try {
      ast = parse(source, {
        sourceType: "unambiguous",
        plugins: ["jsx", "typescript", "dynamicImport", "classProperties", "optionalChaining", "nullishCoalescingOperator"],
        errorRecovery: true
      });
    } catch {
      continue;
    }

    for (const statement of ast.program.body) {
      if (t.isImportDeclaration(statement)) {
        const usage = dependencyUsage(usages, importerPath, statement.source.value);
        usage.sideEffectOnly ||= statement.specifiers.length === 0;
        for (const specifier of statement.specifiers) {
          if (t.isImportDefaultSpecifier(specifier)) {
            usage.defaultImport = true;
          } else if (t.isImportNamespaceSpecifier(specifier)) {
            usage.namespaceImport = true;
          } else {
            usage.importedNames.add(importSpecifierName(specifier));
          }
        }
        continue;
      }
      if (t.isExportAllDeclaration(statement)) {
        const usage = dependencyUsage(usages, importerPath, statement.source.value);
        usage.exportAll = true;
        continue;
      }
      if (!t.isExportNamedDeclaration(statement) || !statement.source) {
        continue;
      }
      const usage = dependencyUsage(usages, importerPath, statement.source.value);
      for (const specifier of statement.specifiers) {
        if (t.isExportNamespaceSpecifier(specifier)) {
          usage.namespaceImport = true;
          usage.reExportedNames.add(exportName(specifier.exported));
          continue;
        }
        if (!t.isExportSpecifier(specifier)) {
          continue;
        }
        const importedName = exportName(specifier.local);
        const exportedName = exportName(specifier.exported);
        if (importedName === "default") {
          usage.defaultImport = true;
        } else {
          usage.importedNames.add(importedName);
        }
        usage.reExportedNames.add(exportedName);
      }
    }
  }

  const records: DependencyPlaceholderRecord[] = [];
  for (const usage of usages.values()) {
    if (!usage.specifier.startsWith("./") && !usage.specifier.startsWith("../")) {
      continue;
    }
    const specifierPath = usage.specifier.split(/[?#]/, 1)[0];
    const importerDirectory = path.posix.dirname(toPosix(usage.importerPath));
    const resolvedPath = path.posix.normalize(path.posix.join(importerDirectory, specifierPath));
    const baseRecord = dependencyPlaceholderRecordBase(usage);
    if (
      !specifierPath ||
      specifierPath.includes("\\") ||
      path.posix.isAbsolute(resolvedPath) ||
      resolvedPath === ".." ||
      resolvedPath.startsWith("../")
    ) {
      records.push({
        ...baseRecord,
        resolvedPath: null,
        reason: "unsafe_relative_dependency",
        status: "unsupported",
        limitation: "依赖解析到了规范化输入根目录之外，或使用了不安全路径，因此未写入。"
      });
      continue;
    }

    let safeResolvedPath: string;
    try {
      safeResolvedPath = toPosix(safeArchiveRelativePath(resolvedPath));
    } catch {
      records.push({
        ...baseRecord,
        resolvedPath: null,
        reason: "unsafe_relative_dependency",
        status: "unsupported",
        limitation: "无法安全规范化依赖路径，因此未写入。"
      });
      continue;
    }

    const targetPath = path.join(absoluteRoot, safeResolvedPath);
    let targetStat: Awaited<ReturnType<typeof fs.lstat>> | null = null;
    try {
      targetStat = await fs.lstat(targetPath);
    } catch (error) {
      if (!(error instanceof Error) || !("code" in error) || error.code !== "ENOENT") {
        throw error;
      }
    }
    if (targetStat?.isFile()) {
      continue;
    }
    if (targetStat) {
      records.push({
        ...baseRecord,
        resolvedPath: safeResolvedPath,
        reason: "dependency_path_conflict",
        status: "unsupported",
        limitation: "依赖路径存在但不是常规文件，因此未写入占位模块。"
      });
      continue;
    }
    records.push({
      ...baseRecord,
      resolvedPath: safeResolvedPath,
      reason: "missing_static_relative_dependency",
      status: "generated",
      limitation: "仅提供加载连续性；依赖的语义行为不可用，生成的导出在调用时会抛出异常。"
    });
  }

  return records.sort((left, right) =>
    `${left.resolvedPath ?? ""}\0${left.importerPath}\0${left.specifier}`.localeCompare(
      `${right.resolvedPath ?? ""}\0${right.importerPath}\0${right.specifier}`
    )
  );
}

function dependencyUsage(
  usages: Map<string, StaticDependencyUsage>,
  importerPath: string,
  specifier: string
): StaticDependencyUsage {
  const key = `${importerPath}\0${specifier}`;
  const existing = usages.get(key);
  if (existing) {
    return existing;
  }
  const created: StaticDependencyUsage = {
    importerPath: toPosix(importerPath),
    specifier,
    importedNames: new Set<string>(),
    reExportedNames: new Set<string>(),
    defaultImport: false,
    namespaceImport: false,
    sideEffectOnly: false,
    exportAll: false
  };
  usages.set(key, created);
  return created;
}

function dependencyPlaceholderRecordBase(
  usage: StaticDependencyUsage
): Omit<DependencyPlaceholderRecord, "resolvedPath" | "reason" | "status" | "limitation"> {
  return {
    importerPath: usage.importerPath,
    specifier: usage.specifier,
    importedNames: [...usage.importedNames].sort(),
    reExportedNames: [...usage.reExportedNames].sort(),
    defaultImport: usage.defaultImport,
    namespaceImport: usage.namespaceImport,
    sideEffectOnly: usage.sideEffectOnly,
    exportAll: usage.exportAll
  };
}

function exportName(node: t.Identifier | t.StringLiteral): string {
  return t.isIdentifier(node) ? node.name : node.value;
}

function indexProgramDeclarations(
  body: t.Statement[],
  symbols: Map<string, { kind: string; loc?: string; references: number }>,
  imports: Set<string>,
  exports: Set<string>
): void {
  for (const statement of body) {
    if (statement.type === "FunctionDeclaration" && statement.id?.name) {
      upsertSymbol(symbols, statement.id.name, "function", formatNodeLoc(statement));
    }
    if (statement.type === "ClassDeclaration" && statement.id?.name) {
      upsertSymbol(symbols, statement.id.name, "class", formatNodeLoc(statement));
    }
    if (statement.type === "VariableDeclaration") {
      for (const declaration of statement.declarations) {
        if (declaration.id.type === "Identifier") {
          upsertSymbol(symbols, declaration.id.name, "variable", formatNodeLoc(declaration));
        }
      }
    }
    if (statement.type === "ImportDeclaration") {
      imports.add(statement.source.value);
    }
    if (statement.type === "ExportNamedDeclaration") {
      if (statement.source?.value) {
        exports.add(statement.source.value);
      }
      for (const specifier of statement.specifiers) {
        exports.add(specifier.exported.type === "Identifier" ? specifier.exported.name : specifier.exported.value);
      }
      if (statement.declaration) {
        indexProgramDeclarations([statement.declaration], symbols, imports, exports);
      }
    }
    if (statement.type === "ExportDefaultDeclaration") {
      exports.add("default");
      const declaration = statement.declaration;
      if ((declaration.type === "FunctionDeclaration" || declaration.type === "ClassDeclaration") && declaration.id?.name) {
        upsertSymbol(
          symbols,
          declaration.id.name,
          declaration.type === "FunctionDeclaration" ? "function" : "class",
          formatNodeLoc(declaration)
        );
      }
    }
  }
}

export function detectBundleRuntime(inventory: InputInventory, indexes: AstIndex[]): string[] {
  const detected = new Set<string>();
  const names = inventory.files.map((file) => file.path.toLowerCase()).join("\n");
  const symbolNames = indexes.flatMap((index) => index.symbols.map((symbol) => symbol.name)).join("\n");

  if (names.includes("manifest") || symbolNames.includes("__webpack_require__")) {
    detected.add("webpack");
  }
  if (names.includes("vite") || names.includes("assets/")) {
    detected.add("vite_or_rollup");
  }
  if (inventory.entries.length > 0 && inventory.scripts.length > 1) {
    detected.add("multi_chunk");
  }
  if (inventory.isSingleBundle) {
    detected.add("single_bundle_best_effort");
  }

  return [...detected];
}

export function buildGraphAnalysis(
  inventory: InputInventory,
  astIndexes: AstIndex[],
  sourceMapAnalysis: SourceMapArtifactAnalysis
): GraphAnalysis {
  const fileKindByPath = new Map(inventory.files.map((file) => [file.path, file.kind]));
  const chunkNodes = new Map<string, GraphNodeRecord>();
  const chunkEdges = new Map<string, GraphEdgeRecord>();
  const resourceNodes = new Map<string, GraphNodeRecord>();
  const resourceEdges = new Map<string, GraphEdgeRecord>();
  const moduleNodes = new Map<string, GraphNodeRecord>();
  const moduleEdges = new Map<string, GraphEdgeRecord>();
  const warnings: string[] = [];

  for (const entry of inventory.entries) {
    addGraphNode(chunkNodes, `entry:${entry}`, "entry", entry);
    addGraphNode(resourceNodes, `entry:${entry}`, "entry", entry);
  }

  for (const script of inventory.scripts) {
    addGraphNode(chunkNodes, `script:${script}`, "script", script);
    addGraphNode(moduleNodes, `script:${script}`, "script", script);
    for (const entry of inventory.entries) {
      addGraphEdge(chunkEdges, `entry:${entry}`, `script:${script}`, "entry_includes_script");
    }
  }

  for (const style of inventory.styles) {
    addGraphNode(resourceNodes, `style:${style}`, "style", style);
    for (const entry of inventory.entries) {
      addGraphEdge(resourceEdges, `entry:${entry}`, `style:${style}`, "entry_includes_style");
    }
  }

  for (const asset of inventory.assets) {
    addGraphNode(resourceNodes, `asset:${asset}`, "asset", asset);
    for (const entry of inventory.entries) {
      addGraphEdge(resourceEdges, `entry:${entry}`, `asset:${asset}`, "entry_references_asset");
    }
  }

  for (const sourceMap of sourceMapAnalysis.bundleAnalyses) {
    addGraphNode(moduleNodes, `script:${sourceMap.bundlePath}`, "script", sourceMap.bundlePath);
    addGraphNode(moduleNodes, `source_map:${sourceMap.sourceMapPath}`, "source_map", sourceMap.sourceMapPath);
    addGraphEdge(moduleEdges, `script:${sourceMap.bundlePath}`, `source_map:${sourceMap.sourceMapPath}`, "has_source_map");
    for (const candidate of sourceMap.sourceCandidates) {
      addGraphNode(moduleNodes, `source:${candidate}`, "source_candidate", candidate);
      addGraphEdge(moduleEdges, `source_map:${sourceMap.sourceMapPath}`, `source:${candidate}`, "maps_to_source_candidate");
    }
  }

  for (const astIndex of astIndexes) {
    addGraphNode(moduleNodes, `script:${astIndex.filePath}`, "script", astIndex.filePath);
    for (const importSource of astIndex.imports) {
      const resolved = resolveScriptImportCandidate(astIndex.filePath, importSource, fileKindByPath);
      const targetId = resolved ? `script:${resolved}` : `external:${importSource}`;
      addGraphNode(moduleNodes, targetId, resolved ? "script" : "external_module", resolved ?? importSource);
      addGraphEdge(moduleEdges, `script:${astIndex.filePath}`, targetId, "static_import");
      if (resolved) {
        addGraphEdge(chunkEdges, `script:${astIndex.filePath}`, `script:${resolved}`, "static_import");
      }
    }
    for (const symbol of astIndex.symbols) {
      const symbolId = `symbol:${astIndex.filePath}:${symbol.name}`;
      addGraphNode(moduleNodes, symbolId, symbol.kind, symbol.name);
      addGraphEdge(moduleEdges, `script:${astIndex.filePath}`, symbolId, "declares_symbol");
    }
  }

  if (inventory.entries.length === 0 && inventory.scripts.length > 0) {
    warnings.push("没有可用的 HTML 入口；chunk graph 将脚本作为候选根节点。");
  }
  if (sourceMapAnalysis.bundleCount === 0) {
    warnings.push("没有可用的 source map；模块候选仅限导入和 AST 符号。");
  }

  return {
    chunkGraph: {
      nodes: [...chunkNodes.values()].sort(compareGraphNode),
      edges: [...chunkEdges.values()].sort(compareGraphEdge),
      entryPoints: inventory.entries.length > 0 ? inventory.entries : inventory.scripts,
      warnings
    },
    resourceGraph: {
      nodes: [...resourceNodes.values()].sort(compareGraphNode),
      edges: [...resourceEdges.values()].sort(compareGraphEdge),
      warnings: inventory.assets.length === 0 && inventory.styles.length === 0 ? ["未找到引用的样式或资源。"] : []
    },
    moduleCandidateGraph: {
      nodes: [...moduleNodes.values()].sort(compareGraphNode),
      edges: [...moduleEdges.values()].sort(compareGraphEdge),
      sourceCandidates: sourceMapAnalysis.sourceCandidates,
      warnings: sourceMapAnalysis.warnings
    }
  };
}

async function buildModuleRecoveryAnalysis(
  rootDir: string,
  inventory: InputInventory,
  astIndexes: AstIndex[],
  sourceMapAnalysis: SourceMapArtifactAnalysis,
  detectedRuntime: string[]
): Promise<ModuleRecoveryAnalysis> {
  const runtimeWrappers: RuntimeWrapperCandidate[] = [];
  const moduleBoundaries: ModuleBoundaryCandidate[] = [];
  const importExportCandidates: ImportExportCandidate[] = [];
  const generatedModules: GeneratedModuleRecord[] = [];
  const warnings: string[] = [];
  const seenGeneratedModulePaths = new Set<string>();

  for (const bundleAnalysis of sourceMapAnalysis.bundleAnalyses) {
    for (const recoveredSource of bundleAnalysis.recoveredSources) {
      const modulePath = uniqueGeneratedModulePath(
        seenGeneratedModulePaths,
        path.posix.join("src/modules/recovered", withTsExtension(recoveredSource.candidatePath))
      );
      const boundaryId = candidateId("source-map-boundary", bundleAnalysis.bundlePath, bundleAnalysis.sourceMapPath, recoveredSource.candidatePath);
      moduleBoundaries.push({
        id: boundaryId,
        filePath: bundleAnalysis.bundlePath,
        moduleId: recoveredSource.candidatePath,
        boundaryKind: "source_map_source",
        runtimeKind: runtimeKindFromDetected(detectedRuntime),
        confidence: 0.95,
        riskLevel: "low",
        sourcePath: recoveredSource.candidatePath,
        detail: `已从 ${bundleAnalysis.sourceMapPath} 的 sourcesContent 中恢复模块候选。`,
        evidenceRefs: [`source_map:${bundleAnalysis.sourceMapPath}`, `script:${bundleAnalysis.bundlePath}`]
      });
      generatedModules.push({
        modulePath,
        sourceKind: "source_map_sources_content",
        sourceFilePath: recoveredSource.candidatePath,
        sourceMapPath: bundleAnalysis.sourceMapPath,
        boundaryId,
        sourceHash: recoveredSource.contentHash,
        riskLevel: "low",
        evidenceRefs: [`source_map:${bundleAnalysis.sourceMapPath}`, `script:${bundleAnalysis.bundlePath}`]
      });
    }
  }

  for (const astIndex of astIndexes) {
    const scriptPath = path.join(rootDir, astIndex.filePath);
    let source: string;
    try {
      source = await fs.readFile(scriptPath, "utf8");
    } catch (error) {
      warnings.push(`为恢复模块读取脚本 ${astIndex.filePath} 失败：${error instanceof Error ? error.message : "未知错误"}`);
      continue;
    }

    let ast: ReturnType<typeof parse>;
    try {
      ast = parse(source, {
        sourceType: "unambiguous",
        plugins: ["jsx", "typescript", "dynamicImport", "classProperties", "optionalChaining", "nullishCoalescingOperator"],
        errorRecovery: true,
        tokens: true
      });
    } catch (error) {
      warnings.push(`为恢复模块解析脚本 ${astIndex.filePath} 失败：${error instanceof Error ? error.message : "未知的 Babel 解析错误"}`);
      continue;
    }

    const runtimeKinds = detectRuntimeKindsForSource(source, astIndex, detectedRuntime);
    runtimeWrappers.push(...collectRuntimeWrapperCandidates(astIndex.filePath, source, ast, runtimeKinds));
    moduleBoundaries.push(...collectRuntimeModuleBoundaryCandidates(astIndex.filePath, source, ast, runtimeKinds, inventory.isSingleBundle));
    importExportCandidates.push(...collectImportExportCandidates(astIndex.filePath, ast, runtimeKinds));

    const scriptModulePath = uniqueGeneratedModulePath(
      seenGeneratedModulePaths,
      path.posix.join("src/modules/scripts", `${sanitizeGeneratedModulePath(astIndex.filePath)}.module.ts`)
    );
    generatedModules.push({
      modulePath: scriptModulePath,
      sourceKind: "script_metadata",
      sourceFilePath: astIndex.filePath,
      sourceHash: astIndex.sourceHash,
      riskLevel: runtimeKinds.has("unknown") ? "medium" : "low",
      evidenceRefs: [`script:${astIndex.filePath}`, `ast_index:${astIndex.filePath}`]
    });
  }

  if (runtimeWrappers.length === 0 && astIndexes.length > 0) {
    warnings.push("未检测到明确的 bundle runtime wrapper；恢复范围仅限 source map、ESM 语法和脚本元数据。");
  }
  if (moduleBoundaries.length === 0 && astIndexes.length > 0) {
    warnings.push("未检测到模块边界候选；生成模块仅包含元数据。");
  }

  return {
    runtimeWrappers: sortRuntimeWrappers(runtimeWrappers),
    moduleBoundaries: sortModuleBoundaries(moduleBoundaries),
    importExportCandidates: sortImportExportCandidates(importExportCandidates),
    generatedModules: generatedModules.sort((left, right) => left.modulePath.localeCompare(right.modulePath)),
    warnings
  };
}

function detectRuntimeKindsForSource(source: string, astIndex: AstIndex, detectedRuntime: string[]): Set<RuntimeKind> {
  const runtimeKinds = new Set<RuntimeKind>();
  for (const markerGroup of SUPPORTED_RUNTIME_MARKERS) {
    if (markerGroup.markers.some((marker) => source.includes(marker))) {
      runtimeKinds.add(markerGroup.runtimeKind);
    }
  }
  if (astIndex.imports.length > 0 || astIndex.exports.length > 0) {
    runtimeKinds.add("vite_or_rollup");
  }
  if (/\bmodule\.exports\b|\bexports\.[A-Za-z_$]/.test(source)) {
    runtimeKinds.add("commonjs");
  }
  if (detectedRuntime.includes("single_bundle_best_effort") && runtimeKinds.size === 0) {
    runtimeKinds.add("iife");
  }
  if (runtimeKinds.size === 0) {
    runtimeKinds.add("unknown");
  }
  return runtimeKinds;
}

function collectRuntimeWrapperCandidates(
  filePath: string,
  source: string,
  ast: ReturnType<typeof parse>,
  runtimeKinds: Set<RuntimeKind>
): RuntimeWrapperCandidate[] {
  const candidates: RuntimeWrapperCandidate[] = [];
  const programRuntimeKind = preferredRuntimeKind(runtimeKinds);
  const firstStatement = ast.program.body[0];
  if (firstStatement?.type === "ExpressionStatement" && firstStatement.expression.type === "CallExpression") {
    candidates.push({
      id: candidateId("runtime-wrapper", filePath, "program-call", firstStatement.start ?? 0),
      filePath,
      runtimeKind: programRuntimeKind,
      wrapperKind: isIifeCallExpression(firstStatement.expression) ? "iife_bootstrap" : "call_expression_bootstrap",
      confidence: programRuntimeKind === "unknown" ? 0.55 : 0.8,
      riskLevel: programRuntimeKind === "unknown" ? "medium" : "low",
      loc: formatNodeLoc(firstStatement),
      sourceRange: formatSourceRange(firstStatement),
      astPath: "Program.body[0]",
      detail: "顶层调用表达式标记了 bundle bootstrap 或 IIFE wrapper 候选。",
      evidenceRefs: [`script:${filePath}`]
    });
  }
  if (ast.program.body.length === 1 && ast.program.body[0]?.type === "FunctionDeclaration") {
    candidates.push({
      id: candidateId("runtime-wrapper", filePath, "single-function", ast.program.body[0].start ?? 0),
      filePath,
      runtimeKind: programRuntimeKind,
      wrapperKind: "single_function_wrapper",
      confidence: 0.65,
      riskLevel: "medium",
      loc: formatNodeLoc(ast.program.body[0]),
      sourceRange: formatSourceRange(ast.program.body[0]),
      astPath: "Program.body[0]",
      detail: "单个顶层函数标记了 deferred wrapper 候选。",
      evidenceRefs: [`script:${filePath}`]
    });
  }
  if (source.includes("typeof exports") && source.includes("define.amd")) {
    candidates.push(markerRuntimeWrapper(filePath, "umd", "umd_factory_wrapper", source, "CommonJS/AMD/全局分支表明存在 UMD factory wrapper。"));
  }
  if (source.includes("__webpack_require__")) {
    candidates.push(markerRuntimeWrapper(filePath, "webpack", "webpack_bootstrap", source, "Webpack runtime 标记 __webpack_require__ 表明存在 bootstrap wrapper。"));
  }
  if (source.includes("System.register")) {
    candidates.push(markerRuntimeWrapper(filePath, "systemjs", "system_register_wrapper", source, "System.register 调用表明存在 SystemJS module wrapper。"));
  }
  return dedupeById(candidates);
}

function collectRuntimeModuleBoundaryCandidates(
  filePath: string,
  source: string,
  ast: ReturnType<typeof parse>,
  runtimeKinds: Set<RuntimeKind>,
  isSingleBundle: boolean
): ModuleBoundaryCandidate[] {
  const candidates: ModuleBoundaryCandidate[] = [];
  const traverseAst = getTraverse();
  const primaryRuntime = preferredRuntimeKind(runtimeKinds);

  traverseAst(ast, {
    ObjectExpression(path: NodePath<t.ObjectExpression>) {
      if (!runtimeKinds.has("webpack") && !runtimeKinds.has("browserify") && !runtimeKinds.has("parcel")) {
        return;
      }
      const moduleProperties = path.node.properties.filter((property) => {
        if (!t.isObjectProperty(property)) {
          return false;
        }
        return t.isFunctionExpression(property.value) || t.isArrowFunctionExpression(property.value);
      });
      if (moduleProperties.length < 2) {
        return;
      }
      for (const property of moduleProperties) {
        if (!t.isObjectProperty(property)) {
          continue;
        }
        const moduleId = propertyKeyName(property.key);
        if (!moduleId) {
          continue;
        }
        candidates.push({
          id: candidateId("runtime-module-boundary", filePath, moduleId, property.start ?? 0),
          filePath,
          moduleId,
          boundaryKind: "runtime_module_table",
          runtimeKind: primaryRuntime,
          confidence: 0.82,
          riskLevel: "medium",
          loc: formatNodeLoc(property),
          sourceRange: formatSourceRange(property),
          astPath: formatAstPath(path),
          detail: `函数值模块表属性 ${moduleId} 标记了 bundle 模块边界候选。`,
          evidenceRefs: [`script:${filePath}`, `runtime:${primaryRuntime}`]
        });
      }
    },
    ArrayExpression(path: NodePath<t.ArrayExpression>) {
      if (!runtimeKinds.has("webpack") && !runtimeKinds.has("browserify")) {
        return;
      }
      const moduleElements = path.node.elements.filter((element) => t.isFunctionExpression(element) || t.isArrowFunctionExpression(element));
      if (moduleElements.length < 2) {
        return;
      }
      path.node.elements.forEach((element, index) => {
        if (!t.isFunctionExpression(element) && !t.isArrowFunctionExpression(element)) {
          return;
        }
        candidates.push({
          id: candidateId("runtime-module-boundary", filePath, String(index), element.start ?? index),
          filePath,
          moduleId: String(index),
          boundaryKind: "runtime_module_table",
          runtimeKind: primaryRuntime,
          confidence: 0.78,
          riskLevel: "medium",
          loc: formatNodeLoc(element),
          sourceRange: formatSourceRange(element),
          astPath: formatAstPath(path),
          detail: `函数值模块表数组元素 ${index} 标记了 bundle 模块边界候选。`,
          evidenceRefs: [`script:${filePath}`, `runtime:${primaryRuntime}`]
        });
      });
    },
    ImportDeclaration(path: NodePath<t.ImportDeclaration>) {
      candidates.push(staticModuleBoundary(filePath, primaryRuntime, path, `静态导入 ${path.node.source.value} 将此脚本标记为 ESM 模块候选。`));
    },
    ExportNamedDeclaration(path: NodePath<t.ExportNamedDeclaration>) {
      candidates.push(staticModuleBoundary(filePath, primaryRuntime, path, "命名导出将此脚本标记为 ESM 模块候选。"));
    },
    ExportDefaultDeclaration(path: NodePath<t.ExportDefaultDeclaration>) {
      candidates.push(staticModuleBoundary(filePath, primaryRuntime, path, "默认导出将此脚本标记为 ESM 模块候选。"));
    },
    CallExpression(path: NodePath<t.CallExpression>) {
      if (t.isMemberExpression(path.node.callee) && memberExpressionName(path.node.callee) === "System.register") {
        candidates.push({
          id: candidateId("system-register-boundary", filePath, path.node.start ?? 0),
          filePath,
          moduleId: `${filePath}:system-register`,
          boundaryKind: "system_register",
          runtimeKind: "systemjs",
          confidence: 0.85,
          riskLevel: "medium",
          loc: formatNodeLoc(path.node),
          sourceRange: formatSourceRange(path.node),
          astPath: formatAstPath(path),
          detail: "System.register 调用标记了 SystemJS 模块边界候选。",
          evidenceRefs: [`script:${filePath}`, "runtime:systemjs"]
        });
      }
    }
  });

  if (isSingleBundle && candidates.length === 0 && source.trim().length > 0) {
    candidates.push({
      id: candidateId("single-bundle-boundary", filePath),
      filePath,
      moduleId: filePath,
      boundaryKind: "single_bundle",
      runtimeKind: primaryRuntime,
      confidence: 0.5,
      riskLevel: "medium",
      detail: "没有 HTML 入口的单个 JavaScript bundle 将作为一个尽力保留的模块边界。",
      evidenceRefs: [`script:${filePath}`]
    });
  }

  return dedupeById(candidates);
}

function collectImportExportCandidates(
  filePath: string,
  ast: ReturnType<typeof parse>,
  runtimeKinds: Set<RuntimeKind>
): ImportExportCandidate[] {
  const candidates: ImportExportCandidate[] = [];
  const primaryRuntime = preferredRuntimeKind(runtimeKinds);
  const traverseAst = getTraverse();

  traverseAst(ast, {
    ImportDeclaration(path: NodePath<t.ImportDeclaration>) {
      if (path.node.specifiers.length === 0) {
        candidates.push(importExportCandidate(filePath, "static_import", path, {
          source: path.node.source.value,
          runtimeKind: primaryRuntime,
          detail: `来自 ${path.node.source.value} 的副作用静态导入。`
        }));
        return;
      }
      for (const specifier of path.node.specifiers) {
        candidates.push(importExportCandidate(filePath, "static_import", path, {
          source: path.node.source.value,
          importedName: importSpecifierName(specifier),
          runtimeKind: primaryRuntime,
          detail: `来自 ${path.node.source.value} 的静态导入候选。`
        }));
      }
    },
    ExportNamedDeclaration(path: NodePath<t.ExportNamedDeclaration>) {
      const candidateKind = path.node.source?.value ? "re_export" : "named_export";
      if (path.node.specifiers.length === 0 && path.node.declaration) {
        candidates.push(importExportCandidate(filePath, candidateKind, path, {
          source: path.node.source?.value,
          exportedName: declarationExportName(path.node.declaration),
          runtimeKind: primaryRuntime,
          detail: "命名导出声明候选。"
        }));
        return;
      }
      for (const specifier of path.node.specifiers) {
        candidates.push(importExportCandidate(filePath, candidateKind, path, {
          source: path.node.source?.value,
          exportedName: exportSpecifierName(specifier),
          runtimeKind: primaryRuntime,
          detail: path.node.source?.value ? `来自 ${path.node.source.value} 的再导出候选。` : "命名导出说明符候选。"
        }));
      }
    },
    ExportDefaultDeclaration(path: NodePath<t.ExportDefaultDeclaration>) {
      candidates.push(importExportCandidate(filePath, "default_export", path, {
        exportedName: "default",
        runtimeKind: primaryRuntime,
        detail: "默认导出候选。"
      }));
    },
    CallExpression(path: NodePath<t.CallExpression>) {
      if (path.node.callee.type === "Import" && t.isStringLiteral(path.node.arguments[0])) {
        candidates.push(importExportCandidate(filePath, "dynamic_import", path, {
          source: path.node.arguments[0].value,
          runtimeKind: primaryRuntime,
          detail: `来自 ${path.node.arguments[0].value} 的动态导入候选。`
        }));
      }
      if (t.isIdentifier(path.node.callee, { name: "require" }) && t.isStringLiteral(path.node.arguments[0])) {
        candidates.push(importExportCandidate(filePath, "runtime_dependency", path, {
          source: path.node.arguments[0].value,
          runtimeKind: "commonjs",
          detail: `来自 ${path.node.arguments[0].value} 的 CommonJS require 依赖候选。`
        }));
      }
      if (t.isIdentifier(path.node.callee, { name: "__webpack_require__" }) && path.node.arguments.length > 0) {
        candidates.push(importExportCandidate(filePath, "runtime_dependency", path, {
          source: codeForNode(path.node.arguments[0] as t.Node),
          runtimeKind: "webpack",
          detail: "Webpack runtime 依赖候选。"
        }));
      }
    },
    AssignmentExpression(path: NodePath<t.AssignmentExpression>) {
      if (isCommonJsExportTarget(path.node.left)) {
        candidates.push(importExportCandidate(filePath, "commonjs_export", path, {
          exportedName: commonJsExportName(path.node.left),
          runtimeKind: "commonjs",
          detail: "CommonJS 导出赋值候选。"
        }));
      }
    }
  });

  return dedupeById(candidates);
}

async function buildTransformAnalysis(rootDir: string, astIndexes: AstIndex[]): Promise<TransformAnalysis> {
  const scriptTransforms: ScriptTransformRecord[] = [];
  const transformLog: TransformLogEntry[] = [];
  const rollbackMap: RollbackMapEntry[] = [];

  for (const astIndex of astIndexes) {
    const source = await fs.readFile(path.join(rootDir, astIndex.filePath), "utf8");
    const result = transformScriptSource(astIndex.filePath, source);
    scriptTransforms.push(result.record);
    transformLog.push(...result.transformLog);
    rollbackMap.push(...result.rollbackMap);
  }

  return {
    scriptTransforms,
    transformLog,
    rollbackMap
  };
}

function transformScriptSource(
  filePath: string,
  source: string
): { record: ScriptTransformRecord; transformLog: TransformLogEntry[]; rollbackMap: RollbackMapEntry[] } {
  const transformLog: TransformLogEntry[] = [];
  const rollbackMap: RollbackMapEntry[] = [];
  const transforms = new Set<string>();
  const wrapperMarks = new Set<string>();

  let ast: ReturnType<typeof parse>;
  try {
    ast = parse(source, {
      sourceType: "unambiguous",
      plugins: ["jsx", "typescript", "dynamicImport", "classProperties", "optionalChaining", "nullishCoalescingOperator"],
      errorRecovery: true,
      tokens: true
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "未知的 Babel 解析错误。";
    transformLog.push({ filePath, kind: "parse", status: "skipped", detail });
    return {
      record: {
        filePath,
        originalHash: sha256(Buffer.from(source)),
        transformedHash: sha256(Buffer.from(source)),
        transformedSource: source,
        transforms: [],
        wrapperMarks: []
      },
      transformLog,
      rollbackMap
    };
  }

  markProgramWrappers(filePath, ast.program.body, wrapperMarks, transformLog);
  restoreStaticStringArrayDecoders(filePath, ast, transforms, transformLog, rollbackMap);
  const traverseAst = getTraverse();

  traverseAst(ast, {
    ExpressionStatement(path: NodePath<t.ExpressionStatement>) {
      if (path.node.expression.type !== "SequenceExpression") {
        return;
      }
      const expressions = path.node.expression.expressions;
      if (expressions.length < 2 || expressions.some((expression) => expression.type === "YieldExpression" || expression.type === "AwaitExpression")) {
        transformLog.push({
          filePath,
          kind: "sequence_expression_expand",
          status: "skipped",
          originalLoc: formatNodeLoc(path.node),
          detail: "序列表达式包含 await/yield，或表达式数量过少。"
        });
        return;
      }
      const originalSnippet = codeForNode(path.node);
      const originalLoc = formatNodeLoc(path.node);
      const replacementStatements = expressions.map((expression) => t.expressionStatement(expression));
      const transformedSnippet = replacementStatements.map((statement) => codeForNode(statement)).join("\n");
      path.replaceWithMultiple(replacementStatements);
      transforms.add("sequence_expression_expand");
      logAppliedTransform(filePath, "sequence_expression_expand", originalLoc, originalSnippet, transformedSnippet, transformLog, rollbackMap);
    },
    MemberExpression(path: NodePath<t.MemberExpression>) {
      if (!path.node.computed || !t.isStringLiteral(path.node.property) || !isIdentifierName(path.node.property.value)) {
        return;
      }
      const originalSnippet = codeForNode(path.node);
      const originalLoc = formatNodeLoc(path.node);
      path.node.property = t.identifier(path.node.property.value);
      path.node.computed = false;
      transforms.add("computed_property_literal_restore");
      logAppliedTransform(filePath, "computed_property_literal_restore", originalLoc, originalSnippet, codeForNode(path.node), transformLog, rollbackMap);
    },
    BinaryExpression(path: NodePath<t.BinaryExpression>) {
      const literal = evaluateLowRiskBinaryExpression(path.node);
      if (!literal) {
        return;
      }
      const originalSnippet = codeForNode(path.node);
      const originalLoc = formatNodeLoc(path.node);
      const transformedSnippet = codeForNode(literal);
      path.replaceWith(literal);
      transforms.add("low_risk_constant_fold");
      logAppliedTransform(filePath, "low_risk_constant_fold", originalLoc, originalSnippet, transformedSnippet, transformLog, rollbackMap);
    }
  });

  const transformedSource = generate.default(ast, { comments: true, retainLines: true }, source).code;

  return {
    record: {
      filePath,
      originalHash: sha256(Buffer.from(source)),
      transformedHash: sha256(Buffer.from(transformedSource)),
      transformedSource,
      transforms: [...transforms].sort(),
      wrapperMarks: [...wrapperMarks].sort()
    },
    transformLog,
    rollbackMap
  };
}

interface StaticStringArrayProvider {
  name: string;
  node: t.FunctionDeclaration;
  values: string[];
}

interface StaticStringDecoderCandidate {
  decoderName: string;
  infrastructureNodes: Set<t.Node>;
  offset: number;
  values: string[];
}

type StaticPrimitive = string | number | boolean | null;

function restoreStaticStringArrayDecoders(
  filePath: string,
  ast: ReturnType<typeof parse>,
  transforms: Set<string>,
  transformLog: TransformLogEntry[],
  rollbackMap: RollbackMapEntry[]
): void {
  const providers = new Map<string, StaticStringArrayProvider>();
  for (const statement of ast.program.body) {
    if (!t.isFunctionDeclaration(statement)) continue;
    const provider = extractStaticStringArrayProvider(statement);
    if (provider) providers.set(provider.name, provider);
  }

  const candidates: StaticStringDecoderCandidate[] = [];
  for (const statement of ast.program.body) {
    if (!t.isFunctionDeclaration(statement)) continue;
    const decoder = extractStaticStringDecoder(statement, providers);
    if (!decoder) continue;
    const rotation = extractStaticStringArrayRotation(ast.program.body, decoder, providers.get(decoder.providerName));
    if (!rotation) continue;
    candidates.push({
      decoderName: decoder.decoderName,
      infrastructureNodes: new Set([statement, decoder.providerNode, rotation.node]),
      offset: decoder.offset,
      values: rotation.values
    });
  }
  if (candidates.length === 0) return;

  const traverseAst = getTraverse();
  traverseAst(ast, {
    CallExpression(path: NodePath<t.CallExpression>) {
      if (!t.isIdentifier(path.node.callee)) return;
      const firstArgument = path.node.arguments[0];
      if (!firstArgument || !t.isExpression(firstArgument)) return;
      const index = staticNumericExpression(firstArgument);
      if (index === undefined) return;

      for (const candidate of candidates) {
        if (path.findParent((parent) => candidate.infrastructureNodes.has(parent.node))) continue;
        const calleePath = path.get("callee");
        if (!calleePath.isIdentifier() || !identifierResolvesToDecoder(calleePath, candidate.decoderName)) continue;
        const valueIndex = index - candidate.offset;
        if (!Number.isInteger(valueIndex) || valueIndex < 0 || valueIndex >= candidate.values.length) continue;
        const decodedValue = candidate.values[valueIndex];
        const originalSnippet = codeForNode(path.node);
        const originalLoc = formatNodeLoc(path.node);
        const literal = t.stringLiteral(decodedValue);
        path.replaceWith(literal);
        transforms.add("string_array_decoder_restore");
        logAppliedTransform(
          filePath,
          "string_array_decoder_restore",
          originalLoc,
          originalSnippet,
          codeForNode(literal),
          transformLog,
          rollbackMap
        );
        break;
      }
    }
  });
}

function extractStaticStringArrayProvider(node: t.FunctionDeclaration): StaticStringArrayProvider | null {
  const functionName = node.id?.name;
  if (!functionName) return null;
  let arrayName: string | null = null;
  let values: string[] | null = null;
  for (const statement of node.body.body) {
    if (!t.isVariableDeclaration(statement)) continue;
    for (const declaration of statement.declarations) {
      if (!t.isIdentifier(declaration.id) || !t.isArrayExpression(declaration.init)) continue;
      if (declaration.init.elements.some((element) => !t.isStringLiteral(element))) continue;
      arrayName = declaration.id.name;
      values = declaration.init.elements.map((element) => (element as t.StringLiteral).value);
      break;
    }
    if (values) break;
  }
  if (!arrayName || !values || values.length === 0) return null;

  const hasSelfReplacement = node.body.body.some((statement) => {
    if (!t.isExpressionStatement(statement) || !t.isAssignmentExpression(statement.expression, { operator: "=" })) return false;
    const assignment = statement.expression;
    if (!t.isIdentifier(assignment.left, { name: functionName }) || !t.isFunctionExpression(assignment.right)) return false;
    return assignment.right.body.body.some(
      (innerStatement) => t.isReturnStatement(innerStatement) && t.isIdentifier(innerStatement.argument, { name: arrayName })
    );
  });
  const returnsSelfCall = node.body.body.some(
    (statement) =>
      t.isReturnStatement(statement) &&
      t.isCallExpression(statement.argument) &&
      t.isIdentifier(statement.argument.callee, { name: functionName }) &&
      statement.argument.arguments.length === 0
  );
  if (!hasSelfReplacement || !returnsSelfCall) return null;
  return { name: functionName, node, values };
}

function extractStaticStringDecoder(
  node: t.FunctionDeclaration,
  providers: Map<string, StaticStringArrayProvider>
): { decoderName: string; offset: number; providerName: string; providerNode: t.FunctionDeclaration } | null {
  const decoderName = node.id?.name;
  if (!decoderName) return null;
  let providerName: string | null = null;
  let arrayAlias: string | null = null;
  for (const statement of node.body.body) {
    if (!t.isVariableDeclaration(statement)) continue;
    for (const declaration of statement.declarations) {
      if (
        t.isIdentifier(declaration.id) &&
        t.isCallExpression(declaration.init) &&
        t.isIdentifier(declaration.init.callee) &&
        declaration.init.arguments.length === 0 &&
        providers.has(declaration.init.callee.name)
      ) {
        arrayAlias = declaration.id.name;
        providerName = declaration.init.callee.name;
        break;
      }
    }
    if (providerName) break;
  }
  if (!providerName || !arrayAlias) return null;

  const returnStatement = node.body.body.find(
    (statement): statement is t.ReturnStatement => t.isReturnStatement(statement) && t.isSequenceExpression(statement.argument)
  );
  if (!returnStatement || !t.isSequenceExpression(returnStatement.argument)) return null;
  const assignment = returnStatement.argument.expressions.find(
    (expression): expression is t.AssignmentExpression =>
      t.isAssignmentExpression(expression, { operator: "=" }) &&
      t.isIdentifier(expression.left, { name: decoderName }) &&
      t.isFunctionExpression(expression.right)
  );
  if (!assignment || !t.isFunctionExpression(assignment.right)) return null;
  const innerFunction = assignment.right;
  const indexParameter = innerFunction.params[0];
  if (!t.isIdentifier(indexParameter)) return null;

  let offset: number | undefined;
  let valueName: string | null = null;
  let returnsArrayValue = false;
  for (const statement of innerFunction.body.body) {
    if (
      t.isExpressionStatement(statement) &&
      t.isAssignmentExpression(statement.expression, { operator: "=" }) &&
      t.isIdentifier(statement.expression.left, { name: indexParameter.name }) &&
      t.isBinaryExpression(statement.expression.right, { operator: "-" }) &&
      t.isIdentifier(statement.expression.right.left, { name: indexParameter.name })
    ) {
      offset = staticNumericExpression(statement.expression.right.right);
      continue;
    }
    if (t.isVariableDeclaration(statement)) {
      for (const declaration of statement.declarations) {
        if (
          t.isIdentifier(declaration.id) &&
          t.isMemberExpression(declaration.init, { computed: true }) &&
          t.isIdentifier(declaration.init.object, { name: arrayAlias }) &&
          t.isIdentifier(declaration.init.property, { name: indexParameter.name })
        ) {
          valueName = declaration.id.name;
        }
      }
      continue;
    }
    if (t.isReturnStatement(statement)) {
      if (valueName && t.isIdentifier(statement.argument, { name: valueName })) returnsArrayValue = true;
      if (
        t.isMemberExpression(statement.argument, { computed: true }) &&
        t.isIdentifier(statement.argument.object, { name: arrayAlias }) &&
        t.isIdentifier(statement.argument.property, { name: indexParameter.name })
      ) {
        returnsArrayValue = true;
      }
    }
  }
  if (offset === undefined || !returnsArrayValue) return null;
  const provider = providers.get(providerName);
  if (!provider) return null;
  return { decoderName, offset, providerName, providerNode: provider.node };
}

function extractStaticStringArrayRotation(
  body: t.Statement[],
  decoder: { decoderName: string; offset: number; providerName: string },
  provider: StaticStringArrayProvider | undefined
): { node: t.ExpressionStatement; values: string[] } | null {
  if (!provider) return null;
  for (const statement of body) {
    if (!t.isExpressionStatement(statement) || !t.isCallExpression(statement.expression)) continue;
    const call = statement.expression;
    if (!t.isFunctionExpression(call.callee) || call.arguments.length < 2) continue;
    const providerArgument = call.arguments[0];
    const targetArgument = call.arguments[1];
    if (!t.isIdentifier(providerArgument, { name: decoder.providerName }) || !t.isExpression(targetArgument)) continue;
    const target = staticNumericExpression(targetArgument);
    if (target === undefined) continue;
    const providerParameter = call.callee.params[0];
    if (!t.isIdentifier(providerParameter)) continue;

    const declarations: Array<{ id: string; init: t.Expression | null }> = [];
    let arrayName: string | null = null;
    let rotatesArray = false;
    t.traverseFast(call.callee.body, (child) => {
      if (t.isVariableDeclarator(child) && t.isIdentifier(child.id)) {
        declarations.push({ id: child.id.name, init: t.isExpression(child.init) ? child.init : null });
        if (
          t.isCallExpression(child.init) &&
          t.isIdentifier(child.init.callee, { name: providerParameter.name }) &&
          child.init.arguments.length === 0
        ) {
          arrayName = child.id.name;
        }
      }
    });
    if (!arrayName) continue;
    t.traverseFast(call.callee.body, (child) => {
      if (!t.isCallExpression(child) || !t.isMemberExpression(child.callee)) return;
      if (!t.isIdentifier(child.callee.object, { name: arrayName ?? undefined })) return;
      if (memberPropertyName(child.callee) !== "push" || child.arguments.length !== 1) return;
      const shifted = child.arguments[0];
      if (
        t.isCallExpression(shifted) &&
        t.isMemberExpression(shifted.callee) &&
        t.isIdentifier(shifted.callee.object, { name: arrayName ?? undefined }) &&
        memberPropertyName(shifted.callee) === "shift"
      ) {
        rotatesArray = true;
      }
    });
    if (!rotatesArray) continue;

    const decoderAliases = new Set([decoder.decoderName]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const declaration of declarations) {
        if (t.isIdentifier(declaration.init) && decoderAliases.has(declaration.init.name) && !decoderAliases.has(declaration.id)) {
          decoderAliases.add(declaration.id);
          changed = true;
        }
      }
    }
    for (const declaration of declarations) {
      if (!declaration.init) continue;
      const values = rotateStringArrayUntilTarget(provider.values, declaration.init, target, decoderAliases, decoder.offset);
      if (values) return { node: statement, values };
    }
  }
  return null;
}

function rotateStringArrayUntilTarget(
  initialValues: string[],
  checksumExpression: t.Expression,
  target: number,
  decoderAliases: Set<string>,
  offset: number
): string[] | null {
  const values = [...initialValues];
  for (let rotation = 0; rotation < values.length; rotation += 1) {
    const checksum = evaluateStaticDecoderExpression(checksumExpression, values, decoderAliases, offset);
    if (typeof checksum === "number" && checksum === target) return values;
    values.push(values.shift() as string);
  }
  return null;
}

function evaluateStaticDecoderExpression(
  node: t.Expression,
  values: string[],
  decoderAliases: Set<string>,
  offset: number
): StaticPrimitive | undefined {
  const literal = literalValueForExpression(node);
  if (literal !== undefined) return literal;
  if (t.isUnaryExpression(node) && t.isExpression(node.argument)) {
    const argument = evaluateStaticDecoderExpression(node.argument, values, decoderAliases, offset);
    if (argument === undefined) return undefined;
    if (node.operator === "+") return Number(argument);
    if (node.operator === "-") return -Number(argument);
    if (node.operator === "!") return !argument;
    if (node.operator === "~") return ~Number(argument);
    return undefined;
  }
  if (t.isBinaryExpression(node) && t.isExpression(node.left) && t.isExpression(node.right)) {
    const left = evaluateStaticDecoderExpression(node.left, values, decoderAliases, offset);
    const right = evaluateStaticDecoderExpression(node.right, values, decoderAliases, offset);
    if (left === undefined || right === undefined) return undefined;
    switch (node.operator) {
      case "+":
        return typeof left === "string" || typeof right === "string" ? String(left) + String(right) : Number(left) + Number(right);
      case "-": return Number(left) - Number(right);
      case "*": return Number(left) * Number(right);
      case "/": return Number(left) / Number(right);
      case "%": return Number(left) % Number(right);
      case "**": return Number(left) ** Number(right);
      case "|": return Number(left) | Number(right);
      case "&": return Number(left) & Number(right);
      case "^": return Number(left) ^ Number(right);
      case "<<": return Number(left) << Number(right);
      case ">>": return Number(left) >> Number(right);
      case ">>>": return Number(left) >>> Number(right);
      default: return undefined;
    }
  }
  if (t.isCallExpression(node) && t.isIdentifier(node.callee)) {
    if (decoderAliases.has(node.callee.name)) {
      const argument = node.arguments[0];
      if (!argument || !t.isExpression(argument)) return undefined;
      const index = staticNumericExpression(argument);
      if (index === undefined) return undefined;
      const valueIndex = index - offset;
      return Number.isInteger(valueIndex) && valueIndex >= 0 && valueIndex < values.length ? values[valueIndex] : undefined;
    }
    if (node.callee.name === "parseInt") {
      const argument = node.arguments[0];
      if (!argument || !t.isExpression(argument)) return undefined;
      const value = evaluateStaticDecoderExpression(argument, values, decoderAliases, offset);
      if (value === undefined) return undefined;
      const radixArgument = node.arguments[1];
      const radix = radixArgument && t.isExpression(radixArgument) ? staticNumericExpression(radixArgument) : undefined;
      return Number.parseInt(String(value), radix);
    }
  }
  return undefined;
}

function staticNumericExpression(node: t.Expression): number | undefined {
  if (t.isNumericLiteral(node)) return node.value;
  if (t.isUnaryExpression(node) && (node.operator === "+" || node.operator === "-") && t.isNumericLiteral(node.argument)) {
    return node.operator === "-" ? -node.argument.value : node.argument.value;
  }
  return undefined;
}

function memberPropertyName(node: t.MemberExpression): string | null {
  if (!node.computed && t.isIdentifier(node.property)) return node.property.name;
  if (node.computed && t.isStringLiteral(node.property)) return node.property.value;
  return null;
}

function identifierResolvesToDecoder(
  path: NodePath<t.Identifier>,
  decoderName: string,
  seenNodes: Set<t.Node> = new Set()
): boolean {
  const binding = path.scope.getBinding(path.node.name);
  if (!binding || seenNodes.has(binding.path.node)) return false;
  seenNodes.add(binding.path.node);
  if (binding.path.isFunctionDeclaration()) {
    return binding.path.node.id?.name === decoderName;
  }
  if (binding.path.isVariableDeclarator() && t.isIdentifier(binding.path.node.init)) {
    const initPath = binding.path.get("init");
    return !Array.isArray(initPath) && initPath.isIdentifier()
      ? identifierResolvesToDecoder(initPath as NodePath<t.Identifier>, decoderName, seenNodes)
      : false;
  }
  return false;
}

function addGraphNode(nodes: Map<string, GraphNodeRecord>, id: string, kind: string, label: string): void {
  if (nodes.has(id)) {
    return;
  }
  nodes.set(id, { id, kind, label });
}

function addGraphEdge(edges: Map<string, GraphEdgeRecord>, from: string, to: string, kind: string): void {
  const edgeId = `${from} -> ${to} :: ${kind}`;
  if (edges.has(edgeId)) {
    return;
  }
  edges.set(edgeId, { from, to, kind });
}

function compareGraphNode(left: GraphNodeRecord, right: GraphNodeRecord): number {
  return left.id.localeCompare(right.id);
}

function compareGraphEdge(left: GraphEdgeRecord, right: GraphEdgeRecord): number {
  const byFrom = left.from.localeCompare(right.from);
  if (byFrom !== 0) {
    return byFrom;
  }
  const byTo = left.to.localeCompare(right.to);
  if (byTo !== 0) {
    return byTo;
  }
  return left.kind.localeCompare(right.kind);
}

function getTraverse(): (ast: unknown, visitors: TraverseOptions) => void {
  const traverse = (traverseModule as unknown as { default?: (ast: unknown, visitors: TraverseOptions) => void })?.default ?? (traverseModule as unknown as (ast: unknown, visitors: TraverseOptions) => void);
  if (typeof traverse !== "function") {
    throw new Error("Babel traverse runtime 不可用。");
  }
  return traverse;
}

function resolveScriptImportCandidate(
  importerPath: string,
  importSource: string,
  fileKindByPath: Map<string, InputFileRecord["kind"]>
): string | null {
  if (!importSource || importSource.startsWith("\0")) {
    return null;
  }
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(importSource) && !importSource.startsWith("file:")) {
    return null;
  }
  const trimmed = importSource.trim();
  if (!trimmed || trimmed.startsWith("#")) {
    return null;
  }
  const normalized = trimmed.replace(/\\/g, "/").split(/[?#]/, 1)[0];
  const importerDir = path.posix.dirname(importerPath);
  const relativeCandidates = normalized.startsWith(".")
    ? [path.posix.normalize(path.posix.join(importerDir, normalized))]
    : [normalized, path.posix.normalize(path.posix.join(importerDir, normalized))];
  const extensionCandidates = normalized.includes(".")
    ? relativeCandidates
    : [...relativeCandidates.flatMap((candidate) => [candidate, `${candidate}.js`, `${candidate}.ts`, `${candidate}.tsx`, `${candidate}.mjs`, `${candidate}.cjs`])];
  for (const candidate of extensionCandidates) {
    const normalizedCandidate = toPosix(candidate).replace(/^\.?\//, "");
    if (fileKindByPath.get(normalizedCandidate) === "script" || fileKindByPath.get(normalizedCandidate) === "unknown") {
      return normalizedCandidate;
    }
  }
  return null;
}

function markProgramWrappers(
  filePath: string,
  body: t.Statement[],
  wrapperMarks: Set<string>,
  transformLog: TransformLogEntry[]
): void {
  if (body.length === 0) {
    return;
  }
  const firstStatement = body[0];
  if (firstStatement.type === "ExpressionStatement" && firstStatement.expression.type === "CallExpression") {
    wrapperMarks.add("call_expression_wrapper");
    transformLog.push({
      filePath,
      kind: "wrapper_mark",
      status: "applied",
      originalLoc: formatNodeLoc(firstStatement),
      sourceRange: formatSourceRange(firstStatement),
      astPath: "Program.body[0]",
      riskLevel: "low",
      reversible: true,
      evidenceRefs: [`script:${filePath}`],
      detail: "已标记调用表达式包装器，供后续重建。"
    });
  }
  if (body.length === 1 && body[0].type === "FunctionDeclaration") {
    wrapperMarks.add("single_function_wrapper");
    transformLog.push({
      filePath,
      kind: "wrapper_mark",
      status: "applied",
      originalLoc: formatNodeLoc(body[0]),
      sourceRange: formatSourceRange(body[0]),
      astPath: "Program.body[0]",
      riskLevel: "medium",
      reversible: true,
      evidenceRefs: [`script:${filePath}`],
      detail: "已标记单函数包装器，供后续重建。"
    });
  }
}

function evaluateLowRiskBinaryExpression(node: t.BinaryExpression): t.Expression | null {
  if (!t.isExpression(node.left) || !t.isExpression(node.right)) {
    return null;
  }
  const left = literalValueForExpression(node.left);
  const right = literalValueForExpression(node.right);
  if (left === undefined || right === undefined) {
    return null;
  }
  switch (node.operator) {
    case "+":
      if (typeof left === "string" || typeof right === "string") {
        return t.stringLiteral(String(left) + String(right));
      }
      if (typeof left === "number" && typeof right === "number") {
        return t.numericLiteral(left + right);
      }
      return null;
    case "-":
      if (typeof left === "number" && typeof right === "number") {
        return t.numericLiteral(left - right);
      }
      return null;
    case "*":
      if (typeof left === "number" && typeof right === "number") {
        return t.numericLiteral(left * right);
      }
      return null;
    case "/":
      if (typeof left === "number" && typeof right === "number" && right !== 0) {
        return t.numericLiteral(left / right);
      }
      return null;
    default:
      return null;
  }
}

function literalValueForExpression(node: t.Expression): string | number | boolean | null | undefined {
  if (t.isStringLiteral(node)) {
    return node.value;
  }
  if (t.isNumericLiteral(node)) {
    return node.value;
  }
  if (t.isBooleanLiteral(node)) {
    return node.value;
  }
  if (t.isNullLiteral(node)) {
    return null;
  }
  return undefined;
}

function runtimeKindFromDetected(detectedRuntime: string[]): RuntimeKind {
  if (detectedRuntime.includes("webpack")) {
    return "webpack";
  }
  if (detectedRuntime.includes("vite_or_rollup")) {
    return "vite_or_rollup";
  }
  if (detectedRuntime.includes("single_bundle_best_effort")) {
    return "iife";
  }
  return "unknown";
}

function preferredRuntimeKind(runtimeKinds: Set<RuntimeKind>): RuntimeKind {
  const preference: RuntimeKind[] = ["webpack", "vite_or_rollup", "rollup", "esbuild", "umd", "iife", "browserify", "systemjs", "parcel", "commonjs"];
  return preference.find((runtimeKind) => runtimeKinds.has(runtimeKind)) ?? "unknown";
}

function markerRuntimeWrapper(
  filePath: string,
  runtimeKind: RuntimeKind,
  wrapperKind: string,
  source: string,
  detail: string
): RuntimeWrapperCandidate {
  const markerIndex = markerIndexForRuntime(source, runtimeKind);
  return {
    id: candidateId("runtime-wrapper", filePath, runtimeKind, wrapperKind),
    filePath,
    runtimeKind,
    wrapperKind,
    confidence: 0.78,
    riskLevel: "low",
    sourceRange: markerIndex >= 0 ? { start: markerIndex, end: markerIndex + runtimeKind.length } : undefined,
    detail,
    evidenceRefs: [`script:${filePath}`, `runtime:${runtimeKind}`]
  };
}

function markerIndexForRuntime(source: string, runtimeKind: RuntimeKind): number {
  const markers = SUPPORTED_RUNTIME_MARKERS.find((entry) => entry.runtimeKind === runtimeKind)?.markers ?? [];
  for (const marker of markers) {
    const index = source.indexOf(marker);
    if (index >= 0) {
      return index;
    }
  }
  return -1;
}

function staticModuleBoundary(
  filePath: string,
  runtimeKind: RuntimeKind,
  nodePath: NodePath<t.ImportDeclaration | t.ExportNamedDeclaration | t.ExportDefaultDeclaration>,
  detail: string
): ModuleBoundaryCandidate {
  return {
    id: candidateId("static-module-boundary", filePath, nodePath.node.type, nodePath.node.start ?? 0),
    filePath,
    moduleId: filePath,
    boundaryKind: "static_es_module",
    runtimeKind,
    confidence: 0.9,
    riskLevel: "low",
    sourcePath: filePath,
    loc: formatNodeLoc(nodePath.node),
    sourceRange: formatSourceRange(nodePath.node),
    astPath: formatAstPath(nodePath),
    detail,
    evidenceRefs: [`script:${filePath}`, "syntax:esm"]
  };
}

function importExportCandidate(
  filePath: string,
  candidateKind: ImportExportCandidate["candidateKind"],
  nodePath: NodePath,
  options: {
    source?: string;
    importedName?: string;
    exportedName?: string;
    runtimeKind?: RuntimeKind;
    detail: string;
  }
): ImportExportCandidate {
  const source = options.source;
  return {
    id: candidateId("import-export", filePath, candidateKind, source ?? "", options.importedName ?? "", options.exportedName ?? "", nodePath.node.start ?? 0),
    filePath,
    candidateKind,
    source,
    importedName: options.importedName,
    exportedName: options.exportedName,
    runtimeKind: options.runtimeKind,
    confidence: candidateKind === "runtime_dependency" || candidateKind === "commonjs_export" ? 0.75 : 0.92,
    riskLevel: candidateKind === "runtime_dependency" || candidateKind === "commonjs_export" ? "medium" : "low",
    loc: formatNodeLoc(nodePath.node),
    sourceRange: formatSourceRange(nodePath.node),
    astPath: formatAstPath(nodePath),
    detail: options.detail,
    evidenceRefs: [`script:${filePath}`, `syntax:${candidateKind}`]
  };
}

function importSpecifierName(specifier: t.ImportDeclaration["specifiers"][number]): string {
  if (t.isImportDefaultSpecifier(specifier)) {
    return "default";
  }
  if (t.isImportNamespaceSpecifier(specifier)) {
    return "*";
  }
  return specifier.imported.type === "Identifier" ? specifier.imported.name : specifier.imported.value;
}

function exportSpecifierName(specifier: t.ExportNamedDeclaration["specifiers"][number]): string {
  if (t.isExportSpecifier(specifier)) {
    return specifier.exported.type === "Identifier" ? specifier.exported.name : specifier.exported.value;
  }
  if (t.isExportNamespaceSpecifier(specifier)) {
    return specifier.exported.name;
  }
  return "default";
}

function declarationExportName(declaration: t.Declaration): string | undefined {
  if ((t.isFunctionDeclaration(declaration) || t.isClassDeclaration(declaration)) && declaration.id?.name) {
    return declaration.id.name;
  }
  if (t.isVariableDeclaration(declaration)) {
    const first = declaration.declarations[0];
    return t.isIdentifier(first?.id) ? first.id.name : undefined;
  }
  return undefined;
}

function propertyKeyName(key: t.ObjectProperty["key"]): string | null {
  if (t.isIdentifier(key)) {
    return key.name;
  }
  if (t.isStringLiteral(key) || t.isNumericLiteral(key)) {
    return String(key.value);
  }
  return null;
}

function memberExpressionName(node: t.MemberExpression): string {
  const objectName = t.isIdentifier(node.object) ? node.object.name : t.isMemberExpression(node.object) ? memberExpressionName(node.object) : codeForNode(node.object);
  const propertyName = t.isIdentifier(node.property) ? node.property.name : t.isStringLiteral(node.property) ? node.property.value : codeForNode(node.property);
  return `${objectName}.${propertyName}`;
}

function isCommonJsExportTarget(node: t.LVal | t.Expression): boolean {
  if (!t.isMemberExpression(node)) {
    return false;
  }
  const name = memberExpressionName(node);
  return name === "module.exports" || name.startsWith("exports.") || name.startsWith("module.exports.");
}

function commonJsExportName(node: t.LVal | t.Expression): string | undefined {
  if (!t.isMemberExpression(node)) {
    return undefined;
  }
  const name = memberExpressionName(node);
  if (name === "module.exports") {
    return "default";
  }
  return name.replace(/^module\.exports\./, "").replace(/^exports\./, "");
}

function isIifeCallExpression(node: t.CallExpression): boolean {
  return t.isFunctionExpression(node.callee) || t.isArrowFunctionExpression(node.callee);
}

function formatSourceRange(node: t.Node): SourceRange | undefined {
  return typeof node.start === "number" && typeof node.end === "number" ? { start: node.start, end: node.end } : undefined;
}

function formatAstPath(path: NodePath): string {
  const segments: string[] = [];
  let current: NodePath | null = path;
  while (current) {
    const key = typeof current.key === "number" ? `[${current.key}]` : current.key ? `.${String(current.key)}` : "";
    segments.unshift(`${current.node.type}${key}`);
    current = current.parentPath;
  }
  return segments.join("/");
}

function candidateId(...parts: Array<string | number>): string {
  return parts
    .map((part) => String(part).replace(/[^A-Za-z0-9_.:-]+/g, "_"))
    .join(":")
    .replace(/_+/g, "_");
}

function dedupeById<T extends { id: string }>(records: T[]): T[] {
  const deduped = new Map<string, T>();
  for (const record of records) {
    deduped.set(record.id, record);
  }
  return [...deduped.values()];
}

function sortRuntimeWrappers(records: RuntimeWrapperCandidate[]): RuntimeWrapperCandidate[] {
  return dedupeById(records).sort((left, right) => left.id.localeCompare(right.id));
}

function sortModuleBoundaries(records: ModuleBoundaryCandidate[]): ModuleBoundaryCandidate[] {
  return dedupeById(records).sort((left, right) => left.id.localeCompare(right.id));
}

function sortImportExportCandidates(records: ImportExportCandidate[]): ImportExportCandidate[] {
  return dedupeById(records).sort((left, right) => left.id.localeCompare(right.id));
}

function sanitizeGeneratedModulePath(candidatePath: string): string {
  const normalized = candidatePath.replace(/\\/g, "/").replace(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/*/, "").replace(/^\/+/, "");
  const parts = normalized.split("/").filter((part) => part && part !== "." && part !== "..");
  const sanitizedParts = parts.map((part) => part.replace(/[^A-Za-z0-9._-]/g, "_"));
  return sanitizedParts.join("/") || "module";
}

function withTsExtension(candidatePath: string): string {
  const extension = path.posix.extname(candidatePath);
  if (extension === ".ts" || extension === ".tsx") {
    return candidatePath;
  }
  if ([".js", ".mjs", ".cjs", ".jsx"].includes(extension)) {
    return `${candidatePath.slice(0, -extension.length)}.ts`;
  }
  return `${candidatePath}.ts`;
}

function uniqueGeneratedModulePath(seen: Set<string>, desiredPath: string): string {
  let candidate = desiredPath;
  const extension = path.posix.extname(desiredPath);
  const stem = extension ? desiredPath.slice(0, -extension.length) : desiredPath;
  let index = 2;
  while (seen.has(candidate)) {
    candidate = `${stem}-${index}${extension}`;
    index += 1;
  }
  seen.add(candidate);
  return candidate;
}

function logAppliedTransform(
  filePath: string,
  kind: string,
  originalLoc: string | undefined,
  originalSnippet: string,
  transformedSnippet: string,
  transformLog: TransformLogEntry[],
  rollbackMap: RollbackMapEntry[]
): void {
  transformLog.push({
    filePath,
    kind,
    status: "applied",
    originalLoc,
    riskLevel: "low",
    reversible: true,
    evidenceRefs: [`script:${filePath}`],
    originalSnippet,
    transformedSnippet
  });
  rollbackMap.push({
    filePath,
    kind,
    originalLoc,
    transformedLoc: originalLoc,
    riskLevel: "low",
    reversible: true,
    evidenceRefs: [`script:${filePath}`],
    originalSnippet,
    transformedSnippet
  });
}

function codeForNode(node: t.Node): string {
  return generate.default(node, { comments: false, compact: true }).code;
}

function isIdentifierName(value: string): boolean {
  return /^[$A-Z_a-z][$0-9A-Z_a-z]*$/.test(value);
}

export function planReconstruction(
  analysis: CoreAnalysisResult,
  config: AnalyzeInputConfig = {}
): ReconstructionPlan {
  const limitations = [
    ...analysis.inventory.warnings,
    "生成项目是确定性的静态宿主外壳；模块语义恢复仍是受证据约束的后续工作。",
    "在依赖安装策略实现之前，构建和类型检查脚本仅作为离线验证垫片。"
  ];
  const generatedDependencyPlaceholders = analysis.dependencyPlaceholders.filter((record) => record.status === "generated");
  const unsupportedDependencyPlaceholders = analysis.dependencyPlaceholders.filter((record) => record.status === "unsupported");
  if (generatedDependencyPlaceholders.length > 0) {
    limitations.push(
      `${generatedDependencyPlaceholders.length} 个缺失的静态相对 ESM 依赖引用使用显式的仅加载占位模块；语义行为仍不可用，占位导出在调用时会抛出异常。`
    );
  }
  if (unsupportedDependencyPlaceholders.length > 0) {
    limitations.push(
      `${unsupportedDependencyPlaceholders.length} 个缺失的静态相对 ESM 依赖引用无法安全生成占位模块，仅保留在报告中。`
    );
  }

  return {
    kind: "reconstruction_plan",
    jobId: config.jobId,
    strategy: "static_host_project",
    entryHtml: analysis.inventory.entries[0] ?? null,
    sourceFiles: analysis.inventory.files.map((file) => file.path),
    scripts: analysis.inventory.scripts,
    styles: analysis.inventory.styles,
    assets: analysis.inventory.assets,
    sourceMaps: analysis.inventory.sourceMaps,
    manifests: analysis.inventory.manifests,
    detectedRuntime: analysis.detectedRuntime,
    generatedFiles: [...GENERATED_PROJECT_FILES, ...dependencyPlaceholderProjectPaths(analysis.dependencyPlaceholders)],
    inputInventory: analysis.inventory,
    astIndexes: analysis.astIndexes,
    sourceMapAnalysis: analysis.sourceMapAnalysis,
    graphAnalysis: analysis.graphAnalysis,
    moduleRecoveryAnalysis: analysis.moduleRecoveryAnalysis,
    runtimeWrappers: analysis.moduleRecoveryAnalysis.runtimeWrappers,
    moduleBoundaries: analysis.moduleRecoveryAnalysis.moduleBoundaries,
    importExportCandidates: analysis.moduleRecoveryAnalysis.importExportCandidates,
    generatedModules: analysis.moduleRecoveryAnalysis.generatedModules,
    dependencyPlaceholders: analysis.dependencyPlaceholders,
    scriptTransforms: analysis.transformAnalysis.scriptTransforms,
    transformLog: analysis.transformAnalysis.transformLog,
    rollbackMap: analysis.transformAnalysis.rollbackMap,
    evidenceSummary: {
      astIndexFiles: analysis.astIndexes.map((index) => index.filePath),
      chunkGraphEdgeCount: analysis.graphAnalysis.chunkGraph.edges.length,
      resourceGraphEdgeCount: analysis.graphAnalysis.resourceGraph.edges.length,
      moduleCandidateCount: analysis.graphAnalysis.moduleCandidateGraph.nodes.filter((node) => node.kind === "source_candidate").length,
      runtimeWrapperCount: analysis.moduleRecoveryAnalysis.runtimeWrappers.length,
      moduleBoundaryCount: analysis.moduleRecoveryAnalysis.moduleBoundaries.length,
      importExportCandidateCount: analysis.moduleRecoveryAnalysis.importExportCandidates.length,
      generatedModuleCount: analysis.moduleRecoveryAnalysis.generatedModules.length,
      dependencyPlaceholderCount: analysis.dependencyPlaceholders.length,
      transformCount: analysis.transformAnalysis.transformLog.filter((entry) => entry.status === "applied" && entry.kind !== "wrapper_mark").length,
      symbolCount: analysis.astIndexes.reduce((count, index) => count + index.symbols.length, 0)
    },
    limitations
  };
}

export async function writeProject(plan: ReconstructionPlan, config: WriteProjectConfig): Promise<WriteProjectResult> {
  const normalized = await normalizeInputPackage(config.inputPath);
  try {
    const agentFeedback = config.agentFeedback === undefined ? undefined : validateAgentFeedbackInput(config.agentFeedback);
    const inputRoot = path.resolve(normalized.rootDir);
    const projectRoot = assertSafeOutputDir(config.outputDir);
    await fs.rm(projectRoot, { recursive: true, force: true });
    await fs.mkdir(path.join(projectRoot, "src"), { recursive: true });
    await fs.mkdir(path.join(projectRoot, "src", "analysis"), { recursive: true });
    await fs.mkdir(path.join(projectRoot, "src", "entrypoints"), { recursive: true });
    await fs.mkdir(path.join(projectRoot, "src", "modules"), { recursive: true });
    await fs.mkdir(path.join(projectRoot, "src", "modules", "scripts"), { recursive: true });
    await fs.mkdir(path.join(projectRoot, "src", "modules", "recovered"), { recursive: true });
    await fs.mkdir(path.join(projectRoot, "src", "runtime-shims"), { recursive: true });
    await fs.mkdir(path.join(projectRoot, "src", "transformed"), { recursive: true });
    await fs.mkdir(path.join(projectRoot, "src", "types"), { recursive: true });
    await fs.mkdir(path.join(projectRoot, "scripts"), { recursive: true });
    await fs.mkdir(path.join(projectRoot, "public", "original"), { recursive: true });

    const copiedSourceFiles: string[] = [];
    for (const sourceFile of plan.sourceFiles) {
      const safeRelative = safeRelativePath(sourceFile);
      const sourcePath = path.join(inputRoot, safeRelative);
      const targetPath = path.join(projectRoot, "public", "original", safeRelative);
      await fs.mkdir(path.dirname(targetPath), { recursive: true });
      await fs.copyFile(sourcePath, targetPath);
      copiedSourceFiles.push(`public/original/${toPosix(safeRelative)}`);
    }
    const transformedSourceFiles: string[] = [];
    for (const transform of plan.scriptTransforms) {
      const safeRelative = safeRelativePath(transform.filePath);
      const targetPath = path.join(projectRoot, "src", "transformed", safeRelative);
      await fs.mkdir(path.dirname(targetPath), { recursive: true });
      await fs.writeFile(targetPath, transform.transformedSource, "utf8");
      transformedSourceFiles.push(`src/transformed/${toPosix(safeRelative)}`);
    }
    const analysisFiles = [
      "src/analysis/input-inventory.json",
      "src/analysis/ast-indexes.json",
      "src/analysis/source-map-analysis.json",
      "src/analysis/graph-analysis.json",
      "src/analysis/runtime-wrappers.json",
      "src/analysis/module-boundaries.json",
      "src/analysis/import-export-candidates.json",
      "src/analysis/generated-modules.json",
      "src/analysis/dependency-placeholders.json",
      "src/analysis/transform-log.json",
      "src/analysis/rollback-map.json"
    ];
    const generatedModuleFiles = await writeGeneratedModules(projectRoot, plan);
    const dependencyPlaceholderFiles = await writeDependencyPlaceholders(projectRoot, plan.dependencyPlaceholders);
    const entrypointFiles = ["src/entrypoints/reconstruction-entry.ts"];
    const typeDefinitionFiles = ["src/types/reconstruction.d.ts"];
    const runtimeShimFiles = ["src/runtime-shims/browser-globals.ts"];

    const manifest: GeneratedProjectManifest = {
      kind: "generated_project",
      jobId: plan.jobId,
      projectPath: ".",
      entrypoint: "index.html",
      generatedFiles: [...GENERATED_PROJECT_FILES, ...dependencyPlaceholderFiles],
      copiedSourceFiles,
      transformedSourceFiles,
      generatedModuleFiles,
      entrypointFiles,
      typeDefinitionFiles,
      runtimeShimFiles,
      dependencyPlaceholderFiles,
      dependencyPlaceholders: plan.dependencyPlaceholders,
      analysisFiles,
      sourceRoot: "public/original",
      limitations: plan.limitations
    };

    await writeJson(path.join(projectRoot, "package.json"), {
      name: config.packageName ?? "ai-jsunpack-generated-project",
      version: "0.0.0",
      private: true,
      type: "module",
      scripts: {
        build: "node scripts/build.mjs",
        typecheck: "node scripts/typecheck.mjs"
      }
    });
    await writeJson(path.join(projectRoot, "tsconfig.json"), {
      compilerOptions: {
        target: "ES2022",
        module: "ESNext",
        moduleResolution: "Bundler",
        strict: true,
        noEmit: true,
        resolveJsonModule: true
      },
      include: ["src/**/*.ts"]
    });
    await writeJson(path.join(projectRoot, "src", "analysis", "input-inventory.json"), {
      kind: "input_inventory",
      jobId: plan.jobId,
      inventory: plan.inputInventory
    });
    await writeJson(path.join(projectRoot, "src", "analysis", "ast-indexes.json"), {
      kind: "ast_index",
      jobId: plan.jobId,
      astIndexes: plan.astIndexes,
      detectedRuntime: plan.detectedRuntime
    });
    await writeJson(path.join(projectRoot, "src", "analysis", "source-map-analysis.json"), {
      kind: "source_map_analysis",
      jobId: plan.jobId,
      sourceMapAnalysis: plan.sourceMapAnalysis
    });
    await writeJson(path.join(projectRoot, "src", "analysis", "graph-analysis.json"), {
      kind: "graph_analysis",
      jobId: plan.jobId,
      graphAnalysis: plan.graphAnalysis
    });
    await writeJson(path.join(projectRoot, "src", "analysis", "runtime-wrappers.json"), {
      kind: "runtime_wrappers",
      jobId: plan.jobId,
      runtimeWrappers: plan.runtimeWrappers,
      warnings: plan.moduleRecoveryAnalysis.warnings
    });
    await writeJson(path.join(projectRoot, "src", "analysis", "module-boundaries.json"), {
      kind: "module_boundaries",
      jobId: plan.jobId,
      moduleBoundaries: plan.moduleBoundaries,
      warnings: plan.moduleRecoveryAnalysis.warnings
    });
    await writeJson(path.join(projectRoot, "src", "analysis", "import-export-candidates.json"), {
      kind: "import_export_candidates",
      jobId: plan.jobId,
      importExportCandidates: plan.importExportCandidates
    });
    await writeJson(path.join(projectRoot, "src", "analysis", "generated-modules.json"), {
      kind: "generated_modules",
      jobId: plan.jobId,
      generatedModules: plan.generatedModules
    });
    await writeJson(path.join(projectRoot, "src", "analysis", "dependency-placeholders.json"), {
      kind: "dependency_placeholders",
      jobId: plan.jobId,
      dependencyPlaceholders: plan.dependencyPlaceholders,
      contract: {
        continuity: "load_only",
        errorName: "MissingDependencyPlaceholderError",
        errorCode: "AI_JSUNPACK_MISSING_DEPENDENCY",
        semanticBehaviorAvailable: false
      }
    });
    await writeJson(path.join(projectRoot, "src", "analysis", "transform-log.json"), {
      kind: "transform_log",
      jobId: plan.jobId,
      transformLog: plan.transformLog
    });
    await writeJson(path.join(projectRoot, "src", "analysis", "rollback-map.json"), {
      kind: "rollback_map",
      jobId: plan.jobId,
      rollbackMap: plan.rollbackMap
    });
    await writeJson(path.join(projectRoot, "src", "reconstruction-manifest.json"), manifest);
    await fs.writeFile(path.join(projectRoot, "src", "main.ts"), mainTsSource(manifest), "utf8");
    await fs.writeFile(path.join(projectRoot, "src", "entrypoints", "reconstruction-entry.ts"), reconstructionEntrySource(plan, manifest), "utf8");
    await fs.writeFile(path.join(projectRoot, "src", "runtime-shims", "browser-globals.ts"), runtimeShimSource(), "utf8");
    await fs.writeFile(path.join(projectRoot, "src", "types", "reconstruction.d.ts"), reconstructionTypesSource(), "utf8");
    await fs.writeFile(path.join(projectRoot, "index.html"), indexHtmlSource(plan, manifest), "utf8");
    await fs.writeFile(path.join(projectRoot, "scripts", "build.mjs"), buildScriptSource(), "utf8");
    await fs.writeFile(path.join(projectRoot, "scripts", "typecheck.mjs"), typecheckScriptSource(), "utf8");

    if (agentFeedback) {
      const feedbackResult = await applyAgentFeedback(projectRoot, agentFeedback, plan);
      plan.agentFeedback = feedbackResult;
      manifest.agentFeedback = feedbackResult;
      await writeJson(path.join(projectRoot, "src", "reconstruction-manifest.json"), manifest);
      await fs.writeFile(path.join(projectRoot, "src", "main.ts"), mainTsSource(manifest), "utf8");
      await fs.writeFile(
        path.join(projectRoot, "src", "entrypoints", "reconstruction-entry.ts"),
        reconstructionEntrySource(plan, manifest),
        "utf8"
      );
    }

    return {
      projectPath: projectRoot,
      manifest
    };
  } finally {
    await normalized.cleanup();
  }
}

const SUPPORTED_AGENT_FEEDBACK_ACTIONS = new Set<AgentFeedbackActionName>([
  "add_package_script",
  "replace_package_script",
  "mirror_original_static_entry"
]);
const SAFE_PACKAGE_SCRIPTS: Record<string, string> = {
  build: "node scripts/build.mjs",
  typecheck: "node scripts/typecheck.mjs",
  check: "node scripts/typecheck.mjs"
};
const MIRROR_PROTECTED_ROOTS = new Set(["src", "scripts", "node_modules", ".git"]);
const MIRROR_PROTECTED_FILES = new Set([
  "package.json",
  "package-lock.json",
  "npm-shrinkwrap.json",
  "pnpm-lock.yaml",
  "yarn.lock",
  "tsconfig.json",
  "tsconfig.base.json",
  "vite.config.js",
  "vite.config.mjs",
  "vite.config.ts",
  "rollup.config.js",
  "webpack.config.js"
]);
const MIRROR_ALLOWED_EXTENSIONS = new Set([
  ".html",
  ".htm",
  ".js",
  ".mjs",
  ".cjs",
  ".css",
  ".json",
  ".webmanifest",
  ".map",
  ".wasm",
  ".txt",
  ".xml",
  ".svg",
  ".ico",
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".avif",
  ".woff",
  ".woff2",
  ".ttf",
  ".otf",
  ".eot",
  ".mp3",
  ".mp4",
  ".webm",
  ".ogg"
]);

export function validateAgentFeedbackInput(payload: unknown): AgentFeedbackInput {
  if (!isJsonObject(payload) || payload.kind !== "agent_feedback" || payload.protocolVersion !== 1) {
    throw new Error("Agent feedback 必须是 kind=agent_feedback 且 protocolVersion=1 的对象。");
  }
  if (!Array.isArray(payload.sourceReviewArtifactIds) || !payload.sourceReviewArtifactIds.every(isNonEmptyString)) {
    throw new Error("Agent feedback 的 sourceReviewArtifactIds 必须是字符串数组。");
  }
  if (!Array.isArray(payload.approvedActions) || !Array.isArray(payload.rejectedActions)) {
    throw new Error("Agent feedback 的 approvedActions 和 rejectedActions 必须是数组。");
  }

  const rejectedActions = payload.rejectedActions.map(validateFeedbackRejection);
  const approvedActions: AgentFeedbackAction[] = [];
  for (const item of payload.approvedActions) {
    if (!isJsonObject(item)) {
      throw new Error("每个已批准的 Agent feedback 操作都必须是对象。");
    }
    for (const field of ["sourceArtifactId", "action", "path", "value", "reason"] as const) {
      if (!isNonEmptyString(item[field])) {
        throw new Error(`已批准 Agent feedback 操作的字段 ${field} 必须是非空字符串。`);
      }
    }
    const sourceArtifactId = item.sourceArtifactId as string;
    const actionName = item.action as string;
    const actionPath = item.path as string;
    const actionValue = item.value as string;
    const actionReason = item.reason as string;
    if (item.repairInstructionId !== undefined && !isNonEmptyString(item.repairInstructionId)) {
      throw new Error("已批准 Agent feedback 中的 repairInstructionId 在提供时必须是非空字符串。");
    }
    if (!SUPPORTED_AGENT_FEEDBACK_ACTIONS.has(actionName as AgentFeedbackActionName)) {
      rejectedActions.push({
        sourceArtifactId,
        repairInstructionId: typeof item.repairInstructionId === "string" ? item.repairInstructionId : undefined,
        action: actionName,
        path: actionPath,
        reason: `不支持的 Agent feedback 操作：${actionName}。`
      });
      continue;
    }
    approvedActions.push({
      sourceArtifactId,
      repairInstructionId: typeof item.repairInstructionId === "string" ? item.repairInstructionId : undefined,
      action: actionName as AgentFeedbackActionName,
      path: actionPath,
      value: actionValue,
      reason: actionReason
    });
  }

  return {
    kind: "agent_feedback",
    protocolVersion: 1,
    sourceReviewArtifactIds: [...new Set(payload.sourceReviewArtifactIds)],
    approvedActions,
    rejectedActions
  };
}

async function applyAgentFeedback(
  projectRoot: string,
  feedback: AgentFeedbackInput,
  plan: ReconstructionPlan
): Promise<AgentFeedbackResult> {
  const appliedActions: AppliedAgentFeedbackAction[] = [];
  const rejectedActions = [...feedback.rejectedActions];
  const conflicts = conflictingFeedbackActionIndexes(feedback.approvedActions);
  const seen = new Set<string>();

  for (let index = 0; index < feedback.approvedActions.length; index += 1) {
    const action = feedback.approvedActions[index];
    if (conflicts.has(index)) {
      rejectedActions.push(rejectedFeedbackAction(action, "冲突的已批准操作指向同一个生成项目字段。"));
      continue;
    }
    const identity = `${action.action}\u0000${action.path}\u0000${action.value}`;
    if (seen.has(identity)) {
      rejectedActions.push(rejectedFeedbackAction(action, "重复的已批准操作不会重复应用。"));
      continue;
    }
    seen.add(identity);

    if (action.action === "mirror_original_static_entry") {
      if (action.path !== "projectRoot" || action.value !== "public/original") {
        rejectedActions.push(rejectedFeedbackAction(action, "静态入口镜像要求 projectRoot <- public/original。"));
        continue;
      }
      const mirrorResult = await mirrorOriginalStaticEntry(projectRoot, staticMirrorCandidates(plan));
      if (mirrorResult.copied < 1) {
        rejectedActions.push(rejectedFeedbackAction(action, "没有可安全镜像的原始静态文件。"));
        continue;
      }
      appliedActions.push({
        ...action,
        changed: true,
        detail: `已镜像 ${mirrorResult.copied} 个安全静态文件；跳过 ${mirrorResult.skipped} 个。`
      });
      continue;
    }

    const scriptName = packageScriptName(action.path);
    if (!scriptName || SAFE_PACKAGE_SCRIPTS[scriptName] !== action.value) {
      rejectedActions.push(
        rejectedFeedbackAction(action, "包脚本操作需要受支持的脚本名称及其匹配的确定性垫片值。")
      );
      continue;
    }
    const packagePath = path.join(projectRoot, "package.json");
    const packagePayload = JSON.parse(await fs.readFile(packagePath, "utf8")) as unknown;
    if (!isJsonObject(packagePayload)) {
      throw new Error("生成的 package.json 不是 JSON 对象。");
    }
    const scripts = isJsonObject(packagePayload.scripts) ? { ...packagePayload.scripts } : {};
    const existing = scripts[scriptName];
    if (action.action === "add_package_script" && existing !== undefined) {
      rejectedActions.push(rejectedFeedbackAction(action, `包脚本 ${scriptName} 已存在。`));
      continue;
    }
    if (action.action === "replace_package_script" && typeof existing !== "string") {
      rejectedActions.push(rejectedFeedbackAction(action, `包脚本 ${scriptName} 不存在。`));
      continue;
    }
    scripts[scriptName] = action.value;
    packagePayload.scripts = scripts;
    await writeJson(packagePath, packagePayload);
    appliedActions.push({
      ...action,
      changed: existing !== action.value,
      detail: `${action.action === "add_package_script" ? "已添加" : "已替换"}包脚本 ${scriptName}。`
    });
  }

  return {
    sourceReviewArtifactIds: feedback.sourceReviewArtifactIds,
    approvedActions: feedback.approvedActions,
    appliedActions,
    rejectedActions
  };
}

function conflictingFeedbackActionIndexes(actions: AgentFeedbackAction[]): Set<number> {
  const byTarget = new Map<string, Array<{ index: number; signature: string }>>();
  actions.forEach((action, index) => {
    const target = action.action === "mirror_original_static_entry" ? "mirror:projectRoot" : `script:${action.path}`;
    const records = byTarget.get(target) ?? [];
    records.push({ index, signature: `${action.action}\u0000${action.value}` });
    byTarget.set(target, records);
  });
  const conflicts = new Set<number>();
  for (const records of byTarget.values()) {
    if (new Set(records.map((record) => record.signature)).size > 1) {
      records.forEach((record) => conflicts.add(record.index));
    }
  }
  return conflicts;
}

function packageScriptName(actionPath: string): string | null {
  const match = /^package\.json:scripts\.([A-Za-z0-9:_-]+)$/.exec(actionPath);
  return match?.[1] ?? null;
}

function staticMirrorCandidates(plan: ReconstructionPlan): Set<string> {
  return new Set(
    [
      ...plan.inputInventory.entries,
      ...plan.inputInventory.scripts,
      ...plan.inputInventory.styles,
      ...plan.inputInventory.assets,
      ...plan.inputInventory.manifests
    ].map((item) => toPosix(item))
  );
}

async function mirrorOriginalStaticEntry(
  projectRoot: string,
  allowedRelativePaths: Set<string>
): Promise<{ copied: number; skipped: number }> {
  const sourceRoot = path.join(projectRoot, "public", "original");
  const files = await listFiles(sourceRoot);
  let copied = 0;
  let skipped = 0;
  for (const sourceFile of files) {
    const relative = toPosix(path.relative(sourceRoot, sourceFile));
    const parts = relative.split("/");
    const lowerRelative = relative.toLowerCase();
    const basename = parts.at(-1)?.toLowerCase() ?? "";
    const extension = path.extname(basename);
    if (
      !relative ||
      relative.startsWith("../") ||
      !allowedRelativePaths.has(relative) ||
      parts.some((part) => part.startsWith(".")) ||
      MIRROR_PROTECTED_ROOTS.has(parts[0].toLowerCase()) ||
      MIRROR_PROTECTED_FILES.has(lowerRelative) ||
      basename.includes(".config.") ||
      basename.endsWith("rc") ||
      !MIRROR_ALLOWED_EXTENSIONS.has(extension)
    ) {
      skipped += 1;
      continue;
    }
    const stat = await fs.lstat(sourceFile);
    if (stat.isSymbolicLink()) {
      skipped += 1;
      continue;
    }
    const target = path.join(projectRoot, safeRelativePath(relative));
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.copyFile(sourceFile, target);
    copied += 1;
  }
  return { copied, skipped };
}

function validateFeedbackRejection(value: unknown): AgentFeedbackRejection {
  if (!isJsonObject(value) || !isNonEmptyString(value.reason)) {
    throw new Error("每个被拒绝的 Agent feedback 操作都必须包含非空原因。");
  }
  const result: AgentFeedbackRejection = { reason: value.reason };
  for (const field of ["sourceArtifactId", "repairInstructionId", "action", "path"] as const) {
    if (value[field] !== undefined) {
      if (!isNonEmptyString(value[field])) {
        throw new Error(`被拒绝 Agent feedback 的字段 ${field} 在提供时必须是非空字符串。`);
      }
      result[field] = value[field];
    }
  }
  return result;
}

function rejectedFeedbackAction(action: AgentFeedbackAction, reason: string): AgentFeedbackRejection {
  return {
    sourceArtifactId: action.sourceArtifactId,
    repairInstructionId: action.repairInstructionId,
    action: action.action,
    path: action.path,
    reason
  };
}

function isJsonObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

async function analyzeSourceMaps(rootDir: string, inventory: InputInventory): Promise<SourceMapArtifactAnalysis> {
  const bundleAnalyses: SourceMapBundleAnalysis[] = [];
  const sourceCandidates = new Set<string>();
  const warnings: string[] = [];
  const fileMap = new Map(inventory.files.map((file) => [file.path, file]));

  for (const sourceMapPath of inventory.sourceMaps) {
    const sourceMapAbsolutePath = path.join(rootDir, sourceMapPath);
    let mapPayload: unknown;
    try {
      mapPayload = JSON.parse(await fs.readFile(sourceMapAbsolutePath, "utf8")) as unknown;
    } catch (error) {
      warnings.push(`解析 source map ${sourceMapPath} 失败：${error instanceof Error ? error.message : "未知错误"}`);
      continue;
    }

    try {
      const traceMap = new TraceMap(mapPayload as SourceMapInput, sourceMapPath);
      const bundlePath = inferBundlePathForSourceMap(sourceMapPath, traceMap.file, fileMap);
      const rawSources = traceMap.sources.filter((source): source is string => typeof source === "string" && source.trim().length > 0);
      const sources = normalizeSourceMapSources(rawSources);
      const sourceRoot = normalizeSourceMapRoot(traceMap.sourceRoot);
      const sourceCandidatesForBundle = resolveSourceCandidates(bundlePath, sourceMapPath, sources, sourceRoot);
      const availableSourcesContent: string[] = [];
      const missingSourcesContent: string[] = [];
      const recoveredSources: SourceMapRecoveredSource[] = [];

      rawSources.forEach((source, index) => {
        const content = sourceContentFor(traceMap, source);
        const normalizedSource = normalizeSourceMapCandidate(source) ?? source;
        if (content === null) {
          missingSourcesContent.push(normalizedSource);
          return;
        }
        availableSourcesContent.push(normalizedSource);
        const resolvedCandidate = sourceCandidatesForBundle.find((candidate) => candidate.endsWith(normalizedSource.replace(/^\.\.\//, "")))
          ?? sourceCandidatesForBundle[index]
          ?? normalizedSource;
        recoveredSources.push({
          source: normalizedSource,
          candidatePath: sanitizeGeneratedModulePath(resolvedCandidate),
          contentHash: sha256(Buffer.from(content)),
          content
        });
      });

      for (const candidate of sourceCandidatesForBundle) {
        sourceCandidates.add(candidate);
      }

      const bundleWarnings: string[] = [];
      if (sourceCandidatesForBundle.length === 0) {
        bundleWarnings.push(`Source map ${sourceMapPath} 未产生任何源候选。`);
      }
      if (missingSourcesContent.length > 0) {
        bundleWarnings.push(`Source map ${sourceMapPath} 缺少 ${missingSourcesContent.length} 个源的 sourcesContent。`);
      }

      bundleAnalyses.push({
        bundlePath,
        sourceMapPath,
        sourceMapFile: normalizeSourceMapFile(traceMap.file),
        sourceRoot,
        sources,
        recoveredSources,
        sourceCandidates: sourceCandidatesForBundle,
        sourcesContentAvailable: availableSourcesContent,
        missingSourcesContent,
        warnings: bundleWarnings
      });

      warnings.push(...bundleWarnings.map((warning) => `${sourceMapPath}: ${warning}`));
    } catch (error) {
      warnings.push(`分析 source map ${sourceMapPath} 失败：${error instanceof Error ? error.message : "未知错误"}`);
    }
  }

  return {
    bundleCount: bundleAnalyses.length,
    bundleAnalyses,
    sourceCandidates: [...sourceCandidates].sort(),
    warnings
  };
}

function inferBundlePathForSourceMap(
  sourceMapPath: string,
  sourceMapFile: string | null | undefined,
  fileMap: Map<string, InputFileRecord>
): string {
  const sourceMapDir = path.posix.dirname(sourceMapPath);
  const base = sourceMapPath.endsWith(".map") ? sourceMapPath.slice(0, -4) : sourceMapPath;
  const candidates = [sourceMapFile, base, base.replace(/\.min$/i, ""), base.replace(/\.bundle$/i, ""), base.replace(/\.js$/i, ".js")];
  for (const candidate of candidates) {
    const normalized = normalizeSourceMapCandidate(candidate);
    if (!normalized) {
      continue;
    }
    for (const resolved of new Set([normalized, path.posix.normalize(path.posix.join(sourceMapDir, normalized))])) {
      if (fileMap.has(resolved)) {
        return resolved;
      }
    }
  }
  return normalizeSourceMapCandidate(sourceMapFile) ?? base;
}

function resolveSourceCandidates(
  bundlePath: string,
  sourceMapPath: string,
  sources: string[],
  sourceRoot: string | null
): string[] {
  const bundleDir = path.posix.dirname(bundlePath);
  const sourceMapDir = path.posix.dirname(sourceMapPath);
  const resolved = new Set<string>();

  for (const rawSource of sources) {
    const normalizedSource = normalizeSourceMapCandidate(rawSource);
    if (!normalizedSource) {
      continue;
    }
    const candidates = new Set<string>();
    candidates.add(normalizeSourceMapCandidate(path.posix.normalize(path.posix.join(sourceMapDir, normalizedSource))) ?? normalizedSource);
    candidates.add(normalizeSourceMapCandidate(path.posix.normalize(path.posix.join(bundleDir, normalizedSource))) ?? normalizedSource);
    if (sourceRoot) {
      candidates.add(normalizeSourceMapCandidate(path.posix.normalize(path.posix.join(sourceRoot, normalizedSource))) ?? normalizedSource);
    }
    candidates.add(normalizedSource);

    for (const candidate of candidates) {
      if (candidate) {
        resolved.add(candidate);
      }
    }
  }

  return [...resolved].sort();
}

function normalizeSourceMapSources(sources: string[]): string[] {
  return sources
    .map((source) => normalizeSourceMapCandidate(source))
    .filter((source): source is string => Boolean(source));
}

function normalizeSourceMapRoot(sourceRoot: string | undefined): string | null {
  const normalized = normalizeSourceMapCandidate(sourceRoot);
  return normalized ?? null;
}

function normalizeSourceMapFile(file: string | null | undefined): string | null {
  const normalized = normalizeSourceMapCandidate(file);
  return normalized ?? null;
}

function normalizeSourceMapCandidate(candidate: string | null | undefined): string | null {
  if (!candidate) {
    return null;
  }
  const trimmed = candidate.trim();
  if (!trimmed) {
    return null;
  }
  const normalizedSlashes = trimmed.replace(/\\/g, "/");
  const schemeMatch = normalizedSlashes.match(/^([a-zA-Z][a-zA-Z0-9+.-]*):\/+(.+)$/);
  let candidatePath = normalizedSlashes;
  if (schemeMatch) {
    const scheme = schemeMatch[1].toLowerCase();
    if (!LOCAL_SOURCE_SCHEMES.has(scheme)) {
      return null;
    }
    candidatePath = schemeMatch[2];
  }
  return candidatePath.replace(/^\.\//, "").replace(/^\/+/, "");
}

async function writeJson(filePath: string, value: unknown): Promise<void> {
  await fs.writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

async function writeGeneratedModules(projectRoot: string, plan: ReconstructionPlan): Promise<string[]> {
  const writtenFiles: string[] = [];
  const recoveredContentByPath = new Map<string, SourceMapRecoveredSource>();
  for (const bundleAnalysis of plan.sourceMapAnalysis.bundleAnalyses) {
    for (const recoveredSource of bundleAnalysis.recoveredSources) {
      recoveredContentByPath.set(recoveredSource.candidatePath, recoveredSource);
    }
  }

  for (const generatedModule of plan.generatedModules) {
    const safePath = safeGeneratedProjectRelativePath(generatedModule.modulePath);
    const targetPath = path.join(projectRoot, safePath);
    await fs.mkdir(path.dirname(targetPath), { recursive: true });
    const recoveredSource = generatedModule.sourceFilePath ? recoveredContentByPath.get(generatedModule.sourceFilePath) : undefined;
    const sourceText = recoveredSource
      ? recoveredModuleSource(generatedModule, recoveredSource)
      : metadataModuleSource(generatedModule, plan);
    await fs.writeFile(targetPath, sourceText, "utf8");
    writtenFiles.push(toPosix(safePath));
  }

  const indexPath = "src/modules/module-index.ts";
  await fs.writeFile(path.join(projectRoot, indexPath), moduleIndexSource(plan.generatedModules), "utf8");
  writtenFiles.push(indexPath);
  return [...new Set(writtenFiles)].sort();
}

function dependencyPlaceholderProjectPaths(records: DependencyPlaceholderRecord[]): string[] {
  const paths = new Set<string>();
  for (const record of records) {
    if (record.status !== "generated" || !record.resolvedPath) {
      continue;
    }
    paths.add(`public/original/${record.resolvedPath}`);
    if (dependencyPlaceholderRootPathAllowed(record.resolvedPath)) {
      paths.add(record.resolvedPath);
    }
  }
  return [...paths].sort();
}

async function writeDependencyPlaceholders(
  projectRoot: string,
  records: DependencyPlaceholderRecord[]
): Promise<string[]> {
  const recordsByPath = new Map<string, DependencyPlaceholderRecord[]>();
  for (const record of records) {
    if (record.status !== "generated" || !record.resolvedPath) {
      continue;
    }
    const grouped = recordsByPath.get(record.resolvedPath) ?? [];
    grouped.push(record);
    recordsByPath.set(record.resolvedPath, grouped);
  }

  const writtenFiles: string[] = [];
  for (const [resolvedPath, groupedRecords] of recordsByPath) {
    const source = dependencyPlaceholderSource(resolvedPath, groupedRecords);
    const candidatePaths = [`public/original/${resolvedPath}`];
    if (dependencyPlaceholderRootPathAllowed(resolvedPath)) {
      candidatePaths.push(resolvedPath);
    }
    for (const candidatePath of candidatePaths) {
      const safePath = safeRelativePath(candidatePath);
      const targetPath = path.join(projectRoot, safePath);
      try {
        await fs.lstat(targetPath);
        continue;
      } catch (error) {
        if (!(error instanceof Error) || !("code" in error) || error.code !== "ENOENT") {
          throw error;
        }
      }
      await fs.mkdir(path.dirname(targetPath), { recursive: true });
      await fs.writeFile(targetPath, source, "utf8");
      writtenFiles.push(toPosix(safePath));
    }
  }
  return [...new Set(writtenFiles)].sort();
}

function dependencyPlaceholderRootPathAllowed(resolvedPath: string): boolean {
  const normalized = toPosix(resolvedPath);
  const firstSegment = normalized.split("/")[0]?.toLowerCase() ?? "";
  if (["src", "scripts", "public", "dist", "node_modules"].includes(firstSegment)) {
    return false;
  }
  return !new Set(["index.html", "package.json", "tsconfig.json"]).has(normalized.toLowerCase());
}

function dependencyPlaceholderSource(
  resolvedPath: string,
  records: DependencyPlaceholderRecord[]
): string {
  const importedNames = [...new Set(records.flatMap((record) => record.importedNames))]
    .filter((name) => name !== "default")
    .sort();
  const hasDefault = records.some((record) => record.defaultImport || record.importedNames.includes("default"));
  const metadata = {
    code: "AI_JSUNPACK_MISSING_DEPENDENCY",
    resolvedPath,
    importers: [...new Set(records.map((record) => record.importerPath))].sort(),
    specifiers: [...new Set(records.map((record) => record.specifier))].sort(),
    importedNames,
    defaultImport: hasDefault,
    namespaceImport: records.some((record) => record.namespaceImport),
    sideEffectOnly: records.some((record) => record.sideEffectOnly),
    exportAll: records.some((record) => record.exportAll),
    semanticBehaviorAvailable: false
  };
  const lines = [
    "// 由 AI JS Unpack 生成，因为缺少必需的静态相对 ESM 依赖。",
    `const __missingDependencyMetadata = ${JSON.stringify(metadata, null, 2)};`,
    "globalThis.console?.warn?.(\"[AI JS Unpack] 已加载缺失依赖占位模块。\", __missingDependencyMetadata);",
    "class MissingDependencyPlaceholderError extends Error {",
    "  constructor(exportName) {",
    "    super(`调用了来自 ${__missingDependencyMetadata.resolvedPath} 的缺失依赖占位导出 ${exportName}；语义行为不可用。`);",
    "    this.name = \"MissingDependencyPlaceholderError\";",
    "    this.code = \"AI_JSUNPACK_MISSING_DEPENDENCY\";",
    "    this.dependencyPath = __missingDependencyMetadata.resolvedPath;",
    "    this.exportName = exportName;",
    "    this.importers = [...__missingDependencyMetadata.importers];",
    "  }",
    "}",
    "function __throwMissingDependency(exportName) {",
    "  throw new MissingDependencyPlaceholderError(exportName);",
    "}"
  ];
  importedNames.forEach((name, index) => {
    const localName = `__missingDependencyExport${index}`;
    lines.push(`const ${localName} = (..._args) => __throwMissingDependency(${JSON.stringify(name)});`);
    lines.push(`export { ${localName} as ${dependencyPlaceholderExportName(name)} };`);
  });
  if (hasDefault) {
    lines.push("const __missingDependencyDefault = (..._args) => __throwMissingDependency(\"default\");");
    lines.push("export default __missingDependencyDefault;");
  }
  if (importedNames.length === 0 && !hasDefault) {
    lines.push("export {};");
  }
  lines.push("");
  return lines.join("\n");
}

function dependencyPlaceholderExportName(name: string): string {
  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name) ? name : JSON.stringify(name);
}

function recoveredModuleSource(generatedModule: GeneratedModuleRecord, recoveredSource: SourceMapRecoveredSource): string {
  return `// 已从 source map 的 sourcesContent 恢复。
// 来源：${recoveredSource.source}
// 哈希：${recoveredSource.contentHash}

${recoveredSource.content.trimEnd()}

export const __reconstructionModuleMeta = ${JSON.stringify(generatedModule, null, 2)} as const;
`;
}

function metadataModuleSource(generatedModule: GeneratedModuleRecord, plan: ReconstructionPlan): string {
  const boundary = generatedModule.boundaryId ? plan.moduleBoundaries.find((candidate) => candidate.id === generatedModule.boundaryId) : undefined;
  return `import type { GeneratedModuleMeta, ModuleBoundaryMeta } from "../types/reconstruction";

export const moduleMeta: GeneratedModuleMeta = ${JSON.stringify(generatedModule, null, 2)};

export const boundaryMeta: ModuleBoundaryMeta | null = ${JSON.stringify(boundary ?? null, null, 2)};

export function describeModule(): string {
  return moduleMeta.sourceFilePath ?? moduleMeta.modulePath;
}
`;
}

function moduleIndexSource(generatedModules: GeneratedModuleRecord[]): string {
  return `import type { GeneratedModuleMeta } from "../types/reconstruction";

export const generatedModules: GeneratedModuleMeta[] = ${JSON.stringify(generatedModules, null, 2)};

export function generatedModuleCount(): number {
  return generatedModules.length;
}
`;
}

function safeGeneratedProjectRelativePath(filePath: string): string {
  const normalized = filePath.replace(/\\/g, "/");
  if (!normalized.startsWith("src/") || normalized.includes("\0") || normalized.split("/").some((part) => part === "..")) {
    throw new Error(`不安全的生成项目文件路径：${filePath}`);
  }
  return normalized;
}

async function listFiles(rootDir: string): Promise<string[]> {
  const entries = await fs.readdir(rootDir, { withFileTypes: true });
  const files: string[] = [];
  for (const entry of entries) {
    const absolutePath = path.join(rootDir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules" || entry.name === ".git") {
        continue;
      }
      files.push(...await listFiles(absolutePath));
    } else if (entry.isFile()) {
      files.push(absolutePath);
    }
  }
  return files;
}

function classifyPath(filePath: string): InputFileRecord["kind"] {
  const normalized = filePath.toLowerCase();
  const extension = path.extname(normalized);
  if (extension === ".html" || extension === ".htm") return "html";
  if ([".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"].includes(extension)) return "script";
  if (extension === ".css") return "style";
  if (extension === ".map") return "source_map";
  if (extension === ".json" && (normalized.includes("manifest") || normalized.endsWith("package.json"))) return "manifest";
  if (TEXT_EXTENSIONS.has(extension)) return "unknown";
  return "asset";
}

function collectHtmlReferenceKinds(
  htmlSources: HtmlSourceRecord[],
  knownPaths: Set<string>
): { referenceKindsByPath: Map<string, HtmlReferenceKind>; missingReferences: string[] } {
  const referenceKinds = new Map<string, { kind: HtmlReferenceKind; rank: number }>();
  const missingReferences = new Set<string>();

  for (const htmlSource of htmlSources) {
    for (const reference of extractHtmlReferenceCandidates(htmlSource.content, htmlSource.path)) {
      if (!knownPaths.has(reference.path)) {
        missingReferences.add(`${reference.htmlPath} -> ${reference.kind}:${reference.path}`);
        continue;
      }
      const current = referenceKinds.get(reference.path);
      const rank = HTML_REFERENCE_PRIORITY[reference.kind];
      if (!current || rank > current.rank) {
        referenceKinds.set(reference.path, { kind: reference.kind, rank });
      }
    }
  }

  return {
    referenceKindsByPath: new Map([...referenceKinds.entries()].map(([path, value]) => [path, value.kind])),
    missingReferences: [...missingReferences].sort()
  };
}

function extractHtmlReferenceCandidates(html: string, htmlPath: string): HtmlReferenceRecord[] {
  const references: HtmlReferenceRecord[] = [];
  const seen = new Set<string>();
  let cursor = 0;

  while (cursor < html.length) {
    const openIndex = html.indexOf("<", cursor);
    if (openIndex === -1) {
      break;
    }
    if (html.startsWith("<!--", openIndex)) {
      const commentEnd = html.indexOf("-->", openIndex + 4);
      cursor = commentEnd === -1 ? html.length : commentEnd + 3;
      continue;
    }
    if (html.startsWith("</", openIndex) || html.startsWith("<!", openIndex) || html.startsWith("<?", openIndex)) {
      const tagEnd = findHtmlTagEnd(html, openIndex + 1);
      cursor = tagEnd === -1 ? html.length : tagEnd + 1;
      continue;
    }

    const tagEnd = findHtmlTagEnd(html, openIndex + 1);
    if (tagEnd === -1) {
      break;
    }

    const rawTag = html.slice(openIndex + 1, tagEnd).trim();
    cursor = tagEnd + 1;
    if (!rawTag) {
      continue;
    }

    const spaceIndex = rawTag.search(/\s/);
    const tagName = (spaceIndex === -1 ? rawTag : rawTag.slice(0, spaceIndex)).replace(/\/$/, "").toLowerCase();
    if (!tagName) {
      continue;
    }

    const attrSource = spaceIndex === -1 ? "" : rawTag.slice(spaceIndex).replace(/\/\s*$/, "");
    const attributes = parseHtmlAttributes(attrSource);
    collectTagReferences(tagName, attributes, htmlPath, references, seen);

    if (HTML_RAW_TEXT_TAGS.has(tagName) && !rawTag.endsWith("/")) {
      const closeIndex = html.toLowerCase().indexOf(`</${tagName}`, cursor);
      if (closeIndex === -1) {
        break;
      }
      const closeTagEnd = findHtmlTagEnd(html, closeIndex + 2);
      cursor = closeTagEnd === -1 ? html.length : closeTagEnd + 1;
    }
  }

  return references;
}

function collectTagReferences(
  tagName: string,
  attributes: Record<string, string>,
  htmlPath: string,
  references: HtmlReferenceRecord[],
  seen: Set<string>
): void {
  if (tagName === "script") {
    addHtmlReference(attributes.src, "script", htmlPath, tagName, "src", references, seen);
    return;
  }

  if (tagName === "link") {
    const relTokens = splitTokenList(attributes.rel);
    if (relTokens.has("stylesheet")) {
      addHtmlReference(attributes.href, "style", htmlPath, tagName, "href", references, seen);
    }
    if (relTokens.has("modulepreload")) {
      addHtmlReference(attributes.href, "script", htmlPath, tagName, "href", references, seen);
    }
    if (relTokens.has("manifest")) {
      addHtmlReference(attributes.href, "manifest", htmlPath, tagName, "href", references, seen);
    }
    if (relTokens.has("preload")) {
      addHtmlReference(attributes.href, kindForPreloadAs(attributes.as), htmlPath, tagName, "href", references, seen);
    }
    if (relTokens.has("icon") || relTokens.has("apple-touch-icon") || relTokens.has("mask-icon") || relTokens.has("shortcut")) {
      addHtmlReference(attributes.href, "asset", htmlPath, tagName, "href", references, seen);
    }
    return;
  }

  if (tagName === "img" || tagName === "audio" || tagName === "embed" || tagName === "track") {
    addHtmlReference(attributes.src, "asset", htmlPath, tagName, "src", references, seen);
    if (tagName === "img") {
      addSrcsetReferences(attributes.srcset, "asset", htmlPath, tagName, "srcset", references, seen);
    }
    return;
  }

  if (tagName === "source") {
    addHtmlReference(attributes.src, "asset", htmlPath, tagName, "src", references, seen);
    addSrcsetReferences(attributes.srcset, "asset", htmlPath, tagName, "srcset", references, seen);
    return;
  }

  if (tagName === "video") {
    addHtmlReference(attributes.src, "asset", htmlPath, tagName, "src", references, seen);
    addHtmlReference(attributes.poster, "asset", htmlPath, tagName, "poster", references, seen);
    return;
  }

  if (tagName === "object") {
    addHtmlReference(attributes.data, "asset", htmlPath, tagName, "data", references, seen);
    return;
  }

  if (tagName === "input" && (attributes.type ?? "").toLowerCase() === "image") {
    addHtmlReference(attributes.src, "asset", htmlPath, tagName, "src", references, seen);
    return;
  }

  if (tagName === "image") {
    addHtmlReference(attributes.href ?? attributes["xlink:href"], "asset", htmlPath, tagName, "href", references, seen);
  }
}

function addHtmlReference(
  rawValue: string | undefined,
  kind: HtmlReferenceKind,
  htmlPath: string,
  tagName: string,
  attributeName: string,
  references: HtmlReferenceRecord[],
  seen: Set<string>
): void {
  const resolvedPath = normalizeHtmlReferencePath(rawValue, htmlPath);
  if (!resolvedPath) {
    return;
  }
  const key = `${kind}:${resolvedPath}`;
  if (seen.has(key)) {
    return;
  }
  seen.add(key);
  references.push({
    path: resolvedPath,
    kind,
    htmlPath,
    tagName,
    attributeName
  });
}

function addSrcsetReferences(
  rawValue: string | undefined,
  kind: HtmlReferenceKind,
  htmlPath: string,
  tagName: string,
  attributeName: string,
  references: HtmlReferenceRecord[],
  seen: Set<string>
): void {
  if (!rawValue) {
    return;
  }
  for (const candidate of splitSrcsetList(rawValue)) {
    addHtmlReference(candidate, kind, htmlPath, tagName, attributeName, references, seen);
  }
}

function splitSrcsetList(rawValue: string): string[] {
  return rawValue
    .split(",")
    .map((part) => part.trim().split(/\s+/)[0] ?? "")
    .map((candidate) => candidate.trim())
    .filter(Boolean);
}

function splitTokenList(rawValue: string | undefined): Set<string> {
  return new Set(
    (rawValue ?? "")
      .toLowerCase()
      .split(/\s+/)
      .map((token) => token.trim())
      .filter(Boolean)
  );
}

function kindForPreloadAs(rawValue: string | undefined): HtmlReferenceKind {
  switch ((rawValue ?? "").toLowerCase()) {
    case "script":
    case "worker":
    case "serviceworker":
    case "sharedworker":
    case "module":
      return "script";
    case "style":
      return "style";
    case "manifest":
      return "manifest";
    default:
      return "asset";
  }
}

function normalizeHtmlReferencePath(rawValue: string | undefined, htmlPath: string): string | null {
  if (!rawValue) {
    return null;
  }
  const trimmed = rawValue.trim();
  if (!trimmed || trimmed.startsWith("#")) {
    return null;
  }

  const sanitized = trimmed.replace(/\\/g, "/").split(/[?#]/, 1)[0];
  if (!sanitized) {
    return null;
  }
  if (sanitized.startsWith("//") || /^[A-Za-z][A-Za-z\d+\-.]*:/.test(sanitized)) {
    return null;
  }

  const resolved = sanitized.startsWith("/")
    ? path.posix.normalize(sanitized.slice(1))
    : path.posix.normalize(path.posix.join(path.posix.dirname(htmlPath), sanitized));

  if (!resolved || resolved === "." || resolved.startsWith("../")) {
    return null;
  }

  return resolved.startsWith("./") ? resolved.slice(2) : resolved;
}

function findHtmlTagEnd(html: string, startIndex: number): number {
  let quote: "'" | '"' | null = null;
  for (let index = startIndex; index < html.length; index += 1) {
    const char = html[index];
    if (quote) {
      if (char === quote) {
        quote = null;
      }
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      continue;
    }
    if (char === ">") {
      return index;
    }
  }
  return -1;
}

function parseHtmlAttributes(source: string): Record<string, string> {
  const attributes: Record<string, string> = {};
  const pattern = /([^\s=/>`]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(source)) !== null) {
    const name = match[1]?.toLowerCase();
    if (!name || name === "/") {
      continue;
    }
    attributes[name] = match[2] ?? match[3] ?? match[4] ?? "";
  }

  return attributes;
}

function formatMissingHtmlReferenceWarning(missingReferences: string[]): string {
  const preview = missingReferences.slice(0, 5).join("; ");
  const extra = missingReferences.length > 5 ? `；另有 ${missingReferences.length - 5} 个` : "";
  return `HTML 引用了 ${missingReferences.length} 个缺失文件：${preview}${extra}。`;
}

function normalizeInventoryFileKind(file: InputFileRecord, referenceKind?: HtmlReferenceKind): InputFileRecord {
  if (
    file.kind === "html" ||
    file.kind === "script" ||
    file.kind === "style" ||
    file.kind === "source_map" ||
    file.kind === "manifest"
  ) {
    return file;
  }
  return referenceKind ? { ...file, kind: referenceKind } : file;
}

function upsertSymbol(
  symbols: Map<string, { kind: string; loc?: string; references: number }>,
  name: string,
  kind: string,
  loc?: string
): void {
  const current = symbols.get(name);
  if (current) {
    current.references += 1;
    return;
  }
  symbols.set(name, { kind, loc, references: 0 });
}

function formatLoc(path: NodePath): string | undefined {
  const loc = path.node.loc?.start;
  return loc ? `${loc.line}:${loc.column}` : undefined;
}

function formatNodeLoc(node: t.Node): string | undefined {
  const loc = node.loc?.start;
  return loc ? `${loc.line}:${loc.column}` : undefined;
}

function sha256(buffer: Buffer): string {
  return createHash("sha256").update(buffer).digest("hex");
}

function archiveKindForPath(filePath: string): ArchiveInputKind | null {
  const lower = filePath.toLowerCase();
  if (lower.endsWith(".zip")) return "zip";
  if (lower.endsWith(".tar.gz") || lower.endsWith(".tgz")) return "tar_gz";
  if (lower.endsWith(".tar")) return "tar";
  return null;
}

function isSupportedSingleScriptPath(filePath: string): boolean {
  return [".js", ".mjs", ".cjs"].includes(path.extname(filePath).toLowerCase());
}

function safeSingleScriptFilename(filePath: string): string {
  const filename = path.basename(filePath);
  if (safeArchiveRelativePath(filename) !== filename || !isSupportedSingleScriptPath(filename)) {
    throw new Error(`不安全的单 JavaScript 输入文件名：${filename}`);
  }
  return filename;
}

function singleScriptHostHtml(scriptName: string, scriptSource: string): string {
  const scriptType = singleScriptUsesModuleSyntax(scriptName, scriptSource) ? ' type="module"' : "";
  return `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>AI JS Unpack 单脚本宿主页</title>
  </head>
  <body>
    <script${scriptType} src="./${escapeHtmlAttribute(scriptName)}"></script>
  </body>
</html>
`;
}

function singleScriptUsesModuleSyntax(scriptName: string, scriptSource: string): boolean {
  const extension = path.extname(scriptName).toLowerCase();
  if (extension === ".mjs") {
    return true;
  }
  if (extension === ".cjs") {
    return false;
  }
  try {
    const ast = parse(scriptSource, {
      sourceType: "unambiguous",
      plugins: ["jsx", "typescript", "dynamicImport", "classProperties", "optionalChaining", "nullishCoalescingOperator"],
      errorRecovery: true
    });
    return ast.program.sourceType === "module";
  } catch {
    return false;
  }
}

function escapeHtmlAttribute(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function extractZipArchive(archive: Buffer, rootDir: string): Promise<void> {
  const eocdOffset = findZipEndOfCentralDirectory(archive);
  const entryCount = archive.readUInt16LE(eocdOffset + 10);
  const centralDirectorySize = archive.readUInt32LE(eocdOffset + 12);
  const centralDirectoryOffset = archive.readUInt32LE(eocdOffset + 16);

  if (entryCount === 0xffff || centralDirectorySize === 0xffffffff || centralDirectoryOffset === 0xffffffff) {
    throw new Error("不支持 zip64 归档。");
  }
  if (entryCount > ARCHIVE_MAX_ENTRIES) {
    throw new Error(`归档资源限制已超出：zip 包含 ${entryCount} 个条目（最多 ${ARCHIVE_MAX_ENTRIES} 个）。`);
  }
  if (centralDirectoryOffset + centralDirectorySize > archive.length) {
    throw new Error("无效的 zip 归档：中央目录位于归档之外。");
  }

  let cursor = centralDirectoryOffset;
  let totalUncompressedBytes = 0;
  for (let index = 0; index < entryCount; index += 1) {
    if (cursor + 46 > archive.length || archive.readUInt32LE(cursor) !== 0x02014b50) {
      throw new Error("无效的 zip 归档：中央目录条目格式错误。");
    }

    const generalPurposeFlag = archive.readUInt16LE(cursor + 8);
    const compressionMethod = archive.readUInt16LE(cursor + 10);
    const compressedSize = archive.readUInt32LE(cursor + 20);
    const uncompressedSize = archive.readUInt32LE(cursor + 24);
    const fileNameLength = archive.readUInt16LE(cursor + 28);
    const extraFieldLength = archive.readUInt16LE(cursor + 30);
    const fileCommentLength = archive.readUInt16LE(cursor + 32);
    const externalAttributes = archive.readUInt32LE(cursor + 38);
    const localHeaderOffset = archive.readUInt32LE(cursor + 42);
    const centralEntryEnd = cursor + 46 + fileNameLength + extraFieldLength + fileCommentLength;
    if (centralEntryEnd > centralDirectoryOffset + centralDirectorySize || centralEntryEnd > archive.length) {
      throw new Error("无效的 zip 归档：中央目录条目被截断。");
    }
    const fileName = archive.subarray(cursor + 46, cursor + 46 + fileNameLength).toString("utf8");
    cursor = centralEntryEnd;

    const unixMode = externalAttributes >>> 16;
    if ((unixMode & 0o170000) === 0o120000) {
      throw new Error(`不支持的 zip 归档条目类型：符号链接 ${fileName}`);
    }

    if ((generalPurposeFlag & 0x01) !== 0) {
      throw new Error(`不支持加密的 zip 归档条目：${fileName}`);
    }

    const safeRelative = safeArchiveRelativePath(fileName);
    if (fileName.endsWith("/") || fileName.endsWith("\\")) {
      await fs.mkdir(resolveInsideRoot(rootDir, safeRelative), { recursive: true });
      continue;
    }

    assertArchiveEntryWithinLimits({
      fileName,
      uncompressedSize,
      compressedSize,
      totalUncompressedBytes
    });
    totalUncompressedBytes += uncompressedSize;

    if (localHeaderOffset + 30 > archive.length || archive.readUInt32LE(localHeaderOffset) !== 0x04034b50) {
      throw new Error(`无效的 zip 归档：${fileName} 的本地文件头格式错误`);
    }
    const localNameLength = archive.readUInt16LE(localHeaderOffset + 26);
    const localExtraLength = archive.readUInt16LE(localHeaderOffset + 28);
    const dataStart = localHeaderOffset + 30 + localNameLength + localExtraLength;
    const dataEnd = dataStart + compressedSize;
    if (dataEnd > archive.length) {
      throw new Error(`无效的 zip 归档：${fileName} 的压缩数据被截断`);
    }

    const compressed = archive.subarray(dataStart, dataEnd);
    let content: Buffer;
    if (compressionMethod === 0) {
      content = Buffer.from(compressed);
    } else if (compressionMethod === 8) {
      content = inflateRawSync(compressed);
    } else {
      throw new Error(`${fileName} 使用了不支持的 zip 压缩方法 ${compressionMethod}`);
    }

    if (content.byteLength !== uncompressedSize) {
      throw new Error(`无效的 zip 归档：${fileName} 的大小不匹配`);
    }

    await writeExtractedFile(rootDir, safeRelative, content);
  }
}

function findZipEndOfCentralDirectory(archive: Buffer): number {
  const minimumSize = 22;
  const maximumCommentSize = 0xffff;
  const start = Math.max(0, archive.length - minimumSize - maximumCommentSize);
  for (let offset = archive.length - minimumSize; offset >= start; offset -= 1) {
    if (archive.readUInt32LE(offset) === 0x06054b50) {
      return offset;
    }
  }
  throw new Error("无效的 zip 归档：未找到中央目录结束记录。");
}

async function extractTarArchive(archive: Buffer, rootDir: string): Promise<void> {
  let cursor = 0;
  let pendingLongName: string | null = null;
  let pendingPaxPath: string | null = null;
  let entryCount = 0;
  let totalUncompressedBytes = 0;

  while (cursor + 512 <= archive.length) {
    const header = archive.subarray(cursor, cursor + 512);
    cursor += 512;

    if (isZeroBlock(header)) {
      break;
    }

    const name = pendingLongName ?? pendingPaxPath ?? tarEntryName(header);
    pendingLongName = null;
    pendingPaxPath = null;
    const size = parseTarOctal(header.subarray(124, 136));
    const typeFlag = header.subarray(156, 157).toString("ascii");
    entryCount += 1;
    if (entryCount > ARCHIVE_MAX_ENTRIES) {
      throw new Error(`归档资源限制已超出：tar 包含超过 ${ARCHIVE_MAX_ENTRIES} 个条目。`);
    }
    if (size > ARCHIVE_MAX_FILE_BYTES) {
      throw new Error(`归档资源限制已超出：${name} 为 ${size} 字节（最多 ${ARCHIVE_MAX_FILE_BYTES} 字节）。`);
    }
    if (totalUncompressedBytes + size > ARCHIVE_MAX_TOTAL_BYTES) {
      throw new Error(`归档资源限制已超出：解压数据超过 ${ARCHIVE_MAX_TOTAL_BYTES} 字节。`);
    }
    totalUncompressedBytes += size;
    const dataStart = cursor;
    const dataEnd = dataStart + size;
    if (dataEnd > archive.length) {
      throw new Error(`无效的 tar 归档：${name} 的条目数据被截断`);
    }
    const content = archive.subarray(dataStart, dataEnd);
    cursor += Math.ceil(size / 512) * 512;

    if (typeFlag === "L") {
      pendingLongName = trimNulls(content.toString("utf8"));
      continue;
    }
    if (typeFlag === "x") {
      pendingPaxPath = parsePaxPath(content.toString("utf8"));
      continue;
    }

    const safeRelative = safeArchiveRelativePath(name);
    if (typeFlag === "5") {
      await fs.mkdir(resolveInsideRoot(rootDir, safeRelative), { recursive: true });
      continue;
    }
    if (typeFlag === "0" || typeFlag === "\0" || typeFlag === "") {
      await writeExtractedFile(rootDir, safeRelative, content);
      continue;
    }
    if (typeFlag === "1" || typeFlag === "2") {
      throw new Error(`不支持的 tar 归档条目类型：链接 ${name}`);
    }
  }
}

function assertArchiveEntryWithinLimits(options: {
  fileName: string;
  uncompressedSize: number;
  compressedSize: number;
  totalUncompressedBytes: number;
}): void {
  if (options.uncompressedSize > ARCHIVE_MAX_FILE_BYTES) {
    throw new Error(
      `归档资源限制已超出：${options.fileName} 为 ${options.uncompressedSize} 字节（最多 ${ARCHIVE_MAX_FILE_BYTES} 字节）。`
    );
  }
  if (options.totalUncompressedBytes + options.uncompressedSize > ARCHIVE_MAX_TOTAL_BYTES) {
    throw new Error(`归档资源限制已超出：解压数据超过 ${ARCHIVE_MAX_TOTAL_BYTES} 字节。`);
  }
  if (options.uncompressedSize > 0 && options.compressedSize === 0) {
    throw new Error(`归档资源限制已超出：${options.fileName} 的压缩比无效。`);
  }
  if (
    options.compressedSize > 0 &&
    options.uncompressedSize / options.compressedSize > ARCHIVE_MAX_COMPRESSION_RATIO
  ) {
    throw new Error(
      `归档资源限制已超出：${options.fileName} 的压缩比超过 ${ARCHIVE_MAX_COMPRESSION_RATIO}:1。`
    );
  }
}

function tarEntryName(header: Buffer): string {
  const name = trimNulls(header.subarray(0, 100).toString("utf8"));
  const prefix = trimNulls(header.subarray(345, 500).toString("utf8"));
  return prefix ? `${prefix}/${name}` : name;
}

function parseTarOctal(value: Buffer): number {
  const text = trimNulls(value.toString("ascii")).trim();
  if (!text) {
    return 0;
  }
  if (!/^[0-7]+$/.test(text)) {
    throw new Error(`无效的 tar 归档：条目大小不是八进制数（${text}）。`);
  }
  return Number.parseInt(text, 8);
}

function parsePaxPath(content: string): string | null {
  let cursor = 0;
  while (cursor < content.length) {
    const spaceIndex = content.indexOf(" ", cursor);
    if (spaceIndex === -1) {
      return null;
    }
    const lengthText = content.slice(cursor, spaceIndex);
    const recordLength = Number.parseInt(lengthText, 10);
    if (!Number.isFinite(recordLength) || recordLength <= 0) {
      return null;
    }
    const record = content.slice(spaceIndex + 1, cursor + recordLength);
    const equalsIndex = record.indexOf("=");
    if (equalsIndex !== -1 && record.slice(0, equalsIndex) === "path") {
      return trimNulls(record.slice(equalsIndex + 1).replace(/\n$/, ""));
    }
    cursor += recordLength;
  }
  return null;
}

function isZeroBlock(block: Buffer): boolean {
  return block.every((byte) => byte === 0);
}

function trimNulls(value: string): string {
  const nullIndex = value.indexOf("\0");
  return nullIndex === -1 ? value : value.slice(0, nullIndex);
}

async function writeExtractedFile(rootDir: string, safeRelative: string, content: Buffer): Promise<void> {
  const targetPath = resolveInsideRoot(rootDir, safeRelative);
  await fs.mkdir(path.dirname(targetPath), { recursive: true });
  await fs.writeFile(targetPath, content);
}

function assertSafeOutputDir(outputDir: string): string {
  const resolved = path.resolve(outputDir);
  if (resolved === path.parse(resolved).root) {
    throw new Error("拒绝将生成项目写入文件系统根目录。");
  }
  return resolved;
}

function safeRelativePath(filePath: string): string {
  try {
    return safeArchiveRelativePath(filePath);
  } catch {
    throw new Error(`inventory 中存在不安全的输入文件路径：${filePath}`);
  }
}

function safeArchiveRelativePath(filePath: string): string {
  if (!filePath || filePath.includes("\0")) {
    throw new Error(`不安全的归档条目路径：${filePath}`);
  }
  const normalizedSeparators = filePath.replace(/\\/g, "/");
  if (
    normalizedSeparators.startsWith("/") ||
    normalizedSeparators.startsWith("//") ||
    /^[A-Za-z]:($|\/)/.test(normalizedSeparators) ||
    path.isAbsolute(filePath) ||
    path.win32.isAbsolute(filePath)
  ) {
    throw new Error(`不安全的归档条目路径：${filePath}`);
  }

  const parts = normalizedSeparators.split("/").filter((part) => part.length > 0 && part !== ".");
  if (parts.length === 0 || parts.some((part) => part === "..")) {
    throw new Error(`不安全的归档条目路径：${filePath}`);
  }
  return parts.join("/");
}

function resolveInsideRoot(rootDir: string, safeRelative: string): string {
  const root = path.resolve(rootDir);
  const target = path.resolve(root, ...safeRelative.split("/"));
  if (target !== root && !target.startsWith(`${root}${path.sep}`)) {
    throw new Error(`不安全的归档条目解析到了解压根目录之外：${safeRelative}`);
  }
  return target;
}

function mainTsSource(manifest: GeneratedProjectManifest): string {
  return `import { generatedModuleCount } from "./modules/module-index";
import type { ReconstructionManifest } from "./types/reconstruction";

export const reconstructionManifest: ReconstructionManifest = ${JSON.stringify(manifest, null, 2)};

export function sourceFileCount(): number {
  return reconstructionManifest.copiedSourceFiles.length;
}

export function moduleCandidateCount(): number {
  return generatedModuleCount();
}
`;
}

function reconstructionEntrySource(plan: ReconstructionPlan, manifest: GeneratedProjectManifest): string {
  return `import { reconstructionManifest, moduleCandidateCount, sourceFileCount } from "../main";

export const reconstructionEntrySummary = {
  entryHtml: ${JSON.stringify(plan.entryHtml)},
  detectedRuntime: ${JSON.stringify(plan.detectedRuntime)},
  sourceFiles: sourceFileCount(),
  generatedModules: moduleCandidateCount(),
  analysisFiles: reconstructionManifest.analysisFiles.length,
  generatedModuleFiles: reconstructionManifest.generatedModuleFiles.length
} as const;

export function describeReconstructionEntry(): string {
  return \`${manifest.entrypoint}：\${reconstructionEntrySummary.generatedModules} 个生成模块记录\`;
}
`;
}

function runtimeShimSource(): string {
  return `export interface BrowserGlobalShim {
  name: string;
  reason: string;
  riskLevel: "low" | "medium" | "high";
}

export const browserGlobalShims: BrowserGlobalShim[] = [
  {
    name: "window",
    reason: "生成模块可能引用从原始 bundle 保留的浏览器全局对象。",
    riskLevel: "low"
  },
  {
    name: "document",
    reason: "静态宿主验证需要为面向 DOM 的恢复代码提供类型化占位对象。",
    riskLevel: "low"
  }
];
`;
}

function reconstructionTypesSource(): string {
  return `export type EvidenceRiskLevel = "low" | "medium" | "high";

export interface SourceRange {
  start: number;
  end: number;
}

export interface RuntimeWrapperMeta {
  id: string;
  filePath: string;
  runtimeKind: string;
  wrapperKind: string;
  confidence: number;
  riskLevel: EvidenceRiskLevel;
  loc?: string;
  sourceRange?: SourceRange;
  astPath?: string;
  detail: string;
  evidenceRefs: string[];
}

export interface ModuleBoundaryMeta {
  id: string;
  filePath: string;
  moduleId: string;
  boundaryKind: string;
  runtimeKind: string;
  confidence: number;
  riskLevel: EvidenceRiskLevel;
  sourcePath?: string;
  loc?: string;
  sourceRange?: SourceRange;
  astPath?: string;
  detail: string;
  evidenceRefs: string[];
}

export interface GeneratedModuleMeta {
  modulePath: string;
  sourceKind: string;
  sourceFilePath?: string;
  sourceMapPath?: string;
  boundaryId?: string;
  sourceHash: string;
  riskLevel: EvidenceRiskLevel;
  evidenceRefs: string[];
}

export interface ReconstructionManifest {
  kind: "generated_project";
  jobId?: string;
  projectPath: string;
  entrypoint: string;
  generatedFiles: string[];
  copiedSourceFiles: string[];
  transformedSourceFiles: string[];
  generatedModuleFiles: string[];
  entrypointFiles: string[];
  typeDefinitionFiles: string[];
  runtimeShimFiles: string[];
  analysisFiles: string[];
  sourceRoot: string;
  limitations: string[];
}
`;
}

function indexHtmlSource(plan: ReconstructionPlan, manifest: GeneratedProjectManifest): string {
  const runtime = plan.detectedRuntime.length > 0 ? plan.detectedRuntime.join(", ") : "未知";
  const entry = plan.entryHtml ?? "生成的宿主页";
  return `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>AI JS Unpack 生成项目</title>
    <style>
      :root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      body { margin: 0; padding: 32px; background: #f7f7f4; color: #1b1d1f; }
      main { max-width: 960px; margin: 0 auto; }
      h1 { font-size: 28px; margin: 0 0 12px; }
      section { border: 1px solid #d8d5cd; border-radius: 6px; padding: 18px; margin-top: 16px; background: #fff; }
      dl { display: grid; grid-template-columns: 160px 1fr; gap: 10px 16px; }
      dt { color: #62676d; }
      dd { margin: 0; font-family: "SFMono-Regular", Consolas, monospace; overflow-wrap: anywhere; }
    </style>
  </head>
  <body>
    <main>
      <h1>生成的重建外壳</h1>
      <p>此静态项目保留输入证据，并提供可构建的审计界面用于 sandbox 验证。</p>
      <section>
        <dl>
          <dt>原始入口</dt>
          <dd>${escapeHtml(entry)}</dd>
          <dt>检测到的 runtime</dt>
          <dd>${escapeHtml(runtime)}</dd>
          <dt>已复制源文件</dt>
          <dd>${manifest.copiedSourceFiles.length}</dd>
          <dt>生成模块</dt>
          <dd>${manifest.generatedModuleFiles.length}</dd>
          <dt>Manifest</dt>
          <dd>src/reconstruction-manifest.json</dd>
        </dl>
      </section>
    </main>
  </body>
</html>
`;
}

function buildScriptSource(): string {
  return `import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(root, "dist");
await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });
await cp(path.join(root, "index.html"), path.join(dist, "index.html"));
await cp(path.join(root, "src"), path.join(dist, "src"), { recursive: true });
const publicDir = path.join(root, "public");
if (existsSync(publicDir)) {
  await cp(publicDir, path.join(dist, "public"), { recursive: true });
}
const manifest = JSON.parse(await readFile(path.join(root, "src", "reconstruction-manifest.json"), "utf8"));
await writeFile(
  path.join(dist, "build-manifest.json"),
  JSON.stringify({ status: "pass", sourceFiles: manifest.copiedSourceFiles.length }, null, 2) + "\\n"
);
console.log("生成项目构建已将静态产物复制到 dist。");
`;
}

function typecheckScriptSource(): string {
  return `import { access, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(await readFile(path.join(root, "src", "reconstruction-manifest.json"), "utf8"));
const mainSource = await readFile(path.join(root, "src", "main.ts"), "utf8");
if (manifest.kind !== "generated_project") {
  throw new Error("生成项目 Manifest 的 kind 无效。");
}
if (
  !Array.isArray(manifest.copiedSourceFiles) ||
  !Array.isArray(manifest.generatedFiles) ||
  !Array.isArray(manifest.transformedSourceFiles) ||
  !Array.isArray(manifest.generatedModuleFiles) ||
  !Array.isArray(manifest.entrypointFiles) ||
  !Array.isArray(manifest.typeDefinitionFiles) ||
  !Array.isArray(manifest.runtimeShimFiles) ||
  !Array.isArray(manifest.analysisFiles)
) {
  throw new Error("生成项目 Manifest 的文件列表无效。");
}
if (!mainSource.includes("reconstructionManifest")) {
  throw new Error("缺少生成的 TypeScript 入口契约。");
}
for (const filePath of manifest.copiedSourceFiles) {
  if (path.isAbsolute(filePath) || filePath.includes("..")) {
    throw new Error(\`不安全的已复制源文件路径：\${filePath}\`);
  }
  await access(path.join(root, filePath));
}
for (const filePath of [
  ...manifest.transformedSourceFiles,
  ...manifest.generatedModuleFiles,
  ...manifest.entrypointFiles,
  ...manifest.typeDefinitionFiles,
  ...manifest.runtimeShimFiles,
  ...manifest.analysisFiles
]) {
  if (path.isAbsolute(filePath) || filePath.includes("..")) {
    throw new Error(\`不安全的生成证据文件路径：\${filePath}\`);
  }
  await access(path.join(root, filePath));
}
console.log("生成项目的类型契约和已复制源文件 Manifest 验证通过。");
`;
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function toPosix(filePath: string): string {
  return filePath.replace(/\\/g, "/").split(path.sep).join("/");
}
