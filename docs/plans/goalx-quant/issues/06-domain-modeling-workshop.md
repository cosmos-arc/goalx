# 06 领域模型工作坊：核心实体与边界场景

Type: grilling
Status: resolved
Blocked by: 01

## Question

与用户一起（HITL grilling + domain-modeling）建立 goalx 量化投注系统的领域模型：核心实体、术语表（CONTEXT.md 初稿）与关键边界场景的裁决，必要时沉淀 ADR。

要覆盖的候选实体与概念（以 01 的机制调研为准修正）：

- 赛事实体：League/Fixture/Team 与竞彩「场次编号」（周几第几场）的映射；让球盘口（HAD 的 handicap line）作为什么实体。
- 市场实体：Market（胜平负/总进球/比分/半全场）、Selection、Odds（时点快照 vs 收盘）、Overround。
- 奖池实体：Pool（任9/14 期号）、公众投注分布（估计值 vs 事后公布值）、PrizeTier、滚存。
- 投注实体：BetSlip/Ticket（含串关腿）、Stake、PlacedBet（人工下单后回录）、Settlement（官方开奖兑付）、PaperBet（纸面跟踪）——回录 vs 纸面的边界术语。
- 预测实体：Prediction（概率向量，来源 ML/LLM 双线）、Edge/EV、CLV 记录、Divergence（双线分歧）。
- 资金实体：Bankroll、Kelly 建议注额、BankrollEvent（入金/盈亏/调整）。
- 边界场景裁决：比赛延期/取消/腰斩/弃权的回录与结算规则；竞彩调盘后已回录的赔率快照如何处理；任9 复式票的表达（多注一张票）；跨期滚存对 EV 计算的影响。

产出：CONTEXT.md 术语表初稿（写入仓库根目录）+ 必要的 ADR（如「预测与投注分离」「回录为事实源」）。

## Answer（2026-09-13 工作坊闭环，用户三项拍板均按推荐）

产物：[CONTEXT.md](../../../../CONTEXT.md)（六域 30 个术语：赛程/市场/奖池/预测/投注/事实与结算）+ [ADR 0001](../../../../docs/adr/0001-draw-result-single-source-of-truth.md)（开奖唯一事实源；Forecast/OddsSnapshot append-only + 哈希存证）+ [ADR 0002](../../../../docs/adr/0002-unified-bet-entity.md)（paper/live 统一 Bet 实体）。

用户拍板：

1. **Bet 统一实体**（mode: paper|live）——纸面/真金同一套 Settlement 与复盘统计，Bankroll 只受 live 影响。
2. **Forecast 持久化 + 内容哈希存证**——纸面验证/防事后修改/模型版本对比的前提（theopenmodel 借鉴落地）。
3. **回录粒度 = 票级回录 + 注级建议**——系统生成注级建议清单（含未购注），回录时勾选实际购买子集，保留机会成本样本。

边界场景裁决（按推荐采纳）：延期/取消/腰斩 → DrawResult.voided + 按官方规则 Settlement（单关退款/串关腿按 1/不足 2 关退款，不自创规则）；竞彩调盘 → OddsSnapshot 只增不改；任9 复式 → BetSlip 1—N Combination（注数=乘积×2 元）；滚存 → PoolState.rolloverIn 作为滚存 EV 计算器输入；让球盘 → Market 的参数（hhad 的 line）而非独立实体；Market 以官方 poolCode 为标识（与数据源对齐）。

对下游的影响：09（双线架构）与 11（spec）以 CONTEXT.md 术语为准；12（回测协议）依赖 ADR 0001 的 append-only/存证设计；PublicShare 的估计值/公布值双源设计承接 13 号的澳客数据结论。

## 计划修订（2026-09-13）

无效腿按1计奖，剩一有效腿不自动退款；更正追溯与paper/live样本语义见当前spec和[结算修复](33-trusted-settlement-lifecycle.md)。
