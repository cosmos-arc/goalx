# 03 预测与奖池建模研究

Type: research
Status: resolved

## Question

针对本系统玩法（胜平负/让球、总进球、比分、半全场、任9/14、4场进球），学术与业界的最佳建模路线是什么？给出 v1/v2 推荐模型栈与可复用的开源实现清单，供双线架构（09）设计。

具体要回答：

1. **进球/比分模型**：Dixon-Coles (1997)、双变量 Poisson（Karlis & Ntzoufras）、加权极大似然的时间衰减、ELO/clubelo 评分、xG 驱动方法、梯度提升（特征：近期状态、主客场、赛程密度、伤停）与深度学习路线的精度对比结论；从比分矩阵推导胜平负/总进球/半全场/让球盘概率的标准做法（含亚盘/让球的 map）。
2. **开源可复用**：penaltyblog（Python，含 Dixon-Coles 拟合与数据抓取）、goalmodel（R）、kickoff.ai、socceraction（VAEP/xG 价值模型）等的成熟度、许可证、与本项目（Python/FastAPI）的集成成本。
3. **市场基准与校准**：用市场赔率去晦（implied probability，去除 overround，Shin 方法 vs 简单归一化）做基准/融合的文献结论；模型概率与市场融合（Brighton & Fenton 的 hybrid 思路等）。
4. **奖池型（任9/14）建模**：parimutuel 下公众投注分布的估计（favorite-longshot bias 文献、football pools 研究）、「覆盖策略」（复式/矩阵投注的 covering designs 与启发式）、在返奖率 ~73% 下任9/14 是否存在可利用的 +EV 空间的已有证据。
5. **CLV 适用性**：收盘线价值在竞彩（官方盘、无 Pinnacle 类 sharp line）语境下如何定义与测量（用欧洲收盘赔率做代理线？）。
6. **评估协议**：RPS（ranked probability score）、Brier score、校准曲线、按市场基准的 skill 对照——纸面验证期应统计哪些指标。

产出：v1（基线可落地）与 v2（进阶）模型路线推荐 + 开源依赖清单 + 每类玩法的概率推导路径图。

## Answer

详细调研（全部含来源链接）：[../research/03-prediction-modeling.md](../research/03-prediction-modeling.md)

**前提修正**：传统足彩（任9/14、4场进球）官方返奖率是 **65%**（非 73%）；73% 是竞彩固定奖玩法。任9 的 takeout 为 35%，直接收紧奖池 +EV 空间。

### 1. 模型路线推荐

- **v1（基线）**：时间衰减 Dixon-Coles（penaltyblog，MIT，月度发版，PyPI 1.12.1）产出 10×10 比分矩阵 → 全部玩法从矩阵推导；半全场用 λ_1H≈0.45λ 比例拆分；欧洲收盘赔率 **Shin 去晦**做基准，与模型做 **log-linear 融合**（这是文献共识的主增益来源：单独模型难胜收盘价，Berrar et al. 2019 / Štrumbelj 2014）。
- **v2（进阶）**：xG 驱动 λ（socceraction 或 Understat/FBref 聚合）+ GBM 判别层（Elo/pi-rating/xG/赛程特征，Hubáček et al. 2019 路线）与生成层 stacking；Dixon & Robinson (1998) 时间非齐次进球率改进半全场。

### 2. 开源依赖清单

- 采纳：**penaltyblog**（Python/MIT，2026-09 仍月度发版）、**football-data.co.uk** 历史收盘赔率、**clubelo.com** API；v2 加 **socceraction**（MIT，KDD 2019）。
- 不采纳：**goalmodel**（R + GPL-3 + 2024-03 停更，仅作算法参考）、**kickoff.ai**（无开源库，仅论文 arXiv:1609.01176）。

### 3. 各玩法概率推导路径

比分矩阵 P(h,a) 聚合：胜平负=Σsign(h−a)；让球=Σsign(h−a+L)（竞彩整数三向，无 push）；总进球=反对角线 h+a 分桶（0..7+）；比分=单格+三向「其他」尾；半全场需上半场分布（v1 比例拆分 / v2 birth process）；任9/14、4场进球=对应单场边际（1X2 三值 / 8 档总进球）独立连乘。

### 4. CLV 定义建议

竞彩官方盘非 sharp line 且 margin 27 点，不可直接套标准 CLV。用**欧洲收盘价（Pinnacle/Betfair，football-data.co.uk）经 Shin 去晦做代理线**：CLV_proxy = p*_close − 1/o_take（概率域），仅对可映射玩法（胜平负/让球/O-U）计算，并用「CLV_proxy 与实际结算收益的横截面回归斜率显著为正」验证代理线在本语境有效。

### 5. 评估协议

主指标 RPS（Constantinou & Fenton 2012），辅 multi-class Brier/log loss、校准曲线+ECE（平局桶单列）；**通过线 = 对 Shin 去晦收盘市场的 skill（1−L_model/L_market）≥ 0**；RPS 差用 Diebold-Mariano/block bootstrap，按联赛赛季分层；纸面验证 ≥ 1 完整赛季（≥1000 场）；下注侧加 flat-stake ROI、t 值、CLV_proxy 分布；奖池侧加份额/概率比分布与滚存轮 EV 监控（滚存是 65% 返奖下最主要的结构性 +EV 机制；常态轮次应诚实预期接近 −35%，靠份额优化+滚存加注拉回）。

### 关键库引用

penaltyblog https://github.com/martineastwood/penaltyblog · socceraction https://github.com/ML-KULeuven/socceraction · goalmodel https://github.com/opisthokonta/goalmodel · 奖池策略 Clair & Letscher 2007 (Operations Research 55(6)) · 覆盖设计 OEIS A004044 / Kéri SZTAKI / La Jolla Covering Repository
