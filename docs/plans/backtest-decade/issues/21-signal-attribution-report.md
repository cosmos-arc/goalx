# 21 信号级归因：AH/OU 收盘族特征 vs 模型残差

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` S21
Blocked by: 19

## 任务

- **新报告模块** `evaluation/signal_attribution.py` + CLI 子命令：数据 =
  票 19 十年跑的预测残差（模型概率 − 市场概率，按 ftr 三向）× gold
  收盘族特征（gold_reader 只读桥取，PIT 纪律不变）。
- **特征轴**（gold 已有列，不新建特征）：
  - 漂移：`ah_line_drift` / `ou_line_drift` / `ah_anchor_drift`、
    open→close 线移；
  - 共识与分歧：`ah_n_books`/`ou_n_books`、`*_close_dispersion`、
    锚 vs 中位共识；
  - 相位：`*_n_early`/`*_n_pre`/`*_n_inplay` 计数；
  - 水位与临场：close waters、q1h/q24h 切片（1x2 + AH）。
- **归因面**：特征分桶 × 残差均值 × 模型命中——旧结论"模型≈市场"
  在哪些桶松动（模型在该桶有系统性超额信息？）。
- **对照面**：市场自身可预测性——残差方向能否被收盘族特征预测
  （能 = 市场未消化信息 = 策略空间）。
- 报告落 `reports/` MD+JSON。

## 不变量与人裁决项

- 无前视：只用 gold PIT 列与 run 预测；赛后列（home_goals 等）仅作
  结算标签侧；
- 观察性盘点：不进引擎、不改策略参数、不新建 gold 列；
- 多重比较诚实：分桶结论须带 n 与效应量，报告声明探索性；
- 代称红线照旧（书商一律 cid/代称）；
- 人裁决项：松动桶是否值得开新特征票（票 22 提出，不自动开）。

## Out of scope

- 特征进 gold/模型（下一迭代票）；NewsShock；彩池份额。

## Answer

2026-10-14 交付（分支 feat/signal-attribution-c21-20261014，PR #143，
三提交，合 main cec193c）：

- **报告模块** `evaluation/signal_attribution.py` + CLI
  `signal-attribution-report`：model_edge（三向 h/d/a）× realized
  分桶归因，18 特征面（漂移 3/分歧 2/书数 2/相位 6/水位 2/临场
  切片 3——q1h/q24h 1x2 位移 + AH 锚同线水位位移，跨线不比）；
  报告落真树 reports/signal-attribution.{json,md}（run=5，
  33,883/33,883 样本）。
- **十年发现**：
  - **"模型≈市场"不松动**：零松动桶（总体捕捉 r_h=−0.0128 反向；
    全特征×桶无一致方向）；
  - **但市场自身有 8 个显著错价桶**（|t|≥2，模型全部未捕捉）：
    ou_line_drift:pos（h +0.0118, t=2.39）、ah_phase_pre:low
    （h +0.0116, t=2.62）、ah_phase_pre:mid（−0.0097）、
    ah_phase_inplay:high（−0.0093）、late_move_h:pos（h −0.0188,
    t=−2.6 + d +0.0164）、late_move24_h 两桶——特征预测市场误差
    可行（策略空间存在），DC 模型没吃到。
- **review 战果**：双轴 Spec 审查抓到 key/列错位真 bug（_signal_of
  按 key 存、分桶按 column 查——分歧/相位/水位六轴整族静默消失，
  修复后实报告 6 面→18 面，错价桶 3→8）；correctness 0 error
  （取数定序/非 dict JSON 护栏两条 info 修复）；903 tests /
  type / lint / fmt 全绿。
- 18 特征面全为 gold 既有 PIT 列，不新建特征；观察性报告不进引擎。
- 人裁决项（松动桶是否开新特征票）→ 汇总进票 22。
