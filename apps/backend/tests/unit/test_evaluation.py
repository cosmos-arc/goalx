"""指标集测试（票 29 验收：与 penaltyblog 交叉验证、skill 含 p 值、分层汇总）。"""

from __future__ import annotations

import json

import numpy as np
import pytest
from penaltyblog.metrics import multiclass_brier_score, rps_average

from goalx_backend.evaluation import backtest as bt
from goalx_backend.evaluation import metrics as ev


def test_rps_matches_penaltyblog() -> None:
    rng = np.random.default_rng(3)
    probs = rng.dirichlet([2, 1, 1], size=40)
    outcomes = rng.integers(0, 3, size=40)
    ours = [ev.rps(list(p), int(o)) for p, o in zip(probs, outcomes, strict=True)]
    reference = rps_average(probs, outcomes)
    assert sum(ours) / len(ours) == pytest.approx(reference, abs=1e-12)
    # 手算样例：完美预测 RPS=0；反序预测最差
    assert ev.rps([1.0, 0.0, 0.0], 0) == pytest.approx(0.0)
    assert ev.rps([0.0, 0.0, 1.0], 0) == pytest.approx(1.0)


def test_brier_matches_penaltyblog() -> None:
    rng = np.random.default_rng(4)
    probs = rng.dirichlet([1, 1, 1], size=30)
    outcomes = rng.integers(0, 3, size=30)
    ours = sum(
        ev.multiclass_brier(list(p), int(o))
        for p, o in zip(probs, outcomes, strict=True)
    )
    reference = multiclass_brier_score(probs, outcomes) * len(outcomes)
    assert ours == pytest.approx(reference, abs=1e-9)


def test_log_loss_bounds() -> None:
    assert ev.log_loss([0.5, 0.3, 0.2], 0) == pytest.approx(-np.log(0.5))
    assert ev.log_loss([1e-20, 0.5, 0.5], 0) > 30  # 截断保护不炸(ln 1e-15≈34.5)


def test_ece_perfect_and_miscalibrated() -> None:
    # 完美校准（大量样本、概率=频率）→ ECE 接近 0
    vectors, outcomes = [], []
    rng = np.random.default_rng(6)
    for _ in range(3000):
        p = rng.dirichlet([3, 2, 2])
        idx = int(rng.choice(3, p=p))
        vectors.append(list(p))
        outcomes.append(idx)
    ece = ev.ece_per_class(vectors, outcomes, n_bins=10)
    assert all(e < 0.05 for e in ece)
    # 系统性高估第一类 → 第一类 ECE 大
    bad = [([0.9, 0.05, 0.05], 1)] * 100
    ece_bad = ev.ece_per_class([v for v, _ in bad], [o for _, o in bad], n_bins=10)
    assert ece_bad[0] > 0.5


def test_skill_score_direction() -> None:
    assert ev.skill_score(0.18, 0.20) == pytest.approx(0.1)
    assert ev.skill_score(0.22, 0.20) == pytest.approx(-0.1)
    assert ev.skill_score(0.2, 0.0) == 0.0


def test_diebold_mariano_significance() -> None:
    rng = np.random.default_rng(8)
    noise = rng.normal(0, 0.01, 500).tolist()
    dm, p = ev.diebold_mariano(noise)
    assert abs(dm) < 3  # 无差异 → 不显著
    assert p > 0.05
    biased = (rng.normal(-0.02, 0.01, 500)).tolist()
    dm, p = ev.diebold_mariano(biased)
    assert dm < -3  # 模型显著更优 → 拒绝
    assert p < 0.001
    assert ev.diebold_mariano([0.1, 0.2]) == (0.0, 1.0)  # 样本不足


def test_flat_stake_stats() -> None:
    stats = ev.flat_stake_stats([10.0, -10.0, 10.0, -10.0], [10.0] * 4)
    assert stats["n"] == 4
    assert stats["roi"] == pytest.approx(0.0)
    assert stats["t_stat"] == pytest.approx(0.0)
    rng = np.random.default_rng(2)
    profits = [5.0 + float(rng.normal(0, 1)) for _ in range(30)]
    winner = ev.flat_stake_stats(profits, [10.0] * 30)
    assert 0.3 < winner["roi"] < 0.7
    assert winner["t_stat"] > 10
    assert ev.flat_stake_stats([], [])["n"] == 0


def test_evaluate_predictions_structure() -> None:
    samples = [
        {
            "had_probs": {"h": 0.5, "d": 0.3, "a": 0.2},
            "fair_probs": {"h": 0.45, "d": 0.30, "a": 0.25},
            "ftr": "H",
        },
        {
            "had_probs": {"h": 0.2, "d": 0.3, "a": 0.5},
            "fair_probs": {"h": 0.25, "d": 0.30, "a": 0.45},
            "ftr": "A",
        },
        {
            "had_probs": {"h": 0.33, "d": 0.34, "a": 0.33},
            "fair_probs": {"h": 0.33, "d": 0.34, "a": 0.33},
            "ftr": "D",
        },
    ]
    metrics = ev.evaluate_predictions(samples)
    assert metrics["n"] == 3
    assert metrics["rps_model"] >= 0
    assert 0.0 <= metrics["dm_p"] <= 1.0
    assert "ece_d" in metrics
    assert ev.evaluate_predictions([]) == {"n": 0}


STRENGTH = {"A": 0.5, "B": 0.2, "C": -0.1, "D": -0.3, "E": 0.0, "F": -0.4}


def seed_synthetic_league(db, *, rounds: int = 14, season: str = "2324") -> None:
    """与 test_backtest 相同的合成联赛（importlib 模式下不跨文件导入）。"""
    from datetime import date, timedelta

    from goalx_backend.data import results as rs_store

    rng = np.random.default_rng(21)
    teams = sorted(STRENGTH)
    for round_no in range(rounds):
        for i in range(3):
            home, away = teams[i], teams[5 - i]
            lam_h = float(np.exp(0.3 + STRENGTH[home] - STRENGTH[away]))
            lam_a = float(np.exp(STRENGTH[away] - STRENGTH[home]))
            ftr = "H"
            gh, ga = int(rng.poisson(lam_h)), int(rng.poisson(lam_a))
            ftr = "H" if gh > ga else ("A" if gh < ga else "D")
            rs_store.upsert_hist_matches(
                db,
                [
                    {
                        "competition": "E0",
                        "season": season,
                        "match_date": (
                            date(2024, 9, 2) + timedelta(days=7 * round_no)
                        ).isoformat(),
                        "home_team": home,
                        "away_team": away,
                        "fthg": gh,
                        "ftag": ga,
                        "ftr": ftr,
                        "psc_home": 2.3,
                        "psc_draw": 3.3,
                        "psc_away": 3.1,
                        "avgc_home": None,
                        "avgc_draw": None,
                        "avgc_away": None,
                    }
                ],
            )


def test_compute_run_metrics_stratified(db) -> None:
    # 用回测引擎产出真实预测，再算分层指标（31 号看板数据源）
    seed_synthetic_league(db)
    params = bt.BacktestParams(
        competitions=("E0",),
        seasons=("2324",),
        min_train_matches=9,
        ev_threshold=0.0,
    )
    result = bt.run_backtest(db, params, label="metrics-smoke")
    written = ev.compute_run_metrics(db, result.run_id)
    assert written["written"] >= 2
    scopes = {
        str(r["scope"])
        for r in db.execute(
            "SELECT scope FROM backtest_metrics WHERE run_id = ?",
            (result.run_id,),
        ).fetchall()
    }
    assert "overall" in scopes
    assert "E0" in scopes
    assert "2324" in scopes
    overall = json.loads(
        db.execute(
            "SELECT metrics FROM backtest_metrics WHERE run_id = ?"
            " AND scope = 'overall'",
            (result.run_id,),
        ).fetchone()["metrics"]
    )
    assert overall["n"] >= 1
    # 幂等：重算不重复
    again = ev.compute_run_metrics(db, result.run_id)
    count = db.execute(
        "SELECT COUNT(*) AS c FROM backtest_metrics WHERE run_id = ?",
        (result.run_id,),
    ).fetchone()["c"]
    assert again["written"] == written["written"]
    assert count == written["written"]


# --- 票 34：串关玩法去重汇总、双 ROI 口径 ---


def _seed_run_with_bets(db, bets: list[tuple[str, float, float, list[str]]]) -> int:
    """(kind, stake, profit, leg markets) → 一个 run 的 backtest_bets。"""
    db.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at)"
        " VALUES ('t', '{}', 'done', '2026-09-13T00:00:00+00:00')"
    )
    run_id = int(db.execute("SELECT id FROM backtest_runs").fetchone()["id"])
    for i, (kind, stake, profit, markets) in enumerate(bets):
        legs = [{"market_code": m, "selection_code": "h", "odds": 2.0} for m in markets]
        db.execute(
            "INSERT INTO backtest_bets (run_id, kind, competition, placed_week,"
            " stake, legs, ev, status, payout, profit) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                kind,
                "E0",
                "2026-W01",
                stake,
                json.dumps(legs),
                0.01,
                "won",
                stake + profit,
                profit,
            ),
        )
        del i
    db.commit()
    return run_id


def test_parlay_same_market_not_double_counted(db) -> None:
    """同串关两腿同玩法只计一次；had-only 汇总 == overall（票 34 验收 4）。"""
    bets = [
        ("parlay2", 50.0, 40.0, ["had", "had"]),  # 两腿同玩法:曾重复累加
        ("parlay2", 50.0, -50.0, ["had", "hhad"]),  # 跨玩法串关:曾复制进两个 scope
        ("single", 50.0, 25.0, ["had"]),
    ]
    run_id = _seed_run_with_bets(db, bets)
    ev.compute_run_metrics(db, run_id)
    row = lambda scope: json.loads(  # noqa: E731
        db.execute(
            "SELECT metrics FROM backtest_metrics WHERE run_id=? AND scope=?",
            (run_id, scope),
        ).fetchone()["metrics"]
    )
    overall = row("bets:overall")
    had = row("bets:had")
    hhad = row("bets:hhad")
    assert overall["n"] == 3  # 每注一次
    assert had["n"] == 3  # 三注都含 had 腿,各计一次
    assert hhad["n"] == 1  # 只有第二注含 hhad 腿
    assert had["staked"] == overall["staked"]  # had-only 去重汇总 == overall
    assert had["profit"] == pytest.approx(overall["profit"])


def test_dual_roi_reporting(db) -> None:
    """等权(每注均值)与投入加权 ROI 分列,不等时不得混称（票 34）。"""
    stats = ev.flat_stake_stats([10.0, -10.0], [10.0, 100.0])
    assert stats["roi"] == pytest.approx((1.0 + -0.1) / 2)  # 等权 0.45
    assert stats["roi_stake_weighted"] == pytest.approx(0.0 / 110.0)  # 加权 0
    assert stats["roi"] != stats["roi_stake_weighted"]
    run_id = _seed_run_with_bets(
        db, [("single", 10.0, 10.0, ["had"]), ("single", 100.0, -10.0, ["had"])]
    )
    ev.compute_run_metrics(db, run_id)
    metrics = json.loads(
        db.execute(
            "SELECT metrics FROM backtest_metrics"
            " WHERE run_id=? AND scope='bets:overall'",
            (run_id,),
        ).fetchone()["metrics"]
    )
    assert metrics["roi"] == pytest.approx(0.45)
    assert metrics["roi_stake_weighted"] == pytest.approx(0.0)
