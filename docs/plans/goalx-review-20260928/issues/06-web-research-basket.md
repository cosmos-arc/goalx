# 06 web：fixture-research-page 第三份篮子收编进 useHadBasket

Status: resolved（PR #99，2026-09-28）

## 现状（复验坐实）

lean-audit 票 04（PR #92）把篮子抽成 `useHadBasket` + BasketBar/BasketDrawer，
但只接到 market-had-page 与 fixtures-page 两页（grep 证实仅此二处 import）。
`apps/web/src/pages/fixture-research-page.tsx` 仍是完整第三份手写实现：

- `pick` 级联 100-125（注释自认"与场次页同规则"）
- `removeLeg:127`、`createBet` mutation:132-152
- 六个篮子 useState:72-77
- 近乎逐字的 BasketBar/BasketDrawer JSX（451-475、477-588）

唯一真差异：`legEv:85-88` 的 EV 计算。

## 修复

`useHadBasket` 加 `legEv` 注入参数（默认现行为），fixture-research 接入，
删手写副本。预期该页净减百余行。

## 不变量与人裁决项

- 行为零变化：加腿/删腿/下注/抽屉全流程与现在一致；`bun run test` + e2e 绿。
- 人裁决：无（legEv 参数化即全部差异）。

## Comments
