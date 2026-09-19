# 08 GLM 基座（客户端+记账+熔断）

Status: ready-for-agent
Blocked by: （无）

## 目标

`llm/` 领域包骨架 + GLM 接入 + 成本纪律，为 scout/analyst/追问供能。

- config：`GLM_API_KEY`、`GLM_SCOUT_MODEL=glm-5.3-flash`、`GLM_ANALYST_MODEL=glm-5.3`、`GLM_FALLBACK_MODEL=glm-4.7-flash`、月预算（¥360 硬上限）
- openai-agents 接 GLM：`OpenAIChatCompletionsModel` + base_url `https://open.bigmodel.cn/api/paas/v4/` + `set_tracing_disabled(True)`（票 02 结论，无 /responses API）
- 每次 LLM 调用记 `cost_ledger`（模型、tokens、费用、用途标签）
- 三级熔断：月预算 60%→analyst 降 Flash；80%→仅当期彩池场；95%→全切免费档 4.7-Flash
- 开发期默认免费档（4.7-Flash），真 key 由用户按票 02 清单开通后注入 .env

## 验收

task check 通过；记账/熔断三档有单测；key 不出现在任何日志与测试快照。

## 不变量与人裁决项

- 月预算硬上限 ¥360（$50），预充值即上限；超限抛错不静默。
- key 只入 .env（gitignore 已确认）；换模型须改配置不改代码（票 02：勿硬编码型号）。

## Comments

- **2026-09-19 key 实测定案**：用户 key 已落 `.env`（`GLM_API_KEY`）；正式 base_url `https://open.bigmodel.cn/api/paas/v4`（`/api/v1` 403 勿用）；5.3-Flash 与 5.3 实调通过（账号有余额）；4.7-Flash 晚高峰 429 拥挤——客户端须带重试退避。**5.3-Flash 是推理模型**：思考走 `reasoning_content` 且先耗 token，scout 调用 `max_tokens` 要给足余量（小预算会截出空 content）。
- **端面定案（2026-09-19 SDK 实测补充）**：key 为 Coding Plan 订阅 key。默认 base_url=`https://open.bigmodel.cn/api/coding/paas/v4`（订阅额度内零边际成本，`.env` GLM_BASE_URL 已配）；配额耗尽/端面报错降级 `https://open.bigmodel.cn/api/paas/v4`（按量计费，票 02 预算表适用）——熔断链最前一级。`/api/v1` 对本 key 所有模型 model_access_denied，弃用。openai SDK（openai-agents 底层）双端面实测通过。
