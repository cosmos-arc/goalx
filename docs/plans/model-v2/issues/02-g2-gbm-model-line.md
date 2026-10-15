# 02 G2 GBM 模型线：lightgbm walk-forward + 引擎变体接线

Status: resolved
Spec: `.scratch/model-v2/spec.md` S2
Blocked by: 01

## 任务

- **新依赖** lightgbm（pyproject + uv lock；MIT wheel）。
- **模型模块** `modelling/gbm_model.py`：
  - 特征向量：G1 滞后特征 + era 正典公平概率（市场是最强特征）+
    收盘族漂移/相位（K3 同源 8 特征）；缺值留 NaN（lightgbm 原生）；
  - 多分类 booster（h/d/a），按联赛周重估（同 DC 协议：训练池推进、
    本周冻结、PIT 断言）；deterministic=true + seed 固定（重放确定性）；
  - 训练样本=训练池场次（其特征由 G1 滚动面供给——训练池场次自身的
    lag 特征也须可算，特征面在 run 开头对全部 gold 行一次构建）。
- **引擎接线**：`BacktestParams.model`（"dc"|"gbm"，缺省 dc 零行为
  变化）；gbm 路径 **had-only**（hhad/ttg 候选跳过计数不冒充）；
  校准/融合/判别链接力（GBM 输出即模型侧源头）。
- 单测：合成可分特征学得出/不可分不崩/确定性重放/引擎变体全链
  （gold_env）+ hhad/ttg 跳过计数。

## 不变量与人裁决项

- 重放确定性：lightgbm deterministic 参数 + 固定 seed + 特征集定序；
- had-only 诚实范围：run params 记档 model=gbm 时玩法面=had；
- 训练池与预测面同特征链（不吃两套口径）；
- 人裁决项：GBM 进缺省管线（G3 终审报告+点头）。

## Out of scope

- 融合 α 存活性（G3）；比分矩阵 from GBM；多模型集成。

## Answer

2026-10-15 交付（分支 feat/gbm-model-g2-20261015，PR #152，三提交，
合 main 9227636）：

- **模块** `modelling/gbm_model.py`（lightgbm 多分类 20 维特征：
  G1 滞后 9 + 公平概率 3 + 错价桶 8；deterministic+seed）；
  引擎 `model=gbm` had-only 接线（hhad/ttg 跳过计数）；GBM 池与
  DC 池同步推进（冷启动修复）；lag 面旁路（fetch 扩 9 列+升序重排）。
- **真树（run=12 GBM / 13 GBM+融合 vs 基线 5）**：
  - GBM 裸跑 skill −0.0672（差于 DC——三分类直接学过拟合），但 had
    ROI −11.1% 优于 DC −16.4%；
  - GBM+融合 skill −0.0113、α 均值 0.057/存活率 21%（vs DC 17%）
    ——**α 依然不存活：换血模型线不改变 K2 结论**。
- 974 tests / type 0 / lint / fmt 绿。
