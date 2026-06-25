# 开发指南

本文档说明 AI JS Unpack 的本地环境搭建、服务启动、调试和验证流程。

## 环境要求

- Node.js 20 或兼容版本
- npm
- Python 3.11+
- 可选：Playwright browsers
- 可选：Docker 或 Podman，用于容器 sandbox 和部署演练

## 安装依赖

```powershell
npm install
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果需要运行浏览器验证：

```powershell
.venv\Scripts\python.exe -m playwright install
```

## 本地认证 Token

API 使用 HMAC-SHA256 Bearer token。开发环境可以使用固定 secret 生成临时 token。

用户 token：

```powershell
$env:AI_JSUNPACK_AUTH_SECRET = "dev-secret"
$env:VITE_API_AUTH_TOKEN = .venv\Scripts\python.exe -c "from apps.api.app.auth import create_auth_token; print(create_auth_token(subject='local-user', projects={'default':'owner'}, secret='dev-secret', ttl_seconds=86400))"
```

Worker service token：

```powershell
$env:AI_JSUNPACK_BROWSER_RUNNER_TOKEN = .venv\Scripts\python.exe -c "from apps.api.app.auth import create_auth_token; print(create_auth_token(subject='worker-local', kind='service', projects={'default':'owner'}, service_roles=['worker'], secret='dev-secret', ttl_seconds=86400))"
```

## 启动服务

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
npm run dev:web
```

Worker：

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "worker"
$env:AI_JSUNPACK_AUTH_SECRET = "dev-secret"
.venv\Scripts\python.exe -m apps.worker.worker.queue
```

Browser Runner：

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "browser-runner"
$env:AI_JSUNPACK_AUTH_SECRET = "dev-secret"
.venv\Scripts\python.exe -m uvicorn apps.browser_runner.app.main:app --host 127.0.0.1 --port 8001
```

默认 Web 地址是 `http://127.0.0.1:5173`，默认 API 地址是 `http://127.0.0.1:8000`。

## Core CLI 调试

Core 可以独立分析输入目录或压缩包：

```powershell
npm run build
node packages/core/dist/cli.js analyze <inputPath> --job-id <jobId>
node packages/core/dist/cli.js reconstruct <inputPath> --job-id <jobId> --output-dir <dir>
```

支持目录、`.zip`、`.tar`、`.tar.gz` 和 `.tgz`。压缩包会执行路径安全检查，拒绝绝对路径、Windows drive/UNC 路径、路径穿越、zip symlink、tar link 和不支持的压缩成员。

## 常用验证

```powershell
npm run check
npm run test:core
npm run build:web
.venv\Scripts\python.exe -m compileall apps packages tests
.venv\Scripts\python.exe -m unittest discover -s tests
```

前端改动后建议启动 `npm run dev:web`，在浏览器检查无应用错误、无资源 404，桌面和移动宽度下内容不重叠。

## 调试建议

- 先确认 `AI_JSUNPACK_AUTH_SECRET` 和前端 `VITE_API_AUTH_TOKEN` 使用同一个 secret 生成。
- 本地开发未显式配置数据库时，后端会使用项目默认的轻量存储路径。
- Worker 需要能读取 source input artifact，并能写入生成的 analysis、runtime 和 packaging artifacts。
- Browser Runner 是可选边界；没有配置远程 runner 时，Worker 会使用本地 Playwright adapter。
- CrewAI provider 未配置或策略拒绝时，系统应保留 schema-valid 的 best-effort evidence。
