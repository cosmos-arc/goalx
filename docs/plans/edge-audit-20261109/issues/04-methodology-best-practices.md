# 04 国际方法学最佳实践对照

Type: research
Status: resolved

## Question

本项目「训练模型与验证」的方法栈，对照 2024–2026 国际最新最佳实践（学术 + 量化体育投注行业），差距在哪？哪些我们已在做（带证据）、哪些缺但采纳便宜、哪些缺且重、哪些不适用（因 JC 可购面约束）？

我方现行栈（对照起点，详见账本 §3 二十条）：预注册 + 三段切分（搜索/验证/确认各碰一次）+ BH/Holm 多重校正 + append-only 试验账 + PIT/as-of 纪律 + 安慰剂/同接受率对照 + flat-k 反事实基线 + CLV 外锚主判 + σ 闭式功效推导 + 六步门 + 收缩五级 + append-only 版本纪律。

重点核对方向（不限于）：backtest overfitting（PBO/CSCV、deflated Sharpe）、purged/embargoed CV、walk-forward、多重检验（White RC / Hansen SPA / Romano-Wolf——2026-10-22 已裁进 A 线预注册，核对实现面）、体育预测基准（RPS 及其批评、Ley/Van Eetvelde/Groll 系）、校准（Venn-ABERS vs 我方 PAVA）、conformal、CLV 作为评估指标的文献、LLM 时代预测研究。

产出：`research/04-methodology-best-practices.md`——四栏对照表（已覆盖/缺而便宜/缺而重/不适用）+ 每条带来源引用 + 采纳建议（进票 07/08）。

## Answer

对照 2024–2026 国际最佳实践（PBO/CSCV、DSR、purged/embargoed CV、CPCV、White RC/Hansen SPA/Romano-Wolf、RPS 之争、Venn-ABERS、conformal、CLV 文献、LLM 基准、时间衰减），结论：**方法栈无 P0 级结构性缺口**。预注册三窗、BH/Holm、对照设计、PIT 纪律、CLV 外锚主判五柱均在最佳实践前沿侧（CLV 功效门甚至领先文献通行做法，后者多止步于事后描述）。差距集中在实现债与已自认的验证缺口：缺而便宜=S1 挂 Romano-Wolf stepdown、DSR 式 max-z 折减落地（2026-10-22 已裁 A 线）、切分边界 embargo、log loss 第三指标、Venn-ABERS 校准区间补强 E-28②、时间衰减敏感性；缺而重=σ_CLV 实测、Hansen SPA 管线、组合管理层。不适用（JC 约束）=CPCV 多路径选型（Li 2026 不可能三角 + Arian 2024 反向支持我方 walk-forward 选择）、海外 sharp 执行面、parimutuel 学术策略移植、全速 Kelly、LLM 执行面。采纳建议：便宜项全部进 E-28/E-32 措辞级修订，重项随 β 探针/首批幸存者进未来协议，供票 08 汇总人裁。每条均带文献来源与可访问性注记（个别无法核实的来源已如实标注，如「Standardizing Venn-ABERS」疑不存在、两处 LLM 对比无稳定出处）。

Research 笔记：`.scratch/edge-audit-20261109/research/04-methodology-best-practices.md`
