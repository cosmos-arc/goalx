"""校准层（model-iterate 票 01）：纯函数与引擎接线的行为断言。"""

from __future__ import annotations

import itertools
import json
import math
from typing import Any

import pytest

from goalx_backend.modelling import calibration as cal
from goalx_backend.modelling.calibration import (
    IsotonicCalibrator,
    TemperatureCalibrator,
    make_calibrator,
)


def _sample(probs: tuple[float, float, float], ftr: str) -> dict[str, float] | Any:
    return ({"h": probs[0], "d": probs[1], "a": probs[2]}, ftr)


# ---- 温度缩放 ----


def test_temperature_fit_recovers_flattening():
    """模型系统性过置信（喊 0.6 实际 0.4）→ 拟合 T>1 削峰。"""
    samples = [
        _sample((0.6, 0.2, 0.2), "h" if i % 5 < 2 else ("d" if i % 5 == 3 else "a"))
        for i in range(50)
    ]  # p(h)=0.6 但实际 H 率 0.4
    calibrator = TemperatureCalibrator()
    calibrator.fit(samples)
    assert calibrator.temperature > 1.0
    out = calibrator.apply({"h": 0.6, "d": 0.2, "a": 0.2})
    assert out["h"] < 0.6  # 削峰
    assert sum(out.values()) == pytest.approx(1.0)


def test_temperature_perfect_calibration_stays_near_one():
    """已校准样本 → T≈1（比例不变）。"""
    samples = [
        _sample((0.5, 0.3, 0.2), "h" if i % 10 < 5 else ("d" if i % 10 < 8 else "a"))
        for i in range(100)
    ]
    calibrator = TemperatureCalibrator()
    calibrator.fit(samples)
    assert 0.8 < calibrator.temperature < 1.2


def test_temperature_unfitted_apply_raises():
    """未拟合即应用=程序错误 fail-loud。"""
    with pytest.raises(RuntimeError, match="未拟合"):
        TemperatureCalibrator().apply({"h": 0.4, "d": 0.3, "a": 0.3})
    calibrator = TemperatureCalibrator()
    with pytest.raises(RuntimeError, match="未拟合"):
        assert calibrator.temperature


# ---- isotonic / PAVA ----


def test_pava_monotone_and_merges_violations():
    """违反单调的块被合并到加权均值；映射单调不降。"""
    xs = [0.1, 0.2, 0.3, 0.4, 0.5]
    ys = [0.0, 1.0, 0.0, 1.0, 1.0]  # 0.3 处违反 → 0.2-0.3 合并到 0.5
    f = cal.pava_fit(xs, ys)
    values = [f(x) for x in (0.1, 0.2, 0.3, 0.4, 0.5)]
    assert all(a <= b + 1e-12 for a, b in itertools.pairwise(values))
    assert values[1] == pytest.approx(0.5)
    assert values[2] == pytest.approx(0.5)
    # 端点外推：低于最小 x 用首阶梯，高于最大 x 用末阶梯
    assert f(0.0) == pytest.approx(values[0])
    assert f(0.9) == pytest.approx(values[-1])


def test_pava_ties_aggregate():
    """同 x 样本聚合（和/计数）不重复占位。"""
    f = cal.pava_fit([0.5, 0.5, 0.5, 0.6], [0.0, 1.0, 1.0, 1.0])
    assert f(0.5) == pytest.approx(2.0 / 3.0)
    assert f(0.6) == pytest.approx(1.0)


def test_isotonic_fixes_overestimate_and_renormalizes():
    """分侧 isotonic 修系统性高估（喊 0.6 实际 0.45）+ 三向归一。"""
    samples = [
        _sample((0.6, 0.25, 0.15), "h" if i % 20 < 9 else ("d" if i % 20 < 15 else "a"))
        for i in range(60)
    ]  # H 率 0.45、D 0.3、A 0.25
    calibrator = IsotonicCalibrator()
    calibrator.fit(samples)
    out = calibrator.apply({"h": 0.6, "d": 0.25, "a": 0.15})
    assert out["h"] < 0.6
    assert sum(out.values()) == pytest.approx(1.0)
    assert all(v > 0 and math.isfinite(v) for v in out.values())


def test_isotonic_unfitted_apply_raises():
    with pytest.raises(RuntimeError, match="未拟合"):
        IsotonicCalibrator().apply({"h": 0.4, "d": 0.3, "a": 0.3})


def test_make_calibrator_factory():
    assert isinstance(make_calibrator("temperature"), TemperatureCalibrator)
    assert isinstance(make_calibrator("isotonic"), IsotonicCalibrator)
    with pytest.raises(ValueError, match="未知校准"):
        make_calibrator("bogus")


def test_fit_accepts_uppercase_ftr():
    """引擎 ftr 大写（H/D/A）与 SELECTIONS 小写双轨——fit 边界归一
    （真树 run=6 首跑崩溃的回归）。"""
    samples = [
        _sample((0.6, 0.2, 0.2), "H" if i % 5 < 2 else ("D" if i % 5 == 3 else "A"))
        for i in range(50)
    ]
    temperature = TemperatureCalibrator()
    temperature.fit(samples)  # 不抛 ValueError 即过
    assert temperature.temperature > 1.0
    isotonic = IsotonicCalibrator()
    isotonic.fit(samples)
    out = isotonic.apply({"h": 0.6, "d": 0.2, "a": 0.2})
    assert sum(out.values()) == pytest.approx(1.0)


# ---- 引擎接线（gold_env 全链） ----


def _gold_rows(seed: int, count: int) -> list[dict[str, Any]]:
    """可预测伪随机 gold 行集（周分组天然形成，每周 5 场）。"""
    import datetime as dt

    rows = []
    for i in range(count):
        day = 1 + (i // 5) * 7
        gh = (seed * 7 + i * 3) % 4
        ga = (seed * 11 + i * 5) % 3
        kickoff = dt.datetime(2023, 9, 1) + dt.timedelta(days=day - 1)
        rows.append(
            {
                "sid": f"m{seed}-{i}",
                "league": "英超",
                "kickoff": kickoff,
                "season": "2023-24",
                "home": f"T{seed}H{i % 8}",
                "away": f"T{seed}A{(i * 3 + 1) % 8}",
                "home_goals": gh,
                "away_goals": ga,
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
    """窗口起点=第 warmup 行的日期（前 warmup 行只进训练池）。"""
    import datetime as dt

    kickoff = rows[warmup]["kickoff"]
    assert isinstance(kickoff, dt.datetime)
    return kickoff.date().isoformat()


def test_engine_calibration_off_is_default(gold_env):
    """缺省 off：run params 记档且不产生校准计数（行为不变保护）。"""
    from goalx_backend.evaluation import backtest as bt

    rows = _gold_rows(1, 160)
    with gold_env(rows) as (face, duck_con, store):
        params = bt.BacktestParams(
            competitions=("E0",), start=_warm_start(rows), min_train_matches=50
        )
        result = bt.run_backtest(face, duck_con, store, params, label="cal-off")
        assert result.predictions > 0
        run = face.execute(
            "SELECT params FROM backtest_runs WHERE id=?", (result.run_id,)
        ).fetchone()
        assert json.loads(run["params"])["calibration"] == "off"
        assert "calibration_cold_weeks" not in result.skipped


def test_engine_isotonic_warms_up_and_changes_probs(gold_env):
    """isotonic：冷启动透传计数 + 热身后 had_probs 被校准（off vs
    isotonic 两 run 预测面出现差异行）。"""
    from goalx_backend.evaluation import backtest as bt

    rows = [r for seed in (1, 2) for r in _gold_rows(seed, 160)]
    with gold_env(rows) as (face, duck_con, store):
        start = _warm_start(rows)
        common = {"competitions": ("E0",), "start": start, "min_train_matches": 50}
        base = bt.run_backtest(
            face, duck_con, store, bt.BacktestParams(**common), label="cal-base"
        )
        iso = bt.run_backtest(
            face,
            duck_con,
            store,
            bt.BacktestParams(
                **common, calibration="isotonic", calibration_min_samples=30
            ),
            label="cal-iso",
        )
        assert base.predictions == iso.predictions  # 校准不改预测覆盖
        assert iso.skipped.get("calibration_cold_weeks", 0) > 0  # 冷启动有计数
        diff = face.execute(
            """
            SELECT COUNT(*) FROM backtest_predictions p
            JOIN backtest_predictions q
              ON p.match_key = q.match_key AND p.run_id = ? AND q.run_id = ?
            WHERE p.had_probs <> q.had_probs
            """,
            (base.run_id, iso.run_id),
        ).fetchone()[0]
        assert diff > 0  # 热身后有校准差异
        params = face.execute(
            "SELECT params FROM backtest_runs WHERE id=?", (iso.run_id,)
        ).fetchone()
        assert json.loads(params["params"])["calibration"] == "isotonic"


def test_engine_calibration_invalid_fails_loud(gold_env):
    from goalx_backend.evaluation import backtest as bt

    with gold_env(_gold_rows(1, 40)) as (face, duck_con, store):
        with pytest.raises(ValueError, match="未知校准"):
            bt.run_backtest(
                face,
                duck_con,
                store,
                bt.BacktestParams(  # type: ignore[arg-type]
                    competitions=("E0",), calibration="bogus"
                ),
                label="bad",
            )


# ---- 对照报告 ----


def test_calibration_state_freeze_semantics():
    """PIT 冻结：fit_for_week 返回的校准器不受后续 record 影响。"""
    from goalx_backend.evaluation import backtest as bt

    state = bt._CalibrationState(IsotonicCalibrator())
    for i in range(40):
        state.record(
            {"h": 0.4 + (i % 3) * 0.05, "d": 0.3, "a": 0.3}, "H" if i % 2 else "A"
        )
    params = bt.BacktestParams(calibration_min_samples=30)
    frozen = state.fit_for_week(params)
    assert frozen is not None
    probs = {"h": 0.5, "d": 0.3, "a": 0.2}
    first = frozen.apply(probs)
    state.record({"h": 0.1, "d": 0.1, "a": 0.8}, "A")  # 赛后新史不回灌
    assert frozen.apply(probs) == first
    state.fit_for_week(params)  # 下周原地重拟合：已算出的输出不变（F7）
    assert frozen.apply(probs) == first


def test_isotonic_degenerate_output_falls_back_to_input():
    """correctness F1 实证反例：查询点三侧阶梯全零 → 回退原始三向，
    不落非概率向量。"""
    samples = []
    for _ in range(10):
        samples.append(_sample((0.2, 0.55, 0.25), "D"))
        samples.append(_sample((0.3, 0.5, 0.2), "H"))
        samples.append(_sample((0.5, 0.2, 0.3), "H"))
    calibrator = IsotonicCalibrator()
    calibrator.fit(samples)
    out = calibrator.apply({"h": 0.2, "d": 0.5, "a": 0.3})
    assert sum(out.values()) == pytest.approx(1.0)
    assert all(v >= 0 and math.isfinite(v) for v in out.values())


def test_calibration_param_validation_fails_loud():
    """correctness F2：min_samples<2 / window 负或 1 → 开跑前拒绝。"""
    from goalx_backend.evaluation import backtest as bt

    for bad in (
        {"calibration": "isotonic", "calibration_min_samples": 1},
        {"calibration": "temperature", "calibration_window": 1},
        {"calibration": "temperature", "calibration_window": -5},
    ):
        with pytest.raises(ValueError):
            bt._validate_window(bt.BacktestParams(**bad))


def test_model_layers_report_pairs_and_verdicts(gold_env, tmp_path):
    """两 run 对照：配对样本 n 一致、三元组/ECE 落位、结论句生成。"""
    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import backtest as bt
    from goalx_backend.evaluation import model_layers as mlp

    rows = [r for seed in (1, 2) for r in _gold_rows(seed, 160)]
    with gold_env(rows) as (face, duck_con, _store):
        start = _warm_start(rows)
        common = {"competitions": ("E0",), "start": start, "min_train_matches": 50}
        base = bt.run_backtest(
            face, duck_con, _store, bt.BacktestParams(**common), label="r-base"
        )
        iso = bt.run_backtest(
            face,
            duck_con,
            _store,
            bt.BacktestParams(
                **common, calibration="isotonic", calibration_min_samples=30
            ),
            label="r-iso",
        )
        report_store = CorpusStore(tmp_path)
        try:
            payload = mlp.build_model_layers_report(
                face,
                report_store,
                treatments=[("t01-iso", iso.run_id)],
                baseline_run_id=base.run_id,
            )
        finally:
            report_store.close()
    base_n = payload["baseline"]["prediction"]["n"]
    treat_n = payload["treatments"]["t01-iso"]["prediction"]["n"]
    assert base_n > 0
    assert treat_n == base_n  # 配对面=交集
    triple = payload["baseline"]["prediction"]["calibration_triple"]
    assert set(triple) == {"h", "d", "a"}
    md = (tmp_path / "reports" / "model-layers.md").read_text(encoding="utf-8")
    assert "校准三元组" in md
    assert "t01-iso" in md
    assert "注量" in md
