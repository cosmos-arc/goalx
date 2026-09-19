# 04 融合与验证边界的隔离

Type: grilling
Status: resolved (2026-09-19)
Blocked by: 03

## Question

完整双线融合与 spec v1.1 冻结的验证边界如何共存？用户已裁决建完整融合线，同时 spec 要求"LLM 先证明新增价值、自动概率调整另行验证"。本票定隔离方案：

1. **工件分离**：LlmForecast / FusedForecast 与 ML Forecast 的表与 API 边界；票 34 冻结的前瞻评分集合（Forecast×基准）如何避免被融合工件污染。
2. **评分口径**：纸面期间融合线并行评分（同分母、同去重、同赛前证据边界），报告分列——ML-only / LLM-only / Fused 三列。
3. **真钱资格**：纸面转真金的三条件（CLV/skill/复核）在融合证明前是否只认 ML 线口径，写明判定时点。
4. **CLV 归属**：融合建议的注单 CLV 记账走哪条线。

## 不变量与人裁决项

- 真钱资格判定口径的变更时点由用户裁决（默认：票 05 证明完成前不接入）。

## Answer（2026-09-19 HITL grilling，三项全按推荐）

实测基础：`forecasts` 表自带 `track CHECK('ml','llm','fused')`（M2 起），`forward_validation.build_forward_samples(track="ml")` 与 forecast 取数助手（latest/as_of）全链路 track 参数化默认 ml——**隔离边界已内建，非新建**。

1. **工件分离（改判票 03）**：废弃 llm_forecasts/fused_forecasts 两张新表，LLM/融合预测直接落既有 `forecasts` 表（track 列 + content_hash 存证 + as-of 取数现成）；情报引用放 payload JSON（intel_observation ids）。新增表仅剩 `intel_observations` 一张。冻结集合零污染：所有读取路径按 track 参数化，默认 'ml' 不变。
2. **评分口径**：前瞻评分集合按轨道各跑一遍（同分母/去重/赛前证据边界复用同一函数）；验证报告分列 ML-only / LLM-only / Fused 三列——track 参数化后实现成本≈0。
3. **真钱资格（冻结为不变量）**：纸面转真金三条件（CLV≥200 注 beat≥60%、skill≥0、复核无系统性错误）统计口径冻结为 **ML 轨道**；LLM/Fused 列永远只作参考列。切换时点 = 票 05 证明结论上用户显式裁决；不得事后择优或追溯放宽。
4. **CLV 归属（随之消解）**：融合线不建注——M3 建注决策（EV 扫描/串关/彩池冷门）只读 ML 轨道；CLV 记账维持现状全部 ML，无归属歧义。票 05 证明接入时再议 bets 来源字段。
