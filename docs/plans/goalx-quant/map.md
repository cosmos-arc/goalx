# Wayfinder Map: 足彩量化与 Agent 系统（goalx-quant）

> 当前入口（2026-09-13计划修订）：[Spec v1.1：可信纸面闭环](spec.md)。本地图以下保留原设计决策历史；原M3/M4顺序和收益假设不再是当前实施承诺。五张纠偏实施票、依赖与验收见当前spec；历史实现票已补当前验收说明。

> 合并后交接：结算修复已合并；剩余按报价证据→验证统计→页面闭环→真实运行串行。跨模型执行与决策记录见[实施安排](../trusted-paper-handoff.md)，本轮不继续实现功能。

Label: wayfinder:map

## Destination

一份可以直接开工的完整设计 spec，落在 goalx 仓库：领域模型（CONTEXT.md + ADR）、系统架构（数据管道 + 双线预测 + agent 编排 + Web 仪表盘）、数据源选型结论、分阶段实施路线图、首批可开工 tickets。实施本身不在本地图内。

## Notes

- 领域：中国大陆足球彩票**个人**量化投注系统。全玩法并行：竞彩固定赔率（胜平负/让球 2串1、总进球、比分）+ 传统足彩奖池型（14场胜负、任选9、4场进球）。
- 已定前提（2026-09-12 charting 会话与用户确认）：
  - **首要目标**：长期盈利导向——+EV 北极星、CLV（收盘线价值）跟踪、Kelly 型注额优化、回测/纸面验证；纪律系统（记录/复盘/止损）作地基默认包含。
  - **自动化边界**：纯分析 + 人工下单（下单后回录结果），不做任何自动下单——大陆禁止互联网售彩，合规红线。
  - **运行形态**：本地 Mac，沿用 goalx 现有 FastAPI + React monorepo，后端定时任务 + Web UI；后期可平移上云。
  - **LLM/agent 职责**：双线并行对比——ML 量化线与 LLM 分析师线各自产出预测，系统度量分歧，分歧大的场次人工复核。
  - **验证路径**：纸面跟踪先行（预测 vs 官方开奖 + 收盘 CLV），同时用可获得的欧洲市场历史赔率做局部回测近似。
  - **数据预算**：外部数据源 ≤ $50/月。
- 2026-09-12 用户追加决策：**框架不采用 pydantic-ai**，短名单 = DeepSeek Harness（DSH）与 OpenAI Agents SDK，最终选型在 09 裁决（详见 05 的 Addendum）。
- 2026-09-12 用户追加需求：系统必须内建**价值预估**（EV/CI/成本调整 EV/滚存 EV 计算器）与**成本预估**（固定+边际成本记账、盈亏平衡换手率计算器）功能（详见 14）。
- 开工票前参阅技能：grilling（HITL 票必须）、domain-modeling（涉及术语/实体时）；research 票由 subagent 调用 `mattpocock-skills:research` 技能解决。
- **开工前读仓库根 [CONTEXT.md](../../../CONTEXT.md) 术语表与 [docs/adr/](../../adr/)（06 号票产出），实体命名以术语表为准。**
- 交流语言：中文（保留英文术语）。

## Decisions so far

- [33 结算规则与资金生命周期纠偏](issues/33-trusted-settlement-lifecycle.md)：无效腿/未购隔离/回录事务幂等/开奖更正差额冲正/只读旧账核查已实现，本地task check通过；完整纸面用户闭环及持续运行仍待36/37。
- [35 补齐报价观测、匹配与可购买状态](issues/35-trusted-quote-evidence.md)：v5 证据层（quote_observations/sale_statuses/快照时间三分离）+ as-of 判定 `had-quote` API + join 队名交叉核对 + 逐请求 credit 预算 + haircut as-of 配对/方法版本 + prefect.yaml 已实现，task check 通过，[PR #6](https://github.com/cosmos-arc/goalx/pull/6) 已合并（`720694f`）；后继 34 消费其判定。
- [34 修正验证统计与赛前证据边界](issues/34-trusted-validation-boundary.md)：前瞻评分集合(冻结赛前 Forecast×同期基准,skill 不读回测)、CLV 票级口径+决策去重分母+严格赛前 closing、工件身份(数据+配置+seed+版本)、复核/整赛季未评估不通过、PSC/AvgC 分期对照已实现，task check 通过，[PR #7](https://github.com/cosmos-arc/goalx/pull/7) 已合并（`9c21816`）；后继 36 做纸面用户闭环。

<!-- 一行一票：闭环时追加一行（要点 + 链接） -->

- [06 领域模型工作坊](issues/06-domain-modeling-workshop.md)：六域术语表落 CONTEXT.md（Competition/Market/OddsSnapshot/PublicShare/Forecast/Bet/DrawResult 等 30 项）+ ADR 0001（开奖唯一事实源、快照 append-only+哈希存证）+ ADR 0002（paper/live 统一 Bet）；回录=票级回录+注级建议。
- [07 数据源开通](issues/07-data-source-provisioning.md)：API-Football 免费档（100/天，Pro 暂不升）+ The Odds API 免费档（500 credits，EPL h2h 真实拉取验证通过）凭据落 `.env`（gitignore 确认）；open-meteo 实测通过；无 key 国内源沿用前测结论——08 原型可直接开工。
- [08 数据管道原型](issues/08-data-pipeline-prototype.md)：全链路跑通（43 竞彩→223 欧赔→join 25→760 行历史），**数据足够支撑双线预测与回测**；竞彩 EV 系统性 -5~-17%（27% takeout 互证）；调盘时点字段可用（方向 A 基础成立）；join 需球队级 ID 映射（联赛+时间消歧 90%）；Tier 2 欧赔缺口实证（14/43 场）；Pinnacle 收盘 78%+AvgC 补全 100%。
- [09 双线预测架构设计](issues/09-dual-track-architecture.md)：框架=openai-agents（ADR 0004，DSH 验证出局）、存储=SQLite（ADR 0003）、调度=Prefect（ADR 0005，用户指定）、复核只进评测集；比分矩阵为 canonical 概率表示（ADR 0006）；JS 散度路由阈值 0.02/0.06；CLV=开球前尽力快照+PSC 事后对账；ML=penaltyblog 周训练分池。
- [10 资金管理与纪律规则定稿](issues/10-bankroll-rules.md)：Bankroll ¥5,000（初始 ¥2,000 同日上调）；1/4 Kelly + 1% 硬上限 + open-bets 扣减；80/20 竞彩/奖池分配 + 滚存轮事件加码；日损 3% 冷却、DD15% 减半、DD20% 熔断 2 周、不设止盈；纸面转真金=整赛季+三条件（CLV≥200注 beat≥60%、skill≥0、复核无系统性错误）；月成本占 bankroll ~5.8%，盈亏平衡需月换手 ~¥14.5k（2% yield 假设）。
- [12 回测与纸面验证协议](issues/12-backtest-and-paper-protocol.md)：回测=五大 2023-26 三季 walk-forward（用户拍板，边际则扩六季）；ADR 0007（Pinnacle 收盘 Shin 为公允基准、竞彩价 haircut −10%±5~7% 代理、纸面期校准）；防前视铁律；回测/纸面/实盘同一套 Settlement；滚存信号只做纸面前瞻（历史奖池数据为实现期任务）。
- [11 Spec 走查与路线图定稿](issues/11-spec-review-and-roadmap.md)：spec.md v1.0（12 节）通过用户走查；四阶段路线图与 Web 六页确认；首批 M1 实施票 18-23 拆出（ready-for-agent）。**地图抵达目的地。**

- [01 玩法机制与官方数据可得性调研](issues/01-playtype-mechanisms-and-official-data.md)：竞彩返奖 73%/传统足彩 65%；无效场次、奖池分配、滚存规则核实；sporttery 网关实测可用（须 Referer 头），国内无竞彩赔率时序存档 → CLV 必须用欧洲收盘价代理。
- [02 外部数据源选型（≤$50/月）](issues/02-external-data-sources.md)：推荐 API-Football Pro $19/月 + The Odds API 免费起步（按需升 $30）+ football-data.co.uk 回测 CSV（含 Pinnacle 收盘列）+ open-meteo 天气 + Reddit RSS/中文门户舆情；全免费降级路径成立。
- [03 预测与奖池建模研究](issues/03-prediction-and-pool-modeling.md)：v1 = 时间衰减 Dixon-Coles（penaltyblog）→ 比分矩阵推导全玩法，欧洲收盘价 Shin 去晦后 log-linear 融合；任9/14 常态 +EV 窄，机会在滚存轮与份额优化；评估以 RPS 为主指标。
- [04 投注优化、资金管理与业界系统调研](issues/04-staking-optimization-and-industry-systems.md)：1/4 Kelly + 1% 硬上限 + open-bets 扣减；2串1 采用 EV 阈值 + 第二腿下限公式，3串+ 不做；功能闭环五段：EV 扫描 → Kelly 注额 → 注单记录（含收盘价）→ 分维度 CLV 复盘 → 显著性检验。
- [05 LLM agent 架构与框架调研](issues/05-llm-agent-architecture.md)：LLM 直接出概率无 +EV 证据 → 定位为情报提炼/复核层；scout(小模型情报)/gate(JS 散度路由)/analyst(大模型复核) 拓扑；LEAP log-pool 融合；每轮成本 $1-10。**Addendum：用户否决 pydantic-ai，短名单 DSH / OpenAI Agents SDK，09 裁决。**
- [13 补充数据源调研](issues/13-supplementary-data-sources.md)：澳客 okooo 实测可达（专家共识/人气分布 = 任9/14 公众注分布的直接数据源，v1 采纳）；Understat xG（v2）；FBref 需浏览器会话爬取（v2）；FotMob 需逆向签名（暂缓）；WhoScored/SofaScore 反爬放弃。
- [14 可实现优势与预期收益](issues/14-edge-and-expected-returns.md)：竞彩打平需 +37% 相对 edge → 主攻 A=陈盘捕捉（竞彩调盘滞后 vs sharp 实时线，OPAP 类比）、B=任9/14 滚存轮+反共识覆盖（Ziemba EV 分解，学术支撑最硬）；组合预期 yield 低个位数；价值/成本预估功能需求已定义。
- [15 GitHub 开源生态调研](issues/15-github-opensource.md)：无同类系统（端到端仓库均学生级）；库生态厚——soccerdata（2065★，多源抓取底座，化解 FBref 反爬）、penaltyblog、football_analytics（2776★ 教材）；自研边界=官方数据/奖池 EV/双线/价值成本预估/中文情报，全部空白。
- [16 Reddit 社区 + 业界产品对标](issues/16-reddit-and-benchmarks.md)：r/algobetting 共识与我们的研究互证（CLV>胜率、低个位数预期）；⚠️ FBref/Opta 2026-01 下架部分高级数据，v2 以 soccerdata+Understat 为主；对标 Trademate/Pikkit/Outlier/Forebet 后差异化确认：奖池玩法、成本/价值预估、中文生态西方全空白；借鉴与不做清单已列。
- [17 赛事范围与分层投入定稿](issues/17-competition-scope.md)：Tier 1 = 五大+欧冠+欧联（大赛临时升入，友谊赛排除）；Tier 2 被动覆盖（ML+EV 扫描+scout，analyst 仅 Tier 1）——冷门赛事为陈盘主矿区；杯赛末轮/淘汰赛设为 analyst 优先触发场景。

## M2 拆票（2026-09-13，用户参与裁决）

- 票 24-32 已发布（全部 ready-for-agent）：矩阵推导/球队名对齐/DC 训练器三张并行起步 → Forecast 存证 → 回测引擎 → 指标集 → haircut 校准 → 验证看板 → CLV 对账。
- 用户裁决：DC 走 **penaltyblog**；walk-forward 重估**按比赛周**（最严谨口径）；回测下注范围 **had+hhad+ttg**（crs/hafu 只推导不下注）；粒度按 9 张拆。
- M1 已合入实现分支 `feat/m1-data-foundation`（PR #2），票 18-23 resolved。

## Not yet specified

<!-- 地图已抵达目的地（2026-09-13，票 11 闭环）。原雾区全部移交实施阶段：
     ML/LLM 具体功能设计、提示词与评测集 → M2/M3 拆票；社区舆情信息价值、特征工程 → M2/M3；
     复式组合生成、滚存计算器 → M3/M4；模块分解与 API 契约随各实施票走仓库 contract-first 流程。 -->


## Out of scope

- 自动/半自动下单、代购接口探索（charting 已决策排除：合规风险 + 互联网售彩禁令）。
- 多用户、SaaS 化、面向他人提供服务（个人系统）。
- 足球以外的彩种（篮彩、数字彩）与场外/海外开户投注。
- 跨机构套利（竞彩为单一合法渠道，无套利空间）。
