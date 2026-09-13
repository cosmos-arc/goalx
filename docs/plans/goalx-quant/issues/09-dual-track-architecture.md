# 09 双线预测架构设计（ML 线 + LLM 分析师线）

Type: grilling
Status: resolved
Blocked by: 03, 05

## Question

与用户一起敲定双线预测子系统的设计（HITL grilling + domain-modeling，必要时 prototype 辅助）：

- ML 量化线的落地形态：按 03 的 v1 模型路线，训练/推理/存储如何嵌入 goalx 后端（定时任务、模型文件管理、特征存储的最小形态）。
- LLM 分析师线的落地形态：按 05 的框架推荐与 agent 拓扑，情报收集与出概率的编排方式、与 ML 线共享的特征格式。
- 对齐协议：两线输出的统一概率格式（比分矩阵 or 比赛级概率向量）、置信度字段、分歧度量与复核阈值、复核结果如何回流（是否进入训练数据/评测集）。
- CLV 跟踪的实现点：收盘赔率抓取时点、代理线选择。
- 技术选型收口：存储（SQLite vs Postgres）、调度（APScheduler vs Taskfile/cron）、LLM 调用层（框架落点）——本票一并裁决，必要时出 ADR。

产出：架构设计段落（进 spec）+ 技术选型 ADR 草案。

## Answer（2026-09-13 工作坊闭环）

用户四项裁决：**框架 = openai-agents**（用户确认 + 2026 框架调研佐证：对本场景「库式内嵌/类型化输出/OpenAI 兼容 provider/百场批量」没有更优替代；DSH 经 README 验证出局——无文档化 in-process/provider/工具路径 + 预览版不稳定）；**存储 = SQLite**（ADR 0003）；**调度 = Prefect**（用户指定，与其其他项目一致，ADR 0005，FastAPI 不内嵌调度器）；**复核回流 = 只进评测集/审计**（防泄漏、防自我强化，训练数据仅用官方赛果）。

按推荐定案的其余设计：

1. **规范概率表示 = 10×10 比分矩阵**（ADR 0006）：canonical 唯一，各玩法为推导视图；半全场以 λ 半场拆分（≈0.45λ）生成扩展视图。双线对齐协议：ML 输出矩阵 + bootstrap CI；LLM 线（analyst）输出 LEAP 结构化似然项（不直接出矩阵）；融合 `P ∝ P_ML × ∏ L_i^η`。
2. **分歧度量与路由**：JS 散度（融合后 vs 市场 Shin 去晦线），阈值 0.02 自动过 / 0.02-0.06 低优先复核 / >0.06 强制复核；杯赛小组末轮/淘汰赛/国内杯赛放宽阈值（票 17）。
3. **ML 线最小形态**：penaltyblog Dixon-Coles 按联赛分池（Tier 1 主动训练、Tier 2 顺手），Prefect 周任务拟合；模型工件（λ 表/参数 JSON）落 `models/`；特征来自 SQLite 事实表（API-Football + football-data.co.uk 历史）；Forecast 按 ADR 0001 落库存证。
4. **LLM 线形态**：openai-agents 实现 scout（小模型逐场，工具=自家数据 fetchers+搜索）/gate（纯代码）/analyst（大模型，仅 Tier 1 分歧场次）；provider 走 OpenAI 兼容端点（DeepSeek/GLM，key 位已在 .env）。
5. **CLV 实现点**：Prefect 定时在 kickoff 前 -30/-10/-1 分钟尽力快照（Odds API h2h+totals），事后以 football-data.co.uk PSC 收盘对账补漏；代理线 = Pinnacle close Shin 去晦（08 已验证 78% 非空 + AvgC 100% 兜底）。

产物：ADR 0003（SQLite）、0004（openai-agents，含 DSH/pydantic-ai/LangGraph/strands 等落选记录）、0005（Prefect）、0006（比分矩阵 canonical）。

对下游：12（回测协议）解锁；11（spec 走查）还差 10（资金规则）。

## 计划修订（2026-09-13）

技术栈决策保留；完整scout/gate/analyst及概率融合暂缓，先交付可信纸面闭环。半全场附属近似和全场矩阵边界见ADR 0006修订。
