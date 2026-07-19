# Sandbox 包

该包承载 Worker 在 build、typecheck、install 和 runtime validation 阶段使用的受控 Sandbox Runner 接口。

runner 必须具备的行为：

- 使用每次 attempt 独立的临时目录。
- 清理继承的凭据和敏感环境变量。
- 默认禁止网络访问。
- 强制执行命令 allowlist、超时和输出限制。
- 捕获 stdout、stderr、退出码、耗时、资源使用和失败分类。

当前实现：

- `LocalSandboxRunner` 提供 Local Sandbox Runner：每次 attempt 使用独立临时 workspace，并仅以 argv 形式执行子进程。
- `SandboxPolicy` 定义命令 allowlist、超时、输出限制、环境变量 allowlist 和 `network_policy`。
- Local Sandbox Runner 会记录网络策略契约，但暂不提供 OS 级网络隔离。
