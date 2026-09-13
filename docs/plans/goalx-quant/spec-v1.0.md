# Goalx 足彩量化投注与预测系统 — 设计 Spec v1.0

> 2026-09-13 由 wayfinder 地图（.scratch/goalx-quant/，18 张决策票闭环）汇编。术语以仓库根 [CONTEXT.md](../../CONTEXT.md) 为准，架构决策见 [docs/adr/](../../docs/adr/)（0001-0007）。

## 1. 目标与定位

- **北极星**：长期盈利导向——+EV、CLV 跟踪、Kelly 注额优化；纪律系统（记录/复盘/熔断）为地基。
- **诚实预期**：组合 yield 低个位数百分比为最好情形；大部分轮次空仓；「不投 −EV 注」是系统最大价值（普通玩家期望 −27%~−35%）。
- **红线**：纯分析 + 人工下单（回录结果）；不做任何自动下单（大陆互联网售彩禁令）；个人单用户；本地 Mac 运行。
- **玩法**：竞彩（胜平负/让球 2串1、总进球、比分、半全场）+ 传统足彩（任9、14场、4场进球）。

## 2. 主攻方向（票 14）

| 方向 | 机制 | 预期 |
|---|---|---|
| **A：陈盘捕捉** | 竞彩官方调盘滞后 vs sharp 实时线（Pinnacle Shin 去晦），EV≥阈值即旗标 | 触发稀少（每轮 1-5 场量级），单注 +1~5%；08 实测竞彩常态 EV −5~−17% |
| **B：滚存轮+反共识** | Ziemba 滚存 EV 分解（P(独中)×(滚存+池)−成本）+ 澳客人气分布反共识覆盖 | 常态轮 −35% 不参与；滚存轮 EV 可转正（+5~20%/轮，重尾） |
| 辅助 | ML/LLM 双线为概率基座与验证基准，非独立盈利柱 | 对市场 skill ≥ 0 |

## 3. 数据层（票 01/02/13/15，07 已开通）

- **国内**：sporttery 网关（竞彩赛程+全玩法赔率+调盘时点，须 Referer 头）、500.com（历史开奖/欧赔/奖池）、澳客 okooo（专家共识/人气分布=PublicShare 估计值）。
- **国外**：API-Football（赛事/阵容/伤停/场馆，免费档起步）、The Odds API（h2h/totals 快照，免费 500 credits 起步）、football-data.co.uk（历史 Pinnacle 收盘 CSV=回测输入）、open-meteo（天气）。
- **舆情**：Reddit RSS（r/SoccerBetting 等）+ 直播吧/虎扑/懂球帝页面。
- **开源底座**：soccerdata（多源抓取+限速）、penaltyblog（建模）、Understat xG（v2）、FBref（v2，可用则用——Opta 下架风险）。
- **赛事分层（票 17）**：Tier 1 = 五大+欧冠+欧联（大赛临时升入；友谊赛排除）；Tier 2 = 其余开售赛事被动覆盖（ML+EV+scout，analyst 仅 Tier 1）。
- **Join 方案（票 08）**：联赛映射 + 开球时间窗消歧 90%；生产版球队级 ID 映射（API-Football 为 canonical）。
- **坑位备忘**：时区统一 UTC 存储；Odds API sport key 动态发现；businessDate≠matchDate。

## 4. 领域模型（票 06）

CONTEXT.md 六域 30 术语：赛程（Competition/Fixture/MatchCode）、市场（Market=官方 poolCode/OddsSnapshot/ClosingLine）、奖池（PoolPeriod/PoolState/PublicShare 双源）、预测（Forecast/MatchIntel/Divergence/EVAssessment）、投注（Bet/BetSlip/Combination/CostLedger）、事实（DrawResult/Settlement/Bankroll）。
ADR 0001：DrawResult 唯一事实源；Forecast/OddsSnapshot append-only + 哈希存证（防前视/防事后修改）。
ADR 0002：paper/live 统一 Bet 实体，同一套 Settlement 与复盘统计。
边界裁决：无效场次按官方规则（单关退款/串关腿按 1）；调盘快照只增不改；任9 复式=BetSlip 1—N Combination；滚存=PoolState.rolloverIn。

## 5. 双线预测架构（票 03/05/09）

**ML 量化线**：penaltyblog 时间衰减 Dixon-Coles 按联赛分池（Tier 1 主动、Tier 2 顺手），Prefect 周任务拟合；10×10 比分矩阵为 canonical（ADR 0006），全玩法为推导视图（半全场 λ 半场拆分 ≈0.45λ）；bootstrap CI；Forecast 落库存证。v2：xG（Understat）+ GBM stacking + Mar-Co。

**LLM 分析师线**（openai-agents，ADR 0004；provider=DeepSeek/GLM OpenAI 兼容端点）：
```
matchday → scout×N（小模型逐场：伤停/轮换/动机/舆情 → MatchIntel）
        → gate（纯代码：JS(融合后, 市场) 阈值 0.02/0.06 + 杯赛阶段放宽（票 17））
        → analyst（大模型仅 Tier 1 分歧场次 → LEAP 结构化似然项，不改 ML 的 λ）
融合：P ∝ P_ML × ∏ L_i^η（log-pool）
```
- 复核结论只进评测集/审计，不进训练特征（防泄漏）。
- 评测：RPS/Brier/ECE 对三基准（去水收盘/恒基线/纯 ML）；泄漏三层控制（前瞻金标准/训练截止/证据截断快照）。

**CLV 跟踪**：kickoff −30/−10/−1min 尽力快照 + 事后 PSC 收盘对账；代理线 = Pinnacle close Shin。

## 6. 价值与成本预估（票 14）

- 每候选注：模型概率+CI、市场隐含（Shin）、EV%、成本调整 EV、Kelly 注额、双线一致性分层；cost-adjusted EV<0 不进推荐。
- 奖池轮：滚存 EV 计算器（P(独中)×(滚存+池)−成本，敏感度）+ 澳客人气反共识提示。
- 成本：CostLedger 记账固定+边际成本；盈亏平衡换手率计算器常驻（月成本 ~¥290 → 2% yield 需月换手 ~¥14.5k）。

## 7. 资金与纪律（票 10，设置页可调）

- Bankroll ¥5,000（专门资金池）；1/4 Kelly + 1% 单注硬上限 + open-bets 扣减；串关按一笔注联合 EV；EV 阈值 τ=1.5%；3串+ 不做；flat 1% 对照口径。
- 分配：竞彩 80% / 任9-14 基础 20% + 滚存轮事件加码（单轮≤当期竞彩月额 50%）。
- 熔断：日损 3% 冷却 24h；DD≥15% 减半；DD≥20% 停 2 周；不设止盈。
- 纸面转真金：整赛季 + 三条件（CLV≥200 注 beat≥60%；skill≥0；复核无系统性错误）。

## 8. 验证协议（票 12，ADR 0007）

- 回测：五大 2023-26 三季 walk-forward（~5,700 场，边际扩六季）；fair=Pinnacle 收盘 Shin；模拟竞彩价= fair×haircut（−10%±5~7%，纸面期校准）；防前视铁律；LLM 线只前瞻。
- 纸面：滚动 100 注 + 累计双窗口；无效场次按官方规则；与回测/实盘共用 Settlement 代码。
- 滚存信号：历史奖池数据（500.com）为实现期任务，EV 只做纸面前瞻。

## 9. 技术栈与部署（票 09，ADR 0003-0005）

FastAPI（apps/backend）+ React（apps/web，沿用 goalx monorepo 与 CI 门）；SQLite WAL（单机）；Prefect（flows=快照/收盘/scout 批量/周训练/复盘统计，本地 server）；openai-agents（LLM 编排）；uv/bun 工作流与 task 命令沿用仓库规范。

## 10. Web 信息架构（六页）

1. **今日**：竞彩场次对照表（竞彩 vs 欧洲共识、EV、CI、双线分歧标记）+ 陈盘告警——主作战页。
2. **复核队列**：分歧场次（gate 路由）、MatchIntel、analyst 输出、人工裁决记录。
3. **投注**：注级建议清单（含未购）+ 票级回录、纸面/真金、结算状态。
4. **资金**：bankroll 曲线、Kelly 建议、成本台账、盈亏平衡计算器、纪律横幅（冷却/熔断状态）。
5. **验证**：CLV 走势与 beat rate、RPS skill、滚动 yield、纸面三条件进度条。
6. **设置**：票 10 全部参数、tier 配置、数据源健康状态。

## 11. 分阶段路线图

- **M1 数据地基**（~2-3 周）：schema + 采集 flows + 历史导入 + Web 骨架 + 回录/结算——先当「记录复盘工具」用起来。
- **M2 ML 线 + 回测**（~2-3 周）：Dixon-Coles 分池训练、回测引擎（walk-forward + ADR 0007）、验证看板——纸面预测开始累积。
- **M3 LLM 线 + 双线融合**（~2-4 周）：scout/gate/analyst、LEAP 融合、复核队列——纸面完整运行。
- **M4 价值/成本预估 + 打磨**（~1-2 周）：EV 扫描完善、滚存计算器、告警、设置页——进入整赛季纸面期。
- **真金期**：纸面三条件达成后触发，非里程碑（规则驱动）。

## 12. 非目标

自动下单/代购；多用户/SaaS；非足球彩种；跨机构套利；社交跟单/SGP 构造器/策略市场（票 16 借鉴清单的「不做」）。
