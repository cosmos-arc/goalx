# 02 K2 市场融合层：log-linear 融合 + α 存活性十年报告

Status: resolved
Spec: `.scratch/model-iterate/spec.md` S2
Blocked by: 01

## 任务

- **融合模块** `modelling/fusion.py`：
  p_final ∝ p_calib^α × p_fair^(1−α)（log-linear，三向归一）；
  α 由尾随窗内学习（网格 α∈[0,1] 步长 0.05，目标=log-score），赛前
  冻结；era 分层 α 与全局 α 两档对照。
- **引擎接线**：融合开关（缺省关）；十年变体重跑。
- **对照报告**（并入 model_layers）：α 轨迹（按周/按 era）、最优 α
  分布、α>0 存活率（模型权重是否被十年数据保留）、融合后
  skill/ROI/EV 实现率 vs 校准-only 基线。

## 不变量与人裁决项

- p_fair=引擎同链公平概率（era 正典，不另造基准）；融合不引入
  新信息源（只重分配模型/市场权重）；
- α 学习窗内拟合 PIT 安全；α=0 是合法解且要显式报告（模型全灭=
  诚实答案）；
- 下注面注意：融合后 EV 收缩（p_final 趋近 p_fair → EV→0）→ 注数
  下降是预期行为，报告须对照注数/水量变化；
- 人裁决项：无（结果汇总进 K3/收官判断）。

## Out of scope

- 判别层特征进融合权重（K3）；多市场族融合（AH/OU 侧不动，1X2 only）。

## Answer

2026-10-14 交付（分支 feat/market-fusion-k2-20261014，PR #146，四提交，
合 main 04516e2）：

- **模块** `modelling/fusion.py`：fuse/fit_alpha（向量化网格 1.7ms/周）/
  FusionState（era 回落协议）/alpha_survival（分位+直方图+分时代+分
  ISO 年）；引擎重构 _LayerState/_WeekLayers 统一冻结协议；α 轨迹入
  run summary。
- **十年答案（run=8 iso+global / run=9 iso+era vs 基线 5）**：
  - **α 基本不存活**——3,842 拟合周 p50=0.0、存活率（α≥0.05）17-19%、
    均值 0.036-0.039（轨迹时代 0.047-0.056 略高）；
  - skill −0.0404→−0.0060（≈市场）、ΣECE 0.0393→0.0141、三元组
    闭合 0.0037→0.0003——"逼近市场+可校准"以几乎抛弃模型权重达成；
  - had 注量 26,568→2,771（EV 收缩预期行为）；ROI −16.7%/−18.7%——
    残存 α 也不产生正 EV；
  - **判读：research/22"融合是唯一出路"在十年数据上定量化兑现；
    K3 判别层的动机相应弱化（融合已把模型侧压到市场），但 8 个市场
    错价桶的残差方向仍是判别层的独立检验面。**
- **三轴**：双轴六修 + correctness 四修（type 闸/冷启动全历史门槛/
  argmax 圆整/mixed 不进时代桶），全带回归测试；930 tests 全绿。
