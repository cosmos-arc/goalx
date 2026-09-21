# 15 AG-UI 追问 analyst（流式交互面）

Status: resolved（合入 main 28c1b95，PR #41）
Blocked by: 08, 14

## 交付（2026-09-20）

- 后端 `POST /api/v1/fixtures/{id}/ask`：请求体 = AG-UI RunAgentInput
  （协议 schema 归 ag-ui-protocol 1.0，不嵌入 OpenAPI——Redocly 过），
  响应 = AG-UI 1.0 事件 SSE；openai-agents Runner 流式调 GLM；
  resolve_ask_model 熔断档位（ASK 用途 80% 档不放行）
- 无情报场次不调模型，诚实降级话术（零成本）；RUN_ERROR 收尾；
  usage 记 cost_ledger（ask 标签）
- 前端 A 案：useChat + @ag-ui/client transport（agui-transport.ts）；
  追问面板挂证据链：流式逐字+引用徽章+弱点声明
- 流式 e2e（Playwright mock SSE 完整序列）+ 后端事件序列/降级/记账单测

## 目标

增量交互面（票 06 V4 定案）。

- 后端：`POST /api/v1/fixtures/{id}/ask` 说 AG-UI 1.0——`ag-ui-protocol` Python SDK（v1.0.0，≥3.9）实现 Agent，内部经 openai-agents 调 GLM，prompt 只注入该场已存证情报
- 前端 A 案：ai-sdk `useChat` + 自定义 transport 包 `@ag-ui/client`（v1.0.0）；AI Elements 组件按 shadcn copy-in 引入，每次 add 后 lint:fix+肉眼 diff
- 产品形态：流式逐字+只引已存证情报+末尾引用徽章（来源+时点）+明说证据弱点
- 每次调用记 cost_ledger（含用途标签 ask）

## 验收

task check；流式 e2e（mock SSE 事件序列）；追问调用成本可见于记账。

## 不变量与人裁决项

- 回答只引已存证情报；无情报场次追问走诚实降级话术。
- 前端框架冻结 A 案（assistant-ui 已否决）；协议冻结 AG-UI 1.0，升版须重裁决。
