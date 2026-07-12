# 贡献指南

本指南说明 Issue、PR、提交信息、验证和安全边界。仓库当前没有根级 `CONTRIBUTING.md` 或持久化 `AGENTS.md`；在这些入口补齐前，本页是公开贡献流程的详细说明，但会话或执行环境提供的更高优先级指令仍优先。

## 开始前

- 仅处理自有、授权或合规审计范围内的输入和证据。
- 查看 `git status --short`，不要覆盖不属于本次任务的现有改动。
- 阅读目标代码、相邻测试、[开发指南](development.md) 和相关领域文档。
- 功能、修复、测试和文档尽量保持在一个可审查主题内。
- 不引入新依赖，除非变更确实需要且说明维护、安全和许可影响。

## Issue 流程

提交公开 Issue 时包含：

1. 可观察问题或目标结果。
2. 最小复现步骤和输入类型，不附带未授权 bundle 或客户代码。
3. 操作系统、Node/Python 版本、部署 profile 和相关配置的脱敏摘要。
4. 实际结果、期望结果和已运行检查。
5. 相关日志的最小片段；删除 token、URL 凭据、客户标识和敏感路径。

仓库当前没有 `SECURITY.md` 或公开的私密报告渠道。不要在公开 Issue 发布可利用漏洞细节、真实 secret 或客户资料；先通过项目维护者认可的私密渠道协调。补齐正式安全策略是仓库治理待办。

## PR 流程

1. 从最新目标分支创建短生命周期分支。
2. 先用测试或可重复命令证明现状。
3. 实现最小安全改动，并补充回归测试。
4. 更新受影响的公开契约、配置样例和文档。
5. 运行定向测试，再运行适合风险等级的基础回归。
6. 检查 `git diff --check`、`git status --short` 和新增文件。
7. PR 描述列出目标、关键取舍、验证证据、未验证项和迁移/回滚影响。

评审重点：

- 行为和契约是否与测试一致。
- Agent、sandbox、浏览器和 secret 边界是否保持 fail closed。
- 失败与 best-effort 路径是否可审计。
- 配置字段是否有实际消费者，而不只是 UI/API 存储。
- 文档命令和 API 路径是否可执行。

## 提交信息

仓库历史统一使用 Conventional Commit 风格的单行提交信息。格式为：

```text
<type>(<scope>): <中文主题> - <中文变更说明>
```

对于包含多个独立结果的大型提交，可以在同一行继续追加说明：

```text
<type>(<scope>): <中文主题> - <说明一> - <说明二> - <说明三>
```

历史提交没有使用独立正文或 git trailers。提交时保持单行格式，不要混用另一套提交模板。

### Type

使用历史中已经出现的类型：

- `feat`：新增或完成产品、平台、Worker、运行时等能力。
- `docs`：文档、环境模板说明和开发注释更新。
- `refactor`：保持外部行为的结构拆分、边界收敛和清理。
- `ci`：CI、发布门禁、证据归档和自动化交付。
- `build`：构建、镜像或部署拓扑的构建入口。
- `test`：以测试覆盖或验证能力为主要目标。
- `chore`：仓库维护、忽略规则和不属于以上类型的整理。

不要为了描述更细而随意发明新 type；优先选择与历史最接近的类型。

### Scope

Scope 使用小写领域名；多个单词使用连字符。常见历史 scope 包括：

- `worker`、`agent-runtime`、`runtime`
- `core`、`api`、`web`、`browser-runner`
- `deployment`、`deploy`、`sandbox`、`security`、`ops`
- `docs`、`readme`、`env`、`dev`
- `gitignore`、`audit`、`contracts`、`modules`

Scope 应描述主要改动边界，而不是逐个列出文件。一次提交跨越多个目录时，选择能够概括目标的上层领域。

### 中文主题与说明

- 主题简短说明完成了什么，例如“补齐本地启动环境模板”或“拆分 Agent Runtime 分层架构”。
- ` - ` 后说明实现范围、关键行为和配套验证；不要只重复主题。
- 使用中文叙述，保留必要的 API、Worker、CrewAI、OpenAI-compatible 等技术名称。
- 通常只写一条说明；只有确实包含多个主要结果时才追加多条。
- 整条提交信息保持单行，不在标题前添加空格。
- 不写 secret、客户信息、未公开资料路径或临时调试内容。

示例：

```text
docs(dev): 对齐贡献指南提交信息 - 根据仓库历史统一 type(scope)、中文主题和单行变更说明。
```

大型提交示例：

```text
feat(platform): 打通可配置的多智能体工作流 - 引入 JSON/YAML 启动配置和设置修订 - 增加多智能体 DAG 校验与并行专家阶段 - 强化归档资源限制和生产环境 fail-closed 策略。
```

## 验证要求

基础回归：

```powershell
npm run check
npm run test:core
npm run build:web
python -m compileall apps packages tests deploy
python -m unittest discover -s tests
```

静态与安全检查：

```powershell
python -m pip install -e .[dev]
python -m ruff check apps packages tests deploy
python -m bandit -c pyproject.toml -r apps packages deploy -x tests
```

`npm run dev:check` 是轻量反馈，只包含 TypeScript check、Core tests 和 compileall，不替代完整 PR 或发布验证。

按范围选择的测试矩阵见 [开发指南](development.md#按改动选择测试)。

### CI 与 Release Gate

`.github/workflows/release-gate.yml` 当前运行：

- `npm run check`
- `npm run test:core`
- `npm run build:web`
- Python compileall（当前不包含 `deploy`）
- Python unittest
- release gate、镜像、SBOM、扫描和 Compose smoke

它当前不运行 Ruff 或 Bandit。更重要的是，workflow 使用 `npm ci`，但仓库忽略且未跟踪 `package-lock.json`；干净检出会在安装阶段失败。生产发布前必须先决定并落实 lockfile 策略：推荐跟踪 `package-lock.json` 并保留 `npm ci`。在修复并由真实 CI 证明前，不要把 Release Gate 描述为已可用的干净检出门禁。

## 文档贡献

- `README.md` 保持项目定位、快速启动和中心导航。
- `docs/` 是产品、API、开发和运维的中心文档。
- `deploy/**/README.md` 与 `packages/*/README.md` 可以保存局部操作和包职责。
- 中文是当前 `docs/` 事实源；不维护重复的英文页面。
- Markdown 图优先使用 Mermaid。
- 命令必须对应当前脚本或入口，注明 shell 和依赖前提。
- API 文档必须覆盖当前 route decorators。
- 配置文档必须区分启动配置、Settings 存储、Worker 消费和 secret 注入。
- 不链接被忽略的 `dev_docs/`、临时 Artifact 或个人草稿。

## 安全边界

禁止提交或引导以下用途：

- 绕过授权、访问控制或软件许可。
- 窃取源码、secret 或第三方商业逻辑。
- 把未授权产物作为 fixture、演示或截图提交。
- 在文档、测试、日志或报告中包含真实 token、客户代码、生产 URL 凭据或敏感截图。
- 为生产环境弱化 sandbox、Browser Runner 或 service-role 隔离，并静默回退到本地执行。

修改上传、归档解压、Artifact 下载、webhook、模型 endpoint 或 sandbox 命令时，必须覆盖路径、大小、协议、凭据和网络边界。

## 生成物与历史

不要提交：

- `.env` 和本地 JSON/YAML 配置。
- `.venv/`、`node_modules/`、`.crewai-data/`。
- `artifacts/`、`uploads/`、`tmp/`、测试报告和覆盖率目录。
- `dev_docs/`、客户输入、生产日志、数据库快照或 object-store export。

如果敏感文件已经进入 Git 历史，停止发布流程并协调历史清理。历史重写会改变 commit hash，不能在已共享分支上擅自 force push。

## 提交前清单

- 改动范围与目标一致，没有混入无关重构。
- public interface、配置字段和状态变化已记录。
- 回归测试覆盖成功、失败、权限和 best-effort 路径。
- 文档链接、命令和 API 路径与当前仓库一致。
- 生成物、缓存和本地资料未进入 Git。
- 没有 secret、未授权输入或敏感证据。
- 最终说明列出已运行与未运行验证。
