"""odds_math：归一化、Shin 去晦、共识与 EV 测试。"""

from __future__ import annotations

import pytest

from goalx_backend import odds_math as om


def test_normalized_implied_sums_to_one() -> None:
    probs = om.normalized_implied((2.0, 3.5, 4.0))
    assert sum(probs) == pytest.approx(1.0)
    assert probs[0] > probs[1] > probs[2]


def test_shin_fair_odds_returns_uniform() -> None:
    probs = om.shin_implied((3.0, 3.0, 3.0))
    for p in probs:
        assert p == pytest.approx(1 / 3, abs=1e-6)


def test_shin_devig_sums_to_one_and_favors_favorite() -> None:
    # 典型竞彩盘：主胜热门带 overround
    probs = om.shin_implied((1.28, 5.10, 6.60))
    assert sum(probs) == pytest.approx(1.0, abs=1e-9)
    assert probs[0] < 1 / 1.28  # 去晦主要从热门侧挤出水分
    assert probs[0] > probs[1] > probs[2]


def test_shin_no_overround_falls_back() -> None:
    probs = om.shin_implied((2.0, 4.0, 4.0))
    assert sum(probs) == pytest.approx(1.0)


def test_consensus_odds_averages_books() -> None:
    odds = om.consensus_odds(
        [
            {"odds_api:pinnacle": 1.85, "odds_api:bet365": 1.95},
        ]
    )
    assert odds == (1.90,)

    none = om.consensus_odds([{}, {"odds_api:x": 2.0}])
    assert none is None


def test_expected_value() -> None:
    assert om.expected_value(0.5, 2.0) == pytest.approx(0.0)
    assert om.expected_value(0.4, 2.5) == pytest.approx(0.0)
    assert om.expected_value(0.3, 2.5) == pytest.approx(-0.25)
