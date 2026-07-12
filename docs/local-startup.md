# 本地启动与验证

本页给出 Web、API、Worker 和可选 Browser Runner 的可重复本地启动流程。推荐使用 JSON/YAML 描述普通设置，用 `.env` 保存本地 secret 和遗留环境变量。

## 环境要求

- Node.js 20 兼容版本和 npm。
- Python 3.11+。
- 可选 Playwright browsers，用于浏览器验证。
- 可选 Docker，用于容器 sandbox、Compose smoke 和发布演练。

## 1. 安装依赖

```powershell
npm install
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`scripts/dev.mjs` 启动 Python 服务时调用当前 shell 的 `python`。如果依赖安装在 `.venv`，启动 npm dev 命令前必须激活它：

```powershell
.venv\Scripts\Activate.ps1
python --version
```

POSIX shell：

```bash
source .venv/bin/activate
python --version
```

可选工具：

```powershell
python -m playwright install
python -m pip install -e .[dev]
```

## 2. 创建配置

复制启动配置和本地环境模板：

```powershell
Copy-Item config/ai-jsunpack.example.yaml config/ai-jsunpack.yaml
Copy-Item .example.env .env
```

至少替换 `.env` 中的本地 HMAC secret：

```dotenv
AI_JSUNPACK_AUTH_SECRET=choose-a-local-development-secret
```

不要把 Worker 模型凭据直接写入根 `.env`。`scripts/dev.mjs api` 也会加载该文件，而 API strict role 会拒绝 Worker/provider 配置。模型 key 应只在 Worker 终端、`deploy/env/worker.env.example` 对应的部署环境或 secret manager 中设置。

验证配置：

```powershell
python -m packages.configuration validate config/ai-jsunpack.yaml
python -m packages.configuration print-effective config/ai-jsunpack.yaml
```

配置模型和优先级见 [配置指南](configuration.md)。

## 3. 启动服务

每个命令在独立且已激活 `.venv` 的终端中运行：

```powershell
node scripts/dev.mjs api --config config/ai-jsunpack.yaml
node scripts/dev.mjs web --config config/ai-jsunpack.yaml
node scripts/dev.mjs worker --config config/ai-jsunpack.yaml
```

等价 npm scripts：

```powershell
npm run dev:api -- --config config/ai-jsunpack.yaml
npm run dev:web -- --config config/ai-jsunpack.yaml
npm run dev:worker -- --config config/ai-jsunpack.yaml
```

默认地址：

- Web：`http://127.0.0.1:5173`
- API：`http://127.0.0.1:8000`

Web 启动时，如果 `VITE_API_AUTH_TOKEN` 为空，脚本会用 `.env` 中的 `AI_JSUNPACK_AUTH_SECRET` 自动生成 `projects.default=owner` 的 user token。

## 4. 可选 Browser Runner

启动独立浏览器服务：

```powershell
node scripts/dev.mjs browser-runner --config config/ai-jsunpack.yaml
```

再让 Worker 使用它：

```powershell
node scripts/dev.mjs worker --use-browser-runner --config config/ai-jsunpack.yaml
```

默认地址为 `http://127.0.0.1:8001`。开发脚本会为 Worker 生成 service token，并设置远程 runner URL、poll interval 和 timeout。

未使用 `--use-browser-runner` 时，开发 profile 的 Worker 使用本地 Playwright adapter。生产 profile 禁止该 fallback。

## 5. 健康检查

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8001/health
```

API 预期返回：

```json
{
  "status": "ok",
  "serviceRole": "api",
  "deploymentProfile": "ok"
}
```

这里的 `deploymentProfile` 是服务角色校验状态（例如 `ok`、`warning` 或 `invalid`），不是启动配置中的 `shared.deploymentProfile` 值。

还可以打开：

- Web：`http://127.0.0.1:5173`
- API OpenAPI：`http://127.0.0.1:8000/docs`
- 设置中心：`http://127.0.0.1:5173/settings/general`

## 生成本地 Token

手动生成 token 时必须读取与服务相同的 `AI_JSUNPACK_AUTH_SECRET`，不要硬编码另一份值。

先加载 `.env`：

```powershell
Get-Content .env | ForEach-Object {
  if ($_ -and -not $_.TrimStart().StartsWith("#")) {
    $name, $value = $_.Split("=", 2)
    Set-Item -Path "Env:$name" -Value $value
  }
}
```

Web/API user token：

```powershell
$env:VITE_API_AUTH_TOKEN = python -c "from apps.api.app.auth import create_auth_token; import os; print(create_auth_token(subject='local-user', projects={'default':'owner'}, secret=os.environ['AI_JSUNPACK_AUTH_SECRET'], ttl_seconds=86400))"
```

Worker service token：

```powershell
$env:AI_JSUNPACK_BROWSER_RUNNER_TOKEN = python -c "from apps.api.app.auth import create_auth_token; import os; print(create_auth_token(subject='worker-local', kind='service', projects={'default':'owner'}, service_roles=['worker'], secret=os.environ['AI_JSUNPACK_AUTH_SECRET'], ttl_seconds=86400))"
```

## 手动启动

只有排查脚本或环境变量时才建议绕过 `scripts/dev.mjs`。

以下每个服务都运行在独立终端。每个终端先激活 `.venv`，再加载 `.env`；否则 `AI_JSUNPACK_AUTH_SECRET`、数据库 URL 和 Artifact Store 路径不会自动存在：

```powershell
.venv\Scripts\Activate.ps1
Get-Content .env | ForEach-Object {
  if ($_ -and -not $_.TrimStart().StartsWith("#")) {
    $name, $value = $_.Split("=", 2)
    Set-Item -Path "Env:$name" -Value $value
  }
}
```

API：

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "api"
$env:AI_JSUNPACK_CONFIG_FILE = "config/ai-jsunpack.yaml"
python -m uvicorn apps.api.app.main:app --reload --host 127.0.0.1 --port 8000
```

Worker：

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "worker"
$env:AI_JSUNPACK_CONFIG_FILE = "config/ai-jsunpack.yaml"
$env:AI_JSUNPACK_WORKER_ID = "worker-local-1"
$env:AI_JSUNPACK_WORKER_LEASE_SECONDS = "300"
$env:AI_JSUNPACK_WORKER_POLL_SECONDS = "5"
$env:AI_JSUNPACK_WORKER_MAX_ATTEMPTS = "3"
$env:AI_JSUNPACK_CREWAI_DATA_ROOT = ".crewai-data"
python -m apps.worker.worker.queue
```

Browser Runner：

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "browser-runner"
$env:AI_JSUNPACK_CONFIG_FILE = "config/ai-jsunpack.yaml"
$env:AI_JSUNPACK_BROWSER_RUNNER_QUEUE_BACKEND = "sqlite"
$env:AI_JSUNPACK_BROWSER_RUNNER_WORKDIR = "tmp/local-dev/browser-runner"
$env:AI_JSUNPACK_BROWSER_RUNNER_DB_PATH = "tmp/local-dev/browser-runner/browser-runs.sqlite3"
python -m uvicorn apps.browser_runner.app.main:app --host 127.0.0.1 --port 8001
```

Web：

```powershell
$env:VITE_API_BASE_URL = "http://127.0.0.1:8000"
$env:VITE_API_USER_ID = "local-user"
$env:VITE_API_PROJECT_ID = "default"
$env:VITE_API_AUTH_TOKEN = python -c "from apps.api.app.auth import create_auth_token; import os; print(create_auth_token(subject='local-user', projects={'default':'owner'}, secret=os.environ['AI_JSUNPACK_AUTH_SECRET'], ttl_seconds=86400))"
npm run dev:web:raw
```

## Agent Provider

Agent Runtime 只在 Worker 内运行。

云端或脱敏模式：

```powershell
$env:AI_JSUNPACK_AGENT_PROVIDER = "openai-compatible"
$env:AI_JSUNPACK_AGENT_MODEL = "<model-name>"
$env:AI_JSUNPACK_AGENT_BASE_URL = "https://agent.example.com/v1"
$env:AI_JSUNPACK_AGENT_API_KEY = "<worker-only-secret>"
```

本地模式：

```powershell
$env:AI_JSUNPACK_LOCAL_AGENT_PROVIDER = "openai-compatible"
$env:AI_JSUNPACK_LOCAL_AGENT_MODEL = "qwen3-coder"
$env:AI_JSUNPACK_LOCAL_AGENT_BASE_URL = "http://127.0.0.1:11434/v1"
$env:AI_JSUNPACK_LOCAL_AGENT_API_KEY = ""
```

OpenAI-compatible endpoint 必须返回 `choices[0].message.content`。超时、HTTP error 或响应结构错误记录为 `agent_failed` evidence；确定性 Core、构建验证和 packaging 仍尽量保留证据。

## Core CLI

```powershell
npm run build
node packages/core/dist/cli.js analyze <inputPath> --job-id <jobId>
node packages/core/dist/cli.js reconstruct <inputPath> --job-id <jobId> --output-dir <dir>
```

支持的 `inputPath`：

- 目录。
- 单个 `.js`、`.mjs`、`.cjs`。
- `.zip`、`.tar`、`.tar.gz`、`.tgz`。

归档最多 10,000 个成员、单文件 64 MiB、总解压 256 MiB、压缩比 200:1，并拒绝路径穿越、绝对路径、Windows drive/UNC、链接和未知成员类型。

## 回归验证

轻量聚合检查：

```powershell
npm run dev:check
```

它只运行 TypeScript check、Core tests 和 Python compileall，不是完整的发布门禁。

完整基础检查：

```powershell
npm run check
npm run test:core
npm run build:web
python -m compileall apps packages tests deploy
python -m unittest discover -s tests
```

可选静态检查：

```powershell
python -m ruff check apps packages tests deploy
python -m bandit -c pyproject.toml -r apps packages deploy -x tests
```

轻量 deployment smoke：

```powershell
python -m apps.api.app.deployment_smoke --output tmp\deployment-smoke.json
```

## 本地产物

- Metadata DB：`tmp/local-dev/metadata.db`
- Artifact：`tmp/local-dev/artifacts`
- Browser Runner DB：`tmp/local-dev/browser-runner/browser-runs.sqlite3`
- CrewAI 数据：`.crewai-data`

这些目录以及 `.env`、`.venv`、`node_modules` 都不应提交。

## 常见问题

- npm dev 命令提示缺少 Python 包：激活 `.venv`；脚本使用的是当前 shell 的 `python`。
- Web 返回 401：确认 token 与 API 使用同一 `AI_JSUNPACK_AUTH_SECRET`，并包含 `projects.default` 角色。
- API 启动时拒绝环境变量：移除 Worker、sandbox、Browser Runner、Core CLI 或 provider 变量，API strict role 不允许携带这些权限。
- Worker 空转：确认 source input、Metadata DB 和 Artifact Store 指向相同位置。
- `policy_denied`：核对 Job `cloudMode`、模型字段和 Worker provider 凭据。
- production 下浏览器验证失败：配置远程 Browser Runner；本地 Playwright fallback 被有意禁用。
