"""market-λ 联合反推 P1 测试（backtest-decade 票 16）。

联合 NLS 数学性质（OU 信息注入/1X2 拟合有界）+ 端到端报告（era 分层/
回落计数/reports 落盘）。真源网络永不进测试。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from goalx_backend.data import gold as gold_mod
from goalx_backend.evaluation import market_lambda as ml
from goalx_backend.modelling.score_matrix import ScoreMatrix

DAY = "2021-10-02"


def _row(sid: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sid": sid,
        "league": "英超",
        "kickoff": datetime.fromisoformat(f"{DAY}T20:00:00"),
        "season": "2021-22",
        "home": "A",
        "away": "B",
        "home_goals": 2,
        "away_goals": 1,
        "stage": "常规轮",
        "era": gold_mod.ERA_PSC_PROXY,
        "admin_excluded": False,
        "psc_home": 2.0,
        "psc_draw": 3.4,
        "psc_away": 3.8,
        "close1x2_h": None,
        "close1x2_d": None,
        "close1x2_a": None,
        "close1x2_cons_h": None,
        "close1x2_cons_d": None,
        "close1x2_cons_a": None,
        "ou_close_line_med": 2.5,
        "ou_close_over_water_med": 0.90,
        "ou_close_under_water_med": 1.00,
        "version": gold_mod.GOLD_VERSION,
    }
    return base | overrides


def _poisson_probs(lam_h: float, lam_a: float) -> dict[str, float]:
    matrix = ScoreMatrix.from_lambdas(lam_h, lam_a)
    return matrix.had()


def test_joint_lambdas_injects_ou_information() -> None:
    """联合反推把模型 P(over) 拉向市场 OU，且 1X2 拟合误差有界。"""
    # fair 1X2 取自低总量 λ；市场 OU 概率取自高总量（信息冲突场景）
    fair = _poisson_probs(1.0, 0.9)
    single = ScoreMatrix.from_lambdas(1.0, 0.9)
    high_total = ScoreMatrix.from_lambdas(1.9, 1.8)
    ttg = high_total.ttg()
    market_over = sum(p for b, p in ttg.items() if float(b) > 2.5)

    def over_prob(lam_h: float, lam_a: float) -> float:
        t = ScoreMatrix.from_lambdas(lam_h, lam_a).ttg()
        return sum(p for b, p in t.items() if float(b) > 2.5)

    joint = ml.joint_lambdas(fair, 2.5, market_over, x0=(1.0, 0.9))
    assert joint is not None
    single_over = over_prob(single.lam_home, single.lam_away)
    joint_over = over_prob(*joint)
    # 联合反推显著贴近市场 OU（single 偏离 ~0.36，joint 收敛到 0.05 内）
    assert abs(joint_over - market_over) < abs(single_over - market_over)
    assert abs(joint_over - market_over) < 0.05
    # 1X2 拟合有界（概率尺度均方 <0.02²·3）
    joint_had = ScoreMatrix.from_lambdas(*joint).had()
    fit = sum((joint_had[s] - fair[s]) ** 2 for s in ("h", "d", "a"))
    assert fit < 0.02


def test_joint_lambdas_recovers_consistent_lambda() -> None:
    """约束同源（fair 与市场 OU 出自同一 λ）→ 联合反推恢复该 λ。"""
    lam = (1.6, 1.3)
    fair = _poisson_probs(*lam)
    ttg = ScoreMatrix.from_lambdas(*lam).ttg()
    market_over = sum(p for b, p in ttg.items() if float(b) > 2.5)
    joint = ml.joint_lambdas(fair, 2.5, market_over, x0=(1.2, 1.0))
    assert joint is not None
    assert joint[0] == pytest.approx(lam[0], abs=0.05)
    assert joint[1] == pytest.approx(lam[1], abs=0.05)


def test_build_report_end_to_end(gold_env) -> None:
    with gold_env([_row("9401"), _row("9402", home_goals=1, away_goals=1)]) as (
        face,
        duck_con,
        store,
    ):
        payload = ml.build_market_lambda_report(
            store, duck_con, today=date(2026, 10, 10)
        )
        try:
            assert payload["n_sampled"] == 2
            s = payload["methods"]["single"]["psc_proxy"]
            j = payload["methods"]["joint"]["psc_proxy"]
            assert s["n"] == 2
            assert j["n"] == 2
            assert 0.0 <= s["ou_brier"] <= 1.0
            assert 0.0 <= j["had_fit_err"] <= 0.1
            # era 主层 ttg 隐含两法齐备；league 层不进 ttg 表
            assert "psc_proxy" in payload["ttg_implied_single"]
            assert "psc_proxy" in payload["ttg_implied_joint"]
            assert all(":" not in k for k in payload["ttg_implied_single"])
            assert (store.root / "reports" / "market-lambda-p1.json").exists()
            md = (store.root / "reports" / "market-lambda-p1.md").read_text(
                encoding="utf-8"
            )
            assert "market-λ 联合反推 P1" in md
        finally:
            duck_con.close()
            face.close()


def test_build_report_skips_without_ou(gold_env) -> None:
    """无 OU 半线（四分线）的行不入样，skip 如实计数。"""
    rows = [_row("9501", ou_close_line_med=2.75)]
    with gold_env(rows) as (face, duck_con, store):
        payload = ml.build_market_lambda_report(store, duck_con)
        try:
            assert payload["n_sampled"] == 0
            assert payload["skipped"]["ou_no_half_line"] == 1
        finally:
            duck_con.close()
            face.close()


def test_joint_failure_falls_back_to_single(monkeypatch, gold_env) -> None:
    """correctness F2 回归：joint 拟合失败回落 single 并如实计数（不静默混入）。"""
    rows = [_row("9601"), _row("9602", home_goals=1, away_goals=1)]
    with gold_env(rows) as (face, duck_con, store):
        monkeypatch.setattr(ml, "joint_lambdas", lambda *a, **k: None)
        payload = ml.build_market_lambda_report(store, duck_con)
        try:
            assert payload["n_sampled"] == 2
            assert payload["skipped"]["joint_fit_failed"] == 2
            s = payload["methods"]["single"]["psc_proxy"]
            j = payload["methods"]["joint"]["psc_proxy"]
            # 回落语义：joint 统计==single 统计（可判读、可归因）
            assert j["ou_brier"] == s["ou_brier"]
            assert j["had_fit_err"] == s["had_fit_err"]
        finally:
            duck_con.close()
            face.close()
