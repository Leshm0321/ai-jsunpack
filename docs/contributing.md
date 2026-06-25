# 贡献指南

本文档说明 AI JS Unpack 的协作流程、提交格式、验证要求和公开仓库边界。

## 协作原则

- 保持改动小而可审查，避免把无关重构和功能改动混在同一次提交里。
- 修改共享契约、API、Worker pipeline、Artifact、sandbox、认证或部署配置前，先阅读相关测试和文档。
- 行为变化必须配套测试、文档或明确的未验证说明。
- 生成产物、本地缓存、密钥、上传样本、临时调试资料和个人草稿不得进入 Git。
- 面向公开仓库的文档只放在 `README.md` 和 `docs/`；本地方案稿、迁移资料和未公开分析材料只放在被忽略的 `dev_docs/`。

## 工作流程

1. 从干净工作区开始，确认当前分支和待改范围。
2. 阅读相关模块、契约和测试，先确认现有行为再修改。
3. 按最小可审查单元提交代码、测试和文档。
4. 运行与改动范围匹配的验证命令。
5. 提交前检查 `git status --short`、`git diff --stat` 和新增文件列表。

## 提交信息规范

提交信息统一使用两行中文格式，不添加额外 trailers、模板说明或无关正文：

```text
<type>(<scope>): <中文标题>
- <中文说明。>
```

要求：

- `type` 使用 `feat`、`fix`、`docs`、`test`、`refactor`、`chore`、`build`、`ci`、`perf`、`security`。
- `scope` 使用稳定模块名，例如 `scaffold`、`core`、`api`、`worker`、`web`、`runtime`、`browser-runner`、`artifact-store`、`ops`、`audit`、`docs`、`deployment`、`sandbox`、`shared`、`agent-runtime`。
- 标题说明本次提交带来的目标结果，使用简洁中文动宾结构。
- 第二行只保留一条 `- ` 开头的说明，解释改动价值、约束或验证意义，不列文件清单。
- 不在提交信息中写入密钥、客户信息、未公开资料路径或本地草稿文件名。

示例：

```text
docs(contributing): 统一协作与提交规范
- 对齐当前中文提交格式、公开文档边界和提交前验证流程，避免本地资料进入提交历史。
```

## 验证要求

基础验证按改动范围选择执行：

```powershell
npm run check
npm run test:core
npm run build:web
.venv\Scripts\python.exe -m compileall apps packages tests
.venv\Scripts\python.exe -m unittest discover -s tests
```

范围验证：

- API 改动：运行 `tests/test_api_endpoints.py` 相关测试，并检查认证、权限和错误响应。
- Worker 改动：运行 queue、pipeline、runtime smoke、packaging 相关测试。
- Shared contract 改动：同步更新 TypeScript 与 Python models，并运行契约一致性测试。
- Sandbox 改动：验证 failure class、resource policy、超时和不降级行为。
- Browser Runner 改动：运行服务测试和 benchmark 相关测试，记录容量或兼容性影响。
- Web 改动：运行 `npm run build:web`，并做桌面和移动端 smoke。
- 部署改动：检查 `deploy/`、环境变量示例和部署配置测试。

如果验证无法运行，提交说明或 PR 描述必须写明原因、影响范围和替代检查。

## 文档规范

- `README.md` 保持项目名片定位：一句话简介、快速启动、核心特性和文档导航。
- `docs/` 存放可提交、可公开、可长期维护的深度文档。
- `docs/images/` 存放文档引用的静态资源；Markdown 图优先使用 ` ```mermaid ` 代码块。
- 文档中的命令必须对应当前 `package.json`、`pyproject.toml`、`deploy/` 或源码入口。
- README 和 docs 之间的链接必须真实存在，不能链接到被忽略的本地资料目录。
- 本地资料迁移到忽略目录后，不要在公开文档中继续引用原始私有路径。

## 本地资料与历史边界

- `dev_docs/` 是本地参考资料目录，默认不提交、不链接、不作为公开文档入口。
- `.gitignore` 已忽略 `dev_docs/`、`artifacts/`、`uploads/`、`tmp/`、`.venv/`、`node_modules/` 等本地或生成目录。
- 如果发现本地资料、敏感样本、密钥或不应公开的文件已经进入历史，先停止发布流程，再清理索引或重写历史。
- 历史重写会改变 commit hash；已经推送到远端后，需要协调后再 force push。
- 清理后用路径日志、对象列表和工作区状态验证，不只依赖文件系统是否删除。

常用只读检查：

```powershell
git status --short
git log --all --name-only --format="%H %s" -- <path>
git rev-list --objects --all | Select-String -Pattern "<name-or-path>"
git ls-files | Select-String -Pattern "<name-or-path>"
```

## 安全边界

本项目只面向自有代码、授权代码、合规安全审计、软件资产恢复、研究和内部治理场景。

禁止提交或引导以下用途：

- 绕过授权或访问控制。
- 窃取源码、提取秘密或复制第三方商业逻辑。
- 将未授权产物作为测试 fixture、演示资料或截图提交。
- 在文档、测试、日志或报告中提交真实密钥、token、客户代码片段或敏感截图。

## 提交前检查

- 改动范围与目标一致，没有无关重构。
- 新增或修改的 public interface 已记录。
- 失败路径、best-effort 路径和权限边界有测试或说明。
- 生成产物、缓存、临时文件和本地资料未进入 Git。
- 文档链接和命令与当前仓库结构一致。
- 最终说明包含已运行验证和未运行验证。
