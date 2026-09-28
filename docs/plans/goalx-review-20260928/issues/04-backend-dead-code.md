# 04 backend：死类与死流清理

Status: resolved（PR #97，2026-09-28）

## 现状（复验坐实，2026-09-28 @ a35338f）

1. **models.py 死类**（`apps/backend/src/goalx_backend/models.py:51-366`）：
   `Team / Competition / MatchCode / QuoteObservation / SaleStatus / PoolState /
   CostEntry / BankrollEvent / HistMatch` 等——src 与 tests 精确词边界 grep
   均为 0 引用（仅 `*Input`/`*Purpose` 系列被消费）。
2. **死流包装**：`flows.py:225 propline_snapshot_flow`、`flows.py:56
   eu_odds_snapshot_flow`——唯一"调用方"是 flows.py:9 的模块 docstring；
   `tasks.py:113 propline_snapshot` 任务同死（无调度/CLI/测试）。docstring 自认
   2026-09-25 移除后"残留仅供手工调用"，但手工路径不存在。

## 修复

直接删除（git 历史即归档）。propLine 互备语义已由 daily-capture 承担（见
memory/票 50 终局），删除不改变行为。

## 不变量与人裁决项

- 人裁决：models.py 死类是删还是标记 deprecated 留一版？(推荐直接删——
  lean-audit 同口径。)
- 验收：`task check` 绿；grep 零残留引用。

## Comments
