# 19: 验证页重设计落地

**What to build:** 票 09 定稿的验证页形态：手写内联 SVG 曲线换 ECharts（滚动/累计双线、条件 markline、缩放/tooltip）、指标分层（首屏结论 vs 深度下钻）、弱类型指标（clv/forward 字典）的产品化呈现。

**Blocked by:** 09（设计定稿），13（导航基座）

**Status:** resolved

- [x] 收益曲线用 use-echarts 重写，交互按 09 定稿；option builder 纯函数单测
- [x] 指标分层与首屏结论按 09 定稿；指标卡附判读方向（18 的 tooltip 体系如已上线则接入）
- [x] 弱类型字段（clv/forward 字典）不再裸字典直出，按 09 定稿映射呈现
- [x] 三态空状态 + 加载骨架；e2e+axe 零严重违例

## 不变量与人裁决项

- "验证三条件只认前瞻 skill"的口径不得在呈现中弱化或暗示回测 skill 可替代
- 条件达成/进行中的判定逻辑仍以服务端为准，前端只呈现

## Answer

2026-09-19 落地（分支 `feat/ui-19-validation`，commit 76d527a，worktree wt-15）：

1. **首屏** = 状态结论行（`validationVerdict` 纯函数推导：core 条件 x/3 + 还差清单逐字列出，整赛季条件独立显示不计入——与总览 x/3 同口径）+ 三条件卡（达成/进行中沿用服务端 `achieved` 字段，各附判读方向与 CLV/skill 词条 tooltip）+ 前瞻 yield 主图（`yieldCurveOption` 纯函数：滚动 100 注/累计双线 + 0 基准虚线 markline；ECharts 补注册 LegendComponent；沿用 17 号"图表独立子组件、点数 ≥2 才挂载"模式）。
2. **数据源裁决**：`GET /validation/progress` 已内嵌 clv/forward 字典，其中 forward 与 `GET /validation/forward-skill` 是同一份 `forward_skill_report`——前端只请求 progress，不重复拉 forward-skill（零 contract 变更）。
3. **下钻层**折叠 `<details>`：CLV 明细（单关/2串1 × 纸面/真金 beat rate、唯一注分母、距开赛分桶、回归）、前瞻评分明细（纳入规则/覆盖四态/分组 skill）、样本约束表（唯一注为验证分母 + 未购买在途）、条件口径说明、回测 run 表。回测 skill 刻意不上首屏：对比次级区呈现并逐字标注"口径上不算通过线"；`bestForwardSkill` 与服务端判定同构（样本不足组不作通过依据）。
4. **弱类型映射**：`clvMetricRows`/`forwardMetricRows` 已知 key → 带标签指标行（接 18 号 GlossaryTerm tooltip），未知 key → "其他指标" `<details>` 折叠原始 JSON，不裸字典直出、不冒充已解读。
5. **测试**：RTL+MSW 15 用例（结论推导/条件两态/图表挂载与空态/映射折叠/降级重试/骨架）+ 6 个纯函数单测；e2e smoke 双路径断言保留并升级（新增 `validation-verdict` 数据路径标记，`metric-回测 skill`/`validation-error` 契约不变）；axe 九路由零 serious——条件徽章 muted 底灰字 4.34:1 违例按票 14/15 同教训改前景色修复；e2e:loop 纸面闭环 9/9 保持。质量门 lint/type/test:coverage(97.1%)/build 全绿。worktree wt-15 留给 20 号堆叠。
