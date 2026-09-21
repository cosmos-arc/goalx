# 11 gate 路由 + analyst 复核

Status: resolved（2026-09-19/20 合入 main，PR #32-38）
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

## Comments（2026-09-20 实施定案）

- ✅ gate：JS 散度（base-2 对称）记 divergences（ml 轨经比分矩阵 had() 推边际，llm 轨直读三项）；>0.02 计分歧、>0.06 且 Tier1 → review_items 入队 + analyst。
- ✅ analyst：GLM-5.3 复核**追加**新 forecasts(track='llm') 行（payload 带 analyst=true + revision_of 指向 scout 行 id），不改写原行；修订后重扫 JS 自然收敛、不再重复路由（收敛语义，测试固化）。
- 真库 e2e（2026-09-20）：fixture 12（莱切vs蒙扎）双轨配对 JS=0.0003 落 divergences（判断一致不路由——正确行为）；analyst 真调用待真实大分歧场次自然触发（单测已全路径覆盖）。
- **数据面发现**：fixture 110（帕尔马vs热那亚）ML 预测 skip `no_mapping`（热那亚→Genoa 别名缺失）——别名缺口待数据面补（挂票 14 前清理）。
