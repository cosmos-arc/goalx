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

## M2 运行手册（ML 线 + 回测）

M2 = 概率基座 + 验证：DC 分池训练 → 在售场次 Forecast（哈希存证）→
walk-forward 回测 → 指标/haircut 校准/CLV 对账 → 验证页（`/validation`）。
schema v3 追加 backtest/haircut/clv 表；模型工件落 `data/models/`（gitignored）。

### 一次性初始化

```bash
task db-migrate                                     # v3：回测/校准/CLV 表
uv run python -m goalx_backend.cli ingest-hist      # 五大 5 季（含 2122/2223 暖机，~8,900 行）
uv run python -m goalx_backend.cli train-models --bootstrap 50   # 五大 DC + bootstrap（~30s）
# 可选 Tier2：荷甲需先导入 N1 历史（fdhist competitions 参数）再 train-models --competitions E0 D1 SP1 I1 F1 N1
```

### 每日/每周

```bash
uv run python -m goalx_backend.cli forecast         # 每日：在售场次 ML Forecast（幂等，跳过清单入日志）
uv run python -m goalx_backend.cli train-models     # 每周：重估工件（内容寻址，自动版本化）
```

Prefect flows：`weekly-train` / `forecast-daily`（与 M1 采集 flows 并存）。

### 回测与校准（票 28-30）

```bash
uv run python -m goalx_backend.cli calibrate-haircut   # 竞彩 vs 欧共识 → haircut 分布（样本<30 回落默认）
uv run python -m goalx_backend.cli backtest --label m2   # 三季 walk-forward（--haircut auto 用校准值）
```

口径（ADR 0007 + 用户裁决 2026-09-13，含一处实现期偏离）：fair = Pinnacle
收盘 Shin（AvgC 兜底）；模拟竞彩价 = fair × (1−haircut)；EV≥1.5% + 1/4
Kelly（单注 1% 上限）；每周每联赛至多一笔 2串1；结算复用 Settlement 引擎；
防前视断言常开。

**偏离记录（had-only）**：裁决原为模拟 had+hhad+ttg，但实现期实测发现
1X2→比分矩阵反推（penaltyblog goal_expectancy）在总进球维度系统性欠分散
（6,174 场对照：ttg 桶 5 隐含 7.1% vs 实际 9.1%；hhad 实际 33% < 隐含
40%），由反推派生的 hhad/ttg 模拟价会制造假 edge。v1 回测只对 had 下注
（定价直接来自 fair 1X2，无反推）；待真实 totals/handicap 报价接入后再
启用另两玩法。三季基线结果：RPS skill −3.75%（DM p≈1e-18，市场显著更
优）、flat ROI −25.3%（t=−8.6）——与文献共识一致，验证了「模型须先 ≥
市场才谈下注」的通过线设计。

### CLV 收盘窗口（票 32）

部署 cron 在 kickoff −30/−10/−1min 附近触发（每 sport×market 记 1 credit；
按当前在售规模 ~5 sport × 8 次/日 ≈ 40 credits/日，与日预算持平——建议只在
有 Tier1 场次开球的日子开窗，或把日预算调到 60）：

```bash
uv run python -m goalx_backend.cli closing-snapshot   # 窗口内(35min)场次 purpose=closing 快照
uv run python -m goalx_backend.cli clv-reconcile      # 已结算注单 CLV 对账 + beat rate/回归报表
```

CLV_proxy = Shin(收盘) − 1/竞彩买入价（概率域）；验证页三条件之一
（≥200 注 beat≥60%）。
