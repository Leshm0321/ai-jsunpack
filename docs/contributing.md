# 贡献指南

本文档说明 AI JS Unpack 的协作、提交、测试和安全边界约定。

## 工作原则

- 优先保持小而可审查的改动。
- 先理解现有契约和测试，再修改共享行为。
- 对 Job、Artifact、API、Worker pipeline、sandbox 和认证相关改动保持高验证标准。
- 不提交本地运行产物、密钥、`.venv`、`node_modules`、`artifacts`、`tmp`、`dev_docs`。
- 文档面向提交的内容放在 `docs/`；个人草稿和历史实现资料放在被忽略的 `dev_docs/`。

## 分支与提交

提交信息遵循 Lore Commit Protocol：

```text
<intent line: why the change was made, not what changed>

Constraint: <external constraint that shaped the decision>
Rejected: <alternative considered> | <reason for rejection>
Confidence: <low|medium|high>
Scope-risk: <narrow|moderate|broad>
Directive: <forward-looking warning for future modifiers>
Tested: <what was verified>
Not-tested: <known gaps in verification>
```

示例：

```text
Document the public onboarding path for GitHub readers

Constraint: dev_docs is intentionally ignored and cannot be the submitted documentation surface.
Confidence: high
Scope-risk: narrow
Tested: README/docs link structure checked; npm run check
Not-tested: browser-rendered Markdown preview
```

## 代码改动验证

基础验证：

```powershell
npm run check
npm run test:core
npm run build:web
.venv\Scripts\python.exe -m compileall apps packages tests
.venv\Scripts\python.exe -m unittest discover -s tests
```

按改动范围补充：

- API 改动：运行 `tests/test_api_endpoints.py` 相关测试，并检查认证和权限边界。
- Worker 改动：运行 pipeline、queue、runtime smoke、packaging 相关测试。
- Shared contract 改动：同时更新 TypeScript 与 Python models，并运行契约一致性测试。
- Sandbox 改动：验证 failure class、resource policy 和不降级行为。
- Web 改动：运行 `npm run build:web`，并做桌面/移动 smoke。

## 文档改动验证

- README 中的链接必须指向真实文件。
- Mermaid 代码块使用 ` ```mermaid `。
- 文档中的命令必须能对应当前 `package.json`、`pyproject.toml` 或源码入口。
- 不把被忽略的 `dev_docs/` 当作提交文档入口。

## 安全边界

本项目只面向自有代码、授权代码、合规安全审计、软件资产恢复、研究和内部治理场景。

禁止提交或引导以下用途：

- 绕过授权或访问控制。
- 窃取源码、提取秘密或复制第三方商业逻辑。
- 将未授权产物作为测试 fixture 或演示资料提交。
- 在文档、测试或日志中提交真实密钥、token、客户代码片段或敏感截图。

## Review Checklist

提交前检查：

- 改动范围与目标一致，没有无关重构。
- 新增或修改的 public interface 已记录。
- 失败路径、best-effort 路径和权限边界有测试或说明。
- 生成产物、缓存、临时文件未进入 Git。
- 最终报告中明确说明已运行和未运行的验证。
