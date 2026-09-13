# 领域包拥有其表 SQL；跨包访问走包内函数

2026-09 架构评审发现两条持久化约定并存：M1 建立的 `store/` 仓储层覆盖
fixtures/odds/bets/results/forecasts 表，而 M2/M3.x 每个里程碑新增的表
（backtest_*、clv_records、haircut_calibrations、验证表等）由各领域模块
内嵌 SQL 直连——`quote_evidence` 甚至 import 了仓储又自行查询
odds_snapshots。两条约定都付了成本，哪条都无法在 review 中执行。

裁决（用户 2026-09-13 批准，按候选全部落地）：

1. **表 SQL 归属其领域包**：`data/`（fixtures/odds_snapshots/draw_results/
   hist_matches/cost_ledger 及 `data/ingest/` 采集）、`modelling/`
   （forecasts/team_aliases）、`evaluation/`（backtest_*/clv_records/
   haircut_calibrations/验证表）、`betting/`（bets/settlements/bankroll/
   bet_slips）。包内模块可以直接写本包表的 SQL；**包外（含 api/cli/flows
   等交付层）不允许裸 SQL，必须走包内函数**。
2. `store/` 解散：仓储模块并入所属领域包（store/fixtures→data/fixtures、
   store/betting→betting/store、store/results→data/results、
   store/forecasts 并入 modelling/forecast）。"模块拥有自己表的 SQL"
   的粒度是**包**而非文件——包内允许拆分（如 betting/store 持 SQL、
   use-case 模块编排）。
3. 例外：`db.py`/`migrations.py`（infra）拥有 schema_migrations 与 DDL；
   共享纯规则模块（settlement/odds_math/score_matrix/markets）保持无 SQL。

不选"反向做大 store/"：评估簇的 SQL 与回放循环交织，抽到仓储层只会产生
浅模块。本裁决与 ADR-0003（SQLite 单机）正交，不改变存储选型。
