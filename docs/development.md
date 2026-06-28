# 开发指南

本文档说明 AI JS Unpack 的仓库结构、开发流程、验证命令和调试建议。本地服务启动细节放在 [本地启动与验证](local-startup.md)。

## 仓库结构

```text
apps/
  api/              FastAPI API、认证、store、Ops、deployment smoke
  browser_runner/   独立 Playwright capture 服务
  web/              React + Vite 工作台
  worker/           Worker queue、pipeline、Agent/runtime/build/package
packages/
  core/             Headless TypeScript 分析与重建 CLI
  shared/           TypeScript 共享契约与示例 fixtures
  sandbox/          sandbox runner 策略和执行边界
  memory/           memory service/context
  knowledge/        knowledge evidence/rules/retriever
  deployment/       服务角色环境变量校验
deploy/
  docker/           服务镜像 Dockerfile
  env/              Compose 环境变量模板
  firecracker/      Firecracker launcher 模板和部署清单
  *.py              compose smoke、release gate、归档校验
tests/              Python 单元和集成测试
docs/               公开文档
```

公开、可长期维护的文档放在 `README.md` 和 `docs/`。本地方案草稿、迁移资料和未公开分析材料放在被忽略的 `dev_docs/`，不要从公开文档链接到该目录。

## 开发环境

安装依赖：

```powershell
npm install
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

可选依赖：

```powershell
.venv\Scripts\python.exe -m playwright install
.venv\Scripts\python.exe -m pip install -e .[dev]
```

创建本地环境文件：

```powershell
Copy-Item .example.env .env
```

本地服务启动优先使用：

```powershell
npm run dev:api
npm run dev:web
npm run dev:worker
npm run dev:browser-runner
```

## 常用验证

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

Core CLI smoke：

```powershell
npm run build
node packages/core/dist/cli.js analyze <inputPath> --job-id <jobId>
node packages/core/dist/cli.js reconstruct <inputPath> --job-id <jobId> --output-dir <dir>
```

## 范围化测试

- API 改动：运行 `tests/test_api_endpoints.py` 相关测试，并检查认证、项目角色、错误响应和下载路径。
- Worker 改动：运行 queue、pipeline、runtime smoke、packaging 和 agent runtime 相关测试。
- Shared contract 改动：同步 TypeScript 类型、Python models 和契约一致性测试。
- Core 改动：运行 `npm run test:core`，必要时补充 CLI analyze/reconstruct fixture。
- Sandbox 改动：验证 runner kind、failure class、resource policy、超时和不降级行为。
- Browser Runner 改动：运行服务测试和 benchmark 相关测试，记录容量或兼容性影响。
- Web 改动：运行 `npm run build:web`，并在桌面和移动宽度做 smoke。
- 部署改动：检查 `deploy/`、`deploy/env/`、Compose 和 release gate 相关测试。

无法运行某项验证时，在最终说明或 PR 描述中写明原因、影响范围和替代检查。

## 调试建议

- 先确认 `.env` 中的 `AI_JSUNPACK_AUTH_SECRET` 与生成 token 使用的 secret 一致。
- Web 若返回 401，检查 `VITE_API_AUTH_TOKEN` 是否为空、过期或不是 `projects.default=owner/maintainer/viewer`。
- API 在 `AI_JSUNPACK_SERVICE_ROLE=api` 下会拒绝 Worker、sandbox、Browser Runner、Core CLI 和模型 provider 配置；启动失败时先检查环境变量污染。
- Worker 空转时，检查 source input artifact、Metadata DB、Artifact Store 路径或 S3/MinIO 配置是否共享。
- Browser Runner 未配置时，Worker 使用本地 Playwright adapter；配置了 `AI_JSUNPACK_BROWSER_RUNNER_URL` 后会走远程服务。
- CrewAI provider 未配置或策略拒绝时，系统应保留 schema-valid best-effort evidence，而不是阻断 deterministic pipeline 的可审计输出。

## 生成产物与忽略目录

以下目录属于本地或生成产物，默认不进入 Git：

- `.venv/`
- `node_modules/`
- `.crewai-data/`
- `artifacts/`
- `uploads/`
- `tmp/`
- `dev_docs/`
- `coverage/`
- `playwright-report/`
- `test-results/`

如果需要保留验证证据，优先写入 `tmp/` 或外部归档位置，再在说明中引用路径和 hash；不要提交真实客户输入、secret、token、生产日志或敏感截图。

## 文档维护

- README 保持项目名片、快速启动、核心能力和文档导航。
- `docs/local-startup.md` 是本地启动细节的唯一详细入口。
- `docs/api.md` 必须与 FastAPI 路由和 Browser Runner 路由一致。
- `docs/deployment.md` 记录服务边界、环境变量、release gate、证据归档和回滚。
- 文档命令必须对应当前 `package.json`、`pyproject.toml`、`deploy/` 或源码入口。
