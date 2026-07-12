# Firecracker Launcher 部署

本目录包含 Worker Firecracker sandbox adapter 的生产 launcher 模板。

Worker 不直接启动 Firecracker。`packages.sandbox.FirecrackerSandboxRunner` 会向 `AI_JSUNPACK_FIRECRACKER_RUNNER_COMMAND` 或 `buildValidation.firecrackerRunnerCommand` 配置的命令发送 JSON 请求。`launcher.py` 校验该请求、准备 microVM 交换目录，并把实际 KVM/jailer 执行委托给部署方提供的 wrapper command。

## 主机前置条件

- Linux 主机，launcher 进程可以访问 `/dev/kvm`。
- 已安装 Firecracker 和 firecracker-jailer，并固定到部署版本。
- 已准备 guest kernel image 和 rootfs image。
- guest rootfs 包含 Node.js/npm，或包含生成工程所需的精确 build/typecheck 工具链。
- 使用非 root 服务用户，并为 jailer workspace 配置受限权限。
- 为 VM 边界内外传递 workspace 输入和证据输出准备 Artifact Store 访问路径。

## Worker 配置

示例：

```bash
AI_JSUNPACK_SANDBOX_RUNNER=firecracker
AI_JSUNPACK_SANDBOX_RUNTIME_NAME=firecracker
AI_JSUNPACK_SANDBOX_RUNTIME_VERSION=1.9.1
AI_JSUNPACK_FIRECRACKER_RUNNER_COMMAND="/usr/local/bin/ai-jsunpack-firecracker-launcher --kernel /srv/ai-jsunpack/firecracker/vmlinux --rootfs /srv/ai-jsunpack/firecracker/rootfs.ext4 --jailer /usr/bin/firecracker-jailer --firecracker /usr/bin/firecracker --socket-dir /run/ai-jsunpack/firecracker --wrapper-command /usr/local/bin/ai-jsunpack-firecracker-wrapper"
```

`deploy/env/worker.env.example` 将该值留空，因为本地开发和 CI 通常没有 KVM、kernel、rootfs 和 jailer 资产。

## 请求协议

Worker 向 launcher stdin 写入一个 JSON 对象：

```json
{
  "version": 1,
  "runnerKind": "firecracker",
  "workspace": "/tmp/ai-jsunpack-sandbox-abcd",
  "workingDirectory": "project",
  "command": ["npm", "run", "--ignore-scripts", "build"],
  "stdinBase64": null,
  "environment": {"PATH": "/usr/local/bin:/usr/bin"},
  "timeoutMs": 120000,
  "outputLimitBytes": 131072,
  "networkPolicy": "deny",
  "resourcePolicy": {
    "runnerKind": "firecracker",
    "enforcement": "runtime_isolated",
    "processLimit": 64,
    "cpuTimeLimitMs": 120000,
    "memoryLimitBytes": 536870912
  }
}
```

安全要求：

- `workspace` 必须是 Worker attempt 创建的现有目录。
- `workingDirectory` 必须是相对路径，且不能逃逸 `workspace`。
- `environment` 已由 Worker 清理；launcher 应只把这些值传入 guest。
- `networkPolicy=deny` 是默认值，必须创建无出站网络的 guest。
- `networkPolicy=allow` 必须通过部署策略显式映射，例如使用专用 tap 设备和出站 allowlist。

## Wrapper 契约

`launcher.py` 会向 `--wrapper-command` stdin 传入控制文档。wrapper 负责部署相关的 Firecracker 步骤：

- 创建 jailer workspace 和 Firecracker API socket。
- 创建或挂载 guest rootfs copy/overlay。
- 将 attempt workspace 复制或挂载到 guest 边界内。
- 使用配置的 kernel/rootfs 启动 Firecracker。
- 在 guest 内执行请求命令。
- 将 stdout/stderr 和输出 artifact 复制回 launcher exchange directory。
- 停止 microVM 并清理临时资源。

wrapper 可以直接打印最终 launcher response JSON，也可以打印普通 stdout/stderr，并让 launcher 在失败时把 wrapper exit code 归类为 `unknown`。

推荐 wrapper 响应：

```json
{
  "stdout": "build output",
  "stderr": "",
  "exitCode": 0,
  "timedOut": false,
  "outputTruncated": false,
  "failureClass": "none"
}
```

允许的 `failureClass` 值来自项目共享失败分类，包括 `sandbox_denied`、`timeout`、`resource_limit`、`dependency_missing`、`install_failed`、`build_error`、`type_error` 和 `unknown`。

## 资源映射

- `processLimit`：映射到 guest init policy、cgroup pids limit 或 jailer/cgroup 配置。
- `cpuTimeLimitMs`：映射到 wrapper timeout，并在可用时叠加 guest/cgroup CPU 控制。
- `memoryLimitBytes`：映射到 Firecracker machine memory 和主机 cgroup memory limit。
- `timeoutMs`：launcher 会在该 wall-clock timeout 后终止 wrapper；wrapper 也应在 guest 侧执行超时控制。
- `outputLimitBytes`：launcher 会截断 stdout/stderr，保证证据有界。

如果某个请求控制无法强制执行，wrapper 必须以 `sandbox_denied` 拒绝，或在 stderr 中说明限制并返回可审计失败。不能静默回退到本地执行。

## Artifact Store 交换

生产部署优先使用 Artifact Store 中介交换，而不是主机级共享路径：

- Worker 将生成工程物化到 attempt workspace。
- Launcher 只复制该 workspace 到每次运行独立的 exchange directory，或上传到 object-store prefix。
- Guest 将结果日志和生成证据写入 exchange directory 或 object-store prefix。
- Launcher 将 stdout/stderr 和退出元数据返回给 Worker；Worker 通过常规 Artifact Store 持久化 `build_log`、`build_artifact` 和 review evidence。

模板不内嵌 object-store 凭据。如果 wrapper 需要 object-store 访问权限，应将凭据限定到单次运行 prefix，并避免注入 API 和 Web 服务环境。

## 部署 Smoke 测试

在已经准备好真实 kernel、rootfs、jailer 和 Firecracker binary 的主机上，可以用 `--dry-run` 验证请求、路径和控制文档，而不启动 microVM。该模式仍会检查所有运行时资产，并创建 socket 目录，因此不是脱离部署依赖的纯协议测试：

```bash
workspace="$(mktemp -d)"
printf '{"version":1,"runnerKind":"firecracker","workspace":"%s","workingDirectory":".","command":["node","--version"],"stdinBase64":null,"environment":{},"timeoutMs":1000,"outputLimitBytes":4096,"networkPolicy":"deny","resourcePolicy":{"runnerKind":"firecracker"}}' "$workspace" \
  | /usr/local/bin/ai-jsunpack-firecracker-launcher \
      --kernel /srv/ai-jsunpack/firecracker/vmlinux \
      --rootfs /srv/ai-jsunpack/firecracker/rootfs.ext4 \
      --jailer /usr/bin/firecracker-jailer \
      --firecracker /usr/bin/firecracker \
      --socket-dir "$workspace/sockets" \
      --dry-run
```

生产验收检查：

- 缺少 kernel/rootfs/jailer/firecracker 时返回 `sandbox_denied`，不能本地回退。
- `workingDirectory=..` 返回 `sandbox_denied`。
- `networkPolicy=deny` 会生成无出站网络的 guest。
- 命令超过 `timeoutMs` 时返回 `failureClass=timeout`。
- 输出过量时设置 `outputTruncated=true`。
- Wrapper 应把非零 guest 命令映射为结构化响应中的明确 `failureClass`（例如 `build_error` 或 `type_error`）；如果只返回普通非零退出码和非 JSON stdout，launcher 会保守归类为 `unknown`。
- `build_artifact.resourcePolicy.runnerKind` 为 `firecracker`，`enforcement` 为 `runtime_isolated`。
- Worker 报告保留 `build_log`、`build_artifact`、Review evidence 和 result package lineage。
