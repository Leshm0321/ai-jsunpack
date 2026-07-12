# AI JS Unpack 文档

这里是 AI JS Unpack 的公开、长期维护文档入口。项目面向有授权的前端构建产物分析：输入目录、JavaScript 文件或归档包，输出可构建的还原工程、结构化审计记录和浏览器运行证据。

## 从哪里开始

| 目标 | 建议阅读 |
| --- | --- |
| 在本机启动 Web、API 和 Worker | [本地启动与验证](local-startup.md) |
| 配置 JSON/YAML、环境变量和设置中心 | [配置指南](configuration.md) |
| 理解服务、Worker pipeline 和多智能体 DAG | [架构设计](architecture.md) |
| 调用 FastAPI、Settings、Ops 或 Browser Runner | [API 参考](api.md) |
| 修改代码、运行测试和排查问题 | [开发指南](development.md) |
| 使用 Compose、发布门禁和隔离运行时 | [部署指南](deployment.md) |
| 提交 Issue、PR 和文档改动 | [贡献指南](contributing.md) |

## 文档边界

- `README.md` 是项目概览和最快启动入口。
- `docs/` 是产品、开发、API 和运维的中心文档，中文为当前事实源。
- `deploy/README.md` 与 `deploy/firecracker/README.md` 是部署实现的局部操作参考。
- `packages/*/README.md` 描述单个包的职责，不替代架构与开发文档。
- `design-system/MASTER.md` 是 Web 视觉实现约束，不是产品运行文档。
- `dev_docs/` 是被忽略的本地资料，不得从公开文档引用。

## 事实来源

文档中的行为应按以下顺序核对：

1. 公开契约与源码：`packages/shared`、`packages/configuration`、`apps/api`、`apps/worker`、`apps/browser_runner`、`packages/core`。
2. 可执行入口：`package.json`、`pyproject.toml`、`scripts/dev.mjs`、`deploy/*.py`。
3. 配置样例：`.example.env`、`config/ai-jsunpack.example.{yaml,json}`、`deploy/env/*.example`。
4. 本目录文档和局部 README。

如果文档与代码不一致，以当前实现为准，并在同一改动中更新文档。

## 维护检查

文档改动至少确认：

- 相对链接指向已跟踪文件。
- 命令在当前仓库中存在，并明确 PowerShell、POSIX 或 Docker 前提。
- API 路径与 FastAPI 路由一致。
- 配置字段区分启动配置、运行时设置和 secret 注入。
- 未把真实 token、客户输入、生产日志或敏感截图写入示例。
- 无法验证的行为明确标注为限制或待完成项。
