# 08 · 指标词典产品化

Type: grilling
Status: resolved
Blocked by: 03

## Question

`.scratch/goalx-quant/glossary.md` 的指标口径词典如何产品化：

1. 形态分工：独立词典页 vs 页内 tooltip/popover（悬停指标名看定义）两者怎么组合
2. 指标卡片结构：定义、判读方向（高了好还是低了好）、数字实例（中文配实例的偏好）、口径注意事项
3. 内容源：词条内容放前端常量/TS 模块，还是后端字典端点——单一事实源如何与 glossary.md 保持同步（该文件验收后要转正 docs/plans，见备忘）
4. 覆盖范围：先覆盖哪些高频指标（EV、CLV、前瞻纳入、Shin 去晦、had 资格…）

## Comments

- 2026-09-16 预裁决（用户）：**首批 10 条按页面落地顺序选**——今日五件套：EV（含"诊断量非机会"警示，最易误读优先级最高）、欧共识 p、资格徽章三态（含"可投≠必成交"）、单固、books；全局三件套：纸面 vs 真金、盈亏与 ROI 口径、前瞻纳入/排除；验证两件套：CLV（正=买在好价）、skill 与前瞻 skill（三条件只认前瞻）。第二批滚动补：Shin 原理、回撤、haircut、closing line 细则，按 tooltip 悬停缺词反馈补。

## Answer

2026-09-17 剩余项定稿（用户"全按推荐"）：

1. **内容源 = TS 常量模块**（`src/lib/glossary.ts`：词条 id/定义/判读方向/数字实例）：纯前端零端点成本、18 号票自包含；词条注释标"口径以 docs/plans 词典为准"（glossary.md 转正后指向它）；未来多端消费再升后端端点。
2. **形态组合**：词典页 = 检索框 + 词条卡列表（定义/判读/实例三要素齐全才上线）；页内 tooltip = 指标名虚线下划线，悬停/聚焦出 Popover（shadcn tooltip，键盘可达）。
