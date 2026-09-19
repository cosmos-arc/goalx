# 03 双线融合架构设计

Type: grilling
Status: resolved (2026-09-19)
Blocked by: 01, 02

## Question

scout/gate/analyst + LEAP 融合在本仓库怎么落？旧图票 05/09 的结论是方向（拓扑、JS 阈值 0.02/0.06、LEAP log-pool、openai-agents），本票把它落成可实施设计：

0. ~~开场裁决~~ **已裁（2026-09-19 用户）：选项 B 免费起步**。选项 A（API-Football Pro $19/月）留作情报价值证明后的升级项。免费输入面定案：
   - 内部推导（fdhist 2627 在库 + 源D/源B/open-meteo）；
   - football-data.org 免费档（12 项赛事，赛程/赛果/积分）；
   - **伤停/阵容情报双路**：已知目标站点**直爬**（源B 赛事页伤停栏、zhibo8 等可达站——复用现有 ingester+observations 存证模式，零成本、可归档）**+ GLM web_search 兜底**（土超/日职等无结构化场次的泛化路径）——两路配比在本票管线形状里定。
1. **管线形状**：scout（全场次，轻量模型+结构化基本面+情报）→ gate（ML Forecast vs LLM 概率的 JS 散度路由）→ analyst（Tier 1 且分歧大的场次复核）→ LEAP 融合产出。各环节的输入/输出工件、落库表（情报与证据的存证形态——是否复用 quote_observations 的 append-only+哈希模式）、Prefect 调度接入点（新增 deployments 清单）。
2. **交互面运行时（用户已定 ai-sdk；2026-09-19 查证后默认 FastAPI 原生）**：`useChat` 消费的 UI Message Stream Protocol 官方语言无关（[文档](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol)），PyPI 有 `ai-sdk-stream-python`（v6 UIMessageStream 兼容，2026-09 仍在维护）。**默认路径 = FastAPI 端点 + 该包（或薄协议层）**：不加 Node 进程、RUNBOOK 不变、LLM 调用留 Python 侧（openai-agents 调 GLM）、**成本记账天然统一**（复用现有 credit 记账）。会话内需实测该包成熟度（与本仓 ai-sdk 版本的协议版本对齐），不成熟则降级 Node BFF 兜底（出 ADR）。pydantic-ai 的原生协议支持不走（用户已否决该框架，不为协议桥接引入第二 agent 框架）。AI Elements 引入方式在此确认。
3. **概率表示**：LLM 线概率是否走 ADR 0006 比分矩阵 canonical（LLM 出胜负平三项还是直接出比分分布？）。
4. **术语定案**：情报（intel）vs 证据（evidence）vs 证据总结的边界，入 CONTEXT.md。
5. **代码落点**：新领域包（如 `modelling/llm_line/` 或独立域）vs 挂 modelling；ADR-0008 领域包纪律。

HITL 票：与用户逐项裁决，产出设计记录于本票 Answer。

## 不变量与人裁决项

- 融合概率是独立工件，不得覆写 ML Forecast（真钱资格与验证口径另见票 04）。
- 调度新增不得扰动票 37 运行中的既有 deployments。

## Answer（2026-09-19 HITL grilling 会话，四项全按推荐落定）

**管线总图**：直爬源B伤停栏等目标站 + GLM web_search 兜底 → `intel_observations`（存证）→ scout（GLM-5.3-Flash；输入=存证情报+内部推导基本面 fdhist2627 + football-data.org 免费档）→ `llm_forecasts`（三项概率，轨道=LLM）→ gate（JS 散度 vs ML Forecast，记 `divergences`，阈值 0.02/0.06 沿用旧图）→ analyst（GLM-5.3，Tier 1 且 JS>0.06）→ 复核版 llm_forecasts → LEAP 三项层 log-pool 融合 → `fused_forecasts`（轨道=融合）→ 证据卡渲染 + 交互面。

1. **存证工件（新三表+复用 divergences）**：`intel_observations`（append-only+来源 URL+采集时点+采集器标识+raw 哈希，对齐 quote_observations/ADR-0001 模式）；`llm_forecasts`（三项+模型版本+输入情报引用）；`fused_forecasts`（引用 ML Forecast 与 LlmForecast）。`divergences` 空表复用记 JS 散度；`match_intels` 空表废弃（迁移期 drop）。LLM/融合工件绝不覆写 ML Forecast。
2. **交互面运行时（事实修正）**：`ai-sdk-stream-python` 与本仓 Python ≥3.13 硬冲突（包限 `<3.13`）——排除。定案：**FastAPI 内自写薄协议层**（UI Message Stream v6 只实现用到的 part：text/data/finish，约百余行），零新依赖不加进程，成本记账天然统一；Node BFF 兜底保留（需要 tool-streaming 等复杂 part 时再议+ADR）。AI Elements 走 shadcn registry copy-in（票 06 定组件）。
3. **概率表示**：LLM 线只出胜/平/负三项，融合在三项层；ADR 0006 比分矩阵 canonical 限定 ML 线（三项=矩阵投影层）——实施 PR 补 ADR-0009。
4. **术语**：已当场落 CONTEXT.md——MatchIntel 精化为原始素材，新增 IntelObservation/EvidenceSummary；Forecast 的轨道字段（ML/LLM/融合）已覆盖后两张表语义。
5. **代码落点**：新领域包 `intel/`（采集器+三新表 SQL+scout/gate/analyst/融合任务体自持，ADR-0008）；divergences 归 intel/ 域；交付走 api/cli/flows/tasks.py 既有模式。
6. **scout 范围**：Tier 1 + 当期彩池场次（约 30-50 场/日）；调度：scout 跟每日 forecast 后 + 彩池同步后增量，gate→analyst→LEAP 链式；新增 deployments 于实施票定（合入 main 后须重启 serve，票 37 运行不扰动既有 deployments）。

**成本**：订阅全免费；GLM 典型 ¥101/月（票 02），credit 记账复用 cost_ledger 模式。
**遗留到后继票**：直爬站点清单（实施票调研）；复核回流评测集（票 05）；API 契约（票 06）；spec 增补+ADR-0009+拆票（票 07）。

## Addendum（2026-09-19 二次调研后修订，用户推动）

1. **包名改判：`intel/` → `llm/`**（用户裁决）。管线/存证/三表/融合/scout 范围全部不变，仅命名。
2. **交互面协议层改判：自写 Vercel 薄层 → AG-UI 协议**（用户质疑"应有通用协议 SDK"，二次调研证实）：
   - 后端：`ag-ui-protocol` **v1.0.0**（PyPI，2026-09-17 发版，Python ≥3.13 兼容）——开放协议 1.0 正式规格（docs.ag-ui.com：HTTP+SSE 传输绑定、八大事件族、run input），typed events + SSE encoder，FastAPI/ASGI 原生；pydantic-ai（仅依赖 ag-ui-protocol+starlette）与 Microsoft Agent Framework 均此模式；生态适配器覆盖 langgraph/mastra/crewai/llamaindex/langchain/agno/aws-strands/claude。
   - 前端：默认 ai-sdk `useChat` + 自定义 transport 包 `@ag-ui/client` v1.0.0（同天发版）——TS 侧薄事件转换胶水，保留用户 ai-sdk 决策；备选 `@assistant-ui/react-ag-ui`（0.0.60，成熟聊天 UI 库官方适配）；**最终形态票 06 原型 A/B 定**。
   - 出局（留档）：自写 Vercel UI 流协议薄层、vendor `ai-sdk-stream-python`（<3.13 且 11 星个人库）、pydantic-ai 适配器（绑其 agent 运行时，且用户已否决该框架）、Node BFF（用户明确不要）。
   - openai-agents（Python）管 LLM 调用的分工不变；AG-UI 只是 agent↔前端的交互协议层，正交于 LLM 客户端。

> 2026-09-19 后记：票 04 基于实测（forecasts 表已带 track 列且全链路参数化）改判本票"三新表"——预测工件复用 forecasts(track)，情报引用入 payload JSON；新增表仅 intel_observations。以 04 Answer 为准。
