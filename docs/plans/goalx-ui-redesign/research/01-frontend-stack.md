# Research 01 · 前端基础选型验证（shadcn/ui + ECharts）

Ticket: [issues/01-frontend-stack-research.md](../issues/01-frontend-stack-research.md)
Date: 2026-09-15。外部事实均标注来源 URL；仓库事实标注本地路径。标注「不确定」处为未能从一手来源完全核实项。

## TL;DR

**可行。** shadcn/ui 官方全量支持 React 19 + Tailwind v4（new-york-v4 style），与仓库现有栈（Vite 8 / Biome / vitest+jsdom+MSW / bun isolated）无阻塞性冲突；ECharts 6 按需引入走 `echarts/core` + `echarts.use()` 官方模式，React 封装自写薄 hook（~40 行）优于第三方 wrapper。两个需要流程纪律的点：每次 `shadcn add` 后跑 `bun run lint:fix` 重排格式（CLI 不集成 Biome，GitHub issue #6882 仍 open），以及 jsdom 测试需要 pointer-capture mock + ResizeObserver stub + echarts mock（或 vitest-canvas-mock）。

---

## 1. 仓库现状（本地事实）

| 事实 | 值 | 来源 |
|---|---|---|
| React | 19.2.4（`^19.2.4`） | `apps/web/package.json` |
| Vite | 8.0.2 + `@vitejs/plugin-react` 6.0.1 | 同上 |
| Tailwind | v4.2.2，经 `@tailwindcss/vite` 插件接入；无 tailwind.config，CSS-first | `apps/web/package.json`、`apps/web/vite.config.ts` |
| 全局 CSS | 仅一行 `@import "tailwindcss";`（挂在 `src/styles/globals.css`，main.tsx 引入） | `apps/web/src/styles/globals.css`、`apps/web/src/main.tsx` |
| 路径别名 | `@` → `./src`，vite / vitest / tsconfig.base 三处齐备（shadcn 前置条件已满足） | `apps/web/vite.config.ts`、`apps/web/vitest.config.ts`、`apps/web/tsconfig.base.json` |
| Biome | 2.5.12：tabs、width 120、双引号、分号、`noExplicitAny: error`、organizeImports on；`src/api/generated/**` 豁免 | `apps/web/biome.json` |
| 测试 | vitest 4.1.11 + jsdom 29 + MSW 2.12.14；setup 走 `@testing-library/jest-dom/matchers` 入口（isolated linker 教训）；coverage 门槛 85 | `apps/web/vitest.config.ts`、`apps/web/src/test/setup.ts` |
| e2e | Playwright 1.62.1 + `@axe-core/playwright` 4.13.0 | `apps/web/package.json` |
| 安装 | `bun install --frozen-lockfile`（workspace，isolated linker） | `Taskfile.yml` |
| 已知坑两条 | jest-dom 必须 `/matchers` 入口；openapi-fetch 在 `createClient()` 时捕获 `globalThis.fetch` | `AGENTS.md` |

注意：web 的 coverage 门槛是 **85**（`vitest.config.ts` thresholds），不是 map 里写的后端 90。copy-in 的 shadcn 组件会进 `src/components/ui/`，若计入覆盖率先行分母会拉低通过率——实现票需决定是否在 coverage exclude 中加 `src/components/ui/**`（见 §7 风险）。

## 2. shadcn/ui × React 19 × Tailwind v4 兼容性

- **官方声明全量支持**："We have added full support for React 19 and Tailwind v4 in the latest release"。来源：https://ui.shadcn.com/docs/react-19 （页面提示可能过时，但结论被 tailwind-v4 页与现行 registry 佐证）
- **Tailwind v4 迁移内容**（2025-03 完成，现行状态）：全部组件面向 TW v4 + React 19 重写；`forwardRef` 全部移除（改 `React.ComponentProps` + 具名函数 + `data-slot`）；HSL → OKLCH；`tailwindcss-animate` → `tw-animate-css`；CSS 变量结构为 `:root`/`.dark` 原始 token + `@theme inline` 引用。来源：https://ui.shadcn.com/docs/tailwind-v4
  - 与本仓库的契合点：仓库 biome 的 `noReactForwardRef: warn` 与新组件风格（无 forwardRef）天然一致，不会告警。
  - `:root` + `.dark` 双 token 结构正好是 map 决策「浅/暗双主题」的标准载体；2026-03 CLI v4 的 `init -t vite` 模板默认带暗色模式。来源：https://ui.shadcn.com/docs/changelog/2026-03-cli-v4
- **React 19 peer 依赖问题只影响 npm**：ERESOLVE 是 npm 独有；"PNPM and Bun just show a silent warning"。仓库用 bun，无此问题。来源：https://ui.shadcn.com/docs/react-19
- **Vite 安装路径**（Existing project 流程，仓库全部前置条件已满足）：`tsconfig` paths + vite alias → `bunx shadcn@latest init` → `bunx shadcn@latest add button`。2026 现状：`init` 会安装依赖、写入 cn 工具、配置 CSS 变量，并在 CSS 里加 `@import "shadcn/tailwind.css"`（shadcn 包内置的共享 TW v4 utilities/动画）。来源：https://ui.shadcn.com/docs/installation/vite 、https://ui.shadcn.com/docs/cli
- **2026 新变化：组件基座可选**。`init --base <base>`（radix | base(Base UI) | aria），CLI 文档默认示例混用 Base UI；**map 已决策 Radix 基座，安装时必须显式 `--base radix` 或选 radix 变体**，否则拿到的是 Base UI 组件（默认 `docs/components/dialog` 页面就是 Base UI 变体，Radix 在 `/docs/components/radix/dialog`）。来源：https://ui.shadcn.com/docs/cli 、https://ui.shadcn.com/docs/changelog/2026-03-cli-v4 、https://ui.shadcn.com/docs/components/dialog vs https://ui.shadcn.com/docs/components/radix/dialog
- **components.json**：`style: "new-york"`（唯一现行 style）；`tailwind.config` 在 v4 留空；`css` 指向 `src/styles/globals.css`；aliases 用现有 `@/` 约定。来源：https://ui.shadcn.com/docs/components-json

### cn 工具链是否必须

**cn() 必须有**——所有生成组件都 `import { cn }` 合并 class；但底层实现 2026 年已换代：

- 现行 registry（new-york-v4）的 dialog 依赖声明是 `["cn", "radix-ui"]`，生成代码 `import { cn } from "cn"`。来源：直接抓取 https://ui.shadcn.com/r/styles/new-york-v4/dialog.json （一手 registry payload）
- `cn` 现在是 **shadcn 第一方独立 npm 包**（repo `github.com/shadcn-ui/cn`，maintainer shadcn，v0.3.0，2026-09-12 发布），自述 "Fast, small, compiled class-name merging for Tailwind CSS. Drop-in replacement for clsx + tailwind-merge."。来源：https://registry.npmjs.org/cn
- 传统组合 clsx + tailwind-merge 仍受支持：CLI 的 `migrate cn` 明说 "cn merge engine supports Tailwind CSS v4, like tailwind-merge v3"。来源：https://ui.shadcn.com/docs/cli
- **建议**：跟随 CLI 默认装 `cn` 包（shadcn 第一方、零依赖）；如实现票时对 v0.x（发布仅数天、迭代快：0.1→0.3 十个版本）有顾虑，回退 clsx + tailwind-merge@^3 是等价保守项。标注：`cn` 包的编译期合并行为与 tailwind-merge 的运行时差异未实测——不确定，首张实现票落地时验证。

## 3. Radix 依赖形态：拆包 → 统一包

- shadcn 现行 new-york-v4 registry 依赖**统一包 `radix-ui`**，生成代码为 `import { Dialog as DialogPrimitive } from "radix-ui"`（按原语命名空间导入）。来源：https://ui.shadcn.com/r/styles/new-york-v4/dialog.json 的 dependencies 与 files[].content
- Radix 官方（2025-01-22）发布统一包并**推荐**之："We recommend installing the radix-ui package and importing the primitives you need... prevent version conflicts"；tree-shakable。2026-07-20 起还加了按原语子路径入口（`import { Accordion } from "radix-ui/accordion"`）。来源：https://www.radix-ui.com/primitives/docs/overview/releases 、https://www.radix-ui.com/primitives/docs/overview/introduction
- 旧粒度 `@radix-ui/react-dialog` 等 scoped 包仍存在可用（historically shadcn 就是这么装的），但对新项目无收益：统一包 + tree-shaking 等价、且免去 20+ 条 package.json 条目与版本冲突。**清单问题因此消解：装一个 `radix-ui`，用到什么原语 import 什么。**
- React 19 支持：Radix 2024-06-19 起全量兼容 React 19，且 2026 年仍在发 React 19.2 相关修复（6/30 composed ref 循环、7/6 stale onEscapeKeyDown 等）——维护活跃。来源：https://www.radix-ui.com/primitives/docs/overview/releases
- copy-in 脚手架文件清单（CLI 自动生成）：`components.json`、`src/lib/utils.ts`（或 cn 包直引，取决于 CLI 版本行为——不确定，安装时核实）、`src/components/ui/<component>.tsx` 每组件一份、globals.css 的 token 注入。来源：https://ui.shadcn.com/docs/cli （"installs dependencies, adds the cn util and configures CSS variables"）

### 需要新增的 npm 依赖（shadcn 侧）

| 包 | 说明 |
|---|---|
| `radix-ui` | 统一原语包（shadcn registry 声明的依赖） |
| `cn`（或 clsx + tailwind-merge@^3） | class 合并，见上节 |
| `lucide-react` | 生成组件的默认图标库（dialog 的 XIcon 等；iconLibrary 可在 init 时选） |
| `shadcn`（CLI 包，devDep 语义上由 `bunx` 拉起即可） | init 向 globals.css 加 `@import "shadcn/tailwind.css"`，该 CSS 来自此包 |

## 4. shadcn 生成代码 × Biome 冲突处理

**事实**（一手 registry payload）：生成代码是 2 空格缩进、无分号、双引号、独立 import 块（`"use client"` 头、`@/registry/...` 占位 alias 安装时重写为项目 alias）。仓库 Biome 是 tabs + 分号 + width 120 + organizeImports。**缩进与分号直接冲突；import 顺序会被 organizeImports 重排。**

**CLI 侧无格式化集成**：CLI 文档与 2026-03 CLI v4 changelog 均无 formatter/linter 选项；功能请求 "Add linter choice (ESLint/Biome)" 的 issue #6882 仍 open。来源：https://ui.shadcn.com/docs/cli 、https://ui.shadcn.com/docs/changelog/2026-03-cli-v4 、https://github.com/shadcn-ui/ui/issues/6882

**标准处理**（社区通行 + 本仓库已有设施）：

1. 每次 `bunx shadcn@latest add <component>` 后立即 `bun run lint:fix`（= `biome check --write .`，package.json 已有脚本）；纯格式重排无语义变化，diff 可安全 review。
2. 先用 CLI 的 `--dry-run` / `--diff` / `--view` 预览再落盘（CLI v4 新能力）。来源：https://ui.shadcn.com/docs/changelog/2026-03-cli-v4
3. 生成代码无 `any`、无 forwardRef，`noExplicitAny`/`noReactForwardRef` 不触发；double quotes 本就一致。
4. 不要把 `src/components/ui/**` 加进 biome 豁免——它是 copy-in 源码，本来就该被格式化与 lint 约束（与 `src/api/generated/**` 性质不同）。
5. `"use client"` 头：Vite 项目 components.json `rsc: false` 时旧版 CLI 会省略；v4 行为未从文档确认——**不确定**，安装后如残留由 lint:fix 不处理（非格式问题），首张实现票核实（若存在，直接手删或让 CLI 配置处理）。

## 5. ECharts 按需引入与 React 封装

### 官方 tree-shaking 模式（Apache 手册，现行 ECharts 6 适用）

```ts
import * as echarts from "echarts/core";
import { BarChart, LineChart } from "echarts/charts";            // 图表以 Chart 结尾
import { GridComponent, TooltipComponent, LegendComponent } from "echarts/components"; // 组件以 Component 结尾
import { LabelLayout, UniversalTransition } from "echarts/features";
import { CanvasRenderer } from "echarts/renderers";               // 渲染器必选其一

echarts.use([BarChart, LineChart, GridComponent, TooltipComponent, LegendComponent, LabelLayout, CanvasRenderer]);
```

- 子包：`echarts/core`（核心接口）、`echarts/charts`、`echarts/components`、`echarts/features`、`echarts/renderers`（Canvas/SVG 必须显式注册一个，只要 SVG 时 CanvasRenderer 不进包）。
- 类型：`ComposeOption<BarSeriesOption | TooltipComponentOption | ...>` 组合出精确 option 类型，能在类型层对上「已注册的图表/组件」。
- 来源：https://echarts.apache.org/handbook/en/basics/import/
- 版本：npm latest **6.1.0**（来源：https://registry.npmjs.org/echarts dist-tags）。

### React 封装方式

- **官方不提供 React 封装**；生态第一 wrapper `echarts-for-react`（hustcc）仍在发版：3.0.6（2026-01-21），peer `react: ^15 || >=16`、`echarts: ... || ^6`，React 19 无 peer 冲突。来源：https://registry.npmjs.org/echarts-for-react 、https://github.com/hustcc/echarts-for-react
- 但 wrapper 是薄运行时（init/setOption/resize 生命周期）+ 对按需注册的控制力弱（需要把 echarts 实例从外部传入才能 tree-shake），社区惯用做法与多个 2026 综述均指向**自写 ~40 行 hook**（`useRef` 存 dom、`useEffect` init/dispose、watch option 调 `setOption`、`ResizeObserver` 调 `resize`），完全掌控注册表与类型。来源（二手，方向一致）：https://medium.com/@saidheerajv/using-apache-echarts-the-og-of-charting-in-react-app-without-wrapper-package-41ed0e8f76ba 、https://blog.logrocket.com/best-react-chart-libraries-2026/
- **建议**：`src/components/charts/` 下自建 `use-echarts.ts` hook + `EChart` 组件 + 一个集中注册模块（`echarts.use` 只注册本项目用到的图表/组件，类型用 `ComposeOption` 收敛）。不引入 echarts-for-react。

### 需要新增的 npm 依赖（ECharts 侧）

| 包 | 说明 |
|---|---|
| `echarts` | ^6.1.0，从子路径按需 import，无其他运行时依赖 |

## 6. 测试策略（vitest/jsdom/MSW 与 Playwright）

### jsdom 缺什么（根因）

jsdom 官方 README：不实现布局（`getBoundingClientRect`/`offsetTop` 等无真实值）；canvas 需以 peer 方式另装 `canvas` 3.x 原生包，否则 `<canvas>` "behave like `<div>`s"（`getContext` 拿不到 2d context）；并明言 "has many missing APIs"。PointerEvent/`hasPointerCapture`/`ResizeObserver`/`matchMedia` 都不在实现列表。来源：https://github.com/jsdom/jsdom （README）

### Radix 组件在 jsdom 下的处理

- 已知崩溃点：Radix（Select/Slider 等）依赖 `Element.hasPointerCapture()`，jsdom 未实现 → `TypeError: target.hasPointerCapture is not a function`。官方追踪 issue：https://github.com/radix-ui/primitives/issues/1822 ；testing-library 维护者确认根因：https://github.com/testing-library/user-event/discussions/1087
- **处理**：`src/test/setup.ts` 加一次性 mock（仓库级，不必每测试文件重复）：

```ts
Object.defineProperties(window.HTMLElement.prototype, {
	hasPointerCapture: { value: () => false },
	setPointerCapture: { value: () => {} },
	releasePointerCapture: { value: () => {} },
});
```

  （方案出处：上述 issue/discussion 的通行解法；Qiita/SSO 同方案）
- portal 本身没问题：Radix Portal 渲染到 `document.body`，`@testing-library/react` 的 `screen` 查询全 document，天然可见；jsdom 下 portal 不受影响（无额外配置项）。
- 交互用 `@testing-library/user-event`（仓库已装 v14；v14 自带 pointer 事件实现，与上述 mock 配合即可开 Select/Dialog）。
- Radix 会给 `body` 设 `pointer-events: none`（overlay 打开时）——jsdom 不执行真实 hit-testing，一般无感，个别用例需注意（二手来源：https://momentic.ai/blog/libraries-and-heartbreak ）。
- matchMedia：Radix 原语本体用得少，若组件层用到再按需 stub（参考 Mantine vitest 指南的 setup 模式：https://mantine.dev/guides/vitest/ ）。

### ECharts 在 jsdom 下的处理

- Canvas renderer 在 jsdom 下 `getContext("2d")` 不可用 → init 即崩。三条路：
  1. **单测 mock echarts 模块**（推荐）：`vi.mock` 掉 `echarts/core` 的 `init`，断言 `setOption` 收到的 option——单测只验组件逻辑（数据→option 映射），不验渲染；
  2. `vitest-canvas-mock`（jest-canvas-mock 的 vitest 移植）setup 引入后 canvas 真跑（来源：https://www.npmjs.com/package/vitest-canvas-mock 、https://stackoverflow.com/questions/48828759/unit-test-raises-error-because-of-getcontext-is-not-implemented ）；
  3. 装原生 `canvas` 包——**不建议**：bun isolated linker 下原生依赖构建风险大、CI 复杂化（此为推断，非文档来源，标注不确定；但与仓库「避免原生依赖」直觉一致）。
- **推荐组合**：option 构造函数做成纯函数（`buildXOption(data): ECOption`）直接单测（零 DOM）；`use-echarts` hook 层用方案 1 mock；视觉/交互断言全部推给 Playwright e2e。
- 自写 hook 若用 `ResizeObserver` 做响应式，jsdom 无此 API → setup 里 `vi.stubGlobal("ResizeObserver", ...)` 空实现（官方推荐 `vi.stubGlobal`：https://vitest.dev/guide/mocking/globals ；通行模式：https://stackoverflow.com/questions/64558062/how-to-mock-resizeobserver-to-work-in-unit-tests-using-react-testing-library ）。
- MSW 层无交集：shadcn/ECharts 不发请求；现有 `onUnhandledRequest: "error"` 不受影响。

### Playwright 不受影响

e2e 在真实 Chromium 里跑，canvas/pointer/portal/layout 全部为浏览器原生实现，jsdom 的所有缺口都不存在；`@axe-core/playwright` 已在仓库，Radix 的 a11y 属性（dialog 焦点陷阱、aria）在真实浏览器里才能被 axe 正确评估。依据：jsdom README 的 "Unimplemented parts of the web platform" 仅限 jsdom 类环境（https://github.com/jsdom/jsdom ）。

## 7. bun isolated linker 注意事项（对照 AGENTS.md 两条教训）

1. **新增依赖必须走 lockfile 流程**：`shadcn add` 会调用包管理器装依赖（monorepo 下从仓库根目录用 `-c apps/web` 或进 apps/web 目录跑）；装完 `task bun-install`（`--frozen-lockfile`）必须仍然成立——即 CLI 产生的 lockfile 变更要随代码一起提交。来源：`Taskfile.yml`、https://ui.shadcn.com/docs/installation/vite （`add card -c apps/web` monorepo 用法）
2. **jest-dom 入口教训延续**：新增的测试基建继续用 `@testing-library/jest-dom/matchers` 入口，不因新组件库改变（来源：`AGENTS.md`、`apps/web/src/test/setup.ts` 现状）。
3. **openapi-fetch fetch 捕获教训不相关**（组件库/图表不发请求），但提醒：若未来给 ECharts 加数据层，测试里继续用现有 MSW 模式即可。
4. isolated linker 对本次新增包无特殊风险：`radix-ui`、`cn`、`lucide-react`、`echarts`、`vitest-canvas-mock` 均为纯 JS 包，无 bin/无原生模块（`echarts` 无 bin；来源：registry 元数据）。唯一要避免的是原生 `canvas` 包（见 §6）。
5. **新教训候选（提前记录）**：`bunx shadcn@latest` 每次拉最新 CLI（4.21.0，2026-09-04 发布，迭代快）；生成代码随 registry 演进，同一组件不同时间 add 的代码可能有差异——建议团队固定 CLI 版本（`bunx shadcn@4.21.0 ...`）并把 components.json 提交入库，保证可复现。来源：npm registry（shadcn 4.21.0）、推断部分标注不确定。

## 8. 引入步骤要点（给实现票）

1. （分支上）`cd apps/web && bunx shadcn@latest init --base radix`（交互选：style new-york、css `src/styles/globals.css`、baseColor neutral、cssVariables true、rsc false）；检查 globals.css：token 注入 + `@import "shadcn/tailwind.css"` 与现有 `@import "tailwindcss"` 合并正确。
2. `bunx shadcn@latest add button card ...`（Radix 基座组件，如 dropdown-menu/dialog/tabs/tooltip/select 等）；**每批 add 后立刻 `bun run lint:fix` + 肉眼 diff**。
3. 提交 `components.json`；把 CLI 触发的 package.json/bun.lock 变更与组件代码同一 commit。
4. `bun add echarts`；建 `src/components/charts/`（注册模块 + `use-echarts` hook + 纯函数 option builder）。
5. 扩 `src/test/setup.ts`：pointer-capture mock、ResizeObserver stub、（如走 canvas 路线）vitest-canvas-mock。
6. 决定 coverage exclude 是否加 `src/components/ui/**`（copy-in 组件不改不测，否则 85% 门槛被未测文件稀释；这是实现票的决策点）。
7. 全门禁：`task web-lint && task web-type && task web-coverage && task web-build && task web-e2e`。

## 9. 风险与坑汇总

| # | 风险/坑 | 等级 | 缓解 |
|---|---|---|---|
| 1 | `init`/`add` 默认基座是 Base UI（2026 变化），忘 `--base radix` 会装错组件族 | 高 | init 显式 `--base radix`；核对生成代码 import 是否 `"radix-ui"` |
| 2 | CLI 不做 Biome 格式化，生成代码 2-空格/无分号 | 中 | add 后必跑 `bun run lint:fix`；--diff 预览 |
| 3 | `cn` 包 v0.x 很新（0.3.0，2026-09-12），API/行为可能仍动 | 中 | 回退项 clsx + tailwind-merge@^3 等价可用 |
| 4 | copy-in 组件拖累 coverage 85 门槛 | 中 | coverage exclude `src/components/ui/**` 或为关键组件补测（实现票决策） |
| 5 | jsdom：hasPointerCapture 崩溃、ResizeObserver 缺失、canvas 不可用 | 中（一次性 setup 成本） | §6 的 setup.ts 三件套；图表单测走纯函数 + mock |
| 6 | CLI 版本漂移（registry 演进，组件代码随时间不同） | 低 | 固定 shadcn@x.y.z；components.json 入库 |
| 7 | `"use client"` 残留 / `@import "shadcn/tailwind.css"` 的具体落盘行为 | 低（不确定） | init 后核对 globals.css 与组件头；异常手修 |
| 8 | echarts-for-react 若被误引入会带来全量 echarts + wrapper 维护面 | 低 | 约定不装，自写 hook |

## Sources

- shadcn/ui 官方文档：[installation/vite](https://ui.shadcn.com/docs/installation/vite)、[cli](https://ui.shadcn.com/docs/cli)、[components-json](https://ui.shadcn.com/docs/components-json)、[react-19](https://ui.shadcn.com/docs/react-19)、[tailwind-v4](https://ui.shadcn.com/docs/tailwind-v4)、[changelog 2026-03-cli-v4](https://ui.shadcn.com/docs/changelog/2026-03-cli-v4)、[components/dialog (Base UI 默认)](https://ui.shadcn.com/docs/components/dialog)、[components/radix/dialog](https://ui.shadcn.com/docs/components/radix/dialog)
- shadcn registry 原始 payload（一手）：[r/styles/new-york-v4/dialog.json](https://ui.shadcn.com/r/styles/new-york-v4/dialog.json)
- shadcn-ui/ui GitHub：[issue #6882 linter choice](https://github.com/shadcn-ui/ui/issues/6882)
- Radix 官方：[releases](https://www.radix-ui.com/primitives/docs/overview/releases)、[introduction（统一包推荐）](https://www.radix-ui.com/primitives/docs/overview/introduction)、[primitives #1822](https://github.com/radix-ui/primitives/issues/1822)
- ECharts 官方：[按需引入手册](https://echarts.apache.org/handbook/en/basics/import/)、[npm registry](https://registry.npmjs.org/echarts)
- cn 包：[npm registry](https://registry.npmjs.org/cn)（shadcn-ui/cn 第一方）
- echarts-for-react：[npm registry](https://registry.npmjs.org/echarts-for-react)、[GitHub](https://github.com/hustcc/echarts-for-react)
- 测试生态：[jsdom README](https://github.com/jsdom/jsdom)、[user-event discussion #1087](https://github.com/testing-library/user-event/discussions/1087)、[vitest-canvas-mock](https://www.npmjs.com/package/vitest-canvas-mock)、[vitest mocking globals](https://vitest.dev/guide/mocking/globals)、[ResizeObserver mock 模式](https://stackoverflow.com/questions/64558062/how-to-mock-resizeobserver-to-work-in-unit-tests-using-react-testing-library)、[Mantine vitest 指南](https://mantine.dev/guides/vitest/)、[getContext not implemented](https://stackoverflow.com/questions/48828759/unit-test-raises-error-because-of-getcontext-is-not-implemented)
- 本地：`apps/web/package.json`、`apps/web/biome.json`、`apps/web/vite.config.ts`、`apps/web/vitest.config.ts`、`apps/web/tsconfig.base.json`、`apps/web/src/main.tsx`、`apps/web/src/styles/globals.css`、`apps/web/src/test/setup.ts`、`apps/web/src/lib/ui.ts`、`Taskfile.yml`、`AGENTS.md`
