# Wayfinder Map: Goalx 目标对齐与最小下一阶段

Label: wayfinder:map

## Destination

基于现有盈利目标与本次审视，确定下一阶段最小可验证范围、可信度标准及暂缓边界；不重做整个项目，不执行产品实现。

## Notes

- 目标沿用[已确认设计](../goalx-quant/spec.md)：个人本地、人工下单、长期盈利导向、有限数据预算、先纸面验证。
- 用户要求：专业详细审视符合目标、待优化与无必要投入，避免过度设计。
- 使用 wayfinder、grilling、domain-modeling；只在需要外部事实时用research。
- 本轮事实和建议见[目标、研究、设计与实现审视](report.md)。报告不是用户裁决，不自动更新既有spec/ADR。
- 2026-09-13用户随后要求按五票方案完善计划；当前实施入口为[Spec v1.1](../goalx-quant/spec.md)。报告保留审视时事实，新计划不代表修复已落地。
- 决策票按本地tracker的Status和Blocked by查询；HITL项保持待讨论，不以代理意见代替用户回答。

## Decisions so far

- [下一阶段优先可信纸面闭环与陈盘验证](issues/02-next-milestone.md)：用户授权按五票方案更新计划，原目标与技术栈保留，纠偏与运行验收优先。

- 同一决议的[合并后交接补充](issues/02-next-milestone.md)：结算修复已合并，剩余报价→验证→页面→运行；工程边界和执行入口集中在[实施安排](../trusted-paper-handoff.md)。

## Not yet specified

本轮路线已明确。具体验收比赛/窗口按当前spec和运行票，在执行时依据已有覆盖与预算冻结；不再作为本地图的开放决策。

## Out of scope

- 本轮不修改产品代码、部署或回测结果；原答案保留，未完成验收已重挂。
- 不建设多用户、自动下单、分布式平台，不重做技术栈选型。
- [进一步真钱放行的统计判定](issues/01-validation-boundary.md)：已知纠偏纳入当前计划，剩余判断依赖长期样本，未来另议；当前不放行。
- [LLM与奖池的具体重启裁决](issues/03-expansion-gates.md)：需运行证据后另开工作，本轮仅明确暂缓与粗条件。
- 无信号后是否改变产品方向，由运行报告与用户后续决定，不提前回答。
