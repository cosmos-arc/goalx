# Wayfinder Map: 足彩量化与 Agent 系统（goalx-quant）

> 当前入口（2026-09-13计划修订）：[Spec v1.1：可信纸面闭环](spec.md)。本地图以下保留原设计决策历史；原M3/M4顺序和收益假设不再是当前实施承诺。五张纠偏实施票、依赖与验收见当前spec；历史实现票已补当前验收说明。

> 合并后交接：结算修复已合并；剩余按报价证据→验证统计→页面闭环→真实运行串行。跨模型执行与决策记录见[实施安排](../../docs/plans/trusted-paper-handoff.md)，本轮不继续实现功能。

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
- **开工前读仓库根 [CONTEXT.md](../../CONTEXT.md) 术语表与 [docs/adr/](../../docs/adr/)（06 号票产出），实体命名以术语表为准。**
- 交流语言：中文（保留英文术语）。

## Decisions so far

- [33 结算规则与资金生命周期纠偏](issues/33-trusted-settlement-lifecycle.md)：无效腿/未购隔离/回录事务幂等/开奖更正差额冲正/只读旧账核查已实现，本地task check通过；完整纸面用户闭环及持续运行仍待36/37。
- [35 补齐报价观测、匹配与可购买状态](issues/35-trusted-quote-evidence.md)：v5 证据层（quote_observations/sale_statuses/快照时间三分离）+ as-of 判定 `had-quote` API + join 队名交叉核对 + 逐请求 credit 预算 + haircut as-of 配对/方法版本 + prefect.yaml 已实现，task check 通过，[PR #6](https://github.com/cosmos-arc/goalx/pull/6) 已合并（`720694f`）；后继 34 消费其判定。
- [34 修正验证统计与赛前证据边界](issues/34-trusted-validation-boundary.md)：前瞻评分集合(冻结赛前 Forecast×同期基准,skill 不读回测)、CLV 票级口径+决策去重分母+严格赛前 closing、工件身份(数据+配置+seed+版本)、复核/整赛季未评估不通过、PSC/AvgC 分期对照已实现，task check 通过，[PR #7](https://github.com/cosmos-arc/goalx/pull/7) 已合并（`9c21816`）；后继 36 做纸面用户闭环。
- [38 had 单固误记修正](issues/38-had-single-fix.md)：had 链补 poolList 池级 single（与 wb-04 ttg/crs 同构；冲突时 poolList 优先=票面人裁决项）+ 存量修正走票 35 证据重解析（`goalx reprocess-sporttery`，sha256 校验、幂等、修正行沿用原观测时间线）已实现，主库复现 20 行误记（6/24 当日）、副本实证修正与二跑零增量，全门禁+e2e 双跑通过；主库应用待用户在更新后端后执行该命令。
- [39 共识分母护栏](issues/39-consensus-denominator-guard.md)：`odds_math.LOW_CONFIDENCE_BOOK_THRESHOLD=4`（常量参数）+ 今日页 flags/研究页 `ConsensusView.low_confidence`（契约增量 required bool，零破坏）+ 三页面琥珀低置信标注接词条、few_books(<3) 并打时显示位让位（<4 覆盖其区间）；不动共识算法（Shin/均价原样）；阈值 4 与合并显示方案待人追认（Answer 内 pending-user-confirm）；全门禁 + e2e 双跑（死/常驻各 27 绿）+ paper-loop 12 绿通过。
- [40 CLV 基准分层](issues/40-clv-anchor-tiering.md)：closing 三级取锚——pinnacle 主锚（单书 Shin）→ betfair_ex 辅（back 价扣佣 2% 参数化，eu/uk 都认）→ 多书共识 Shin fallback（onexbet 类高 margin 书只进共识永不作锚）；`clv_records.close_basis`（v7）随行标注、历史行 NULL=legacy 不重算，报表 `by_close_basis` 分列新旧并行（窗口期「至下一整轮销售周结束」与佣金率 2% 均待人追认）；skill 三条件口径与回测基线不动；contract 零漂移（clv 为 free-form dict）；全门禁 + e2e 双跑（死/常驻各 27 绿）+ paper-loop 12 绿通过。
- [41 注级 EV/CLV 概率快照](issues/41-bet-level-ev-clv-snapshot.md)：建注落注级双口径快照（**双存待人追认**）——`bets` 四可空列（v8，v7 留给 PR #24 防版本号冲突）：共识 = 锁定时欧共识 Shin（复用 eu_consensus_asof）、模型 = 锁定时已发出的最新 Forecast（`latest_forecast_asof` 防时间泄漏）、EV 按联合概率×联合锁定赔率（串关独立假设与 CLV 票级同口径）、非 had 腿/口径缺失/存量注 NULL 不倒填；BetView 增可空 `ev_snapshot`（零破坏）；历史页平均 EV 点亮（共识口径优先、两口径不混算、样本数注明，"—"兜底保留）+ `bet-ev-snapshot` 词典词条；CLV 侧链路已有（clv_records.bet_id 直连快照列，join 测试覆盖），BetView 注级 CLV 字段与历史页平均 CLV 记缺口留后续票；全门禁 + e2e 双跑（死/常驻各 27 绿）+ paper-loop 12 绿通过。
- [42 赛果自动同步源](issues/42-draw-results-sync.md)：四源实证（代称源A/B/C/D，对照行见票 Answer，域名只在配置）——源A 网关 403+开奖页 JS 壳、源B 缺半场比分、源C 不可达，仅源D 全维度通过（浏览器 UA+一次重定向+GB18030，周XNNN 对齐，全场+半场服务端渲染）→ `data/ingest/caiguo`（解析纯函数+fail-closed：无效场次/对不上/与库内不一致一律待人工不落库，ADR-0001 澄清条款保留）+ 候选业务日零成本跳过（7 天窗口）+ migrations v9 `draw_sync_runs`（append-only 元信息+待人工清单）+ 双 deployment 调度（18:00-05:59 每 30 分钟+08:00 补扫，**频率待人追认**）+ `POST /draw-sync/run`/`GET /draw-sync/status`（纯增量契约，502=源不可达）+ 投注页同步面板点亮（状态/主动触发/待人工清单，人工兜底与更正预览不动）；落库复用 import_draw_results 零改动；全门禁 task check exit 0 + e2e 双跑（死/常驻各 27 绿）+ paper-loop 12 绿通过。

- [43 彩池数据源](issues/43-pool-data-source.md)：四源实证（代称同 42）——期次/对阵/**分布**按源B 落地（期次页服务端渲染 14 场 + 人气接口 percent/number 两口径，键 1/2/3→官方池码 3/1/0 欧赔交叉验证，Referer 访问限制，在售+完场可回溯）；**官方销量四源直接 GET 全不可得 → AI 代采兜底层**（`POST /api/v1/pool-states` 结构化幂等录入 source=agent，用户命令触发不进调度，页面销量区块"代采待命"占位只降级该区块）；彩池口径三修正只落抽水折算（估计派彩赔率 = 返奖率 65%÷份额，price impact/分彩风险记 docstring+词条不建模）；概率映射 = fixture 窗口+队名前缀匹配 → 模型概率优先、期次页欧指去水兜底；/markets/pool 点亮（骨架横幅移除、真金档禁用只呈现 flat 纸面、提交接线 pool-slips 仅 paper）+ 词典 19 号「彩池 EV」；migrations v10（v9 留给 wt-42，合并纯追加）；全门禁 + e2e 双跑（死/常驻各 27 绿）+ paper-loop 12 绿通过；同步频率/代采启用时点待人追认。

<!-- 一行一票：闭环时追加一行（要点 + 链接） -->

- [47 xG blend 接线](issues/47-xg-blend-forecast.md)：2026-09-21 用户裁决 blend 终选（票 45 三待追认同批落定：俄超不纳入/forecast 只进对比基准族；票 46 清单与 AvgC 分期口径追认）；矩阵层算术混合（had 池精确=实证线性池，canonical 表示不破）+ as-of 当日现拟合 xG 侧 + 队名位置索引解析；荷甲/Tier2 纯 goal-DC 兜底（model_version dc-/dc-xgblend- 溯源）；CI=goal bootstrap 池化 xG 点估计（口径注明）；票 34 冻结协议不动；后端 487 绿。

- [46 十年回测语料](issues/46-decade-backtest-corpus.md)：fdhist 扩联赛 E1/P1/T1/B1/SC0（美职/巴甲/墨超/日职 fd 无季目录=结构限制记档）+ 十一年窗 1617..2627 落库 38,460 行零失败；openfootball↔fdhist 交叉验证 8,178 场 99.98% 一致（仅葡超 2526 末轮 2 场分歧）；收盘缺口量化——2425 前 PSC 完整、2526 起 PSC 缺 53%、当季全缺但 AvgC 全量可得（=PSC Shin 主锚+AvgC 分期兜底口径）；corpus-report CLI + completeness_report；清单/当季口径待人追认（PR #48）。

- [45 Understat xG 特征层](issues/45-understat-xg-features.md)：getLeagueData 纯 JSON 直通复核成立（history 时刻键=date 实测坑；forecast 仅已赛场次携带）；migrations v15 understat_matches（prior_*=本季严格早于本场开球累计，同刻互不可见）+ sync_runs；每日 6 请求调度 09:20；join 确定性键当前季 53 场竞彩；xG 版 DC（scipy 双 Poisson，rho 不适用文档化）+ npxG 收缩校准（速率混合 w=n/(n+k) 重归零均值）+ walk-forward 周口径实证对比 runner（2019-2026 语料 14,312 场零缺 npxG）；实证 7,284 场配对：blend 全桶唯一一致改善（-0.0012）、收缩早季反伤（+0.0019）晚季最优（k10 -0.0022）、understat 自家 forecast 大幅领先（-0.0359 但仅已赛场次可得=基准非信号）；终选建议 blend 待人裁决（票 34 冻结边界未动）；全门禁后端 480 绿 + contract 零漂移（fc8b5e2）。

- [44 官方赛果并行对账](issues/44-official-results-reconciliation.md)：uniform 族官方赛果观测+双参照源对账落地（migrations v14：uniform_result_observations/draw_reconciliation_runs/source_coverage 四态）——join=source_match_id 一跳确定性键，matchResultStatus 七周 725 场枚举定型（2 完场/0 取消/1 未完场+Refund=void），并行阶段不改事实源（ADR 0001）；openfootball 11 联赛 raw 直下对账（not_covered 与 fetch_failed 分离）；分支 feat/official-results（30dfdc5→7315619 五 commit 含兼容清理，未 push）；**2026-09-20 晚用户裁决免观察期直接切换已执行**：uniform 升事实源（终态导入+不冲正）、源D 降审计源（audit_draw_results 近7天窗口）、调度/UI 面板无感切换、migrations v15 四态 coverage；切换冒烟源D 审计抓到真实人工录入错误（周四003 1:1 vs 双源 2:0，待人工更正）；coverage 粒度=按源粒度。

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
