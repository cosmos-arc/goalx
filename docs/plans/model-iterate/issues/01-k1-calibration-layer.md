# 01 K1 校准层：DC 输出 isotonic/温度缩放 + 十年对照

Status: resolved
Spec: `.scratch/model-iterate/spec.md` S1
Blocked by: （无）

## 任务

- **校准模块** `modelling/calibration.py`：
  - 温度缩放（单参数 T，三向共享或分侧）与 isotonic（h/d/a 分侧，
    单调回归）两法实现，walk-forward 协议：训练窗内拟合、赛前冻结、
    应用到本周预测（PIT 断言随引擎常开）；
  - 拟合输入=引擎既有预测/结算流（fair 校准目标=p_model vs 1[ftr]），
    窗长/分侧粒度为可调参数，缺省由 K1 报告定。
- **引擎接线**：`BacktestParams` 加校准开关（缺省关=行为不变），
  十年变体重跑（run=6+）。
- **对照报告** `evaluation/model_layers.py` + CLI：校准前后
  ECE（h/d/a 分侧）/RPS skill/EV 实现率/校准快照（模型 vs 隐含 vs
  实际三元组），落树 reports/model-layers.{json,md}。

## 不变量与人裁决项

- 校准器只用训练窗内样本（无前视）；温度缩放保序、isotonic 单调
  不降；概率归一（三向和=1）拟合后重归一；
- 缺省关闭：不合入即零行为变化（重放确定性回归保护）；
- 报告必须给"校准修复了多少失准"结论句（对照 0.334/0.246/0.236
  三元组基线）；
- 人裁决项：无（纯模型面；K2/K3 是否继续看本票报告，不设门）。

## Out of scope

- 融合层（K2）；判别层（K3）；下注面参数。

## Answer

2026-10-14 交付（分支 feat/calibration-layer-k1-20261014，PR #145，
三提交，合 main 95feb9b）：

- **模块** `modelling/calibration.py`：温度缩放（黄金分割 NLL 搜 1/T）
  + isotonic（h/d/a 分侧 PAVA 自实现）；CALIBRATION_KINDS 词表单一
  定义；未拟合即用 fail-loud；isotonic 曲线全零退化回退原始三向
  （correctness F1 实证反例修复）。
- **引擎接线**：calibration 三参（缺省 off 零行为变化）；_CalibrationState
  按联赛作用域 + walk-forward 冻结（本周赛果不进拟合）；历史只累积
  原始三向；作用于 1X2 预测面+had 下注侧（hhad/ttg=原始矩阵）。
- **对照报告** model_layers（配对交集对称；ECE/三元组/skill/EV 实现
  率/had 注面）。
- **十年发现（run=6 temp / run=7 iso vs 基线 5）**：
  - 基线在全预测集均值近无偏（h 0.4396 vs 0.4421）——票 19 的
    0.334/0.236 高估在下注子集（选择偏差），报告已声明口径差；
  - temp：ΣECE −25%（0.0393→0.0296）但 skill 略降、三元组差距反扩；
  - iso：三元组闭合 0.0037→0.0015、EV 实现率 −0.456→−0.371、
    had ROI −16.78%→−15.54%，skill −0.0471 略降；
  - **判读：校准动不了排序问题（瓶颈在 misranking）——K2 融合动机
    强化**。
- **三轴**：双轴四修 + correctness 七修（全零回退/参数前置校验/
  配对对称/判读三分支/冷启动计数更名周/PIT 二次拟合回归），全带
  回归测试；919 tests / type / lint / fmt 绿。
