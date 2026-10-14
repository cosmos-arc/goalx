"""市场融合层（model-iterate 票 02）：纯函数与引擎接线的行为断言。"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from goalx_backend.modelling import fusion as fu
from goalx_backend.modelling.fusion import FusionState, fit_alpha, fuse


def _triplet(h: float, d: float, a: float) -> dict[str, float]:
    return {"h": h, "d": d, "a": a}


def test_fuse_endpoints_and_interpolation():
    """α=1 → 模型三向；α=0 → fair 三向；中间为几何插值且归一。"""
    model = _triplet(0.5, 0.3, 0.2)
    fair = _triplet(0.4, 0.3, 0.3)
    at_one = fuse(model, fair, 1.0)
    assert at_one == pytest.approx(model)
    at_zero = fuse(model, fair, 0.0)
    assert at_zero == pytest.approx(fair)
    mid = fuse(model, fair, 0.5)
    assert sum(mid.values()) == pytest.approx(1.0)
    assert math.isclose(mid["h"], (0.5**0.5 * 0.4**0.5) / _norm(model, fair))


def _norm(model: dict[str, float], fair: dict[str, float]) -> float:
    return sum(
        m**0.5 * f**0.5 for m, f in zip(model.values(), fair.values(), strict=False)
    )


def test_fit_alpha_prefers_model_when_model_is_right():
    """模型优于市场（喊得准）→ α 高；市场优于模型 → α 低/0。"""
    good = [
        fu._HistoryEntry(_triplet(0.7, 0.2, 0.1), _triplet(0.4, 0.3, 0.3), "H", "x")
    ] * 20
    assert fit_alpha(good) >= 0.5
    bad = [
        fu._HistoryEntry(_triplet(0.1, 0.2, 0.7), _triplet(0.45, 0.3, 0.25), "H", "x")
    ] * 20
    assert fit_alpha(bad) <= 0.1  # 市场对 → 模型权重压到 0 附近


def test_fusion_state_freeze_and_era_fallback():
    """状态协议：冷启动 None；热身后 α 冻结（record 不回灌）；era 模式
    该时代样本不足回落全局。"""
    state = FusionState(mode="era", min_samples=30, window=0)
    assert state.fit_for_week("trajectory") is None  # 冷启动
    for _i in range(40):
        # 模型持续优于市场 → 热身后 α 应 > 0
        state.record(_triplet(0.7, 0.2, 0.1), _triplet(0.4, 0.3, 0.3), "H", "psc_proxy")
    alpha = state.fit_for_week("trajectory")
    assert alpha is not None
    assert alpha > 0  # era=trajectory 窗内无样本 → 回落全局（仍是模型信号）
    before = alpha
    state.record(_triplet(0.1, 0.1, 0.8), _triplet(0.4, 0.3, 0.3), "A", "psc_proxy")
    assert state.fit_for_week("trajectory") == before  # 冻结不受赛后新史影响


def test_fusion_era_specific_alpha_when_samples_sufficient():
    """era 窗 ≥MIN_ERA_SAMPLES：两时代各自拟合（专属 α 可不同于全局）。"""
    state = FusionState(mode="era", min_samples=10, window=0)
    # psc 时代：模型优于市场（α 高）；trajectory 时代：市场优于模型（α=0）
    for _ in range(30):
        state.record(_triplet(0.7, 0.2, 0.1), _triplet(0.4, 0.3, 0.3), "H", "psc_proxy")
    for _ in range(30):
        state.record(
            _triplet(0.1, 0.2, 0.7), _triplet(0.45, 0.3, 0.25), "H", "trajectory"
        )
    alpha_psc = state.fit_for_week("psc_proxy")
    alpha_traj = state.fit_for_week("trajectory")
    assert alpha_psc is not None
    assert alpha_psc >= 0.5  # 时代专属：模型信号强
    assert alpha_traj is not None
    assert alpha_traj == 0.0  # 时代专属：模型反向 → α=0


def test_alpha_survival_stats():
    """存活统计：n/均值/中位/存活率/分时代。"""
    log = [
        {"era": "psc_proxy", "alpha": 0.0},
        {"era": "psc_proxy", "alpha": 0.1},
        {"era": "trajectory", "alpha": 0.5},
    ]
    stats = fu.alpha_survival(log)  # type: ignore[arg-type]
    assert stats["n_weeks"] == 3
    assert stats["alpha_mean"] == pytest.approx(0.2)
    assert stats["survival_rate"] == pytest.approx(2 / 3, abs=1e-3)  # 4 位圆整
    assert stats["per_era"]["trajectory"]["alpha_mean"] == 0.5


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
                "version": "gold-test",
            }
        )
    return rows


def _warm_start(rows: list[dict[str, Any]], warmup: int = 110) -> str:
    import datetime as dt

    kickoff = rows[warmup]["kickoff"]
    assert isinstance(kickoff, dt.datetime)
    return kickoff.date().isoformat()


def test_engine_fusion_off_is_default(gold_env):
    """缺省 off：行为不变（无 fusion 计数、无 α 轨迹）。"""
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
            label="f-off",
        )
        assert result.predictions > 0
        assert result.alpha_log == []
        assert "fusion_cold_weeks" not in result.skipped


def test_engine_fusion_global_warms_and_logs_alpha(gold_env):
    """global 融合：冷启动计数 → 热身后 α 轨迹入 summary、预测被融合。"""
    from goalx_backend.evaluation import backtest as bt

    rows = [r for seed in (1, 2) for r in _gold_rows(seed, 160)]
    with gold_env(rows) as (face, duck_con, store):
        start = _warm_start(rows)
        common = {"competitions": ("E0",), "start": start, "min_train_matches": 50}
        base = bt.run_backtest(
            face, duck_con, store, bt.BacktestParams(**common), label="f-base"
        )
        fused = bt.run_backtest(
            face,
            duck_con,
            store,
            bt.BacktestParams(**common, fusion="global", fusion_min_samples=30),
            label="f-global",
        )
        assert base.predictions == fused.predictions
        assert fused.skipped.get("fusion_cold_weeks", 0) > 0
        assert len(fused.alpha_log) > 0
        summary = json.loads(
            face.execute(
                "SELECT summary FROM backtest_runs WHERE id=?", (fused.run_id,)
            ).fetchone()["summary"]
        )
        assert summary["fusion_alpha"]["n_weeks"] == len(fused.alpha_log)
        diff = face.execute(
            """
            SELECT COUNT(*) FROM backtest_predictions p
            JOIN backtest_predictions q
              ON p.match_key = q.match_key AND p.run_id = ? AND q.run_id = ?
            WHERE p.had_probs <> q.had_probs
            """,
            (base.run_id, fused.run_id),
        ).fetchone()[0]
        assert diff > 0  # 热身后有融合差异


def test_engine_fusion_invalid_fails_loud(gold_env):
    from goalx_backend.evaluation import backtest as bt

    with gold_env(_gold_rows(1, 40)) as (face, duck_con, store):
        with pytest.raises(ValueError, match="未知融合"):
            bt.run_backtest(
                face,
                duck_con,
                store,
                bt.BacktestParams(  # type: ignore[arg-type]
                    competitions=("E0",), fusion="bogus"
                ),
                label="bad",
            )
        with pytest.raises(ValueError, match="fusion_window"):
            bt.run_backtest(
                face,
                duck_con,
                store,
                bt.BacktestParams(
                    competitions=("E0",), fusion="global", fusion_window=1
                ),
                label="bad2",
            )
