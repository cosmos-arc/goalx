# 18: 指标词典产品化落地

**What to build:** 票 08 定稿的词典形态：独立词典页（可浏览/检索）+ 页内指标 tooltip/popover 体系，内容源按 08 决策落位（前端 TS 模块或后端字典端点）；首批覆盖 08 定的高频指标（EV/欧共识/资格徽章/CLV/skill/前瞻）。

**Blocked by:** 08（设计定稿），13（导航基座）

**Status:** resolved

- [x] 词典页可浏览、可检索；词条含定义、判读方向、数字实例三要素
- [x] tooltip 体系接入已上线页面（今日/验证等）的高频指标名，键盘可访问（聚焦可达）
- [x] 内容源按 08 定稿落位，与 glossary.md 的同步方式（谁改哪边）在 PR 中写明
- [x] RTL + e2e + axe 全绿

## 不变量与人裁决项

- 词条口径以 glossary.md 为唯一准绳，UI 不得自创口径或简化掉警示（如"可投≠必成交"）
- 每个词条的判读方向为必填要素，缺失的词条不上线

## Answer

2026-09-18 落地（分支 `feat/ui-18-glossary`，commit `e0e15e3`，基于 17 号分支堆叠，未 push/未建 PR）：

1. **内容源** = `apps/web/src/lib/glossary.ts`（票 08 定稿 TS 常量模块）：首批 10 词条按 08 清单顺序
   （EV/欧共识 p/资格徽章/单固/books/纸面 vs 真金/盈亏与 ROI/前瞻纳入/CLV/skill 与前瞻 skill），
   每条 {id, term, aliases, definition, direction, example, caution} 六字段齐；文件头注明
   **同步方式（谁改哪边）**：`.scratch/goalx-quant/glossary.md` 为唯一口径准绳（转正后
   `docs/plans/goalx-quant/glossary.md`，转正时只改该文件头指针），口径变更先改准绳、再同步转写本
   模块；本模块是 UI 呈现层，不自创口径、不删减警示（"可投≠必成交""EV 是诊断量非机会信号"
   "三条件只认前瞻""回测 skill≈−3.75% 不可作实盘依据""腿数不凑"均逐字保留）。
2. **词典页** `/glossary` 替换 13 号占位：检索框（term/aliases/definition，大小写不敏感，空查询=全部）
   + 词条卡（term + 判读方向徽章 + 定义 + 别名 + 中文数字实例 + 琥珀警示条），无结果 EmptyState no-data
   + "清空检索"动作。
3. **tooltip 体系**：`shadcn add tooltip`（Base UI 基座，add 后 lint:fix 对齐 Biome）+ `GlossaryTerm`
   封装（`src/components/glossary-term.tsx`）：指标名虚线下划线（decoration-dashed），悬停/**聚焦**出
   Popover（定义+判读方向），触发器为原生 button 键盘可达。**aria-describedby 由组件受控接线**
   （仅展开时挂、关闭不挂）——Base UI 1.8 把 tooltip 视为纯视觉元素不自动挂，且关闭时挂悬空 idref
   过不了 axe 的 aria-valid-attr-value。
4. **接入点（克制，只接指标名）**：今日页表头 资格/欧共识/EV H/D/A/books + 可投行的"单固"徽章；
   验证页 MetricCard "回测 skill"/"CLV beat(纸面)" 标签（testid 不变）；历史页口径行"前瞻纳入"由
   17 号链接占位升级为 Popover（07 定稿文案逐字未动）；总览页真金 ROI 卡标签。
5. **验收**：glossary 数据完整性单测门（10 条齐+id 唯一+三要素非空+关键警示逐字断言）、
   GlossaryTerm 键盘可达+aria-describedby 单测、词典页检索单测；e2e /glossary smoke（检索+空态）
   + axe 九路由零 serious；e2e:loop 纸面闭环 9/9 保持。validation smoke 改"数据/降级"双路径断言
   （兼容 8000 常驻后端环境，与 overview/today/history smoke 同款写法）。lint/type/coverage(85 阈值)/build 全绿。
