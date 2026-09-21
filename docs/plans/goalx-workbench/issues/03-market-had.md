# 03: 胜平负玩法页（推荐流 + 组合头部）

**What to build:** 玩法组第一页：全部场次的 had 机会按 EV×置信排序的推荐流（复用今日页卡片组件），头部给 v1 组合推荐（具体场次+注数+预期收益，纯函数引擎：top-N + 单注 1–5% + 同场不重复 + 串关 ≤2），一键带入选注。

**Blocked by:** None（用现有 had 数据）

**Status:** resolved

- [x] 组合引擎纯函数 + 单测（约束与预期收益口径，标注纸面/真金）
- [x] 推荐流（排序、EV/资格编码沿用今日页语义）+ 组合头部卡（带入选注篮动作）
- [x] 导航"玩法"组上线（胜平负/进球/14场任9 三入口，进球与 14 场先占位）
- [x] axe/e2e/空态

## Answer

- **组合语义（关键裁决）**：组合 v1 = **一组独立单关注单**（非一注 2串1）——依据三处原文自洽："每腿建议注额"（2串1 只有一个注额）、"单注 1–5%"（单注=每条单关）、"注数"为卡级统计（N 可 >1）。预期收益 = Σ EV×注额（共识口径，卡片 GlossaryTerm id=ev 标注诊断量警示）。带入 2 条时选注篮按一注 2串1 提交（既有 basket 形态），口径差异在带入消息里诚实说明（"组合卡预期收益按独立单关口径合计"）——组合 v2 若要串关口径需 (1+EV₁)(1+EV₂)−1，留给后续票评估。
- **引擎**（`lib/combo-engine.ts` 纯函数，17 单测）：score = EV × 置信，置信 = min(books/5, 1)（样本厚度线性降权，books=0 无共识不入）；约束 EV≤0 不入选、可投+单固才入组合（单关须单固；非单固正 EV 留在推荐流人工研判）、同场不重复、top-N=2；flat 档注额 = clamp(bankroll×2%, 1–5%) 下限 ¥2——未入金按 ¥2 建议并说明、bankroll 过小（¥2 > 5% 上限）说明冲突；`rankFixturesForFeed` 独立导出（推荐流排序不依赖 bankroll，资金池失败也能先看流）。档位参数在 `StakeProfile` 结构里，票 06 扩 ¼Kelly 不动排序/约束逻辑。
- **置信口径**：books 是行内唯一置信信号（共识分母<4 护栏是 goalx-quant 外部依赖，未到）。ev_deviation（|EV|≥5%）**不排除**（票面只定 EV≤0 排除）但随腿/卡片展示琥珀警示——人判覆盖默认判读，是否降权归组合 v2。
- **页面** `/markets/had`：组合头部卡（腿列表含场次/玩法/odds/EV/books/置信/建议注额/单条预期 + 注数/总注额/总预期 + 约束说明 5 条 + 一键带入）+ 推荐流（3 日窗口全部场次跨日混排，卡片=场次页同组件）。一键带入=就地打开本页选注篮（复用常驻条+Drawer 模式，腿直接 makeLeg，flat 注额预填）——不跳场次页（basket 是页内 state，跳页即丢）。
- **导航**：一级加单入口"玩法"（/markets beforeLoad 重定向 /markets/had），两级扁平基调不动；组内三入口 = 玩法页顶部 `MarketTabs`（胜平负/进球/14场任9，语义 nav + aria-current，沿页内 Tab 先例），进球/14场任9 为 EmptyState not-available 占位（"随票 04/07 上线"）。前缀归属：/markets/* 都点亮一级"玩法"。
- **共享下沉**：`EligibleCard` 从 fixtures-page 抽入 `components/had-quote-ui`（新增 feed 态：非可投卡按钮禁用 + EligibilityBadge 说明原因 + 可选跨日 dayNote/testid 前缀——场次页渲染路径逐像素不变）；`Selection/SELECTIONS` 与业务日工具（beijingBusinessDate/addDays/dayLabel）下沉 `lib/ui`（引擎要 `SELECTIONS`、玩法页要 dayLabel；had-quote-ui re-export 保持既有 import 稳定）。
- **e2e**：smoke 导航断言加"玩法"（7 项一级），/markets 重定向与玩法页数据/降级双路径新测试，axe 基线 9→12 路由全过（25 过）；paper-loop 闭环扩为 11 步——demo 种子三场 EV 实测全负（jc 价含 ~13% 水位高于去水共识），组合空态"无正 EV 机会"正是引擎诚实行为，步骤同时验证推荐流排序/停售禁用/就地选注（不提交，后续步骤注数断言不变）。
- **给 04/05 的衔接点**：① 进球页（05）复用 `market-had-page.tsx` 的页面骨架与 `MarketTabs`；引擎按玩法参数化——ttg/crs 候选展开换 selection 集合与 EV 来源（比分矩阵推导，`forecast_matrix_from_payload` 入口见票 02 Answer ④），`StakeProfile`/约束逻辑直接复用；② 04（ingest ttg/crs）落地前 `/markets/goals` 占位文案已指向票 04；③ 06（仓位）：`StakeProfile` 已留结构，¼Kelly 只加档位分支；④ demo 种子若要覆盖"组合非空"路径，需后端票加正 EV 场次（本票零后端改动未动 demo.py——e2e 非空路径由 MSW 单测覆盖）。
- **质量门**：web lint/biome、type、154 单测、coverage 85.35% branches（≥85 门）/95.49% lines、build 绿；后端零改动零 contract 变更（git status 仅 apps/web）；smoke 25 + paper-loop 11 全绿（E2E_WEB_PORT=5273 / loop 5274+8932 避开主工作区）。
- **提交**：`feat(wb)` 2e1e98f（单 commit，纯前端票无 api 部分可拆），worktree `.scratch/wt-wb` 分支 feat/wb-03-market（基于 feat/wb-01-fixtures）。
