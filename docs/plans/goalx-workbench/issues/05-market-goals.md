# 05: 进球玩法页

**What to build:** 玩法组第二页：ttg/crs 推荐流 + 组合头部，复用 03 的引擎与组件；进球类以单关为主（概率×赔率→EV 排序）。

**Blocked by:** 03, 04

**Status:** resolved

- [x] ttg/crs 推荐流（EV 排序、进球类口径标注）+ 组合头部
- [x] 玩法组导航点亮"进球"
- [x] axe/e2e/空态；与胜平负页组件复用不复制

## Answer

- **路由与点亮**：`/markets/goals` 占位换真实页（与 MarketTabs 既有占位路由对齐，零路由增删）；MarketTabs "进球" aria-current 随路径点亮（机制本就存在，删除占位即生效）；导航断言（smoke）改为真实页三断言（Tab 点亮/口径行/数据或降级双路径）。
- **引擎参数化（03 衔接点 ① 的落实）**：`buildGoalsCombo` 加进 `lib/combo-engine.ts`——复用 `flatStake`（同 `StakeProfile`：2% flat、1–5% 区间、¥2 下限、未入金/过小诚实说明）与同场不重复、top-N 贪心骨架；**差异三处如实分离**：① EV 来源换行内 `GoalsSelectionView.ev`（后端矩阵推导的模型×竞彩价口径，前端不再计算）；② **不组串**（map 定稿"进球类以单关为主"）——组合=独立单关注单，maxPicks=3；③ **置信信号缺位**：进球类无欧赔 books、模型 CI 仅 had——v1 按 EV 排序不加权，notes 与页头如实标注"置信信号缺位不加权"（不伪造置信，与 03 的 books/5 降权形成明确口径差异）。`isGoalsPickable` 与 had 的 isPickable 同构（在售+单固+未开赛）。
- **选注篮语义（与 had 页的关键差异）**：had 篮=一注 1–2 腿（2串1 语义）；goals 篮=**待提交独立单关列表**（≤3 注，同场多注允许——独立单关本就合法，同选项再点=取消）；提交=逐腿 `POST /bets`（market_code=ttg/crs、selection_code="2"/"1:1"，服务器侧 non-had 腿按票 36 既有行为不校验——前端禁用兜底，缺口已在 04 Answer 标注跟进）。结算引擎已支持 ttg/crs，纸面闭环全链路可走（paper-loop 第 11 步实证：ttg 单关建议落 bet_legs）。
- **页面**（`market-goals-page.tsx` 复用 03 骨架：MarketTabs+组合头部卡+推荐流+常驻条/Drawer）：页头口径三处 GlossaryTerm——`score-matrix`（**新词条**"比分矩阵推导"，三要素齐全：定义=canonical 10×10 矩阵边际视图、方向=ttg/crs 同源一致、实例=λ1.4/1.3→P(2 球)≈24.5%、警示=模型覆盖与诊断量口径，词条计数 12→13 同步 glossary 单测与 smoke 断言）、`model-prob`（EV=模型概率×竞彩价−1 口径与共识 EV 不同源）、`single`（单关为主）；推荐流页内 ttg/crs 切换 Tab（role=tablist，8 档/31 档网格同卡换渲染）；无模型场"概率/EV 空缺"行内标注、停售场按钮禁用+"已停售"。
- **组件复用不复制**：`components/goals-ui.tsx` 只做进球特有原语（ttgLabel"7+"/crsLabel"胜其他"、GoalsLeg 类型、GoalsOddsButton、GoalsMarketCard）；EV 着色/倒计时/时间格式直接 import had-quote-ui 的 evClass/evText/kickoffInfo/localTime，零拷贝。
- **测试**：引擎 8 组新单测（排序/同场一注/ttg×crs 跨玩法最优/停售非单固已开赛不入/flat 边界/口径 notes/沉底排序）；页面 9 测（口径行/组合卡/带入提交逐腿独立单关/玩法切换/篮规则 toggle-off·同场共存·上限/服务器 400 透出/空组合/三态/未入金）；MSW goals 网格 mock（与 demo 种子同形状：002 带模型 s2 EV+10.25% 正、001 无模型、003 停售）；smoke 26 过（含 /markets/goals axe 基线与降级双路径——旧常驻后端无 /markets/goals 时按后端不可用诚实降级）；**paper-loop 10→12 步全绿**（新增进球页步骤：组合非空(002 s2)→一键带入→¥2 最低注（bankroll ¥2.40 过小路径顺带验证）→单关建议落地→`bet_legs.market_code='ttg'` DB 断言；置于既有步骤后，后续计数断言全部动态无破坏）。
- **质量门**：web lint（biome）/type（tsc）绿；170 单测全过、coverage 85.65% branches（≥85 门）/95.13% lines；build 绿；后端零改动（纯前端票）。提交：worktree `.scratch/wt-wb` 分支 `feat/wb-04-goals` 单 commit `feat(web): 进球玩法页…`（紧随 04 的后端 commit）。
- **给 06/07 的衔接点**：① 06（仓位）：`StakeProfile` 被 goals 引擎原样复用，¼Kelly 只加档位分支两玩法同享；② 07（14场任9）：MarketTabs 第三入口仍为占位（票 07 上线时同样换真实页+点亮，本票已验证机制）；③ 若后续要做"进球类服务器侧资格校验"（补 non-had 腿的 had-quote 同款 adjudication），前端 `isGoalsPickable` 口径即现成规范。
