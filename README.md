# AI JS Unpack

AI JS Unpack 是一个面向授权前端构建产物的解包、审计与可运行还原平台。

它接收目录、单个 JavaScript 文件或受支持的归档包，输出可构建的还原工程、结构化 Agent 证据、浏览器运行对比和可下载结果包。系统由 React/Vite Web、FastAPI API、Python Worker、TypeScript Headless Core、多智能体 Runtime、Sandbox、Browser Runner、Metadata DB 和 Artifact Store 组成。

> 仅用于自有代码、授权代码、合规安全审计、软件资产恢复、研究和内部治理。不要用于绕过授权、窃取源码、提取秘密或复制第三方商业逻辑。

## 核心能力

- 目录、`.js`/`.mjs`/`.cjs`、ZIP/TAR/TGZ 输入规范化与归档安全限制。
- HTML/资源清单、AST、source map、模块候选和重建计划。
- Planner、Analysis、Naming、Type、Framework、Dead Code、Runtime、Repair、Report、Review 多阶段 Agent DAG。
- build/typecheck sandbox、runtime smoke/compare、截图、trace 和 review/fix 收敛。
- Artifact hash、producer、stage、attempt、parent lineage、sensitivity 和 retention 证据链。
- JSON/YAML 启动配置、系统/项目 Settings revision、回滚和 provider readiness。
- 本地文件/SQLite 与 PostgreSQL、S3/MinIO、远程 Browser Runner 部署模式。

## 快速开始

安装依赖并激活虚拟环境：

```powershell
npm install
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\Activate.ps1
```

创建配置：

```powershell
Copy-Item config/ai-jsunpack.example.yaml config/ai-jsunpack.yaml
Copy-Item .example.env .env
```

把 `.env` 中的 `AI_JSUNPACK_AUTH_SECRET` 替换为本地 secret，然后在独立终端启动：

```powershell
node scripts/dev.mjs api --config config/ai-jsunpack.yaml
node scripts/dev.mjs web --config config/ai-jsunpack.yaml
node scripts/dev.mjs worker --config config/ai-jsunpack.yaml
```

可选远程 Browser Runner：

```powershell
node scripts/dev.mjs browser-runner --config config/ai-jsunpack.yaml
node scripts/dev.mjs worker --use-browser-runner --config config/ai-jsunpack.yaml
```

默认地址：

- Web：`http://127.0.0.1:5173`
- API：`http://127.0.0.1:8000`
- API OpenAPI：`http://127.0.0.1:8000/docs`
- Browser Runner：`http://127.0.0.1:8001`

完整步骤见 [本地启动与验证](docs/local-startup.md)。

## 验证

```powershell
npm run check
npm run test:core
npm run build:web
python -m compileall apps packages tests deploy
python -m unittest discover -s tests
```

`npm run dev:check` 是较轻的聚合检查，不包含 Web production build、Python unittest、Ruff 或 Bandit。

需要使用已配置的真实模型重复评估角色契约、Planner 选择、Review 状态、冲突数量和延迟时，可运行：

```powershell
python scripts/evaluate_agents.py <inputPath> --cloud-mode local_only --local-model <model> --iterations 3
```

## Core CLI

```powershell
npm run build
node packages/core/dist/cli.js analyze <inputPath> --job-id <jobId>
node packages/core/dist/cli.js reconstruct <inputPath> --job-id <jobId> --output-dir <dir> [--agent-feedback-file <json>]
```

Core 支持目录、单个 JavaScript 文件和 `.zip`/`.tar`/`.tar.gz`/`.tgz`。归档会执行成员数量、单文件大小、总解压大小、压缩比、路径穿越和 link 检查。

## 文档

| 文档 | 内容                                                   |
| --- |------------------------------------------------------|
| [文档中心](docs/README.md) | 读者路径、文档边界和维护规则                                       |
| [本地启动与验证](docs/local-startup.md) | 虚拟环境、配置、token、服务和 smoke                              |
| [配置指南](docs/configuration.md) | JSON/YAML、环境覆盖、Settings 和 secret 边界                  |
| [架构设计](docs/architecture.md) | 服务、Worker pipeline、多智能体 DAG 和 lineage                |
| [API 参考](docs/api.md) | Auth、Config、Settings、Job、Evidence、Ops、Browser Runner |
| [开发指南](docs/development.md) | 仓库结构、验证矩阵和调试路径                                       |
| [部署指南](docs/deployment.md) | Compose、隔离执行、release gate、归档和回滚                      |
| [贡献指南](docs/contributing.md) | Issue、PR、提交规范、安全和提交清单                                |

## 当前限制

- 生成工程是可构建、可审计的重建壳，不保证还原原始作者源码。
- Settings API 已支持存储、合并、revision 与回滚；Worker 已消费 `agents.maxParallel` 和 `agents.contextBudget`，但并非所有 `validation.*` 字段都直接驱动执行。
- 生产 profile 必须配置隔离 sandbox 与远程 Browser Runner；本地 fallback 被有意拒绝。
- GitHub Release Gate 使用 `npm ci`，但当前版本树未跟踪 `package-lock.json`；修复 lockfile 策略并完成真实 CI 验证前，不应视为可用的干净检出发布门禁。
