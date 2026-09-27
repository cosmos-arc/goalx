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


def test_consensus_low_confidence_threshold() -> None:
    """共识分母护栏（票 39）：books <4 打低置信；阈值是常量参数可调。"""
    assert om.LOW_CONFIDENCE_BOOK_THRESHOLD == 4  # 工程初值，待人追认
    assert om.consensus_low_confidence(0)
    assert om.consensus_low_confidence(2)
    assert om.consensus_low_confidence(3)
    assert not om.consensus_low_confidence(4)  # 边界：阈值本身不打标
    assert not om.consensus_low_confidence(5)
    # 常量参数：收紧到 5 时 4 也打标（追认后改常量即全局生效，无须改调用点）
    assert om.consensus_low_confidence(4, threshold=5)
    assert not om.consensus_low_confidence(5, threshold=5)


# --- 金标值断言（lean-audit 票 02）：值取自换装前二分实现真实输出，容差 1e-12 ---
_GOLDEN_VECTORS = [
    (2.1, 3.4, 3.6),
    (1.5, 4.0, 6.0),
    (1.85, 3.6, 4.2),
    (1.25, 6.0, 10.0),
    (1.9, 2.05),
    (2.98, 3.0, 2.98),
]


@pytest.mark.parametrize(
    ("odds", "expected"),
    zip(
        _GOLDEN_VECTORS,
        [
            (0.46017445786048766, 0.2779799640763887, 0.2618455780631236),
            (0.6410903825253892, 0.21870191995294214, 0.14020769752166876),
            (0.5220713748974793, 0.25838052194031347, 0.21954810316220721),
            (0.7820279594877506, 0.13887201836121796, 0.07910002215103142),
            (0.519379409618193, 0.4806205903818071),
            (0.3340787495030484, 0.33184250099390333, 0.3340787495030484),
        ],
        strict=True,
    ),
)
def test_golden_power_implied(
    odds: tuple[float, ...], expected: tuple[float, ...]
) -> None:
    assert om.power_implied(odds) == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize(
    ("odds", "expected"),
    zip(
        [*_GOLDEN_VECTORS, (3.0,)],
        [
            (0.45866571596133554, 0.2787376371327903, 0.26259664690587414),
            (0.6327606694282152, 0.22428186018664373, 0.14295747038514103),
            (0.5191426245012639, 0.25998009489801005, 0.22087728060072612),
            (0.7706163406205357, 0.1472189303844266, 0.08216472899503752),
            (0.5192554557124518, 0.48074454428754815),
            (0.33407821228114126, 0.3318435754377175, 0.33407821228114126),
            (1.0,),
        ],
        strict=True,
    ),
)
def test_golden_shin_implied(
    odds: tuple[float, ...], expected: tuple[float, ...]
) -> None:
    assert om.shin_implied(odds) == pytest.approx(expected, abs=1e-12)
