# 18 既有护栏十年复验：模拟价天花板/市场概率下限等（S18）

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` C 相 User Story 18
Blocked by: 13

## 任务

引擎护栏在新数据上复验（十年 48,561 场的极端档行为与五大三季不同量级）：

- `DEFAULT_MAX_SIM_ODDS=50`（模拟价天花板）、`MIN_MARKET_PROB=0.02`
  （市场隐含概率下限）、`MARKET_MAX_GOAL_ERROR`（反推拟合误差上限）
  等常量在十年分时代数据上的触发面统计（触发率/触发分布/被拦候选
  的实际结局——拦对了还是拦掉了真 edge）；
- era2 共识兜底（traj_cons）与 era1 AvgC 兜底的尾部行为对照；
- 护栏参数**只调有数据依据的**：每处拟调整给触发面证据；无证据不动。

## 不变量与人裁决项

- 护栏语义（为什么存在：模型-市场尾部分歧噪声，见 backtest.py 模块
  docstring）不因复验弱化；调整须逐参数举证。
- 人裁决项：任何参数值变更最终由用户点头。

## Out of scope

- 新增护栏类型（有证据另开票）；hhad/ttg 尾部（had-only 期间无此面）。

## Answer

2026-10-10 交付（分支 feat/guardrail-c18-20261010，PR #139）：

- **代码**：`evaluation/guardrail_report.py`（触发面/被拦结局/反推失败率/
  fair 源配比，CLI --haircut 直通）；报告落 reports/guardrail-decade。
- **真树结论**（haircut 0.10/0.1128 两值一致）：触发面 era1 0.03% +
  era2 0.01%；被拦 29 selection 实际命中 0（隐含均值 0.0166）——拦的
  全是尾部噪声零真信号损失；反推误差护栏 34,166 场零触发；era2 共识
  兜底仅 2 场。MIN_MARKET_PROB=1/50=max_sim_odds 倒数恒等（任意
  haircut 同触发，非 0.10 巧合）。
- **判读建议（留用户）**：三条护栏全维持现状，无任何调参的数据依据。

## Comments

- 2026-10-10 用户裁决：**护栏全维持现状**（无调参依据）。
