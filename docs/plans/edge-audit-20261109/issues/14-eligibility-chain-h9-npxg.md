# 14: 玩法资格链 + H9 npxG 验证

Parent: 票 10（伞）。同分支同 PR。

**What to build:** auto-v2 预筛按玩法（had/hhad/ttg/crs/hafu）分别验在售状态与单关资格，下游建注资格链补玩法级验证，两者单源；H9 报告的 npxG 特征读数路径查证入账。

**Blocked by:** None (can start immediately)

**Status:** resolved

- [x] 外部审计反例转单测：had 在售但 ttg 停售且不允许单关 → 拒绝
- [x] 预筛与下游建注的玩法资格判定单源（一处定义两处引用）
- [x] H9 npxG 查证结论落 `research/`：属实 → H9 排除范围收窄注记稿供票 08；不属实 → 记录证据链
- [x] 资格未齐的历史记录不追溯改判（仅前向生效）注记

## 不变量与人裁决项

- 玩法资格语义以官方规则为准（单关开放范围、在售状态），近似口径 docstring 同步更新。
- H9 判决词本身不动——查证结果只产生范围注记建议，人裁后回写。

## Answer

**玩法资格链（分支 edge-audit-20261109-14-eligibility-chain）**：

1. **单源**：`bets.market_eligibility_problem(conn, fixture_id, market_code, as_of, *, requires_single)` 一处定义；两处引用=①`auto_strategy.anchor_candidates` 预筛（逐 pick 按玩法判，排除账 `market_{reason}` 按场×玩法去重，原 `_roster_exclusion_reason` 的 had 硬编码退役，只留时间面 not_playable）；②`bets.validate_had_selections` 非 had 腿（单腿注要求单关资格；had 腿深链不动）。`auto_strategy._place` 改为全腿无条件过闸（原「仅 had/hhad 在场才验」跳过纯 ttg/crs 注）。词表与 had 深链一致（sale_status_unknown/sale_stopped/sale_not_on_sale/sale_window_closed/sale_status_unknown_value/single_ineligible/single_unknown），未知即拒（票 35 验收 4 纪律）。
2. **反例单测**：`tests/unit/test_market_eligibility.py`（谓词层+下游建注闸层）与 `test_auto_strategy.py::test_prescreen_rejects_stopped_or_non_single_market`（预筛函数层+整锚）——had 在售（比赛+had 行）但 ttg 市场行 stopped+非单固 → 预筛排除（market_sale_stopped）、建注闸抛 BetEligibilityError、整锚零注。
3. **前向数据面**：ingest `store_matches` 补落 hhad/hafu 市场行（`ParsedMatch.pool_single_eligible`，链=市场块 single 防御→poolList 池级→比赛级否决，与进球类同构）；PARSE_VERSION → `sporttery_calculator_v5`；`POOL_CODES` 改引 `markets.JC_POOL_MARKETS`（五池码单源，票 14）。历史无行时代单关资格按未知拒（fail-closed），不回扫补判——注记落在谓词/ingest/docstring 四处。
4. **近似口径 docstring**（谓词内逐条）：市场行优先于比赛行的共享读取舍（比赛级停售探测行晚于玩法行时不被读为停售）、单池下架在采集面表现为市场块消失（不可分辨→沿用玩法行最后态）、开球/新鲜度不在本谓词（had 深链司职）。
5. **行为变化注记**：API/建注面 hhad/hafu 单关在无市场行数据时由「静默通过」变「400 拒」（`test_bet_snapshot`/`test_paper_user_loop` 相应改写；hhad 腿经串关面不受影响）。

**H9 npxG 查证：属实**。证据链（`research/14-h9-npxg-verification.md`）：H9 脚本读 `match_features.xg_home/xg_away` ← gold `_fetch_understat` 读 `understat_matches.xg_home/xg_away`（dates 口径**含点球**）——同表 `npxg_home/npxg_away`（teams.history 去点球）未被读取；实测量化：H9 窗内 12,459 场 understat 完场，27.4% xg≠npxg，场均总量差 +0.238（max +3.197），npxg 列零缺失（改读对列零覆盖损失）；另 2,185/12,169 场为源T 口径 xG（口径另行裁）。H9 判决词不动；范围收窄注记稿已备（refuted 维持，但排除范围收窄为「penalty-inclusive xG 滚动速率特征」，真 npxG 未测）供票 08 人裁回写。H9 侧零代码改动（判决词/报告未触碰）。

测试：`pytest tests/unit`（1620 passed，1 failed=test_bucket_face 需主检出 data/goalx.db，stash 验证与本分支无关的既有环境失败）+ contract 1 passed；`task lint`/`task fmt-check`/`task type` 全绿。
