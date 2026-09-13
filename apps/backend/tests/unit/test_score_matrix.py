"""比分矩阵与全玩法推导视图测试（票 24 验收：和为 1、手算样例、边界归档）。"""

from __future__ import annotations

import math

import pytest
from penaltyblog.models import DixonColesGoalModel

from goalx_backend.markets import CRS_EXACT_SCORES
from goalx_backend.score_matrix import (
    HALF_SPLIT,
    ScoreMatrix,
    combination_probability,
    matrix_from_lambdas,
)


def sums_to_one(probs: dict[str, float]) -> float:
    return sum(probs.values())


def test_matrix_sums_to_one_and_poisson_marginals() -> None:
    matrix = ScoreMatrix.from_lambdas(1.4, 1.1)
    total = sum(sum(row) for row in matrix.grid)
    assert total == pytest.approx(1.0, abs=1e-12)
    # 无 rho 时格子应精确等于独立 Poisson 乘积（截断归一化后近似原值）
    raw = (math.exp(-1.4) * 1.4**2 / math.factorial(2)) * (
        math.exp(-1.1) * 1.1**1 / math.factorial(1)
    )
    assert matrix.cell(2, 1) == pytest.approx(raw, rel=1e-3)


def test_hand_computed_independent_poisson() -> None:
    # λh=λa=0.5：P(0:0)=e^-1，P(主胜)=Σ P(h>a)
    matrix = ScoreMatrix.from_lambdas(0.5, 0.5)
    assert matrix.cell(0, 0) == pytest.approx(math.exp(-1.0), rel=1e-6)
    p0_h, p0_a = math.exp(-0.5), math.exp(-0.5)
    expected_home = sum(
        (math.exp(-0.5) * 0.5**h / math.factorial(h))
        * (math.exp(-0.5) * 0.5**a / math.factorial(a))
        for h in range(10)
        for a in range(10)
        if h > a
    )
    assert p0_h + p0_a > 0  # 结构占位断言
    had = matrix.had()
    assert had["h"] == pytest.approx(expected_home, rel=1e-6)
    assert had["h"] == pytest.approx(had["a"], rel=1e-9)


def test_dc_rho_adjusts_low_scores_only() -> None:
    plain = matrix_from_lambdas(1.3, 1.0)
    adjusted = matrix_from_lambdas(1.3, 1.0, rho=0.06)
    # rho>0 抬高 0-0/1-1（tau=1-rho<1 降低 1-1）——按公式逐格验证相对变化
    plain_00, adj_00 = plain[0][0], adjusted[0][0]
    plain_01, adj_01 = plain[0][1], adjusted[0][1]
    # tau(0,1)=1+λh·ρ 抬高、tau(1,1)=1-ρ 压低（归一化前）；断言方向性
    assert adj_01 / plain_01 > adj_00 / plain_00
    # 高比分格不受 tau 影响（仅整体归一化差异）
    assert adjusted[3][3] == pytest.approx(plain[3][3], rel=1e-3)
    assert sum(sum(row) for row in adjusted) == pytest.approx(1.0, abs=1e-12)


def test_matches_penaltyblog_predict() -> None:
    # 纯函数矩阵构建与 penaltyblog 拟合输出逐位一致（票 26 工件复载前提）
    import numpy as np

    rng = np.random.default_rng(11)
    teams = ["A", "B", "C", "D"]
    strength = {"A": 0.45, "B": 0.10, "C": -0.15, "D": -0.40}
    goals_home: list[int] = []
    goals_away: list[int] = []
    teams_home: list[str] = []
    teams_away: list[str] = []
    for i in range(160):
        home = teams[i % 4]
        away = teams[(i + 1 + i // 4) % 4]
        if away == home:
            away = teams[(teams.index(away) + 1) % 4]
        lam_h = np.exp(0.25 + strength[home] - strength[away])
        lam_a = np.exp(strength[away] - strength[home])
        goals_home.append(int(rng.poisson(lam_h)))
        goals_away.append(int(rng.poisson(lam_a)))
        teams_home.append(home)
        teams_away.append(away)
    model = DixonColesGoalModel(goals_home, goals_away, teams_home, teams_away)
    model.fit()
    params = model.params
    lam_h = math.exp(
        params["home_advantage"] + params["attack_A"] + params["defence_B"]
    )
    lam_a = math.exp(params["attack_B"] + params["defence_A"])
    grid = matrix_from_lambdas(lam_h, lam_a, rho=params["rho"])
    reference = model.predict("A", "B", max_goals=10).grid
    for h in range(10):
        for a in range(10):
            assert grid[h][a] == pytest.approx(reference[h][a], abs=1e-15)


def test_matrix_rejects_runaway_rho() -> None:
    # ρ 大到 tau(0,0)<0 时应显式拒绝而不是产生负概率
    with pytest.raises(ValueError, match="负概率"):
        matrix_from_lambdas(1.8, 1.6, rho=0.4)


def test_had_hhad_ttg_views_sum_to_one() -> None:
    matrix = ScoreMatrix.from_lambdas(1.6, 0.9, rho=0.05)
    assert sums_to_one(matrix.had()) == pytest.approx(1.0, abs=1e-12)
    assert sums_to_one(matrix.hhad(-1)) == pytest.approx(1.0, abs=1e-12)
    assert sums_to_one(matrix.hhad(1)) == pytest.approx(1.0, abs=1e-12)
    assert sums_to_one(matrix.ttg()) == pytest.approx(1.0, abs=1e-12)


def test_hhad_goal_line_hand_example() -> None:
    # λ 均匀小值矩阵中手算让 1 球：hhad(h) 应≈P(净胜≥2)
    matrix = ScoreMatrix.from_lambdas(1.5, 0.7)
    had = matrix.had()
    minus_one = matrix.hhad(-1)
    assert minus_one["h"] < had["h"]
    assert minus_one["a"] > had["a"]
    # 净胜恰好 1 球的概率从 h 移到 d
    net_one = sum(matrix.cell(h, h - 1) for h in range(1, 10))
    assert had["h"] - minus_one["h"] == pytest.approx(net_one, abs=1e-12)


def test_crs_view_grid_and_other_buckets() -> None:
    matrix = ScoreMatrix.from_lambdas(1.9, 1.8, rho=0.03)
    crs = matrix.crs()
    assert len(crs) == len(CRS_EXACT_SCORES) + 3
    assert sums_to_one(crs) == pytest.approx(1.0, abs=1e-12)
    # 边界比分：5:2 在官方网格内；4:3 / 3:4 等落入「其他」档
    assert (5, 2) in CRS_EXACT_SCORES
    assert (4, 3) not in CRS_EXACT_SCORES
    assert crs["5:2"] == pytest.approx(matrix.cell(5, 2), abs=1e-15)
    assert crs["h_other"] > 0
    assert crs["a_other"] > 0
    # 「其他」档 = 对应三向总和 − 网格内精确格
    had = matrix.had()
    grid_home = sum(crs[f"{h}:{a}"] for h, a in CRS_EXACT_SCORES if h > a)
    assert crs["h_other"] == pytest.approx(had["h"] - grid_home, abs=1e-12)


def test_ttg_seven_plus_bucket_merges_tail() -> None:
    matrix = ScoreMatrix.from_lambdas(1.7, 1.5)
    ttg = matrix.ttg()
    assert list(ttg) == [str(n) for n in range(8)]
    assert sums_to_one(ttg) == pytest.approx(1.0, abs=1e-12)
    tail = sum(matrix.cell(h, a) for h in range(10) for a in range(10) if h + a >= 7)
    assert ttg["7"] == pytest.approx(tail, abs=1e-12)
    # 中档手算：P(总进球=2)=Σ P(h+a=2)
    two = sum(matrix.cell(h, 2 - h) for h in range(3))
    assert ttg["2"] == pytest.approx(two, abs=1e-15)


def test_hafu_view_sums_to_one_and_marginals_consistent() -> None:
    matrix = ScoreMatrix.from_lambdas(1.5, 1.2, rho=0.05)
    hafu = matrix.hafu()
    assert len(hafu) == 9
    assert sums_to_one(hafu) == pytest.approx(1.0, abs=1e-12)
    # 全场边际应与 had 视图一致到截断/rho 近似误差（松容差）
    had = matrix.had()
    ft_h = sum(v for code, v in hafu.items() if code[1] == "h")
    ft_d = sum(v for code, v in hafu.items() if code[1] == "d")
    assert ft_h == pytest.approx(had["h"], abs=0.02)
    assert ft_d == pytest.approx(had["d"], abs=0.02)
    # 半场拆分比例可参数化且单调：拆分到上半场的进球越多，HT 主胜概率越高
    early = matrix.hafu(half_split=0.30)
    late = matrix.hafu(half_split=0.60)
    assert late["hh"] > early["hh"]
    assert HALF_SPLIT == 0.45


def test_hafu_hand_example_symmetric() -> None:
    # 等强度对阵：主客对称（hh↔aa、hd↔ad、dh↔da），dd 是最大格之一
    matrix = ScoreMatrix.from_lambdas(1.0, 1.0)
    hafu = matrix.hafu()
    assert hafu["hh"] == pytest.approx(hafu["aa"], rel=1e-9)
    assert hafu["hd"] == pytest.approx(hafu["ad"], rel=1e-9)
    assert hafu["dh"] == pytest.approx(hafu["da"], rel=1e-9)
    assert hafu["dd"] > hafu["ha"]


def test_pool_margins_reuse_views() -> None:
    matrix = ScoreMatrix.from_lambdas(1.4, 1.1)
    wdl = matrix.pool_wdl()
    assert set(wdl) == {"3", "1", "0"}
    assert sums_to_one(wdl) == pytest.approx(1.0, abs=1e-12)
    assert matrix.pool_margin("goals4") == matrix.crs()
    assert matrix.pool_margin("htft6") == matrix.hafu()
    assert matrix.pool_margin("ttt14") == wdl
    with pytest.raises(ValueError, match="未知"):
        matrix.pool_margin("nope")


def test_combination_probability_multiplies_independently() -> None:
    marginals = {
        "m1": {"3": 0.5, "1": 0.3, "0": 0.2},
        "m2": {"3": 0.4, "1": 0.3, "0": 0.3},
        "m3": {"3": 0.6, "1": 0.25, "0": 0.15},
    }
    picks = {"m1": "3", "m2": "1", "m3": "0"}
    assert combination_probability(picks, marginals) == pytest.approx(0.5 * 0.3 * 0.15)
    # 未知选项 → 组合不可能
    picks_bad = {"m1": "9", "m2": "1", "m3": "0"}
    assert combination_probability(picks_bad, marginals) == 0.0


def test_combination_probability_from_matrices() -> None:
    # 端到端：两场矩阵的任9 连乘 == 两场 had 的乘积
    m1 = ScoreMatrix.from_lambdas(1.6, 0.8)
    m2 = ScoreMatrix.from_lambdas(0.9, 1.3)
    marginals = {"m1": m1.pool_wdl(), "m2": m2.pool_wdl()}
    picks = {"m1": "3", "m2": "0"}
    assert combination_probability(picks, marginals) == pytest.approx(
        m1.had()["h"] * m2.had()["a"]
    )


def test_matrix_rejects_nonpositive_lambdas() -> None:
    with pytest.raises(ValueError, match="λ"):
        matrix_from_lambdas(0.0, 1.0)
