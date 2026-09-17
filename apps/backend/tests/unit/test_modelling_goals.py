"""进球类概率推导领域函数测试（票 wb-04：矩阵一致性 + EV 口径）。"""

from __future__ import annotations

import pytest

from goalx_backend.markets import CRS_EXACT_SCORES, CRS_SELECTIONS, TTG_SELECTIONS
from goalx_backend.modelling.goals import (
    goals_probabilities,
    goals_selection_grid,
    goals_selection_rows,
)
from goalx_backend.modelling.score_matrix import ScoreMatrix

MATRIX = ScoreMatrix.from_lambdas(1.4, 1.3)


def test_selection_grids_match_official_shape() -> None:
    assert goals_selection_grid("ttg") == TTG_SELECTIONS
    assert tuple(str(n) for n in range(8)) == TTG_SELECTIONS
    assert goals_selection_grid("crs") == CRS_SELECTIONS
    assert len(CRS_SELECTIONS) == 31  # 28 精确 + 三档其他
    assert CRS_SELECTIONS[-3:] == ("h_other", "d_other", "a_other")
    with pytest.raises(ValueError, match="非进球类玩法"):
        goals_selection_grid("had")


def test_goals_probabilities_dispatch_and_sums() -> None:
    ttg = goals_probabilities(MATRIX, "ttg")
    crs = goals_probabilities(MATRIX, "crs")
    assert set(ttg) == set(TTG_SELECTIONS)
    assert set(crs) == set(CRS_SELECTIONS)
    assert sum(ttg.values()) == pytest.approx(1.0)
    assert sum(crs.values()) == pytest.approx(1.0)


def test_ttg_consistent_with_matrix() -> None:
    """ttg 分布 = 矩阵反对角求和（ADR-0006 一致性，票面验收）。"""
    ttg = goals_probabilities(MATRIX, "ttg")
    for total in range(7):
        expected = sum(
            MATRIX.cell(h, a) for h in range(10) for a in range(10) if h + a == total
        )
        assert ttg[str(total)] == pytest.approx(expected)
    tail = sum(MATRIX.cell(h, a) for h in range(10) for a in range(10) if h + a >= 7)
    assert ttg["7"] == pytest.approx(tail)


def test_crs_consistent_with_matrix() -> None:
    """crs 精确格 = 矩阵格；其他三档 = 胜/平/负方向的非精确尾部之和。"""
    crs = goals_probabilities(MATRIX, "crs")
    exact = set(CRS_EXACT_SCORES)
    assert crs["1:1"] == pytest.approx(MATRIX.cell(1, 1))
    assert crs["2:0"] == pytest.approx(MATRIX.cell(2, 0))
    for sign, code in (("h", "h_other"), ("d", "d_other"), ("a", "a_other")):
        tail = sum(
            MATRIX.cell(h, a)
            for h in range(10)
            for a in range(10)
            if (h > a, h == a, h < a) == (sign == "h", sign == "d", sign == "a")
            and (h, a) not in exact
        )
        assert crs[code] == pytest.approx(tail)


def test_ttg_and_crs_from_same_matrix_are_jointly_consistent() -> None:
    """同一矩阵推得的 ttg 与 crs 联合一致：Σ P(crs 精确比分和=k) = P(ttg=k)。

    仅对 k≤5 断言：k≤5 的全部格都在 28 精确比分内（k=6 起部分格落入
    "其他"档，精确格之和自然小于 ttg 档——矩阵口径自洽的边界即此）。
    """
    ttg = goals_probabilities(MATRIX, "ttg")
    crs = goals_probabilities(MATRIX, "crs")
    for total in range(6):
        from_crs = sum(
            prob
            for code, prob in crs.items()
            if ":" in code
            and int(code.split(":")[0]) + int(code.split(":")[1]) == total
        )
        assert from_crs == pytest.approx(ttg[str(total)], abs=1e-9)
    # k=6：精确格之和 + 该方向其他档中 total=6 的份额 = ttg(6)（用矩阵直接验证）
    tail6 = sum(MATRIX.cell(h, a) for h in range(10) for a in range(10) if h + a == 6)
    exact6 = sum(
        prob
        for code, prob in crs.items()
        if ":" in code and int(code.split(":")[0]) + int(code.split(":")[1]) == 6
    )
    assert exact6 < tail6  # 6:0 与 0:6 落"其他"档
    assert ttg["6"] == pytest.approx(tail6)


def test_goals_selection_rows_ev_caliber() -> None:
    """EV = 模型概率 × 竞彩价 − 1（模型×竞彩价口径）；无报价选项 ev=None。"""
    odds = {"2": 4.5, "3": 3.4}
    rows = goals_selection_rows(MATRIX, "ttg", odds)
    assert [row["code"] for row in rows] == list(TTG_SELECTIONS)
    by_code = {row["code"]: row for row in rows}
    ttg = goals_probabilities(MATRIX, "ttg")
    assert by_code["2"]["odds"] == 4.5
    assert by_code["2"]["probability"] == pytest.approx(ttg["2"], abs=1e-6)
    assert by_code["2"]["ev"] == pytest.approx(ttg["2"] * 4.5 - 1, abs=1e-6)
    # 无竞彩价的选项：odds/ev 置空，概率仍在（诚实显示网格）
    assert by_code["0"]["odds"] is None
    assert by_code["0"]["ev"] is None
    assert by_code["0"]["probability"] == pytest.approx(ttg["0"], abs=1e-6)
    # 无效价（≤1，防御）不算 EV
    rows_bad = goals_selection_rows(MATRIX, "ttg", {"2": 1.0})
    assert rows_bad[2]["ev"] is None


def test_goals_probabilities_rejects_non_goals_market() -> None:
    with pytest.raises(ValueError, match="非进球类玩法"):
        goals_probabilities(MATRIX, "hafu")
