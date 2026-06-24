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
}

export interface NormalizedInputPackage {
  rootDir: string;
  sourcePath: string;
  sourceKind: "directory" | "zip" | "tar" | "tar_gz";
  cleanup: () => Promise<void>;
}

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
  scriptTransforms: ScriptTransformRecord[];
  transformLog: TransformLogEntry[];
  rollbackMap: RollbackMapEntry[];
  evidenceSummary: {
    astIndexFiles: string[];
    chunkGraphEdgeCount: number;
    resourceGraphEdgeCount: number;
    moduleCandidateCount: number;
    transformCount: number;
    symbolCount: number;
  };
  limitations: string[];
}

export interface WriteProjectConfig {
  inputPath: string;
  outputDir: string;
  packageName?: string;
}

export interface GeneratedProjectManifest {
  kind: "generated_project";
  jobId?: string;
  projectPath: string;
  entrypoint: string;
  generatedFiles: string[];
  copiedSourceFiles: string[];
  transformedSourceFiles: string[];
  analysisFiles: string[];
  sourceRoot: string;
  limitations: string[];
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
  sourceCandidates: string[];
  sourcesContentAvailable: string[];
  missingSourcesContent: string[];
  warnings: string[];
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
  originalSnippet?: string;
  transformedSnippet?: string;
  detail?: string;
}

export interface RollbackMapEntry {
  filePath: string;
  kind: string;
  originalLoc?: string;
  transformedLoc?: string;
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
  "src/analysis/transform-log.json",
  "src/analysis/rollback-map.json",
  "scripts/build.mjs",
  "scripts/typecheck.mjs"
];
const LOCAL_SOURCE_SCHEMES = new Set(["webpack", "rollup", "vite", "parcel", "browserify", "ng", "esbuild", "turbopack", "snowpack"]);

export async function analyzeInputPackage(inputPath: string, config: AnalyzeInputConfig = {}): Promise<CoreAnalysisResult> {
  const normalized = config.rootDir ? undefined : await normalizeInputPackage(inputPath);
  try {
    const rootDir = path.resolve(config.rootDir ?? normalized?.rootDir ?? inputPath);
    const sourceKind = config.inputSourceKind ?? normalized?.sourceKind;
    const inventory = await buildInputInventory(rootDir);
    if (sourceKind && sourceKind !== "directory") {
      inventory.warnings.unshift(`Input ${sourceKind} archive was extracted into a verified temporary workspace.`);
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

    return {
      inventory,
      astIndexes,
      detectedRuntime,
      sourceMapAnalysis,
      graphAnalysis,
      transformAnalysis,
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
    throw new Error(`Input path must be a directory or supported archive file: ${inputPath}`);
  }

  const sourceKind = archiveKindForPath(sourcePath);
  if (!sourceKind) {
    throw new Error(`Unsupported input file type. Expected a directory, .zip, .tar, .tar.gz, or .tgz: ${inputPath}`);
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
      const tarBuffer = sourceKind === "tar_gz" ? gunzipSync(archive) : archive;
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
    warnings.push("No HTML entry found; a minimal host page will be required for browser validation.");
  }
  if (scripts.length === 0) {
    warnings.push("No JavaScript bundle found.");
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
    warnings.push(error instanceof Error ? error.message : "Unknown Babel parse error.");
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
    warnings.push("No HTML entry was available; chunk graph uses scripts as candidate roots.");
  }
  if (sourceMapAnalysis.bundleCount === 0) {
    warnings.push("No source maps were available; module candidates are limited to imports and AST symbols.");
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
      warnings: inventory.assets.length === 0 && inventory.styles.length === 0 ? ["No referenced styles or assets were found."] : []
    },
    moduleCandidateGraph: {
      nodes: [...moduleNodes.values()].sort(compareGraphNode),
      edges: [...moduleEdges.values()].sort(compareGraphEdge),
      sourceCandidates: sourceMapAnalysis.sourceCandidates,
      warnings: sourceMapAnalysis.warnings
    }
  };
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
    const detail = error instanceof Error ? error.message : "Unknown Babel parse error.";
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
          detail: "Sequence expression includes await/yield or too few expressions."
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
    throw new Error("Babel traverse runtime is unavailable.");
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
      detail: "Marked call-expression wrapper for later reconstruction."
    });
  }
  if (body.length === 1 && body[0].type === "FunctionDeclaration") {
    wrapperMarks.add("single_function_wrapper");
    transformLog.push({
      filePath,
      kind: "wrapper_mark",
      status: "applied",
      originalLoc: formatNodeLoc(body[0]),
      detail: "Marked single-function wrapper for later reconstruction."
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
    originalSnippet,
    transformedSnippet
  });
  rollbackMap.push({
    filePath,
    kind,
    originalLoc,
    transformedLoc: originalLoc,
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
    "Generated project is a deterministic static host shell; semantic module recovery remains evidence-bound future work.",
    "Build and typecheck scripts are offline validation shims until dependency installation policy is implemented."
  ];

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
    generatedFiles: GENERATED_PROJECT_FILES,
    inputInventory: analysis.inventory,
    astIndexes: analysis.astIndexes,
    sourceMapAnalysis: analysis.sourceMapAnalysis,
    graphAnalysis: analysis.graphAnalysis,
    scriptTransforms: analysis.transformAnalysis.scriptTransforms,
    transformLog: analysis.transformAnalysis.transformLog,
    rollbackMap: analysis.transformAnalysis.rollbackMap,
    evidenceSummary: {
      astIndexFiles: analysis.astIndexes.map((index) => index.filePath),
      chunkGraphEdgeCount: analysis.graphAnalysis.chunkGraph.edges.length,
      resourceGraphEdgeCount: analysis.graphAnalysis.resourceGraph.edges.length,
      moduleCandidateCount: analysis.graphAnalysis.moduleCandidateGraph.nodes.filter((node) => node.kind === "source_candidate").length,
      transformCount: analysis.transformAnalysis.transformLog.filter((entry) => entry.status === "applied" && entry.kind !== "wrapper_mark").length,
      symbolCount: analysis.astIndexes.reduce((count, index) => count + index.symbols.length, 0)
    },
    limitations
  };
}

export async function writeProject(plan: ReconstructionPlan, config: WriteProjectConfig): Promise<WriteProjectResult> {
  const normalized = await normalizeInputPackage(config.inputPath);
  try {
    const inputRoot = path.resolve(normalized.rootDir);
    const projectRoot = assertSafeOutputDir(config.outputDir);
    await fs.rm(projectRoot, { recursive: true, force: true });
    await fs.mkdir(path.join(projectRoot, "src"), { recursive: true });
    await fs.mkdir(path.join(projectRoot, "src", "analysis"), { recursive: true });
    await fs.mkdir(path.join(projectRoot, "src", "transformed"), { recursive: true });
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
      "src/analysis/transform-log.json",
      "src/analysis/rollback-map.json"
    ];

    const manifest: GeneratedProjectManifest = {
      kind: "generated_project",
      jobId: plan.jobId,
      projectPath: ".",
      entrypoint: "index.html",
      generatedFiles: GENERATED_PROJECT_FILES,
      copiedSourceFiles,
      transformedSourceFiles,
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
    await fs.writeFile(path.join(projectRoot, "index.html"), indexHtmlSource(plan, manifest), "utf8");
    await fs.writeFile(path.join(projectRoot, "scripts", "build.mjs"), buildScriptSource(), "utf8");
    await fs.writeFile(path.join(projectRoot, "scripts", "typecheck.mjs"), typecheckScriptSource(), "utf8");

    return {
      projectPath: projectRoot,
      manifest
    };
  } finally {
    await normalized.cleanup();
  }
}

export async function runHeadlessPipeline(inputPath: string, config: AnalyzeInputConfig = {}): Promise<CoreAnalysisResult> {
  return analyzeInputPackage(inputPath, config);
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
      warnings.push(`Failed to parse source map ${sourceMapPath}: ${error instanceof Error ? error.message : "Unknown error"}`);
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

      for (const source of rawSources) {
        const content = sourceContentFor(traceMap, source);
        const normalizedSource = normalizeSourceMapCandidate(source) ?? source;
        if (content === null) {
          missingSourcesContent.push(normalizedSource);
          continue;
        }
        availableSourcesContent.push(normalizedSource);
      }

      for (const candidate of sourceCandidatesForBundle) {
        sourceCandidates.add(candidate);
      }

      const bundleWarnings: string[] = [];
      if (sourceCandidatesForBundle.length === 0) {
        bundleWarnings.push(`Source map ${sourceMapPath} did not yield any source candidates.`);
      }
      if (missingSourcesContent.length > 0) {
        bundleWarnings.push(`Source map ${sourceMapPath} is missing sourcesContent for ${missingSourcesContent.length} source${missingSourcesContent.length === 1 ? "" : "s"}.`);
      }

      bundleAnalyses.push({
        bundlePath,
        sourceMapPath,
        sourceMapFile: normalizeSourceMapFile(traceMap.file),
        sourceRoot,
        sources,
        sourceCandidates: sourceCandidatesForBundle,
        sourcesContentAvailable: availableSourcesContent,
        missingSourcesContent,
        warnings: bundleWarnings
      });

      warnings.push(...bundleWarnings.map((warning) => `${sourceMapPath}: ${warning}`));
    } catch (error) {
      warnings.push(`Failed to analyze source map ${sourceMapPath}: ${error instanceof Error ? error.message : "Unknown error"}`);
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
  const extra = missingReferences.length > 5 ? `; and ${missingReferences.length - 5} more` : "";
  return `HTML references ${missingReferences.length} missing file${missingReferences.length === 1 ? "" : "s"}: ${preview}${extra}.`;
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

function archiveKindForPath(filePath: string): NormalizedInputPackage["sourceKind"] | null {
  const lower = filePath.toLowerCase();
  if (lower.endsWith(".zip")) return "zip";
  if (lower.endsWith(".tar.gz") || lower.endsWith(".tgz")) return "tar_gz";
  if (lower.endsWith(".tar")) return "tar";
  return null;
}

async function extractZipArchive(archive: Buffer, rootDir: string): Promise<void> {
  const eocdOffset = findZipEndOfCentralDirectory(archive);
  const entryCount = archive.readUInt16LE(eocdOffset + 10);
  const centralDirectorySize = archive.readUInt32LE(eocdOffset + 12);
  const centralDirectoryOffset = archive.readUInt32LE(eocdOffset + 16);

  if (entryCount === 0xffff || centralDirectorySize === 0xffffffff || centralDirectoryOffset === 0xffffffff) {
    throw new Error("Unsupported zip64 archive.");
  }
  if (centralDirectoryOffset + centralDirectorySize > archive.length) {
    throw new Error("Invalid zip archive: central directory is outside the archive.");
  }

  let cursor = centralDirectoryOffset;
  for (let index = 0; index < entryCount; index += 1) {
    if (archive.readUInt32LE(cursor) !== 0x02014b50) {
      throw new Error("Invalid zip archive: central directory entry is malformed.");
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
    const fileName = archive.subarray(cursor + 46, cursor + 46 + fileNameLength).toString("utf8");
    cursor += 46 + fileNameLength + extraFieldLength + fileCommentLength;

    const unixMode = externalAttributes >>> 16;
    if ((unixMode & 0o170000) === 0o120000) {
      throw new Error(`Unsupported zip archive entry type: symlink ${fileName}`);
    }

    if ((generalPurposeFlag & 0x01) !== 0) {
      throw new Error(`Unsupported encrypted zip archive entry: ${fileName}`);
    }

    const safeRelative = safeArchiveRelativePath(fileName);
    if (fileName.endsWith("/") || fileName.endsWith("\\")) {
      await fs.mkdir(resolveInsideRoot(rootDir, safeRelative), { recursive: true });
      continue;
    }

    if (localHeaderOffset + 30 > archive.length || archive.readUInt32LE(localHeaderOffset) !== 0x04034b50) {
      throw new Error(`Invalid zip archive: local file header is malformed for ${fileName}`);
    }
    const localNameLength = archive.readUInt16LE(localHeaderOffset + 26);
    const localExtraLength = archive.readUInt16LE(localHeaderOffset + 28);
    const dataStart = localHeaderOffset + 30 + localNameLength + localExtraLength;
    const dataEnd = dataStart + compressedSize;
    if (dataEnd > archive.length) {
      throw new Error(`Invalid zip archive: compressed data is truncated for ${fileName}`);
    }

    const compressed = archive.subarray(dataStart, dataEnd);
    let content: Buffer;
    if (compressionMethod === 0) {
      content = Buffer.from(compressed);
    } else if (compressionMethod === 8) {
      content = inflateRawSync(compressed);
    } else {
      throw new Error(`Unsupported zip compression method ${compressionMethod} for ${fileName}`);
    }

    if (content.byteLength !== uncompressedSize) {
      throw new Error(`Invalid zip archive: size mismatch for ${fileName}`);
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
  throw new Error("Invalid zip archive: end of central directory not found.");
}

async function extractTarArchive(archive: Buffer, rootDir: string): Promise<void> {
  let cursor = 0;
  let pendingLongName: string | null = null;
  let pendingPaxPath: string | null = null;

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
    const dataStart = cursor;
    const dataEnd = dataStart + size;
    if (dataEnd > archive.length) {
      throw new Error(`Invalid tar archive: entry data is truncated for ${name}`);
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
      throw new Error(`Unsupported tar archive entry type: link ${name}`);
    }
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
    throw new Error(`Invalid tar archive: entry size is not octal (${text}).`);
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
    throw new Error("Refusing to write generated project to a filesystem root.");
  }
  return resolved;
}

function safeRelativePath(filePath: string): string {
  try {
    return safeArchiveRelativePath(filePath);
  } catch {
    throw new Error(`Unsafe input file path in inventory: ${filePath}`);
  }
}

function safeArchiveRelativePath(filePath: string): string {
  if (!filePath || filePath.includes("\0")) {
    throw new Error(`Unsafe archive entry path: ${filePath}`);
  }
  const normalizedSeparators = filePath.replace(/\\/g, "/");
  if (
    normalizedSeparators.startsWith("/") ||
    normalizedSeparators.startsWith("//") ||
    /^[A-Za-z]:($|\/)/.test(normalizedSeparators) ||
    path.isAbsolute(filePath) ||
    path.win32.isAbsolute(filePath)
  ) {
    throw new Error(`Unsafe archive entry path: ${filePath}`);
  }

  const parts = normalizedSeparators.split("/").filter((part) => part.length > 0 && part !== ".");
  if (parts.length === 0 || parts.some((part) => part === "..")) {
    throw new Error(`Unsafe archive entry path: ${filePath}`);
  }
  return parts.join("/");
}

function resolveInsideRoot(rootDir: string, safeRelative: string): string {
  const root = path.resolve(rootDir);
  const target = path.resolve(root, ...safeRelative.split("/"));
  if (target !== root && !target.startsWith(`${root}${path.sep}`)) {
    throw new Error(`Unsafe archive entry resolved outside extraction root: ${safeRelative}`);
  }
  return target;
}

function mainTsSource(manifest: GeneratedProjectManifest): string {
  return `export interface ReconstructionManifest {
  kind: "generated_project";
  jobId?: string;
  projectPath: string;
  entrypoint: string;
  generatedFiles: string[];
  copiedSourceFiles: string[];
  transformedSourceFiles: string[];
  analysisFiles: string[];
  sourceRoot: string;
  limitations: string[];
}

export const reconstructionManifest: ReconstructionManifest = ${JSON.stringify(manifest, null, 2)};

export function sourceFileCount(): number {
  return reconstructionManifest.copiedSourceFiles.length;
}
`;
}

function indexHtmlSource(plan: ReconstructionPlan, manifest: GeneratedProjectManifest): string {
  const runtime = plan.detectedRuntime.length > 0 ? plan.detectedRuntime.join(", ") : "unknown";
  const entry = plan.entryHtml ?? "generated host page";
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>AI JS Unpack Generated Project</title>
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
      <h1>Generated reconstruction shell</h1>
      <p>This static project preserves input evidence and exposes a buildable audit surface for sandbox validation.</p>
      <section>
        <dl>
          <dt>Original entry</dt>
          <dd>${escapeHtml(entry)}</dd>
          <dt>Detected runtime</dt>
          <dd>${escapeHtml(runtime)}</dd>
          <dt>Copied source files</dt>
          <dd>${manifest.copiedSourceFiles.length}</dd>
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
console.log("Generated project build copied static artifacts to dist.");
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
  throw new Error("Generated project manifest kind is invalid.");
}
if (
  !Array.isArray(manifest.copiedSourceFiles) ||
  !Array.isArray(manifest.generatedFiles) ||
  !Array.isArray(manifest.transformedSourceFiles) ||
  !Array.isArray(manifest.analysisFiles)
) {
  throw new Error("Generated project manifest file lists are invalid.");
}
if (!mainSource.includes("export interface ReconstructionManifest")) {
  throw new Error("Generated TypeScript entry contract is missing.");
}
for (const filePath of manifest.copiedSourceFiles) {
  if (path.isAbsolute(filePath) || filePath.includes("..")) {
    throw new Error(\`Unsafe copied source file path: \${filePath}\`);
  }
  await access(path.join(root, filePath));
}
for (const filePath of [...manifest.transformedSourceFiles, ...manifest.analysisFiles]) {
  if (path.isAbsolute(filePath) || filePath.includes("..")) {
    throw new Error(\`Unsafe generated evidence file path: \${filePath}\`);
  }
  await access(path.join(root, filePath));
}
console.log("Generated project type contract and copied source manifest validated.");
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
