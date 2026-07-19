#!/usr/bin/env node
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const command = process.argv[2];
const flags = new Set(process.argv.slice(3));
const configFile = argumentValue(process.argv.slice(3), "--config");

const commands = new Set(["api", "web", "worker", "browser-runner", "check"]);

if (!commands.has(command)) {
  usage(command ? `未知命令：${command}` : null);
}

let baseEnv = {
  ...process.env,
  ...loadDotEnv(resolve(repoRoot, ".env")),
};
let applicationConfig = null;
if (configFile) {
  baseEnv.AI_JSUNPACK_CONFIG_FILE = resolve(repoRoot, configFile);
  applicationConfig = loadApplicationConfig(baseEnv);
  baseEnv = {
    ...baseEnv,
    VITE_API_BASE_URL:
      applicationConfig.web?.apiBaseUrl || baseEnv.VITE_API_BASE_URL || "http://127.0.0.1:8000",
    AI_JSUNPACK_DEPLOYMENT_PROFILE:
      applicationConfig.shared?.deploymentProfile || baseEnv.AI_JSUNPACK_DEPLOYMENT_PROFILE || "development",
  };
}

switch (command) {
  case "api":
    run("python", [
      "-m", "uvicorn", "apps.api.app.main:app", "--reload",
      "--host", applicationConfig?.api?.host || "127.0.0.1",
      "--port", String(applicationConfig?.api?.port || 8000)
    ], {
      ...baseEnv,
      AI_JSUNPACK_SERVICE_ROLE: "api",
    });
    break;
  case "web":
    run("npm", ["run", "dev:web:raw"], {
      ...baseEnv,
      VITE_API_AUTH_TOKEN: baseEnv.VITE_API_AUTH_TOKEN || createAuthToken(baseEnv, {
        subject: baseEnv.VITE_API_USER_ID || "local-user",
        kind: "user",
      }),
    });
    break;
  case "worker":
    run("python", ["-m", "apps.worker.worker.queue"], workerEnv(baseEnv, flags));
    break;
  case "browser-runner":
    run("python", ["-m", "uvicorn", "apps.browser_runner.app.main:app", "--host", "127.0.0.1", "--port", "8001"], {
      ...baseEnv,
      AI_JSUNPACK_SERVICE_ROLE: "browser-runner",
      AI_JSUNPACK_BROWSER_RUNNER_QUEUE_BACKEND: "sqlite",
      AI_JSUNPACK_BROWSER_RUNNER_WORKDIR: "tmp/local-dev/browser-runner",
      AI_JSUNPACK_BROWSER_RUNNER_DB_PATH: "tmp/local-dev/browser-runner/browser-runs.sqlite3",
      AI_JSUNPACK_BROWSER_RUNNER_WORKERS: "2",
      AI_JSUNPACK_BROWSER_RUNNER_MAX_ATTEMPTS: "3",
      AI_JSUNPACK_BROWSER_RUNNER_LEASE_SECONDS: "120",
      AI_JSUNPACK_BROWSER_RUNNER_RETRY_BACKOFF_SECONDS: "1",
      AI_JSUNPACK_BROWSER_RUNNER_MAX_QUEUE_AGE_MS: "60000",
      AI_JSUNPACK_BROWSER_RUNNER_MAX_CLAIM_LATENCY_MS: "60000",
      AI_JSUNPACK_BROWSER_RUNNER_MAX_EXPIRED_RUNNING: "0",
      AI_JSUNPACK_BROWSER_RUNNER_MAX_RETRY_RATE: "0.25",
    });
    break;
  case "check":
    runChecks(baseEnv);
    break;
}

function loadDotEnv(path) {
  if (!existsSync(path)) {
    return {};
  }
  const env = {};
  for (const rawLine of readFileSync(path, "utf8").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) {
      continue;
    }
    const equalsIndex = line.indexOf("=");
    if (equalsIndex === -1) {
      continue;
    }
    const key = line.slice(0, equalsIndex).trim();
    let value = line.slice(equalsIndex + 1);
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (key) {
      env[key] = value;
    }
  }
  return env;
}

function argumentValue(args, name) {
  const index = args.indexOf(name);
  if (index === -1) {
    return null;
  }
  const value = args[index + 1];
  if (!value || value.startsWith("-")) {
    usage(`${name} 需要文件路径`);
  }
  return value;
}

function loadApplicationConfig(env) {
  const result = spawnSync(
    "python",
    ["-m", "packages.configuration", "print-effective", env.AI_JSUNPACK_CONFIG_FILE],
    { cwd: repoRoot, env, encoding: "utf8" }
  );
  if (result.status !== 0) {
    const output = [result.stdout, result.stderr].filter(Boolean).join("\n").trim();
    throw new Error(`无法加载应用配置：${output || `退出码 ${result.status}`}`);
  }
  return JSON.parse(result.stdout);
}

function workerEnv(env, optionFlags) {
  const nextEnv = {
    ...env,
    AI_JSUNPACK_SERVICE_ROLE: "worker",
    AI_JSUNPACK_WORKER_ID: env.AI_JSUNPACK_WORKER_ID || "worker-local-1",
    AI_JSUNPACK_WORKER_LEASE_SECONDS: env.AI_JSUNPACK_WORKER_LEASE_SECONDS || "300",
    AI_JSUNPACK_WORKER_POLL_SECONDS: env.AI_JSUNPACK_WORKER_POLL_SECONDS || "5",
    AI_JSUNPACK_WORKER_MAX_ATTEMPTS: env.AI_JSUNPACK_WORKER_MAX_ATTEMPTS || "3",
    AI_JSUNPACK_SANDBOX_RUNNER: env.AI_JSUNPACK_SANDBOX_RUNNER || "local",
    AI_JSUNPACK_CREWAI_DATA_ROOT: env.AI_JSUNPACK_CREWAI_DATA_ROOT || ".crewai-data",
  };

  if (optionFlags.has("--use-browser-runner") || optionFlags.has("-UseBrowserRunner")) {
    nextEnv.AI_JSUNPACK_BROWSER_RUNNER_URL =
      env.AI_JSUNPACK_BROWSER_RUNNER_URL || "http://127.0.0.1:8001";
    nextEnv.AI_JSUNPACK_BROWSER_RUNNER_TOKEN =
      env.AI_JSUNPACK_BROWSER_RUNNER_TOKEN ||
      createAuthToken(env, {
        subject: "worker-local",
        kind: "service",
      });
    nextEnv.AI_JSUNPACK_BROWSER_RUNNER_POLL_SECONDS =
      env.AI_JSUNPACK_BROWSER_RUNNER_POLL_SECONDS || "0.25";
    nextEnv.AI_JSUNPACK_BROWSER_RUNNER_TIMEOUT_MS =
      env.AI_JSUNPACK_BROWSER_RUNNER_TIMEOUT_MS || "60000";
  } else {
    delete nextEnv.AI_JSUNPACK_BROWSER_RUNNER_URL;
    delete nextEnv.AI_JSUNPACK_BROWSER_RUNNER_TOKEN;
  }

  return nextEnv;
}

function createAuthToken(env, { subject, kind }) {
  if (!env.AI_JSUNPACK_AUTH_SECRET) {
    throw new Error("缺少 AI_JSUNPACK_AUTH_SECRET，无法生成本地 Bearer token。");
  }

  const script =
    "from apps.api.app.auth import create_auth_token\n" +
    "import os\n" +
    `print(create_auth_token(subject=${pythonString(subject)}, kind=${pythonString(kind)}, projects={'default':'owner'}, service_roles=${kind === "service" ? "['worker']" : "[]"}, secret=os.environ['AI_JSUNPACK_AUTH_SECRET'], ttl_seconds=86400))\n`;
  const result = spawnSync("python", ["-c", script], {
    cwd: repoRoot,
    env,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    const output = [result.stdout, result.stderr].filter(Boolean).join("\n").trim();
    throw new Error(`生成本地 Bearer token 失败：${output || `退出码 ${result.status}`}`);
  }
  return result.stdout.trim();
}

function pythonString(value) {
  return JSON.stringify(value);
}

function runChecks(env) {
  const checks = [
    ["npm", ["run", "check"]],
    ["npm", ["run", "test:core"]],
    ["python", ["-m", "compileall", "apps", "packages", "tests", "deploy"]],
  ];
  for (const [executable, args] of checks) {
    const command = commandForPlatform(executable, args);
    const result = spawnSync(command.executable, command.args, {
      cwd: repoRoot,
      env,
      stdio: "inherit",
    });
    if (result.status !== 0) {
      process.exit(result.status ?? 1);
    }
  }
}

function run(executable, args, env) {
  const command = commandForPlatform(executable, args);
  const child = spawn(command.executable, command.args, {
    cwd: repoRoot,
    env,
    stdio: "inherit",
  });

  child.on("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code ?? 0);
  });
}

function commandForPlatform(executable, args) {
  if (process.platform === "win32" && executable === "npm") {
    return { executable: "cmd.exe", args: ["/d", "/s", "/c", "npm", ...args] };
  }
  return { executable, args };
}

function usage(error) {
  if (error) {
    console.error(error);
  }
  console.error(`
用法:
  node scripts/dev.mjs api [--config config/ai-jsunpack.yaml]
  node scripts/dev.mjs web [--config config/ai-jsunpack.yaml]
  node scripts/dev.mjs worker [--use-browser-runner] [--config config/ai-jsunpack.yaml]
  node scripts/dev.mjs browser-runner [--config config/ai-jsunpack.yaml]
  node scripts/dev.mjs check
`);
  process.exit(error ? 1 : 0);
}
