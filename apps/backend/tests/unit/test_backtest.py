"""回测引擎测试（票 28 验收：防前视断言、结算路径统一、run 隔离幂等）。"""

from __future__ import annotations

import json

import numpy as np
import pytest

from goalx_backend import backtest as bt
from goalx_backend.dc_model import TrainingRow
from goalx_backend.store import results as rs_store

STRENGTH = {"A": 0.5, "B": 0.2, "C": -0.1, "D": -0.3, "E": 0.0, "F": -0.4}


def hist_row(day: str, home: str, away: str, gh: int, ga: int, season: str) -> dict:
    ftr = "H" if gh > ga else ("A" if gh < ga else "D")
    return {
        "competition": "E0",
        "season": season,
        "match_date": day,
        "home_team": home,
        "away_team": away,
        "fthg": gh,
        "ftag": ga,
        "ftr": ftr,
        # 均衡收盘价（带少量 overround，与实力无关的固定盘便于断言）
        "psc_home": 2.3,
        "psc_draw": 3.3,
        "psc_away": 3.1,
        "avgc_home": None,
        "avgc_draw": None,
        "avgc_away": None,
    }


def seed_synthetic_league(db, *, rounds: int = 14, season: str = "2324") -> None:
    rng = np.random.default_rng(21)
    teams = sorted(STRENGTH)
    for round_no in range(rounds):
        for i in range(3):
            home = teams[i]
            away = teams[5 - i]
            lam_h = float(np.exp(0.3 + STRENGTH[home] - STRENGTH[away]))
            lam_a = float(np.exp(STRENGTH[away] - STRENGTH[home]))
            rs_store.upsert_hist_matches(
                db,
                [
                    hist_row(
                        _date(round_no),
                        home,
                        away,
                        int(rng.poisson(lam_h)),
                        int(rng.poisson(lam_a)),
                        season,
                    )
                ],
            )


def _date(round_no: int) -> str:
    from datetime import date, timedelta

    return (date(2024, 9, 2) + timedelta(days=7 * round_no)).isoformat()


def test_match_week_grouping() -> None:
    rows = [
        {"match_date": "2024-09-14", "id": 1},
        {"match_date": "2024-09-15", "id": 2},
        {"match_date": "2024-09-21", "id": 3},
    ]

    class Row(dict):
        def __getitem__(self, key):
            return dict.__getitem__(self, key)

    weeks = bt.group_by_week([Row(r) for r in rows])
    assert len(weeks) == 2
    assert len(weeks[0][1]) == 2


def test_assert_no_lookahead_trips_on_leak() -> None:
    train = [TrainingRow("2024-09-15", "A", "B", 1, 0)]
    with pytest.raises(AssertionError, match="look-ahead"):
        bt.assert_no_lookahead(train, ["2024-09-14"])
    # 严格早于 → 通过
    bt.assert_no_lookahead(train, ["2024-09-16"])


def test_fair_probs_psc_preferred_avgc_fallback() -> None:
    class Row(dict):
        pass

    psc_row = Row(
        psc_home=2.2,
        psc_draw=3.4,
        psc_away=3.2,
        avgc_home=2.1,
        avgc_draw=3.3,
        avgc_away=3.1,
    )
    probs, source = bt.fair_probs_from_close(psc_row)
    assert source == "psc"
    assert sum(probs.values()) == pytest.approx(1.0)
    avgc_row = Row(
        psc_home=None,
        psc_draw=None,
        psc_away=None,
        avgc_home=2.1,
        avgc_draw=3.3,
        avgc_away=3.1,
    )
    probs, source = bt.fair_probs_from_close(avgc_row)
    assert source == "avgc"
    empty = Row(
        psc_home=None,
        psc_draw=None,
        psc_away=None,
        avgc_home=None,
        avgc_draw=None,
        avgc_away=None,
    )
    assert bt.fair_probs_from_close(empty) is None


def test_simulated_jc_odds_haircut() -> None:
    fair = {"h": 0.5, "d": 0.3, "a": 0.2}
    odds = bt.simulated_jc_odds(fair, haircut=0.10)
    assert odds["h"] == pytest.approx(2.0 * 0.9)
    # 概率域隐含 = fair / (1 - haircut)
    assert 1.0 / odds["h"] == pytest.approx(0.5 / 0.9)


def test_market_implied_matrix_recovers_had() -> None:
    matrix = bt.market_implied_matrix({"h": 0.45, "d": 0.28, "a": 0.27})
    assert matrix is not None
    had = matrix.had()
    assert had["h"] == pytest.approx(0.45, abs=0.01)
    assert had["a"] == pytest.approx(0.27, abs=0.01)


def test_pick_hhad_line_balances_sides() -> None:
    matrix = bt.market_implied_matrix({"h": 0.62, "d": 0.22, "a": 0.16})
    assert matrix is not None
    line = bt.pick_hhad_line(matrix)
    probs = matrix.hhad(float(line))
    assert abs(probs["h"] - probs["a"]) < 0.1
    assert line <= 0  # 主热门 → 需要让球


def test_kelly_stake_cap_and_threshold() -> None:
    params = bt.BacktestParams()
    # 大 EV：1/4 Kelly × 5000 > 50 → 封顶
    stake, kelly = bt.kelly_stake(0.8, 2.0, params)
    assert stake == 50.0
    assert kelly == pytest.approx(0.6)
    # 小 EV：EV>0 但按比例下注
    stake, _ = bt.kelly_stake(0.52, 2.0, params)
    assert 0 < stake <= 50.0
    # EV=0 → 0
    assert bt.kelly_stake(0.5, 2.0, params) == (0.0, 0.0)


def test_candidates_had_only_respect_price_bounds() -> None:
    # v1 had-only：极端热门市场（尾部隐含概率 <2%）不产生候选（竞彩实际不报价）
    params = bt.BacktestParams(max_sim_odds=50.0, haircut=0.11)
    from goalx_backend.score_matrix import ScoreMatrix

    matrix = ScoreMatrix.from_lambdas(2.4, 1.0)
    extreme_jc = bt.simulated_jc_odds({"h": 0.91, "d": 0.05, "a": 0.04}, 0.11)
    candidates = bt._candidates_for_match(matrix, None, extreme_jc, params, 1)
    # h(0.98)/a(22.2) 在界内，d(17.8) 在界内 → 3 个 had 候选
    assert {cand.selection_code for cand in candidates} == {"h", "d", "a"}
    assert all(cand.odds <= 50.0 for cand in candidates)
    # 尾部隐含 1.4% 的选项被下限排除
    tail_jc = bt.simulated_jc_odds({"h": 0.955, "d": 0.031, "a": 0.014}, 0.11)
    tail_candidates = bt._candidates_for_match(matrix, None, tail_jc, params, 1)
    assert all(cand.selection_code != "a" for cand in tail_candidates)


def test_run_backtest_end_to_end(db) -> None:
    seed_synthetic_league(db)
    params = bt.BacktestParams(
        competitions=("E0",),
        seasons=("2324",),
        min_train_matches=9,
        ev_threshold=0.0,  # 便于产生注单的测试设置
        ref_bankroll=5000.0,
    )
    result = bt.run_backtest(db, params, label="smoke")
    assert result.predictions >= 1
    assert result.bets >= 0
    summary = bt.run_summary(db, result.run_id)
    assert summary is not None
    assert summary["predictions"] == result.predictions
    # 注单走 Settlement 代码路径：won/lost + 每注有 EV/Kelly/盈亏
    rows = db.execute(
        "SELECT * FROM backtest_bets WHERE run_id = ?", (result.run_id,)
    ).fetchall()
    for row in rows:
        assert row["status"] in ("won", "lost", "void")
        assert row["ev"] is not None
        legs = json.loads(row["legs"])
        assert legs
        if row["kind"] == "parlay2":
            assert len(legs) == 2
            assert legs[0]["hist_match_id"] != legs[1]["hist_match_id"]
    # 预测行含 fair 基准与模型指纹
    pred = db.execute(
        "SELECT * FROM backtest_predictions WHERE run_id = ? LIMIT 1",
        (result.run_id,),
    ).fetchone()
    assert pred is not None
    assert pred["fair_source"] == "psc"
    fair = json.loads(pred["fair_probs"])
    assert abs(sum(fair.values()) - 1.0) < 1e-6


def test_run_backtest_run_isolation(db) -> None:
    seed_synthetic_league(db)
    params = bt.BacktestParams(
        competitions=("E0",),
        seasons=("2324",),
        min_train_matches=9,
        ev_threshold=0.0,
    )
    first = bt.run_backtest(db, params, label="run-1")
    second = bt.run_backtest(db, params, label="run-2")
    assert first.run_id != second.run_id
    counts = db.execute(
        "SELECT run_id, COUNT(*) AS c FROM backtest_bets GROUP BY run_id"
    ).fetchall()
    by_run = {int(r["run_id"]): int(r["c"]) for r in counts}
    assert by_run[first.run_id] == by_run.get(second.run_id, 0)
    statuses = db.execute("SELECT status FROM backtest_runs").fetchall()
    assert {str(r["status"]) for r in statuses} == {"done"}


def test_run_backtest_marks_failed_run(db) -> None:
    seed_synthetic_league(db)
    params = bt.BacktestParams(competitions=("E0",), seasons=("2324",))
    # min_train 巨大不会失败，用坏数据触发：插入畸形行
    db.execute(
        "UPDATE hist_matches SET psc_home = 0.5 WHERE id"
        " = (SELECT MIN(id) FROM hist_matches)"
    )
    db.commit()
    # psc<=1 的行应被 fair_probs_from_close 判无效（跳过），不应崩溃
    result = bt.run_backtest(db, params, label="bad-odds")
    assert result.predictions >= 0


def test_backtest_lookahead_guard_via_weeks(db) -> None:
    # 端到端防前视：训练截止取周最早日前一天，训练集不可能含当周比赛
    seed_synthetic_league(db)
    params = bt.BacktestParams(
        competitions=("E0",),
        seasons=("2324",),
        min_train_matches=9,
    )
    result = bt.run_backtest(db, params, label="wf")
    preds = db.execute(
        """
        SELECT p.match_date, p.train_window_end FROM backtest_predictions p
        WHERE p.run_id = ?
        """,
        (result.run_id,),
    ).fetchall()
    for row in preds:
        assert str(row["train_window_end"]) < str(row["match_date"])
