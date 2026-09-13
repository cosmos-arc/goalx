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

## M1 运行手册（数据地基）

M1 = 记录复盘工具：竞彩采集 → 欧赔对照 → 投注建议/回录 → 开奖导入 → 结算 → 复盘。
数据落 `data/goalx.db`（SQLite WAL，已 gitignore）；凭据在 `.env`（见 `.env.template`）。

### 一次性初始化

```bash
task db-migrate        # 建 schema（v1：六域 25 表）
task ingest-hist       # 五大 2023-26 三季回测底座（~5,257 行，幂等可重跑）
```

### 每日采集

```bash
task ingest-jingcai    # 竞彩官方全玩法快照（append-only，保留调盘时点）
task ingest-odds       # The Odds API 欧赔 + join（credit 护栏：日 40/月 480）
```

销售期高频轮询走 Prefect（ADR 0005）：

```bash
uv run prefect server start          # 本地 server（另一个终端）
uv run prefect deploy goalx_backend.flows:jingcai-snapshot   # 后续按需设 cron
```

flows：`jingcai-snapshot` / `eu-odds-snapshot` / `fd-history-import` / `settlement-sweep`。

### 纸面闭环（票 23 验收路径）

1. 今日页（`task dev` → `/`）：竞彩 vs 欧洲共识（Shin 去晦）、EV、books、调盘时点；
2. 投注页（`/bets`）：建注建议 → 勾选实际购买子集做票级回录；
3. 赛后：投注页导入官方比分（DrawResult，唯一事实源）→「结算批跑」；
4. 复盘列表展示状态/盈亏；资金页（`/bankroll`）只受真金（live）影响。

结算口径（研究 01/票 06）：单关退款、串关无效腿赔率按 1、去除后不足 2 关整单退款、
任9 复式按组合逐个判定。

### 手工补跑

`task settle`（结算批跑）、`uv run python -m goalx_backend.cli --help`。
