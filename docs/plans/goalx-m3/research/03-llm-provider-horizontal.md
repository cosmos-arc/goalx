# 03 LLM 提供商横向比较（调研报告）

Ticket 定位：票 02 裁决智谱后的横向验证 · 调研日期 2026-09-19。
目的：在票 02 已定 GLM 双档（`02-glm-selection.md`）的基础上，对 9 家候选逐一查证 2026-09-19 当日现行模型阵容与官方定价，做能力横评与三档成本模拟，验证"GLM 双档最优"是否成立。初版覆盖 8 家；同日复核按用户指示补入 xAI Grok（§2.10），方法与口径不变。口径：人民币报价为主，汇率按 ¥7.2 = $1 双标；用量画像（scout/analyst/兜底三角色、三档月用量）直接沿用票 02。

## 1. 一句话速览

| # | 提供商 | 2026-09 旗舰 | 轻量档 | 免费档 | OpenAI 兼容 | 内置 web search |
|---|---|---|---|---|---|---|
| 1 | 智谱 GLM（基线） | GLM-5.3 ¥8/¥28 | GLM-5.3-Flash ¥0.8/¥2.8 | GLM-4.7-Flash（200K） | 有 | 有（¥0.01/次） |
| 2 | OpenAI | GPT-6 Astra $10/$50 | GPT-5.6 Luna $0.2/$1.2 | 无 | 原生 | 有（$0.01/次） |
| 3 | Anthropic | Claude Fable 5.1 $10/$50 | Haiku 4.5 $1/$5 | 无（仅初始赠金） | **无**（需 SDK 适配层） | 有（$0.01/次） |
| 4 | Google Gemini | Gemini 3.1 Pro Preview $2/$12 | Gemini 3.8 Flash $0.75/$3.75（促销） | 有（Flash 免费档） | 有（兼容层） | 有（5k 次/月免费） |
| 5 | DeepSeek | V4-Pro $1.32/$3.96（错峰半价） | V4.1-Flash $0.30/$1.20 | 无 | 有（+原生 Responses） | 无（需外接） |
| 6 | 阿里 Qwen（百炼） | qwen3.8-max ¥12/¥36 | qwen3.8-flash ¥1.094/¥3.427 | 新人 100 万 tok/模型（90 天） | 有 | 有（按日阶梯计费） |
| 7 | 月之暗面 Kimi | kimi-k3 ¥20/¥100 | kimi-k2.6 ¥6.5/¥27 | 无 | 有 | 有（$0.005/次，二手口径） |
| 8 | 字节豆包（方舟） | Doubao-Seed-2.1-pro ¥6/¥30 | Seed-2.1-turbo ¥3/¥15；2.0-mini ~¥0.2/¥2（估） | 部分模型促销日额度 | 有 | 有（插件按次计费） |
| 9 | MiniMax | MiniMax-M3 $0.30/$1.20（单档） | 同旗舰（≤512K 输入半价促销） | 无 | 有 | 无（需外接） |
| 10 | xAI Grok | Grok 4.6 $2/$6 | Grok 4.1 Fast $0.2/$0.5 | 无（仅 $25 新户赠金） | 有（api.x.ai/v1） | 有（**$0.025/来源**，Web+X 多源至 $0.10/次） |

## 2. 各家现行阵容与定价（官方定价页直查，2026-09-19）

> 标注：**一手** = 官方定价/文档页直查；**二手** = 官方公告 + 多家聚合交叉，未能直查官方表格（页面 JS 渲染或快照滞后）。所有价格为每 1M token。

### 2.1 智谱 GLM（基线，数字复用票 02）

| 模型 | 上下文 | 输入/输出（¥/M） | 缓存命中 | 备注 |
|---|---|---|---|---|
| GLM-5.3 | 1M | 8 / 28 | ¥2 | thinking 模式，json_schema + function calling（tool_choice 仅 auto） |
| GLM-5.3-Flash | 1M | 0.8 / 2.8 | ¥0.23 | 多模态 |
| GLM-4.7-Flash | 200K | **免费** | — | 兜底/开发档 |
| 内置 web_search | — | search_std ¥0.01/次、search_pro ¥0.03/次 | — | Chat Completions 内置工具 + MCP |

base_url `https://open.bigmodel.cn/api/paas/v4/`（无 /responses API）。来源：[bm-pricing]、[bm-models]、[bm-openai]（见票 02 来源清单，均为官方文档）。

### 2.2 OpenAI（二手为主：官方页快照滞后，型号与价格经公告+聚合交叉）

现行三代同堂：**GPT-6 Astra**（2026-09-03 发布，~1.05M 上下文 / 128K 输出，分阶段放量中）、**GPT-5.6**（Sol/Terra/Luna 三档，2026-07-09 发布、07-30 降价）、GPT-5.5 及以下为历史款。

| 模型 | 输入/输出（$/M） | 缓存命中 | 定位 |
|---|---|---|---|
| GPT-6 Astra | 10 / 50 | $1 | 旗舰（ARC-AGI-3/SWE-bench Pro 榜首） |
| GPT-5.6 Sol | 4 / 20（07-30 从 5/30 降） | $0.50 | 上代旗舰 |
| GPT-5.6 Terra | 2 / 12 | — | 均衡档 |
| GPT-5.6 Luna | 0.20 / 1.20（07-30 降 80%） | $0.02 | 轻量档 |

- Web search 工具：$10/1000 次（$0.01/次），搜索内容 token 另按模型单价计费（官方定价页口径）。
- 免费档：API 无免费模型（仅新账号一次性赠金）。
- 注意：官方 pricing 页 2026-09-19 抓取快照仍是 GPT-5.2 时代，且标注 **2026-10-05 起部分价格调整**——接入前需复核。
- OpenAI 兼容：原生（openai-agents SDK 首选路径）。来源：[oai-56]、[oai-astra]、[oai-pricing-snapshot]、[oai-search]。

### 2.3 Anthropic Claude（一手：官方 pricing/models 页直查）

2026-06-09 发布 **Claude Fable 5 / Mythos 5**（frontier 档），09-01 更新 Fable 5.1；07-24 发布 Claude Opus 5。

| 模型 | 输入/输出（$/M） | 缓存读 | 上下文 | 备注 |
|---|---|---|---|---|
| Claude Fable 5.1（claude-fable-5-1） | 10 / 50 | $0.25（2.5%） | 1M | 旗舰；AA 指数 v4.2 榜首 |
| Claude Opus 5（claude-opus-5） | 5 / 25 | ~$0.50（10%） | 1M | 次旗舰；Batch 半价 $2.5/$12.5 |
| Claude Sonnet 5 | 3 / 15 | — | 1M | 中档 |
| Claude Haiku 4.5 | 1 / 5 | — | 200K | 轻量档（价格经二手确认） |

- Web search：$10/1000 次（$0.01/次，含 2 万 token 搜索内容费）；web fetch 免费。
- 免费档：无（仅初始小额赠金）。
- **OpenAI 兼容端点：没有**。仅 Messages API（另有 Anthropic 协议）；openai-agents SDK 走 `AnthropicModel` 适配层，与 ADR 0004 的"OpenAI 兼容 Chat Completions"路线有摩擦。
- thinking：现行旗舰为自适应思考（adaptive），扩展思考参数仅 Haiku 4.5 档保留。来源：[aa-pricing]、[aa-models]、[aa-fable]。

### 2.4 Google Gemini（一手：官方 pricing 页，页面标注 2026-09-16 更新）

现行：Pro 线最新为 **Gemini 3.1 Pro Preview**（Gemini 3 Pro 已于 2026-06-23 弃用）；Flash 线最新为 **Gemini 3.8 Flash**（长程工程/智能体定位）。

| 模型 | 输入/输出（$/M） | 缓存 | 上下文 | 备注 |
|---|---|---|---|---|
| Gemini 3.1 Pro Preview | 2 / 12（≤200K 输入）；4 / 18（>200K） | $0.20 | 长上下文分档 | 免费档不可用；Batch 半价 |
| Gemini 3.8 Flash | **0.75 / 3.75**（促销，至 2026-12-31；2027 起恢复 1.50 / 7.50） | $0.075 | 1M+ | 有免费档 |
| Gemini 3.7 Flash | 0.75 / 3.75（同促销） | — | 1M+ | 有免费档 |
| Gemini 3.1 Flash-Lite | 0.25 / 1.50 | — | 1M | 有免费档 |

- **Grounding（Google Search）：Gemini 3 系每月共享 5,000 次免费**，超出 $14/1000 次（$0.014/次）——goalx 三档用量（500/2000/4000 次/月）全部落在免费额度内。
- 免费档限速（Flash 线）：约 10 RPM / 250K TPM / 1500 RPD（Gemini 3 Flash 口径，第三方汇总+官方限速页）——冒烟够用。
- OpenAI 兼容：有兼容层（`generativelanguage.googleapis.com/v1beta/openai/`），另有新 Interactions API。
- 风险：中国大陆访问需自备网络路径，美元计费。来源：[gem-pricing]、[gem-limits]。

### 2.5 DeepSeek（一手：官方 pricing 页两次直查一致）

现行两款（V4 世代，`deepseek-chat`/`deepseek-reasoner` 旧名仍接受但路由新模型）：

| 模型 | 输入（缓存未命中/命中） | 输出（$/M） | 上下文/最大输出 | 并发 |
|---|---|---|---|---|
| deepseek-v4-pro（V4-Pro-0813） | 1.32 / 0.044 | 3.96 | 1M / 384K | 500 |
| deepseek-flash（V4.1-Flash） | 0.30 / 0.006 | 1.20 | 1M / 384K | 2500 |

- **错峰半价**：非高峰时段全部五折（Pro $0.66/$1.98）。
- 支持 thinking、tool calls、JSON 结构化输出；**原生支持 OpenAI Responses API 与 Anthropic API**（openai-agents 可走首选路径）。
- 无内置 web search，需外接 Serper（$1/1000 次）或 Tavily（$8/1000 次基础档，免费 1000 次/月）。
- 免费档：无。背景：2026-08 行业报道 DeepSeek 因智能体需求整体涨价。来源：[ds-pricing]、[ds-v4]、[tavily]、[serper]。

### 2.6 阿里通义千问 Qwen / 百炼（一手：官方帮助文档；免费额度页 6 天前更新）

现行命名已切换到 3.7/3.8 系列（qwen-max/plus/turbo/flash 旧名逐步退役）：

| 模型 | 输入/输出（¥/M） | 缓存命中 | 上下文 | 能力（官方模型表） |
|---|---|---|---|---|
| qwen3.8-max | 12 / 36 | 有 | 1M | thinking/FC/内置工具/结构化输出全支持 |
| qwen3.7-plus | ~2 / ~8（二手） | 有 | 1M | 同上 |
| qwen3.8-flash | 1.094 / 3.427 | 0.117 | 1M | 同上；显式缓存创建 ¥1.458/M |

- 免费额度：每模型独立的 100 万 token 新人额度（90 天有效，主/子账号共享）；无长期免费模型。
- 联网搜索：UnifiedSearch 工具按日阶梯计费（通用版 Generic），历史口径约 ¥0.03/次量级（现价以计费文档为准，本报告按 ¥0.03/次估算）。
- OpenAI 兼容：`dashscope.aliyuncs.com/compatible-mode/v1`；Batch 半价、节省计划 8 折。
- 佐证：SuperCLUE 2026-07 评测（8 月 6 日发布）qwen3.8-max 以 71.48 分列国产综合第一。来源：[qwen-models]、[qwen-text]、[qwen-38max]、[qwen-free]、[qwen-search]、[superclue]。

### 2.7 月之暗面 Kimi（一手：platform.kimi.com 定价页直查）

| 模型 | 输入（未命中/命中） | 输出（¥/M） | 上下文 |
|---|---|---|---|
| kimi-k3 | 20 / 2 | **100** | 1,048,576 |
| kimi-k2.7-code | 6.5 / 1.3 | 27 | 262,144 |
| kimi-k2.6 | 6.5 / 1.1 | 27 | 262,144 |

- K3 缓存写入收费（5 分钟 TTL ¥20/M、1 小时 TTL ¥40/M）；**输出 ¥100/M 是全部候选中最贵**（GLM-5.3 的 3.6 倍）。
- 官方工具：联网搜索 web-search 按次收费（第三方口径 $0.005/次，结果 token 另计入输入）；其余工具限时免费。
- OpenAI 兼容：`api.moonshot.cn/v1`（另兼容 Anthropic 协议）。kimi-k2.7-code 在百炼表中"结构化输出：No"，k3 需真机验证 json_schema。
- 免费档：无（文件类接口限时免费）。36 氪报道《高价的 Kimi K3，缺钱的月之暗面》佐证其定价策略。来源：[kimi-pricing]、[kimi-tools]、[kimi-36kr]。

### 2.8 字节豆包 / 火山方舟（二手为主：价格页 JS 渲染无法直查，经官方文章+媒体交叉）

现行旗舰 **Doubao-Seed-2.1-pro**（2026-06-23 FORCE 大会发布，2026-09-16 更新 0915 版并全量上线 API）：

| 模型 | 输入/输出（¥/M） | 缓存读 | 备注 |
|---|---|---|---|
| Doubao-Seed-2.1-pro | 6 / 30 | 1.2（写 ¥0.017/M/时） | 旗舰；官方称综合成本较 Claude Opus 4.6 降近 80% |
| Doubao-Seed-2.1-turbo | 3 / 15 | ~0.6 | 高频场景档 |
| Doubao-Seed-2.0-pro | 3.2 / 16（≤32K 分段） | — | 上代旗舰 |
| Doubao-Seed-2.0-mini | ~0.2 / ~2（第三方 EvoLink 估，¥口径未官方直查） | — | 低价档 |

- 上下文 256K 级（沿用 1.6 代口径，2.x 模型列表页可核，未直查到单值）。
- 联网内容插件 WebSearch：`plugins:[{type:"web_search"}]`，按次计费（现价未查到确切数字，按历史口径 ~¥0.04/次估算）；搜索结果 token 计入输入，单次提问 token 消耗易过万。
- 免费额度：官方营销文章称 Doubao-Seed-2.1-pro（编程场景）每日 500 万 token 促销额度（2026 营销活动口径，非长期承诺）。
- OpenAI 兼容：`ark.cn-beijing.volces.com/api/v3`。来源：[ark-21pro]、[ark-pricing]、[ark-models]、[ark-websearch]。

### 2.9 MiniMax（一手：官方 pay-as-you-go 定价页直查）

| 模型 | 输入/输出（$/M） | 缓存读 | 上下文 |
|---|---|---|---|
| MiniMax-M3（≤512K 输入） | **0.30 / 1.20**（永久五折，原 0.60/2.40） | 0.06 | 分档 ≥512K |
| MiniMax-M3（>512K 输入） | 0.60 / 2.40 | 0.12 | 同上 |
| MiniMax-M2.7（legacy） | 0.30 / 1.20 | 0.06 | — |

- 单模型策略：M3 同时充当旗舰与轻量（百炼侧列表上下文 192K、结构化输出标 No，原生 API 支持 JSON 输出需验证）。
- 无内置 web search（外接 Serper/Tavily）；无免费档；另有 Priority 档（1.5 倍价换优先排队）。
- OpenAI 兼容：有（海外 platform.minimax.io / 国内 api.minimaxi.com）。来源：[mm-pricing]、[qwen-models]。

### 2.10 xAI Grok（一手：docs.x.ai 模型/定价页直查；价格表为 JS 渲染，数字经官方页导语 + 聚合交叉）

现行旗舰 **Grok 4.6**（2026-08 下旬发布，Grok 4.5 的后训练刷新版，官方导语称"最智能且最快的模型，除专项外一切场景首选"）；另有 beta 线 grok-4.20 与中档 4.3。

| 模型 | 输入/输出（$/M） | 缓存命中 | 上下文 | 备注 |
|---|---|---|---|---|
| Grok 4.6（grok-4.6） | 2 / 6 | $0.50 | 500K | >200K 提示词翻倍至 $4/$12（二手口径）；Agent 模式 $0.08/分钟 |
| Grok 4.5 | 2 / 6 | — | — | 上代旗舰，同价 |
| Grok 4.3 | 1.25 / 2.50 | $0.20 | — | 中档 |
| Grok 4.1 Fast | 0.20 / 0.50 | — | 2M（长提示词涨至 $1.25/$2.50） | 轻量档；输出上限 ~30K；多模态 |

- **Web Search / X Search（服务端工具）**：按**来源（source）计费**而非按次——Web Search $0.025/来源（单源封顶即 $0.025/次）；Web+X+News+RSS 多源最高 $0.10/次（4 源）；搜索内容 token 另按模型单价计。对照：GLM search_std ¥0.01/次 ≈ $0.0014/次，Grok 单源搜索贵 ~18 倍。
- **X 独占价值（2026-09-19 用户指出，评估补记）**：X Search 直查帖子与趋势，是其余 9 家均无的源。对 goalx 落点一强一弱：**强 = 伤停/阵容新闻首发**——英文记者圈（Romano/Ornstein 类）在 X 首发，且 X 对外部抓取封闭、通用网页搜索（含 GLM web_search）存在数小时级盲区（结构性判断，未实测），正中 scout 伤停双路预案；**弱 = 趋势/舆情**——票 01 已裁中文舆情不进管道，且中文舆情主场在微博/虎扑/懂球帝不在 X，X trends 对竞彩几乎无映射。低频兜底口径成本可承受：彩池 14 场×2 侧×8 期 ≈ 224 次/月 × $0.025 ≈ **$5.6/月**——与"全量搜索走 Grok（2000 次/月 $50）不可承受"的结论不冲突，前者才是 X 的正确用法。
- 结构化输出（Structured Outputs）与 function calling（含并行工具调用）：官方支持；`api.x.ai/v1` OpenAI 兼容端点，换 base_url 即用。
- 免费档：无长期免费模型，新户一次性 $25 赠金——不适合持续冒烟。
- 美元计费、海外平台。来源：[xai-models]、[xai-websearch]、[xai-pricing]、[xai-fast]。

## 3. 能力横评（榜单数据，注明榜单名与查询日期 2026-09-19）

### 3.1 Arena.ai（原 LMArena）文本总榜 — 快照 2026-09-13，8,146,274 票 / 402 模型

| 排名 | 模型 | Elo | 备注 |
|---|---|---|---|
| 1 | claude-fable-5-high | 1506±5 | Anthropic |
| 9 | gemini-3.8-flash-high | 1493±9 | Google（**Flash 档进入前十**） |
| 15 | gemini-3.1-pro-preview | 1487±3 | Google Pro 旗舰 |
| 17 | kimi-k3-max | 1485±5 | 国产最高 |
| 19 | glm-5.3-max | 1483±6 | GLM 旗舰（max 思考档） |
| 22 | qwen3.8-max | 1481±6 | 阿里旗舰 |
| 29 | glm-5.3-flash | 1475 | GLM 轻量 |
| ~30 | grok-4.20-beta1 | ~1474 | xAI beta（Grok 4.6 发布仅 3 周，未入前 15，样本少） |
| ~50 | deepseek-v4-pro-high | 1463±7 | DeepSeek 旗舰 |

要点：GLM-5.3 与 Kimi K3、Qwen3.8-max 同处 1481-1485 国产第一梯队（差距 ±2 Elo，无显著分差）；距榜首 Fable 5 约 -23 Elo；距 Gemini 3.1 Pro -4 Elo。轻量档：Gemini 3.8 Flash（1493）显著高于 GLM-5.3-Flash（1475，+18 Elo）。Grok 4.6 在本快照无足量样本（仅有 grok-4.20-beta1 #30），arena 证据薄。来源：[arena]。

### 3.2 Artificial Analysis Intelligence Index（v4.2，2026-09-04 发布；09-07 升级 v4.3）

- v4.2 榜首 **Claude Fable 5.1**，次席 **GPT-6 Astra**（较 v4.1 +4 分；第三方引 max effort 分 61.1）；
- **Grok 4.6 得 61 分，与 GPT-5.6 Sol 并列**，且在 GDPval-AA v2、CursorBench、FrontierCode、APEX-Agents 等**智能体子项反超 Sol**（felloai 2026-09-05 引，8 月黑马：较 Grok 4.5 +5 分而单价不变）——对 goalx analyst 这类"智能体+工具链"画像的相关性高于其 arena 排位；
- 国产线：GLM-5.3、Kimi K3、DeepSeek V4、Qwen3.8-max 在榜（官网 JS 渲染未能直取各分值，趋势与 Arena 一致：国产旗舰位于海外旗舰之后、彼此接近）；
- HN 讨论提示：跨版本分数不可直接比较（v4.2 换入 AA-Briefcase/GDPval-AA、移除 GPQA Diamond）。来源：[aa-idx]。

### 3.3 工程类基准（SWE-bench Pro / Terminal-Bench，2026-09-19 查询）

- **SWE-bench Pro**：GPT-6 Astra 58.2%、Claude Fable 5.1 57.9%、**GLM-5.3 41.8%（开源权重第一）**——GLM 与海外旗舰在重工程任务上差距约 16 pt（codingfleet 汇总榜）。
- Terminal-Bench 3.0：Claude Fable 5 39.5% vs GLM-5.3 34.5%（max effort，厂商口径）；Z.ai Code Bench（High）：GLM-5.3 31.4% vs Claude Opus 4.8 29.5%（厂商自报，注意偏置）。
- tau²-bench（工具使用）已饱和（GLM-5.2 99.1%、Haiku ~96%），不具区分度，弃用。来源：[swe-codingfleet]、[edenai]。

### 3.4 中文能力佐证

- **SuperCLUE**（2026-07 评测，2026-08-06 发布）：qwen3.8-max 71.48 分列国产综合第一；2026-04 月报中 Doubao-Seed-2.0-pro、Qwen3.6-Plus、文心 5.0、GLM-5.1 同破 70 分列第一梯队（GLM-5.3 发布于 8-14，未入 7 月样本；8 月榜单未查到）。
- 结合 Arena 中文场常态（国产旗舰中文互有胜负、明显强于海外旗舰的中文子项），中文能力不构成 GLM vs Qwen/Doubao/Kimi 的决定性差异；对 OpenAI/Anthropic 则是 GLM/Qwen 的相对优势。来源：[superclue]。

### 3.5 轻量档（对标 GLM-5.3-Flash）可靠性

Arena 佐证：gemini-3.8-flash-high 1493（#9）> glm-5.3-flash 1475（#29）> GPT-5.6 Luna / DeepSeek V4.1-Flash / qwen3.8-flash（未进前排）。qwen3.8-flash 官方表确认 thinking/FC/内置工具/结构化输出全支持；DeepSeek V4.1-Flash 与 Pro 能力清单一致（同 1M/384K/thinking/工具）。轻量档结构化输出可靠性均需真机验证 strict json_schema（与 GLM 同等风险）。

## 4. 成本模拟（三档用量 → "轻量当 scout + 旗舰当 analyst"）

用量（沿用票 02）与月 token 量：

| 参数 | 保守 | 典型 | 激进 |
|---|---|---|---|
| scout 场次/月 × 输入 tok | 412×1k | 712×2k | 1068×3k |
| → scout 输入/输出（M tok） | 0.41 / 0.21 | 1.42 / 0.71 | 3.20 / 1.60 |
| analyst 场次/月 × 输入 tok | 202×5k | 352×10k | 618×15k |
| → analyst 输入/输出（M tok） | 1.01 / 0.51 | 3.52 / 1.76 | 9.27 / 4.64 |
| web search 次/月 | 500 | 2000 | 4000 |

公式：月成本 = scout(入×Pin + 出×Pout) + analyst(同) + search 单价×次数。GLM 行为票 02 基线原数。

| 组合（scout + analyst + search） | 保守 | **典型** | 激进 | 典型 $ |
|---|---|---|---|---|
| **GLM：5.3-Flash + 5.3 + 内置 std（基线）** | ¥28 | **¥101** | ¥251 | $14 |
| OpenAI：Luna + GPT-5.6 Sol + $0.01/次 | ¥140 | ¥507 | ¥1241 | $70 |
| OpenAI：Luna + GPT-6 Astra + $0.01/次 | ¥293 | ¥1039 | ¥2642 | $144 |
| Anthropic：Haiku 4.5 + Opus 5 + $0.01/次 | ¥174 | ¥623 | ¥1536 | $87 |
| **Gemini：3.8 Flash + 3.1 Pro + grounding（≤5k 免费）** | ¥66 | **¥230** | ¥595 | $32 |
| **DeepSeek：V4.1-Flash + V4-Pro + Serper $0.001/次** | ¥30 | **¥107** | ¥270 | $15 |
| Qwen：3.8-flash + 3.8-max + 内置 ~¥0.03/次 | ¥46 | ¥170 | ¥407 | $24 |
| Qwen：3.8-flash + 3.7-plus + 内置（降级参照） | ¥22 | ¥85 | ¥185 | $12 |
| Kimi：k2.6 + k3 + $0.005/次 | ¥97 | ¥347 | ¥857 | $48 |
| 豆包：2.0-mini(估) + 2.1-pro + 插件 ~¥0.04/次 | ¥42 | ¥156 | ¥358 | $22 |
| MiniMax：M3 + M3 + Serper（单模型） | ¥13 | ¥46 | ¥110 | $6.4 |
| **Grok：4.1 Fast + 4.6 + 内置 Web Search $0.025/次（单源）** | ¥128 | **¥491** | ¥1064 | $68 |
| Grok：4.1 Fast + 4.6 + 外接 Serper $0.001/次 | ¥42 | ¥146 | ¥373 | $20 |

关键观察：

1. **预算内（典型 ≤¥360/$50）的组合**：GLM、Gemini、DeepSeek、Qwen（两种）、豆包、MiniMax，以及"Grok + 外接 Serper"混合口径（¥146）；OpenAI/Anthropic/Kimi 典型档均超或贴线（Kimi ¥347 贴线但激进档爆炸）；**Grok 用自家内置搜索则典型 ¥491 超预算——按 source 计费的搜索费（$50/月@2000 次）占其总成本 73%，是全场最贵的搜索**。
2. **analyst 单价决定一切**（占总成本 80-95% 不变，Grok 除外：其内置搜索口径下搜索费反超模型费）。按"每 M 输入的含输出折合价"（Pin + 0.5×Pout）：豆包 2.1-pro ¥21 ≈ **GLM-5.3 ¥22（最低之一）** < DeepSeek ¥24 < Qwen3.8-max ¥30 < Grok 4.6 ¥36（$5） < Gemini 3.1 Pro ¥58（$8） < Kimi K3 ¥70 < GPT-5.6 Sol ¥101 < Opus 5 ¥126 < Fable/Astra ¥252。
3. Gemini 是唯一"双档能力都高于 GLM 对应档（1493>1475、1487>1483）且典型档预算内（¥230）"的组合；激进档 ¥595 超预算，且促销价 2026-12-31 到期（3.8 Flash 翻倍后典型档约 +¥40）。
4. DeepSeek 总成本与 GLM 几乎持平（¥107 vs ¥101）但旗舰能力低一档（Arena -20 Elo）——"更便宜"不成立；其错峰半价只对可延迟批处理有意义，goalx 的赛前情报有时效性。
5. 换 analyst 单点敏感性（保持 GLM scout+search）：Gemini 3.1 Pro 典型 ¥226（预算内）、Grok 4.6 典型 ¥150（预算内，$2/$6 的 analyst 折合价 ¥36/M 输入，仅次于国产三强）；GPT-5.6 Sol ¥378、Opus 5 ¥467、Fable 5.1 ¥910（均超 ¥360）。

## 5. 结论

**Q1：GLM 双档是否仍在能力-价格前沿最优？**
基本成立，且在"CNY 直连 + OpenAI 兼容 + 内置搜索"约束下最优。被超越的维度与差距：①arena 总榜被 Fable 5（1506，+23 Elo）与 Gemini 3.8 Flash（1493，+18 vs GLM-Flash）超越，但对应组合典型档 ¥623-1039 或 ¥230（Gemini，超 GLM 2.3 倍）；②重工程基准被 Astra/Fable 拉开 16 pt（SWE-bench Pro 41.8 vs 58）——与 goalx 分析师任务（结构化证据摘要而非代码工程）相关性弱；③中文：SuperCLUE 2026-07 Qwen3.8-max 71.48 居首（GLM-5.3 未入样）。

**Q2：能力明显更强且预算内的替代组合？**
两个 B 方案：① **Gemini（3.8 Flash scout + 3.1 Pro analyst + grounding 免费搜索）**：典型 ¥230/$32 预算内、双档 arena 均压 GLM 对应档；代价：激进档 ¥595 超预算、促销价年底到期、大陆访问+美元计费。② **Grok 4.6 当 analyst**（保持 GLM scout+搜索或外接 Serper，典型 ¥150-¥226 区间）：AA v4.2 得 61 与 GPT-5.6 Sol 并列、智能体子项反超 Sol；但全量搜索走 Grok 按 source 计费不可承受（典型 $50/月），须外接 Serper，且 arena 样本薄、无免费档、美元计费。②的轻量变体：**只把 Grok 当 X 独占情报的低频补充源**（伤停首发兜底 ≈224 次/月 $5.6/月，见 §2.10），主力架构不动——若票 05 评测证明伤停时效性有增量价值，这是最先值得试的 Grok 用法。两者均不建议直接切换，作记录。

**Q3：同能力更便宜的替代？**
没有。arena 同梯队（±5 Elo）的 kimi-k3-max（1485）与 qwen3.8-max（1481）折合 analyst 单价分别是 GLM 的 3.2 倍（¥70 vs ¥22/M 输入折合）与 1.4 倍（¥30）；DeepSeek V4-Pro（¥24 折合）与 GLM 持平但能力 -20 Elo；AA 口径与 Sol 并列的 Grok 4.6 折合 ¥36（1.6 倍）；Qwen plus 档/MiniMax 更便宜但能力低半档。**GLM-5.3 是 arena ≥1480 模型里最便宜的**。

**Q4：免费兜底档谁最强？**
**GLM-4.7-Flash 仍最优**：免费、200K 上下文、FC+json_schema、无限期（仅并发限制），是唯一适合持续日跑冒烟的档。Gemini 3.x Flash 免费档能力更强（arena 1493）但 10 RPM/1500 RPD + 大陆访问限制，只适合偶发验证；Qwen 每模型 100 万 token（90 天一次性）；豆包 2.1-pro 每日 500 万 token 属编程场景促销（口径不稳）；OpenAI/Anthropic/DeepSeek/Kimi/MiniMax/Grok 无免费档（Grok 仅 $25 新户一次性赠金）。

**Q5：最终建议：维持 GLM，不调整。**
- scout = GLM-5.3-Flash、analyst = GLM-5.3、兜底/开发 = GLM-4.7-Flash（免费）、web search = search_std（¥0.01/次），与票 02 裁决一致；
- 若未来 analyst 质量成为瓶颈且接受海外访问，唯一升级路径是 analyst 切 Gemini 3.1 Pro（典型 ¥226 仍预算内），届时再做单独评审；
- 若未来 analyst 质量成为瓶颈且接受海外访问，升级路径有二：analyst 切 Gemini 3.1 Pro（典型 ¥226 预算内）或切 Grok 4.6 + 外接 Serper 搜索（典型 ¥150 预算内，智能体子项强但 arena 证据薄），届时再单独评审；
- 复核节点：2026-10-05 OpenAI 调价、2026-12-31 Gemini 3.8 Flash 促销到期、Grok 4.6 arena 样本充实后、SuperCLUE 8 月榜（GLM-5.3 首个中文样本）发布后复查一次本章结论。

## 6. 时效性与置信度注记

- **一手直查**：Anthropic、Gemini（页标 09-16 更新）、DeepSeek（两次一致）、Kimi、MiniMax、Qwen 主要定价、GLM（票 02）。
- **二手交叉**：OpenAI（官方页快照滞后至 GPT-5.2 时代，GPT-6/5.6 价格经公告+聚合一致确认）、豆包（价格页 JS 渲染，2.1-pro ¥6/¥30 经官方文章+多媒体一致）、**xAI Grok（docs.x.ai 价格表 JS 渲染无法直取，4.6/4.1 Fast 价格经官方页导语 + x.ai/API 页 + 聚合一致确认；">200K 提示词翻倍"为二手口径）**、qwen3.7-plus、Haiku 4.5、豆包 mini、Qwen/Kimi/豆包/Grok 搜索按次/按源价。
- Gemini 3.8/3.7 Flash 促销价 2026-12-31 截止；OpenAI 2026-10-05 起调价；GPT-6 Astra 分阶段放量中（API 可用性以账户为准）。
- 榜单：Arena 快照 2026-09-13、AA Index v4.2（09-04）/v4.3（09-07）、SWE-bench Pro 汇总（2026-09-19 查询）、SuperCLUE 2026-07（08-06 发布）——榜单随时间漂移，GLM-5.3（08-14 发布）在部分榜单样本滞后。

## 来源

- [bm-pricing] 智谱价格页 — <https://docs.bigmodel.cn/cn/guide/start/pricing>（票 02 一手复用）
- [oai-56] GPT-5.6 发布与 07-30 降价 — <https://openai.com/index/gpt-5-6/>
- [oai-astra] GPT-6 Astra 发布（2026-09） — <https://openai.com/index/gpt-6-astra/>
- [oai-pricing-snapshot] OpenAI pricing（快照滞后，仅作 web search $10/1k 与缓存价佐证） — <https://platform.openai.com/docs/pricing>
- [oai-search] OpenAI web search 计费（developers.openai.com pricing，经 CloudZero/Parallel 汇总） — <https://developers.openai.com/pricing>
- [aa-pricing] Anthropic pricing（官方直查） — <https://platform.claude.com/docs/en/about-claude/pricing>
- [aa-models] Anthropic models overview（官方直查） — <https://platform.claude.com/docs/en/about-claude/models/overview>
- [aa-fable] Claude Fable 5 发布（2026-06-09） — <https://www.anthropic.com/news/claude-fable-5>
- [gem-pricing] Gemini API pricing（官方，页标 2026-09-16） — <https://ai.google.dev/gemini-api/docs/pricing>
- [gem-limits] Gemini rate limits（免费档 RPM/RPD） — <https://ai.google.dev/gemini-api/docs/rate-limits>
- [ds-pricing] DeepSeek models & pricing（官方直查两次） — <https://api-docs.deepseek.com/quick_start/pricing>
- [ds-v4] DeepSeek-V4-Pro 正式版上线 + Responses API 公告 — <https://api-docs.deepseek.com>（changelog 2026-08-13）
- [qwen-models] 百炼模型列表（能力矩阵官方表） — <https://help.aliyun.com/zh/model-studio/models>
- [qwen-text] 百炼文本生成推荐表 — <https://help.aliyun.com/zh/model-studio/text-generation-model>
- [qwen-38max] qwen3.8-max 模型信息（¥12/¥36，2026-09-10） — <https://help.aliyun.com/zh/model-studio/qwen3.8-max>
- [qwen-free] 新人免费额度规则 — <https://help.aliyun.com/zh/model-studio/>（新人免费额度获取及用完即停开关规则）
- [qwen-search] 联网搜索 WebSearch 计费说明 — <https://help.aliyun.com/zh/model-studio/>（联网搜索WebSearch计费说明）
- [kimi-pricing] Kimi 开放平台定价 — <https://platform.kimi.com/pricing>
- [kimi-tools] Kimi 官方工具（web-search 按次收费） — <https://platform.kimi.com>（如何在 Kimi API 中使用官方工具）
- [kimi-36kr] 高价的 Kimi K3，缺钱的月之暗面 — <https://m.36kr.com>（2026-07）
- [ark-21pro] Doubao-Seed-2.1-pro 收费标准 — <https://www.volcengine.com/article/2605348>（2026-08-19）；发布报道 <https://m.36kr.com>（2026-06-23）
- [ark-pricing] 火山方舟模型价格 — <https://www.volcengine.com/docs/82379/1544106>
- [ark-models] 火山方舟模型列表 — <https://www.volcengine.com/docs/82379/1799865>
- [ark-websearch] 方舟 Web Search 联网内容插件 — <https://docs.volcengine.com/docs/82379/1330310>
- [mm-pricing] MiniMax pay-as-you-go — <https://platform.minimax.io/docs/guides/pricing-paygo>
- [xai-models] Grok models & pricing（官方，2026-08-21 发布） — <https://docs.x.ai/docs/models>
- [xai-websearch] xAI Web Search / X Search（按 source 计费） — <https://docs.x.ai/docs/guides/web-search>
- [xai-pricing] x.ai API 定价导语（grok-4.6 $2/$6） — <https://x.ai/api>
- [xai-fast] Grok 4.1 Fast 定价与长上下文分档 — <https://cloudprice.net>（聚合口径）；OpenRouter 佐证 <https://openrouter.ai>
- [xai-felloai] Grok 4.6 AA Intelligence Index 61（2026-09-05） — <https://felloai.com>
- [arena] Arena.ai（LMArena）text overall 快照 2026-09-13 — <https://arena.ai/leaderboard/text/overall>
- [aa-idx] Artificial Analysis Intelligence Index v4.2/v4.3 — <https://artificialanalysis.ai>（v4.2 公告 2026-09-04）
- [swe-codingfleet] SWE-bench Pro 汇总榜 — <https://codingfleet.com>（2026-09-19 查询）
- [edenai] GLM-5.3 vs GPT-5.6 Sol / Fable 5 / Gemini 3.1 Pro 基准对比 — <https://www.edenai.co/post/glm-5-3-benchmark-vs-gpt-5-6-sol-claude-fable-5-gemini-3-1-pro>（2026-08-14）
- [superclue] SuperCLUE 中文测评 — <https://www.superclueai.com>（2026-07 评测，08-06 发布）
- [tavily] Tavily pricing（$0.008/credit，免费 1000/月） — <https://www.tavily.com/pricing>
- [serper] Serper pricing（$1/1000 次，注册赠 2500） — <https://serper.dev>
