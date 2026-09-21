# 01 · 前端基础选型验证（shadcn/ui + ECharts 引入方式）

Type: research
Status: resolved

## Question

在 goalx 现有栈（React 19 + Vite 8 + Tailwind v4 + Biome + bun isolated linker + vitest/jsdom + MSW + Playwright）下，shadcn/ui 模式（Radix 原语 copy-in）与 ECharts 按需引入是否可行、标准引入方式是什么？产出需覆盖：

1. 兼容性结论：React 19 与 Tailwind v4 双向（shadcn/ui 官方 Tailwind v4 支持状态；cn 工具链是否必须）
2. 依赖清单：Radix 包粒度（按原语拆包）+ 需要 copy-in 的脚手架文件
3. 与 Biome 约定的冲突：shadcn 生成代码默认 Prettier 风格（空格缩进/单引号），转 Biome（tabs/双引号/width 120）的处理方式
4. ECharts 按需 tree-shake 的标准做法（echarts/core + 具体图表/组件注册）与 React 封装方式
5. 测试策略：jsdom/MSW 下 Radix 组件（portal/pointer 事件）与 ECharts（canvas）的 vitest 处理；Playwright e2e 是否不受影响
6. 已知坑：bun isolated linker 下新增依赖的注意事项（对照 AGENTS.md 记录的两条教训）

结论回填本票 `## Answer`；详细研究笔记写入本目录 `research/01-frontend-stack.md`。

## Answer

**可行性判定：可行，无阻塞性冲突。** shadcn/ui 官方全量支持 React 19 + Tailwind v4（[react-19](https://ui.shadcn.com/docs/react-19)、[tailwind-v4](https://ui.shadcn.com/docs/tailwind-v4)），React 19 peer 冲突仅影响 npm（bun 只警告）；仓库的 `@` 别名三处齐备、globals.css 已是 v4 CSS-first，前置条件全满足。ECharts 6.1.0 按需引入为官方一等模式（[echarts 手册](https://echarts.apache.org/handbook/en/basics/import/)），自写薄 hook 封装即可。

1. **兼容性**：shadcn "full support for React 19 and Tailwind v4"；Radix 2024-06 起全量兼容 React 19 且 2026 年仍活跃发 React 19.2 修复（[Radix releases](https://www.radix-ui.com/primitives/docs/overview/releases)）。**2026 关键变化：CLI 默认基座是 Base UI，必须显式 `--base radix`** 才拿到 map 决策的 Radix 基座（[cli](https://ui.shadcn.com/docs/cli)）。
2. **依赖清单**：`radix-ui`（统一包，2026 现状，不再按 `@radix-ui/react-*` 拆装——shadcn registry 实测依赖 `["cn","radix-ui"]`）、`cn`（shadcn 第一方新包 v0.3.0，clsx+tailwind-merge 的替代；保守回退 clsx+tailwind-merge@^3）、`lucide-react`（默认图标）、`echarts@^6`；copy-in 产物为 `components.json` + `src/components/ui/*.tsx` + globals.css token 注入。
3. **Biome 冲突**：CLI 无格式化集成（[issue #6882](https://github.com/shadcn-ui/ui/issues/6882) open），生成代码是 2 空格/无分号——**每次 `shadcn add` 后跑 `bun run lint:fix` 重排**即可，双引号/noExplicitAny/无 forwardRef 天然不冲突；不要把 `src/components/ui/**` 加 biome 豁免。
4. **ECharts**：`echarts/core` + `echarts.use([...图表/组件/CanvasRenderer])` + `ComposeOption` 类型收敛；React 封装自写 `use-echarts` hook（~40 行，init/dispose/setOption/ResizeObserver），不引入 echarts-for-react（虽仍在发版 3.0.6/2026-01，但 wrapper 对按需注册控制力弱）。
5. **测试**：jsdom 三件套进 `src/test/setup.ts`——pointer-capture mock（Radix Select/Slider 崩溃根因，[radix#1822](https://github.com/radix-ui/primitives/issues/1822)）、`ResizeObserver` stub、echarts 走纯函数 option builder + `vi.mock`（不装原生 canvas 包，规避 isolated linker 原生依赖风险）；portal 无需处理；Playwright 真浏览器全部不受影响，axe 断言反而更真实。
6. **bun isolated**：新依赖均纯 JS 包无特殊风险；add 产生的 lockfile 变更同 commit 提交（`task bun-install --frozen-lockfile` 必须仍过）；AGENTS.md 两条教训（jest-dom 入口、openapi-fetch 捕获）与新组件无交集；建议固定 shadcn CLI 版本保证生成可复现。

**主要风险**：忘 `--base radix` 装错基座（高）；`cn` 包 v0.x 很新（中，有等价回退）；copy-in 组件稀释 coverage 85 门槛（中，实现票决定是否 exclude `src/components/ui/**`）；`"use client"` 残留与 `@import "shadcn/tailwind.css"` 落盘行为未实测（低，init 后核对）。

详见 [research/01-frontend-stack.md](../research/01-frontend-stack.md)（含逐步引入清单与全部来源）。

## Comments

- 2026-09-16 用户裁决：**组件基座由 Radix 改为 Base UI**（地图 Notes 已同步）。Answer 中基座相关的条目按此换算：不再需要 `--base radix`（CLI 默认即 Base UI）；依赖 `radix-ui` → `@base-ui/react`；风险"忘 `--base radix` 装错基座"消除。改判理由：greenfield 零切换成本；Base UI 1.0 稳定（2025-12）、原 Radix 团队全职维护、2026-07 起 shadcn 默认基座（社区新项目 2:1 选择）；Combobox/Number Field/Drawer 恰合本项目需求（开奖场次选择、赔率金额录入、选注篮侧栏），Radix 独有的 Context Menu/Hover Card/Toast 本项目用不上。API 差异注意：`asChild` → `render`，网上旧示例需翻译。研究笔记中 Radix 细节保留作为回退参考。
