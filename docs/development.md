# 开发指南

本页面向修改实现、契约、测试和文档的贡献者。启动服务的操作步骤见 [本地启动与验证](local-startup.md)，配置模型见 [配置指南](configuration.md)。

## 仓库结构

```text
apps/
  api/              FastAPI、认证、Job/Artifact、Settings、Ops
  browser_runner/   独立 Playwright capture 与持久队列
  web/              React/Vite 官网、工作台和设置中心
  worker/           Queue、Core bridge、Agent DAG、验证与 packaging
packages/
  shared/           TypeScript 契约和状态事实源
  core/             Headless TypeScript 分析与重建 CLI
  configuration/    JSON/YAML、环境覆盖、运行时设置与脱敏
  sandbox/          local/container/gVisor/Firecracker 执行策略
  memory/           任务、项目、实体和场景记忆
  knowledge/        确定性知识检索与 evidence refs
  audit/            审计、报告和 lineage 包边界
  deployment/       服务角色与部署配置校验
deploy/
  docker/           服务镜像 Dockerfile
  env/              各服务环境模板
  firecracker/      Firecracker launcher 契约
  *.py              smoke、release gate、证据与归档工具
config/             可提交的 JSON/YAML 示例
scripts/            本地开发入口
tests/              Python 单元与集成测试
docs/               产品、API、开发和运维中心文档
```

`deploy/**/README.md` 和 `packages/*/README.md` 是局部参考；中心信息架构从 [docs/README.md](README.md) 进入。`dev_docs/` 是被忽略的本地资料，不是公开事实源。

## 开发循环

1. 查看 `git status --short`，区分现有用户改动和本次范围。
2. 阅读目标实现、相邻契约和覆盖测试。
3. 先运行最小可证明现状的测试，再修改。
4. 保持变更小而可审查；行为变化同步测试和文档。
5. 先运行定向验证，再运行适合风险等级的全局检查。
6. 最终检查 diff、未跟踪文件、生成物和 secret。

## 安装与启动

```powershell
npm install
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\Activate.ps1
Copy-Item config/ai-jsunpack.example.yaml config/ai-jsunpack.yaml
Copy-Item .example.env .env
```

启动命令：

```powershell
node scripts/dev.mjs api --config config/ai-jsunpack.yaml
node scripts/dev.mjs web --config config/ai-jsunpack.yaml
node scripts/dev.mjs worker --config config/ai-jsunpack.yaml
node scripts/dev.mjs browser-runner --config config/ai-jsunpack.yaml
```

开发脚本调用当前 shell 的 `python`，因此虚拟环境必须处于激活状态。

## 验证层级

### 快速反馈

```powershell
npm run check
npm run test:core
python -m compileall apps packages tests deploy
```

`npm run dev:check` 顺序执行以上三类检查，但不运行 Web production build、Python unit tests、Ruff 或 Bandit。

### 基础回归

```powershell
npm run check
npm run test:core
npm run build:web
python -m compileall apps packages tests deploy
python -m unittest discover -s tests
```

### 静态与安全检查

先安装 dev extras：

```powershell
python -m pip install -e .[dev]
python -m ruff check apps packages tests deploy
python -m bandit -c pyproject.toml -r apps packages deploy -x tests
```

### 部署验证

```powershell
python -m apps.api.app.deployment_smoke --output tmp\deployment-smoke.json
python -m deploy.compose_smoke --dry-run --output tmp\deployment-compose-smoke\dry-run.json
docker compose -f deploy/docker-compose.yml config --quiet
```

真实 Compose smoke 需要 Docker daemon、可用端口和足够资源，见 [部署指南](deployment.md)。

## 按改动选择测试

| 改动 | 最小验证 |
| --- | --- |
| `packages/core` | `npm run test:core`，必要时 CLI analyze/reconstruct fixture |
| `packages/shared` | TypeScript build、Python 契约测试、API/Web 消费方 |
| `packages/configuration` | `tests/test_configuration.py`、`tests/test_settings_api.py`、样例 validate |
| `apps/api` | 认证、角色、设置、Job、下载、retention、Ops endpoint tests |
| `apps/worker` | queue、pipeline、Agent DAG、build/runtime、packaging tests |
| `apps/browser_runner` | service、queue backend、lease recovery、metrics、benchmark |
| `packages/sandbox` | runner、resource policy、timeout、failure class、fail-closed |
| `apps/web` | `npm run check`、`npm run build:web`、桌面/移动 smoke |
| `deploy/` | compose config、相关 Python tests、dry-run、必要时真实 smoke |
| `docs/` | 链接、路径、命令、API 路由和 UTF-8 检查 |

无法运行某项验证时，最终说明必须写明原因、影响和替代证据。

## Core 与输入处理

```powershell
npm run build
node packages/core/dist/cli.js analyze <inputPath> --job-id <jobId>
node packages/core/dist/cli.js reconstruct <inputPath> --job-id <jobId> --output-dir <dir>
```

Core 支持目录、单个 JavaScript 文件和受限归档。修改输入规范化时，必须覆盖：

- 扩展名与 source kind。
- 临时目录 cleanup。
- 路径穿越、绝对路径、drive/UNC 和 link 拒绝。
- 成员数量、单文件大小、总大小和压缩比上限。
- 目录、单脚本、ZIP、TAR、TAR.GZ/TGZ fixtures。

## API 与契约

- FastAPI endpoint 公开字段使用共享 camelCase 契约。
- 新增路由时同步 `docs/api.md` 和 Web client。
- 修改 Job status、Artifact kind、failure class、sandbox kind 时先更新 `packages/shared`，再同步 Python model 和测试。
- Settings 更新使用 append-only revision 和乐观锁；回滚也是新 revision。
- API 输出不得包含 secret 值，只能返回脱敏字段、secret ref 或配置状态。

## Worker 与 Agent DAG

Worker pipeline 的正常阶段由 `apps/worker/worker/pipeline.py` 定义。Agent Runtime 的依赖图由 `CrewRuntimePlanner` 构造，必须保持：

- 节点名唯一。
- 依赖只指向更早阶段。
- 不存在环。
- 并行 specialist 只返回自己的结构化范围。
- 失败依赖导致下游 `skipped`，而不是使用缺失证据继续推断。
- Repair 指令经过 deterministic allowlist，不直接自由修改工程。

如果新增 Settings 字段，还必须实现并测试 Worker 消费逻辑；仅增加 API/UI 字段不代表执行行为已经改变。

## 配置与环境

- 普通进程设置优先写入 `config/ai-jsunpack.example.{yaml,json}` 对应结构。
- secret 值不写入配置文件；`*SecretRef` 只是外部标识符。
- API strict role 必须继续拒绝 Worker、sandbox、Browser Runner、Core CLI 和 provider 权限。
- 新增环境覆盖时同步 `ENVIRONMENT_OVERRIDES`、配置文档和样例。
- `scripts/dev.mjs` 的 `--config` 只选择文件，不是通用 CLI override。

## 调试路径

- 401：确认 token 与 API 使用同一 HMAC secret，项目 claim 与 `VITE_API_PROJECT_ID` 匹配。
- Settings 409：重新读取 revision，再提交新的 `expectedRevision`。
- Worker 空转：检查 source input、租约、共享 Metadata DB 和 Artifact Store。
- `policy_denied`：检查 `cloudMode`、模型字段、provider 环境和部署 profile。
- `agent_failed`：检查 endpoint、HTTP 响应形状、timeout 和 Worker-only key。
- Browser Runner degraded：检查 queue backend、lease、retry、Playwright 依赖和 service token。
- production 本地执行被拒绝：这是 fail-closed 行为，配置隔离 sandbox 或远程 Browser Runner。

## 生成物与敏感信息

以下目录默认不提交：

- `.venv/`
- `node_modules/`
- `.crewai-data/`
- `artifacts/`
- `uploads/`
- `tmp/`
- `coverage/`
- `playwright-report/`
- `test-results/`
- `dev_docs/`

验证证据可以暂存在 `tmp/` 或外部归档，但不要提交真实客户输入、token、provider key、生产日志或敏感截图。

## 文档维护

- 中文是 `docs/` 当前事实源；不要创建未维护的重复英文页面。
- 保持现有文件路径稳定；重命名必须同时提供迁移说明并修复所有链接。
- API 表必须从 FastAPI route decorators 核对。
- 命令必须来自当前 `package.json`、`pyproject.toml`、`scripts/` 或 `deploy/`。
- `docs/deployment.md` 解释整体流程；`deploy/**/README.md` 保存实现级细节，避免复制完整内容。
