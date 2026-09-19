# 06 证据卡产品原型：彩池页 + 场次详情

Type: prototype
Status: resolved (2026-09-19)
Blocked by: 03

## Question

双消费场景的产品形态长什么样？用户已裁决：彩池页"AI 证据总结"coming-soon 块转正 + 场次详情双场景。做低保真原型供用户反应：

1. **彩池页证据卡**：期次页按场次展示——伤停/近况/共识 vs 模型分歧点、情报来源与时点标注（诚实降级：无情报时显示什么）。
2. **场次详情**：完整证据链（结构化基本面 + scout 情报 + analyst 复核结论 + 融合概率对照 ML 概率）。挂现有 fixture 页还是新路由，原型给两个方案。**交互面（票 03 定形：AG-UI 协议）**：详情页"追问 analyst"对话原型——后端 FastAPI + `ag-ui-protocol` v1.0.0 事件流；前端 **A/B 两案对比**：A=ai-sdk useChat + 自定义 transport 包 `@ag-ui/client`（保留 ai-sdk 决策），B=`@assistant-ui/react-ag-ui`（成熟聊天 UI 原生适配）；AI Elements 组件按 shadcn copy-in 引入。
3. **API 契约草案**：两个场景各需要什么端点形状（供票 07 拆票用）。

产出：`prototype/` 下原型（线框图或 HTML/React stub）+ 本票 Answer 记录用户反馈。

## 不变量与人裁决项

- 诚实降级样式（无数据不装懂）与用户确认由用户裁决。
- 消费场景范围已定（彩池页+场次详情），不加第三场景。

## Answer（2026-09-19 HITL prototype 会话，四项全按推荐）

原型：[prototype/evidence-ui.html](../prototype/evidence-ui.html)（四变体可交互，渲染无 JS 错误）；API 草案：[prototype/api-contract-draft.md](../prototype/api-contract-draft.md)。

1. **证据卡形态（V1 照单）**：展开式卡片 + 每条情报来源/时点徽章 + 卡底 scout/analyst/Fused 状态行 + 无情报诚实降级（明说不出概率，仅展示官方份额，不装懂）。
2. **详情挂靠（V2 方案 A）**：证据链区块挂现有 fixture-research 页底部（三轨概率对照 + JS 徽章 + 复核结论 + 追问入口）；独立路由（V3）否决；盲评入口作区块内链接后补。
3. **前端（A 案）**：ai-sdk useChat + 自定义 transport 包 `@ag-ui/client`（AI Elements 组件照用）；assistant-ui 否决（避免第二 UI 体系）。V4 产品形态定案：流式逐字 + 只引已存证情报 + 末尾引用徽章 + 明说证据弱点。
4. **API 草案照此拆票**：五端点（彩池摘要/场次证据链/AG-UI ask/复核队列提交/验证三列），正式契约走仓库 contract-first。
