# 04: 进球类数据链（ttg/crs 后端）

**What to build:** 进球类玩法的数据与端点：ingest 补竞彩 ttg/crs 赔率；概率由比分矩阵（ADR-0006）推导；contract 增量（玩法赔率 + 概率/EV 端点）。纯后端票，前端 05 消费。

**Blocked by:** None（可与 03 并行）

**Status:** resolved

- [x] ingest 拉竞彩 ttg/crs 在售赔率（复用 sporttery 链路）
- [x] 比分矩阵 → ttg/crs 概率推导（领域函数 + 单测，口径进词典注释）
- [x] contract：玩法赔率/概率端点（export→codegen 同 commit）
- [x] 后端测试与 coverage ≥90%

## Answer

- **sporttery 数据源实证结论（票面首要问题）**：端点**含** ttg/crs，无需等数据源确认。`POOL_CODES = (had, hhad, crs, ttg, hafu)` 自票 19 起就含两玩法；主库 `odds_snapshots` 实测 2026-09-11~09-17 有 ttg 864 条（0..7 八档全）、crs 3286 条（28 精确 + 三档其他全）。ingest 唯一缺口是**单固资格**：ttg/crs 市场块实测恒缺 `single` 字段（票 35 只给 had 建了资格链），本票从 matchInfo 的 `poolList` 各池 `single`（int 1/0，实测唯一可靠来源）解析入库（`sale_statuses` 增 ttg/crs 市场级行）；实测当日 24 场 ttg/crs 全部 single=1。
- **发现的既有口径缺口（不属本票，已如实报告未动）**：poolList 里 HAD 池 single=1 的 6/24 场，现行 had 逻辑（市场块 single 缺失 + 比赛级 bettingSingle=0 → False）会误记非单固——had 口径归票 35，本票 `goals_single_eligible` 只管 ttg/crs（市场块 single 优先 → poolList → 比赛级否决 → None），`had_single_eligible` 一行未动。建议立跟进票修 had 链（poolList 插到市场块与比赛级否决之间）。
- **概率推导**：`modelling/goals.py`——`goals_selection_grid`（官方网格全仓唯一出处进 `markets.py`：TTG 8 档、CRS 28+3）、`goals_probabilities`（ScoreMatrix.ttg()/crs() 的玩法分发入口）、`goals_selection_rows`（矩阵概率×竞彩价→选项行，EV=概率×价−1）。**EV 口径 = 模型×竞彩价**（进球类无欧赔共识——The Odds API 足球仅 h2h），与 had 页的共识 EV 不同源，docstring 按 model-prob 词条口径转写并保留"诊断量非机会信号"警示。一致性单测：Σ=1、ttg 反对角=矩阵格和、crs 精确格=矩阵格、其他档=方向尾部和、同矩阵推得的 ttg 与 crs 联合一致（≤5 球精确格和=ttg 档；6 球起部分格落"其他"档的边界也断言了）。
- **contract（最小增量形状裁决）**：独立端点 `GET /api/v1/markets/goals`（新 tag `markets`）而非扩 `/fixtures/today`——today 行被场次页/had 页共用，塞 39 个选项会污染所有消费方；独立端点一跳拿全（票 02 研究页先例）。行 = 场次身份 + `ttg`/`crs` 两块（官方网格×{odds/probability/ev} + single_eligible/sale_state/updated_at）+ model_version/issued_at；`date/days` 参数与场次列表同口径（1–7）。**无 Forecast 的场次整块 probability/ev 置 null**（仅五大有模型覆盖，不伪造）；只采到 had 的场次网格照常返回、报价空。contract diff：+1 路径 +3 schema（GoalsFixtureView/GoalsMarketBlock/GoalsSelectionView），零破坏，export→codegen 同 commit。
- **demo 种子**（05 的 e2e 前置，03 Answer ④ 的落实）：三场全补 ttg/crs 盘口 + ttg/crs 市场级销售行；002 场（利物浦 vs 曼城）插 `dc-demo` Forecast（λ1.4/1.3 矩阵）——ttg s2 价 4.50 → 模型 EV≈+10.3%（组合非空路径），其余档与 crs 全网格按 ~25% 水位（EV 全负的诚实多数态）；001 无 Forecast（研究页"暂无模型预测"演示口径保留，paper-loop 既有步骤不动）；003 停售（禁用路径）。
- **给 05 的衔接点**：① `GET /markets/goals`（days=3 与场次页同窗）行内 `ttg`/`crs` 块即推荐流数据源，`GoalsSelectionView.ev` 就是引擎的 EV 输入（无需前端再算）；② 单关资格 = 块内 `single_eligible === true` + `sale_state === 'on_sale'` + 未开赛（kickoff_utc 前端判），与 had 的 isPickable 同构；③ 引擎约束按 map：不组串、单注 1–5%（flat 档复用 `StakeProfile`）；置信信号缺位（无 books、CI 仅 had）——v1 按 EV 排序不加权并如实标注，勿造置信；④ 建注走既有 `POST /bets`（market_code=ttg/crs、selection_code 如 "2"/"1:1"），服务器侧 non-had 腿暂不校验（票 36 既有行为，建注不限）——页面端禁用逻辑兜底，服务器校验缺口已在报告标注跟进；⑤ 结算引擎已支持 ttg/crs（`_ttg_code`/`_crs_code`），纸面闭环全链路可走。
- **质量门**：`task test` 277 全绿、coverage 94.98%（≥90%）；`task type`（basedpyright strict）0 错；`task lint`/`fmt-check` 绿；contract conformance（served==reviewed）过。提交：worktree `.scratch/wt-wb` 分支 `feat/wb-04-goals`（基于 feat/wb-03-market）单 commit `feat(api): 进球类数据链…`。
