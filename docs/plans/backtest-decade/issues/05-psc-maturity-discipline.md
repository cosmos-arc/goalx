# 05 PSC 成熟度纪律：开球不足 6 个月的收盘列视为暂定

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` A 组 User Story 5

## 任务

硬规则：开球日 + 6 个月 ≤ today 才可用的 fdhist 行 PSC/PSH/AvgC；不足
6 个月视为暂定，不进 fair 基准与校准（源回填滞后约 5 个月，未收敛数值
污染）。实现为统一谓词，fair 基准与判门②共用同一份。

## 不变量与人裁决项

- 谓词=日历月加法（月末钳制：2024-08-31+6mo=2025-02-28），含端点
  （+6mo 当天即可用）；`data/hygiene.psc_mature(match_date, today=)`。
- 接线点（spec 点名的消费面）：
  - fair 基准 `evaluation/backtest.fair_probs_from_close`：未成熟行
    PSC/AvgC 一律视为缺席 → 返回 None（auto/psc/avgc 三种来源同判），
    引擎侧落入既有 no_fair_baseline 桶不下注；today 缺省=当天，测试
    可注入固定日期。
  - 判门② `gate2_psc_anchor(..., today=)`（必传）：未成熟对跳过比较，
    单列 `immature_skipped`；2025/26 门② median 1.3% 即未成熟轨迹，
    纪律落地后自然消解（spec Further Notes 预言兑现，真树数字见票 03）。
- 消费面盘点（诚实留白，不静默扩面）：
  - haircut 校准（`evaluation/haircut.py`）读 odds_snapshots 配对面，
    不消费 fdhist 收盘列——谓词无接线点，C17 十年重估若引 PSC 再接；
  - `baseline_quality_report` 基准分期质检=契约冻结面（票 06 键集），
    保持描述统计全量（含未成熟行如实可见），过滤另开票走契约变更；
  - drift/pool 回放（`drift_replay`/`pool_replay`）有"纯函数逐字节
    一致"契约——重放到近窗时由 C/D 相显式传 today 接线，本票不动。
- 引擎入口的统一生效（与排除表一起）=票 14（C 相）。

## Answer

2026-10-08 交付（分支 feat/hygiene-a4-a5-20261008）：

- `data/hygiene.psc_mature` + `_plus_months`（calendar 钳制，零新依赖，
  PSC_MATURITY_MONTHS=6 常量）。
- `fair_probs_from_close` 硬规则接线（未成熟=无基准）；引擎模块
  docstring 同步。
- 判门② today 必传 + immature_skipped 单列（JSON+MD 两视图）+ 模块
  docstring 门②段更新。
- 测试：边界（含端点/月末钳制/闰年）三反例；fair_probs 未成熟→None、
  +6mo 当天→可用；合成树全链（today 落开球+6mo 内 → immature_skipped
  =3、usable 0、verdict insufficient、MD 可见）。
- 真树判门复查数字见票 03：immature_skipped=1,181 截走、门② median 0
  维持 pass；2025-26 成熟残余 median 1.3% 仍在（非未成熟所致，留账
  待 C/D 相复查）。
- correctness-review 加固：run_backtest 开头钉住成熟度基准日一次（跨
  午夜不换口径）并以 fair_maturity_today 入档 run params——同输入不同
  日重跑的尾窗行判定差异可由该字段解释（重放契约）。
