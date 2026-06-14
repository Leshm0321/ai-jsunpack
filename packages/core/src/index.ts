import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import path from "node:path";
import { parse } from "@babel/parser";
import traverseModule, { type NodePath, type TraverseOptions } from "@babel/traverse";
import type * as t from "@babel/types";
import type { AstIndex, HeadlessAnalysisResult, InputFileRecord, InputInventory } from "@ai-jsunpack/shared";

export interface AnalyzeInputConfig {
  jobId?: string;
  rootDir?: string;
}

const TEXT_EXTENSIONS = new Set([".html", ".htm", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css", ".json", ".map"]);

export async function analyzeInputPackage(inputPath: string, config: AnalyzeInputConfig = {}): Promise<HeadlessAnalysisResult> {
  const rootDir = path.resolve(config.rootDir ?? inputPath);
  const inventory = await buildInputInventory(rootDir);
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

export async function runHeadlessPipeline(inputPath: string, config: AnalyzeInputConfig = {}): Promise<HeadlessAnalysisResult> {
  return analyzeInputPackage(inputPath, config);
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

function toPosix(filePath: string): string {
  return filePath.split(path.sep).join("/");
}
