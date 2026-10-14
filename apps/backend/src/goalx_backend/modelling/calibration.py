"""
校准层（model-iterate 票 01，spec S1；research/22 P0 路线第一步）。

十年回测钉死 DC 概率系统性高估（had：模型 0.334 vs 隐含 0.246 vs
实际 0.236），校准先于准确率（Walsh & Joshi 方向性结论，证据 A-）。
两法实现，walk-forward 协议由引擎侧施加（训练窗内拟合、赛前冻结）：

- **温度缩放**（单参数 T，三向共享）：p' ∝ p^(1/T)，黄金分割最小化
  NLL。保序（只整温不重排），样本需求小；
- **isotonic**（h/d/a 分侧 PAVA 单调回归）：p'_c = iso_c(p_c) 后三向
  重归一。可修非保序失准（如平局桶），样本需求大。

两法均为纯确定性函数（无随机源，重放确定性友好）；PAVA 自实现
（仓库无 sklearn）。拟合样本 =（原始模型三向概率, 赛果字母）；
apply 前必须 fit（未拟合即用=程序错误 fail-loud）。
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from typing import Protocol

import numpy as np

from goalx_backend.markets import SELECTIONS

MIN_FIT_SAMPLES = 2  # 拟合最少样本（低于即引擎侧不启用）
_TEMP_LO, _TEMP_HI = 0.2, 5.0  # T 搜索区间
_TEMP_ITERS = 48  # 黄金分割迭代（区间收敛 ~1e-8）


class Calibrator(Protocol):
    """校准器协议：fit（原始样本）→ apply（概率 dict 进出）。"""

    def fit(self, samples: list[tuple[dict[str, float], str]]) -> None:
        """在（原始三向概率, 赛果字母）样本上拟合。"""
        ...

    def apply(self, probs: dict[str, float]) -> dict[str, float]:
        """把拟合冻结的校准映射应用到一组三向概率。"""
        ...


def _renormalize(probs: dict[str, float]) -> dict[str, float]:
    """三向重归一（和≤0/非有限=退化 → 原样返回不冒充校准）。"""
    total = sum(probs.values())
    if total <= 0.0 or not np.isfinite(total):
        return dict(probs)
    return {side: value / total for side, value in probs.items()}


class TemperatureCalibrator:
    """单参数温度缩放（p' ∝ p^(1/T)；T>1 削峰、T<1 锐化）。"""

    def __init__(self) -> None:
        self._inverse_t: float | None = None

    def fit(self, samples: list[tuple[dict[str, float], str]]) -> None:
        """黄金分割最小化 NLL（搜索 1/T；向量化、确定性）。"""
        if len(samples) < MIN_FIT_SAMPLES:
            raise ValueError(f"温度缩放至少 {MIN_FIT_SAMPLES} 样本")
        logs = np.array(
            [
                [np.log(max(probs[side], 1e-15)) for side in SELECTIONS]
                for probs, _ in samples
            ]
        )
        # ftr 大小写双轨（引擎大写 H/D/A、SELECTIONS 小写）——此处归一
        outcomes = np.array([SELECTIONS.index(ftr.lower()) for _, ftr in samples])

        def nll(inverse_t: float) -> float:
            scaled = logs * inverse_t  # log p^(1/T)
            scaled -= scaled.max(axis=1, keepdims=True)  # 数值稳定
            log_probs = scaled - np.log(np.exp(scaled).sum(axis=1, keepdims=True))
            return float(-log_probs[np.arange(len(outcomes)), outcomes].mean())

        lo, hi = 1.0 / _TEMP_HI, 1.0 / _TEMP_LO  # 1/T 区间（T 区间倒数）
        ratio = (5**0.5 - 1) / 2
        x1 = hi - ratio * (hi - lo)
        x2 = lo + ratio * (hi - lo)
        f1, f2 = nll(x1), nll(x2)
        for _ in range(_TEMP_ITERS):
            if f1 <= f2:
                hi, x2, f2 = x2, x1, f1
                x1 = hi - ratio * (hi - lo)
                f1 = nll(x1)
            else:
                lo, x1, f1 = x1, x2, f2
                x2 = lo + ratio * (hi - lo)
                f2 = nll(x2)
        self._inverse_t = float((lo + hi) / 2)

    @property
    def temperature(self) -> float:
        """已拟合温度 T=1/(搜索值)（未拟合=错误）。"""
        if self._inverse_t is None:
            raise RuntimeError("TemperatureCalibrator 未拟合即取温度")
        return 1.0 / self._inverse_t

    def apply(self, probs: dict[str, float]) -> dict[str, float]:
        """P' ∝ p^(1/T) 后归一（未拟合即用=程序错误）。"""
        if self._inverse_t is None:
            raise RuntimeError("TemperatureCalibrator 未拟合即应用")
        raw = {side: max(probs[side], 1e-15) ** self._inverse_t for side in SELECTIONS}
        return _renormalize(raw)


def pava_fit(probs: list[float], outcomes: list[float]) -> Callable[[float], float]:
    """
    Isotonic 回归（PAVA，单调不降）→ 阶梯映射 x→E[y|x]。

    输入按 x 升序（调用方保证）；同 x 合并（和/计数聚合）。返回闭包：
    越界端点用最近阶梯值外推（isotonic 惯例）。
    """
    if len(probs) != len(outcomes) or not probs:
        raise ValueError("PAVA 输入非空且等长")
    # 同 x 聚合（ys=和，ws=计数）
    xs: list[float] = []
    ys: list[float] = []
    ws: list[float] = []
    for x, y in zip(probs, outcomes, strict=True):
        if xs and x == xs[-1]:
            ys[-1] += y
            ws[-1] += 1.0
        else:
            xs.append(x)
            ys.append(y)
            ws.append(1.0)
    # PAVA：栈上违反单调（前均值 > 后均值）即合并（加权和/计数）
    i = 1
    while i < len(ys):
        if i > 0 and ys[i - 1] / ws[i - 1] > ys[i] / ws[i]:
            ys[i - 1] += ys[i]
            ws[i - 1] += ws[i]
            del xs[i], ys[i], ws[i]
            i -= 1
        else:
            i += 1
    values = [y / w for y, w in zip(ys, ws, strict=True)]

    def evaluate(x: float) -> float:
        if not xs:
            return 0.0
        index = bisect_right(xs, x)
        return values[index - 1] if index > 0 else values[0]

    return evaluate


class IsotonicCalibrator:
    """h/d/a 分侧 isotonic + 三向重归一。"""

    def __init__(self) -> None:
        self._curves: dict[str, Callable[[float], float]] | None = None

    def fit(self, samples: list[tuple[dict[str, float], str]]) -> None:
        """分侧拟合阶梯映射（样本按侧概率升序喂 PAVA）。"""
        if len(samples) < MIN_FIT_SAMPLES:
            raise ValueError(f"isotonic 至少 {MIN_FIT_SAMPLES} 样本")
        curves: dict[str, Callable[[float], float]] = {}
        for side in SELECTIONS:
            ordered = sorted(samples, key=lambda s: s[0][side])
            curves[side] = pava_fit(
                [probs[side] for probs, _ in ordered],
                [1.0 if ftr.lower() == side else 0.0 for _, ftr in ordered],
            )
        self._curves = curves

    def apply(self, probs: dict[str, float]) -> dict[str, float]:
        """分侧阶梯映射后重归一（未拟合即用=程序错误）。"""
        if self._curves is None:
            raise RuntimeError("IsotonicCalibrator 未拟合即应用")
        return _renormalize(
            {side: self._curves[side](probs[side]) for side in SELECTIONS}
        )


def make_calibrator(kind: str) -> Calibrator:
    """工厂：off/temperature/isotonic（未知值 fail-loud）。"""
    if kind == "temperature":
        return TemperatureCalibrator()
    if kind == "isotonic":
        return IsotonicCalibrator()
    raise ValueError(f"未知校准 {kind!r}（合法: off/temperature/isotonic）")
