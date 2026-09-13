# 16 Reddit 社区 + 业界产品对标调研

Type: research
Status: resolved

## Question

（用户 2026-09-12 追加。）Reddit 相关频道有哪些信息与共识？业界有无类似产品能力可对标借鉴？输出能力矩阵与借鉴/不做清单，供 09/11 的 spec 设计。

## Answer

详见 [research/16-reddit-and-benchmarks.md](../research/16-reddit-and-benchmarks.md)。要点：

1. **Reddit**：核心频道 r/algobetting（方法论）、r/SoccerBetting（情绪源，RSS 已通）。社区共识与我们的研究互证：CLV > 胜率、模型收益「低个位数、没新手幻想的神奇」、推荐数据栈（FBref xG/Understat/Club Elo/soccerdata）与我们高度重合。⚠️ 重要环境变化：**FBref/Opta 2026-01 被迫下架高级数据**——13 的 FBref v2 路线降级为「可用则用」，以 soccerdata 多源冗余 + Understat 为主。
2. **产品对标矩阵**：Trademate（软庄家 EV 扫描，宣称均 ROI 3.5%）、Pikkit（自动 CLV，~$40/月）、Outlier（line movement 告警）、Forebet/KickForm（公开概率+Kelly 标注/物理模型+蒙特卡洛）、澳客专家（国内共识聚合）。
3. **差异化确认**：奖池型玩法（滚存 EV）、成本/价值预估、中文生态——西方产品全部空白，即本项目的定位。
4. **借鉴清单**：Trademate 的 EV 扫描交互范式、Pikkit 的「每注必带收盘价」口径（以收盘价自动抓取+人工回录复刻）、Outlier 的陈盘告警、Forebet 的按联赛准确率透明度；不做：社交跟单/SGP 构造器/策略市场。
5. Football Radar 等机构博客纳入 LLM 情报线检索种子；r/algobetting 精华帖目录沉淀为检索种子。
