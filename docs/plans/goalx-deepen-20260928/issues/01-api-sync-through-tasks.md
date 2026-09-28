# 01 api：同步触发端点接线 tasks——关掉「按钮走退役源」

Status: resolved
Blocked by: None（可立即开工）

**交付（用户视角）**：web 上两个手工同步按钮（彩池同步、赛果同步）与后台定拍跑同一
条路、同一个源——点按钮不再从退役/过期路径写库。

## 现状

两个手工同步按钮各自直调 ingest，与定拍跑的是**不同 implementation**：

- `apps/backend/src/goalx_backend/api/pool.py:397-407`：`POST /api/v1/pool-sync/run`
  在 router 里开 `polite_client()` 直调 `zucai.sync_pool_data`（旧 okooo 路径，
  2026-09-26 三源停采后属备源）；定拍 `tasks.pool_snapshot`（tasks.py:486-490）走
  票 68 官方化的 `zucai_official.sync_current`。同一批 pool 域表，按钮和定拍从两个源写。
- `apps/backend/src/goalx_backend/api/results.py:350-371`：`POST /api/v1/draw-sync/run`
  直调 `uniform.sync_uniform_results`；定拍 `tasks.draw_results_sync`（tasks.py:319-347）
  跑的是票 76 链（映射刷新→源T 物化→uniform 降审计）。按钮冻结在切源之前。

这正是 tasks.py 模块文档（1-10 行）声称已消灭的漂移类：「Prefect flow 与 cli 命令是同一
任务的两个 adapter」——api 路由是没接线的第三个 adapter。附带问题：router 从 ingest
模块 import `polite_client`（oddsapi.py:185-190），delivery 层下探采集内部件。

## 方案

两个端点改调对应 tasks 函数（pool_snapshot / draw_results_sync）；router 只保留 HTTP
形状（异常→502 映射、状态视图装配）。触发即触发，不再自带实现。

## 实现决策

- 调用点换 tasks.*；缺的注入口（如 now）在 tasks 函数签名上补齐，不在 router 里拼装。
- `_sync_status` / `_run_view` 等状态视图留在 router——它们读库、属响应装配，不属任务。
- 验收项之一：`polite_client` 不再被 api 层 import（两个调用点随本票消失）。

## 测试接缝

- 最高 seam：FastAPI TestClient 打两个 POST 端点，monkeypatch tasks 函数断言被调、
  502 映射仍成立（先例：tests/unit/test_api*.py 家族）。
- tasks 函数本身已有单测；不测 router 私有函数。

## 范围外

- 不改 tasks 既有函数语义；契约零 diff（端点请求/响应形状不变）。
- 其他 router 的同类形态（evidence/markets 的装配）不在本票。

## 不变量与人裁决项

- 不变量：按钮触发与定拍触发走同一 implementation、同一源（票 68/76 裁决的执行面）；
  contract 零 diff。
- 人裁决：无——两裁已有记录，本票纯执行。

## 验收

- [x] 两端点分别调用 tasks.pool_snapshot / tasks.draw_results_sync（测试断言委派）
- [x] api 层对 `data.ingest` 内部件（polite_client 等）的 import 归零
- [x] 502 映射与状态响应形状不变，contract 零 diff（形状零 diff；两端点
  summary/description 文案对齐真实链路——旧文案描述退役源，保留才是错）
- [x] `task test` + 全量 `task lint` 绿（771 过 / 覆盖 92.15%；importlinter 1 kept）

## Answer

PR #108（分支 refactor/deepen-01-api-sync-tasks，单提交 1488107）。两
端点改调 tasks 函数，router 只留 502 映射 + 状态视图；polite_client 自
api 层消失。双轴 review 三修正：① pool 502 元组补 RuntimeError
（zucai_official 对 success=false/无期次抛 RuntimeError，旧元组会 500
裸奔，参数化测试覆盖）；② 契约文案去内部模块路径（票号引用保留，契约
既有惯例）；③ GET status 时钟口径统一。

**超出票面的接线**（review 判定方向一致）：importlinter 把 tasks 从
`api | tasks | server` 带拆出、独立成层降到 api/server 之下——同层互
import 禁令挡住委派，而「api 是 tasks 的第三个 adapter」正是本票语义；
执法记录落 ADR-0008 执法节 2026-09-28 补。测试面把原 router 缝的 e2e
集成挪到 tasks 缝（test_draw_results_sync_chain_uniform_fallback：语料
桥缺席降级 uniform 独走），断言不缩水。

## Comments
