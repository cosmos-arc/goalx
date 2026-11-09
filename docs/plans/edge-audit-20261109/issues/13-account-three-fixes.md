# 13: 账户面三修 + 反例单测

Parent: 票 10（伞）。同分支同 PR。

**What to build:** 账户聚合三处修复恢复证据资格——税后利润按「同人+同期+同游戏」归集计税（竞彩期=票内最晚比赛编号日期，复用既有期次归集机制）；sample_sufficient 分母收窄为已结算、合格、按场独立的暴露数；CLV 聚合按场聚类（同场多锚并一）。外部审计三个合成反例（p 值组结构除外——属票 12）转正为单测。

**Blocked by:** None (can start immediately)

**Status:** resolved

- [x] 同期同游戏两笔 6000 元奖金 → 税 2400（非 0）单测过
- [x] 200 条未结/全部同场记录 → sample_sufficient=false 单测过
- [x] 同场复制行 CLV SE 收敛到按场独立量级（外部审计数值例）单测过
- [x] ROI 报告同时给等注口径与按场口径双栏
- [x] 既有账户面测试全绿；读面字段语义注释更新

## 不变量与人裁决项

- 读面字段名不改（晨检与前端引用）。
- 税口径=2024 年第 12 号公告（>1 万全额 20%、同期同游戏合并）。
- 纸面历史利润重算差异如实呈现，不回改历史行（append 纪律）。

## Answer

分支 `edge-audit-20261109-13-account-fixes`，commit `41343d9`（fix(betting)）。三修全部落在既有读面函数层（`paper_account_face` / `edge_source_faces`），外部审计三个合成反例转正为单测：

1. **税期归集**（`account._after_tax`）：won 注奖金按（同人[纸面单一纳税人假设]、同期、同游戏）归集后套 `tax_on_prize`——竞彩期=票内最晚比赛编号日期（`data/fixtures.jingcai_business_dates` 批量助手，SQL 留 data 包，ADR-0008）；池票按（池游戏, 期号）；无期次信息注保守自成一计税组。反例断言：同期（业务日 2026-11-08）同游戏两笔 ¥6000 → `tax_total=2400.0`、`profit_after_tax=9596.0`（旧逐注口径税 0）；对照组跨期两笔 ¥6000 → 税 0（期次隔离不被过度合并）。
2. **sample_sufficient 分母**：新读面字段 `independent_exposures_n`（已结算、非 void、按场去重的暴露数），`sample_sufficient` 改据此判定（≠total_bets_n）。反例断言：200 条 open 注全部同一场 → `independent_exposures_n=0`、`sample_sufficient=false`；同场 200 注已结算 → 1 场仍 false；跨 200 场各 1 注 → true。
3. **CLV 按场聚类**（`edge_source_faces`）：clv 行按票内场次集聚类，同场多锚并成场均值，均值/标准误跨场统计（<2 场 SE 诚实 None）。反例断言（审计数值例）：两场各 25 注、场内 CLV 0.03/0.01 → `clv_se_last50=0.01`（旧按行独立 0.001429，精度虚增 7 倍），`clv_mean(_last50)=0.02`。
4. **ROI 双栏**：`StrategyFace.realized_roi_per_match`（单场注先并成场 ROI 再跨场等权；跨场串关不入该口径）与既有等注口径 `realized_roi` 并列；API view/web 账户页（列+横幅分母改独立暴露）+ 契约 export/codegen 同车。数值例断言：场1 两注 +1/−1、场2 一注 +1 → 等注 0.3333 vs 按场 0.5。

**历史利润重算差异**（只读派生面重算，历史行零改动）：seeded 合成账（¥6 与 ¥15000 同期同游戏）税 3000→**3001.2**、税后 11992→**11990.8**，测试与 web mock 同步如实更新并注明旧值。生产真库差异幅度待伞票 10 定点重算出具新旧差对照（本票不执行重算）。

读面字段名零改动（晨检 `auto_v1_pulse`/`check_paper_account` 与前端引用字段全保留），仅新增 `independent_exposures_n` / `realized_roi_per_match` 两个可选字段。定向验证：后端 6 个测试文件 63 passed（account/probes/tax/morning_check/data_fixtures/contract）+ ruff/importlinter/basedpyright 全绿 + web lint/type/307 单测全绿。
