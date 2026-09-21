# 01: 场次列表 3 日化

**What to build:** 今日页升级为"场次"页：展示今天起 3 天的在售场次（按日分组/切换），数据已入库，端点加日期范围参数（contract 增量），列表交互（排序/跳研究页入口留位）。

**Blocked by:** None

**Status:** resolved

- [x] fixtures 端点支持 days/日期范围参数（contract-first 流程）
- [x] 场次页按日分组或日期 Tab，默认今天；`/today` 路由让位（重定向到新路由）
- [x] 现有 e2e/单测迁移；axe 与空态三态延续

## Answer

- **参数形状**：`GET /api/v1/fixtures/today?days=N`（默认 1，1–7）+ 行内新增 `business_date` 字段。选 `days` 而非 `date_from/date_to`：sqlite 验证 match_codes 按 business_date 离散分布（09-17 有 11 场、09-18 有 14 场），"今天起 N 天"语义即窗口本身，改动最小且零破坏（旧客户端不传 days 行为不变）。
- **前端形态**：日期 Tab（fieldset + aria-pressed 按钮，沿历史页模式先例）而非长页按日分组——票 14 的"可投卡片置顶+全量表"分层结构按日原样保留，默认今天；Tab 计数随行内 business_date 推导，空窗日保留 Tab 并诚实占位（"后两日场次随开售逐步入库"）。
- **Tab 日期推导**：前端按北京时区算今天起 3 天 ∪ 数据实际出现的业务日（时钟偏差不丢单）；行内 `business_date` 缺失（常驻旧后端双路径 e2e 场景）按今天兜底——实测旧后端无该字段曾炸 `dayLabel(undefined).slice`，兜底后降级正确。
- **路由让位**：`/fixtures` 承接（component），`/today` 保留为 beforeLoad 重定向（书签不断）；导航"今日"→"场次"，总览四处跳转与文案同步（"去今日"→"去场次"）。testid `today-*` → `fixtures-*` 全量随迁（e2e 契约显式迁移，纸面闭环 9/9 断言同步）。
- **选注篮跨日**：篮不按日清空（2串1 跨日组合合法，服务器校验唯一权威）。
- **MSW**：默认 handler 按 `days` 查询参数过滤（与真实 API 同口径），新增 fixture 4（明天，可投）驱动 Tab 测试；单日消费方（总览/投注/历史）不看到跨日数据。
- **排序/跳研究页留位**：排序未做（列表按开赛时间排序已可用，玩法轴票 03 的 EV×置信排序才是主排序场景）；研究页入口由票 02 的行点击补上（避免 01 先上死链）。
- **质量门**：后端 257 过（coverage ≥90 门保持）；web lint/type/test:coverage(96.8%)/build 绿；e2e smoke 19 过（含 /today 重定向新断言）+ paper-loop 9/9；ruff/biome/import-linter（ADR-0008 契约 KEPT）全绿。
- **提交**：`feat(api)` 后端+contract+codegen（63ed832）与 `feat(web)` 前端（d2c6f32）分 commit，在 worktree `.scratch/wt-wb`（分支 feat/wb-01-fixtures）。
