# Wayfinder Map: M3 LLM 双线融合（goalx-m3）

Label: wayfinder:map

## Destination

一份可直接开工的 M3 实施设计：基本面/情报源选型定案、GLM 双线架构（scout/gate/analyst + LEAP 融合）与证据存证方案、融合与现有验证边界的隔离方案、新增价值评测口径、成本预算、双消费场景（彩池页 + 场次详情）产品形态，并拆出首批实施票。实施本身不在本地图内。

## Notes

- 领域：中国大陆足球彩票个人量化系统；本图 = spec v1.1 暂缓的 M3 全面启动。
- **2026-09-19 用户裁决（charting 会话）**：
  - 边界：**完整双线融合**（不走窄版 advisory 先行）。spec v1.1 的证明义务不移除，落为本图 04/05 两票——融合线建起来，但验证隔离、真钱资格口径在证明完成前不因融合改变。
  - LLM 提供商：**GLM（智谱）**，`.env` 现无任何 LLM key，预算估算后用户开通。
  - 基本面源：**先调研对比再定**（票 01）。
  - 消费场景：**彩池页证据卡转正 + 场次详情双场景**（票 06）。
- **2026-09-19 用户追加固件裁决**：
  - **agent 框架 = openai-agents-sdk（Python）**——ADR 0004 既有定案再确认，管线（scout/gate/analyst/LEAP、存证、调度）全走 Python 侧。
  - **前后端交互基础 = Vercel AI SDK（TS）+ AG-UI 协议**——用户先定 ai-sdk（useChat/AI Elements），二次调研（用户推动）后补定 **AG-UI** 为前后端交互协议：后端 `ag-ui-protocol` v1.0.0（开放 1.0 规格，FastAPI 原生，≥3.13 兼容），前端默认 useChat+自定义 transport 包 `@ag-ui/client`（备选 assistant-ui 适配器），最终形态票 06 原型 A/B。不引入 Node BFF（用户明确）。
  - 推论：证据卡本身是**存量工件渲染**（DB 数据，不涉协议层）；AG-UI 只服务**增量交互场景**（如详情页"追问 analyst"）。
- **2026-09-19 用户裁决（票 03 开场项，聊天内闭环）**：**基本面免费起步（选项 B）**——内部推导（fdhist 2627 在库）+ football-data.org 免费档 12 项 + 伤停/阵容情报双路（**目标站点直爬**复用 ingester+observations 存证模式 + GLM web_search 兜底）；API-Football Pro $19/月 留作情报价值证明后的升级项。
- **2026-09-19 裁决追加（伤停路径终局）**：web-search 路线用户否决；直爬四层验证不可行（票 09 Comments 1-4）。伤停情报面降级"有则更好"：现有情报面（fdhist 推导+fdorg）支撑双线开工，重点场次 AI 代采（端点挂票 14），充值前置取消。
- 既有结论直接复用，勿重调研：
  - 票 05（旧图）：LLM 直接出概率无 +EV 证据 → 定位情报提炼/复核层；scout/gate/analyst 拓扑；LEAP log-pool 融合。
  - 票 09（旧图）：框架 openai-agents（ADR 0004）、JS 散度路由阈值 0.02/0.06、复核只进评测集。
  - 票 17（旧图）：Tier 1 = 五大+欧冠欧联；analyst 仅 Tier 1，scout 全覆盖。
  - 票 13（旧图）：okooo 专家共识已集成（源B）；Understat xG 曾列 v2；WhoScored/SofaScore 反爬放弃。
  - `.env` 已有 `API_FOOTBALL_KEY`（免费档 100 次/天）与 `ODDS_API_KEY`。
- 预算约束：数据源总预算 ≤ $50/月（既定），GLM 费用并入此约束核算（票 02）。
- **票 37 真实运行验收并行进行中**（day 1 = 2026-09-19，serve 已修复六 deployments）。M3 开发期间任何新增/变更 deployments 合入 main 后须重启 serve（见 docs/RUNBOOK.md）；不得扰动运行库既有表。
- 开工票前读仓库根 CONTEXT.md 与 docs/adr/（尤其 0004/0006/0007）；实体命名以术语表为准。术语注意：**情报**（intel，scout 采集的原始素材）与**证据**（evidence，带来源/时点的可存证陈述）需在票 03 会话中定案入 CONTEXT.md。
- 交流语言：中文（保留英文术语）。

## Decisions so far

<!-- 一行一票：闭环时追加一行（要点 + 链接） -->

- [03 双线融合架构设计](issues/03-dual-track-architecture.md)：免费起步+存证表设计（**04 改判：预测工件复用既有 forecasts(track)，新表仅 intel_observations**）+新领域包 **llm/**（原 intel/ 改名）+ LLM 只出三项、融合在三项层（实施补 ADR-0009）+**交互面 AG-UI 协议**（后端 ag-ui-protocol v1.0.0 FastAPI 原生，前端 useChat+@ag-ui/client transport，票 06 A/B 定形）+scout 范围 Tier1+当期彩池；术语 IntelObservation/EvidenceSummary 已入 CONTEXT.md。
- [04 融合与验证边界的隔离](issues/04-fusion-validation-isolation.md)：实测发现 forecasts 表已带 track 列且验证/取数全链路参数化——隔离边界内建；改判三新表为复用 forecasts(track)；评分报告分列 ML/LLM/Fused 三列（成本≈0）；**冻结：真钱资格三条件只认 ML 轨道、融合线不建注**，切换时点=票 05 证明上用户显式裁决，不得事后择优。
- [05 增量价值评测口径](issues/05-incremental-value-evaluation.md)：两档证明阈值冻结——Tier A 去留（≥200 配对场+≥6 周+RPS 胜率≥52%+ECE 无劣化，DM 如实报告）/Tier B 真钱切换（整赛季+≥500+DM p<0.05+复核无系统性错误）；盲评双周定性参考；证伪=停 analyst 留 scout 存证；复核双路自动入队（赛前 JS>0.06 Tier1+赛后一对一错），结论三分类只进评测集。
- [06 证据卡产品原型](issues/06-evidence-ui-prototype.md)：V1 证据卡照单（来源/时点徽章+状态行+诚实降级）；详情挂现有 fixture-research 页（独立路由否决）；前端定 A 案 useChat+@ag-ui/client transport（assistant-ui 否决）；AG-UI 追问产品形态定案（流式+只引存证+引用徽章）；API 五端点草案照此拆票。
- [07 M3 spec 增补与拆票](issues/07-m3-spec-and-tickets.md)：八张实施票 08-15 发布（全 ready-for-agent）；spec §9 已改写入工作副本（暂缓→启动记录+两档冻结阈值）；转正随首个实施 PR；开工 08+09 并行。**地图抵达目的地。**

## M3 拆票（2026-09-19，用户确认粒度与顺序）

- 实施票 08-15 已发布于 [issues/](issues)，全部 ready-for-agent：08 GLM 基座 / 09 情报与基本面采集 / 10 scout / 11 gate+analyst / 12 LEAP+ADR-0009 / 13 评测协议落库 / 14 API+存量 UI / 15 AG-UI 追问。
- 依赖链：08+09 并行起步 → 10 → 11/12 并行 → 13/14 → 15。
- 实施期纪律：新 deployments 合入 main 后重启 serve（票 37 不受扰）；spec/地图/ADR-0009 转正随票 08 的 PR。
- [01 基本面与情报源调研对比](issues/01-fundamentals-and-intel-sources.md)：实测发现 API-Football 免费档 season gate 锁 2022-2024、当季不可得；推荐主力 = API-Football Pro $19/月 + open-meteo + 源B/源D 复用，Understat/中文舆情不进管道（scout 用 GLM web-search 替代）；**付费与否待票 03 开场裁决**。
- [02 GLM 选型与成本预算](issues/02-glm-model-and-budget.md)：scout=GLM-5.3-Flash、analyst=GLM-5.3、兜底=GLM-4.7-Flash 免费；OpenAI 兼容 v4 base_url（openai-agents 需 OpenAIChatCompletionsModel + 关 tracing）；月成本保守/典型/激进 ≈ ¥28/¥101/¥251（≤$50 达标）；熔断 60%→80%→95% 三级；开 key 清单在调研文件。

## Not yet specified

- 直爬目标站点清单与各自反爬边界（源B 伤停栏之外还有哪些站值得进）——挂票 07 拆出的采集实施票。

## Out of scope

- M4 全玩法/奖池优化（独立后续图）。
- 真钱下单自动化（合规红线，永久）。
- 证明完成前把融合概率接入真钱资格判定（spec v1.1 纪律，见票 04）。
- 新增付费数据源订阅（≤$50/月 约束内 GLM 优先；数据源付费升级另议）。
- 更换 agent 框架（ADR 0004 openai-agents 沿用，除非票 03 推翻并出 ADR）。
