# 15: 总览 Dashboard 落地

**What to build:** 新首页：待办区（未锁定建议/待录开奖/待结算/数据新鲜度异常，判定规则按票 05）、资金与收益快照（真金/纸面隔离）、机会入口与下钻。所需聚合/待办端点整条竖切：后端领域包实现 → contract export+codegen → MSW → UI → 测试。

**Blocked by:** 05（设计定稿），10（API 排期），13（导航基座）

**Status:** resolved

## Comments

- 2026-09-17 修正（票 10 定稿联动）：数据源改为 **v1 前端拼装、零新聚合端点**（bets/bankroll/fixtures/today/validation 组合）；下方"后端聚合端点"验收项替代为"数据由现有端点拼装，无新增 contract 变更"，其余验收项不变。

- [ ] 待办区判定规则按 05 定稿实现，每项可下钻到对应页并携带上下文
- [ ] 首屏快照指标全部附判读方向；真金与纸面视觉隔离不混列
- [ ] ~~后端聚合端点按 ADR-0008 落对应领域包（SQL 不出所属包），contract export→codegen 与代码同 commit~~（2026-09-17 票 10 定稿：v1 前端拼装零新端点，本项作废）
- [ ] 后端测试（coverage ≥90% 门槛）+ 前端 RTL+MSW + e2e+axe 全绿
- [ ] 作为默认首页生效（原 `/` 今日页让位，路由按 13 落定）

## 不变量与人裁决项

- 快照口径以 05 裁决为准；任何与验证页口径不同的数字必须显式标注差异
- 纸面收益不得与真金余额并列呈现而无隔离标识

## Answer

2026-09-15 实现完成（分支 `feat/ui-15-overview`，commit 8389dfb，未 push）。`/` 从 13 号引导态替换为真实总览；零 contract/后端变更，数据 = fetchTodayFixtures/fetchBets/fetchBankroll/fetchDrawResults/fetchValidationProgress 五路 react-query 拼装（缓存键与各页对齐）。

**落地形态（按 05 定稿）**：

1. **待办清单卡**：四规则按 ①→④ 排序，每行 状态点 + 一句话 + 动作链接（①→/today、②③→/bets、④就地指引 `task ingest-jingcai` 无跳转）；无待办整卡"今日无事"。
2. **首屏快照四卡**：真金区（余额+今日真金盈亏 / 累计 ROI）+ 今日可投场次卡 + 纸面独立虚线区（"纸面"徽章，与真金不同区）；每卡附判读方向行；红涨绿跌+正负号+tabular-nums。未入金→真金区 EmptyState 引导（标注页内记账随票 20）；无纸面记录→"从今日页建第一笔建议"引导。
3. **页底验证进度细线** x/3（三条件，full_season 独立条件不计入），→/validation；验证接口挂了整线隐藏。
4. **分诊原则**：页面零写操作，仅链接下钻。

**边界口径裁决（实现中定，记录备查）**：

- 规则①②的开赛时点只能对**今日列表内**的场次推导——历史挂注场次现有端点无 kickoff，不下判不误报（后者是"挂未来场次的未结注"主体，不判防止整卡常驻误报）。
- 规则③与后端 `run_settlement` 同口径（status=open 且全部腿有赛果，不区分是否已锁定）。
- 规则④ = 过 10 点且**整份快照缺**（含当日 0 场）：jc 全缺或 joined 全缺（0/N 接入，与 14 号健康行同口径近似 eu_updated_at）；部分覆盖是常态不算异常，明细留 today 页。今日接口**请求失败≠快照缺**，失败走"判定暂缺"降级注不做真空异常。
- 今日真金盈亏 = 当日 bankroll 投注流水净额（bet_stake+bet_payout，**入金/出金/成本不计入盈亏**），本地时区按日切。
- 真金累计 ROI = 已结算 live 注 Σprofit/Σ(actual_stake??stake)，显式标注"未结 N 注不计"（与验证页 paper 口径不同，已按不变量要求标注差异）。
- 今日纸面盈亏 = mode=paper 且当日 settled 的 Σprofit；无当日结算显示"—"而非 0。

**质量门**：biome/tsc/vitest(64)/coverage(98.8% lines)/build 全绿；e2e:loop 纸面闭环 **9/9**；smoke 14/15——唯一失败 `validation page degrades gracefully without backend` 为**预存环境项**（8000 常驻后端让验证页有数据不进 error 态，已用 stash 在无本票改动的树上复现归因）；axe `/` 零 serious（修复两处 10% 底纹徽章对比不足：徽章文字改前景色，票 14 flag 同教训，随本票记录）。
