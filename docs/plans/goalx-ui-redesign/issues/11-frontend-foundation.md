# 11: 前端基础引入（Base UI + ECharts 地基）

**What to build:** 前端开发地基一次性铺好：shadcn/ui init（Base UI 基座）与 cn 工具落位、Biome 对齐流程固化；ECharts 6 按需引入与自写 use-echarts 薄 hook；jsdom 测试三件套。不改变任何现有页面行为——后续所有实现票在此地基上开发。

**Blocked by:** None（票 01 选型验证已 resolve，含 Base UI 改判记录）

**Status:** resolved（2026-09-16，分支 feat/ui-11-frontend-foundation）

- [x] shadcn init 完成（Base UI 基座、components.json、globals.css token 注入），`bun run lint:fix` 后 Biome 零报错
- [x] cn 工具落位（第一方 `cn` 包或 clsx + tailwind-merge 回退，选型与理由记入 PR）
- [x] echarts/core 按需注册 + use-echarts hook（init/dispose/setOption/ResizeObserver）有单测
- [x] jsdom 三件套进 test setup：pointer-capture mock、ResizeObserver stub、echarts mock 策略（option builder 纯函数化）
- [x] coverage 排除决策落定（copy-in 组件 components/ui/** 是否出分母）并记录理由
- [x] `task check` 全绿（web-lint / web-type / web-coverage / web-build / e2e）

## 决策与执行记录（2026-09-16）

- **CLI**：固定 `shadcn@4.21.0`，`init -y -d -b base`（Base UI 即 2026 CLI 默认，显式传参防漂移）。生成的 style 为 `base-nova`；init 附带 `add button`（作为 add→lint:fix 管线验证，属票面 What 段"管线固化"范围）。无 `"use client"` 残留。
- **cn 选型**：第一方 `cn@^0.3.0` 包——shadcn 官方默认、零依赖、生成代码直引 `import { cn } from "cn"`；`src/lib/utils.ts` re-export 仅作 components.json `aliases.utils` 落点。回退项（clsx + tailwind-merge@^3）不需要。
- **根 tsconfig.json**：补 `compilerOptions.paths`（与 tsconfig.base 一致）——shadcn CLI 不读 solution-style 引用，这是让它识别 `@/` 别名的最小改动；对 `tsc -b` 无影响。
- **echarts**：`echarts@6.1.0`；`src/components/charts/echarts.ts` 唯一 `echarts.use` 入口（现注册 Line/Bar/Grid/Tooltip/Canvas，后续票按需追加），`ComposeOption` 收敛 option 类型；`use-echarts.ts` 约 30 行 hook，5 条单测全 mock `echarts/core`。option builder 纯函数化约定写入 hook 头注释。
- **jsdom 三件套**：setup.ts 加 pointer-capture mock + 空实现 ResizeObserver stub；echarts 走"测试文件内 `vi.mock("echarts/core")`"策略（use-echarts.test.tsx 为示范），不加全局 canvas mock、不装原生 canvas。
- **coverage**：`src/components/ui/**` 出分母（vitest.config.ts 内注释记录理由：vendor copy-in，单测它=测上游；真实行为由页面单测 + e2e/axe 覆盖；不豁免 Biome）。覆盖率 98.3%（阈值 85）。
- **门禁**：fmt/lint/type/test、contract、web-lint/type/coverage、web-build、web-e2e 3/4、web-e2e-loop 9/9 全过。唯一失败 e2e（validation 降级用例）经 stash 对照在干净工作树上同样失败——本机 8000 端口有票 37 活后端（两查询均 200，进不了错误态），环境性问题，与本票无关。
- **AGENTS.md**：Conventions 增补 shadcn 流程一条（copy-in 性质 + add 后必跑 lint:fix）。

## 不变量与人裁决项

- 不引任何运行时组件库；Base UI 基座不得擅自更换
- 纯地基票：现有页面行为、既有 e2e 用例不得变化（行为与 e2e 未动；init 注入的语义 token 底座 + Geist 字体属设计系统基座，票 12 按 02 定稿统一调整）
- lockfile 变更与代码同 commit（frozen-lockfile 必须仍过）✅ `bun install --frozen-lockfile` 通过
