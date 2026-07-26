#!/usr/bin/env node
import { CONTRACT_SCHEMA_VERSION } from "@ai-jsunpack/shared";
import { analyzeInputPackage, collectObfuscatedBindings, normalizeInputPackage, planReconstruction, writeProject } from "./index.js";
import { readFile } from "node:fs/promises";

interface CliOptions {
  command?: string;
  inputPath?: string;
  jobId?: string;
  outputDir?: string;
  agentFeedbackFile?: string;
}

interface ArtifactPayload {
  schemaVersion: string;
  jobId: string;
  kind: "input_inventory" | "source_index" | "ast_index" | "reconstruction_plan" | "generated_project";
}

function parseArgs(args: string[]): CliOptions {
  const options: CliOptions = {};
  const positional: string[] = [];

  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--job-id") {
      options.jobId = args[index + 1];
      index += 1;
      continue;
    }
    if (arg === "--output-dir") {
      options.outputDir = args[index + 1];
      index += 1;
      continue;
    }
    if (arg === "--agent-feedback-file") {
      options.agentFeedbackFile = args[index + 1];
      index += 1;
      continue;
    }
    positional.push(arg);
  }

  const [command, inputPath] = positional;
  return { ...options, command, inputPath };
}

function usage(): string {
  return [
    "用法：",
    "  node packages/core/dist/cli.js analyze <inputPath> --job-id <jobId>",
    "  node packages/core/dist/cli.js reconstruct <inputPath> --job-id <jobId> --output-dir <dir> [--agent-feedback-file <json>]"
  ].join("\n");
}

async function main(): Promise<void> {
  const options = parseArgs(process.argv.slice(2));

  if (!options.inputPath || !options.jobId) {
    throw new Error(usage());
  }

  if (options.command === "reconstruct") {
    if (!options.outputDir) {
      throw new Error(usage());
    }
    const normalized = await normalizeInputPackage(options.inputPath);
    try {
      const result = await analyzeInputPackage(normalized.rootDir, {
        jobId: options.jobId,
        rootDir: normalized.rootDir,
        inputSourceKind: normalized.sourceKind
      });
      const plan = planReconstruction(result, { jobId: options.jobId });
      const agentFeedback = options.agentFeedbackFile
        ? (JSON.parse(await readFile(options.agentFeedbackFile, "utf8")) as unknown)
        : undefined;
      const generatedProject = await writeProject(plan, {
        inputPath: normalized.rootDir,
        outputDir: options.outputDir,
        agentFeedback
      });
      const reconstructionPlanPayload: ArtifactPayload & { plan: typeof plan } = {
        schemaVersion: CONTRACT_SCHEMA_VERSION,
        jobId: options.jobId,
        kind: "reconstruction_plan",
        plan
      };
      const generatedProjectManifestPayload: ArtifactPayload & { manifest: typeof generatedProject.manifest } = {
        schemaVersion: CONTRACT_SCHEMA_VERSION,
        jobId: options.jobId,
        kind: "generated_project",
        manifest: generatedProject.manifest
      };
      process.stdout.write(
        JSON.stringify(
          {
            jobId: options.jobId,
            schemaVersion: CONTRACT_SCHEMA_VERSION,
            generatedProjectPath: generatedProject.projectPath,
            reconstructionPlanPayload,
            generatedProjectManifestPayload
          },
          null,
          2
        )
      );
      process.stdout.write("\n");
    } finally {
      await normalized.cleanup();
    }
    return;
  }

  if (options.command !== "analyze") {
    throw new Error(usage());
  }

  const normalized = await normalizeInputPackage(options.inputPath);
  try {
    const result = await analyzeInputPackage(normalized.rootDir, {
      jobId: options.jobId,
      rootDir: normalized.rootDir,
      inputSourceKind: normalized.sourceKind
    });
    const inventoryArtifactPayload: ArtifactPayload & { inventory: typeof result.inventory } = {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      jobId: options.jobId,
      kind: "input_inventory",
      inventory: result.inventory
    };
    const astIndexArtifactPayload: ArtifactPayload & {
      astIndexes: typeof result.astIndexes;
      detectedRuntime: typeof result.detectedRuntime;
    } = {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      jobId: options.jobId,
      kind: "ast_index",
      astIndexes: result.astIndexes,
      detectedRuntime: result.detectedRuntime
    };
    const sourceIndexArtifactPayload: ArtifactPayload & {
      scripts: Array<{
        filePath: string;
        targetPath: string;
        originalHash: string;
        transformedHash: string;
        transforms: string[];
        transformedSource: string;
        obfuscatedBindings: Array<{
          name: string;
          kind: string;
          loc?: string;
          references: number;
          fallbackName: string;
        }>;
      }>;
    } = {
      schemaVersion: CONTRACT_SCHEMA_VERSION,
      jobId: options.jobId,
      kind: "source_index",
      scripts: result.transformAnalysis.scriptTransforms.map((transform) => {
        let fallbackIndex = 0;
        const obfuscatedBindings = collectObfuscatedBindings(transform.transformedSource)
          .map((symbol) => {
            fallbackIndex += 1;
            return {
              name: symbol.name,
              kind: symbol.kind,
              loc: symbol.loc,
              references: symbol.references,
              fallbackName: `recovered${symbol.kind === "function" ? "Function" : symbol.kind === "parameter" ? "Parameter" : "Value"}${fallbackIndex}`
            };
          });
        return {
          filePath: transform.filePath,
          targetPath: `src/transformed/${transform.filePath.replaceAll("\\", "/")}`,
          originalHash: transform.originalHash,
          transformedHash: transform.transformedHash,
          transforms: transform.transforms,
          transformedSource: transform.transformedSource,
          obfuscatedBindings
        };
      })
    };

    process.stdout.write(
      JSON.stringify(
        {
          jobId: options.jobId,
          schemaVersion: CONTRACT_SCHEMA_VERSION,
          inventoryArtifactPayload,
          sourceIndexArtifactPayload,
          astIndexArtifactPayload
        },
        null,
        2
      )
    );
    process.stdout.write("\n");
  } finally {
    await normalized.cleanup();
  }
}

main().catch((error: unknown) => {
  const message = error instanceof Error ? error.message : "未知的 Core CLI 错误";
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
});
