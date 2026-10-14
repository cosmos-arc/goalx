"""错价桶判别层（model-iterate 票 03）：纯函数与引擎接线的行为断言。"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from goalx_backend.modelling import residual_layer as rl
from goalx_backend.modelling.residual_layer import ResidualState, feature_vector


def _triplet(h: float, d: float, a: float) -> dict[str, float]:
    return {"h": h, "d": d, "a": a}


def test_feature_vector_missing_to_zero():
    vec = feature_vector((0.25, None, 5.0, None, -0.1, 0.2, 0.95, None))
    assert vec.shape == (8,)
    assert vec[1] == 0.0
    assert vec[0] == 0.25


def test_residual_model_zero_weights_is_identity():
    """w=0 时判别层恒等（softmax(log p_fused + 0) = p_fused）。"""
    model = rl.ResidualModel(
        weights=np.zeros((3, 9)),
        mean=np.zeros(8),
        std=np.ones(8),
    )
    fused = _triplet(0.5, 0.3, 0.2)
    out = model.apply(fused, feature_vector((0.1, 0.2, 3, 4, 0.0, 0.0, 0.9, 0.9)))
    assert out == pytest.approx(fused)


def test_fit_softmax_learns_real_signal_not_noise():
    """特征真携带信号（x>0 → H）→ 出窗概率朝 H 移；w 非零。"""
    rng_free = [
        (
            np.array([1.0, 0.0, 5.0, 2.0, 0.0, 0.0, 0.9, 0.9]),
            _triplet(0.4, 0.3, 0.3),
            "H",
        )
    ] * 30 + [
        (
            np.array([-1.0, 0.0, 5.0, 2.0, 0.0, 0.0, 0.9, 0.9]),
            _triplet(0.4, 0.3, 0.3),
            "A",
        )
    ] * 30
    model = rl._fit_softmax(rng_free, l2=0.01)
    fused = _triplet(0.4, 0.3, 0.3)
    high = model.apply(fused, feature_vector((1.0, 0.0, 5.0, 2.0, 0.0, 0.0, 0.9, 0.9)))
    low = model.apply(fused, feature_vector((-1.0, 0.0, 5.0, 2.0, 0.0, 0.0, 0.9, 0.9)))
    assert high["h"] > fused["h"]  # 特征正 → H 概率上调
    assert low["h"] < fused["h"]


def test_residual_state_freeze_and_cold_start():
    """冷启动 None；热身冻结（record 不回灌；二次 fit 不改已算输出）。"""
    state = ResidualState(mode="on", min_samples=10, window=0)
    assert state.fit_for_week() is None
    for i in range(20):
        state.record(
            feature_vector((i % 3 - 1, 0, 5, 2, 0, 0, 0.9, 0.9)),
            _triplet(0.4, 0.3, 0.3),
            "H" if i % 2 else "A",
        )
    model = state.fit_for_week()
    assert model is not None
    vec = feature_vector((1.0, 0, 5, 2, 0, 0, 0.9, 0.9))
    first = model.apply(_triplet(0.4, 0.3, 0.3), vec)
    state.record(feature_vector((9.0,) * 8), _triplet(0.1, 0.1, 0.8), "A")
    assert state.fit_for_week() is not None
    assert model.apply(_triplet(0.4, 0.3, 0.3), vec) == first


def _holdout_gain(
    fitted: rl.ResidualModel,
    holdout: list[tuple[np.ndarray, dict[str, float], str]],
) -> float:
    """留出集平均 log(p_adj[pick]) − log(p_fused[pick])（>0=判别有增益）。"""
    gains = [
        np.log(fitted.apply(fused, feats)[ftr.lower()] + 1e-15)
        - np.log(fused[ftr.lower()] + 1e-15)
        for feats, fused, ftr in holdout
    ]
    return float(np.mean(gains))


def test_placebo_breaks_feature_outcome_link():
    """placebo：特征与结局错位（同边际、无关联）→ 留出增益≈0；
    真关联 → 增益>0（判读口径同对照报告）。"""
    import hashlib

    def _x(i: int) -> np.ndarray:
        h = hashlib.sha256(i.to_bytes(2, "big")).digest()
        return np.array([h[0] / 255.0 - 0.5] + [0.0] * 7)

    # 结局由特征排序决定（强关联），边际对齐 fused 三元组（40/30/30）
    # ——placebo 无类别先验增益可学，只剩特征关联断没断
    order = sorted(range(600), key=lambda i: _x(i)[0])
    y_of = {
        i: ("H" if r < 240 else "D" if r < 420 else "A") for r, i in enumerate(order)
    }

    entries = [(_x(i), _triplet(0.4, 0.3, 0.3), y_of[i]) for i in range(600)]
    train, holdout = entries[:400], entries[400:]
    l2 = 0.5  # 引擎缺省岭罚
    strong = rl._fit_softmax(train, l2=l2)
    assert _holdout_gain(strong, holdout) > 0.05  # 真信号：留出正增益
    shifted = [
        (
            train[i][0],
            train[(i + 1) % len(train)][1],
            train[(i + 1) % len(train)][2],
        )
        for i in range(len(train))
    ]
    placebo = rl._fit_softmax(shifted, l2=l2)
    assert abs(_holdout_gain(placebo, holdout)) < 0.02  # 无关联：≈0


# ---- 引擎接线 ----


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
                "ah_line_drift": (i % 3 - 1) * 0.25,
                "version": "gold-test",
            }
        )
    return rows


def _warm_start(rows: list[dict[str, Any]], warmup: int = 110) -> str:
    import datetime as dt

    kickoff = rows[warmup]["kickoff"]
    assert isinstance(kickoff, dt.datetime)
    return kickoff.date().isoformat()


def test_engine_residual_off_is_default(gold_env):
    from goalx_backend.evaluation import backtest as bt

    rows = _gold_rows(1, 160)
    with gold_env(rows) as (face, duck_con, store):
        result = bt.run_backtest(
            face,
            duck_con,
            store,
            bt.BacktestParams(
                competitions=("E0",), start=_warm_start(rows), min_train_matches=50
            ),
            label="r-off",
        )
        assert result.predictions > 0
        assert "residual_cold_weeks" not in result.skipped


def test_engine_residual_warms_and_adjusts(gold_env):
    from goalx_backend.evaluation import backtest as bt

    rows = [r for seed in (1, 2) for r in _gold_rows(seed, 160)]
    with gold_env(rows) as (face, duck_con, store):
        start = _warm_start(rows)
        common = {
            "competitions": ("E0",),
            "start": start,
            "min_train_matches": 50,
            "fusion": "global",
            "fusion_min_samples": 30,
        }
        base = bt.run_backtest(
            face, duck_con, store, bt.BacktestParams(**common), label="r-base"
        )
        resid = bt.run_backtest(
            face,
            duck_con,
            store,
            bt.BacktestParams(**common, residual="on", residual_min_samples=40),
            label="r-resid",
        )
        assert base.predictions == resid.predictions
        assert resid.skipped.get("residual_cold_weeks", 0) > 0
        diff = face.execute(
            """
            SELECT COUNT(*) FROM backtest_predictions p
            JOIN backtest_predictions q
              ON p.match_key = q.match_key AND p.run_id = ? AND q.run_id = ?
            WHERE p.had_probs <> q.had_probs
            """,
            (base.run_id, resid.run_id),
        ).fetchone()[0]
        assert diff > 0  # 热身后判别层生效
        params = json.loads(
            face.execute(
                "SELECT params FROM backtest_runs WHERE id=?", (resid.run_id,)
            ).fetchone()["params"]
        )
        assert params["residual"] == "on"


def test_engine_residual_invalid_fails_loud(gold_env):
    from goalx_backend.evaluation import backtest as bt

    with gold_env(_gold_rows(1, 40)) as (face, duck_con, store):
        with pytest.raises(ValueError, match="未知判别层"):
            bt.run_backtest(
                face,
                duck_con,
                store,
                bt.BacktestParams(  # type: ignore[arg-type]
                    competitions=("E0",), residual="bogus"
                ),
                label="bad",
            )
        with pytest.raises(ValueError, match="residual_window"):
            bt.run_backtest(
                face,
                duck_con,
                store,
                bt.BacktestParams(
                    competitions=("E0",), residual="on", residual_window=1
                ),
                label="bad2",
            )
