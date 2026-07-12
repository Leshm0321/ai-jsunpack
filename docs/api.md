# API 参考

AI JS Unpack 暴露两个 FastAPI 服务：主 API `apps.api.app.main:app` 和 Browser Runner `apps.browser_runner.app.main:app`。默认本地地址分别是 `http://127.0.0.1:8000` 与 `http://127.0.0.1:8001`。

服务启动后可查看主 API 的交互式 OpenAPI 页面：

```text
http://127.0.0.1:8000/docs
http://127.0.0.1:8000/openapi.json
```

## 认证与角色

业务和数据接口除健康检查外使用 HMAC-SHA256 Bearer token。FastAPI 默认生成的 `/docs` 与 `/openapi.json` 当前未加业务认证，部署到不受信网络时应由网关限制：

```http
Authorization: Bearer <token>
```

Token kind：

- `user`：面向 Web 和人工调用，`projects` claim 记录项目角色。
- `service`：面向 Worker 和 Browser Runner，受限调用需要 `serviceRoles` 包含 `worker`。

项目角色从低到高为 `viewer`、`maintainer`、`owner`：

- `viewer`：读取 Job、Artifact、报告、设置和审计证据。
- `maintainer`：创建、上传、rerun、cancel、retention cleanup，并可读取 Ops。
- `owner`：包含 maintainer 权限，并可修改项目设置；任意项目 owner 还可修改系统设置。

常见认证响应：

- `401`：缺少、过期或签名不匹配的 Bearer token。
- `403`：token 有效，但 token kind、service role 或项目角色不足。
- `404`：Job、Artifact、报告或其他资源不存在。

本地 token 生成见 [本地启动与验证](local-startup.md#生成本地-token)。

## 健康检查

| Method | Path | 认证 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/health` | 无 | 返回 API 状态、`serviceRole=api` 和部署角色校验状态 |

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

## 配置与设置

### 启动配置

| Method | Path | 权限 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/v1/config/effective` | 已认证 user | 返回脱敏后的启动配置、来源和 fingerprint |
| `GET` | `/v1/config/schema` | 已认证 user | 返回启动配置与运行时设置的 JSON Schema |
| `GET` | `/v1/providers/readiness` | 已认证 user | 返回 cloud/local provider 的脱敏就绪状态 |

`/v1/providers/readiness` 返回 provider、model、endpoint 类型、凭据与 secret ref 是否已配置，以及问题列表；不会返回 API key。

### 系统设置

| Method | Path | 权限 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/v1/settings/system` | 已认证 user | 当前系统设置快照 |
| `PUT` | `/v1/settings/system` | 任意项目 `owner` | 创建新系统设置 revision |
| `GET` | `/v1/settings/system/revisions` | 任意项目 `owner` | 查询 revision 历史 |
| `POST` | `/v1/settings/system/rollback` | 任意项目 `owner` | 以历史 revision 创建新的回滚 revision |

更新示例：

```http
PUT /v1/settings/system
Content-Type: application/json
Authorization: Bearer <owner-token>

{
  "settings": {
    "agents": {"maxParallel": 3},
    "validation": {"minimumConfidence": 0.8}
  },
  "expectedRevision": 0,
  "reason": "Tune review defaults"
}
```

`expectedRevision` 不匹配时返回 `409`。

### 项目设置

| Method | Path | 权限 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/v1/projects/{project_id}/settings` | 项目 `viewer` | 当前项目设置快照 |
| `PUT` | `/v1/projects/{project_id}/settings` | 项目 `owner` | 创建项目设置 revision |
| `GET` | `/v1/projects/{project_id}/settings/effective` | 项目 `viewer` | 合并系统与项目设置 |
| `GET` | `/v1/projects/{project_id}/settings/revisions` | 项目 `owner` | 查询项目 revision 历史 |
| `POST` | `/v1/projects/{project_id}/settings/rollback` | 项目 `owner` | 创建回滚 revision |

运行时设置的优先级、字段和当前 Worker 消费边界见 [配置指南](configuration.md#运行时设置)。

## Job 与输入

| Method | Path | 权限 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/jobs` | 项目 `maintainer` | 创建 Job |
| `POST` | `/jobs/{job_id}/upload` | 项目 `maintainer` | 上传单个 source input 文件 |
| `GET` | `/jobs/{job_id}` | 项目 `viewer` | 获取 Job 和 Artifact 列表 |
| `POST` | `/jobs/{job_id}/rerun` | 项目 `maintainer` | 从原始 `source_input` 创建新 Job |
| `POST` | `/jobs/{job_id}/cancel` | 项目 `maintainer` | 请求取消非终态 Job |

创建 Job：

```http
POST /jobs
Content-Type: application/json
Authorization: Bearer <maintainer-token>

{
  "projectId": "default",
  "ownerId": "local-user",
  "cloudMode": "local_only",
  "config": {
    "localAgentModel": "qwen3-coder",
    "localAgentProvider": "openai-compatible"
  }
}
```

`cloudMode` 支持：

- `cloud_allowed`
- `local_only`
- `desensitized`

API 上传会读取最多 `AI_JSUNPACK_MAX_UPLOAD_BYTES` 字节，超过上限返回 `413`。API 接收的是单个文件；目录输入应先打包。当前 Headless Core 支持目录、单个 `.js`/`.mjs`/`.cjs` 和 `.zip`/`.tar`/`.tar.gz`/`.tgz`。归档安全与资源限制见 [本地启动与验证](local-startup.md#core-cli)。

上传使用 `multipart/form-data`：

```powershell
curl.exe -X POST `
  -H "Authorization: Bearer <maintainer-token>" `
  -F "file=@sample.zip" `
  "http://127.0.0.1:8000/jobs/<job-id>/upload"
```

## Artifact、报告与保留策略

| Method | Path | 权限 | 返回/行为 |
| --- | --- | --- | --- |
| `GET` | `/jobs/{job_id}/artifacts/{artifact_id}/download` | 项目 `viewer` | 下载单个文件 Artifact |
| `GET` | `/jobs/{job_id}/reports` | 项目 `viewer` | 报告类 `ArtifactRecord[]`，可用 `kind` 过滤 |
| `GET` | `/jobs/{job_id}/reports/audit` | 项目 `viewer` | 最新 Markdown 审计报告 |
| `GET` | `/jobs/{job_id}/reports/{report_kind}` | 项目 `viewer` | 最新指定报告 |
| `GET` | `/jobs/{job_id}/result-package` | 项目 `viewer` | 最新结果包 ZIP |
| `POST` | `/jobs/{job_id}/retention/cleanup` | 项目 `maintainer` | dry-run 或执行保留策略清理 |

`report_kind` 只接受报告 Artifact 类型，包括 `audit_report`、`html_report` 和 `evidence_index`；未知类型返回 `400`，合法类型没有对应 Artifact 时返回 `404`。`html_report` 作为下载产物提供，不应直接注入 Web 页面渲染。

Retention 请求示例：

```json
{
  "dryRun": true,
  "categories": ["logs", "screenshots"],
  "retentionClasses": ["ephemeral"],
  "deleteExpired": true,
  "reason": "preview expired evidence cleanup"
}
```

## Agent 与运行证据

| Method | Path | 返回 |
| --- | --- | --- |
| `GET` | `/jobs/{job_id}/runtime-validations` | `RuntimeValidationRun[]` |
| `GET` | `/jobs/{job_id}/runtime-validations/latest` | 最新 `RuntimeValidationRun` |
| `GET` | `/jobs/{job_id}/inference-records` | `InferenceRecord[]` |
| `GET` | `/jobs/{job_id}/review-runs` | `ReviewRun[]` |
| `GET` | `/jobs/{job_id}/tool-calls` | `ToolCall[]` |
| `GET` | `/jobs/{job_id}/tool-registry` | `ToolRegistryEntry[]` |
| `GET` | `/jobs/{job_id}/memory-records` | `MemoryRecord[]` |
| `GET` | `/jobs/{job_id}/audit-records` | 聚合的 inference、review 和 tool 记录 |

`memory-records` 支持 `memory_type` 或 `memoryType` 查询参数，允许 `short_term`、`long_term`、`entity`、`scenario`。`audit-records` 的 `category` 支持 `all`、`inference`、`review`、`tool`。

多智能体阶段和 Artifact lineage 见 [架构设计](architecture.md)。

## Ops

| Method | Path | 权限 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/ops/heartbeats` | worker service | Worker/Browser Runner 写入 heartbeat |
| `GET` | `/ops/heartbeats` | ops read | 查询 heartbeat，可按服务和 active 状态过滤 |
| `GET` | `/ops/metrics` | ops read | 聚合 Job、heartbeat、队列与告警快照 |
| `GET` | `/ops/prometheus` | ops read | Prometheus text exposition |
| `GET` | `/ops/alerts` | ops read | 计算并记录当前告警，可投递 webhook |
| `GET` | `/ops/alert-events` | ops read | 查询历史告警事件 |

ops read 允许：

- `serviceRoles` 包含 `worker` 的 service token。
- 任意项目中具有 `maintainer` 或 `owner` 的 user token。

Ops 指标包含实例、队列、Job 和告警信息，不提供匿名访问。

## Browser Runner API

Browser Runner 的业务接口要求 worker service token。它的 FastAPI `/docs` 与 `/openapi.json` 当前也保持默认匿名访问；部署到不受信网络时应由网关限制。

| Method | Path | 认证 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/health` | 无 | backend、队列、worker、lease recovery 和 alerts |
| `POST` | `/browser-runs` | worker service | 提交异步浏览器 capture |
| `GET` | `/browser-runs/metrics` | worker service | 查询队列指标 |
| `GET` | `/browser-runs/{run_id}` | worker service | 查询运行状态和结果 |

Browser Runner 只执行受控浏览器 capture，不执行 Worker 的依赖安装、build 或 typecheck。Worker 提交 source archive、轮询结果，并将 trace、screenshot 与 comparison 写入主 Artifact Store。

## 状态与失败分类

终态 Job：

- `completed`
- `completed_best_effort`
- `failed`
- `cancelled`

常见 failure class：

- `invalid_input`
- `parse_error`
- `agent_failed`
- `dependency_missing`
- `install_failed`
- `type_error`
- `build_error`
- `runtime_error`
- `sandbox_denied`
- `policy_denied`
- `timeout`
- `resource_limit`
- `unknown`

Browser Run 结果状态为 `pass`、`fail` 或 `best_effort`。
