# 05 Research: LLM Agent 架构与框架调研

- 票：`../issues/05-llm-agent-architecture.md`
- 调研日期：2026-09-12。所有链接均已访问核实；框架版本/定价为当日官方页面或 GitHub/PyPI API 实时数据。
- 方法：一手来源优先（arXiv 原文、官方文档、GitHub/PyPI API、官方定价页），二手报道仅用于交叉印证。

---

## TL;DR

1. **LLM 直接出概率打不过市场**：三篇独立 2026 World Cup 基准 + FT 报道的整季 EPL 模拟下注研究一致表明，前沿 LLM 的最优 Brier/accuracy 只能**逼近**去水市场共识，不能稳定超越；但**开放检索（open-book web search）是唯一显著提升预测质量的因素**，且 LLM 的价值集中在「读证据、提炼结构化情报」上。→ LLM 线定位为**情报提炼 + 统计先验之上的复核/重排层**，不是替代 ML 出概率。
2. **框架推荐 pydantic-ai**：typed agent、provider 中立、与 FastAPI/Pydantic 同族、可选 Temporal/DBOS durable execution、内置 Pydantic Evals，v2.43.0（当日发布）非常活跃。调度/持久化对单用户本地场景用 SQLite + launchd/cron 即可，不引入 LangGraph 平台。
3. **成本量级：每轮 100 场 $3–$15，赛季 $200–$800**。小模型批量提炼 + 大模型只复核分歧场次（LEAP 式「逐条证据→结构化似然→确定性融合」是现成的融合范式）。
4. **评测金标准是前瞻（prospective）+ 证据截断（evidence cutoff）**：训练截止晚于比赛日的模型做历史回放 + prompt/output 哈希冻结 + 候选池冻结，可把「预知未来」污染压到可解释范围。

---

## 1. LLM 足球预测的已有证据

### 1.1 LLM 直接出概率 vs 市场/统计模型

| 研究 | 设计 | 核心结果 |
|---|---|---|
| **WC2026-Agents**（[arXiv:2607.17765](https://arxiv.org/html/2607.17765v1)） | 4 个前沿助手（Claude Opus 4.8、GPT-5.5 high reasoning、Gemini 3.1 Pro、Grok Expert）带 web search，对 2026 世界杯 104 场做 search→act(1X2 概率+虚拟下注)→reflect；去水市场赔率作基准 | **无任何 agent 打败市场 Brier（市场 0.469 vs 最佳 agent 0.471）**；下注 ROI −18.1%（Claude）至 +10.3%（Grok）；最佳校准 ECE 0.068（Gemini）；4 个 agent 在 92% 场次给出相同 top pick；**共享检索面 → 共享误差，ensemble「继承而非抵消」共同偏差** |
| **LLM-SoccerArena**（[arXiv:2607.24573](https://arxiv.org/html/2607.24573v1)，MIT 开源） | 7 模型 × 104 场 × 3 时间窗 × 开/闭卷 × 2 prompt = 8,736 份预测（经 OpenRouter） | T−24h Brier 0.506–0.546，众数准确率 60–64%；**Gemini 开卷 T−2h Brier 0.497 ≈ 去水市场共识 0.498**（打平不超越）；**web 访问是唯一显著因子（Brier 0.535→0.512，Holm 校正后 p=0.045）**；模型间概率相关性高达 0.943（ensemble 仅 +0.0047 Brier）；T−24h→T−2h 几乎无增益；开卷每预测多花 ~$0.11 / ~22k input tokens / ~4s；开卷理由更多引用近况(+68pp)、赔率(+60.2pp)、**伤停(+53pp)** |
| **WorldCupArena**（[arXiv:2607.18084](https://arxiv.org/html/2607.18084v1)） | 13 个系统（9 个给定共享证据包、3 个带搜索、1 个 Deep Research），104 场五层细粒度评分 | 最佳系统（Claude Opus 4.7 Thinking+Search）结果准确率 70.7%，仅比 152 人球迷基线（69.7%）高 1.0pp、比 BetVictor（68.3%）高 2.4pp、比 Polymarket（65.4%）高 5.3pp；**13 个系统集体错押热门（相关联失败）**；在该共享证据设计下搜索反而无稳定增益；低比分场次最难（57.7%） |
| **AI World Cup 2026**（[arXiv:2608.03416](https://arxiv.org/abs/2608.03416)） | 10 个 LLM 赛前预测完整 2026 世界杯（分组/淘汰赛/名次），赛后计分 | GPT-5.5 Thinking 夺冠（744 分）；**自报置信度与准确率相关系数 r=−0.060（不相关）**；总分几乎只由淘汰赛环节决定（r=0.986） |
| **FT / General Index 整季研究**（[FT 报道](https://www.ft.com/content/544cbd80-492e-4ee8-a8b4-66e447361651)，2026-04；[technology.org 摘要](https://www.technology.org/2026/04/13/ai-models-lost-money-trying-to-bet-on-premier-league-soccer/)） | 8 个主流 AI 模型对完整模拟 EPL 赛季按真实 bookmaker 赔率下注 | **全部亏损**；Claude Opus 4.6「亏得最少」 |
| **ForecastBench**（[arXiv:2409.19839](https://arxiv.org/abs/2409.19839)，ICLR 2025，[forecastbench.org](https://www.forecastbench.org/)） | 动态无污染预测基准（~1000 道关于未来的题，双周轮换），含体育类 | 超级预测员 Brier 0.081–0.093，仍领先最佳 LLM（0.101–0.111）约 20%；LLM 只在「数据集题」上追平人类 |

**结论（Q1 前半）**：GPT-4 级以上的 LLM 直接输出比赛概率，最好情况与去水市场共识打平（Brier 差 <0.005），常态略逊，且**不具备正期望下注能力**（FT 整季研究、WC2026-Agents ROI 区间）。这为「ML 量化线为主、LLM 线为辅」的双线定位提供了直接依据。

### 1.2 LLM 做「结构化情报提炼」的有效性证据

- **LLM-SoccerArena 理由分析**：开卷模型的无依据断言更少，且更频繁引用伤停/近况/赔率等具体证据——说明 LLM 能把检索到的新闻转化为决策依据（[arXiv:2607.24573](https://arxiv.org/html/2607.24573v1)）。
- **ICDM 2025**：Samuel 等《Integrating LLM Sentiment Analysis into Machine Learning for Soccer Betting》（[IEEE Xplore](https://ieeexplore.ieee.org/abstract/document/11415979/)，[官方代码](https://github.com/SSSamueLDS/Integrating-LLM-Sentiment-Analysis-into-Machine-Learning-for-Soccer-Betting)）：抓取 BBC 赛后/赛前评论 → LLM zero-shot 打情感分 → 并入 ML 特征，在 EPL 2023-24 做下注模拟（F1/logloss + tick-by-tick 仿真）。管线形态与 goalx 计划一致（LLM 出结构化特征、ML 出概率），代码可参考。
- **SportsMetrics**（[arXiv:2402.10979](https://arxiv.org/html/2402.10979v2)）：长上下文 LLM 混合文本+数值特征预测比赛结果优于纯数值模型，GPT-4-1106 最佳——文本情报有增量信息。
- **Auditable LLM Harness**（[arXiv:2608.05030](https://arxiv.org/html/2608.05030v1)）：**「互补而非替代」架构的最严格单点证据**。动态 Dixon-Coles 出概率与候选比分池，LLM 只做语义重排（不动 λ）：
  - V1 纯 Dixon-Coles：Top-1 比分 10.0%，Top-3 26.7%，1X2 53.3%（log loss 0.9878 / Brier 0.587 / RPS 0.2095）；
  - V4（共享根节点+级联+尾锚）LLM 重排：Top-1 14.7%，Top-3 30.7%，score-1X2 50.0%（vs 32.0%，McNemar p=0.0024）。
  - 明确警示：可解释性≠校准；前 100 场用于设计迭代属「开发污染」；闭源模型赛后运行无法排除记忆（model-memory risk）。
- **LEAP**（[arXiv:2609.01337](https://arxiv.org/html/2609.01337v1)，2026-09）：LLM 每次**只读一条证据**、输出结构化 likelihood 参数（绝不直接出预测），确定性闭式共轭贝叶斯更新合成后验：FutureX +3.6~18.1 分，ECE 几乎减半（0.184→0.088），优于线性 opinion pool；代价 ~2× tokens、p95 延迟 27.8s。**这是「LLM 提炼情报→统计融合」范式的最佳现成方法论**（见 §5）。

---

## 2. Agent 框架现状（2026-09-12 实测核实）

### 2.1 硬数据（GitHub API + PyPI API，2026-09-12 拉取）

| 框架 | 最新版（发布日） | Stars | 最近 push | Python |
|---|---|---|---|---|
| [LangGraph](https://github.com/langchain-ai/langgraph) | 1.2.11（PyPI，2026-08-11） | 41,528 | 2026-09-11 | ≥3.10 |
| [pydantic-ai](https://github.com/pydantic/pydantic-ai) | 2.43.0（2026-09-12，当日） | 19,877 | 2026-09-12 | ≥3.10 |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) | 0.22.2（2026-09-09） | 29,384 | 2026-09-12 | ≥3.10 |
| [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) | 0.2.152（2026-09-02） | 8,086 | 2026-09-11 | ≥3.10 |

全部活跃；差异在成熟度（版本号）与生态定位。

### 2.2 能力对照（来源：各自官方文档）

- **LangGraph**（[docs.langchain.com/oss/python/langgraph/overview](https://docs.langchain.com/oss/python/langgraph/overview)）：低层编排框架 + 运行时，状态图、durable execution、checkpoint 持久化、human-in-the-loop；「不抽象 prompt 与架构」，学习曲线陡。**内置 cron/后台任务是付费 LangSmith Deployment（原 LangGraph Platform，2025-10 更名）的功能，不在 MIT 开源库内**（[官方博客](https://www.langchain.com/blog/langgraph-cloud)、[ambient agents 博客](https://www.langchain.com/blog/introducing-ambient-agents)、[langserve#791](https://github.com/langchain-ai/langserve/issues/791)）。文档已迁移到 docs.langchain.com，生态处于 LangChain 大重组期。
- **pydantic-ai**（[pydantic.dev/docs/ai/overview](https://pydantic.dev/docs/ai/overview/)）：typed agent loop（`Agent[Deps, Output]`）、`@agent.tool` 函数工具 + RunContext 类型化依赖、deferred tools 支持 human-in-the-loop 审批、内置 MCP/web-search/web-fetch 能力、YAML/JSON agent spec；**provider 中立**（OpenAI/Anthropic/Google/Bedrock/Groq/Ollama…字符串切换）；durable execution 与 Temporal/DBOS/Prefect/Restate 官方共维护；Pydantic Evals 评测；离线 `test` 模型免 key 测试。FastAPI 集成虽未单列页面，但同属 Pydantic 家族、天然契合（本仓库后端即 FastAPI + Pydantic）。
- **OpenAI Agents SDK**（[openai.github.io/openai-agents-python](https://openai.github.io/openai-agents-python/)）：轻量 agent loop + handoffs + sessions（循环内工作记忆）+ guardrails + tracing，sandbox/realtime/HITL；非 OpenAI 模型经 LiteLLM/Any-LLM 适配器可用。**仍处 0.x**，API 可能变动；仅 58 个 open issues（管理良好）。
- **Claude Agent SDK**（[GitHub README](https://github.com/anthropics/claude-agent-sdk-python)）：本质是 Claude Code CLI 的 Python 包装（捆绑 CLI），自带 Read/Write/Edit/Bash 全套通用工具；自定义域工具经 `@tool` + in-process MCP server 注册；**模型锁定 Claude 家族**，受 Anthropic 商业条款约束。为「通用编码/文件 agent」设计，不是可插模型的推理服务框架。
- **自研 tool-loop**：Anthropic《Building Effective Agents》（[anthropic.com/engineering/building-effective-agents](https://www.anthropic.com/engineering/building-effective-agents)）明确建议「最成功的团队用简单可组合的模式而非复杂框架」，框架抽象会遮蔽 prompt、妨碍调试。对本场景，一个 100 行的 while 循环完全可行。

### 2.3 推荐：**pydantic-ai（主）+ 纯 Python 编排（辅）**

理由（针对「本地 Mac、单用户、FastAPI 后端」）：
1. **类型即契约**：LLM 线的核心资产是结构化输出（`MatchIntel`、likelihood 参数），pydantic-ai 的 typed output/tools 与后端 Pydantic 模型无缝复用，也符合本仓库 basedpyright strict。
2. **Provider 中立**：分层策略需要小模型（Gemini Flash-Lite/gpt-5-mini）+ 大模型（Sonnet/Opus/GPT-5.5）混用，字符串即可切换；对比 Claude Agent SDK 锁定 Claude、OpenAI SDK 主推 OpenAI。
3. **不引入平台依赖**：LangGraph 的 cron/队列属付费 LangSmith Deployment；单用户场景调度用 launchd/cron/Taskfile 打 FastAPI 端点，持久化用 SQLite（情报记录 + prompt/output 哈希），比引入 LangGraph 状态机简单一个数量级。LangGraph 的优势（多 agent 状态图、大规模并发 checkpoint）在单用户场景是负资产。
4. **退路清晰**：若 pydantic-ai 某能力不够，其抽象薄，降到裸 SDK/自研 loop 的迁移成本低（Anthropic 的简单性建议本身就是背书）。OpenAI Agents SDK 是第二候选（更轻、MIT），但 0.x 且 provider 适配绕一层。

不推荐：LangGraph（过重 + 平台绑定 + 生态重组期 churn）；Claude Agent SDK（模型锁定，适合本地 CLI 助手而非嵌入后端的推理服务）。

---

## 3. 赛前情报 agent 设计模式

### 3.1 模式选择（依据 Anthropic《Building Effective Agents》）

情报收集是**任务可预先分解**的 workflow（每场比赛 → 固定几个信息槽位），不是开放式 agent。推荐 **prompt chaining + routing + parallelization（sectioning 变体）**，不用 orchestrator-workers：

```
matchday（50-150 场）
  └─ 每场一个 scout 任务（并行，asyncio）
       ├─ T1 新闻/伤停检索（per-club query，小模型）
       ├─ T2 天气/场地（可纯 API，无 LLM）
       ├─ T3 赔率快照（内部 ML 线 + 外部盘口，无 LLM）
       └─ 合并 → MatchIntel（结构化 schema）
  └─ 纯代码 gate：JS 散度(P_ML, P_mkt) + 情报冲击评分 → 是否进复核
       └─ analyst（大模型，仅分歧场次）：读 ML 概率 + MatchIntel + 开放检索
            → 结构化 likelihood 建议 + 理由（LEAP 式，不改 ML 参数）
```

### 3.2 工具集

- `search_news(club, days)`：web search（Gemini grounding 或 SerpAPI/Tavily）；**新鲜新闻用搜索而非静态 RAG**——LLM-SoccerArena 显示开卷是唯一显著改善因子，而静态语料无法覆盖赛前 24h 的伤停/轮换新闻。
- `fetch_page(url)`：正文抽取（trafilatura/jina reader）。
- `get_fixture` / `get_injuries` / `get_weather`：结构化体育 API（football-data.org、API-Football 等，票 02/07 范畴）。
- `get_ml_probabilities(match)` / `get_odds_snapshot(match)`：内部工具，让 analyst 看到统计先验（Harness 论文的「证据包」模式）。

### 3.3 输出结构：结构化特征 > 自然语言简报

证据链：LEAP（结构化 likelihood，ECE 减半且可审计 leave-one-out 贡献）> Harness（schema 校验器 + 冻结候选池，错误可检查）> 自由文本（WorldCupArena 五层细粒度评分证明 LLM 能产出丰富结构，但自报置信度不可信）。建议 `MatchIntel` schema：

```python
class MatchIntel(BaseModel):
    evidence_cutoff: datetime          # 证据截断时间（防泄漏）
    injuries: list[InjuryFact]         # 球员、性质、来源 URL、影响评级 0-1、recency
    rotation_risk: RotationFact        # 赛程密度、杯赛轮换史、来源
    motivation: MotivationFact         # 保级/欧战/无欲无求，标签化
    sentiment: SentimentFact           # 本地舆情摘要 + 极性分 + 来源
    likelihood_hints: LikelihoodHints  # 每条证据对 H/D/A 的方向与强度（LEAP 式）
    confidence_note: str               # 自由文本（仅作人读备注，不进模型）
```

### 3.4 RAG 的必要性：**线上不需要，评测需要**

- 线上（赛前情报）：新闻时效 <24h，静态向量库必然滞后；web search 实证有效（§1.1/§1.2）。
- 线下（历史回放评测）：需要「当日可见」的语料快照 → 这正是 RAG 的用武之地——把按 kickoff 截断的历史新闻建成检索库，保证回放时模型只能看到赛前信息（详见 §6）。

---

## 4. 成本估算（价格 2026-09-12 官方页核实）

### 4.1 单价表（每 1M tokens，标准价；Batch 半价）

| 模型 | Input | Output | 来源 |
|---|---|---|---|
| gpt-5-nano | $0.05 | $0.40 | [OpenAI 定价页](https://developers.openai.com/api/docs/pricing) |
| gpt-5-mini | $0.25 | $2.00 | 同上 |
| gpt-5.5 | $5.00 | $30.00 | 同上 |
| Claude Haiku 4.5 | $1 | $5 | [claude.com/pricing](https://claude.com/pricing) |
| Claude Sonnet 5 | $2 | $10 | 同上 |
| Claude Opus 5 | $5 | $25 | 同上 |
| Gemini 3.5 Flash-Lite | $0.30 | $2.50 | [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| Gemini 3.1 Pro | $2 | $12 | 同上 |

Gemini grounding（Google Search）：每月 5,000 次免费，之后 $14/1,000 次。LLM-SoccerArena 实测开卷每预测额外 ~$0.11（含 ~22k input tokens）——独立印证下面的量级。

### 4.2 每轮 100 场的分层估算

假设：每场 scout 读 8–15 个文档 ≈ 25k input + 1.5k output（小模型）；20% 场次进大模型复核（+ 开放检索 20k input + 2k output）。

| 层 | 用量 | 模型与单价 | 费用 |
|---|---|---|---|
| scout 批量（100 场） | 2.5M in + 0.15M out | gpt-5-mini（$0.25/$2） | ≈ **$0.93** |
| 同上（换 Gemini Flash-Lite） | 同上 | $0.30/$2.50 | ≈ **$1.13** |
| analyst 复核（20 场） | 0.4M in + 0.04M out | Sonnet 5（$2/$10） | ≈ **$1.20** |
| analyst 复核（全量 100 场） | 2M in + 0.2M out | Sonnet 5 | ≈ **$6** |
| 检索（300 次/轮） | — | Gemini grounding 免费额度 | **$0**（月 5,000 次内） |

**量级结论：每轮（50–150 场）$1–$10；一个赛季（多联赛 ~40–60 轮）$100–$600**。即使全量都用 Opus/GPT-5.5 复核 + LEAP 式双倍 token，也在每轮 $20–$40、赛季 < $2,000 的量级。情报线成本可忽略，真正的成本在评测回放（历史场次多）——回放用 nano/mini + Batch API（半价）控制。

---

## 5. 双线对比与融合

- **分歧度量**：1X2 概率向量上的 **JS 散度**（有界 [0, ln3]，阈值语义稳定）或 TV；比分矩阵层面用行列边缘的加权和。触发分级：JS < 0.02 自动通过 → 0.02–0.06 进低优先级复核队列 → > 0.06 强制人工/大模型复核。数值仅为初始建议，应在校准集上标定。
- **ensemble 证据**：
  - 12-LLM 简单聚合即可媲美 925 人类预测群体（[Wisdom of the Silicon Crowd, PNAS Nexus 2024](https://pmc.ncbi.nlm.nih.gov/articles/PMC11800985/)）——但这是**跨人群**的多样性。
  - LLM 之间冗余度极高（概率相关 0.943，ensemble 仅 +0.0047 Brier，LLM-SoccerArena）；共享检索面导致共享偏差（WC2026-Agents）。**→ 融合的价值在「ML 概率 × LLM 情报」这种跨信息源的互补，不在多 LLM 投票。**
  - **推荐融合范式 = LEAP**（[arXiv:2609.01337](https://arxiv.org/html/2609.01337v1)）：ML 线出先验（Dixon-Coles/GLM），LLM 对每条情报输出结构化似然，确定性 log-pool 后验 `P ∝ P_ML × ∏ L_i^η`（η 温度控制 LLM 话语权），优于线性 opinion pool（0.7284 vs 0.6808 FutureX），ECE 减半，且闭式更新可审计（每条证据的 Δⱼ 贡献可算）。依赖聚类（同源证据去重）与可靠性采样（重复询问按一致度收缩）是其两个关键护栏。
- **复核队列交互设计**：AI World Cup 证明 LLM 自报置信度与准确率零相关（r=−0.060）→ 队列 UI **不要展示 LLM 置信度**，应并列展示双方概率（P_ML vs P_mkt vs 融合后）、top 分歧驱动证据（带来源链接与 recency）、以及「接受 LLM 调整 / 维持 ML / 跳过」三态决策；决策回流为评测标签。

---

## 6. 评测集设计与数据泄漏控制

### 6.1 威胁模型：「预知未来」污染

闭源 LLM 训练语料含赛后报道与「最终比分」文本，历史回放时模型可能直接回忆结果（Harness 论文明言：闭源模型赛后运行，「输入隔离不能排除记忆化的赛果」）。三层控制（综合 WC2026-Agents、ForecastBench、Harness 的做法）：

1. **前瞻测试为金标准**（prospective，WC2026-Agents 的 "contamination-free by construction"：训练截止后的比赛，答案在查询时不存在）——日常运行本身就是评测，prompt 版本固定后滚雪球积累。
2. **回放时选模型**：只用「训练截止日 < 比赛 kickoff」的模型版本组合（闭源模型需查官方 knowledge cutoff 文档并记录版本号）；报告结果时标注该约束。
3. **证据截断 + 冻结**（Harness 的 auditable harness）：每场只喂 kickoff 前抓取的语料快照（RAG 库按日期截断）；prompt、输出、候选池全部哈希冻结；同日所有预测先于任何当日状态更新落地。

### 6.2 评测矩阵

- **基准线**：去水市场收盘赔率（最强基准，WC2026-Agents 已示范）、恒基线（home/draw/away 频率）、纯 ML 线。
- **指标**：RPS/Brier/log loss（概率质量）+ ECE（校准）+ ROI/Kelly 收缩后收益（决策质量）。注意 LLM 自报置信度不可作为指标。
- **Prompt 版本对比**：固定评测集（回放快照或前瞻积累），跑 A/B（pydantic Evals 管理版本与断言）；LLM-SoccerArena 证明 prompt 顺序（比分式 vs 概率式）对准确率无显著影响（p=0.693）——但建议概率式 prompt（决策支持语义更直接）。
- **消融**：开/闭卷（预期差 ~0.023 Brier）、有无 likelihood_hints、η 取值（LEAP 在 0.5–2.0 间稳定）、scout 模型档位。
- **反窥探测试**（可选但推荐）：对回放集插入「未来日期语料」探针，若模型输出显著变化则记忆污染报警。

---

## 参考文献汇总

论文（arXiv/出版方原文）：
- WC2026-Agents — https://arxiv.org/html/2607.17765v1
- LLM-SoccerArena — https://arxiv.org/html/2607.24573v1
- WorldCupArena — https://arxiv.org/html/2607.18084v1
- AI World Cup 2026 — https://arxiv.org/abs/2608.03416
- Auditable LLM Harness for Exact-Score Reranking — https://arxiv.org/html/2608.05030v1
- LEAP — https://arxiv.org/html/2609.01337v1
- ForecastBench — https://arxiv.org/abs/2409.19839
- SportsMetrics — https://arxiv.org/html/2402.10979v2
- Wisdom of the Silicon Crowd — https://pmc.ncbi.nlm.nih.gov/articles/PMC11800985/
- Integrating LLM Sentiment Analysis into ML for Soccer Betting（ICDM 2025）— https://ieeexplore.ieee.org/abstract/document/11415979/ （代码：https://github.com/SSSamueLDS/Integrating-LLM-Sentiment-Analysis-into-Machine-Learning-for-Soccer-Betting）
- FT/General Index EPL 整季下注研究 — https://www.ft.com/content/544cbd80-492e-4ee8-a8b4-66e447361651

官方文档/一手数据：
- pydantic-ai — https://pydantic.dev/docs/ai/overview/ （v2.43.0，PyPI API 2026-09-12）
- LangGraph — https://docs.langchain.com/oss/python/langgraph/overview （v1.2.11；cron 属付费 LangSmith Deployment：https://www.langchain.com/blog/introducing-ambient-agents）
- OpenAI Agents SDK — https://openai.github.io/openai-agents-python/ （v0.22.2）
- Claude Agent SDK — https://github.com/anthropics/claude-agent-sdk-python （v0.2.152）
- Anthropic, Building Effective Agents — https://www.anthropic.com/engineering/building-effective-agents
- 定价：OpenAI https://developers.openai.com/api/docs/pricing ；Anthropic https://claude.com/pricing ；Gemini https://ai.google.dev/gemini-api/docs/pricing （均为 2026-09-12 页面）
