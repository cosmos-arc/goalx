# 11 gate 路由 + analyst 复核

Status: ready-for-agent
Blocked by: 10

## 目标

分歧路由与大模型复核。

- gate：LLM 三项 vs 最新 ML Forecast 的 JS 散度，记既有 `divergences` 表（票 03 复用定案，归 llm/ 域）
- 阈值沿用旧图：>0.02 记录分歧；>0.06 且 Tier1 → analyst 复核
- analyst = GLM-5.3（thinking）：复核产出**追加**修订 forecasts(track='llm') 新行，不改写 scout 行
- 复核队列赛前入队（票 05）：`review_items` 表归 llm/ 域——route='pre_match'，JS>0.06 Tier1 场次

## 验收

task check；阈值路由单测；复核追加语义单测（原行仍在）。

## 不变量与人裁决项

- 复核只进评测集，不改任何预测工件（旧图纪律）。
- Tier1 定义以票 17 为准（五大+欧冠欧联，大赛临时升入）。
