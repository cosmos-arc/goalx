# 02 GLM 选型与成本预算（调研报告）

Ticket: `issues/02-glm-model-and-budget.md` · 调研日期 2026-09-19
提供商已裁决为智谱 GLM（bigmodel.cn 国内 / z.ai 海外），本文不比较其他提供商。

## 1. 2026-09 现行模型阵容与定价（官方定价页直查）

现行主力阵容只有 **GLM-5.3（旗舰）** 与 **GLM-5.3-Flash / FlashX（轻量）**。官方"套餐概览"公告：调用历史模型 GLM-5.2、GLM-5.1 自动切换至 GLM-5.3，调用 GLM-5-Turbo、GLM-4.7 自动切换至 GLM-5.3-Flash（[docs.bigmodel.cn 套餐概览][bm-home]，知乎评测佐证 [zhihu-53]）——**不要在新代码里硬编码 4.x/5.1/5.2 型号名**。

| 模型 | 档位 | 上下文 | 最大输出 | bigmodel.cn 输入/输出（¥/M） | z.ai 输入/输出（$/M） | 缓存命中 |
|---|---|---|---|---|---|---|
| **GLM-5.3** | 旗舰 | 1M | 128K | 8 / 28 | 1.40 / 4.40 | ¥2（$0.26） |
| **GLM-5.3-Flash** | 轻量（多模态） | 1M | 128K | 0.8 / 2.8 | 0.15 / 0.50 | ¥0.23（$0.03） |
| GLM-5.3-FlashX | 轻量高速（~200 tok/s） | 1M | 128K | 2 / 7 | 0.37 / 1.25 | ¥0.57（$0.075） |
| GLM-4.7-Flash | **免费** | 200K | 128K | 0 / 0 | 0 / 0 | 免费 |
| GLM-4-Flash-250414 | **免费** | 128K | 16K | 0 / 0 | — | — |
| GLM-5.1（历史） | 旗舰 | 200K | 128K | 6/24（<32K），8/28（≥32K） | 1.40 / 4.40 | — |
| GLM-4.7（历史） | 中档 | 200K | 128K | 2/8 ~ 4/16 分档 | 0.60 / 2.20 | — |

来源：[bigmodel.cn 价格页][bm-pricing]、[z.ai 价格页][zai-pricing]、[bigmodel.cn 模型总览][bm-models]。缓存存储费当前限时免费。
国内 bigmodel.cn 比 z.ai 海外便宜约 15-25%（按 ¥7.2/$1 折算），Flash 档差距最大（¥0.8 ≈ $0.11 vs $0.15）。

## 2. scout / analyst 分档映射（建议）

| 角色 | 负载画像 | 映射模型 | 理由 |
|---|---|---|---|
| **scout** | 高频、全场次、结构化输入（1-3k tok）、输出短 | **GLM-5.3-Flash** | 输入价仅旗舰 1/10（¥0.8 vs ¥8），输出 1/10（¥2.8 vs ¥28）；1M 上下文余量大；多模态留有余地 |
| **analyst** | 低频、Tier 1 路由场 + 彩池 14 场、5-15k tok 长上下文推理 | **GLM-5.3** | 现行旗舰（2026-08 发布），1M 上下文，支持 thinking 模式（`extra_body={"thinking": ...}`）；缓存命中 ¥2/M 可摊薄固定系统提示词成本 |
| **兜底/开发** | 全流水线冒烟、预算熔断后 | **GLM-4.7-Flash（免费）** | 200K 上下文，零成本；dev/test 全走免费档，生产用量才计费 |

FlashX 不推荐做主力：比 Flash 贵 2.5 倍，scout 吞吐场景才考虑。

## 3. 工具面与 openai-agents 接入

- **Web search**：有，独立 API + 内置工具两种形态。Chat Completions 里 `tools: [{"type": "web_search", ...}]` 内置调用；计费按次：search_std ¥0.01、search_pro ¥0.03、sogou/quark ¥0.05（z.ai 统一 $0.01/次）。另有 MCP server 端点（[web-search 文档][bm-websearch]）。
- **Function calling**：OpenAI 风格 `tools` 数组，`tool_choice` **仅支持 `auto`**（[function calling 文档][bm-funcall]）。结构化输出 `response_format` 支持 `json_object` / `json_schema` 两种模式（[结构化输出文档][bm-structured]）。
- **OpenAI 兼容端点**：官方支持，base_url 国内 `https://open.bigmodel.cn/api/paas/v4/`、海外 `https://api.z.ai/api/paas/v4/`。**只有 chat.completions，没有 /responses API**；temperature 取值开区间 (0,1)（不能传 0）（[OpenAI 兼容文档][bm-openai]）。
- **openai-agents SDK（ADR 0004）官方接入方式**（[SDK models 文档][oai-agents-models]）：

```python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(True)  # 否则 trace 上传 OpenAI 会 401
client = AsyncOpenAI(api_key=..., base_url="https://open.bigmodel.cn/api/paas/v4/")
model = OpenAIChatCompletionsModel(model="glm-5.3", openai_client=client)
scout = Agent(name="scout", instructions=..., model=model)  # analyst 同法可换 model="glm-5.3"
```

  已知坑（官方文档明列）：① SDK 默认走 Responses API，GLM 不支持，必须 `OpenAIChatCompletionsModel` 或 `set_default_openai_api("chat_completions")`；② tracing 默认上传 OpenAI，无 OpenAI key 会 401，先 `set_tracing_disabled(True)`；③ `tool_choice` 只能 `auto`；④ temperature 不能传 0；⑤ 开发期开 `OpenAIProvider(strict_feature_validation=True)` 及早暴露被 Chat Completions 静默丢弃的字段；⑥ 流式 tool-call 不稳时开 `buffer_streamed_tool_calls=True`；⑦ `output_type`（json_schema）GLM 支持，但需真机验证 strict 行为。

## 4. 月成本三档估算（对照 ≤$50/月，按 ¥7.2 = $1）

用量假设：每月 30 天；彩池 14 场/期（保守/典型 8 期、激进 12 期）；scout 覆盖全部场次，输出 = 输入 × 0.5；analyst 路由 = Tier1 的一部分 + 彩池全部场次，输出 = 输入 × 0.5。

| 参数 | 保守 | 典型 | 激进 |
|---|---|---|---|
| Tier1 场次/日 | 10 | 20 | 30 |
| scout 场次/月（含彩池） | 412 | 712 | 1068 |
| scout 输入 tok/场 | 1k | 2k | 3k |
| analyst 路由场/月 | 202（30% + 彩池） | 352（40% + 彩池） | 618（50% + 彩池） |
| analyst 输入 tok/场 | 5k | 10k | 15k |
| web search 次/月 | 500 | 2000 | 4000 |

**bigmodel.cn（国内，推荐计费口径）：**

| 项 | 保守 | 典型 | 激进 |
|---|---|---|---|
| scout @ Flash | ¥0.9 | ¥3.1 | ¥7.1 |
| analyst @ 5.3 | ¥22.2 | ¥77.4 | ¥203.9 |
| search @ std | ¥5 | ¥20 | ¥40 |
| **月合计（¥）** | **≈¥28（$3.9）** | **≈¥101（$14）** | **≈¥251（$35）** |
| $50（¥360）占用 | 8% | 28% | 70% |

**z.ai（海外，$ 计费）**：保守 ≈$8.8、典型 ≈$33、**激进 ≈$75（超预算 1.5 倍）**。结论：能走国内 bigmodel.cn 就走国内；若必须 z.ai，激进档需降 analyst 路由比例。
关键结构性事实：**analyst 占总成本 80-95%**，scout 几乎免费——省钱杠杆全在 analyst 路由率与模型档位。缓存命中（¥2/M）可再省 analyst 固定提示词输入约 1/4-1/3。

## 5. 预算熔断降级顺序（建议）

按预算占用率分级触发（月累计消费，由 ledger 侧计数）：

1. **60%（$30 / ¥215）预警**：Tier1 非彩池场的 analyst 从 GLM-5.3 降级到 GLM-5.3-Flash（成本 1/10）；彩池 14 场保持旗舰。
2. **80%（$40 / ¥290）**：analyst 仅保留彩池场（每期 14 场），Tier1 深度分析暂停，仅 scout 标记。
3. **95%（$47.5 / ¥342）熔断**：全部付费模型停用，scout 切 GLM-4.7-Flash（免费），analyst 挂起，流水线回落 baseline/纯模型路线；通知用户充值。
4. 充值额即硬上限：预充值 ¥360/月，用尽即服务自然止付，杜绝超支；恢复需用户裁决。

## 6. 用户开 key 清单

1. **选平台**：推荐 `bigmodel.cn`（国内实名注册，CNY 充值，Flash 更便宜）；需美元计费才选 `z.ai/model-api`。
2. **注册 + 实名**（国内平台需实名认证）。
3. **建 key**：控制台 → API Keys → 新建。**key 为账号级、无 scope 可配**（一个 key 通吃全部模型与工具），因此务必当秘密保管、走 env var（如 `ZHIPU_API_KEY` / `ZAI_API_KEY`），不进 git。
4. **base_url**：`https://open.bigmodel.cn/api/paas/v4/`（或 `https://api.z.ai/api/paas/v4/`）。
5. **充值建议**：首充 ¥100（≈$14）足够典型月开销 + 余量；月度上限对齐预算 ¥360（$50）。开发/冒烟一律走 GLM-4.7-Flash 免费档，付费额零消耗。
6. **监控**：每周看"财务 → 费用明细"，并设 60%/80% 阈值告警（对应上文熔断档）。

## 来源

- [bm-home] 套餐概览 / 智谱开放文档首页 — <https://docs.bigmodel.cn>（历史模型自动切换公告、免费模型）
- [bm-pricing] 价格页 — <https://docs.bigmodel.cn/cn/guide/start/pricing>（全部 ¥ 定价、search 计费、Batch 半价）
- [bm-models] 模型总览 — <https://docs.bigmodel.cn/cn/guide/start/model-overview>（上下文/最大输出）
- [zai-pricing] Z.ai pricing — <https://docs.z.ai/guides/overview/pricing>（$ 定价、缓存价、Web Search $0.01/次）
- [zai-quickstart] Z.ai quick start — <https://docs.z.ai/guides/overview/quick-start>（海外 base_url、billing、key 创建）
- [bm-websearch] Web search 工具 — <https://docs.bigmodel.cn/cn/guide/tools/web-search>（内置 web_search 工具、按次计价、MCP）
- [bm-funcall] Function calling — <https://docs.bigmodel.cn/cn/guide/capabilities/function-calling>（tool_choice 仅 auto）
- [bm-openai] OpenAI 兼容接入 — <https://docs.bigmodel.cn/cn/guide/develop/openai/introduction.md>（base_url、无 /responses、temperature 开区间）
- [bm-structured] 结构化输出 — docs.bigmodel.cn 能力章节（response_format: json_object / json_schema）
- [oai-agents-models] openai-agents Python — <https://openai.github.io/openai-agents-python/models/>（自定义 base_url 官方模式与坑清单）
- [zhihu-53] GLM-5.3 深度评测（2026-08，佐证自动切换与新旗舰发布）— 知乎专栏
