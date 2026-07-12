# 配置指南

AI JS Unpack 使用两类配置：服务启动时读取的 JSON/YAML 配置，以及 API 保存的系统、项目和 Job 运行时设置。普通结构化配置优先写入配置文件；secret 值仍由环境变量或部署平台的 secret manager 注入。

## 快速开始

复制 YAML 或 JSON 示例：

```powershell
Copy-Item config/ai-jsunpack.example.yaml config/ai-jsunpack.yaml
```

先验证，再查看脱敏后的有效配置：

```powershell
.venv\Scripts\python.exe -m packages.configuration validate config/ai-jsunpack.yaml
.venv\Scripts\python.exe -m packages.configuration print-effective config/ai-jsunpack.yaml
```

使用同一配置启动服务：

```powershell
node scripts/dev.mjs api --config config/ai-jsunpack.yaml
node scripts/dev.mjs web --config config/ai-jsunpack.yaml
node scripts/dev.mjs worker --config config/ai-jsunpack.yaml
node scripts/dev.mjs browser-runner --config config/ai-jsunpack.yaml
```

`scripts/dev.mjs` 调用当前 shell 中的 `python`。如果依赖安装在 `.venv`，先激活虚拟环境：

```powershell
.venv\Scripts\Activate.ps1
```

也可以通过环境变量选择配置文件：

```powershell
$env:AI_JSUNPACK_CONFIG_FILE = "config/ai-jsunpack.yaml"
```

## 启动配置结构

配置根对象使用 `version: 1`，未知字段会被拒绝。

```yaml
version: 1

shared:
  deploymentProfile: development
  logLevel: info

api:
  host: 127.0.0.1
  port: 8000
  corsOrigins:
    - http://127.0.0.1:5173
  maxUploadBytes: 104857600
  artifactRoot: tmp/local-dev/artifacts
  database:
    urlSecretRef: database/local

worker:
  sandbox:
    runner: local
    allowLocalInDevelopment: true
  agent:
    cloud:
      provider: openai-compatible
      model: null
      baseUrl: null
      apiKeySecretRef: ai/cloud
    local:
      provider: openai-compatible
      model: qwen3-coder
      baseUrl: http://127.0.0.1:11434/v1
      apiKeySecretRef: ai/local

browserRunner:
  mode: local
  baseUrl: null
  tokenSecretRef: browser-runner/local

web:
  apiBaseUrl: http://127.0.0.1:8000
```

主要分区：

- `shared`：部署环境和日志级别。
- `api`：监听地址、CORS、上传上限、Artifact 根目录和数据库引用。
- `worker`：sandbox runner 以及云端、本地 Agent provider 元数据。
- `browserRunner`：期望模式、远程地址和 token 引用元数据。
- `web`：Web 调用的 API 地址。

完整可提交示例位于 `config/ai-jsunpack.example.yaml` 和 `config/ai-jsunpack.example.json`。本地实际配置 `config/ai-jsunpack.yaml`、`config/ai-jsunpack.json` 已被 Git 忽略。

## 启动配置优先级

当前加载器只实现以下顺序：

```text
内置默认值 < JSON/YAML 文件 < 已注册的环境变量覆盖
```

`--config` 只选择配置文件，不是任意字段的 CLI 覆盖层。环境覆盖仅限 `packages/configuration/config.py` 注册的字段：

- `AI_JSUNPACK_DEPLOYMENT_PROFILE`
- `AI_JSUNPACK_LOG_LEVEL`
- `AI_JSUNPACK_API_HOST`
- `AI_JSUNPACK_API_PORT`
- `AI_JSUNPACK_CORS_ORIGINS`
- `AI_JSUNPACK_MAX_UPLOAD_BYTES`
- `AI_JSUNPACK_ARTIFACT_ROOT`
- `AI_JSUNPACK_AGENT_PROVIDER`
- `AI_JSUNPACK_AGENT_MODEL`
- `AI_JSUNPACK_AGENT_BASE_URL`
- `AI_JSUNPACK_AGENT_API_KEY_SECRET_REF`
- `AI_JSUNPACK_LOCAL_AGENT_PROVIDER`
- `AI_JSUNPACK_LOCAL_AGENT_MODEL`
- `AI_JSUNPACK_LOCAL_AGENT_BASE_URL`
- `AI_JSUNPACK_LOCAL_AGENT_API_KEY_SECRET_REF`

数据库 URL、S3/MinIO 凭据、HMAC secret、Browser Runner token 等现有运行变量仍由各服务直接读取；配置文件中的 `*SecretRef` 只是外部 secret 的标识符，不会在仓库代码中自动解析或注入值。

## 运行时设置

API 保存三层运行时设置：

```text
运行时默认值 < 系统设置 < 项目设置 < 创建 Job 时的 config
```

当前注册字段：

| 分区 | 字段 | 默认值 |
| --- | --- | --- |
| `ai.cloud` / `ai.local` | `provider`、`model`、`baseUrl`、`apiKeySecretRef` | provider 相关默认值 |
| `agents` | `enabled` | `true` |
| `agents` | `maxParallel` | `5`，允许 1–10 |
| `agents` | `contextBudget` | `16000` |
| `validation` | `runTypecheck` | `true` |
| `validation` | `runRuntimeCompare` | `true` |
| `validation` | `minimumConfidence` | `0.7` |

系统设置修改要求 user token，并且至少在一个项目中拥有 `owner`。项目设置读取要求 `viewer`，修改、历史和回滚要求该项目的 `owner`。更新使用 `expectedRevision` 做乐观锁；版本冲突返回 HTTP 409。回滚会创建新 revision，不删除历史。

创建 Job 时，API 会把系统、项目和请求中的 `ai`、`agents`、`validation` 合并进 Job `config`。

### 当前执行边界

Settings API 和 Web 设置中心已经提供存储、合并、版本和回滚能力，但 Worker 并未消费所有嵌套运行时字段。当前 provider 选择仍主要读取 Job 顶层的 `agentModel`、`agentModelProvider`、`localAgentModel`、`localAgentProvider`，或 Worker 环境变量；build/runtime compare 仍读取 `buildValidation`、`runtimeCompare`、`reviewFix` 等现有 Job 配置。

因此，在对应 Worker 消费逻辑补齐前，不要把 `agents.maxParallel`、`agents.contextBudget` 或 `validation.*` 的界面保存成功等同于执行行为已经改变。它们目前是已验证、可审计的配置记录和 Job 输入。

启动模型也包含尚未直接驱动执行的字段：`worker.sandbox.allowLocalInDevelopment`、`browserRunner.mode` 和 `browserRunner.tokenSecretRef` 当前会被校验、脱敏和展示，但 `apply_application_config_to_environment` 只映射 sandbox `runner`、Agent provider/model/base URL 和 Browser Runner `baseUrl`。实际 token、队列 backend 和本地执行许可仍由服务环境与部署策略决定。

## Web 设置中心

Web 提供以下页面：

```text
/settings/general
/settings/ai
/settings/agents
/settings/security
/settings/validation
/projects/<project-id>/settings
```

启动配置是只读信息，修改后需要通过配置文件、环境变量或 secret manager 更新，并重启对应服务。系统和项目运行时设置可以从界面保存为新 revision。

相关 API 见 [API 参考](api.md#配置与设置)。

## Secret 与 Provider

以下值必须通过 Worker/API 环境或部署 secret manager 提供，不能把明文写入配置文件或设置 API：

```text
AI_JSUNPACK_AUTH_SECRET
AI_JSUNPACK_AGENT_API_KEY
AI_JSUNPACK_LOCAL_AGENT_API_KEY
AI_JSUNPACK_BROWSER_RUNNER_TOKEN
OPENAI_API_KEY
ANTHROPIC_API_KEY
GOOGLE_API_KEY
AZURE_OPENAI_API_KEY
```

`/v1/config/effective`、Settings API 和 provider readiness 只返回脱敏配置、secret 引用或“是否已配置”的布尔状态，不返回 secret 值。

OpenAI-compatible endpoint 必须是绝对 HTTP(S) URL，不能包含用户名或密码。生产云端 endpoint 应使用 HTTPS；loopback HTTP 只适合本地 provider。

## 生产配置

生产环境设置：

```yaml
shared:
  deploymentProfile: production
```

生产 profile 的关键边界：

- 本地主机 sandbox 不得作为生产隔离方案。
- 本地 Playwright fallback 被拒绝，必须配置远程 Browser Runner。
- 高隔离 runner 配置失败时必须 fail closed，不能静默回退到 `local`。
- secret 引用必须由实际部署平台映射到环境变量或短期凭据。
- API、Worker、Browser Runner 和 Web 使用各自的服务配置，避免把 Worker provider 凭据注入 API/Web。

部署变量和 secret 分工见 [部署指南](deployment.md)。

## 故障排查

- `validate` 报未知字段：配置模型使用 `extra=forbid`，检查拼写和 camelCase。
- `baseUrl must be an absolute HTTP(S) URL`：补全 scheme，并移除 URL 中的用户信息。
- 服务仍使用旧值：确认 `AI_JSUNPACK_CONFIG_FILE` 指向实际文件并重启进程。
- 环境变量没有覆盖：只有上方列出的注册变量进入启动配置加载器；其他变量由对应服务直接读取。
- provider readiness 显示未就绪：分别检查 model、base URL、明文环境凭据和 `apiKeySecretRef`。引用存在不代表 secret 已被解析。
- Settings 更新返回 409：重新读取当前 revision，再以新的 `expectedRevision` 提交。
