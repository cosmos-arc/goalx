# 17: 历史复盘页落地

**What to build:** 新页：已结算注的筛选（时间/Competition/模式/策略版本）、聚合指标（盈亏/命中率等，口径按票 07）、聚合行→注明细→Fixture 的下钻链路。数据源按票 10 定稿（2026-09-17）：现有 `GET /bets` 前端过滤聚合，零新查询端点。

**Blocked by:** 07（设计定稿），10（API 排期），13（导航基座）

**Status:** resolved

- [x] 筛选组合与聚合口径按 07 定稿实现
- [x] 与验证页的口径差异（前瞻纳入/排除规则）在页面显式标注，两页数字可对账
- [ ] 下钻链路完整：聚合行 → 注明细 → Fixture（2026-09-17：聚合行→注明细已落地；明细→Fixture 的一跳留待场次详情页——map "Not yet specified"，依赖后端赔率历史数据可用性，见 Answer §5）
- [x] ~~后端查询端点按 ADR-0008 落包，contract 流程闭环（export→codegen 同 commit）~~（2026-09-17 票 10 定稿：v1 前端拼装零新端点，本项作废）
- [x] 大分页、空筛选结果、三态空状态处理完备；e2e+axe 全绿

## 不变量与人裁决项

- 口径差异标注文案须人裁决（07 承接），不得由实现票自拟
- 纸面/真金分组呈现，不得混算

## Answer

2026-09-17 落地（分支 `feat/ui-17-history`，commit 1324f83，基于 `feat/ui-16-bets`）：

1. **两层筛选**：常驻 = 模式大标签（纸面/真金 fieldset + aria-pressed，默认纸面，分别统计不混算）+ 时间范围预设（近 7/30/90 天/全部，按 settled_at，默认全部不少画）；二层 = `<details>` 折叠的 Competition / 策略版本 / 结果状态。全部前端过滤（`GET /bets` 取 purchased && settled_at 非空，与 16 号已结算段同判定），筛选选项随当前模式样本推导、不写死。Competition 归因唯一来源是今日列表的 competition 字段（v1 无历史 fixture 详情端点），不在今日列表的场次仅"全部"下可见—— limitation 已在实现注释标明。
2. **聚合区**：五指标卡随筛选联动——总盈亏（正负红绿 + 正负号）、注数、命中率（= 胜/(胜+负+部分)，退款不计，hint 注明口径）、平均 EV / 平均 CLV（**BetView 无注级 EV/CLV 字段（v1 API），按 07 预案显示"—"并注明口径与判读方向**：EV 正 = 正期望、CLV 正 = 买在好价（closing 优于锁定），聚合口径指向验证页；不造假数据，注级字段属 API 增量待排期）。盈亏累计曲线复用 use-echarts：settled_at 升序累计（profit 缺失按 0 接力），0 基准虚线 markline（echarts.ts 按需注册 MarkLineComponent），<2 点不出图显示积累提示；**图表容器随独立子组件挂载**——use-echarts 的 init effect deps=[]，容器晚于挂载出现则 init 拿不到（实现票发现的时序约束，hook 本身未改）。
3. **口径标注**：页头固定一行按 07 Answer 逐字："统计已锁定且已结算的注；前瞻验证口径（含排除规则）见验证页。"（验证页内链）；"前瞻纳入"四字链接指向 /glossary（tooltip 随 18 号词典落地，按指示接受 href 先行）。空筛选结果 = no-data 空态"该筛选下无已结算注" + 清筛选动作（清时间/Competition/策略/状态，模式保留——模式是口径选择不是筛选）；后端不可用 = backend-unavailable + 重试。大分页：千注级前端拼装按票 10 定稿无压力，未做分页。
4. **明细**："查看明细"同页展开 compact Table 五列（内容 / 注金[建议→实际] / 状态徽章 / 盈亏红绿 / 结算时点），按结算时点倒序，与投注页已结算行同构（模式列省略——本页模式已隔离），不跳页。
5. **下钻链路的 Fixture 一跳未做**：明细行到场次详情页的链路留待场次详情页（map "Not yet specified"，依赖后端赔率历史数据可用性）；本票按"聚合行 → 注明细"完成度落地，Fixture 端如实标注未闭环。
6. **测试与门**：RTL+MSW 新增 10 例（口径行逐字与链接/五指标聚合与 live 不混算/时间范围四预设联动/二层筛选与清筛选还原/曲线 option 升序累计 + 0 基准 markline + 不足 2 点/明细同构含回录快照列/降级 + 重试/骨架屏）；smoke 新增 /history 断言（口径行 + 聚合/空态双通过 + axe expectNoSeriousAxeViolations），占位断言随之下线；e2e:loop 纸面闭环 **9/9 保持**。lint/type/coverage（全仓 branch 86.4% ≥ 85%，history 页行覆盖 97.8%）/build 全绿。已知环境坑（与 16 号同一项，与本票无关）：smoke 的 validation 降级用例在 8000 常驻后端存在时失败（用例假设后端不可达）。
7. **给 18 号的衔接点**：口径行的"前瞻纳入"链接位置即词典 tooltip 的落点（Popover 替换 href 即可）；STATUS_META / pnlClass / legText / stakeText 呈现工具可直接参照复用；worktree `.scratch/wt-15` 保留给 18 号堆叠（分支基于本分支）。
