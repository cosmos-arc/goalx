# 12: 设计系统 token 与双主题落地

**What to build:** 票 02 定稿的设计语言落成代码：token 进 CSS variables + Tailwind v4 `@theme`，浅/暗双主题全应用生效、切换持久化；AppShell 基础样式改吃语义 token。现有页面不要求重设计，但在双主题下不得破损。

**Blocked by:** 02（token 定稿），11（地基）

**Status:** resolved

- [x] token 文件按 02 定稿结构落地：语义 token（含盈亏/资格状态色，红涨绿跌按 02 裁决）、功能色、可视化色板、密度分层（专业表格/卡片两档）
- [x] 浅/暗切换控件可用，选择持久化（重载保留）
- [ ] 全应用无漏色：页面与组件不出现散写色值，仅语义 token（AppShell/新组件已语义化；现存四页散写色按执行约束保留，待 13-18 号重设计票清退）
- [x] 数据可视化色板在浅/暗两套下各自可读（按 02 定稿值）
- [x] 现有四页在双主题下可用（无破损、无不可读对比度）
- [x] e2e 含主题切换冒烟；`task check` 全绿（e2e 1 例环境性失败，见 Answer）

## 不变量与人裁决项

- token 结构单一事实源；组件/页面只允许语义 token，不允许原始色值
- 02 未裁决项（如具体色值偏好）不得由实现票代决，回报 02 会话

## Answer

2026-09-16 落地（分支 `feat/ui-12-design-tokens`，堆叠于票 11 分支之上）：

**做了什么**

1. **token 落地**（`apps/web/src/styles/globals.css`，纯叠加不动票 11 结构）：
   - 字体栈 `--font-sans` 补中文平台回退（PingFang SC / HarmonyOS Sans SC / MiSans / Microsoft YaHei / system-ui）。
   - 钱层 `--profit`（red-600/400）`--loss`（green-600/400，暗色用 02 给定 green-400 值）+ `@theme inline` 映射 `--color-profit/--color-loss`。
   - 界面层 `--warning`（amber-600/400）、`--info`（blue-600/400，可投徽章）、`--success`（green-600/400，补齐）+ 映射；destructive 原样。
   - 图表五色 chart-1..5 灰阶 → blue/orange/teal/indigo/slate，浅 600 暗 400，取自本仓 tailwindcss 4.3.3 theme.css 参考值。
   - `--radius` 0.625rem → 0.5rem；:root/.dark 补 `color-scheme`（原生控件/滚动条随主题）。
2. **密度变体**：`shadcn add table` copy-in 后加 `data-density="compact"`（13px / th h-auto + th/td py-1.5），实现放 table.tsx 内的 Tailwind 变体类（`.cls[data-density=compact] th/td` 特异性高于 p-2/h-10 工具类；globals.css components 层会被 utilities 层盖掉，故不进 CSS）。默认舒适档零改动。
3. **主题切换**：`src/lib/theme.ts`（resolve/apply/persist，localStorage key `goalx-theme`，回退 prefers-color-scheme，localStorage 不可用静默降级）+ `src/components/theme-toggle.tsx`（lucide Sun/Moon，ghost icon-sm 按钮，aria-label/aria-pressed）+ main.tsx 首帧渲染前 `applyTheme(resolveTheme())` 防闪错主题。
4. **AppShell 语义 token 化**：bg-white/text-neutral-900 → bg-background/text-foreground，导航 active → bg-primary/text-primary-foreground，hover → bg-muted；头部右侧挂 ThemeToggle（13 号票会重排）。
5. **ui.ts** 暴露 `TABULAR_NUMS = "tabular-nums"`（票 02：等宽+正负号是盈亏可读性主承载，颜色仅辅助）。
6. **测试**：theme.test.ts（持久化优先/跟随系统/非法值回退/applyTheme）+ theme-toggle.test.tsx（RTL+localStorage mock，双向切换+持久化断言）；e2e 加主题切换冒烟（切暗 → html.dark → reload 保留）。

**关键取舍**

- 新语义 token 不配 `-foreground` 对：02 未裁决实底前景色，组件用 `bg-x/10 text-x` 透明度模式（与 button destructive 同构）；实底需求回 02 裁决。
- 存量四页散写色（emerald/amber/red/neutral 等）按本票执行约束保留不动（纯叠加、别大改页面），双主题下可用但不完美（如浅色徽章在暗底上是亮片），清退归 13-18 号重设计票。
- 主题不发 React context：单一消费者（ThemeToggle），`<html>.dark` class 即事实源，重载/多标签页天然一致。

**质量门**：web-lint / web-type / web-coverage（42/42，四项 ≥85 门禁过）/ web-build / e2e-loop（9/9）全绿；web-e2e 4/5——`validation page degrades gracefully without backend` 环境性失败（本机 8000 有票 37 常驻 goalx-backend，页面拿得到数据故不进 error 态；lsof/curl 确认，基础分支同样失败，非本票回归）。
