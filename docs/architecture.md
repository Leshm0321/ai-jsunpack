# 架构设计

AI JS Unpack 由 Web、API、Worker、Headless Core、Agent Runtime、Sandbox、Browser Runner、Metadata DB 和 Artifact Store 组成。核心原则是：确定性分析与写出负责工程产物，Agent 只通过结构化证据影响结果，所有阶段都保留可审计 Artifact lineage。

## 总体架构

```mermaid
graph TD
  User[User / Reviewer] --> Web[React + Vite Workbench]
  Web -->|Bearer token| API[FastAPI API]
  API -->|Job metadata| DB[(PostgreSQL / SQLite)]
  API -->|Artifact content| Store[(Local FS / S3 / MinIO)]

  Worker[Worker Queue Runner] -->|lease / heartbeat| DB
  Worker -->|read / write| Store
  Worker --> Core[Headless Core]
  Worker --> Agent[Agent Runtime / CrewAI]
  Worker --> Sandbox[Build and Typecheck Sandbox]
  Worker --> BrowserBoundary{Browser Boundary}
  BrowserBoundary --> LocalPlaywright[Local Playwright]
  BrowserBoundary --> BrowserRunner[Remote Browser Runner]

  BrowserRunner -->|queue / metrics| DB
  BrowserRunner -->|capture result| Worker
  API --> Ops[Ops Metrics / Prometheus / Alerts]
  Worker --> Ops
  BrowserRunner --> Ops
```

## 模块职责

- `apps/web`：上传输入、创建 Job、展示状态、Artifact、runtime validation、Agent evidence、报告和下载入口。
- `apps/api`：认证、Job 生命周期、Artifact 下载、报告聚合、rerun/cancel、retention cleanup、Ops metrics 和 alerts。
- `apps/worker`：领取 Job，串联 Core、Agent、reconstruction、build/typecheck、runtime smoke/compare、review/fix 和 packaging。
- `apps/browser_runner`：独立执行 Playwright capture，提供队列、lease recovery、metrics 和远程执行边界证据。
- `packages/core`：输入规范化、文件清单、HTML 引用解析、AST 索引、Source Map 分析、低风险转换、重建计划和工程写出。
- `packages/shared`：Job、Artifact、Review、Runtime、Memory、Tool、Ops 等跨 TS/Python 契约。
- `packages/sandbox`：本地、容器、gVisor、Firecracker 和 remote browser runner profile 的执行策略与审计含义。
- `packages/deployment`：按服务角色校验环境变量，避免 API 接收 Worker/sandbox/model provider 配置。

## 作业生命周期

```mermaid
sequenceDiagram
  actor U as User
  participant W as Web
  participant A as API
  participant D as Metadata DB
  participant S as Artifact Store
  participant R as Worker
  participant C as Core
  participant G as Agent Runtime
  participant B as Browser Runner

  U->>W: Select input and cloudMode
  W->>A: POST /jobs
  A->>D: Create Job
  W->>A: POST /jobs/{job_id}/upload
  A->>S: Write source_input
  A->>D: Move to intake
  R->>D: Lease next Job
  R->>C: Analyze input
  C-->>R: input_inventory + ast_index
  R->>G: Run structured Agent passes
  G-->>R: inference / review / tool / memory evidence
  R->>C: Generate reconstruction_plan and generated_project
  R->>S: Persist generated artifacts
  R->>R: Build and typecheck in sandbox
  R->>B: Optional remote browser validation
  B-->>R: trace / screenshot / comparison
  R->>S: audit_report + html_report + evidence_index + result_package
  R->>D: completed or completed_best_effort
```

## Worker Pipeline

```mermaid
flowchart LR
  queued[queued] --> leased[leased]
  leased --> intake[intake]
  intake --> indexing[indexing]
  indexing --> agent_planning[agent_planning]
  agent_planning --> agent_pass[agent_pass]
  agent_pass --> reconstructing[reconstructing]
  reconstructing --> building[building]
  building --> typechecking[typechecking]
  typechecking --> runtime_smoke[runtime_smoke]
  runtime_smoke --> runtime_compare[runtime_compare]
  runtime_compare --> reviewing{review gate}
  reviewing -->|repair needed| repairing[repairing]
  repairing --> runtime_smoke
  reviewing -->|pass / disabled / budget exhausted| packaging[packaging]
  packaging --> completed[completed]
  packaging --> completed_best_effort[completed_best_effort]
  leased -. cancel .-> cancelled[cancelled]
  intake -. unrecoverable .-> failed[failed]
```

关键约束：

- Agent 输出必须经过 schema 校验和证据绑定，不能直接自由改写最终工程。
- Deterministic writer 只消费结构化 plan 和低风险 repair instruction。
- `completed_best_effort` 必须保留失败分类、限制说明、证据和可下载产物。
- 每个 attempt 都应写入新 artifact，便于回溯和复现。

## Artifact Lineage

```mermaid
graph TD
  source[source_input] --> inventory[input_inventory]
  source --> ast[ast_index]
  inventory --> agentPlan[agent_plan]
  ast --> agentPlan
  agentPlan --> inference[inference_record]
  agentPlan --> memory[memory_record]
  agentPlan --> knowledge[knowledge_evidence]
  inference --> review[review_run]
  review --> repair[repair_instruction]

  inventory --> plan[reconstruction_plan]
  ast --> plan
  repair --> plan
  plan --> generated[generated_project]
  generated --> build[build_artifact / build_log]
  generated --> smoke[runtime_validation / runtime_trace / runtime_screenshot]
  smoke --> compare[runtime_comparison]
  compare --> convergence[review_fix_convergence_summary]
  build --> audit[audit_report]
  compare --> audit
  convergence --> audit
  audit --> evidence[evidence_index]
  audit --> package[result_package]
```

## Browser Runner 边界

Browser Runner 可以把 Playwright 工作从 Worker 中拆出。Worker 将源输入或生成工程打包为受控 source archive，Browser Runner 在独立服务内执行 capture，返回 console、network、DOM、screenshot 和 execution boundary。`runtime_trace.executionBoundary` 会记录 runner kind、remote run id、queue backend、attempt、lease recovery、队列长度、运行耗时和告警状态。

## 数据与安全模型

- Job 状态由 `packages/shared` 的 `JOB_STATUSES` 定义。
- Artifact kind 包括 `source_input`、`input_inventory`、`ast_index`、`reconstruction_plan`、`generated_project`、`runtime_validation`、`runtime_trace`、`runtime_comparison`、`audit_report`、`result_package` 等。
- `cloudMode` 包括 `cloud_allowed`、`local_only`、`desensitized`。
- Artifact 带有 `sensitivityClass`、`retentionClass`、`parentArtifactIds`、`producer`、`hash` 和生命周期字段。
