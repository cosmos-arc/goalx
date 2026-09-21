# Wayfinder Map: goalx 工作台 v2（场次研究 + 玩法轴 + 仓位建议）

Label: wayfinder:map

## Destination

从"报表堆"升级为研究工作台：场次轴（近 3 日列表 → 单场研究页）+ 玩法轴（胜平负/进球推荐页、14场任9 专页）+ 建议仓位，全部合入 main。前图 goalx-ui-redesign 的设计系统/组件/基调直接继承。

## Notes

- 继承：Base UI 组件、语义 token（红涨绿跌/双层色）、EmptyState 三态、axe 基调、e2e 契约（见前图 map.md）。
- 本轮 grilling 已定（2026-09-17）：
  - 双轴 IA：场次（列表→研究页）+ 玩法（胜平负/进球/14场任9）；导航"玩法"为组
  - 场次列表 ≥3 日（fixtures 表已有多日数据，ingest 已在拉）
  - 研究页含多 book 赔率明细（odds_snapshots 已逐书存储，从未露出）+ 共识 + 模型概率 + EV；基本面/AI 研判区块留位标注"随 M3 到来"
  - 组合 v1 规则：EV×置信排序 top-N；约束 单注 1–5% bankroll、同场不重复、串关 ≤2 腿
  - 仓位=只读建议：纸面期一律 flat；真金 ¼ fractional Kelly + 1–5% 硬上限；EV≤0 建议 ¥0；不自动改单（调研：research/staking-plans.md，倍投/斐波那契系否决——不改变 EV 只重排破产路径）
  - 14场任9：UI 骨架先行（期次→选项概率→推荐→三档额度；AI 证据总结/目标金额/搏冷留位），彩池数据源后端接入在 goalx-quant 图立票（并行）
  - 玩法三分类进 CONTEXT.md：胜平负类(had/hhad) / 进球类(ttg/crs) / 奖池型(ttt14/pick9)；半全场暂不呈现
  - 亚盘：暂不引入（The Odds API 足球仅 h2h）；零成本先评估 draw_no_bet/btts 覆盖率；共识方法保持现状只做 A/B（调研：research/odds-consensus-methodology.md）
- 跨图外部依赖（goalx-quant 立票）：①彩池数据源+ingest ②赛果自动同步源 ③CLV 基准分层(Pinnacle 主锚) ④共识分母<4 低置信护栏 ⑤注级 EV/CLV 快照字段
- **e2e 推送前规约（PR #17/#19 三次同类 CI 失败后立）**：凡改 smoke 或页面渲染结构，推送前双跑——①死后端模拟 CI：`VITE_DEV_API_TARGET=http://127.0.0.1:8999 E2E_WEB_PORT=<空闲> bunx playwright test -c playwright.local-port.config.ts`；②常驻后端一遍——两遍全绿才推。数据分支专属 testid 的断言一律 `.or(empty-state)` 双路径（本地有 8000 后端，降级路径不跑就是盲区）。

## Decisions so far

- [01 场次列表 3 日化](issues/01-fixtures-3days.md): 参数形状定 `days`（默认1,1–7）+行内 `business_date`（sqlite 验证业务日离散分布，零破坏增量）；前端日期 Tab 默认今天（空窗日诚实占位、business_date 缺失按今天兜底兼容旧后端双路径 e2e）；`/fixtures` 承接 `/today` 重定向，导航与总览跳转同步，testid `today-*`→`fixtures-*` 显式迁移（纸面闭环 9/9 保持）；MSW handler 按 days 过滤与真实 API 同口径。
- [02 场次研究页](issues/02-fixture-research.md): 单端点 `GET /fixtures/{id}/research` 一跳拿全（逐书 H/D/A+捕获时间/去水共识/模型概率与模型 EV/had 判定），共识与列表页同口径不出现两个共识；模型区用 `latest_forecast`（与前瞻冻结同序），缺 Forecast 诚实 null；偏差 ≥5pp 琥珀+方向在前端算（展示层关注点不进 API）；共享原语抽 `components/had-quote-ui`（PickableFixture 最小结构，03 号"一键带入选注"可直接复用）；404 与后端不可用按 HTTP 状态分空态（fetch 专用错误挂 status）；词典补 model-prob/book-deviation 两词条（计数断言 10→12）；paper-loop 闭环扩为 10 步。
- [03 胜平负玩法页](issues/03-market-had.md): 组合 v1 语义定为**一组独立单关注单**（"每腿建议注额/单注 1–5%/注数"三处原文自洽），预期收益 = Σ EV×注额 共识口径；引擎纯函数 `lib/combo-engine.ts`（EV×置信排序 top-N≤2，置信=books/5 截断 1；EV≤0/非可投/非单固/books=0 不入选；flat 档注额 clamp(bankroll×2%, 1–5%) 下限 ¥2，未入金/过小诚实说明；`StakeProfile` 留给票 06 扩 ¼Kelly）；导航加一级"玩法"（/markets 重定向 /markets/had），组内三入口 = 玩法页顶部 MarketTabs，进球/14场任9 占位（随票 04/07）；EligibleCard 下沉 had-quote-ui（feed 态）、Selection/业务日工具下沉 lib/ui；e2e axe 基线 9→12 路由、paper-loop 10→11 步（demo 全负 EV → 组合空态即引擎诚实行为）。
- [04 进球类数据链](issues/04-goals-data.md): sporttery 实证**含** ttg/crs（票 19 起 POOL_CODES 已拉，主库 864+3286 快照全网格）——ingest 唯一增量是 poolList 池级 single 解析（ttg/crs 市场块实测恒缺 single，had 口径不动；顺带发现 had 链漏 poolList 的既有缺口，另立跟进）；概率推导 `modelling/goals.py`（矩阵→ttg/crs 边际 + 选项行，EV 口径=**模型×竞彩价**，进球类无欧共识与 had 不同源并进 docstring/词典口径）；contract 最小增量=独立端点 `GET /api/v1/markets/goals`（不污染 today 消费方），行=场次+ttg/crs 两块（网格×odds/probability/ev+单固/销售）+模型出处，无 Forecast 整块置 null；demo 种子补 ttg/crs+002 场 dc-demo Forecast（s2 价 4.50→EV+10.3% 组合非空路径）；277 测/94.98%、type/lint/contract 同 commit 绿。
- [05 进球玩法页](issues/05-market-goals.md): `/markets/goals` 占位换真实页（MarketTabs 点亮机制零新增）；引擎参数化 `buildGoalsCombo` 复用 had 骨架（flat 档/同场不重复/top-N）差异三处如实分离——EV=行内模型×竞彩价（前端不计算）、**不组串**（独立单关注单 maxPicks=3）、置信信号缺位按 EV 排序不加权并标注；选注篮语义换**待提交独立单关列表**（同场多注允许、同选项 toggle-off、逐腿 POST /bets）；页头三处 GlossaryTerm（新词条 `score-matrix`，12→13）；goals-ui 只做进球原语（ttgLabel/crsLabel/卡片），EV 着色等直接 import had-quote-ui 零拷贝；paper-loop 10→12 步（ttg 单关建议落 bet_legs 的 DB 断言）、smoke 26 过（axe 含新页）、170 单测/85.65% branches；纯前端票零后端改动。
- [06 建议仓位](issues/06-stake-advice.md): 领域函数归 `betting/staking.py`（建注决策域，ADR-0008；纯函数无表）；规则=EV≤0→¥0、paper 一律 flat（bankroll×2% 截断 1–5% 下限 ¥2，**与引擎 StakeProfile 同口径同精度可对账**）、live=¼Kelly（f\*=EV/(odds−1) 取 1/4）截断单注 1%–cap%（cap 参数默认 5%，票 07 三档复用）、串关=整注口径（调用方传联合赔率/联合 EV）；contract 增量 `POST /api/v1/stake-advice`（bets 路由）；前端共享 `StakeAdviceNote` 嵌四处选注篮+两组合头部卡（只读，进球篮按最高 EV 一注口径标注），三态诚实降级（资金池失败/缺 EV/服务不可用）；词条 +2（kelly/stake-advice，13→15）；ruff ignore +RUF001（中文标点 UI 文案，与 RUF002/003 同因）；293 测/95.07% + smoke 26 + paper-loop 12（含 EV≤0→¥0 与 flat ¥2 两断言路径）。
- [07 14场任9 骨架页](issues/07-market-pool.md): `/markets/pool` 占位换真实骨架页（MarketPlaceholderPage 随末位消费者删除）；诚实原则=页头 warning 横幅"彩池数据源接入中（goalx-quant 立票）"+期次演示（业务日聚合）+14 槽位缺数据虚线"待期次数据"+概率占位=欧共识（had 模型概率/彩池派彩待数据源，无共识显示"待数据"）+推荐/搏冷标记明示纯前端非生成器；交互完整可演示：一场一选 toggle/预选封顶 9/组合数 C(N,9)/三档额度复用 06 端点（**映射定稿：保守=flat、标准=¼Kelly cap 2%、激进=¼Kelly cap 5%**，联合口径输入，EV≤0 三档 ¥0，真金档标注 skill 未过线仅演示）；AI 证据总结/目标金额反推/搏冷生成器 not-available 留位（M3/v2）、提交 disabled 占位（pool-slips 接线随数据源）；StakeAdviceNote 加 label prop 复用；axe 修横幅对比度；185 单测/85.54% branches、smoke 27、paper-loop 12 全绿；纯前端零后端改动。
## Not yet specified

- 目标金额反推选择、搏冷倾向模式（14场任9 v2，骨架落地后毕业）
- AI 证据总结生成（等 M3 LLM 线）
- 研究页基本面数据源与 AI 研判（等 M3）
- 大小球/亚盘数据源（进球玩法上线后按需评估）
- i18n（不变，推广真实发生时）

## Out of scope

- 自动下注/自动改单（仓位仅建议）
- 真亚盘数据源引入（暂不必要，调研结论）
- 登录/多租户、原生 App（沿前图）
- 破坏性 API 变更（增量原则不变）

## Comments

- 2026-09-17 charting：grilling 三轮（工作台目的→玩法分类/仓位调研→用户完整场景复述确认），两份调研笔记（staking-plans / odds-consensus-methodology）随图。建图 + 票 01-07。
