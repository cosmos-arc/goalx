# 07 web：query-keys 注册表——缓存键一个住所

Status: resolved（2026-09-28，PR #115 合入 main f5fb248）
Blocked by: None（可立即开工，与后端族无交集）

**交付（用户视角）**：从总览页跳到赛程页不再重复请求同一份 today 数据；失效一处
声明、处处生效。

## 现状

server-state 缓存键是字符串元组散在 12 个文件，失效清单手抄 ×15 处：

- bets-page.tsx:191-198 `refresh()` 手列 6 键；market-pool-page.tsx:78-91 列 3 键；
  had-basket.tsx:108、market-goals-page.tsx:170、bankroll-page.tsx:147、
  review-page.tsx:46,106 各自为政。
- 已发生的漂移：`GET /api/v1/fixtures/today` 被 `["today"]`（overview:76 /
  bets:184 / history:162）与 `["fixtures-window", 3]`（fixtures-page:65-68 /
  market-had:29-31）两把键各缓存——overview:16-17 注释「缓存键与各页对齐，跨页返回
  不重拉」只活在注释里且已不成立。

键注册表这个 interface 目前不存在任何住所；「跨页不重拍」的 invariant 无代码可依。

## 方案

lib 下一个 query-keys module：typed 键工厂 + invalidate 辅助函数，成为唯一键注册表。
12 个消费方改引工厂；双键撞同一端点结构性消失。

## 实现决策

- 工厂 = 纯函数 + 常量，不建类不建 context（YAGNI）。
- `/fixtures/today` 双键合一：overview/bets/history 的 1 日窗与 fixtures/market-had
  的窗口数是同一意图的两种表达，统一到带窗参数的键。
- 失效辅助（如 invalidateBets()）只包住既有清单，不发明新失效语义。

## 测试接缝

- 键工厂一个小单测（同参同键、不同参异键）；页面既有测试不动——键不是页面行为断言面。
- 跨页不重拉：web-e2e 顺手断言一次导航后的请求数（不强求，锦上添花）。

## 范围外

- 不换数据层（react-query 版本与用法不动）；篮子跨页存活（状态 seam 缺席）不在
  本票——那是产品决策；URL 化筛选器不在本票。

## 不变量与人裁决项

- 不变量：失效行为迁移前后等价（该刷新的页还刷新、不该重拍的不重拍）。
- 人裁决：双键合一后 staleTime 取值（建议窗参数版本统一，overview 的 1 日窗改为
  按窗参表达）。

## 验收

- [x] 键工厂 + invalidate 辅助唯一住所，12 个消费方改引，散键归零
- [x] `/fixtures/today` 双键合一（口径缩窄人追认：= 同端点单份缓存条目；「跨页不重拉」的 staleTime 实现经用户 2026-09-28 终裁删除——PR #116）
- [x] 失效行为等价（既有页面测试全绿 + 工厂单测）

## Answer

**落地**（PR #115，单提交 1e12e6f，+284/−52，17 文件）：

- `apps/web/src/lib/query-keys.ts` = 全站 queryKey + 失效清单 + today 窗
  staleTime 的唯一住所：11 无参键常量 + 7 带参工厂 + 3 个失效辅助
  （`invalidateBets`/`invalidateSettlementViews`/`invalidatePoolViews`——只包
  既有清单，不发明新失效语义）。
- 双键合一：`todayFixturesKey(days)` → `["today-fixtures", days]`；单日页
  （overview/bets/history）显式 `fetchTodayFixtures(undefined, 1)`（后端 days
  缺省即 1，请求等价，api/fixtures.py:52-54 独立复核过）；3 日页传窗口常数。
- 消费方实为 15 文件（组件 4 + 页面 11）——票面写 12 是盘点漏数，另漏列
  `["backtest-runs"]`（validation-page）一并收编，键名原样。

**人裁决已终裁（2026-09-28 用户）**：staleTime = **删**。PR #115 曾按票面建议
方向代落 30s（TODAY_STALE_MS），用户看过代价/收益后裁定属过度设计——单用户
工作台每次挂载重拍代价可忽略、赔率新鲜优先，全站回到默认 staleTime=0（票 07
之前的一贯行为）。PR #116（eb99aa7）收口：常量删除、五消费方去 staleTime、
overview 注释去「跨页不重拉」假 invariant（0 口径下该说法只活在注释里，正是
本票要消灭的病）。验收 2 口径随之缩窄（人追认）：双键合一 = 同端点单份缓存
条目 + 注册表唯一住所；「不重拉」的 staleTime 实现被否决。

**评审收口**（双轴）：
- Standards P1：`fixtureEvidenceKey(activeId ?? 0)` 哨兵违背键值等价不变量
  （旧禁用态键哈希含 null）→ 工厂签名放宽为 `number | null`，两处去哨兵。
- Standards P2 采纳：stakeAdviceKey 改输入对象整进键（请求体加字段自动进
  键，消手抄清单漂移面）；字面量改名 `today-fixtures` 对齐工厂名（无
  persister，无缓存连续性损失）；测试去永真断言、补无参键钉字面量；模块
  自述补失效清单+staleTime 归属。
- Standards P2 保留：poolPeriodDetailKey 可选参坍缩（单期键/全期前缀一函数）
  ——有测试钉住、market-pool 传 `string | undefined` 正好依赖，可辩护。
- Spec 七条六符；唯一程序性偏差即上述 staleTime 代裁记录。

**不变量验证**：散键字面量（`queryKey: [`）全站零命中；setQueryData/
prefetchQuery 等旁路通道零存在；main 15 处 invalidate 逐一等价（六键/三键/
单键清单原样）；react-query 版本与用法不动；contracts/ 未触碰（CI contract
gate 绿）。门：biome/tsc/vitest 218（+1）/build/task check 含 e2e（30+12）
全绿；PR #115 CI 16 项全绿。

## Comments

- 2026-09-28 用户终裁 staleTime：「拉一遍新的也没什么问题」「直接删就好了，这个
  有点过度设计了」→ PR #116（eb99aa7）删 TODAY_STALE_MS，全站回默认 0。
