# 07 web：market-pool-page 巨组件拆分

Status: resolved（PR #102，2026-09-28 裁决后落地）

## 现状（复验坐实）

`apps/web/src/pages/market-pool-page.tsx` 单组件 821 行：10 个 useState + 3 个
mutation，`periods.length > 0 ?` 守卫 ×8（374/425/449/549/589/685/774/791），
错误处理两处偏离 `errorText` 惯例（225、641）。Divergent Change：期次选择、
注单管理、EV 表、抽屉全挤一个文件一个组件。

## 修复

按 UI 区块拆子组件（期次条 / 注单表 / 抽屉等，切分 agent 定），空态守卫收敛为
单一 early-return 或 EmptyState 组件，错误处理统一走 lib/ui errorText。
优先级低于 06（纯可维护性，无行为风险）。

## 不变量与人裁决项

- 行为零变化；`bun run test` + `web-e2e` 绿。
- 人裁决：无（拆分粒度 agent 定）。

## Comments
