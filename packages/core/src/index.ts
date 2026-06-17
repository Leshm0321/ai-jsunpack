import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { gunzipSync, inflateRawSync } from "node:zlib";
import { parse } from "@babel/parser";
import traverseModule, { type NodePath, type TraverseOptions } from "@babel/traverse";
import type * as t from "@babel/types";
import type { AstIndex, HeadlessAnalysisResult, InputFileRecord, InputInventory } from "@ai-jsunpack/shared";

export interface AnalyzeInputConfig {
  jobId?: string;
  rootDir?: string;
  inputSourceKind?: NormalizedInputPackage["sourceKind"];
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
  evidenceSummary: {
    astIndexFiles: string[];
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
  sourceRoot: string;
  limitations: string[];
}

export interface WriteProjectResult {
  projectPath: string;
  manifest: GeneratedProjectManifest;
}

const TEXT_EXTENSIONS = new Set([".html", ".htm", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css", ".json", ".map"]);
const GENERATED_PROJECT_FILES = [
  "package.json",
  "tsconfig.json",
  "index.html",
  "src/main.ts",
  "src/reconstruction-manifest.json",
  "scripts/build.mjs",
  "scripts/typecheck.mjs"
];

export async function analyzeInputPackage(inputPath: string, config: AnalyzeInputConfig = {}): Promise<HeadlessAnalysisResult> {
  const normalized = config.rootDir ? undefined : await normalizeInputPackage(inputPath);
  try {
    const rootDir = path.resolve(config.rootDir ?? normalized?.rootDir ?? inputPath);
    const sourceKind = config.inputSourceKind ?? normalized?.sourceKind;
    const inventory = await buildInputInventory(rootDir);
    if (sourceKind && sourceKind !== "directory") {
      inventory.warnings.unshift(`Input ${sourceKind} archive was extracted into a verified temporary workspace.`);
    }
    const astIndexes = await Promise.all(
      inventory.scripts.map(async (scriptPath) => buildAstIndexForFile(path.join(rootDir, scriptPath), rootDir))
    );
    const detectedRuntime = detectBundleRuntime(inventory, astIndexes);

    return {
      inventory,
      astIndexes,
      detectedRuntime,
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

  for (const absolutePath of filePaths) {
    const relativePath = toPosix(path.relative(absoluteRoot, absolutePath));
    const buffer = await fs.readFile(absolutePath);
    files.push({
      path: relativePath,
      kind: classifyPath(relativePath),
      size: buffer.byteLength,
      hash: sha256(buffer)
    });
  }

  const entries = files.filter((file) => file.kind === "html").map((file) => file.path);
  const scripts = files.filter((file) => file.kind === "script").map((file) => file.path);
  const styles = files.filter((file) => file.kind === "style").map((file) => file.path);
  const assets = files.filter((file) => file.kind === "asset").map((file) => file.path);
  const sourceMaps = files.filter((file) => file.kind === "source_map").map((file) => file.path);
  const manifests = files.filter((file) => file.kind === "manifest").map((file) => file.path);
  const warnings: string[] = [];

  if (entries.length === 0) {
    warnings.push("No HTML entry found; a minimal host page will be required for browser validation.");
  }
  if (scripts.length === 0) {
    warnings.push("No JavaScript bundle found.");
  }

  return {
    files,
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

    const traverseAst = traverseModule as unknown as (ast: unknown, visitors: TraverseOptions) => void;

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

export function planReconstruction(
  analysis: HeadlessAnalysisResult,
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
    evidenceSummary: {
      astIndexFiles: analysis.astIndexes.map((index) => index.filePath),
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

    const manifest: GeneratedProjectManifest = {
      kind: "generated_project",
      jobId: plan.jobId,
      projectPath: ".",
      entrypoint: "index.html",
      generatedFiles: GENERATED_PROJECT_FILES,
      copiedSourceFiles,
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

export async function runHeadlessPipeline(inputPath: string, config: AnalyzeInputConfig = {}): Promise<HeadlessAnalysisResult> {
  return analyzeInputPackage(inputPath, config);
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
if (!Array.isArray(manifest.copiedSourceFiles) || !Array.isArray(manifest.generatedFiles)) {
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
