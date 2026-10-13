# 23 引擎解锁实施：hhad/ttg 真实线市场侧接线 + market-λ 转正（裁决 1+2）

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` S15/S16 裁决落地（2026-10-10 用户点头）
Blocked by: （无——15/16 已 resolved，裁决已记档）

## 任务

- **market-λ 转正**：`joint_lambdas`（1X2 fair + OU 半线水位联合 NLS
  反推）从报告模块移入 `evaluation/backtest.py`（引擎拥有定价通道，
  报告侧反向复用，定义单一）。
- **引擎取数扩列**：gold_reader 十年取数补 OU 收盘族（线+水位），
  GoldMatch 携带；同卫生入口不变。
- **候选扩展**（`_candidates_for_match`）：had 保留现状；hhad/ttg 市场
  侧=joint 矩阵（P1 验证口径：1X2+OU）——hhad 线=pick_hhad_line(joint
  矩阵)（整数线，JC 让球胜平负语义=settlement `home+goal_line` 三向），
  ttg=八档 0..7+；模拟价=(1/p_mkt)×(1−haircut)；护栏（MIN_MARKET_PROB/
  max_sim_odds）逐 selection 同 had；无 OU 线/拟合失败 → 该场 hhad/ttg
  无候选（skip 计数），had 不受影响。
- **参数**：`BacktestParams.markets`（缺省 ("had","hhad","ttg")）+ CLI
  `--markets`；模块 docstring 改写（had-only 理由 → 2026-10-10 解锁）。

## 不变量与人裁决项

- 结算仍走 settle_fixed_bet（hhad 腿带 goal_line，ttg 腿八档码）；
  防前视/卫生单一入口/run 溯源不变；重放确定性（joint 拟合确定性）。
- 单 1X2 反推（market_implied_matrix）保留为诊断工具不删。
- 性能：逐场 joint 拟合（~15ms）全量十年 ≈+9min，可接受（CLI 一次性）。
- 人裁决项：已由 15/16 票裁决覆盖（本票纯实施）。

## Out of scope

- crs/hafu 玩法；AH 线进联合拟合约束（P1 只验证了 1X2+OU，加约束另证）；
  haircut/护栏参数（17/18 已裁维持）。

## Answer

2026-10-10 交付（分支 feat/unlock-impl-c23-20261010，PR #140，两提交
4feb790/0f8cc78）：

- **market-λ 转正**：`joint_lambdas` 移入 `evaluation/backtest.py`
  （定价通道归引擎；market_lambda/unlock_report 报告侧 import 复用，
  OU_LINE_MAX/WATER_SANITY_BAND/HALF_STEP_EPS 常量随转正单一落点引擎）。
- **解锁接线**：取数扩 OU 收盘族（同卫生入口）；`market_side_matrix`
  （无干净 OU/四分线/整数线 push 面/水位越带/拟合失败 → None + skip
  计数，had 不受影响）；`_market_side_candidates`——hhad 整数让球线
  （pick_hhad_line 于联合矩阵；settlement `home+goal_line` 三向口径
  correctness 审查逐案追认一致）、ttg 八档 0..7+；模拟价
  (1/p_mkt)×(1−haircut)；护栏逐 selection 同 had；混玩法串关经
  settle_fixed_bet 逐腿按各自市场结算（审查确认无跨市场假设）。
- **params.markets** 缺省三玩法 + CLI --markets（correctness F1：CLI
  接线 bug 已修）；玩法值校验 fail-loud（F3）。
- **验证**：863 全绿 + type/lint/fmt 绿；真树 E0 半季 run=4——三玩法
  355 注（had 189 与解锁前逐位一致=had 零扰动；hhad 51/ttg 132），
  结算正常；重放确定性解锁路径变体逐字节一致（F2）。
- correctness 审查九探（线约定端到端/ttg 码/模型-市场配对/混玩法串关/
  SQL 解包/确定性/参数边界/skip 语义/报告回引）除 F1-F3 外全净。
