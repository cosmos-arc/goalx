"""
GBM 模型线（model-v2 票 02，spec S2）。

spec-v1.0 v2 计划的兑现（xG+GBM stacking）：lightgbm 多分类（h/d/a）
按联赛周重估 walk-forward（同 DC 协议），替换/并行于 DC 作为 1X2
模型侧源头。

- **特征**：G1 滞后特征（elo/xG/技统/积分差量）+ era 正典公平概率
  （市场是最强单一特征——GBM 学"残差修正"形态）
  + 错价桶特征 7 维（measure-05 剔除 inplay）；
  缺值留 NaN（lightgbm 原生处理，不插补）；
- **确定性**：deterministic=true + 固定 seed + 特征名定序（重放确定性
  与 DC 同纪律）；
- **had-only 诚实范围**：GBM 不产比分矩阵（无 λ）——hhad/ttg 模型侧
  仍走 DC/市场侧，GBM run 的 hhad/ttg 候选跳过计数不冒充；
- 训练样本=联赛训练池（cutoff 前）场次，特征经 G1 旁路面（lag_face）。
"""

from __future__ import annotations

# lightgbm 存根不全
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
from dataclasses import dataclass
from typing import Any

import lightgbm as lgb
import numpy as np

from goalx_backend.markets import SELECTIONS

GBM_SEED = 20261015
MIN_GBM_SAMPLES = 2  # 训练最少样本
GBM_FEATURES = (
    # G1 滞后差量
    "elo_diff",
    "xg_for_diff",
    "xg_against_diff",
    "shots_for_diff",
    "shots_against_diff",
    "sot_for_diff",
    "sot_against_diff",
    "goal_diff_diff",
    "points_diff",
    # 市场公平概率（era 正典）——最强单一特征
    "fair_h",
    "fair_d",
    "fair_a",
    # 错价桶特征（K3 同源；ah_phase_inplay 已剔除——measure-05 PIT 契约：
    # 赛中相位计数定义上开球后才可得，不进赛前特征集）
    "ah_line_drift",
    "ou_line_drift",
    "ah_phase_pre",
    "late_move_h",
    "late_move24_h",
    "ah_home_water",
    "ou_over_water",
)


@dataclass(frozen=True)
class GBMArtifact:
    """一次 GBM 周拟合的冻结产物（predict 只读）。"""

    booster: Any  # lightgbm.Booster（存根不全走 Any）
    feature_names: tuple[str, ...]
    train_window_end: str
    data_fingerprint: str

    def predict(self, features: dict[str, float | None]) -> dict[str, float]:
        """单场预测 → h/d/a 概率（缺特征 NaN 入 lightgbm）。"""
        row = np.array(
            [
                [np.nan if (value := features.get(name)) is None else float(value)]
                for name in self.feature_names
            ]
        ).T
        probs = self.booster.predict(row, num_iteration=-1)[0]
        return dict(zip(SELECTIONS, (float(p) for p in probs), strict=True))


def feature_row(
    lag: dict[str, float | None],
    fair_probs: dict[str, float],
    bucket: dict[str, float | None],
) -> dict[str, float | None]:
    """滞后面+公平概率+错价桶 → GBM 特征行（缺=NaN 由 predict 转）。"""
    row: dict[str, float | None] = dict(lag)
    row["fair_h"] = fair_probs.get("h")
    row["fair_d"] = fair_probs.get("d")
    row["fair_a"] = fair_probs.get("a")
    row.update(bucket)
    return row


def fit_gbm_model(
    samples: list[tuple[dict[str, float | None], str]],
    *,
    train_window_end: str,
) -> GBMArtifact:
    """
    联赛训练池样本 → lightgbm 多分类。

    samples 每项 =（特征行, 赛果字母）；确定性：seed 固定 +
    deterministic 参数 + 特征定序。样本 <MIN_TRAIN 时抛 ValueError
    （引擎层按 thin week 计数跳过）。
    """
    if len(samples) < MIN_GBM_SAMPLES:
        raise ValueError(f"GBM 训练样本不足（{len(samples)}<{MIN_GBM_SAMPLES}）")
    matrix = np.array(
        [
            [
                np.nan if (value := sample[0].get(name)) is None else float(value)
                for name in GBM_FEATURES
            ]
            for sample in samples
        ]
    )
    labels = np.array([SELECTIONS.index(sample[1].lower()) for sample in samples])
    dataset = lgb.Dataset(matrix, label=labels, feature_name=list(GBM_FEATURES))
    params = {
        "objective": "multiclass",
        "num_class": 3,
        "verbosity": -1,
        "seed": GBM_SEED,
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": 1,
        "learning_rate": 0.05,
        "num_leaves": 15,
        "min_data_in_leaf": 20,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
    }
    booster = lgb.train(
        params, dataset, num_boost_round=100, callbacks=[lgb.log_evaluation(0)]
    )
    return GBMArtifact(
        booster=booster,
        feature_names=GBM_FEATURES,
        train_window_end=train_window_end,
        data_fingerprint=f"lgbm-{len(samples)}",
    )
