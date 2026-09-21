# 14: 今日页重设计落地

**What to build:** 票 04 原型的最终实现：机会分层与全量对照列表的关系、EV 强度/资格徽章/报价新鲜度的视觉编码、选注篮交互、竞彩规则（同场不可串、2串1 上限、非单固只能串关）前置到选择时传达。今日页从 12 列大表变成"机会发现工作台"。

**Blocked by:** 04（今日页原型定稿），13（导航/空状态基座）

**Status:** resolved

- [x] 票 04 定稿的信息分层实现（机会呈现与全量对照的关系按原型）
- [x] EV 强度、资格徽章（可投/拒绝/证据未知）、报价新鲜度按语义 token 色编码，一眼可辨
- [x] 选注篮按 04 定稿形态（常驻/抽屉/浮动）实现，浏览全程可见已选腿
- [x] 竞彩规则在选择时前置传达：无效组合到不了提交（服务器校验仍为唯一权威）
- [x] 三态空状态 + 加载骨架；移动端可完成浏览与轻操作
- [x] 页面级 RTL+MSW 测试更新、e2e+axe 零严重违例
- [x] 如需小 API 增量（加字段级）：内联走 contract-first 流程（export→review→codegen 同 commit）——**未触发，零 API 变更**

## 不变量与人裁决项

- 服务器校验仍是唯一权威，前端前置只是体验层，不得替代后端判定
- 今日页 EV 仍按 glossary 口径呈现（市场共识诊断量，|EV|≥5% 标记为偏差非机会），不得暗示为信号

## Answer

2026-09-16 实现（分支 `feat/ui-14-today`，commit `23b1dc3`，基于 `feat/ui-13-ia-navigation`）：

1. **信息分层**：按原型——可投卡片置顶（联赛+T1 中性灰徽章/倒计时/EV 三向+最强标注/快捷选注钮/仅串关标记）+ 全量 compact 表在下（8 列：时间/编号/对阵/资格/竞彩 HDA/欧共识/EV HDA/books），资格列第 4 位，1440 视口实测无横滚（scrollWidth=clientWidth=1102）；不做视图切换；卡片区不重复"可投"徽章，标题行给"N 场不可投"对照计数。
2. **编码**：EV 数字永远只按正负红绿（text-profit/text-loss + tabular-nums，近零中性）；|EV|≥5% 与样本少用独立琥珀徽章（`bg-warning/10`），不抢数字色；资格徽章 蓝=可投（bg-info/10 text-info）/红=拒绝+原因/灰框=证据未知/无判定弱化；新鲜度折进时间格第二行（竞彩报价年龄，>30 分钟琥珀）；页头一行数据健康（竞彩报价 N 分钟前 + 欧赔已接入 N/M 场）+ 一行配色图例。
3. **选注篮**：底部常驻条（N/2 + 选中摘要 + 两腿时组合赔率）→ 右侧 Drawer（复用 Base UI drawer，票 13 分支无 drawer/badge，自 proto 分支移植 `ui/drawer.tsx`/`ui/badge.tsx` copy-in），内含腿列表/移除/金额/模式/策略版本/建立建议/去投注页；提交走现有 `fetchTodayFixtures`/`createBet`，data-testid 契约保留（today-message/today-row/pick-*/basket-*），e2e:loop 纸面闭环 9/9 全绿。
4. **规则前置**：同场换选=直接替换+行内提示；2串1 上限=行内提示；非单固首腿=提示"只能作为串关第二腿"但不阻止选择；停售/已开赛/证据未知=按钮禁用+行弱化。服务器校验仍为唯一权威（400 detail 透出为失败消息）。
5. **三态空状态 + 加载骨架**：沿用票 13 EmptyState（no-data/backend-unavailable 保留 task server/ingest 指引+唯一动作），加载为与信息分层同构的骨架（sr-only 文本给读屏）。

**实现裁决（记录给后续票与人审）：**

- **浅色 token 700 步校准（动了票 12 交付物，请人审追认）**：`--loss`/`--warning`/`--info` 浅色从 600 步提到 700 步（globals.css）——600 步在白底/浅底低于 WCAG AA 4.5:1（loss 3.3、warning 3.2、info-on-info/10 徽章 4.499），过不了票 13 确立的 axe 零 serious 基调；此为落实票 02 预裁决"红绿不用纯饱和色，浅/暗主题分别调对比度"。暗色 400 步不动；`--profit`（4.8:1）与 `--success`（尚无文本用例）未动。
- **行弱化用 `bg-muted/50` 背景替代原型的 `opacity-60`**：opacity 会把整行文字（含近黑前景）压到 AA 以下（0.6 不透明度下任何文字 <4.5:1），axe rich-data 实测确认后改背景 wash，视觉意图不变。
- **零 API 变更**：倒计时/新鲜度/单固标记均可前端推导；欧赔报价时间戳 API 确实没有——页头健康行改用"欧赔已接入 N/M 场"（joined 覆盖数）表达欧赔健康，真实欧赔年龄留待后续票 contract-first 加 `eu_updated_at`。
- **testid 增量**：卡片快捷选注钮用 `pick-card-{id}-{sel}` 前缀，表格保留 `pick-{id}-{sel}`（e2e:loop 契约）——卡片/表格各有一组选注钮，避免 Playwright strict mode 撞重复 testid。
- **验证**：单测 9 例全绿（动态时间 fixture 覆盖全部编码/规则/边界分支；MSW todayFixture 改为相对 now 动态时间，原静态时间在真实时钟下全部"已开赛"）；coverage 98.4%/88.8%（门 85%）；type/lint/build 全绿；smoke e2e 14/15（唯一失败为已知的"8000 常驻后端使 validation 降级用例环境性失败"，与本票无关）；axe 用富数据（琥珀/红绿/徽章齐全，route interception 注入）实测零 serious；e2e:loop 在 worktree 经 `uv sync` 后 9/9 全绿（本地端口覆写 `playwright.loop-local.config.ts`，5194/8931，不入库）。
