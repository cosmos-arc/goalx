"""GBM 模型线（model-v2 票 02）：拟合/预测/引擎接线的行为断言。"""

from __future__ import annotations

from typing import Any

import pytest

from goalx_backend.modelling.gbm_model import (
    GBM_FEATURES,
    feature_row,
    fit_gbm_model,
)


def _sample(elo: float, outcome: str) -> tuple[dict[str, float | None], str]:
    row = dict.fromkeys(GBM_FEATURES)
    row["elo_diff"] = elo
    row["fair_h"] = 0.4
    row["fair_d"] = 0.3
    row["fair_a"] = 0.3
    return row, outcome


def test_fit_and_predict_signal_recoverable():
    """可分特征学得出：elo_diff>100 → H、<−100 → A。"""
    samples = [*_deep_samples(60, 100.0, "H"), *_deep_samples(60, -100.0, "A")]
    artifact = fit_gbm_model(samples, train_window_end="2024-01-01")
    high = artifact.predict(_feature_row_with(150.0))
    low = artifact.predict(_feature_row_with(-150.0))
    assert high["h"] > 0.5
    assert low["a"] > 0.5


def _deep_samples(n: int, elo: float, outcome: str):
    return [(_feature_row_with(elo + (i % 7) - 3), outcome) for i in range(n)]


def _feature_row_with(elo: float) -> dict[str, float | None]:
    row = dict.fromkeys(GBM_FEATURES)
    row["elo_diff"] = elo
    row["fair_h"], row["fair_d"], row["fair_a"] = 0.4, 0.3, 0.3
    return row


def test_deterministic_replay():
    """同输入同输出（重放确定性纪律）。"""
    samples = [*_deep_samples(40, 50.0, "H"), *_deep_samples(40, -50.0, "A")]
    a = fit_gbm_model(samples, train_window_end="d1")
    b = fit_gbm_model(samples, train_window_end="d1")
    probe = _feature_row_with(30.0)
    assert a.predict(probe) == pytest.approx(b.predict(probe), rel=1e-9)


def test_min_samples_fails_loud():
    with pytest.raises(ValueError, match="不足"):
        fit_gbm_model([_sample(1.0, "H")], train_window_end="d")


def test_predict_missing_features_are_nan():
    """全缺特征不炸（lightgbm 原生 NaN）；输出合法分布。"""
    samples = [*_deep_samples(30, 80.0, "H"), *_deep_samples(30, -80.0, "A")]
    artifact = fit_gbm_model(samples, train_window_end="d")
    empty = dict.fromkeys(GBM_FEATURES)
    out = artifact.predict(empty)
    assert sum(out.values()) == pytest.approx(1.0)
    assert all(v >= 0 for v in out.values())


def test_feature_row_merges_three_sources():
    """特征行 = 滞后面 + 公平概率 + 错价桶三源合并。"""
    row = feature_row(
        {"elo_diff": 50.0},
        {"h": 0.5, "d": 0.3, "a": 0.2},
        {"ah_line_drift": 0.25},
    )
    assert row["elo_diff"] == 50.0
    assert row["fair_h"] == 0.5
    assert row["ah_line_drift"] == 0.25


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
                "elo_home_pre": 1500.0 + i,
                "elo_away_pre": 1450.0,
                "xg_home": 1.0 + (i % 3) * 0.5,
                "xg_away": 0.8,
                "fd_shots_home": 10 + i % 5,
                "fd_shots_away": 8,
                "version": "gold-test",
            }
        )
    return rows


def test_engine_gbm_variant_runs_had_only(gold_env):
    """gbm 变体全链：预测产生、hhad/ttg 跳过计数、run params 记档。"""
    import json

    from goalx_backend.evaluation import backtest as bt

    rows = [r for seed in (1, 2) for r in _gold_rows(seed, 160)]
    with gold_env(rows) as (face, duck_con, store):
        start = _warm_start(rows, warmup=80)  # 留 6+ 周给 gbm 池积累
        result = bt.run_backtest(
            face,
            duck_con,
            store,
            bt.BacktestParams(
                competitions=("E0",),
                start=start,
                min_train_matches=50,
                model="gbm",
            ),
            label="gbm-smoke",
        )
        assert result.predictions > 0
        assert result.skipped.get("gbm_no_matrix_skips", 0) > 0  # hhad/ttg 计数
        params = json.loads(
            face.execute(
                "SELECT params FROM backtest_runs WHERE id=?", (result.run_id,)
            ).fetchone()["params"]
        )
        assert params["model"] == "gbm"


def _warm_start(rows: list[dict[str, Any]], warmup: int = 110) -> str:
    import datetime as dt

    kickoff = rows[warmup]["kickoff"]
    assert isinstance(kickoff, dt.datetime)
    return kickoff.date().isoformat()


def test_engine_dc_default_unchanged(gold_env):
    """缺省 dc：无 gbm 计数（行为不变保护）。"""
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
            label="dc-default",
        )
        assert "gbm_no_matrix_skips" not in result.skipped


def test_engine_invalid_model_fails_loud(gold_env):
    from goalx_backend.evaluation import backtest as bt

    with gold_env(_gold_rows(1, 40)) as (face, duck_con, store):
        with pytest.raises(ValueError, match="未知模型线"):
            bt.run_backtest(
                face,
                duck_con,
                store,
                bt.BacktestParams(  # type: ignore[arg-type]
                    competitions=("E0",), model="bogus"
                ),
                label="bad",
            )
