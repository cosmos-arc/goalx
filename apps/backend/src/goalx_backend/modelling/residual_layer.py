"""
错价桶判别层（model-iterate 票 03，spec S3）。

signal-attribution 发现 8 个 |t|≥2 市场错价桶（特征可预测市场误差，
效应 1-2pp）。本层把该假设放进十年引擎做 walk-forward 终审：特征进
第二阶段残差调整，对照 K2 融合输出问"判别层还有没有增量"——有增量
→ 接缺省管线建议；无增量 → 诚实留空（这本身是答案）。

模型形态（参数量小+正则化，防 1-2pp 效应被噪声吃掉）：

- **带偏置的 softmax 残差调整**：logit_c = log p_fused_c + w_c·x，
  x=错价桶特征向量（窗内标准化）；w 三类各自（3×(F+1) 含截距）；
- **L2 岭罚** λ 固定正则；numpy 梯度下降零初始化定步长定迭代——
  纯确定性（无随机源，重放确定性友好）；
- **walk-forward 协议同 K1/K2**（尾随窗拟合、本周冻结、PIT）；特征
  全为 gold 既有 PIT 列；**安慰剂**：特征按确定性置换变体跑同管线
  （真特征增量 ≈ 安慰剂增量 ⇒ 无真信号）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from goalx_backend.markets import SELECTIONS

# 判别层词表（引擎校验与 CLI choices 皆从此取）
RESIDUAL_MODES = ("off", "on", "placebo")
L2_LAMBDA = 0.5  # 岭罚系数（特征窗内标准化后量级）
GRAD_ITERATIONS = 300  # 梯度下降迭代（固定=确定性）
GRAD_STEP = 0.5
MIN_FIT_ENTRIES = 2
_EPS = 1e-15


def feature_vector(features: Sequence[float | None]) -> np.ndarray:
    """错价桶特征向量（8 维，signal-attribution 八桶连续化；缺失→0）。"""
    return np.array([0.0 if v is None else float(v) for v in features])


class ResidualModel:
    """带偏置 softmax 残差调整（拟合冻结后 apply）。"""

    def __init__(self, weights: np.ndarray, mean: np.ndarray, std: np.ndarray) -> None:
        self._weights = weights  # (3, F+1)
        self._mean = mean
        self._std = std

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

    历史条目=（特征, 融合后三向, ftr）；placebo 模式特征按行号确定性
    置换（同管线同分布、无真信号——安慰剂对照）。
    """

    mode: str
    window: int = 1000
    min_samples: int = 300
    l2: float = L2_LAMBDA
    history: list[tuple[np.ndarray, dict[str, float], str]] = field(
        default_factory=list
    )

    def _window(self) -> list[tuple[np.ndarray, dict[str, float], str]]:
        return self.history[-self.window :] if self.window > 0 else list(self.history)

    def fit_for_week(self) -> ResidualModel | None:
        """热身充分 → 尾随窗拟合返回冻结模型；冷启动 None（透传）。"""
        window = self._window()
        if len(window) < self.min_samples:
            return None
        if self.mode == "placebo":
            # 确定性置换：特征与（结局,概率）行整体错位——断特征↔结局
            # 关联、保边际分布（概率行随结局错位才保 offset 语义）
            entries = [
                (
                    window[i][0],
                    window[(i + 1) % len(window)][1],
                    window[(i + 1) % len(window)][2],
                )
                for i in range(len(window))
            ]
        else:
            entries = window
        return _fit_softmax(entries, self.l2)

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
    fused = np.array(
        [[max(e[1][s], _EPS) for s in SELECTIONS] for e in entries]
    )  # (n, 3)
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
        grad = probs.T @ design / n + l2 * weights / n  # (3, F+1) 岭罚
        weights -= GRAD_STEP * grad
    return ResidualModel(weights, mean, std)
