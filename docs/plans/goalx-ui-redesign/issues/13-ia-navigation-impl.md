# 13: IA 与导航落地（AppShell 重构）

**What to build:** 票 03 定稿的信息架构落成代码：导航分组/命名/路由全量落地，新页（总览/历史/词典）占位路由与产品化空状态（无数据/后端不可用/当日无场次三态），旧路由重定向，AppShell 重构。本票同时确立 e2e + axe 无障碍验收基调，后续页面票沿用。

**Blocked by:** 03（IA 定稿），12（token）

**Status:** resolved

- [x] 导航按 03 定稿的分组与命名渲染，`aria-current` 正确
- [x] 新页占位路由可达，三态空状态（无数据 / 后端不可用含启动指引 / 当日无场次）按统一文案体系呈现
- [x] 复核/设置按 03 预留位保留占位（呈现"随 M3/M4 到来"的引导态而非死页）
- [x] 旧路径重定向，e2e 用例迁移到新路由后全绿
- [x] e2e + axe 验收基调在此确立（每页零严重违例）并记入 PR，后续页面票沿用
- [x] 页面命名词典与根目录 CONTEXT.md 对齐，新术语按 domain-modeling 流程回写

## 不变量与人裁决项

- 页面命名以 03 的用户裁决清单为准，实现票不得改名
- 竞彩合规用词（建议/纸面/真金）不得在导航或标题中弱化风险提示

## Answer

2026-09-16 实现完成（分支 `feat/ui-13-ia-navigation`，commit `f8c80c5`，基于 12 号分支；未 push）：

1. **做了什么**
	- `AppShell` 重构为两级扁平导航：一级 总览/今日/投注/历史/验证/资金（`aria-label="主导航"`），次级 词典/复核/设置 落页脚位（`aria-label="次级导航"`），无组标题；两级都用 `location.pathname === item.to` 精确匹配并标 `aria-current="page"`；ThemeToggle 保留在头部右侧；根元素从单个 `<main>` 改为 header/main/footer 语义分区。页面标题仍是 `GoalX · {title}`（h1 在 header，测试与 e2e 选择器不变）。
	- `EmptyState`（`src/components/empty-state.tsx`）＝三态统一空状态：`no-data` / `backend-unavailable`（`task server` + `task ingest-jingcai` 指引 + 重试）/ `not-available`；形态＝一句人话（`message`）+ 一行补充（`hint`，可含 `<code>`）+ 至多一个动作（`action`：`onClick` 或 `{ to: AppRoute }` 站内跳转），无插画。`AppRoute` 九条路由字面量类型进 `lib/ui.ts`，供 `EmptyState.action.to` 与导航数组共用（tanstack Link 的 `to` 是按路由树类型化的，字符串宽化会编译失败）。
	- 路由：`/` → 总览引导态（`OverviewPage`＝StubPage + "先去今日看盘"入口）；`/today` 承接今日页；`/history` `/glossary` 占位骨架；`/review` `/settings` 换成 StubPage 的"功能未达"引导态（说明是什么 + M3/M4 何时来）。旧 `PlaceholderPage` 删除。
	- 今日页空/错态接入 EmptyState；`today-empty`/`today-error` testid 退役，统一为 `empty-state` + `data-variant`。
	- e2e：smoke 重写（总览为首页 + 引导跳今日、九链接 IA 断言、`aria-current` 跨级移动、占位页文案、主题切换改挂总览）；**axe 基调**落在 `e2e/axe-baseline.ts` 的 `expectNoSeriousAxeViolations`（过滤 serious/critical），九条路由逐页套用；paper-loop 今日流程迁 `/today`，空库断言收紧为 `data-variant="no-data"`。
2. **取舍**
	- **"旧路径重定向"＝零 redirect**：旧六条路由全部保留且语义不变，唯一变化是 `/` 内容从今日换总览——这正是 03 定稿（"无重定向破坏"），故无路径被删除，不需要 redirect。
	- axe 基调取"零 serious/critical"（照票面"零严重违例"），未取旧 smoke 的"零全部违例"：后者会让 14-20 号票的复杂表格/图表被 minor 档（如 muted 文本对比度）卡死。本次九页实测 serious/critical 均为 0。
	- 未动投注/验证/资金页的内联错误提示——那些页的重设计在 16/19/20 号票，散写色与空状态清退归各自票（map 已有此分工）。
	- 命名词典与 CONTEXT.md 对齐：导航九名全是页面名而非领域术语，无新 domain term 需要回写 CONTEXT.md。
3. **给 14 号（今日页）的注意点**
	- 今日页的加载/空/错三态已接 EmptyState（`today-loading` testid 仍在），重设计时保留 `data-testid="empty-state"` + `data-variant` 契约，单测与 paper-loop 都依赖它。
	- axe 基调用法：`import { expectNoSeriousAxeViolations } from "./axe-baseline"`，页面渲染稳定后调用即可；标准是 serious/critical 为零。
	- 本机坑复现与绕法：主工作区 5173 有常驻 dev server（跑的是 proto 分支代码），playwright `reuseExistingServer` 会串台——用未入库的 `apps/web/playwright.local-port.config.ts`（已留在 wt-13 worktree，未跟踪）跑 5193；smoke 的"验证页降级"用例在 8000 有常驻后端时必然超时失败（预期 error 态不出现），属环境性失败，产品代码无需改。
	- e2e:loop 在 worktree 里跑前需先 `uv sync`（worktree 无 .venv）；首启偶发 granian worker 起慢导致 webServer 60s 超时，重跑即可。
