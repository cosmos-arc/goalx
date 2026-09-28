"""赔率数学：隐含概率归一、Shin 去晦与 EV 计算（spec §2 方向 A 基准）。"""

# scipy 的类型存根不完整，以下规则的第三方 unknown 在本文件放宽（同 metrics.py）。
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from scipy.optimize import brentq

_MIN_OUTCOMES = 2

# 共识分母护栏（票 39）：完整三向 book 数低于该值时共识打低置信标记。
# 建议值 4（调研 odds-consensus-methodology.md §3.3/§5.1：亚洲联赛 book 覆盖
# 缩水，分母 <4 时共识可信度不足），待人追认——调整只改这一个常量。
LOW_CONFIDENCE_BOOK_THRESHOLD = 4


def normalized_implied(odds: tuple[float, ...]) -> tuple[float, ...]:
    """Naive de-vig: scale raw implied probabilities to sum to one."""
    raw = [1.0 / o for o in odds]
    total = sum(raw)
    return tuple(p / total for p in raw)


def power_implied(odds: tuple[float, ...]) -> tuple[float, ...]:
    """Power de-vig: find z with ``sum((1/o)**z) = 1``（票 35 敏感性对照）。"""
    raw = [1.0 / o for o in odds]
    if sum(raw) <= 1.0 + 1e-12:
        return normalized_implied(odds)

    def total(z: float) -> float:
        return sum(p**z for p in raw)

    def residual(z: float) -> float:
        return total(z) - 1.0

    # 原二分区间 [0.5, 2.0]；区间内无根的病态盘口取上界（原二分的极限行为）
    z = (
        2.0
        if total(2.0) > 1.0
        else cast("float", brentq(residual, 0.5, 2.0, xtol=1e-15))
    )
    probs = tuple(p**z for p in raw)
    scale = sum(probs)
    return tuple(p / scale for p in probs)


def _shin_probabilities(z: float, raw: list[float]) -> list[float]:
    """Shin inverse for one bookmaker-probability vector at insider rate z."""
    total = sum(raw)
    p = [
        (((z * z + 4.0 * (1.0 - z) * pi * pi / total) ** 0.5) - z) / (2.0 * (1.0 - z))
        for pi in raw
    ]
    return p


def shin_implied(odds: tuple[float, ...]) -> tuple[float, ...]:
    """Shin de-vigged probabilities; fair/degenerate input falls back to naive."""
    raw = [1.0 / o for o in odds]
    if sum(raw) <= 1.0 + 1e-12 or len(raw) < _MIN_OUTCOMES:
        return normalized_implied(odds)

    def total_prob(z: float) -> float:
        return sum(_shin_probabilities(z, raw))

    def residual(z: float) -> float:
        return total_prob(z) - 1.0

    # 原二分区间 [0.0, 0.9999]；区间内无根的病态盘口取上界（原二分的极限行为）
    z = (
        0.9999
        if total_prob(0.9999) > 1.0
        else cast("float", brentq(residual, 0.0, 0.9999, xtol=1e-15))
    )
    probs = _shin_probabilities(z, raw)
    scale = sum(probs)
    return tuple(p / scale for p in probs)


def consensus_odds(book_odds: Iterable[dict[str, float]]) -> tuple[float, ...] | None:
    """
    Average price per selection across books → one consensus odds vector.

    ``book_odds`` maps selection code → per-book decimal price. Returns None
    when any selection is missing from every book (对照不完整)。
    """
    per_selection = [list(prices.values()) for prices in book_odds]
    if not per_selection or any(not prices for prices in per_selection):
        return None
    return tuple(sum(prices) / len(prices) for prices in per_selection)


def consensus_low_confidence(
    books: int, *, threshold: int = LOW_CONFIDENCE_BOOK_THRESHOLD
) -> bool:
    """共识分母护栏（票 39）：books < 阈值（默认 4）时共识打低置信标记。"""
    return books < threshold


def consensus_probs(
    by_selection: dict[str, dict[str, float]],
    selections: Iterable[str],
) -> tuple[int, tuple[float, ...]] | None:
    """
    三向共识装配单一正典（今日列表页与场次详情页同源，review-20260928 票 05）。

    ``by_selection``: selection → {book: price}（fx_store.eu_book_odds 形态）；
    ``selections``：选定顺序（调用方传 markets.SELECTIONS——odds_math 不越层
    引 markets，分层契约 lint-imports 强制）。任一向无价（对照不完整）返回
    None；否则返回 (书数 = 最大向价数, Shin 去水概率按 selections 序)。
    """
    ordered = tuple(selections)
    consensus = consensus_odds([by_selection.get(s) or {} for s in ordered])
    if consensus is None:
        return None
    books = max(len(prices) for prices in by_selection.values())
    return books, shin_implied(consensus)


def expected_value(probability: float, odds: float) -> float:
    """EV of a unit stake: ``p * odds - 1``."""
    return probability * odds - 1.0
