# 执行单 Schema（定稿）

- 状态：**已定稿待人过目签字**（vision-build-20261110 票 12 交付 2）
- 代码真源：`apps/backend/src/goalx_backend/betting/execution_order.py`
  （pydantic 模型即代码——本文档是人审定稿记录与字段清单，两处不一致时
  以代码为准；改 schema=PR 人审，本文档随同提交更新）
- 消费方：票 10（γ 件）执行单生成 + web 只读录入面（契约先行）+ 三级对账
- 红线：执行单是纸面产物——本 schema 落地时仓库无任何下单代码路径、无
  web 面、零 API 契约改动；成交靠人照单，回执靠人录入

## 1. 定位

真金 γ 件（票 10）的人照单投注产物：live-real 策略版本（票 01 状态机，
生成时版本必须处于 live-real 态）× 单关/串关/池面候选 → 一张执行单。
人拿着执行单到实体渠道投注，回执（成交价/部分成交/拒单因由）经票 10 的
web 只读录入面回系统，进逐注/周/月三级对账。回执与对账事件不在本
schema——执行单是不可变的纸面产物，回执是另行录入的事件。

## 2. 字段清单

### 2.1 `ExecutionOrder`（执行单主体）

| 字段 | 类型 | 约束 | 语义 |
| --- | --- | --- | --- |
| `order_id` | str | 非空 | 幂等键（生成器决定，如 `auto-v1:{date}:{anchor}:x:…`） |
| `kind` | OrderKind | `single`/`parlay`/`pool` | 执行单类型（固定赔率单关/串关、奖池面） |
| `lines` | list[ExecutionLine] | ≥1 | 执行腿（见 2.2；单关=1、串关≥2、池面=每场一选） |
| `stake` | MoneyCny | >0、≤1e9、分位精度 | 注额（元）——税感知 sizing 后的落单金额 |
| `deadline_utc` | UtcIsoTimestamp | 带时区 ISO-8601（naive 拒） | 截止：固定赔率=最早开球−前置；池面=期次销售截止 |
| `strategy_version_id` | int | ≥1 | 策略版本 id 回指（票 01 `strategy_versions`） |
| `estimator_version` | str | 非空 | 三元组之一（估计器版本） |
| `calibration_version` | str | 非空 | 三元组之一（校准版本） |
| `params_version` | str | 非空 | 三元组之一（参数指纹） |
| `h_id` | str \| null | 可空 | 假设注册表 h_id 回指（票 02）；null=无假设归属（池面既有策略底注等） |
| `pool_period_id` | int \| null | ≥1；仅 pool | 彩池期次 id 回指（固定赔率面不得携带） |
| `generated_at` | UtcIsoTimestamp | 带时区 ISO-8601（naive 拒） | 生成时刻 |

### 2.2 `ExecutionLine`（执行腿：场次×玩法×选项）

| 字段 | 类型 | 约束 | 语义 |
| --- | --- | --- | --- |
| `fixture_id` | int | ≥1 | 库内场次 id（对账 join 键） |
| `match_code` | str | 非空 | JC 编号（周六001，人照单标识） |
| `market_code` | LegMarketCode | `markets.LEG_MARKETS` 闭集（Literal 枚举） | 玩法（had/hhad/ttg/crs/hafu/htft6；池面=ttt14/pick9/goals4） |
| `selection_code` | str | 非空、`LEG_SELECTIONS[玩法]` 闭集 | 选项码（闭集外拒收=结算纪律） |
| `goal_line` | float \| null | hhad 必填 | 盘口线（prereg 整数线） |
| `kickoff_utc` | UtcIsoTimestamp | 带时区 ISO-8601（naive 拒） | 开球时刻 |
| `locked_odds` | float \| null | >1、两位小数（竞彩报价）；池面必 null | 固定赔率面锁价（decimal）；奖池无锁价 |

## 3. 结构不变量（模型级强制，`execution_order.py` 验证器）

1. **形状分治**：单关恰 1 腿；串关 ≥2 腿且各腿异场（每注腿必异场，与
   `bet_legs` 同律）；池面必须带 `pool_period_id` 且腿一律无锁价；
   `pool_period_id` 仅池面可有。
2. **税感知免税桶**（票 12 交付 1 同规则）：固定赔率面单票奖金 =
   `stake × 联合赔率（腿锁价连乘）≤ ¥10,000`——越线全额 20% 计税
   （2024-09-01 口径），注额须已压帽 `floor(¥10,000/联合赔率)`
   （取整=向下取整到元，`betting/staking.py::tax_free_stake_cap`）。
   赔率两位小数+容差 1e-9 闭死与税视图的口径缝（correctness 批修 F3）。
   池面豁免：奖池奖金额由瓜分决定不由赔率决定，大奖越线口径=税后
   （`betting/tax.py` 视图，H7）。
3. **截止下界**：固定赔率面 `deadline_utc` 须早于最早开球（kickoff
   前置口径的 schema 级下界）；池面=期次销售截止语义，不做开球比对。
   时刻一律带时区（naive 拒——混比防 TypeError 穿透，批修 F2），
   异 offset 按绝对时刻比较。
4. **hhad 载荷**：hhad 腿必须带 `goal_line`（结算判别载荷）。
5. **回指齐备**：策略版本三元组非空 + 版本 id ≥1（票 01 状态机）；
   `h_id` 可空。

## 4. 生命周期与票 10 接口

- 生成（票 10）：live-real 版本 × 候选 → 执行单；`order_id` 幂等（同锚
  重跑不双单）；生成时点的 sizing 已过税感知帽（票 12 规则
  `staking.tax_free_stake_cap`），schema 再校验一次（防生成器外的
  手工单越线）。
- 回执（票 10）：成交价/部分成交/拒单因由——另行录入，不改写执行单。
- 对账（票 10）：逐注/周/月三级；月并入 CLV 审计按 `strategy_version_id`
  与 `h_id` 归因。

## 5. 评审记录

| 项 | 值 |
| --- | --- |
| 状态 | 已定稿待人过目签字 |
| 定稿来源 | vision-build-20261110 票 12（税感知 sizing + 执行单 schema），票面字段=场次/玩法/选项/金额/截止/策略版本 h_id 回指 |
| 实现 | `betting/execution_order.py` + `apps/backend/tests/unit/test_execution_order.py`（形状/税桶/截止/回指/时区/2dp/闭集/schema 快照全覆盖；correctness 批修 F2-F5 同步） |
| 评审人 | （人过目签字） |
| 评审日期 | （待填） |
| 结论 | （待填：通过 / 修订后通过 + 修订条目） |
