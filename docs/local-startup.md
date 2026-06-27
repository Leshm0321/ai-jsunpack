# 本地启动与验证

本文档说明 API、Web、Worker 以及可选 Browser Runner 服务的本地开发流程。

## 环境变量文件

仓库包含以下本地环境文件：

- `.env`：本地开发默认值。该文件已被 Git 忽略，只应包含本地占位值。
- `.example.env`：提交到仓库的新环境模板。

不要把 `AI_JSUNPACK_SERVICE_ROLE`、`AI_JSUNPACK_SANDBOX_*`、`AI_JSUNPACK_AGENT_*` 或 `AI_JSUNPACK_BROWSER_RUNNER_*` 写入根目录 `.env`。API 以 `AI_JSUNPACK_SERVICE_ROLE=api` 启动时会拒绝 Worker 或 Browser Runner 变量，因此每个服务终端应在启动前临时设置对应服务变量。

在 PowerShell 中加载 `.env`：

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
.venv\Scripts\python.exe -m playwright install
```

如需运行静态检查：

```powershell
.venv\Scripts\python.exe -m pip install -e .[dev]
```

## 生成本地 Token

API 使用由 `AI_JSUNPACK_AUTH_SECRET` 签名的 HMAC Bearer token。

Web/API 用户 token：

```powershell
$env:VITE_API_AUTH_TOKEN = .venv\Scripts\python.exe -c "from apps.api.app.auth import create_auth_token; print(create_auth_token(subject='local-user', projects={'default':'owner'}, secret='dev-secret', ttl_seconds=86400))"
```

Worker 调用 Browser Runner 的 service token：

```powershell
$env:AI_JSUNPACK_BROWSER_RUNNER_TOKEN = .venv\Scripts\python.exe -c "from apps.api.app.auth import create_auth_token; print(create_auth_token(subject='worker-local', kind='service', projects={'default':'owner'}, service_roles=['worker'], secret='dev-secret', ttl_seconds=86400))"
```

## 启动服务

加载 `.env` 后，在独立终端中启动每个服务。

API:

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "api"
.venv\Scripts\python.exe -m uvicorn apps.api.app.main:app --reload --host 127.0.0.1 --port 8000
```

Web:

```powershell
npm run dev:web
```

Worker:

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "worker"
$env:AI_JSUNPACK_WORKER_ID = "worker-local-1"
$env:AI_JSUNPACK_WORKER_LEASE_SECONDS = "300"
$env:AI_JSUNPACK_WORKER_POLL_SECONDS = "5"
$env:AI_JSUNPACK_WORKER_MAX_ATTEMPTS = "3"
$env:AI_JSUNPACK_SANDBOX_RUNNER = "local"
$env:AI_JSUNPACK_CREWAI_DATA_ROOT = ".crewai-data"
.venv\Scripts\python.exe -m apps.worker.worker.queue
```

可选 Browser Runner：

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "browser-runner"
$env:AI_JSUNPACK_BROWSER_RUNNER_QUEUE_BACKEND = "sqlite"
$env:AI_JSUNPACK_BROWSER_RUNNER_WORKDIR = "tmp/local-dev/browser-runner"
$env:AI_JSUNPACK_BROWSER_RUNNER_DB_PATH = "tmp/local-dev/browser-runner/browser-runs.sqlite3"
$env:AI_JSUNPACK_BROWSER_RUNNER_WORKERS = "2"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_ATTEMPTS = "3"
$env:AI_JSUNPACK_BROWSER_RUNNER_LEASE_SECONDS = "120"
$env:AI_JSUNPACK_BROWSER_RUNNER_RETRY_BACKOFF_SECONDS = "1"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_QUEUE_AGE_MS = "60000"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_CLAIM_LATENCY_MS = "60000"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_EXPIRED_RUNNING = "0"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_RETRY_RATE = "0.25"
.venv\Scripts\python.exe -m uvicorn apps.browser_runner.app.main:app --host 127.0.0.1 --port 8001
```

如果要让 Worker 使用 Browser Runner，在启动 Worker 前在 Worker 终端设置以下变量：

```powershell
$env:AI_JSUNPACK_BROWSER_RUNNER_URL = "http://127.0.0.1:8001"
$env:AI_JSUNPACK_BROWSER_RUNNER_TOKEN = "<worker service token>"
$env:AI_JSUNPACK_BROWSER_RUNNER_POLL_SECONDS = "0.25"
$env:AI_JSUNPACK_BROWSER_RUNNER_TIMEOUT_MS = "60000"
```

如果 Browser Runner 未运行，在 Worker 终端保持 `AI_JSUNPACK_BROWSER_RUNNER_URL` 未设置，使 Worker 使用本地 Playwright adapter：

```powershell
Remove-Item Env:AI_JSUNPACK_BROWSER_RUNNER_URL -ErrorAction SilentlyContinue
Remove-Item Env:AI_JSUNPACK_BROWSER_RUNNER_TOKEN -ErrorAction SilentlyContinue
```

## 健康检查

API:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Browser Runner:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
```

Web:

```powershell
Start-Process http://127.0.0.1:5173
```

## 回归验证

交付代码改动前运行：

```powershell
npm run check
npm run test:core
.venv\Scripts\python.exe -m unittest discover tests
```

运行静态检查：

```powershell
.venv\Scripts\python.exe -m ruff check apps packages tests deploy
.venv\Scripts\python.exe -m bandit -c pyproject.toml -r apps packages deploy -x tests
```

运行编译检查：

```powershell
.venv\Scripts\python.exe -m compileall apps packages tests
```

## 端到端 Smoke

Docker Compose 演练：

```powershell
.venv\Scripts\python.exe -m deploy.compose_smoke `
  --output tmp\deployment-compose-smoke\compose-smoke.json `
  --artifact-root tmp\deployment-compose-smoke\artifacts `
  --soak-runs 10
```

报告中 `status` 为 `pass` 时表示通过。

不依赖 Docker 的轻量 API/Worker smoke：

```powershell
.venv\Scripts\python.exe -m apps.api.app.deployment_smoke --output tmp\deployment-smoke.json
```

## 预期本地产物位置

- Metadata DB：`tmp/local-dev/metadata.db`
- Artifact 文件：`tmp/local-dev/artifacts`
- Browser Runner 队列 DB：`tmp/local-dev/browser-runner/browser-runs.sqlite3`
- CrewAI 本地数据：`.crewai-data`

## 注意事项

- `.env` 仅用于本地开发。
- `.example.env` 是提交到仓库的模板。
- 生产 secret 必须来自 CI 或 secret manager。
- 本地 sandbox runner 只用于开发和审计，不提供生产多租户隔离。
