# 02: 场次研究页

**What to build:** 单场研究页 `/fixtures/:id`：多 book 赔率明细（逐书 H/D/A + 与共识偏差）、去水共识、模型概率与 EV、资格判定；基本面/AI 研判区块留位（"随 M3 到来"）。场次列表行可点击进入。

**Blocked by:** 01

**Status:** resolved

- [x] per-book odds 端点（odds_snapshots 逐书数据；contract-first）
- [x] 研究页：赔率明细表（书/三向/偏差高亮）+ 共识 + 模型概率 + EV + 资格徽章
- [x] 基本面与 AI 留位组件（EmptyState not-available 态）
- [x] 研究页内可选注并带入选注篮（复用今日页交互），就地建注
- [x] axe、空态、双主题；列表→研究页路由与返回

## Answer

- **端点形态**：单端点 `GET /api/v1/fixtures/{id}/research`（而非单独 per-book odds 端点）——研究页首屏一跳拿全（逐书 H/D/A+捕获时间、去水共识、模型概率/EV、资格判定、开赛信息），少一次往返；SQL 归属：`eu_book_quotes`/`fixture_detail` 在 data 包，`latest_forecast` 在 modelling 包（ADR-0008，import-linter KEPT）。
- **共识口径**：与场次列表页逐字一致（per-selection book 集合 → 均价 → Shin），同 fixture 两页不出现两个共识；`consensus.books` 沿用列表页口径（各向 book 数取最大）。
- **模型区**：`latest_forecast`（track=ml，(issued_at,id) 最新，与前瞻冻结规则同序）→ payload 重建 ScoreMatrix → had 概率；模型 EV = 模型概率 × 竞彩价 − 1（显式标注与共识 EV 不同源）。无 Forecast 诚实为 `model: null`（UI "暂无模型预测——仅五大联赛覆盖"），不造假。
- **偏差高亮**：前端计算（归一化隐含 − 共识 ≥5 个百分点琥珀 + ↑/↓ 方向 + title 明细），阈值是展示层关注点不进 API；红涨绿跌只用于 EV 数字，偏差走琥珀（票 04 编码硬约束延续）。
- **前端结构**：共享原语抽 `components/had-quote-ui`（EligibilityBadge/OddsButton/isPickable/makeLeg 等 + `PickableFixture` 最小结构类型），列表页与研究页两页编码逐像素一致；选注篮交互复用场次页模式（常驻条+Drawer+同一套规则前置消息），类型放宽到 PickableFixture 两页共用。
- **404 vs 后端不可用**：`fetchFixtureResearch` 专用 fetch 把 HTTP 状态挂上错误对象（openapi-fetch 默认 error 只有 body）——404（场次不存在/旧后端无端点）走 no-data 空态给"返回场次"，5xx/网络错误走 backend-unavailable 给重试。smoke 双路径断言兼容 8000 常驻旧后端（旧后端对该路径 404 → 空态也算过）。
- **导航**：`/fixtures/$id` 与 `/fixtures` 平级路由；一级导航高亮改前缀归属（研究页"场次"保持 aria-current）；列表对阵 cell 与可投卡片标题可点进，研究页"← 返回场次"。
- **词典**：补"模型概率与模型 EV"（model-prob）与"书价偏差"（book-deviation）两词条，三要素+caution 齐全（数据完整性单测强制）；glossary 计数断言 10→12（单测+smoke 同步）；研究页指标名接 GlossaryTerm（去水共识/books/模型概率与模型 EV）。
- **e2e**：paper-loop 新增研究页步骤（demo 种子 fixture 1 三本书行 + 共识 + 模型诚实占位 + 返回），闭环 9→10 步全绿；smoke 新增研究页双路径测试 + /fixtures/1 进 axe 基线循环（21 过）。
- **质量门**：后端 261 过（含 contract 无漂移测试）、ruff/basedpyright strict/import-linter 绿；web lint/type/117 单测/coverage 94.95%/build 绿；redocly recommended-strict 过（豁免未动）；smoke 21 + paper-loop 10 全绿（E2E_WEB_PORT=5273 避开主工作区）。
- **提交**：`feat(api)`（6729e13）与 `feat(web)`（29bbf55）分 commit，worktree `.scratch/wt-wb`（分支 feat/wb-01-fixtures）。
- **给 03（玩法轴）的衔接点**：① 选注篮交互与 `PickableFixture`/`Leg` 原语已抽 `components/had-quote-ui`，推荐组合"一键带入选注"可直接复用（basket 状态管理模式见两页）；② EV×置信排序需要逐场 EV——`/fixtures/today?days=N` 已带行内 EV，玩法页可直接复用该端点；③ 研究页共识口径与列表页同源，03 若需"机会分"不必另立共识计算；④ 模型概率目前仅研究页露出（`latest_forecast`），03 进球类（ttg/crs）推导沿 `forecast_matrix_from_payload` 同入口。
