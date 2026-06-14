#!/usr/bin/env node
import { CONTRACT_SCHEMA_VERSION } from "@ai-jsunpack/shared";
import { analyzeInputPackage } from "./index.js";

interface CliOptions {
  command?: string;
  inputPath?: string;
  jobId?: string;
}

interface ArtifactPayload {
  schemaVersion: string;
  jobId: string;
  kind: "input_inventory" | "ast_index";
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
    positional.push(arg);
  }

  const [command, inputPath] = positional;
  return { ...options, command, inputPath };
}

function usage(): string {
  return "Usage: node packages/core/dist/cli.js analyze <inputPath> --job-id <jobId>";
}

async function main(): Promise<void> {
  const options = parseArgs(process.argv.slice(2));

  if (options.command !== "analyze" || !options.inputPath || !options.jobId) {
    throw new Error(usage());
  }

  const result = await analyzeInputPackage(options.inputPath, { jobId: options.jobId });
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

  process.stdout.write(
    JSON.stringify(
      {
        jobId: options.jobId,
        schemaVersion: CONTRACT_SCHEMA_VERSION,
        inventoryArtifactPayload,
        astIndexArtifactPayload
      },
      null,
      2
    )
  );
  process.stdout.write("\n");
}

main().catch((error: unknown) => {
  const message = error instanceof Error ? error.message : "Unknown core CLI error";
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
});
