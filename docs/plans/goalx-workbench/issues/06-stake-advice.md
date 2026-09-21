# 06: 建议仓位（只读）

**What to build:** 前后端"建议仓位"：端点输入 bankroll/edge/赔率 → 输出建议注额与理由；规则：纸面一律 flat（红线）、真金 ¼ fractional Kelly + 单注 1–5% 硬上限、EV≤0 → ¥0。UI 嵌入选注篮与组合头部，只读不改单。

**Blocked by:** None（调研定稿：research/staking-plans.md）

**Status:** resolved

- [x] 仓位计算领域函数（纯函数 + 单测：Kelly 公式/分数/上限/纸面 flat）
- [x] contract：stake-advice 端点
- [x] UI：选注篮与组合头部显示建议额+档位理由（词典词条：Kelly/建议仓位）
- [x] 倍投/斐波那契等不做（决策已定）；axe/e2e

## Answer

**归属（ADR-0008）**：`betting/staking.py`（注额建议服务于建注决策，归 betting 域；evaluation 是度量历史表现，不贴切）。纯函数、无表无 SQL——建议留痕随注单落库是后续项（调研 §5.3）。

**规则实现**（与 map 定稿逐条对齐）：
- `suggest_stake(mode, bankroll, ev, odds, cap_fraction=0.05)` → `StakeSuggestion{stake, tier, fraction, full_kelly_fraction, capped, reason}`；
- EV≤0 → ¥0 + 显式理由（防"看推荐再自己加码"），两模式一致；
- paper 一律 flat（红线）：bankroll×2% 截断 1–5% 区间、下限 ¥2——**与组合引擎 StakeProfile（03/05 两玩法页共用）同口径同精度（按分取整），前后端数字可对账**；未入金按最低注 ¥2 并说明；
- live = ¼ fractional Kelly：f\* = EV/(odds−1) 取 1/4，截断单注 1%–cap%（cap 参数 0–5% 默认 5%）；未入金诚实 ¥0（不伪造比例）；
- 串关注额=单关口径：调用方传联合赔率/联合 EV（`Π(1+EV_i)−1`，腿间独立性假设），整注一个 Kelly 不分腿——docstring 与 UI 口径行均标注。

**contract**：`POST /api/v1/stake-advice`（最小形状：mode/bankroll/ev/odds + 可选 cap_fraction → 建议注额/档位/理由），挂在 bets 路由（投注 API）。export+codegen 同 commit，Redocly recommended-strict 通过。

**前端**：共享组件 `components/stake-advice.tsx`（StakeAdviceNote + parlayAdviceInput 联合口径 helper）：
- 嵌入**四处选注篮**（场次页/研究页/胜平负页/进球页的 Drawer）+ **两处组合头部卡**（had/goals，按纸面 flat 档口径展示）；
- 档位理由直出后端 reason（flat 红线 / ¼Kelly / 截断说明 / EV≤0 提示）；进球篮为 N 注独立单关同额提交——按篮内最高 EV 一注口径并标注；
- 三态诚实降级：资金池读取失败 / 缺 EV 数据（如无欧赔共识场）/ 建议服务不可用，均不给数字只给说明；
- 只读：不写注额输入框、不改单（建注仍全人工确认）。
- 词条 +2（`kelly`、`stake-advice`，三要素齐全，13→15）；MSW 加了与后端同规则的最小镜像 handler。

**边界外**：倍投/斐波那契等 progression 不做（已否决，词条 caution 保留否决理由）；`pyproject.toml` ruff ignore +RUF001（中文标点 UI 文案直出前端，与既有 RUF002/003 豁免同因）。

**测试**：后端 13 个域函数用例（flat 红线/¼Kelly 基例/上限截断/cap 分档/1% 下限/EV≤0/未入金/最小注越限/串关整注口径/非法输入）+ 4 个端点用例；前端组件 6 用例 + had 页组合卡/选注篮（含真金串关联合 EV≤0 → ¥0）断言，176 单测过；smoke 词典计数 13→15、axe 全绿；paper-loop 12 步含两处建议断言（demo 主胜负 EV → ¥0 诚实路径；进球步 bankroll ¥2.40 → flat ¥2+越限说明）。

**commit**：`feat/wb-06-stake`（worktree `.scratch/wt-wb`，基于 `feat/wb-04-goals`）——`feat(betting): 建议仓位(只读)…`；门禁：lint/fmt/type/test 293 测 95.07%（≥90%）、contract lint+conformance、web lint/type/test、smoke 26、paper-loop 12 全绿。
