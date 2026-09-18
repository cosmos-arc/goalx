"""赔率数学：隐含概率归一、Shin 去晦与 EV 计算（spec §2 方向 A 基准）。"""

from __future__ import annotations

from collections.abc import Iterable

_MIN_OUTCOMES = 2

# 共识分母护栏（票 39）：完整三向 book 数低于该值时共识打低置信标记。
# 建议值 4（调研 odds-consensus-methodology.md §3.3/§5.1：亚洲联赛 book 覆盖
# 缩水，分母 <4 时共识可信度不足），待人追认——调整只改这一个常量。
LOW_CONFIDENCE_BOOK_THRESHOLD = 4


def raw_implied(odds: float) -> float:
    """Overround-inclusive implied probability ``1/odds``."""
    return 1.0 / odds


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

    low, high = 0.5, 2.0
    for _ in range(80):
        mid = (low + high) / 2.0
        if total(mid) > 1.0:
            low = mid
        else:
            high = mid
    z = (low + high) / 2.0
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

    low, high = 0.0, 0.9999
    for _ in range(60):
        mid = (low + high) / 2.0
        if total_prob(mid) > 1.0:
            low = mid
        else:
            high = mid
    probs = _shin_probabilities((low + high) / 2.0, raw)
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


def expected_value(probability: float, odds: float) -> float:
    """EV of a unit stake: ``p * odds - 1``."""
    return probability * odds - 1.0
