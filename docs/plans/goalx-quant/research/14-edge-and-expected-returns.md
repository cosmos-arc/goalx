# 研究 14：可实现 Edge 与预期收益（学术依据 + 主攻方向 + 价值/成本预估功能）

> 历史研究记录：2026-09-13审视已纠正部分结算规则、证据等级与收益推断。本文保留原文供追溯，不作为当前实施默认；以[Spec v1.1](../spec.md)及[审视报告](../../goalx-review-20260913/report.md)为准。

> 调研日期 2026-09-12。来源见文末；收益率数字除注明外为文献报告值或按机制的推导值，均需在票 12 的回测/纸面协议中实证校准。

## 一、学术证据地图

### 1. 市场效率与 CLV（我们能赢多少的基准尺）

- **收盘线是最优预测**：Lahtinen (2019, Aalto) 实证收盘赔率优于任何开盘时点与其他预测源；football-data 圈共识（Buchdahl）同。
- **CLV 是技能的最强可测代理**：Pinnacle 交易团队研究「A question of luck or skill」——持续以超过庄家边际的幅度击败收盘线者长期盈利；经验基准：**持续 +1~3% CLV 已是强表现，+0.5% 在大样本下即有意义**；长期盈利者占比仅 ~3-5%。
- **含义**：在全球最有效的市场（Pinnacle 级），顶级玩家的真实 edge 也就 1-3%。任何宣称「稳定 10%+ yield」的说法都应默认不可信。

### 2. 模型路线的真实战绩

- **Dixon & Coles (1997)**：时间衰减极大似然，90 年代对英系庄家开盘价取得正回报（那时市场远未有效）。
- **Constantinou 系（pi-ratings → pi-football → Dolores 2018）**：Dolores 用 20 万+ 场次跨国训练，「实证证明模型可对庄家取得正回报」（对多庄家平均价）；其 2013 论文《Profiting from an inefficient association football gambling market》报告了对特定注型（odds-on favourites 等）的盈利策略；2022 亚盘市场效率研究进一步限定条件。
- **但**：这些证据全部针对**软庄家开/平均价**；对 sharp 收盘线，现代 ML 的 skill ≈ 0（票 03/05 结论一致）。**模型赢软价 ≠ 赢收盘**。
- **Mar-Co (Petretta 2021, arXiv:2103.07272)**：在多个下注场景胜过 Dixon-Coles——v2 模型的候选升级。

### 3. 奖池型（parimutuel）理论

- **FLB（favourite-longshot bias）**：Griffith (1949) 以来最稳健的实证规律——冷门被系统性高估（押冷门长期 −40% 量级 vs 押热门 −5% 量级）；奖池市场更甚（Ottaviani & Sørensen 2010 的信息噪声理论；Economics Bulletin 2021：奖池间 FLB 不被套利、组合型注更强）。
- **滚存 EV 分解（Ziemba 2020）**：奖池 EV = P(独中) × (滚存资金 + 本期池) − 成本。**滚存轮 = 过去轮次的资金补贴本期边际投注者**——这是奖池玩法唯一的结构性 +EV 窗口，且可事前计算。
- **奖池最优策略（Clair & Letscher 2007）**：期望对数效用下，分散/覆盖组合 + 避开热门组合的份额优化框架。
- **国内适用**：任9 单奖级独享+滚存、14 场 70/30+滚存——结构与英国 football pools / pick-6 同构；takeout 35% 高于多数西方奖池（典型 15-25%），常态轮更没得玩，**滚存轮集中发力**的结论反而更强。

### 4. 垄断固定赔率市场的错价（竞彩的关键类比）

- **OPAP 研究（Greek 垄断固定赔率，4 年开收盘样本）**：垄断庄家调价慢，开→收盘漂移显著可预测（聪明钱进入）。竞彩同为**官方垄断、调盘不连续**的固定赔率——机制同构。
- 推论（需票 12 回测校准）：**竞彩锁定赔率 vs 实时 sharp 隐含概率的偏离，存在可捕捉的「陈盘」窗口**——尤其重大新闻（伤停/轮换/天气）后竞彩调盘前的时段。

## 二、主攻方向建议（按学术支撑 × 国内适用性 × 成本排序）

### 方向 A：竞彩陈盘捕捉（stale-line capture）——固定赔率线主引擎

- **机制**：实时拉 Pinnacle/Betfair（The Odds API）→ Shin 去晦得「真概率」→ 对照竞彩当前赔率 → EV 超阈值（≥1-2%）即旗标，等待/捕捉官方调盘前的滞后价。
- **学术支撑**：OPAP 类比 + CLV 文献（本质是人为制造正 CLV）。⚠️ 触发频率与幅度需回测量化——这是票 12 的核心校准项。
- **预期收益**：触发时单注 +1~5%（对竞彩 takeout 后仍可为正因为错价>27% 的情形只在极端滞后时出现，故触发稀少）；**每轮 1-5 场量级**。诚实预期：这条线月均触发注数可能只有 10-50 注。
- **成本**：数据 $19-49/月（已定）+ 实时快照频率要求（可能需要 The Odds API $30 档）。

### 方向 B：任9/14 滚存轮 + 反共识覆盖——奖池线主引擎

- **机制**：跟踪滚存状态 → 滚存/预期池比例超阈值时入场 → 覆盖组合生成（Dixon-Coles 比分矩阵给场次概率，Clair-Letscher 框架做份额优化）→ 反共识：避开澳客人气集中的组合（FLB 的奖池版：热门组合被公众超配，独中概率不变但分成人数暴涨，EV 被稀释）。
- **学术支撑**：三者中最硬（Ziemba EV 分解是可计算的事前 EV）。
- **预期收益**：常态轮 EV≈−35%（不参与）；**滚存轮 EV 可转正，+5~20%/轮量级**（重尾：头奖独中才兑现，需 Kelly 小注 + 多轮分散）。年滚存轮次数（14场大滚存+任9 中滚存）需从 500.com 历史统计（票 08 原型顺手做）。
- **风险**：方差极大，兑现依赖独中或少数人中大档——纸面期必须跑满一个赛季。

### 辅助线：模型基座与验证（不作为独立盈利柱）

- ML 线（Dixon-Coles→xG/GBM）出全玩法概率 + 对市场 skill ≥ 0 验证；LLM 线情报提炼提升 A/B 的输入质量（伤停早知道=更早发现陈盘）。
- 2串1 组合：在 A 触发注中选两条独立 +EV 腿（票 04 公式），不是独立方向。

### 组合预期（诚实版）

- **最好情形**：组合 yield 低个位数（1-3%）；大部分时间空仓或小注。「不投 −EV 注」是系统最大的隐性价值（普通玩家期望 −27%~-35%）。
- 纸面验证期（≥1 赛季）不设盈利预期，只验证：方向 A 的触发频率/幅度、方向 B 的滚存轮 EV 计算是否兑现、双线 skill ≥ 0。

## 三、系统功能需求：价值预估 + 成本预估（进 09/11 的 spec）

### 价值预估（每候选注/每轮）

1. **概率侧**：模型概率向量 + bootstrap 置信区间（样本量/联赛级别标注）；市场隐含概率（Shin 去晦，多源对照：竞彩 vs Pinnacle vs Betfair）。
2. **EV 侧**：原始 EV% = p×odds−1；**cost-adjusted EV** = EV − 月固定成本分摊（见下）；双线一致性分层（ML/LLM 分歧度着色）。
3. **注额侧**：分数 Kelly 建议（成本调整后 edge 代入）+ 硬上限；串关用联合 EV 公式（票 04）。
4. **奖池侧（B 线专属）**：滚存 EV 计算器——输入滚存额/预期池/自估独中概率/自估同中人数分布 → 输出 EV 与敏感度；澳客人气分布的反共识提示（「该组合公众份额 x%，EV 稀释 y%」）。

### 成本预估（月度/每轮）

1. **固定成本记账**：数据订阅（$19-49）+ LLM（$1-10/轮→月度汇总）+ 基建（本地 $0）——系统自动从用量累计。
2. **边际成本**：每轮新增（API credits/LLM tokens）。
3. **盈亏平衡计算器**：required_turnover = monthly_cost / expected_yield；例：$40/月 ÷ 2% → 需月换手 $2,000（≈¥1.4 万）才覆盖成本——用于设定最低活动规模与「值不值得升 $30 档」决策。
4. **下单闸门**：cost-adjusted EV < 0 的注默认不进推荐列表（可看不可下）。

## 来源

- Pinnacle: *Becoming a profitable bettor: a question of luck or skill*（pinnacle.com/en/betting-resources）
- Lahtinen 2019, *When do betting odds best represent the actual outcomes?*（Aalto 学位论文）
- Dixon & Coles 1997; Petretta 2021 *On the dependence in football match outcomes*（arXiv:2103.07272）
- Constantinou 2013 *Profiting from an inefficient association football gambling market*; Constantinou & Fenton pi-football/pi-ratings; Constantinou 2018 *Dolores*; Constantinou 2022 亚盘效率（constantinou.info / ACM）
- Ottaviani & Sørensen 2010 *Noise, Information, and the Favorite-Longshot Bias*（JSTOR）
- Ziemba 2020 *Parimutuel Betting Markets: Racetracks and Lotteries Revisited*（LSE Systemic Risk Centre DP-103）
- Clair & Letscher 2007 *Optimal Strategies for Sports Betting Pools*
- OPAP 固定赔率市场研究（ResearchGate: *Information and Efficiency: An Empirical Study of a Fixed Odds Betting Market*）
- Football-Data.co.uk / Buchdahl CLV 方法论
