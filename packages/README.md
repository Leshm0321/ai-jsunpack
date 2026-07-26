> 本文件只说明包的语言分类与引入方式。包的职责见各自的 `README.md`，整体架构见 [架构设计](../docs/architecture.md)。

`packages/` 下同时存放 TypeScript 和 Python 包，两者的依赖管理、构建方式和引入形式完全不同。目录名本身不体现语言，请以本表为准。

## TypeScript 包

由 npm workspaces 管理，参与 `tsc -b` 项目引用构建。

| 包 | 职责 | 被谁使用 |
| --- | --- | --- |
| `shared` | 跨语言契约、状态枚举、Artifact 类型和 schema 的事实源 | Web 前端与 `core` 同时依赖 |
| `core` | 输入规范化、AST/source map 分析、重建计划与可构建工程写出 | Python Worker 以子进程调用其 CLI |

引入方式是包名，由 workspace 软链解析：

```ts
import type { Artifact, Job } from "@ai-jsunpack/shared";
```

两个包都以 `dist/` 为入口（`main`/`types`），修改源码后需要构建才对使用方生效：

```bash
npm run build          # tsc -b packages/shared packages/core apps/web
```

`core` 额外提供 `bin`，Worker 通过 `packages/core/dist/cli.js` 跨语言调用它，路径解析见 `apps/worker/worker/core_bridge.py`。因此 **`core` 属于后端执行链，不是前端代码**，尽管它用 TypeScript 编写。

## Python 包

不参与 npm workspaces。虽然根 `package.json` 的 workspaces 声明为 `packages/*`，但这些目录没有 `package.json`，npm 会跳过，实际只解析上面三个 TS 包（含 `apps/web`）。

| 包 | 职责 |
| --- | --- |
| `configuration` | JSON/YAML 启动配置、环境覆盖、运行时设置模型、脱敏与 fingerprint |
| `sandbox` | local、container、gVisor、Firecracker 和远程浏览器执行策略 |
| `memory` | 任务内、项目级、实体和场景记忆证据 |
| `knowledge` | 框架、运行时、混淆模式和历史修复的确定性检索 |
| `deployment` | 服务角色和部署配置校验，阻止 API 携带 Worker 执行权限 |

这些包以命名空间包 `packages.*` 的形式引入，需要带 `packages.` 前缀：

```python
from packages.configuration import load_settings
from packages.deployment import validate_current_environment
```

`packages/` 下没有 `__init__.py`，依赖仓库根目录位于 `sys.path` 上：本地从仓库根运行即可，容器内由 `PYTHONPATH=/app` 提供（见 `deploy/docker/*.Dockerfile`），测试由 `pyproject.toml` 的 `[tool.pytest.ini_options]` 配置。

## 新增包时

- TypeScript 包需要 `package.json`，并加入 `npm run build` / `check` 的 `tsc -b` 序列。
- Python 包不需要 `package.json`；若要进入 Worker 镜像，必须在 `deploy/docker/worker.Dockerfile` 补一条 `COPY packages/<name> ./packages/<name>`，否则运行时缺失。
- 两类包都应带 `README.md` 说明职责，并在 `docs/architecture.md` 的共享包清单中登记。
