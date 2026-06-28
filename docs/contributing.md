# 贡献指南

本文档说明 AI JS Unpack 的协作流程、提交规范、验证要求和安全边界。

## 协作原则

- 保持改动小而可审查，避免把无关重构和功能改动混在同一次提交里。
- 修改共享契约、API、Worker pipeline、Artifact、sandbox、认证或部署配置前，先阅读相关测试和文档。
- 行为变化必须配套测试、文档或明确的未验证说明。
- 生成产物、缓存、secret、token、上传样本、临时调试资料和个人草稿不得进入 Git。
- 公开文档只放在 `README.md` 和 `docs/`；本地方案草稿和未公开资料放在被忽略的 `dev_docs/`。

## 工作流程

1. 从当前工作区状态开始，确认分支、待改范围和是否已有他人改动。
2. 阅读相关模块、契约和测试，先确认现有行为再修改。
3. 按最小可审查单元提交代码、测试和文档。
4. 运行与改动范围匹配的验证命令。
5. 提交前检查 `git status --short`、`git diff --stat` 和新增文件列表。

## 提交信息规范

本仓库提交信息使用 Lore Commit Protocol：第一行说明为什么做这个改动，而不是逐项罗列改了什么；随后用 git-native trailers 记录约束、取舍、验证和风险。

格式：

```text
<intent line: why the change was made, not what changed>

<optional concise body: constraints and approach rationale>

Constraint: <external constraint that shaped the decision>
Rejected: <alternative considered> | <reason for rejection>
Confidence: <low|medium|high>
Scope-risk: <narrow|moderate|broad>
Directive: <forward-looking warning for future modifiers>
Tested: <what was verified>
Not-tested: <known gaps in verification>
```

规则：

- 第一行写决策目的，不写文件清单。
- trailers 只在提供决策上下文时使用。
- `Rejected:` 用于记录未来不应反复探索的替代方案。
- `Directive:` 用于给后续修改者的前向警告。
- `Tested:` 和 `Not-tested:` 必须真实反映验证情况。
- 不在提交信息中写入 secret、客户信息、未公开资料路径或本地草稿文件名。

示例：

```text
Keep local startup docs aligned with service-role validation

Constraint: API strict mode rejects Worker and Browser Runner environment variables.
Rejected: Keep manual-only startup steps | The dev script is now the primary local entrypoint.
Confidence: high
Scope-risk: narrow
Tested: npm run dev:check
Not-tested: Browser Runner manual smoke was not started.
```

## 验证要求

基础验证按改动范围选择执行：

```powershell
npm run check
npm run test:core
npm run build:web
.venv\Scripts\python.exe -m compileall apps packages tests deploy
.venv\Scripts\python.exe -m unittest discover -s tests
```

静态检查：

```powershell
.venv\Scripts\python.exe -m ruff check apps packages tests deploy
.venv\Scripts\python.exe -m bandit -c pyproject.toml -r apps packages deploy -x tests
```

范围验证：

- API 改动：认证、权限、Job、artifact、report、retention 和 Ops 测试。
- Worker 改动：queue、pipeline、agent runtime、runtime smoke/compare、packaging 测试。
- Shared contract 改动：TypeScript 与 Python 契约一致性测试。
- Sandbox 改动：runner kind、resource policy、timeout、failure class 和不降级行为。
- Browser Runner 改动：服务测试、queue backend、lease recovery、metrics 和 benchmark。
- Web 改动：`npm run build:web`，再做桌面和移动 smoke。
- 部署改动：Compose、env 模板、release gate、deployment smoke 和 archive 校验。

验证无法运行时，说明原因、风险和替代检查。

## 文档规范

- README 保持项目名片定位：一句话简介、快速启动、核心能力和文档导航。
- `docs/` 存放可提交、可公开、可长期维护的深度文档。
- Markdown 图优先使用 Mermaid；如需静态图片，放在 `docs/images/`。
- 文档中的命令必须对应当前 `package.json`、`pyproject.toml`、`deploy/` 或源码入口。
- README 和 docs 之间的链接必须真实存在，不链接到被忽略的本地资料目录。
- 移除或迁移本地资料后，不要在公开文档中继续引用原始私有路径。

## 本地资料与历史边界

- `dev_docs/` 是本地参考资料目录，默认不提交、不链接、不作为公开文档入口。
- `.gitignore` 已忽略 `dev_docs/`、`artifacts/`、`uploads/`、`tmp/`、`.venv/`、`node_modules/` 等本地或生成目录。
- 如果发现本地资料、敏感样本、secret 或不应公开的文件已经进入历史，先停止发布流程，再清理索引或重写历史。
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
- 在文档、测试、日志或报告中提交真实 secret、token、客户代码片段或敏感截图。

## 提交前检查

- 改动范围与目标一致，没有无关重构。
- 新增或修改的 public interface 已记录。
- 失败路径、best-effort 路径和权限边界有测试或说明。
- 生成产物、缓存、临时文件和本地资料未进入 Git。
- 文档链接和命令与当前仓库结构一致。
- 最终说明包含已运行验证和未运行验证。
