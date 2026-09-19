# 02 GLM 选型与成本预算

Type: research
Status: resolved

## Question

GLM（智谱）线内选型与预算：用户已裁决提供商为 GLM，本票回答选哪个/哪些模型、花多少钱。

1. **2026-09 当前 GLN 模型阵容**：旗舰 vs 轻量（如 flash/air/pro 档位对应型号）、各自定价（每 M tokens 输入/输出）、上下文窗口。
2. **scout/analyst 分档**：scout（高频、全场次、结构化输入为主）用轻量档？analyst（低频、Tier 1、长上下文推理）用旗舰档？给出映射建议。
3. **工具面**：GLM 是否提供 web-search/工具调用 API；openai-agents 框架（ADR 0004）的 OpenAI 兼容接入方式与已知坑。
4. **成本估算**：按真实量算——Tier 1 约 10-30 场/日 + 彩池期次 14 场，scout 每场输入约 1-3k tokens、analyst 每路由场 5-15k tokens，估算月成本区间，对照 ≤$50/月 总预算（当前 The Odds API 免费档，月余量充足）。
5. **预算熔断建议**：超月预算时先停哪条线的降级顺序。

产出：`research/02-glm-selection.md`——模型映射表 + 月预算表（保守/典型/激进三档）+ 用户开 key 的清单（需要哪些 scope/充值建议）。

## 不变量与人裁决项

- 提供商已定 GLM，本票不比较其他提供商。
- 充值额度与最终模型映射由用户裁决。

## Answer

详见 `research/02-glm-selection.md`。结论：

- 现行阵容仅 GLM-5.3（旗舰，1M ctx，¥8/¥28 per M）与 GLM-5.3-Flash（轻量，1M ctx，¥0.8/¥2.8）；5.2/5.1/5-Turbo/4.7 为历史型号会被自动切换，勿硬编码。
- 映射：scout → GLM-5.3-Flash；analyst（Tier1 路由 + 彩池 14 场）→ GLM-5.3；开发与熔断兜底 → GLM-4.7-Flash（免费）。
- 工具面齐全：内置 web_search（¥0.01-0.05/次）、OpenAI 风格 function calling、json_schema 结构化输出；OpenAI 兼容 base_url `https://open.bigmodel.cn/api/paas/v4/`，openai-agents 需 `OpenAIChatCompletionsModel` + `set_tracing_disabled(True)`（无 /responses API）。
- 月成本（bigmodel.cn）：保守 ≈¥28（$3.9）、典型 ≈¥101（$14）、激进 ≈¥251（$35），均在 ≤$50 预算内；z.ai 海外计费激进档 ≈$75 超预算，优先国内平台。
- 熔断：60% 预算 analyst 降 Flash → 80% 仅保彩池场 → 95% 全切免费档；充值额即硬上限。
- 开 key：bigmodel.cn 注册实名 → 控制台建 key（账号级、无 scope）→ 首充 ¥100，月上限 ¥360（$50）。
