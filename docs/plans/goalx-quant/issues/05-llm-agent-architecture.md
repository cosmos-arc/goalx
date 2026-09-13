# 05 LLM Agent 架构与框架调研

Type: research
Status: resolved

## Question

LLM 分析师线（与 ML 量化线并行）的技术架构与框架选型是什么？双线对比的评测怎么做？供双线架构设计（09）与后续实施。

具体要回答：

1. **LLM 足球预测的证据**：已有的 LLM（GPT-4 级）足球比赛预测研究/项目（论文、blog、Kaggle），LLM 直接出概率的精度 vs 统计模型 vs 市场基准的结论；LLM 在「结构化情报提炼」（伤停、新闻、动机因素）上的有效性证据。
2. **Agent 框架现状（2026）**：LangGraph、pydantic-ai、OpenAI Agents SDK、Claude Agent SDK、自研 tool-loop——对「本地 Mac、单用户、FastAPI 后端」场景的适配性、Python 集成成本、持久化/定时调度能力、社区活跃度；给出推荐与理由。
3. **情报 agent 模式**：赛前情报收集（伤停名单、轮换新闻、天气、舆情情绪）的 agent 设计模式——工具集（搜索/抓取/API）、输出结构（结构化特征 vs 自然语言简报）、RAG 用于新闻聚合的必要性。
4. **成本与模型选择**：每轮比赛日约 50-150 场比赛的情报处理，LLM 调用成本量级估算；用小模型批量 + 大模型复核的分层策略。
5. **双线对比与融合**：LLM 概率与 ML 概率的分歧度量（如 JS 散度）、分歧复核队列的交互设计参考、ensemble 融合的研究结论。
6. **评测**：LLM 线的评测集设计（历史场次回放、prompt 版本对比、防止数据泄漏——LLM 训练数据含赛后报道的「预知未来」污染问题如何控制）。

产出：框架推荐 + agent 拓扑草图（哪些 agent、各自工具与输出）+ 评测方案草案 + 成本量级估算。

## Answer

详细调研（全部来源已核实，2026-09-12）：[../research/05-llm-agent-architecture.md](../research/05-llm-agent-architecture.md)

### 1. 定位结论（证据驱动）

三篇独立 2026 世界杯基准 + FT 报道的整季 EPL 模拟下注研究一致表明：前沿 LLM 直接出 1X2 概率最好只能**打平**去水市场共识（Brier 差 <0.005），无正期望下注能力；但**开放检索是唯一显著改善因子**，且 LLM 的真实价值在结构化情报提炼。→ **LLM 线定位为「情报提炼 + 统计先验之上的复核/重排层」，ML 线为主出概率**。融合范式采 LEAP（arXiv:2609.01337）：LLM 逐条证据出结构化似然，与 ML 先验做确定性 log-pool（`P ∝ P_ML × ∏ L_i^η`），实证 ECE 减半且优于线性 pool。

### 2. 框架推荐：pydantic-ai + 纯 Python 编排

- **pydantic-ai v2.43.0**（当日仍发版，19.9k stars）：typed `Agent[Deps, Output]`、provider 中立（小/大模型混用一行切换）、原生 Pydantic 输出与本仓库 FastAPI + basedpyright strict 契合、内置 Pydantic Evals 与离线 test 模型。
- **不选**：LangGraph（cron/队列绑付费 LangSmith Deployment，状态机对单用户过重）；Claude Agent SDK（模型锁定 Claude + 商业条款）；OpenAI Agents SDK 作第二候选（轻但 0.x）。Anthropic 官方亦建议简单可组合模式优于重框架。
- 调度：launchd/cron/Taskfile → FastAPI 端点；持久化：SQLite（MatchIntel + prompt/output 哈希）。

### 3. Agent 拓扑草图

```
matchday (50–150 场，并行)
 ├─ scout ×N（小模型：gpt-5-mini / Gemini Flash-Lite，每场一个）
 │   工具：search_news(club) / fetch_page / get_injuries / get_weather / get_odds
 │   输出：MatchIntel{伤停(球员/来源/影响0-1), 轮换风险, 动机标签, 舆情+极性,
 │         likelihood_hints(每条证据→H/D/A方向+强度), evidence_cutoff}
 ├─ gate（无 LLM，纯代码）：JS(P_ML, P_mkt) + 情报冲击分 → 路由
 │   JS<0.02 自动过 / 0.02–0.06 低优先复核 / >0.06 强制复核
 └─ analyst（大模型：Sonnet 5 级，仅 20% 分歧场次）
     输入：ML 概率 + MatchIntel + 开放检索（Harness「证据包」模式）
     输出：结构化似然建议 + 理由；不改 ML 的 λ，只提供 LEAP 似然项
```

### 4. 评测方案草案

- 基准：去水收盘赔率（最强线）、恒基线、纯 ML 线；指标 RPS/Brier/log loss + ECE + ROI；**不采信 LLM 自报置信度**（与准确率 r=−0.06）。
- 泄漏控制三层：①前瞻运行为金标准（WC2026-Agents「构造即无污染」）；②回放只用「训练截止 < kickoff」的模型版本并记录；③证据按 kickoff 截断建 RAG 快照 + prompt/输出/候选池哈希冻结（Harness auditable harness）；可选「未来语料探针」报警记忆污染。
- Prompt 版本对比走 pydantic Evals 固定集 A/B；消融：开/闭卷、η、模型档位。

### 5. 成本量级

每轮 100 场：scout 全量（小模型）≈ **$1**；大模型复核 20% 场次 ≈ **$1–2**（全量复核 ≈ $6）；grounding 检索月 5,000 次内免费。**每轮 $1–$10，赛季 $100–$600**；评测回放用 nano/mini + Batch API（半价）控制。情报线成本相对数据源订阅可忽略。

## Addendum（2026-09-12，用户决策）

**用户否决 pydantic-ai 推荐**，指定短名单：**DeepSeek Harness（DSH）** 与 **OpenAI Agents SDK（openai-agents）**，最终选型在 09（双线架构设计）裁决。原 §2 的 pydantic-ai 结论作废，其余（定位/拓扑/评测/成本）不变。

两个候选的要点（供 09 核实）：

- **DSH（`deepseek-ai/deepseek-harness`）**：2026-08-13 开源（MIT），「一切皆插件」Agent 运行时（模型/工具/技能/会话/沙箱全插件化，基于 Cordis 元框架），`npx` 一键启动 + Python SDK，发布 3 天 12.8 万 star。定位是把模型接入文件/终端/网页工具并组织上下文的**运行时**（对标开放的 Claude Code 层，非 API 客户端库）。优势：工具/模型全可换，与「scout 多源检索」拓扑契合，对 DeepSeek/GLM 系开源生态友好。风险：v0.1.0-rc.5 开发者预览版，API 稳定性与 FastAPI 服务内嵌方式需验证。
- **OpenAI Agents SDK**：成熟度最好的轻量选择（sessions/handoffs/guardrails/tracing），支持 OpenAI 兼容端点自定义 model provider（DeepSeek/GLM 均兼容），纯库形态嵌入 FastAPI 最顺。当初「第二候选」的理由（0.x 版本）需按 2026-09 现状复核。

09 的验证清单：① DSH Python SDK 的服务化嵌入（能否作为库调用而非独立进程）；② 两者的 OpenAI 兼容 provider 配置（DeepSeek/GLM）；③ 检索/抓取工具插件化成本；④ scout 小模型批量调用的并发与断点续跑。
