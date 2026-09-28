# 02 backend：api/pool.py 搏冷策略下沉 betting 域

Status: resolved（PR #96，2026-09-28）

## 现状

`apps/backend/src/goalx_backend/api/pool.py` 内嵌约 280 行业务策略（329 起）：

- `_ticket_view:329`、`_cold_candidates:376`、`_cold_variants:407`（冷门变体生成）
- `build_target_plan:526`（目标金额反向规划：风险档策略 + 单位数学）

AGENTS.md Layout：「api/ 是 delivery」；CONTEXT.md 注额优化口径归 betting/pool
域。对照 `api/results.py:393` 的薄委托是正确姿势。纯函数、无 SQL，下沉无表权属
问题（ADR-0008 不受阻）。

## 修复

策略函数整体迁出 api（建议 betting 或 pool 策略模块，命名 agent 定），api 路由
只留参数解析 + 调用 + 视图组装。顺带处理票 05 的 `api/pool.py:347` 硬编码
0.65（本票文件必动，搭车最省）。

## 不变量与人裁决项

- 行为零变化：迁移是移动不是重写；现有 pool 接口测试全绿即可。
- 人裁决：下沉目标模块放 betting/ 还是 data/pool 侧？（策略=选注决策语义，
  推荐 betting。）
- 验收：api/pool.py 策略函数清零（仅剩路由），`task test` + `task type` 绿。

## Comments
