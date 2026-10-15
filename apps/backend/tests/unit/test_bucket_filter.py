"""错价桶下注过滤（model-v2 票 04）：门控谓词/随机门/引擎接线的行为断言。"""

from __future__ import annotations

from typing import Any

import pytest

from goalx_backend.evaluation import backtest as bt
from goalx_backend.modelling.residual_layer import FeatureTuple


def _cand(
    market: str = "had",
    sel: str = "h",
    bucket: FeatureTuple | None = None,
) -> bt._Candidate:
    return bt._Candidate(
        match_ref=0,
        match_key="s1",
        market_code=market,
        selection_code=sel,
        odds=2.0,
        model_prob=0.55,
        bucket=bucket or FeatureTuple(),
    )


def test_bucket_gate_had_direction():
    """had 门控：H 要 late_move>0（市场向主队移）、A 要 <0、D 要死区。"""
    up = FeatureTuple(late_move_h=0.02)
    down = FeatureTuple(late_move_h=-0.02)
    flat = FeatureTuple(late_move_h=0.001)
    none = FeatureTuple(late_move_h=None)
    assert bt._bucket_gate(_cand("had", "h", up)) is True
    assert bt._bucket_gate(_cand("had", "h", down)) is False
    assert bt._bucket_gate(_cand("had", "a", down)) is True
    assert bt._bucket_gate(_cand("had", "d", flat)) is True
    assert bt._bucket_gate(_cand("had", "h", none)) is False


def test_bucket_gate_drift_consistency():
    """hhad/ttg 门控：线移与主队价移同号。"""
    agree = FeatureTuple(ah_line_drift=0.25, late_move_h=0.01)
    disagree = FeatureTuple(ah_line_drift=0.25, late_move_h=-0.01)
    assert bt._bucket_gate(_cand("hhad", "h", agree)) is True
    assert bt._bucket_gate(_cand("ttg", "0", disagree)) is False
    assert (
        bt._bucket_gate(_cand("hhad", "h", FeatureTuple(ah_line_drift=None))) is False
    )


def test_random_gate_deterministic_and_bounded():
    """随机门确定性 + 接受率有界（~50%）。"""
    cands = [_cand("had", s) for s in ("h", "d", "a")] + [
        _cand("ttg", str(i)) for i in range(8)
    ]
    first = [bt._random_gate(c) for c in cands]
    second = [bt._random_gate(c) for c in cands]
    assert first == second  # 确定性
    assert any(first)
    assert not all(first)


def test_bet_filter_params_validated(gold_env):
    """非法过滤值开跑前 fail-loud。"""
    rows = _gold_rows(1, 40)
    with gold_env(rows) as (face, duck_con, store):
        with pytest.raises(ValueError, match="未知下注过滤"):
            bt.run_backtest(
                face,
                duck_con,
                store,
                bt.BacktestParams(  # type: ignore[arg-type]
                    competitions=("E0",), bet_filter="bogus"
                ),
                label="bad",
            )


def _gold_rows(seed: int, count: int) -> list[dict[str, Any]]:
    import datetime as dt

    rows = []
    for i in range(count):
        day = 1 + (i // 5) * 7
        kickoff = dt.datetime(2023, 9, 1) + dt.timedelta(days=day - 1)
        rows.append(
            {
                "sid": f"m{seed}-{i}",
                "league": "英超",
                "kickoff": kickoff,
                "season": "2023-24",
                "home": f"T{seed}H{i % 8}",
                "away": f"T{seed}A{(i * 3 + 1) % 8}",
                "home_goals": (seed * 7 + i * 3) % 4,
                "away_goals": (seed * 11 + i * 5) % 3,
                "stage": "常规轮",
                "era": "trajectory",
                "admin_excluded": False,
                "close1x2_h": 2.0 + (i % 5) * 0.1,
                "close1x2_d": 3.4,
                "close1x2_a": 3.8,
                "version": "gold-test",
            }
        )
    return rows


def _warm_start(rows: list[dict[str, Any]], warmup: int = 110) -> str:
    import datetime as dt

    kickoff = rows[warmup]["kickoff"]
    assert isinstance(kickoff, dt.datetime)
    return kickoff.date().isoformat()


def test_engine_filter_off_is_default(gold_env):
    """缺省 off：无门控计数（行为不变保护）。"""
    rows = _gold_rows(1, 160)
    with gold_env(rows) as (face, duck_con, store):
        result = bt.run_backtest(
            face,
            duck_con,
            store,
            bt.BacktestParams(
                competitions=("E0",), start=_warm_start(rows), min_train_matches=50
            ),
            label="off",
        )
        assert result.predictions > 0
        assert "bet_filter_rejected" not in result.skipped


def test_engine_buckets_and_random_variants_run(gold_env):
    """buckets/random 两变体全链：门控计数产生、注量 ≤ off 基线。"""
    rows = [r for seed in (1, 2) for r in _gold_rows(seed, 160)]
    with gold_env(rows) as (face, duck_con, store):
        start = _warm_start(rows)
        common = {"competitions": ("E0",), "start": start, "min_train_matches": 50}
        base = bt.run_backtest(
            face, duck_con, store, bt.BacktestParams(**common), label="off"
        )  # type: ignore[arg-type]
        filtered = bt.run_backtest(
            face,
            duck_con,
            store,
            bt.BacktestParams(**common, bet_filter="buckets"),  # type: ignore[arg-type]
            label="buckets",
        )
        random = bt.run_backtest(
            face,
            duck_con,
            store,
            bt.BacktestParams(**common, bet_filter="random"),  # type: ignore[arg-type]
            label="random",
        )
        assert filtered.skipped.get("bet_filter_rejected", 0) > 0
        assert random.skipped.get("bet_filter_rejected", 0) > 0
        assert filtered.bets <= base.bets
        assert random.bets <= base.bets
