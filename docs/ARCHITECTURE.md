# goalx 架构总览

一句话：个人大陆足彩分析与记录系统——外部数据采集 → 概率建模 → 资格/价值判定 → 纸面投注与结算复盘，FastAPI backend + React web，单机本地运行、人工下单。

本文是结构性导览。分工：**意图与边界**看 [spec](plans/goalx-quant/spec.md)，**术语**看 [CONTEXT.md](../CONTEXT.md)，**不可逆决策及其理由**看 [ADR](adr/)，**操作手册**看 [README](../README.md)。本文只回答"系统长什么样、东西在哪、为什么这样分"。

## 系统边界（spec §1 的工程投影）

- 个人、本地 Mac 运行；不做自动下单、多用户或 SaaS。纯分析 + 人工下单 + 真实票据回录。
- 先纸面验证再谈真金：整赛季、CLV ≥200 注且 beat ≥60%、skill ≥0、复核无系统性错误之前，不放行真钱推荐。
- 外部数据预算 ≤$50/月；SQLite 单机存储（ADR-0003）、Prefect 编排（ADR-0005），不引入分布式组件。

## 数据流

```mermaid
flowchart LR
    subgraph 外部源
        A[体彩官方网关\n竞彩全玩法快照]
        B[The Odds API\n欧洲赔率]
        C[football-data\n五大联赛历史]
        D[官方开奖比分]
    end
    subgraph data 域
        E[ingest 采集\nsporttery/oddsapi/fdhist/results]
        F[观测证据\nquote_observations\n+ data/observations 原始响应]
    end
    G[(SQLite WAL\ndata/goalx.db\nappend-only 快照)]
    subgraph modelling 域
        H[DC 分池训练\ndc_model + team_align]
        I[10×10 比分矩阵\nscore_matrix]
        J[Forecast\n哈希存证]
    end
    subgraph evaluation 域
        K[资格判定 had-quote\n新鲜度/两源时差]
        L[walk-forward 回测\nhaircut 校准]
        M[前瞻验证 / CLV]
    end
    subgraph betting 域
        N[Bet paper|live\n建议→回录→锁定]
        O[Settlement\n开奖结算/更正冲正]
        P[Bankroll / Cost 台账]
    end
    subgraph 交付层
        Q[FastAPI api/]
        R[Web 六页运营台]
        S[Prefect flows\n+ schedules 常驻]
    end
    A & B & C & D --> E --> F --> G
    G --> H --> I --> J
    J --> K --> N
    G --> L & M
    D --> O
    N --> O --> P
    G --> Q --> R
    S -.驱动.-> E & J & O
```

关键语义（各处详细规则见 spec §5-§7 与 ADR-0001/0002）：

- **开奖是唯一事实源**；赔率与预测都是 append-only 时点快照。时间区分 `source_updated_at` / `observed_at` / 入库时间，未知可空、不倒填。
- **paper 与 live 是同一个 Bet 实体**（mode 字段），只有已购 live 产生真钱流水；未购建议的反事实统计不混入正式成绩。
- **正式候选门槛**：同义市场、同公司完整三向、可信匹配、新鲜度与两源时差达标（默认各 5 分钟）；不合格只观察不推荐，拒绝原因要可见。

## 领域包（ADR-0008：表 SQL 归属其领域包，跨包访问走包内函数）

| 包 | 职责 | 要点 |
| --- | --- | --- |
| `data/` | 采集与证据 | `ingest/`（sporttery 竞彩、oddsapi 欧赔、fdhist 历史、results 开奖）；fixtures/odds/results 仓储；`quote_evidence`（票 35 报价证据）；`today`（今日页聚合） |
| `modelling/` | 概率基座 | `dc_model` Dixon-Coles 分池训练、`team_align` 队名对齐、`score_matrix` 10×10 比分矩阵（ADR-0006，had/hhad/ttg/crs 的共同表示）、`forecast` 在售预测 + 哈希存证 |
| `evaluation/` | 验证 | `backtest` walk-forward 回测（ADR-0007，had-only）、`baseline` PSC/AvgC 分期质检、`haircut` 折价校准、`clv` 收盘对账、`forward_validation` 前瞻验证（票 34：市场 skill 只来自前瞻集合，不读回测）、`metrics`/`validation` |
| `betting/` | 生命周期 | `store`/`bets` 建议与回录、`settle` 结算编排与更正重算、`ledger_audit` 只读旧账核查 |
| 根级共享 | — | `models.py`（Pydantic 实体）、`migrations.py`（schema v1→v5）、`db.py`、`config.py`、`markets.py`（竞彩选项网格，结算与迁移共用）、`odds_math.py`（隐含概率/Shin 去晦/EV）、`settlement.py`（**纯函数结算引擎**：无效腿赔率按 1 等官方规则，无存储依赖；`betting/` 负责编排落库） |
| 交付层 | — | `api/`（fixtures/bets/results/validation 四组路由，契约优先）、`cli.py`（采集/训练/回测/对账命令行）、`flows.py`（9 个 Prefect flow）、`schedules.py`（票 37 单进程 `serve` 入口）、`main.py`/`server.py` |

Web（`apps/web/`）：TanStack Router 六页——今日 `/`、复核 `/review`（M3 占位）、投注 `/bets`、资金 `/bankroll`、验证 `/validation`、设置 `/settings`（M4 占位）。契约驱动：`contracts/openapi/v1.json` 单一事实源 → `openapi-typescript` 生成 web 类型，CI 校验无漂移。e2e 的 `paper-loop.spec.ts`（12 用例）就是当前阶段的"可执行用户故事"。

## 运行形态

- **开发**：`task server`（API）+ `task dev`（web，/api 代理）；数据落 `data/goalx.db`（gitignored）。
- **连续运行（票 37 协议 v1）**：`task serve-schedules` 单进程常驻，无需 Prefect server——daily-capture 10:00/19:00（竞彩→预测→范围内欧赔）、eu-odds-closing 每 30 分钟（无窗口场次零成本跳过）、daily-wrap 23:30（结算批跑+CLV 对账+账务核查）。节奏口径冻结于 [run-protocol-v1](plans/goalx-quant/run-protocol-v1.md) §3。
- **备选**：Prefect server + worker 按 `prefect.yaml` 注册 deployments（同一批 flows）。

## 决策索引

| ADR | 一句话 |
| --- | --- |
| [0001](adr/0001-draw-result-single-source-of-truth.md) | 开奖为唯一事实源；赔率/预测 append-only 时点快照 |
| [0002](adr/0002-unified-bet-entity.md) | 纸面与真金统一为单一 Bet 实体（mode: paper \| live） |
| [0003](adr/0003-sqlite-single-node-storage.md) | SQLite 单机存储（WAL） |
| [0004](adr/0004-openai-agents-for-llm-orchestration.md) | LLM 编排用 OpenAI Agents SDK（M3 暂缓中，决策保留） |
| [0005](adr/0005-prefect-task-orchestration.md) | Prefect 统一任务编排 |
| [0006](adr/0006-score-matrix-canonical-probability.md) | 比分矩阵 10×10 为规范概率表示 |
| [0007](adr/0007-euro-close-proxy-backtest-baseline.md) | 回测以有质量标记的欧赔为基准，合成价仅用于模型实验 |
| [0008](adr/0008-domain-packages-own-their-tables.md) | 领域包拥有其表 SQL；跨包访问走包内函数 |

## 质量门禁

`task check` 本地全量：ruff（strict）→ basedpyright strict → pytest（覆盖率 ≥90%）→ Biome/tsc → vitest + Playwright + axe → 契约防漂移。`main` 受 pre-commit `no-commit-to-branch` 与 fail-closed CI 保护；提交走 Conventional Commits。API 变更流程：改 FastAPI → `task contract-export` → 审 diff → `task contract-codegen` → 一并提交。
