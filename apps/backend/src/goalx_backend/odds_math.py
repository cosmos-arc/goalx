"""赔率数学：隐含概率归一、Shin 去晦与 EV 计算（spec §2 方向 A 基准）。"""

from __future__ import annotations

from collections.abc import Iterable

_MIN_OUTCOMES = 2


def raw_implied(odds: float) -> float:
    """Overround-inclusive implied probability ``1/odds``."""
    return 1.0 / odds


def normalized_implied(odds: tuple[float, ...]) -> tuple[float, ...]:
    """Naive de-vig: scale raw implied probabilities to sum to one."""
    raw = [1.0 / o for o in odds]
    total = sum(raw)
    return tuple(p / total for p in raw)


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


def expected_value(probability: float, odds: float) -> float:
    """EV of a unit stake: ``p * odds - 1``."""
    return probability * odds - 1.0
