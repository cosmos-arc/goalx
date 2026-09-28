# 08 web：死代码 + 重复家族批票（含两条复验修正的收编）

Status: resolved（PR #100（批一；中件缓办见票内），2026-09-28）

## 死代码（复验坐实）

- `api/goalx.ts:164 fetchBacktestRun`（单数）+ `:31 BacktestRunDetail`：全库
  零调用（页面用的是复数 fetchBacktestRuns；契约路径未 mock 恰因无人用）。
- `api/client.ts:4 Status`、`api/goalx.ts:9 GoalsMarketBlock`：零引用。
- `mocks/handlers.ts:1133 POST pool-states`：路径在契约（v1.json:1002）但无
  任何客户端调用——死 mock（复验修正：非"契约缺路径"，撤回原表述）。
- `combo-engine.ts:76/265 ComboResult.feedOrder / GoalsComboResult.feedOrder`
  字段：生产零读、仅自测读（复验收窄：rank*ForFeed 函数两页在用，勿动）。

## 重复家族（按收益排）

1. toast div ×4（fixtures:114 / market-had:104 / market-goals:210 /
   research:169）、loading skeleton ×3 → 抽 lib/ui。
2. `dayNoteOf` ×2（market-had:38 / market-goals:78）→ lib/ui。
3. `localClock`(bets:155) ≡ `localTime`(had-quote-ui:77) ≡ `formatTime`
   (market-pool:170) → 统一 lib/ui `shortTime`。
4. `IntelRow/IntelLine`（evidence-chain:26 vs evidence-card:25）、
   `kickoffMs/goalsKickoffMs`（combo-engine:95/282）。
5. joint-EV 内联重算（market-pool:248-257）vs `parlayAdviceInput`。
6. 最早开球 + 在场注单集合逻辑（overview:105-141 vs bets:212-227,305-307）。
7. goals drawer footer 控件（market-goals:516-553 vs had-basket:250-285）——
   篮子主体是 documented deliberate variant 不动，footer 纯复制可抽。

## 不变量与人裁决项

- 行为零变化；一次 PR 一族或分两 PR（agent 定）；`web-lint/type/coverage/e2e` 绿。
- 系统性根因顺手修：handlers.ts 至少给关键 handler 加 `satisfies` 生成类型
  锚点，让字段漂移对 tsc 可见。

## Comments

## Comments

**2026-09-28 本票落地（PR #100）：**
- 死代码全清：fetchBacktestRun/BacktestRunDetail/GoalsMarketBlock/Status、
  handlers 的 fixtures/1/odds（含 oddsFixture）与 POST pool-states 死 mock、
  ComboResult/GoalsComboResult.feedOrder 冗余字段（排序断言改测
  rankFixturesForFeed 本体）。
- 收敛：lib/ui 增 localTime+dayNoteOf 正典（had-quote-ui 转发、bets localClock
  删、两 market 页本地副本删，market-goals 死 ?? 守卫随统一签名消解）；
  toast ×4 → components/StatusToast（testid 注入，e2e 锚不变）；
  handlers beijingBusinessDate 改引 lib/ui。

**仍缓办（下批，均为中件）：**
- 骨架屏 ×3：三处形状各异，泛化=参数化得不偿失，跳过。
- IntelRow/IntelLine、kickoffMs/goalsKickoffMs、joint-EV 内联 vs
  parlayAdviceInput、earliest-kickoff+openBetFixtureIds（overview vs bets）、
  goals drawer footer 复制。
- handlers satisfies 生成类型锚点（需 fixture 与生成类型逐个对齐）。
- market-pool formatTime 保留：zh-CN 月日+时分与 shortTime 语义不同，非重复。

**2026-09-28 批二收官（PR #104）：** IntelRow/IntelLine 合一（components/IntelLine）、
kickoffMs 结构类型合一、market-pool 联合 EV 换 parlayAdviceInput 正典、
overview/bets 最早开赛+open 场次集进 lib/ui（earliestKickoffMs/openBetFixtureIds）、
两篮抽屉三输入 → had-basket.BasketFormFields；复核 nit 两条同批（localTime 转发删、
useHadBasket 缺省参删）。**仍缓办仅剩：handlers satisfies 生成类型锚点**（需
fixture 与生成类型逐个对齐，独立小批）。

**2026-09-28 终收（PR #106）：** handlers 17 个 fixture 上 `as const satisfies
DeepReadonly<T>` 生成类型锚点（poolPeriodDetail 直对可变类型）；锚点即时报一处
类型选错（review 队列=ReviewQueueView 含 items 包装），其余与契约全量对齐。
**本票全部缓办项至此收口。**
