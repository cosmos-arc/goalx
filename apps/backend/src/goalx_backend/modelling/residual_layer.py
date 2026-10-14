"""
错价桶判别层（model-iterate 票 03，spec S3）。

signal-attribution 发现 8 个 |t|≥2 市场错价桶（特征可预测市场误差，
效应 1-2pp）。本层把该假设放进十年引擎做 walk-forward 终审：特征进
第二阶段残差调整，对照 K2 融合输出问"判别层还有没有增量"——有增量
→ 接缺省管线建议；无增量 → 诚实留空（这本身是答案）。

模型形态（参数量小+正则化，防 1-2pp 效应被噪声吃掉）：

- **带偏置的 softmax 残差调整**：logit_c = log p_fused_c + w_c·x，
  x=错价桶特征向量（窗内标准化）；w 三类各自（3×(F+1) 含截距）；
- **L2 岭罚** λ（数据梯度按 n 均值、罚项不除——λ 即有效强度）；
  numpy 梯度下降零初始化定步长定迭代——纯确定性；
- **walk-forward 协议同 K1/K2**（尾随窗拟合、本周冻结、PIT）；特征
  全为 gold 既有 PIT 列；**安慰剂**：特征按确定性大步长置换跑同管线
  （真特征增量 ≈ 安慰剂增量 ⇒ 无真信号）；周级权重轨迹入 run summary
  供对照报告的系数面（票面"特征重要性/系数表"义务）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NamedTuple, cast

import numpy as np

from goalx_backend.markets import SELECTIONS

# 判别层词表（引擎校验与 CLI choices 皆从此取）
RESIDUAL_MODES = ("off", "on", "placebo")
L2_LAMBDA = 0.5  # 岭罚系数（特征窗内标准化后量级；数据项按 n 均值）
GRAD_ITERATIONS = 300  # 梯度下降迭代（固定=确定性）
GRAD_STEP = 0.5
MIN_FIT_ENTRIES = 2
_EPS = 1e-15


class FeatureTuple(NamedTuple):
    """错价桶八特征（signal-attribution 八桶连续化；None=缺失）。"""

    ah_line_drift: float | None = None
    ou_line_drift: float | None = None
    ah_phase_pre: float | None = None
    ah_phase_inplay: float | None = None
    late_move_h: float | None = None
    late_move24_h: float | None = None
    ah_home_water: float | None = None
    ou_over_water: float | None = None


FEATURE_NAMES = FeatureTuple._fields


def feature_vector(features: Sequence[float | None]) -> np.ndarray:
    """特征元组 → 向量（缺失/非有限→0，层内自防御）。"""
    return np.array(
        [
            0.0 if v is None or not math.isfinite(float(v)) else float(v)
            for v in features
        ]
    )


def placebo_entries(
    entries: list[tuple[np.ndarray, dict[str, float], str]],
) -> list[tuple[np.ndarray, dict[str, float], str]]:
    """
    确定性大步长置换：特征行与（概率,结局）行按 (i·s) % n 重配。

    大步长破坏周内自相关（shift-by-1 会被相邻场关联穿透，安慰剂偏热）；
    s 取与 n 互素的最小 ≥7 步长（correctness F1：s=7 在 7|n 时非双射，
    (fused,ftr) 多重集被破坏）；双射 ⇒ 同边际、特征↔结局无关联。
    状态与测试共用此单一定义。
    """
    n = len(entries)
    stride = 7
    while math.gcd(stride, n) > 1:
        stride += 1
    return [
        (entries[i][0], entries[(i * stride) % n][1], entries[(i * stride) % n][2])
        for i in range(n)
    ]


class ResidualModel:
    """带偏置 softmax 残差调整（拟合冻结后 apply）。"""

    def __init__(self, weights: np.ndarray, mean: np.ndarray, std: np.ndarray) -> None:
        self._weights = weights  # (3, F+1)
        self._mean = mean
        self._std = std

    @property
    def weights(self) -> np.ndarray:
        """冻结权重（系数面报告读取）。"""
        return self._weights

    def apply(self, fused: dict[str, float], features: np.ndarray) -> dict[str, float]:
        """p_adj = softmax(log p_fused + W·[x_std, 1])。"""
        x = np.concatenate([(features - self._mean) / self._std, [1.0]])
        delta = self._weights @ x  # (3,)
        logits = np.array([np.log(max(fused[s], _EPS)) for s in SELECTIONS]) + delta
        logits -= logits.max()
        probs = np.exp(logits)
        probs /= probs.sum()  # softmax 归一
        return {s: float(p) for s, p in zip(SELECTIONS, probs, strict=True)}


@dataclass
class ResidualState:
    """
    联赛作用域判别层状态（K1/K2 同款冻结协议）。

    历史条目=（特征, 融合后三向, ftr）；placebo 模式经 placebo_entries
    置换（断关联、保边际）；weights_log 记周级特征侧 |w| 均值轨迹。
    """

    mode: str
    window: int = 1000
    min_samples: int = 300
    l2: float = L2_LAMBDA
    history: list[tuple[np.ndarray, dict[str, float], str]] = field(
        default_factory=list
    )
    weights_log: list[dict[str, object]] = field(default_factory=list)

    def _window(self) -> list[tuple[np.ndarray, dict[str, float], str]]:
        return self.history[-self.window :] if self.window > 0 else list(self.history)

    def fit_for_week(self) -> ResidualModel | None:
        """热身充分 → 尾随窗拟合返回冻结模型；冷启动 None（透传）。"""
        window = self._window()
        if len(window) < self.min_samples:
            return None
        entries = placebo_entries(window) if self.mode == "placebo" else window
        model = _fit_softmax(entries, self.l2)
        self.weights_log.append(
            {
                "feature_abs_w": {
                    name: round(float(np.abs(model.weights[:, i]).mean()), 5)
                    for i, name in enumerate(FEATURE_NAMES)
                }
            }
        )
        return model

    def record(self, features: np.ndarray, applied: dict[str, float], ftr: str) -> None:
        """一场（特征, 融合后三向, 赛果）入史（本周冻结完毕后调用）。"""
        self.history.append((features, applied, ftr))


def _fit_softmax(
    entries: list[tuple[np.ndarray, dict[str, float], str]], l2: float
) -> ResidualModel:
    """梯度下降拟合（零初始化、固定迭代/步长=确定性）。"""
    if len(entries) < MIN_FIT_ENTRIES:
        raise ValueError("判别层拟合窗样本不足")
    x = np.stack([e[0] for e in entries])  # (n, F)
    outcomes = np.array([SELECTIONS.index(e[2].lower()) for e in entries])  # (n,)
    fused = np.array([[max(e[1][s], _EPS) for s in SELECTIONS] for e in entries])
    mean = x.mean(axis=0)
    std = x.std(axis=0)
    std = np.where(std < _EPS, 1.0, std)  # 零方差特征不除零
    x_std = (x - mean) / std
    design = np.concatenate([x_std, np.ones((len(x_std), 1))], axis=1)  # (n, F+1)
    offset = np.log(fused)  # 偏置=融合三向对数
    weights = np.zeros((3, design.shape[1]))
    n = len(entries)
    for _ in range(GRAD_ITERATIONS):
        logits = offset + design @ weights.T  # (n, 3)
        logits -= logits.max(axis=1, keepdims=True)
        probs = np.exp(logits)
        probs /= probs.sum(axis=1, keepdims=True)
        probs[np.arange(n), outcomes] -= 1.0  # dNLL/dlogit
        grad = probs.T @ design / n + l2 * weights  # 数据项按 n 均值，罚项不除
        weights -= GRAD_STEP * grad
    return ResidualModel(weights, mean, std)


def residual_weights_summary(
    weights_log: list[dict[str, object]],
) -> dict[str, object]:
    """周级权重轨迹 → 系数面汇总（run summary / 对照报告共用）。"""
    if not weights_log:
        return {"n_weeks": 0}
    sums = dict.fromkeys(FEATURE_NAMES, 0.0)
    n = len(weights_log)
    for entry in weights_log:
        per_feature = cast("dict[str, float] | None", entry.get("feature_abs_w"))
        if per_feature is not None:
            for name, value in per_feature.items():
                sums[str(name)] += float(value)
    return {
        "n_weeks": n,
        "mean_abs_w": {
            name: round(value / n, 5) for name, value in sorted(sums.items())
        },
    }
