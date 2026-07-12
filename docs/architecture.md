# 架构设计

AI JS Unpack 把输入解析、模型推断、工程写出、构建验证、浏览器对比和报告打包拆成可审计的边界。确定性代码拥有最终写入权；Agent 只产生结构化推断、诊断、修复建议和报告证据。

## 系统拓扑

```mermaid
graph TD
  User[User / Reviewer] --> Web[React + Vite Web]
  Web -->|Bearer token| API[FastAPI API]
  API --> DB[(Metadata DB)]
  API --> Store[(Artifact Store)]

  Worker[Python Worker] --> DB
  Worker --> Store
  Worker --> Core[TypeScript Headless Core]
  Worker --> Agents[Multi-agent Runtime]
  Worker --> Sandbox[Build / Typecheck Sandbox]
  Worker --> BrowserBoundary{Browser execution}
  BrowserBoundary --> LocalBrowser[Local Playwright]
  BrowserBoundary --> BrowserRunner[Remote Browser Runner]

  BrowserRunner --> DB
  BrowserRunner -->|capture result| Worker
  API --> Ops[Heartbeat / Metrics / Alerts]
  Worker --> Ops
  BrowserRunner --> Ops
```

共享基础设施可以使用本地 SQLite/文件系统，也可以使用 PostgreSQL 与 S3/MinIO。Worker 将 Browser Runner 返回的 capture 结果注册到主 Artifact Store。生产环境必须把 HTTP、Worker 执行、浏览器执行和 secret 注入分离。

## 代码边界

### 应用

- `apps/web`：官网、工作台和设置中心。负责创建 Job、上传输入、展示 Artifact、Agent evidence、运行时结果和报告。
- `apps/api`：认证、Job/Artifact 元数据、设置 revision、报告下载、retention 和 Ops 接口。
- `apps/worker`：队列租约、Core、Agent DAG、重建、sandbox、runtime compare、review/fix 和 packaging。
- `apps/browser_runner`：独立 Playwright capture 服务，提供持久队列、lease recovery、指标和远程执行边界。

### 共享包

- `packages/shared`：TypeScript 契约、状态、Artifact 类型和跨语言 schema 的事实源。
- `packages/core`：输入规范化、文件清单、HTML 引用、AST/source map 分析、重建计划和可构建工程写出。
- `packages/configuration`：JSON/YAML 启动配置、环境覆盖、运行时设置模型、脱敏与 fingerprint。
- `packages/sandbox`：local、container、gVisor、Firecracker 和远程浏览器执行策略。
- `packages/memory`：任务内、项目级、实体和场景记忆证据。
- `packages/knowledge`：框架、运行时、混淆模式和历史修复的确定性检索。
- `packages/audit`：审计概念、回滚映射、报告与 lineage 的包级边界。
- `packages/deployment`：服务角色和部署配置校验，阻止 API 携带 Worker 执行权限。
- `deploy/`：Compose、镜像、smoke、release gate、归档校验和 Firecracker launcher 模板。

## Job 生命周期

公开状态枚举由 `packages/shared/src/index.ts` 定义。当前实际上传与 Worker 路径为：

```mermaid
flowchart LR
  queued -->|upload| intake
  intake -->|worker lease| leased
  leased --> intake --> indexing --> agent_planning --> reconstructing
  reconstructing --> building --> typechecking --> runtime_smoke --> runtime_compare
  runtime_compare --> reviewing --> packaging
  reviewing --> repairing --> runtime_smoke
  packaging --> completed
  packaging --> completed_best_effort
  queued -. cancel .-> cancelled
  intake -. cancel .-> cancelled
  indexing -. failure .-> failed
```

`planning`、`parsing`、`analyzing` 和 `agent_pass` 仍保留在公开状态/事件契约中，但当前 Worker 没有为它们调用 `store.update_status`；其中 `agent_pass` 只写入当前 `PipelineRun` 事件。不能把这些值描述为当前可观察的持久 Job 状态。`repairing` 只在 review/fix 循环中出现。取消请求可以发生在任意非终态，Worker 会在阶段边界收敛到 `cancelled`。

端到端流程：

1. API 创建 Job，上传文件并写入 `source_input`。
2. Worker 获取租约，调用 Core 建立 `input_inventory` 和 `ast_index`。
3. Agent Runtime 从确定性摘要、memory、knowledge 和 Artifact refs 构建多阶段 DAG。
4. Core 根据分析与受限修复建议生成 `reconstruction_plan` 和 `generated_project`。
5. Sandbox 运行依赖、build 和 typecheck 计划，并写出日志与资源策略。
6. Playwright 对原始与还原目标执行 smoke/compare，记录 DOM、console、network、screenshot 和差异。
7. Review/fix gate 在预算内应用允许的低风险动作；不可修复问题进入 best-effort 证据。
8. Packaging 生成审计报告、证据索引和结果包。

## 多智能体 DAG

Agent Runtime 使用固定阶段、显式依赖和受限动态选择：

```mermaid
graph LR
  Planner[PlannerAgent] --> Analysis[AnalysisAgent]
  Analysis --> Naming[NamingAgent]
  Analysis --> Type[TypeAgent]
  Analysis --> Framework[FrameworkAgent]
  Analysis --> DeadCode[DeadCodeAgent]
  Analysis --> Runtime[RuntimeAgent]
  Naming --> Repair[RepairAgent]
  Type --> Repair
  Framework --> Repair
  DeadCode --> Repair
  Runtime --> Repair
  Naming --> Report[ReportAgent]
  Type --> Report
  Framework --> Report
  DeadCode --> Report
  Runtime --> Report
  Repair --> Review[ReviewAgent]
  Report --> Review
```

执行规则：

- Planner 只能从固定 specialist 白名单中选择本次节点；非法、空或失败输出回退到全部 specialist，不能创建 Agent 或修改阶段。
- `planner`、`analysis`、`review` 按依赖执行；specialist 以及互不依赖的 Repair/Report 可并行。
- 每次 CrewAI 调用运行在独立 Python 子进程和独立 `.crewai-data/<job>/<agent>/<run>` 中；主进程只负责受控 DAG 调度和 Artifact 持久化。
- `agents.maxParallel` 限制并发子进程数；不具备进程隔离能力的 Adapter 自动退化为串行。
- DAG 在执行前拒绝重复名称、缺失依赖、同阶段/后置依赖和依赖环。
- 上游失败时，下游节点标记为 `skipped` 并继承失败分类。
- Planner 失败不会阻断 Analysis；系统使用全部 specialist 的安全回退计划继续运行。
- conflict detector 按 inference type、target 和 value 区分同值 overlap 与异值 conflict，ReviewAgent 可读取全部 specialist、Repair 和 Report 的规范化输出。
- 每个 Agent 使用独立的严格 Pydantic 输出模型；服务端覆盖模型自报的 `agentName`，跨角色字段和 inference type 会被拒绝。
- `agents.contextBudget` 按确定顺序裁剪可选 knowledge、memory、摘要和 excerpt，同时保留直接依赖输出与 evidence locator，并记录预算审计。
- Agent 不能直接修改生成工程；只有 Review 明确批准的 `planned`、低风险、白名单 RepairAction 才会在首次 Core 重建完成前由确定性 Writer 应用。
- Python Worker 与 TypeScript Core 之间的反馈文件使用显式 `protocolVersion: 1`；版本或结构不匹配时 Core fail closed。

CrewAI 在此架构中是隔离的 Agent execution adapter；Job 状态、DAG、失败传播、审批、重试和 Artifact lineage 仍由 Worker 的确定性编排层负责。详情见 [配置指南](configuration.md#当前执行边界)。

## 输入与 Headless Core

Core CLI 支持：

- 目录。
- 单个 `.js`、`.mjs`、`.cjs`；Core 会创建临时 `index.html` 承载脚本。
- `.zip`、`.tar`、`.tar.gz`、`.tgz`。

归档解压限制：

- 最多 10,000 个成员。
- 单文件解压后最多 64 MiB。
- 总解压数据最多 256 MiB。
- 单成员压缩比最多 200:1。
- 拒绝绝对路径、路径穿越、Windows drive/UNC 路径、zip symlink、tar link 和未知成员类型。

Core 的输出是审计与重建壳，不承诺还原原始作者源码。生成工程保留复制文件、模块候选、运行时 shim、重建 manifest 和 rollback map，供后续构建与人工评审。

## 配置与设置边界

```mermaid
graph TD
  Defaults[Built-in defaults] --> Startup[Startup config]
  File[JSON / YAML] --> Startup
  Env[Registered environment overrides] --> Startup
  Startup --> Services[API / Worker / Browser Runner / Web]

  RuntimeDefaults[Runtime defaults] --> Effective[Effective Job settings]
  System[System revision] --> Effective
  Project[Project revision] --> Effective
  Job[Job config] --> Effective
```

启动配置决定服务监听、部署 profile、sandbox/provider 基线等进程级设置。运行时设置由 API 版本化保存，并在创建 Job 时合并。secret ref 只是外部标识符，实际 secret 仍由环境或部署平台注入。

API settings 的存储/合并能力与 Worker 对字段的实际消费是不同边界；文档和 UI 不应把“保存成功”描述为所有执行开关已经生效。

## Artifact Lineage

```mermaid
graph TD
  source[source_input] --> inventory[input_inventory]
  source --> ast[ast_index]
  inventory --> plan[agent_plan]
  ast --> plan
  plan --> execution[agent_execution]
  execution --> inference[inference / memory / knowledge / tool evidence]
  inference --> reconstruction[reconstruction_plan]
  reconstruction --> generated[generated_project]
  generated --> build[build_log / build_artifact]
  generated --> runtime[runtime_validation / trace / screenshot / comparison]
  runtime --> review[review_run / repair_instruction / runtime_diagnosis]
  build --> reports[audit_report / html_report / evidence_index]
  review --> reports
  reports --> package[result_package]
```

Artifact 记录至少包含 `kind`、`stage`、`attempt`、`schemaVersion`、`contentType`、hash、storage URI、producer、父 Artifact、敏感级别、保留级别和时间信息。每次 retry/repair 产生新 attempt，不覆盖旧证据。

## 浏览器执行边界

本地开发可使用 Worker 内的 Playwright adapter。配置远程 Browser Runner 后，Worker 提交受控 source archive，Browser Runner 异步 capture 并返回结果。

队列后端：

- `sqlite`：单实例本地运行。
- `postgresql`：多实例部署，推荐与 Metadata DB 共用 PostgreSQL。

生产 profile 禁止本地 Playwright fallback；没有可用远程 runner 时返回 `policy_denied`，而不是降级执行。

## 安全不变量

- API strict role 不得加载 Worker、sandbox、Browser Runner、Core CLI 或模型 provider 凭据。
- Agent 输出必须经过 schema、证据引用和 deterministic gate。
- 高隔离 sandbox 配置失败时 fail closed。
- 默认浏览器/沙箱网络策略为拒绝，放开网络必须显式配置和审计。
- Ops 指标要求认证，因为包含实例、队列、Job 与告警信息。
- `completed_best_effort` 必须携带失败分类、限制和可下载证据，不能伪装成完全成功。
