# 20: 资金页重设计落地

**What to build:** 票 09 定稿的资金页形态：余额与流水的可视化升级（形态按 09）、期间成本摘要的信息层级（credits 与人民币分列）、空状态与首次入金引导。

**Blocked by:** 09（设计定稿），13（导航基座）

**Status:** resolved

- [x] 可视化按 09 定稿实现（如余额迷你趋势），真金/纸面隔离强化
- [x] 成本摘要层级清晰：金额与 credits 分列、"未记录成本标缺失、不视为总成本已覆盖"的警示保留
- [x] 空资金页引导首次入金记录流程（教流程而非死页）
- [x] e2e+axe 零严重违例；`task check` 全绿

## 不变量与人裁决项

- Bankroll 只受 live 模式影响的口径不变；纸面收益不进资金页主呈现

## Answer

2026-09-16 落地（分支 `feat/ui-20-bankroll`，基于 19 号；commit `6eb6a63` feat(api) + `f021112` feat(web)，未 push）：

1. **后端入金端点（全批唯一 contract 变更）**：`POST /api/v1/bankroll/deposits`，`DepositPayload{amount_cny gt=0, occurred_at?, note?}` → 201 `DepositCreatedView{event, balance}`；无鉴权（单用户既定），withdraw 不做。领域函数 `betting/store.record_deposit`（kind=deposit、金额>0 域守卫、occurred_at 缺省取当前；SQL 留在拥有 bankroll 表的 betting 包，ADR-0008），API 层薄。contract-first 全流程：export → redocly recommended-strict 过 → codegen → conformance 过 → 契约 diff 与实现同 commit；既有端点零改动，redocly.yaml 豁免未动。
2. **页面按 09 定稿重写**：余额大数字（null 显"尚未入金"）+ 近 30 天余额迷你曲线（ECharts 单线；`balancePoints` 30 天窗口过滤+时间正序、`balanceCurveOption` 纯函数，独立子组件点数 ≥2 才挂载，窗口外不凑点）；流水 compact 表语义色 = 投注/兑付与盈亏色一致（红涨绿跌 text-profit/text-loss）、入金/出金/成本中性；成本摘要 ¥ 与 credits 分列、"不视为总成本已覆盖"警示逐字保留；口径行（只受 live 影响/纸面隔离）逐字保留。
3. **入金流程**：空态 EmptyState（no-data）"记录第一笔入金" → 内联表单（金额必填>0 前端校验+友好报错 role=alert、datetime-local 留空=现在、备注可选）；已有余额时次级"记录入金"按钮不抢余额视觉；成功后 invalidate ["bankroll"] 刷新余额与流水并收起表单。
4. **测试与门**：后端领域+API 测试（201/422/余额/非法金额不落账），`task test` 254 passed coverage ≥90% 过；web RTL 12 例（空态引导→表单→成功刷新/曲线挂载与不足诚实提示/语义色/成本分列/降级），lint/type/coverage/build 全绿；smoke e2e 新增 /bankroll 口径行+双路径用例，axe 九路由零 serious（18/18）；e2e:loop 纸面闭环 9/9 保持（"尚未入金"/bankroll-empty/cost-missing 断言未动，纸面锁定零资金流水断言原样通过）。
5. **注意点**：MSW bankrollFixture 时间改相对 now（防 30 天窗口过滤随日历漂移）；playwright 两配置加端口环境变量覆盖（默认 5173/5199/8931 不变）——主工作区常驻 dev server 时 worktree 门禁可并行跑，避免 reuse 撞到别的代码。
