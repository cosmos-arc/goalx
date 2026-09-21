# Wayfinder Map: goalx UI/UE 产品级重设计

Label: wayfinder:map

## Destination

`apps/web` 从工程原型升级为产品级 SaaS 分析台（基准 Linear/Vercel/Stripe Dashboard）：完整设计系统（浅/暗双主题）+ 重设计的现有页面 + 新增总览/历史复盘/指标词典页，含为 UX 所需的增量 API 变更，全部合入 main。本 effort 携带执行：设计票 resolve 后，实现票从雾区毕业，按顺序落地。

## Notes

- 域：足彩量化工具的前端体验。设计前读 `.scratch/goalx-quant/glossary.md`（指标口径）与根目录 `CONTEXT.md`（领域术语）；指标呈现必须附判读方向（用户对指标口径不全熟，中文配数字实例）。
- **设计红线（2026-09-16 用户定）**：不要有明显的"AI 生成感"，保持简洁清晰——禁紫色渐变、emoji 图标、装饰性玻璃拟态、套路化 hero/空状态插画；视觉决策以成熟产品惯例为先。
- 框架决策（2026-09-15 charting grilling 定，详见本文件 Comments）：
  - **产品化标准**：面向陌生用户做 IA/术语/空状态/指标引导/无障碍；不做登录/多租户/权限
  - **风格基准**：现代 SaaS 分析台；今日场次表等纯专业场景允许局部高密度
  - **组件**：shadcn/ui 模式（**Base UI** 基座 + Tailwind copy-in，不引运行时组件库；2026-09-16 由 Radix 改判，理由见票 01 Comments）
  - **图表**：ECharts 按需引入
  - **主题**：token 一次到位，浅/暗双主题
  - **API**：增量为主（加字段/加端点），破坏性变更个别过审；contract-first 流程不变（export→review diff→codegen 一起提交）
  - **文案**：zh-CN 单语，文案集中管理，不引 i18n 框架
- 技能约定：设计/口径类票调 `grilling` + `domain-modeling`；"长什么样"类票调 `prototype`；research 票调 `research`。
- 实现票毕业规则：每张页面设计票 resolve 后，其实现票（含单测/e2e/axe 无障碍）从雾区毕业；落地顺序 设计系统 → 今日 → Dashboard → 投注 → 历史 → 词典 → 验证/资金。
- 仓库硬约束：Biome（tabs/双引号/width 120/noExplicitAny）、bun isolated linker 陷阱（jest-dom 入口、openapi-fetch fetch 捕获）、coverage ≥90%、contract drift CI 门禁、`main` 保护走 PR。

## Decisions so far

- [01 前端基础选型验证](issues/01-frontend-stack-research.md): shadcn/ui（**Base UI** 基座，2026-09-16 改判自 Radix）+ TW v4 + React 19 与 ECharts 6 按需引入均可行；add 后跑 lint:fix 对齐 Biome，ECharts 自写薄 hook，jsdom 测试需 pointer-capture mock + ResizeObserver stub + echarts mock。
- [02 设计语言与 token 体系](issues/02-design-tokens.md): 红涨绿跌（钱层 profit红/loss绿 配正负号）；调性=克制/可信/专业；界面层 warning amber、可投徽章 info blue（避绿）、destructive/success；字体 Geist+平台中文栈零 webfont；图表五色 blue/orange/teal/indigo/slate；密度组件级两档；圆角收 8px；灰阶与近黑 primary 不动。
- [03 信息架构与导航](issues/03-information-architecture.md): 每页一个动词（总览分诊/今日选/投注记/历史分析/验证判/资金对/词典释）；空状态=一句人话+一个动作三态统一；路由 `/` 换总览、`/today` 承接今日、新增 `/history` `/glossary`，其余六条不动。
- [04 今日页重设计](issues/04-today-redesign.md): 可投卡片置顶+全量表在下（无切换）；EV 数字永远只按正负红绿、偏差独立琥珀徽章；资格徽章蓝/红/灰框；底部选注条+右侧 Drawer；规则前置（替换/上限/单固/禁用）；资格列第 4 位无横滚。原型=proto/ui-04-today 分支 /proto/today。
- [12 设计系统 token 与双主题落地](issues/12-design-tokens-impl.md): 02 定稿 token 全量进 globals.css 双主题（钱层红涨绿跌/界面层四功能色/图表五色/圆角 0.5rem）+ Table data-density=compact + AppShell 语义化与 ThemeToggle（localStorage 持久化、首帧防闪）；存量页散写色留给 13-18 号清退。
- [05 总览 Dashboard 页设计](issues/05-dashboard-design.md): 待办清单卡四规则（紧迫倒计时/待录/可结算/数据异常）+ 首屏四卡（真金纸面隔离）+ 清单卡形态与就地指引下钻；v1 现有端点前端拼装零新端点。
- [06 投注生命周期](issues/06-bets-lifecycle.md): 赛果同步优先人工兜底（面板+Prefect 兜底，后端源跨图立票）；增强分组表三段；真实回录右侧 Drawer 带快照对照；规则前置补开赛警示与实款提示。
- [07 历史复盘页](issues/07-history-page.md): 分析优先（五指标卡+累计曲线+同页展开明细）；口径差异文案页头固定；现有 GET /bets 前端过滤聚合零新端点。
- [08 指标词典产品化](issues/08-glossary-page.md): 内容源 TS 常量模块（口径以 docs/plans 词典为准）；词典页检索+词条卡三要素；tooltip 虚线下划线 Popover 键盘可达；首批 10 词条。
- [09 验证页与资金页](issues/09-validation-bankroll.md): 验证首屏=结论+三条件+前瞻曲线带 0 线；弱类型指标映射行+折叠；资金页余额迷你曲线+成本分列；入金引导需新端点（归 20 内联 contract）。
- [10 API 增量清单与 contract 排期](issues/10-api-increment-plan.md): 唯一 contract 变更 = POST /bankroll/deposits（20 内联）；eu_updated_at 顺带项；赛果同步端点外部依赖（goalx-quant 立票，16 先做降级态面板）；总览/历史 v1 前端拼装零新端点。
- [13 IA 与导航落地](issues/13-ia-navigation-impl.md): AppShell 两级扁平导航（一级六页 + 次级页脚三页，双级 aria-current）+ EmptyState 三态空状态组件（no-data/backend-unavailable/not-available，一句人话+至多一动作）+ 路由落定（/ 总览引导态含去今日入口、/today 承接、/history /glossary 占位、零 redirect）；axe 基调＝e2e/axe-baseline.ts 的零 serious/critical，九路由逐页套用，14-20 沿用。
- [14 今日页重设计落地](issues/14-today-impl.md): 票 04 定稿全量落地（可投卡片置顶+compact 表/红绿 EV+琥珀偏差徽章/蓝红灰资格徽章/底部篮+右 Drawer/规则前置），零 API 变更；两项记录性裁决待追认——浅色 loss/warning/info token 提到 700 步（600 步白底 <AA 4.5:1，落实 02"分主题调对比度"），不可投行弱化用 bg-muted/50 替代原型 opacity-60（opacity 压文字对比过不了 axe）；欧赔时间戳 API 缺失→健康行用 joined 覆盖数表达；卡片选注钮 testid 用 pick-card-* 前缀避让表格 pick-* 契约。
- [15 总览 Dashboard 落地](issues/15-dashboard-impl.md): 票 05 定稿全量落地（待办四规则清单卡+快照四卡真金/纸面分区+页底验证 x/3 细线），现有端点五路前端拼装零 contract 变更、分诊页零写操作；边界口径入票 Answer——待办判定只对今日列表内场次下判、规则④整份快照缺才触发（请求失败≠快照缺）、今日真金盈亏=当日投注流水净额（入金不计）、ROI 已结算口径显式标注未结不计；10% 底纹徽章文字改前景色（票 14 对比度同教训）。
- [16 投注生命周期落地](issues/16-bets-lifecycle-impl.md): 票 06 定稿全量落地（增强分组表三段[建议/已锁定行内真金徽章/已结算按 settled_at]+回录右 Drawer 带快照对照与实款提示+赛果同步面板降级态与待出列表+人工兜底更正预览原样迁入+锁定开赛<5 分钟警示不阻止），零 contract 变更；e2e 纸面闭环 9/9 保持（断言随分组适配），同步端点仍挂票 10 外部依赖，worktree wt-15 留给 17 号堆叠。
- [17 历史复盘页落地](issues/17-history-impl.md): 票 07 定稿全量落地（两层筛选[模式大标签默认纸面不混算+时间范围预设+二层折叠 Competition/策略/状态 选项随数据推导]、五指标卡随筛选联动[EV/CLV 无注级字段显示"—"并注明口径与判读方向，不造假数据]、累计曲线 0 基准 markline、口径行 07 文案逐字+空筛选 no-data+清筛选、明细同页展开同构五列），GET /bets 前端拼装零 contract 变更；明细→Fixture 一跳留待场次详情页（map Not yet specified），"前瞻纳入"链接位置留给 18 号 tooltip；use-echarts 图表容器须随子组件挂载（init effect deps=[] 时序）。
- [18 指标词典产品化落地](issues/18-glossary-impl.md): 票 08 定稿全量落地（内容源 src/lib/glossary.ts 首批 10 词条三要素+警示逐字、/glossary 检索+词条卡+no-data、shadcn tooltip(Base UI)+GlossaryTerm 虚线下划线 Popover 键盘可达），同步口径=glossary.md 唯一准绳先改后转写；tooltip 接入今日表头四指标+单固徽章/验证 skill·CLV/历史"前瞻纳入"Popover/总览 ROI；aria-describedby 受控接线（Base UI 1.8 tooltip 视为纯视觉不自动挂，悬空 idref 过不了 axe）；validation smoke 改双路径断言兼容 8000 常驻后端，e2e:loop 9/9 保持，worktree wt-15 留给 19 号堆叠。
- [19 验证页重设计落地](issues/19-validation-impl.md): 票 09 定稿全量落地（首屏=状态结论 x/3 推导[整赛季独立不计入]+三条件卡带判读方向+前瞻 yield 主图[滚动/累计双线+0 基准虚线 markline，LegendComponent 补注册]，下钻层 details 折叠收 CLV 明细/前瞻明细/样本约束/口径说明/回测 run 表），progress 单请求内嵌 forward（与 forward-skill 同报告不重复拉）零 contract 变更；弱类型 clv/forward 已知 key 映射行接 18 号 tooltip+未知 key 折叠原始 JSON；回测 skill 下钻次级区逐字标注"不算通过线"、bestForwardSkill 与服务端判定同构；条件徽章 muted 底灰字 4.34:1 按票 14/15 教训改前景色，e2e:loop 9/9 保持，worktree wt-15 留给 20 号堆叠。
- [20 资金页重设计落地](issues/20-bankroll-impl.md): 票 09 定稿全量落地（余额大数字+近 30 天余额迷你曲线[独立子组件+点数≥2 才挂载，窗口外不凑点]+流水 compact 表语义色[投注/兑付与盈亏色一致、出入金中性]+成本摘要 ¥/credits 分列与"不视为总成本已覆盖"警示保留），全批唯一 contract 变更 `POST /bankroll/deposits` contract-first 同 commit（redocly recommended-strict 过、既有端点零改动、redocly.yaml 豁免未动）；入金=空态引导"记录第一笔入金"内联表单+已有余额次级入口，领域函数 `betting/store.record_deposit` 落 betting 包（ADR-0008）；e2e:loop 9/9 保持（纸面锁定零流水断言未动）；MSW fixture 时间改相对 now 防 30 天窗口漂移，playwright 两配置加端口 env 覆盖（默认不变）支持主工作区常驻 server 时 worktree 并行跑门禁。

## Not yet specified

- 场次详情页（赔率演变/EV 时间线）：依赖后端赔率历史数据可用性，核心页落地后再评估
- 复核页（M3 LLM 线）与设置页（M4）设计：等对应后端里程碑；IA 票（03）为其留位
- i18n 引入时机：产品化推广真实发生时

## Out of scope

- 登录/账号体系/多租户/权限（产品化标准明确排除）
- i18n 框架与双语（zh-CN 单语已定）
- 原生移动 App（SPA 响应式覆盖移动可用即可）
- 破坏性 API 重构（增量为主原则已定）
- M3 复核线/M4 设置项的后端功能开发（属 goalx-quant 地图，本图只做 UI 预留）

## Comments

- 2026-09-15 charting session：两轮 grilling 定目的地与框架（Q1a 全量落地含执行、Q2b 可产品化标准、Q3b SaaS 分析台基准、Q4c 含新页、Q5c 允许 API 增量；第二轮按推荐锁定新页 a+b+d、shadcn/ui 模式、ECharts、双主题、API 增量为主、zh-CN 单语）。建图 + 10 票，research 票 01 发射子代理。
- 2026-09-16 组件基座改判 Radix → Base UI（用户裁决）：greenfield 零切换成本；Base UI 1.0 稳定（2025-12）、原 Radix 团队维护、2026-07 起 shadcn 默认基座；Combobox/Number Field/Drawer 恰合本项目需求（场次选择、赔率金额录入、选注篮侧栏），Radix 独有的 Context Menu/Hover Card/Toast 用不上。
- 2026-09-16 to-spec 会话产出 [spec.md](spec.md)（Status: ready-for-agent）：what/why 层面定稿，页面级 how 仍归票 02-09；测试接缝确认仅复用现有（无视觉回归、无 Storybook）。
- 2026-09-16 to-tickets 会话：spec 切成实现票 11-20（tracer-bullet 竖切，Status: ready-for-agent，阻塞边挂设计票）。雾区"实现票系列"与"e2e/axe 验收基调"毕业（基调由票 13 承接）；15/17 硬阻塞于票 10（contract 批量排期），14 的小 API 增量内联走流程。落地顺序：11 地基 → 12 token → 13 IA → 14 今日 → 15 总览 → 16 投注 → 17 历史 → 18 词典 → 19 验证 → 20 资金。
- 2026-09-16 预裁决轮（用户逐项过）：九项关键裁决分记各票 Comments（02 红涨绿跌+调性、03 命名+导航+移动底线、05 待办规则+首屏、06 赛果同步优先、07 分析优先、08 首批词条、09 验证首屏）；02/03 随后完整 resolve（Answer 在票内）。spec 同步更新（story 13 改同步优先、Out of Scope 增后端源接入、进度锚点）。
- 2026-09-17 第一批合入 main（PR #16 = 票 11-14，merge a888e23）：地基/Base UI token 双主题/IA 导航+axe 基调/今日页产品级重写（API 零变更，纸面闭环 9/9）。两项追认裁决随 PR 记录：浅色 loss/warning/info 600→700（WCAG AA）、不可投行 bg-muted/50 替代 opacity-60。分支与 worktree 已清理。实现线暂停于 15（等 05 resolve），下一步 05-09 剩余小项批量起草。
- 2026-09-17 设计队列清空（用户"全按推荐"批量过审）：05-10 六票 resolve（Answer 在各票）。关键收口：总览/历史 v1 前端端点拼装零新 API；唯一 contract 变更 = 入金端点随 20 内联；赛果同步后端源为跨图外部依赖（16 先做降级态）。**frontier = 15/16/18/19/20 全部无阻塞**（15/17 的端点验收项已同步作废修正）。
- 2026-09-17 第二批合入 main（PR #17 = 票 15-20 七 commit，merge 382e18b）：总览/投注/历史/词典/验证/资金全部产品级落地，入金端点为全批唯一 contract 变更。CI 一度失败两处已修（历史页常驻筛选条移出数据分支、今日双路径断言补 20s 超时）。**目的地达成**——剩余均为雾区/跨图项：赛果自动同步后端源（goalx-quant 立票，16 号面板已留降级态）、场次详情页（等赔率历史）、复核/设置页设计（等 M3/M4）、glossary.md 转正 docs/plans 后与 lib/glossary.ts 对齐、17 的明细→Fixture 一跳。分支与 worktree 已清理。
