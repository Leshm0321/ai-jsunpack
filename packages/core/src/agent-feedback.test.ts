import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

test("reconstruct applies validated Review-approved feedback before finalizing the project", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-agent-feedback-"));
  try {
    const input = path.join(root, "input");
    const output = path.join(root, "output");
    const feedbackPath = path.join(root, "feedback.json");
    await mkdir(path.join(input, "assets"), { recursive: true });
    await writeFile(path.join(input, "index.html"), '<main id="original">Original</main><script src="assets/app.js"></script>');
    await writeFile(path.join(input, "assets", "app.js"), "window.feedbackApplied = true;");
    await writeFile(path.join(input, ".npmrc"), "script-shell=./public/original/attacker-shell");
    await writeFile(path.join(input, ".env"), "SECRET_SHOULD_NOT_BE_MIRRORED=true");
    await writeFile(path.join(input, "eslint.config.js"), "throw new Error('tool config must stay inert');");
    await writeFile(
      feedbackPath,
      JSON.stringify({
        kind: "agent_feedback",
        protocolVersion: 1,
        sourceReviewArtifactIds: ["artifact_review"],
        approvedActions: [
          {
            sourceArtifactId: "artifact_add",
            repairInstructionId: "repair_add",
            action: "add_package_script",
            path: "package.json:scripts.check",
            value: "node scripts/typecheck.mjs",
            reason: "Use the generated deterministic typecheck shim."
          },
          {
            sourceArtifactId: "artifact_replace",
            repairInstructionId: "repair_replace",
            action: "replace_package_script",
            path: "package.json:scripts.build",
            value: "node scripts/build.mjs",
            reason: "Exercise deterministic script replacement."
          },
          {
            sourceArtifactId: "artifact_mirror",
            repairInstructionId: "repair_mirror",
            action: "mirror_original_static_entry",
            path: "projectRoot",
            value: "public/original",
            reason: "Mirror the audited static entry for runtime parity."
          }
        ],
        rejectedActions: [
          {
            sourceArtifactId: "artifact_high_risk",
            action: "rewrite_everything",
            reason: "High-risk and unsupported action remained audit-only."
          }
        ]
      })
    );

    const parsed = JSON.parse(
      await runCli([
        "reconstruct",
        input,
        "--job-id",
        "job_feedback",
        "--output-dir",
        output,
        "--agent-feedback-file",
        feedbackPath
      ])
    ) as {
      reconstructionPlanPayload: { plan: { agentFeedback: { appliedActions: unknown[] } } };
      generatedProjectManifestPayload: { manifest: { agentFeedback: { appliedActions: unknown[] } } };
    };
    const packageJson = JSON.parse(await readFile(path.join(output, "package.json"), "utf8")) as {
      scripts: Record<string, string>;
    };
    const manifest = JSON.parse(await readFile(path.join(output, "src", "reconstruction-manifest.json"), "utf8")) as {
      agentFeedback: {
        sourceReviewArtifactIds: string[];
        approvedActions: Array<{ sourceArtifactId: string }>;
        appliedActions: Array<{ action: string; changed: boolean }>;
        rejectedActions: Array<{ sourceArtifactId?: string; reason: string }>;
      };
    };

    assert.equal(packageJson.scripts.check, "node scripts/typecheck.mjs");
    assert.equal(packageJson.scripts.build, "node scripts/build.mjs");
    assert.match(await readFile(path.join(output, "index.html"), "utf8"), /id="original"/);
    assert.equal(await readFile(path.join(output, "assets", "app.js"), "utf8"), "window.feedbackApplied = true;");
    await assert.rejects(readFile(path.join(output, ".npmrc"), "utf8"));
    await assert.rejects(readFile(path.join(output, ".env"), "utf8"));
    await assert.rejects(readFile(path.join(output, "eslint.config.js"), "utf8"));
    assert.deepEqual(manifest.agentFeedback.sourceReviewArtifactIds, ["artifact_review"]);
    assert.equal(manifest.agentFeedback.approvedActions.length, 3);
    assert.equal(manifest.agentFeedback.appliedActions.length, 3);
    assert.equal(manifest.agentFeedback.appliedActions.filter((action) => action.changed).length, 2);
    assert.equal(
      manifest.agentFeedback.appliedActions.find((action) => action.action === "replace_package_script")?.changed,
      false
    );
    assert.ok(manifest.agentFeedback.rejectedActions.some((action) => action.sourceArtifactId === "artifact_high_risk"));
    assert.equal(parsed.reconstructionPlanPayload.plan.agentFeedback.appliedActions.length, 3);
    assert.equal(parsed.generatedProjectManifestPayload.manifest.agentFeedback.appliedActions.length, 3);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("reconstruct rejects conflicting approved actions without changing their target", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-agent-feedback-conflict-"));
  try {
    const input = path.join(root, "input");
    const output = path.join(root, "output");
    const feedbackPath = path.join(root, "feedback.json");
    await mkdir(input);
    await writeFile(path.join(input, "index.html"), "<main>input</main>");
    await writeFile(
      feedbackPath,
      JSON.stringify({
        kind: "agent_feedback",
        protocolVersion: 1,
        sourceReviewArtifactIds: ["artifact_review"],
        approvedActions: [
          {
            sourceArtifactId: "artifact_a",
            action: "replace_package_script",
            path: "package.json:scripts.build",
            value: "node scripts/build.mjs",
            reason: "First conclusion."
          },
          {
            sourceArtifactId: "artifact_b",
            action: "replace_package_script",
            path: "package.json:scripts.build",
            value: "node scripts/typecheck.mjs",
            reason: "Conflicting conclusion."
          }
        ],
        rejectedActions: []
      })
    );

    await runCli([
      "reconstruct",
      input,
      "--job-id",
      "job_conflict",
      "--output-dir",
      output,
      "--agent-feedback-file",
      feedbackPath
    ]);
    const packageJson = JSON.parse(await readFile(path.join(output, "package.json"), "utf8")) as {
      scripts: Record<string, string>;
    };
    const manifest = JSON.parse(await readFile(path.join(output, "src", "reconstruction-manifest.json"), "utf8")) as {
      agentFeedback: { appliedActions: unknown[]; rejectedActions: Array<{ reason: string }> };
    };

    assert.equal(packageJson.scripts.build, "node scripts/build.mjs");
    assert.equal(manifest.agentFeedback.appliedActions.length, 0);
    assert.equal(manifest.agentFeedback.rejectedActions.filter((item) => item.reason.includes("Conflicting")).length, 2);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("reconstruct fails closed for a malformed feedback file", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ai-jsunpack-agent-feedback-invalid-"));
  try {
    const input = path.join(root, "input");
    const output = path.join(root, "output");
    const feedbackPath = path.join(root, "feedback.json");
    await mkdir(input);
    await writeFile(path.join(input, "index.html"), "<main>input</main>");
    await writeFile(
      feedbackPath,
      JSON.stringify({ kind: "agent_feedback", protocolVersion: 1, approvedActions: "invalid" })
    );

    await assert.rejects(
      runCli([
        "reconstruct",
        input,
        "--job-id",
        "job_invalid",
        "--output-dir",
        output,
        "--agent-feedback-file",
        feedbackPath
      ]),
      /sourceReviewArtifactIds/
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

async function runCli(args: string[]): Promise<string> {
  const cliPath = fileURLToPath(new URL("./cli.js", import.meta.url));
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [cliPath, ...args], { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) {
        resolve(stdout);
      } else {
        reject(new Error(stderr || `Core CLI exited with ${code}`));
      }
    });
  });
}
