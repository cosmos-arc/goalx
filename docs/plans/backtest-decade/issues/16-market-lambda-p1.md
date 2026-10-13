# 16 market-λ 联合反推 P1：1X2+OU 终盘联合 (λ_home, λ_away)（S16）

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` C 相 User Story 16
Blocked by: （无——研究候选，gold 收盘族就绪即可做）

## 任务

research/22-27 候选（"模型≈市场/edge 在执行侧"）：单 1X2 反推 (λh, λa)
欠约束（总进球维度自由度未被市场钉住）；加 OU 终盘线/水位联合反推可
能显著收紧。P1 = 可行性对照：

- 输入：gold era 分层 1X2 收盘（era1 PSC / era2 cid177 锚）+ OU 收盘
  （`ou_close_line_med` + 水位，11 家中位数族与锚行值都试）；
- 方法：1X2+OU 联合拟合 (λh, λa)（数值优化，DC ρ 或 ρ=0 两档）对照
  单 1X2 反推（goal_expectancy 现路径）；
- 判据：比分矩阵拟合误差（对 1X2 概率、OU 线概率、实际比分分布的
  校准）十年分时代/联赛分层对比；
- 产出：`reports/` MD+JSON 报告 + 是否值得转正为定价通道的建议。

**门=报告+用户点头**：转正与否用户裁决；本票不接引擎、不改定价面。

## 不变量与人裁决项

- 单场严禁混源纪律沿用（1X2 与 OU 可同场不同族，但各自按 gold 列
  定义取数）；era 断层如实分层报告。
- 人裁决项：P1 过关标准（拟合误差改善幅度多少算"值得转正"）在报告
  里给建议值，用户拍板。

## Out of scope

- 定价通道接入（裁决后另票）；hhad/ttg 解锁（票 15 独立裁决）。

## Answer

2026-10-10 交付（分支 feat/market-lambda-c16-20261010，PR #137）：

- **代码**：`evaluation/market_lambda.py`（joint=独立泊松 NLS 联合反推
  (λh,λa)，约束=1X2 三向 fair+OU 半线 P(over) 水位去水，single 反推热
  启动；对照 single=goal_expectancy 基线）；票 15 共享面转公共
  （OU_LINE_MAX/half_line_only/waters_sane）；CLI market-lambda-report；
  报告落 reports/。不接引擎。
- **真树结论**（9,929 样本）：joint ou_brier 0.2500/0.2498 ≈ 票 15
  真实线市场水平，single 0.2587/0.2580；1X2 拟合代价仅 0.0006；ttg 桶
  3-5 欠分散 −2.5pp → −1.2/−0.1/+0.2pp（收敛）。
- **判读建议（留用户裁决）**：转正证据强——信息增益大（Brier 追平真实
  线市场）、代价小（1X2 拟合几乎无损）、欠分散结构性修复。转正后可作为
  hhad/ttg 市场侧定价通道（与票 15 解锁联动）。
- correctness 两条低级别入档：goal_expectancy 内部 11×11 网格 vs 评估
  10×10 的 1e-7 级残差（4dp 不可见，网格变更时复查）；joint 失败回落
  single 语义已回归测试钉死。

## Comments

- 2026-10-10 用户裁决：**转正为市场隐含定价通道**（joint=1X2+OU 联合
  反推）；接线随票 23（hhad/ttg 市场侧市场矩阵来源）。
