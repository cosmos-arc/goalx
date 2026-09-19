# 14 证据面 API + 存量渲染 UI

Status: ready-for-agent
Blocked by: 10（全量数据需 11/12；可先行契约+mock）

## 目标

按票 06 定案与 prototype/api-contract-draft.md 实施（contract-first 流程）。

- 四个存量端点：pool evidence-summary / fixture evidence / review queue+verdict / 验证报告三列
- V1 证据卡组件：展开式卡片+来源/时点徽章+卡底状态行（scout/analyst/Fused）+诚实降级态（无情报=明说不出概率，仅官方份额）
- V2 证据链区块：挂现有 fixture-research 页底部（三轨概率对照+JS 徽章+复核结论+追问入口占位）
- 盲评入口：区块内链接，双周 10 场匿名二选一

## 验收

task check 全门禁（含 web-lint/type/coverage/e2e）；契约 diff 与代码同 PR；诚实降级态有 e2e 断言。

## 不变量与人裁决项

- 无数据不装懂：所有降级态文案经用户确认（V1 原型已确认形态）。
- 消费场景冻结为彩池页+场次详情，不加第三场景。
