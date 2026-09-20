"""xG 消费数学层测试（票 45）：浮点拟合强度恢复 + 收缩校准性质。"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from numpy import random as np_random

from goalx_backend.modelling.dc_model import DCArtifact
from goalx_backend.modelling.xg_dc import XGRow, fit_xg_dc, shrink_dc_params


def _base_artifact() -> DCArtifact:
    return DCArtifact(
        competition="TEST",
        train_window_start="2025-08-01",
        train_window_end="2026-05-01",
        as_of="2026-05-01",
        n_matches=100,
        half_life_days=365.0,
        teams={
            "A": {"attack": 0.30, "defence": -0.20},
            "B": {"attack": 0.00, "defence": 0.00},
        },
        home_advantage=0.25,
        rho=-0.05,
        data_fingerprint="f" * 64,
    )


def test_fit_xg_dc_recovers_strength_ordering_and_level() -> None:
    """合成 Poisson 语料：恢复攻防排序与量级（无 ρ，浮点响应）。"""
    rng = np_random.default_rng(20260920)
    truth_attack = {"A": 0.35, "B": 0.05, "C": -0.15, "D": -0.25}
    truth_defence = {"A": -0.30, "B": -0.05, "C": 0.15, "D": 0.20}
    gamma = 0.25
    rows: list[XGRow] = []
    day = date(2025, 8, 1)
    teams = list(truth_attack)
    pairs = [(h, a) for h in teams for a in teams if h != a]  # 全排列轮转（可分离）
    for i in range(len(pairs) * 28):
        home, away = pairs[i % len(pairs)]
        lam_h = math.exp(gamma + truth_attack[home] + truth_defence[away])
        lam_a = math.exp(truth_attack[away] + truth_defence[home])
        rows.append(
            XGRow(
                day.isoformat(),
                home,
                away,
                float(rng.poisson(lam_h)),
                float(rng.poisson(lam_a)),
            )
        )
        day += timedelta(days=1)
    artifact = fit_xg_dc(rows, competition="TEST", as_of=day)
    assert artifact.rho == 0.0
    assert set(artifact.teams) == set(truth_attack)
    # λ 量级恢复（A 主场 vs C：真实 λ≈exp(0.25+0.35-0.15)≈1.82）
    lam_h, lam_a = artifact.lambdas("A", "C")
    assert lam_h == pytest.approx(1.82, abs=0.35)
    assert lam_a == pytest.approx(math.exp(-0.15 - 0.20), abs=0.25)
    # 攻防排序恢复：A 攻最强、D 攻最弱
    attacks = {t: p["attack"] for t, p in artifact.teams.items()}
    assert attacks["A"] > attacks["B"] > attacks["C"] > attacks["D"]
    # 参数化约束闭合
    assert sum(attacks.values()) == pytest.approx(0.0, abs=1e-8)


def test_fit_xg_dc_raises_on_insufficient_rows() -> None:
    with pytest.raises(ValueError, match="样本不足"):
        fit_xg_dc([], competition="TEST")


def test_shrink_noop_when_rates_match_history() -> None:
    """当前速率=历史速率 → 参数逐位不动。"""
    base = _base_artifact()
    mean_attack = 0.15
    mean_defence = -0.10
    half_adv = base.home_advantage / 2
    hist_for_a = math.exp(base.teams["A"]["attack"] + mean_defence + half_adv)
    hist_agst_a = math.exp(mean_attack + base.teams["A"]["defence"] + half_adv)
    hist_for_b = math.exp(base.teams["B"]["attack"] + mean_defence + half_adv)
    hist_agst_b = math.exp(mean_attack + base.teams["B"]["defence"] + half_adv)
    shrunk = shrink_dc_params(
        base,
        {
            "A": (hist_for_a * 6, hist_agst_a * 6, 6),
            "B": (hist_for_b * 6, hist_agst_b * 6, 6),
        },
        k=6.0,
    )
    assert shrunk.teams == base.teams
    assert (shrunk.home_advantage, shrunk.rho) == (base.home_advantage, base.rho)


def test_shrink_boosts_overperformer_and_recenters() -> None:
    """超预期队伍攻升、防更佳（β 更负）；无数据队友被重归一反向微调。"""
    base = _base_artifact()
    # A 六场：进攻速率≈1.9× 历史（≈1.38/场）、失球≈0.08/场（远低历史≈1.08）
    shrunk = shrink_dc_params(base, {"A": (16.0, 0.5, 6)}, k=6.0)
    assert shrunk.teams["A"]["attack"] > base.teams["A"]["attack"]
    assert shrunk.teams["A"]["defence"] < base.teams["A"]["defence"]
    # 重归零均值：B 承担反向微调（攻降、防松）
    assert shrunk.teams["B"]["attack"] < base.teams["B"]["attack"]
    assert shrunk.teams["B"]["defence"] > base.teams["B"]["defence"]
    # 联赛水平/主场优势/ρ 不动
    assert shrunk.home_advantage == base.home_advantage
    assert shrunk.rho == base.rho


def test_shrink_weight_shrinks_with_prior_k() -> None:
    """k 越大（先验越重）同观测下调幅越小。"""
    base = _base_artifact()
    weak = shrink_dc_params(base, {"A": (16.0, 0.5, 6)}, k=6.0)
    strong = shrink_dc_params(base, {"A": (16.0, 0.5, 6)}, k=30.0)
    shift_weak = abs(weak.teams["A"]["attack"] - base.teams["A"]["attack"])
    shift_strong = abs(strong.teams["A"]["attack"] - base.teams["A"]["attack"])
    assert 0.0 < shift_strong < shift_weak
