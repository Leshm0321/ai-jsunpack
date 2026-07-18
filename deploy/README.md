# 部署 Profile

本目录按服务边界拆分运行时配置。

- `api` 负责 HTTP、认证、元数据和 Artifact 访问。它不能接收 sandbox、browser-runner、Core CLI 或模型 provider 凭据。
- `worker` 负责 Core CLI、Agent runtime、build/typecheck sandbox 执行和 packaging。
- `browser-runner` 为将浏览器工作从主 Worker 池拆出的部署提供 Playwright/browser 执行容量。
- `db` 和 `artifact-store` 是 API 与 Worker 共享的基础设施服务。
- `web` 只在构建/运行边界接收 `VITE_API_*` 值。

compose 文件既是部署契约，也是本地启动入口。它可以从本仓库构建本地服务镜像；当 CI 发布不可变 tag 后，也可以用 `AI_JSUNPACK_*_IMAGE` 覆盖应用镜像，并用 `POSTGRES_IMAGE`、`MINIO_IMAGE`、`MINIO_MC_IMAGE` 固定基础设施镜像。

## 发布门禁

使用 `deploy.release_gate` 作为平台中立的 CI/CD 入口。它会固定服务镜像 tag、记录 SBOM 和漏洞扫描命令计划、列出所需 secret 注入点，并在执行模式启用时运行发布后的 compose smoke gate。

Dry-run 计划：

```powershell
.venv\Scripts\python.exe -m deploy.release_gate `
  --registry registry.example.com `
  --repository-prefix ai-jsunpack `
  --version 2026.06.26 `
  --git-sha <commit-sha> `
  --previous-version 2026.06.25 `
  --output tmp\release-gate\release-gate.json `
  --dry-run
```

CI 执行：

```powershell
.venv\Scripts\python.exe -m deploy.release_gate `
  --registry registry.example.com `
  --repository-prefix ai-jsunpack `
  --version 2026.06.26 `
  --git-sha <commit-sha> `
  --previous-version 2026.06.25 `
  --execute `
  --push
```

门禁会写出 `release-gate.json`，其中包含应注入 compose 或目标编排器的固定 `AI_JSUNPACK_*_IMAGE` 值。默认使用 `syft` 生成 SBOM，使用 `trivy` 执行镜像漏洞扫描；只有明确批准的离线例外才应传入 `--sbom-tool none` 或 `--scan-tool none`。未设置 `--push` 时，执行模式只构建并验证本地 tag，不发布到 registry。

Secret 必须来自 CI 或平台 secret store。不要提交解析后的值。生产注入至少包括 `AI_JSUNPACK_AUTH_SECRET`、`AI_JSUNPACK_ARTIFACT_S3_SECRET_ACCESS_KEY`、`AI_JSUNPACK_BROWSER_RUNNER_TOKEN`、Worker 模型 provider 凭据，以及 Web 的运行时/会话 `VITE_API_AUTH_TOKEN` 策略。

GitHub Actions 用户可以通过 `workflow_dispatch` 运行 `.github/workflows/release-gate.yml`。该 workflow 默认目标为 GHCR，使用具备 `contents: read` 与 `packages: write` 权限的 `GITHUB_TOKEN`，在选定的 `secret_environment` 下运行，并调用 `deploy.release_gate --ci-platform github_actions --secret-environment <environment> --execute`。只有 `push_images` 输入为 true 时才推送镜像。workflow 会把 release gate、SBOM、scan、compose smoke 和 deployment smoke 报告上传为 Actions artifacts；`release-gate.json` 也会记录包含外部证据要求的 `productionArchiveChecklist`。生产 DB snapshot、Artifact Store export、GHCR registry digest、服务日志、回滚证据，以及 GitHub Environment revision 或 approval record 仍必须由部署平台在 GitHub runner workspace 外部保留。

第一次真实运行后，将这些外部引用写入 UTF-8 `production_release_evidence_manifest` JSON 文件，并用以下命令核验最终归档：

```powershell
.venv\Scripts\python.exe -m deploy.release_archive `
  --release-gate-report tmp\release-gate\release-gate.json `
  --compose-smoke-report tmp\release-gate\compose-smoke.json `
  --deployment-smoke-report tmp\release-gate\deployment-smoke.json `
  --evidence-manifest tmp\release-gate\production-evidence-manifest.json `
  --output tmp\release-gate\production-release-archive.json
```

核验器要求存在已执行的 release gate 证据、通过的 compose/deployment smoke 报告、`archiveReady=true`、已推送镜像的 registry digest、不含 secret 值的 secret manager revision 或 approval record、保留的 DB/Artifact Store 证据、服务日志和回滚证据。如实际使用 Kubernetes Secret、Vault、SOPS/SealedSecrets 或其他生产注入差异，请在 manifest 的 `platformDifferences` 中记录，并补充对应文档。

## Compose 镜像与健康检查

本地服务 Dockerfile 位于 `deploy/docker/`：

- `api.Dockerfile` 启动 `uvicorn apps.api.app.main:app`。
- `worker.Dockerfile` 启动 `python -m apps.worker.worker.queue`，并包含已构建的 Core CLI。
- `browser-runner.Dockerfile` 启动 `uvicorn apps.browser_runner.app.main:app`，并安装 Playwright Chromium。
- `web.Dockerfile` 构建 Vite workspace，并在 5173 端口提供静态 bundle。

`deploy/docker-compose.yml` 定义不向宿主发布端口的公共拓扑；`docker-compose.dev.yml` 和 `docker-compose.prod.yml` 分别叠加开发与生产策略。一次性 `artifact-store-init` 服务会创建 MinIO bucket，开发环境的 `auth-init` 会生成 24 小时 user/service token。Worker 是长驻队列消费者，通过 ops heartbeat 和 deployment smoke 报告验证，而不是通过 HTTP health check 验证。

构建并启动完整本地拓扑：

```powershell
docker compose -p ai-jsunpack-dev -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml up --build -d
docker compose -p ai-jsunpack-dev -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml ps
```

检查完成后停止：

```powershell
docker compose -p ai-jsunpack-dev -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml down
```

Web 使用 `http://127.0.0.1:5173`。浏览器请求同源 `/api`，由 Web 容器代理到 API；`runtime-config.js` 在容器启动时注入 API 地址和 token，不需要重新构建 Vite bundle。Web 与 Worker 都会重新读取 token 文件，因此后续 `docker compose ... up -d` 刷新开发 token 时无需重建镜像。

生产部署先复制并填写未跟踪的环境文件：

```powershell
Copy-Item deploy/env/production.env.example deploy/env/production.env
docker compose --env-file deploy/env/production.env -p ai-jsunpack `
  -f deploy/docker-compose.yml -f deploy/docker-compose.prod.yml up -d
```

生产覆盖默认只发布 Web `8080`。DB、MinIO、API 和 Browser Runner 仅在 Compose 网络内访问；缺少数据库密码、HMAC secret 或 Web/Worker token 时会在 Compose 配置阶段失败。

> Worker 挂载 `/var/run/docker.sock`，等同于宿主 Docker 管理权限。本配置只面向已批准的单用户或可信内网部署，不是多租户强隔离边界。

## 验证

设置 `AI_JSUNPACK_SERVICE_ROLE=api` 时，API 进程会在 import/startup 阶段校验环境；如果出现 Worker/browser 执行配置，会快速失败。未显式设置服务角色时，本地开发保持宽松，`/health` 返回 warning profile 而不是失败。

发布交付前运行本地生产 smoke/soak 验收：

```powershell
.venv\Scripts\python.exe -m apps.api.app.deployment_smoke `
  --output tmp\deployment-smoke.json
```

默认路径使用临时 SQLite、临时 Artifact Store、API TestClient、受控 Worker pipeline、合成 Browser Runner soak、模拟 webhook 投递和 retention cleanup 检查。任一关键检查失败时进程返回非零；报告会写入 `archive_manifest`，其中包含结果包 hash、报告类型、Prometheus scrape 证据、告警投递状态、retention 证据和 Browser Runner soak 评估。

Docker 可用时运行 compose 演练：

```powershell
.venv\Scripts\python.exe -m deploy.compose_smoke `
  --output tmp\deployment-compose-smoke\compose-smoke.json `
  --artifact-root tmp\deployment-compose-smoke\artifacts `
  --soak-runs 10
```

compose 演练默认构建镜像，除非传入 `--skip-build`；它会启动 worker 和 browser-runner profiles、等待服务健康检查、针对 `127.0.0.1:5432` 上的 PostgreSQL 与 `127.0.0.1:9000` 上的 MinIO 运行 archive-ready deployment smoke，将保留的 artifact metadata 存到指定 artifact root，捕获近期 compose 日志，并在未传入 `--keep-running` 时关闭拓扑。报告满足 `status=pass`、`deploymentSmoke.status=pass` 和 `deploymentSmoke.archive_manifest.archiveReady=true` 时，可以作为发布交付证据。

如需 archive-ready 拓扑演练，传入共享 metadata DB 并保留 artifact：

```powershell
.venv\Scripts\python.exe -m apps.api.app.deployment_smoke `
  --database-url "postgresql+psycopg://user:pass@db:5432/ai_jsunpack" `
  --artifact-root tmp\deployment-smoke-artifacts `
  --soak-instances 4 `
  --soak-workers-per-instance 2 `
  --soak-runs 200 `
  --output tmp\deployment-smoke-postgres.json
```

持久化报告就是发布交付 artifact。将它与保留的 Artifact Store 目录或 object-store export 一起保存，便于审阅者同时核验 `archive_manifest.archiveReady`、`archive_manifest.artifactKinds`、`archive_manifest.retainedEvidence.resultPackageSha256`、webhook 投递、Prometheus 覆盖、retention cleanup 证据和 Browser Runner 容量评估。

## 失败诊断与回滚

先使用 `docker compose ... ps`；不健康的依赖通常能解释下游启动失败。

- DB 不健康：检查 `db` 日志、`deploy/env/db.env.example` 中的凭据，以及端口 `127.0.0.1:5432` 是否已被占用。
- MinIO 不健康或 bucket init 失败：检查 `artifact-store` 和 `artifact-store-init` 日志，再确认 `MINIO_ROOT_USER`、`MINIO_ROOT_PASSWORD` 与 `AI_JSUNPACK_ARTIFACT_S3_BUCKET` 在 env 文件中一致。
- API 立即退出：检查 `/health` 日志中的部署 profile 违规。API 不能接收 Worker sandbox、Browser Runner、Core CLI 或模型 provider 变量。
- Worker 空闲或 degraded：检查 `worker` 日志和 `/ops/metrics`；确认 source input 存在，且 `AI_JSUNPACK_WORKER_ID`、lease、DB 与 Artifact Store 配置指向共享拓扑。
- Browser Runner degraded：检查 `/health`、队列 backend 设置、lease 阈值，以及 Playwright 依赖是否已在镜像中安装。
- Prometheus 或告警检查失败：确认 auth secret 共享，且生成的 Bearer token 具备 ops read 权限。
- 结果包缺失：检查 Worker packaging 日志、保留的 Artifact Store 内容和 `deploymentSmoke.failedChecks`。

回滚时先保留证据，再回到上一组镜像 tag：

```powershell
docker compose -p ai-jsunpack-dev -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml logs --tail 200 > tmp\deployment-compose-smoke\compose-logs.txt
docker compose -p ai-jsunpack-dev -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml down
```

对于近生产演练，删除 volume 前保留 PostgreSQL volume/export、MinIO bucket export、`release-gate.json`、SBOM 文件、漏洞扫描输出和 compose smoke JSON 报告。回退 tag 后重新运行 `deploy.compose_smoke --skip-build`，并比较新的 `deploymentSmoke.archive_manifest.retainedEvidence.resultPackageSha256`、报告类型、Prometheus scrape 证据和 alert event history。

## Sandbox 与浏览器隔离 Profile

`build_artifact.resourcePolicy` 是执行隔离的审计契约。它为每次 build/typecheck validation 记录 `enforcement`、`runnerKind`、runtime metadata、capability status 和 known limitations。

支持的 runner profile：

| runnerKind | enforcement | 当前执行行为 | 审计含义 |
| --- | --- | --- | --- |
| `local` | `local_best_effort` | 在临时本地 workspace 中执行，使用命令 allowlist 和清理后的环境。 | 记录策略意图；不声明 OS/container 隔离。 |
| `container` | `container_enforced` | 通过 Docker/Podman 执行；Compose 使用命名 volume 与 `volume-subpath` 共享 attempt workspace。 | 记录 Docker/Podman 的网络、进程、内存、CPU 和文件系统能力差异。 |
| `gvisor` | `runtime_isolated` | 配置容器 runtime 时，通过 Docker 或 Podman + `--runtime runsc` 执行 build/typecheck。 | 用于部署将容器执行路由到 gVisor/runsc，并希望证据体现该边界的场景。 |
| `firecracker` | `runtime_isolated` | 配置 `AI_JSUNPACK_FIRECRACKER_RUNNER_COMMAND` 或 `buildValidation.firecrackerRunnerCommand` 时，通过部署方提供的 Firecracker launcher 执行；否则拒绝执行。 | 用于部署方拥有 Firecracker/KVM/jailer/rootfs 设置，以及跨 VM 边界 Artifact Store 交换的场景。 |
| `remote_browser_runner` | `remote_isolated` | 配置 `AI_JSUNPACK_BROWSER_RUNNER_URL` 时，通过独立 Browser Runner 服务执行 runtime smoke/compare；不执行 Worker build/typecheck 命令。 | 用于将 Playwright/browser 工作委托给独立 Browser Runner 服务，并由该服务承担 auth、egress 和 artifact exchange 控制。 |

高隔离 build profile 有意不回退到更弱 runner。如果设置 `AI_JSUNPACK_SANDBOX_RUNNER=gvisor` 但找不到或未配置 Docker/Podman runtime，validation 会写入带所选 profile 和 adapter limitation 的 `sandbox_denied` evidence。如果设置 `AI_JSUNPACK_SANDBOX_RUNNER=firecracker` 但没有 launcher command，validation 会写入 `sandbox_denied` evidence，而不是本地执行。`remote_browser_runner` 只可用于浏览器验证；build/typecheck 仍需要 `local`、`container`、`gvisor` 或已配置的 `firecracker`。

生产建议：

- 使用 `container` 作为当前可执行部署路径。
- 仅当 Docker 或 Podman 已配置 `runsc` runtime 时使用 `gvisor`。Worker 会使用配置的 container runtime 并传入 `--runtime runsc`，保持与 container runner 相同的 workspace/image/env-cleaning 行为，并在 `build_artifact.resourcePolicy` 中记录 `runtime_isolated`、`runnerKind=gvisor`、capability details、runtime version 和 limitations。
- 仅在 Linux 主机具备 KVM、jailer/rootfs provisioning、显式资源限制和跨 microVM Artifact Store transfer 时使用 `firecracker`。配置的 launcher 从 stdin 接收 JSON 请求，包含 `workspace`、`workingDirectory`、`command`、`environment`、`networkPolicy`、`resourcePolicy`、`timeoutMs` 和可选 `stdinBase64`；它必须在 stdout 打印 JSON，包含 `stdout`、`stderr`、`exitCode`、`timedOut`、`outputTruncated` 和 `failureClass`。
- `deploy/firecracker/launcher.py` 是生产 launcher 模板。它校验 Worker 协议、准备每次运行的 exchange directory、检查 kernel/rootfs/jailer/firecracker 前置条件，并将实际 KVM/jailer 执行委托给 deployment wrapper command。`deploy/firecracker/README.md` 定义部署验收清单、资源映射、网络隔离要求、Artifact Store exchange 边界和 JSON request/response 契约。
- 使用 `browser-runner` 服务边界隔离 Playwright/browser 执行。Worker 使用签名 worker service Bearer token 提交异步 `/browser-runs` 请求，轮询完成结果，并把 `executionBoundary` 以及 runtime trace/screenshot 证据记录到结果包中。
- browser-runner ASGI app 是 `apps.browser_runner.app.main:app`；部署时使用与 Worker 相同的 `AI_JSUNPACK_AUTH_SECRET`，并在镜像中安装 Playwright browsers。
- browser-runner 队列由 `AI_JSUNPACK_BROWSER_RUNNER_QUEUE_BACKEND` 选择。多实例部署使用 `postgresql` 和 `AI_JSUNPACK_BROWSER_RUNNER_QUEUE_DATABASE_URL`，与 metadata DB 共享；单实例本地运行才使用 `sqlite` 和 `AI_JSUNPACK_BROWSER_RUNNER_DB_PATH`。
- `AI_JSUNPACK_BROWSER_RUNNER_WORKERS`、`AI_JSUNPACK_BROWSER_RUNNER_MAX_ATTEMPTS`、`AI_JSUNPACK_BROWSER_RUNNER_LEASE_SECONDS`、`AI_JSUNPACK_BROWSER_RUNNER_RETRY_BACKOFF_SECONDS` 和 `AI_JSUNPACK_BROWSER_RUNNER_POLL_SECONDS` 控制每实例并发、重试、lease recovery 和调度节奏。
- `AI_JSUNPACK_BROWSER_RUNNER_MAX_QUEUE_AGE_MS`、`AI_JSUNPACK_BROWSER_RUNNER_MAX_CLAIM_LATENCY_MS`、`AI_JSUNPACK_BROWSER_RUNNER_MAX_EXPIRED_RUNNING` 和 `AI_JSUNPACK_BROWSER_RUNNER_MAX_RETRY_RATE` 定义 `/health`、`/browser-runs/metrics` 和审计证据使用的服务本地健康阈值。
- 服务启动时会 best-effort 恢复队列：过期的 `running` run 会重新入队直到达到 attempt cap，然后以 timeout 分类写出 `best_effort` evidence。
- `/health` 返回 `BrowserRunnerQueueHealth`，包含 backend status、queue metrics、worker settings 和 alerts；将它作为容器 readiness/liveness check。`/browser-runs/metrics` 要求 worker service Bearer token，并返回不带 health wrapper 的相同队列指标。
- API 暴露 `/ops/heartbeats`、`/ops/metrics` 和 `/ops/alerts` JSON 端点，用于共享 heartbeat 持久化、聚合 ops snapshot 和 best-effort alert webhook delivery。
- API 也暴露 `/ops/prometheus` 作为相同聚合 ops snapshot 的 Prometheus scrape surface。Scrape 请求必须包含具备 ops read 权限的 Bearer token；该端点有意不提供匿名指标，因为服务实例、队列、Job 状态和 alert label 都是敏感运维信息。
- `AI_JSUNPACK_OPS_HEARTBEAT_TTL_SECONDS` 控制 API、Worker 和 Browser Runner ops 记录的 heartbeat 过期时间；`AI_JSUNPACK_ALERT_WEBHOOK_URL` 和 `AI_JSUNPACK_ALERT_WEBHOOK_TIMEOUT_SECONDS` 控制 API alert delivery。
- 监控每个 Browser Runner 部署的 `queuedCount`、`oldestQueuedAgeMs`、`claimLatencyMs`、`averageRunDurationMs`、`retryRate`、`leaseRecoveryCount`、`expiredRunningCount` 和 `backendStatus`。当 backend status degraded、`expiredRunningCount` 非零、队列年龄或 claim latency 超过阈值、retry rate 超过阈值，或 queued runs 持续高于总 worker capacity 时触发告警。
- `BrowserRunSummary` 和 `runtime_trace.executionBoundary` 会记录 queue backend、run attempt、max attempts、worker id、lease recovery、retry policy、queue length、claim latency、run duration、retry rate、backend health 和 alert fields，使多实例调度在结果包中保持可审计。
