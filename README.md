# goalx

FastAPI backend + React web monorepo. 技术栈骨架沿用 ditto，剥离量化/业务依赖。

## Stack

| 层 | 选型 |
| --- | --- |
| Backend | Python 3.13, FastAPI, Pydantic v2, granian (Rust ASGI), loguru |
| Frontend | React 19, TanStack Router/Query, Tailwind CSS 4, Vite 8, TypeScript ~6.0 |
| Package managers | uv（Python，单 lockfile workspace）+ bun（Web，isolated linker） |
| Quality | ruff（lint+format）、basedpyright（strict）、Biome 2、tsc -b（strict）、pytest 9 / vitest 4、Playwright + axe |
| Contract | `contracts/openapi/v1.json` 单一事实源：redocly recommended-strict lint、openapi-typescript 生成 Web 类型、后端一致性测试 |
| CI/Hooks | GitHub Actions（fail-closed gate、CodeQL、gitleaks、OSV）、pre-commit（local 模式 ruff、gitleaks、conventional commits、no-commit-to-main） |
| Task runner | go-task（`Taskfile.yml`） |

## Quickstart

```bash
brew install uv go-task        # 一次性；node 用 .node-version 对应版本，bun 按 packageManager 锁定
task bootstrap                 # uv sync + bun install + Playwright chromium + pre-commit hooks
task server                    # API @ http://127.0.0.1:8000
task dev                       # Web @ http://127.0.0.1:5173（/api 代理到后端）
```

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `task check` | 本地全量门禁（含 e2e；CI 的 security 工作流除外） |
| `task test` / `task test-fast` | 后端测试（含 90% 覆盖率门禁 / 不含覆盖率） |
| `task type` | basedpyright strict |
| `task web-coverage` | Web 单测 + 覆盖率阈值 |
| `task web-e2e` | Playwright 冒烟 + a11y |
| `task contract-export` | 从应用重新导出 OpenAPI 契约（变更后提交 diff） |
| `task contract-codegen` | 重新生成 Web API 类型 |
| `task pre-commit-run` | 全仓跑一遍 git hooks |

修改 API 后：`task contract-export && task contract-codegen`，把 `contracts/` 与 `apps/web/src/api/generated/` 的 diff 一起提交（CI 的 `check-contract` 会校验无漂移）。
