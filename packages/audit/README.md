# Audit 包

该包负责证据引用、回滚映射、可读报告和 Artifact lineage 工具。

当前脚手架职责：

- 将审计概念与 UI、Worker 编排保持分离。
- 为 `InferenceRecord`、`ReviewRun`、runtime evidence 和报告构建器提供目标位置。
- 保持产品规则：每次 AI 推断、确定性转换、工具调用、浏览器验证和修复动作都必须可追踪。
