# 本地启动与验证

本文档说明 API、Web、Worker 以及可选 Browser Runner 的本地开发流程。推荐优先使用 `scripts/dev.mjs` 或对应 npm script；只有排查环境变量时才手动启动服务。

## 环境要求

- Node.js 20 兼容版本和 npm
- Python 3.11+
- 可选：Playwright browsers，用于本地或远程浏览器验证
- 可选：Docker 或 Podman，用于容器 sandbox、Compose smoke 和部署演练

## 环境变量文件

仓库包含两个本地环境入口：

- `.example.env`：提交到仓库的模板。
- `.env`：本地开发值，已被 Git 忽略，只应包含本地占位 secret 和本机路径。

创建本地文件：

```powershell
Copy-Item .example.env .env
```

不要把 `AI_JSUNPACK_SERVICE_ROLE`、`AI_JSUNPACK_SANDBOX_*`、`AI_JSUNPACK_AGENT_*`、`AI_JSUNPACK_LOCAL_AGENT_*` 或 `AI_JSUNPACK_BROWSER_RUNNER_*` 写入根目录 `.env`。API 以 `AI_JSUNPACK_SERVICE_ROLE=api` 启动时会拒绝 Worker、Browser Runner、sandbox、Core CLI 或模型 provider 侧配置，因此每个服务终端应在启动前临时设置自己的服务变量。

PowerShell 手动加载 `.env`：

```powershell
Get-Content .env | ForEach-Object {
  if ($_ -and -not $_.TrimStart().StartsWith("#")) {
    $name, $value = $_.Split("=", 2)
    Set-Item -Path "Env:$name" -Value $value
  }
}
```

## 安装依赖

```powershell
npm install
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如需运行 Playwright 浏览器验证：

```powershell
.venv\Scripts\python.exe -m playwright install
```

如需运行 Ruff 或 Bandit：

```powershell
.venv\Scripts\python.exe -m pip install -e .[dev]
```

## 推荐启动方式

`scripts/dev.mjs` 会自动加载 `.env`，按服务补齐本地开发所需环境变量。Web 启动时如果 `VITE_API_AUTH_TOKEN` 为空，脚本会使用 `AI_JSUNPACK_AUTH_SECRET` 生成本地 owner token。

```powershell
npm run dev:api
npm run dev:web
npm run dev:worker
```

可选 Browser Runner：

```powershell
npm run dev:browser-runner
```

如果要让 Worker 使用 Browser Runner，先启动 Browser Runner，再启动 Worker：

```powershell
node scripts/dev.mjs worker --use-browser-runner
```

等价的直接脚本入口：

```powershell
node scripts/dev.mjs api
node scripts/dev.mjs web
node scripts/dev.mjs worker
node scripts/dev.mjs browser-runner
node scripts/dev.mjs check
```

Windows wrapper：

```powershell
.\scripts\dev.ps1 api
.\scripts\dev.cmd api
```

默认地址：

- Web：`http://127.0.0.1:5173`
- API：`http://127.0.0.1:8000`
- Browser Runner：`http://127.0.0.1:8001`

## 生成本地 Token

API 使用由 `AI_JSUNPACK_AUTH_SECRET` 签名的 HMAC-SHA256 Bearer token。

Web/API 用户 token：

```powershell
$env:VITE_API_AUTH_TOKEN = .venv\Scripts\python.exe -c "from apps.api.app.auth import create_auth_token; print(create_auth_token(subject='local-user', projects={'default':'owner'}, secret='dev-secret', ttl_seconds=86400))"
```

Worker 调用 Browser Runner 的 service token：

```powershell
$env:AI_JSUNPACK_BROWSER_RUNNER_TOKEN = .venv\Scripts\python.exe -c "from apps.api.app.auth import create_auth_token; print(create_auth_token(subject='worker-local', kind='service', projects={'default':'owner'}, service_roles=['worker'], secret='dev-secret', ttl_seconds=86400))"
```

## 手动启动服务

通常优先使用脚本。需要排查环境变量问题时，可在独立终端中手动启动每个服务。

API：

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "api"
$env:AI_JSUNPACK_AUTH_SECRET = "dev-secret"
.venv\Scripts\python.exe -m uvicorn apps.api.app.main:app --reload --host 127.0.0.1 --port 8000
```

Web：

```powershell
$env:VITE_API_BASE_URL = "http://127.0.0.1:8000"
$env:VITE_API_USER_ID = "local-user"
$env:VITE_API_PROJECT_ID = "default"
$env:VITE_API_AUTH_TOKEN = "<user bearer token>"
npm run dev:web
```

Worker：

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "worker"
$env:AI_JSUNPACK_AUTH_SECRET = "dev-secret"
$env:AI_JSUNPACK_WORKER_ID = "worker-local-1"
$env:AI_JSUNPACK_WORKER_LEASE_SECONDS = "300"
$env:AI_JSUNPACK_WORKER_POLL_SECONDS = "5"
$env:AI_JSUNPACK_WORKER_MAX_ATTEMPTS = "3"
$env:AI_JSUNPACK_SANDBOX_RUNNER = "local"
$env:AI_JSUNPACK_CREWAI_DATA_ROOT = ".crewai-data"
.venv\Scripts\python.exe -m apps.worker.worker.queue
```

Browser Runner：

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "browser-runner"
$env:AI_JSUNPACK_AUTH_SECRET = "dev-secret"
$env:AI_JSUNPACK_BROWSER_RUNNER_QUEUE_BACKEND = "sqlite"
$env:AI_JSUNPACK_BROWSER_RUNNER_WORKDIR = "tmp/local-dev/browser-runner"
$env:AI_JSUNPACK_BROWSER_RUNNER_DB_PATH = "tmp/local-dev/browser-runner/browser-runs.sqlite3"
$env:AI_JSUNPACK_BROWSER_RUNNER_WORKERS = "2"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_ATTEMPTS = "3"
$env:AI_JSUNPACK_BROWSER_RUNNER_LEASE_SECONDS = "120"
$env:AI_JSUNPACK_BROWSER_RUNNER_RETRY_BACKOFF_SECONDS = "1"
$env:AI_JSUNPACK_BROWSER_RUNNER_POLL_SECONDS = "0.25"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_QUEUE_AGE_MS = "60000"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_CLAIM_LATENCY_MS = "60000"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_EXPIRED_RUNNING = "0"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_RETRY_RATE = "0.25"
.venv\Scripts\python.exe -m uvicorn apps.browser_runner.app.main:app --host 127.0.0.1 --port 8001
```

让 Worker 使用 Browser Runner：

```powershell
$env:AI_JSUNPACK_BROWSER_RUNNER_URL = "http://127.0.0.1:8001"
$env:AI_JSUNPACK_BROWSER_RUNNER_TOKEN = "<worker service token>"
$env:AI_JSUNPACK_BROWSER_RUNNER_POLL_SECONDS = "0.25"
$env:AI_JSUNPACK_BROWSER_RUNNER_TIMEOUT_MS = "60000"
```

让 Worker 回到本地 Playwright adapter：

```powershell
Remove-Item Env:AI_JSUNPACK_BROWSER_RUNNER_URL -ErrorAction SilentlyContinue
Remove-Item Env:AI_JSUNPACK_BROWSER_RUNNER_TOKEN -ErrorAction SilentlyContinue
```

## 可选 Agent 模型配置

Agent Runtime 在 Worker 进程中运行。不要把模型变量或第三方 provider 凭据放进 API 环境；API strict mode 会拒绝这些配置。

未配置模型时，Agent pass 会写入 `policy_denied` 或 best-effort evidence，Core 分析、重建、build/typecheck 和 artifact lineage 仍会继续保留可审计输出。

cloud_allowed 或 desensitized Job 的最小云端模型示例：

```powershell
$env:AI_JSUNPACK_AGENT_PROVIDER = "openai"
$env:AI_JSUNPACK_AGENT_MODEL = "gpt-4o-mini"
$env:OPENAI_API_KEY = "<worker-only secret>"
```

local_only Job 的本地模型示例：

```powershell
$env:AI_JSUNPACK_LOCAL_AGENT_PROVIDER = "ollama"
$env:AI_JSUNPACK_LOCAL_AGENT_MODEL = "ollama/llama3.1"
$env:OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
```

OpenAI Chat Completions 兼容的私有 endpoint 示例：

```powershell
$env:AI_JSUNPACK_AGENT_PROVIDER = "openai-compatible"
$env:AI_JSUNPACK_AGENT_MODEL = "private-model"
$env:AI_JSUNPACK_AGENT_BASE_URL = "https://agent.example.com"
$env:AI_JSUNPACK_AGENT_API_KEY = "<worker-only endpoint secret>"
$env:AI_JSUNPACK_AGENT_TIMEOUT_SECONDS = "30"
$env:AI_JSUNPACK_AGENT_TEMPERATURE = "0.2"
```

本地或私有 local_only endpoint 示例：

```powershell
$env:AI_JSUNPACK_LOCAL_AGENT_PROVIDER = "openai-compatible"
$env:AI_JSUNPACK_LOCAL_AGENT_MODEL = "local-private-model"
$env:AI_JSUNPACK_LOCAL_AGENT_BASE_URL = "http://127.0.0.1:11434/v1"
$env:AI_JSUNPACK_LOCAL_AGENT_API_KEY = ""
```

也可以在创建 Job 时通过 `config.agentModel`、`config.agentModelProvider`、`config.localAgentModel` 或 `config.localAgentProvider` 覆盖模型和 provider。自定义 endpoint 的 base URL、API key、timeout 和 temperature 只从 Worker 环境读取，不从 Job config 读取。CrewAI 默认路径实际使用模型字符串；provider 名称主要用于 policy/audit 证据。配置 `provider=openai-compatible` 且有 base URL 时，Worker 会通过自定义 `BaseLLM` 适配器按 OpenAI Chat Completions 协议发送 `model/messages/temperature/tools`。endpoint 超时、HTTP error 或响应缺少 `choices[0].message.content` 时，Agent evidence 会记录 `agent_failed`，deterministic Core、重建、build/typecheck 和 packaging 证据链仍继续保留。

## 健康检查

API：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Browser Runner：

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
```

Web：

```powershell
Start-Process http://127.0.0.1:5173
```

## 回归验证

基础验证：

```powershell
npm run check
npm run test:core
npm run build:web
.venv\Scripts\python.exe -m compileall apps packages tests deploy
.venv\Scripts\python.exe -m unittest discover -s tests
```

脚本封装：

```powershell
npm run dev:check
```

静态检查：

```powershell
.venv\Scripts\python.exe -m ruff check apps packages tests deploy
.venv\Scripts\python.exe -m bandit -c pyproject.toml -r apps packages deploy -x tests
```

## 端到端 Smoke

不依赖 Docker 的轻量 API/Worker smoke：

```powershell
.venv\Scripts\python.exe -m apps.api.app.deployment_smoke --output tmp\deployment-smoke.json
```

Docker Compose 演练：

```powershell
.venv\Scripts\python.exe -m deploy.compose_smoke `
  --output tmp\deployment-compose-smoke\compose-smoke.json `
  --artifact-root tmp\deployment-compose-smoke\artifacts `
  --soak-runs 10
```

报告中 `status=pass` 表示通过。

## 预期本地产物

- Metadata DB：`tmp/local-dev/metadata.db`
- Artifact 文件：`tmp/local-dev/artifacts`
- Browser Runner 队列 DB：`tmp/local-dev/browser-runner/browser-runs.sqlite3`
- CrewAI 本地数据：`.crewai-data`

## 注意事项

- `.env` 只用于本地开发，不提交真实 secret。
- `.example.env` 是可提交模板。
- 生产 secret 必须来自 CI 或 secret manager。
- 本地 sandbox runner 只用于开发和审计，不提供生产多租户隔离。
