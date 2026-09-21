# 14 证据面 API + 存量渲染 UI

Status: resolved（合入 main 14baff7，PR #41/39）
Blockes by: 10（全量数据需 11/12；可先行契约+mock）

## 交付（2026-09-20）

- 六路由 contract-first（契约同 PR）：`api/evidence.py` 期次证据卡汇总
  /场次证据链/复核队列/结论三分类/盲评提交 + `api/validation.py` M3 评测
  协议报告（三列+两档冻结阈值）；divergences/review_items 读 SQL 归 llm 域
- V1 证据卡转正彩池页（替换 coming-soon）：展开式卡片+来源/时点徽章+
  状态行；no_intel/no_forecast 诚实降级（不出概率仅官方份额）
- V2 证据链挂 fixture-research 底部：三轨对照+JS 徽章+情报时间线+复核
  结论+追问占位+盲评入口；/review 复核页点亮（队列+三分类+盲评二选一）
- 诚实降级 e2e 断言；task check 全门禁过

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
