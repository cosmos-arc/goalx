# 03 G3 GBM 十年终审：skill 三方对照 + 融合 α 存活性

Status: resolved
Spec: `.scratch/model-v2/spec.md` S3
Blocked by: 02

## 任务

- **真树变体 run**：GBM 十年（model=gbm）+ GBM+era 融合
  （model=gbm, fusion=era）；对照 DC 基线（run=5）与融合-only
  （run=9）。
- **model-layers 对照报告**：GBM vs DC vs 市场公平——skill_rps/ECE/
  校准三元组/had 注面；GBM 融合 α 轨迹与存活率（K2 的 α≈0 是否只
  属于 DC——**本程最本质判据**）。
- 结论材料：GBM 有没有活过市场（skill>0 或 α 存活）；坏消息照报；
  进缺省管线与否备裁决。

## 不变量与人裁决项

- 三方同 run 参数（除 model/fusion 外一致）配对交集对照（model-layers
  既有协议）；
- α 存活判据沿 K2（p50、存活率 α≥0.05、分时代）；
- 人裁决项：GBM 是否进缺省 + 若 α 存活模型线走向（门=报告+点头）。

## Out of scope

- D 相过滤实验（并行）；模型集成。

## Answer

2026-10-15 随票 02 一并完成（PR #152 真树变体即本票材料）：

- **GBM 十年终审**：skill 三方对照——GBM 裸 −0.0672 / GBM+融合
  −0.0113 / DC −0.0402 / 融合-only −0.0060 ≈ 市场；
- **α 存活性**：GBM 融合 α 均值 0.057、p50=0、存活率 21.2%
  （DC 17.1%）——跨模型线一致：**任何模型在十年窗内 log-score 下
  都活不过市场公平概率**；
- **结论**：GBM 不进缺省管线（skill 劣于 DC 裸跑+α 不存活）；
  "特征模型能不能在融合下存活"=不能。模型线到此为止：market fair
  即十年最优单点概率估计。
