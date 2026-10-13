# 14 训练集卫生接线：live 周训练面 + 剩余散点统一（S14）

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` C 相 User Story 14
Blocked by: 13

## 任务

S14 原文"行政判赛排除表与 PSC 成熟度纪律在引擎入口统一生效（不是散落
各处的 if）"。票 13 已把**回测引擎**入口统一到 gold 单一取数（`admin_
excluded=false` 单过滤 + 成熟度由 gold 快照承载）。本票收剩余面：

- **live 周训练面**（`tasks.weekly_train` → `train_competition` →
  hist_matches 取数）：现状无卫生纪律——行政判赛行的假 3-0 会进 DC
  训练、PSC 未成熟约束不适用于赛果但排除表适用。接线到同一谓词
  （data/hygiene 单一取数口，不散写第二份语义）。
- **审计剩余 fair/PSC 消费散点**：haircut 校准（`evaluation/haircut.py`）、
  baseline 分期质检、validation 面若有各自绕开 hygiene 的取数，收敛或
  显式声明豁免理由。
- 回测侧复核：票 13 落地后的入口是否仍有旁路（引擎不经 gold 直接摸
  hist_matches 的路径清零确认）。

验收：正反用例（排除生效/正常场不误伤——同 A4 票 05 先例）；全库
grep 无第二份排除/成熟度语义。

## 不变量与人裁决项

- 卫生语义唯一落点 `data/hygiene.py`；任何新消费面从该模块（或 gold
  快照标志）引，不各自实现。
- live 训练面改动不得改变五大联赛现有工件身份语义（排除行剔除属
  数据面修正，fingerprint 变化是预期内的诚实漂移）。
- 人裁决项：live 面是否也切 gold（训练行换名字空间）——默认**不切**
  （live 面吃 hist_matches 现状、只加卫生过滤；名字空间切换是独立
  裁决，成本/收益另议）。

## Out of scope

- 引擎消费接线本体（票 13）；D 相评估面。

## Answer

2026-10-10 交付（分支 feat/hygiene-live-train-c14-20261010，提交 a9c4337）：

- **live 周训练面接线**：`training_rows_for`（train_competition→weekly_train
  唯一取数点）经 `data/hygiene` 单一口剔除行政判赛行；`hist_rows_through`
  投影补 competition 列（排除窗按联赛精确匹配）；排除表缺席 fail-closed
  传导（未迁移面炸训练优于静默吃假分）。正反用例 + fail-closed 测试齐
  （task test 840 全绿）。
- **散点审计（豁免逐项入档）**：
  - haircut 校准（evaluation/haircut.py）：实时报价 as-of 配对（票 35），
    不吃 fdhist odds 面；假 3-0 场从未有竞彩盘 → 天然无配对，**豁免**。
  - baseline 分期质检（baseline_quality_report）：fdhist 源自身描述统计
    （缺失率/overround），非 fair 基准消费面，**豁免**；baseline 对照 run
    已随票 13 引擎走 gold（卫生由 gold 快照承载）。
  - validation 面：消费 forecasts/bets，不触 hist，**clean**。
  - pool_replay（票 51）：已收官的结构性研究，被十年引擎路径取代，
    **豁免不动**（重开另票）。
- **回测侧旁路复核**：全库 grep——`hist_rows_through` 唯一调用方=
  dc_model（live 面）；引擎/evaluation 无直接 hist_matches SQL；
  `psc_mature` 消费面=hygiene/gold/corpus_gate 三处单一来源；排除语义
  消费面=hygiene/gold/corpus_gate/dc_model(+gold_reader 旗标) 零散写。
- **生产影响**：现排除窗全在 T1（土超），不在 TIER1_COMPETITIONS——五大
  live 工件 fingerprint 零漂移（诚实无扰动落地）。
- **correctness 潜在态发现（留白）**：若未来五大联赛出现首条排除窗且
  命中当季最晚比赛，`load_latest_run` 按 train_window_end 排序会持续选中
  排除前的旧工件（内容寻址两文件并存，窗口端旧者胜）——触发条件出现时
  需小票把数据过滤状态纳入 latest 解析；今日为潜在态，如实记档。
- **elo corpus 侧留白**：fixture_universe 里的假 3-0 会进 elo 评分
  （silver 先于 gold 构建拿不到 admin 旗标；hist 侧 warmup 窗 2016 前
  与排除窗 2023/25 零重叠）——影响限 T1 约 8 场，重开 elo 卫生需名字
  桥/旗标依赖裁决，另票。
