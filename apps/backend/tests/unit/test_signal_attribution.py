"""信号级归因报告（票 21）：残差×收盘族特征分桶的行为断言。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

import pytest

from goalx_backend.data import gold as gold_mod
from goalx_backend.evaluation import signal_attribution as sig

DAY = "2025-10-01"
MODEL = {"h": 0.50, "d": 0.30, "a": 0.20}
FAIR = {"h": 0.40, "d": 0.30, "a": 0.30}


def _gold_row(sid: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sid": sid,
        "league": "英超",
        "kickoff": datetime.fromisoformat(f"{DAY}T20:00:00"),
        "season": "2025-26",
        "home": "A",
        "away": "B",
        "home_goals": 2,
        "away_goals": 1,
        "stage": "常规轮",
        "era": gold_mod.ERA_TRAJECTORY,
        "admin_excluded": False,
        "version": gold_mod.GOLD_VERSION,
    }
    return base | overrides


def _seed_run(face: sqlite3.Connection) -> int:
    cur = face.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at)"
        " VALUES ('t21', ?, 'done', '2026-10-14T00:00:00+00:00')",
        (json.dumps({"haircut": 0.1, "markets": ["had"]}),),
    )
    return int(cur.lastrowid or 0)


def _seed_pred(
    face: sqlite3.Connection,
    run_id: int,
    key: str,
    *,
    ftr: str | None = "H",
    model: dict[str, float] | None = None,
    fair: dict[str, float] | None = None,
) -> None:
    face.execute(
        """
        INSERT INTO backtest_predictions
        (run_id, match_key, competition, season, match_date, home_team, away_team,
         era, ftr, had_probs, fair_probs, fair_source, model_fingerprint,
         train_window_end)
        VALUES (?, ?, 'E0', '2025-26', '2025-10-01', 'A', 'B', NULL, ?, ?, ?,
                'psc', 'fp', '2025-09-25')
        """,
        (
            run_id,
            key,
            ftr,
            json.dumps(model or MODEL),
            json.dumps(fair or FAIR),
        ),
    )


def _build(gold_env, rows, preds_kwargs=None):
    """通用装配：gold 行集 + 预测（MODEL vs FAIR）→ payload。"""
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        for row in rows:
            kwargs = (preds_kwargs or {}).get(row["sid"], {})
            _seed_pred(face, run_id, row["sid"], **kwargs)
        payload = sig.build_signal_attribution_report(
            face, duck_con, store, run_id=run_id
        )
    return payload


def test_edge_and_realized_math(gold_env):
    """model_edge/realized 手算对齐：模型主张 +0.10，市场误差 +0.60。"""
    payload = _build(gold_env, [_gold_row("s1", ah_line_drift=0.25)])
    overall = payload["overall"]
    assert overall["n"] == 1
    assert overall["model_edge_h_mean"] == pytest.approx(0.10)
    # ftr=H → realized_h = 1 − 0.40 = 0.60；realized_d = 0 − 0.30 = −0.30
    assert overall["market_bias_h"] == pytest.approx(0.60)
    assert overall["market_bias_d"] == pytest.approx(-0.30)


def test_drift_buckets_by_sign(gold_env):
    """漂移特征按符号三分：neg/zero/pos 各归各桶。"""
    rows = [
        _gold_row("s1", ah_line_drift=-0.5),
        _gold_row("s2", ah_line_drift=0.0),
        _gold_row("s3", ah_line_drift=0.5),
    ]
    payload = _build(gold_env, rows)
    buckets = payload["faces"]["ah_line_drift"]
    assert set(buckets) == {"neg", "zero", "pos"}
    assert all(b["n"] == 1 for b in buckets.values())


def test_dist_features_tertile_buckets(gold_env):
    """分布类特征按分位三分（书数 1/2/3 → low/mid/high）。"""
    rows = [
        _gold_row("s1", ah_n_books=1),
        _gold_row("s2", ah_n_books=2),
        _gold_row("s3", ah_n_books=3),
    ]
    payload = _build(gold_env, rows)
    buckets = payload["faces"]["ah_n_books"]
    assert set(buckets) == {"low", "mid", "high"}
    assert buckets["low"]["n"] == 1
    assert buckets["high"]["n"] == 1


def test_late_move_feature_from_q1h_slice(gold_env):
    """临场切片特征：q1h→close 主队概率位移（devig 两端同法）。"""
    rows = [
        _gold_row(
            "s1",
            q1h1x2_h=2.4,
            q1h1x2_d=3.4,
            q1h1x2_a=3.8,
            close1x2_h=2.0,
            close1x2_d=3.4,
            close1x2_a=3.8,
        )
    ]
    payload = _build(gold_env, rows)
    assert "late_move_h" in payload["faces"]  # close 概率 > q1h → pos 桶
    assert payload["faces"]["late_move_h"]["pos"]["n"] == 1


def test_capture_positive_when_model_tracks_market_error(gold_env):
    """捕捉面：模型残差与市场误差同向且两序列有方差 → r>0。"""
    rows = []
    preds_kwargs: dict[str, dict[str, Any]] = {}
    # pos 桶：ftr 全 H，fair_h 递增（realized=1−fair 递减），
    # model_h 固定 0.40（edge=0.40−fair 递减）→ 两序列同向，r>0
    for i, fair_h in enumerate((0.26, 0.30, 0.34)):
        sid = f"p{i}"
        rows.append(_gold_row(sid, ah_line_drift=0.25))
        preds_kwargs[sid] = {
            "ftr": "H",
            "model": {"h": 0.40, "d": 0.30, "a": 0.30},
            "fair": {"h": fair_h, "d": 0.35, "a": round(1 - fair_h - 0.35, 2)},
        }
    # neg 桶：ftr 全 A，fair_h 递增（realized=−fair 递减），
    # model_h 固定 0.20（edge 递减）→ 同向，r>0
    for i, fair_h in enumerate((0.36, 0.40, 0.44)):
        sid = f"n{i}"
        rows.append(_gold_row(sid, ah_line_drift=-0.5))
        preds_kwargs[sid] = {
            "ftr": "A",
            "model": {"h": 0.20, "d": 0.30, "a": 0.50},
            "fair": {"h": fair_h, "d": 0.30, "a": round(1 - fair_h - 0.30, 2)},
        }
    payload = _build(gold_env, rows, preds_kwargs)
    pos_bucket = payload["faces"]["ah_line_drift"]["pos"]
    neg_bucket = payload["faces"]["ah_line_drift"]["neg"]
    assert pos_bucket["market_bias_h"] == pytest.approx(0.70)  # mean(1−fair)
    assert pos_bucket["model_edge_h_mean"] == pytest.approx(0.10)  # mean(0.4−fair)
    assert pos_bucket["capture_h"] is not None
    assert pos_bucket["capture_h"] > 0
    assert neg_bucket["market_bias_h"] == pytest.approx(-0.40)
    assert neg_bucket["capture_h"] is not None
    assert neg_bucket["capture_h"] > 0


def test_missing_feature_rows_excluded_per_feature(gold_env):
    """某特征缺列的场次只从该特征面剔除，不剔除样本本身。"""
    rows = [_gold_row("s1", ah_line_drift=0.25), _gold_row("s2")]  # s2 无 drift
    payload = _build(gold_env, rows)
    assert payload["n_samples"] == 2
    assert payload["faces"]["ah_line_drift"]["pos"]["n"] == 1


def test_keyed_features_all_axes_present(gold_env):
    """键/列错位回归（Spec 审查）：key≠column 的轴（分歧/相位/水位）
    必须出面——曾整族静默消失。"""
    rows = [
        _gold_row(
            "s1",
            ah_close_dispersion=0.1,
            ah_n_pre=5,
            ah_close_home_water_med=0.95,
        ),
        _gold_row(
            "s2",
            ah_close_dispersion=0.3,
            ah_n_pre=15,
            ah_close_home_water_med=1.05,
        ),
        _gold_row(
            "s3",
            ah_close_dispersion=0.6,
            ah_n_pre=25,
            ah_close_home_water_med=1.15,
        ),
    ]
    payload = _build(gold_env, rows)
    for feature in ("ah_dispersion", "ah_phase_pre", "ah_home_water"):
        assert feature in payload["faces"], f"{feature} 面缺席（键/列错位回归）"
        assert payload["faces"][feature]["low"]["n"] == 1
        assert payload["faces"][feature]["high"]["n"] == 1


def test_late_move24_and_ah_same_line_slices(gold_env):
    """q24h 1x2 位移与 AH 锚同线水位位移都成面；AH 跨线不比。"""
    rows = [
        _gold_row(
            "s1",
            q24h1x2_h=2.4,
            q24h1x2_d=3.4,
            q24h1x2_a=3.8,
            close1x2_h=2.0,
            close1x2_d=3.4,
            close1x2_a=3.8,
            q24hah_line=-1.0,
            q24hah_home_water=0.90,
            q24hah_away_water=0.95,
            ah_anchor_close_line=-1.0,  # 同线 → 面出现
            ah_anchor_close_home_water=0.85,
            ah_anchor_close_away_water=0.95,
        ),
        _gold_row(
            "s2",
            q24hah_line=-0.5,
            ah_anchor_close_line=-1.0,  # 跨线 → late_move_ah_home 缺
        ),
    ]
    payload = _build(gold_env, rows)
    assert payload["faces"]["late_move24_h"]["pos"]["n"] == 1
    # s1 同线：close 主水 0.85 < q24h 0.90 → 主队概率降 = neg 桶；
    # s2 跨线缺位不进该面
    assert payload["faces"]["late_move_ah_home"]["neg"]["n"] == 1


def test_three_sided_residuals(gold_env):
    """三向残差：a 侧与 h/d 同行落面。"""
    payload = _build(gold_env, [_gold_row("s1")])
    overall = payload["overall"]
    # model a=0.20 fair a=0.30 → edge −0.10；ftr=H → realized=−0.30
    assert overall["model_edge_a_mean"] == pytest.approx(-0.10)
    assert overall["market_bias_a"] == pytest.approx(-0.30)


def test_skips_counted_honestly(gold_env):
    """ftr 空/no gold 行/fair 缺字段：各自计数，不进样本。"""
    rows = [_gold_row("s1"), _gold_row("s4"), _gold_row("s5")]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s1")  # 正常
        _seed_pred(face, run_id, "s2")  # 无 gold 行
        _seed_pred(face, run_id, "s3", ftr=None)  # 赛果缺失
        face.execute(  # s4 有 gold 行但 fair 缺 a 键：no_fair 计数
            """
            INSERT INTO backtest_predictions
            (run_id, match_key, competition, season, match_date, home_team,
             away_team, era, ftr, had_probs, fair_probs, fair_source,
             model_fingerprint, train_window_end)
            VALUES (?, 's4', 'E0', '2025-26', '2025-10-01', 'A', 'B', NULL,
                    'H', ?, ?, 'psc', 'fp', '2025-09-25')
            """,
            (
                run_id,
                json.dumps(MODEL),
                json.dumps({"h": 0.4, "d": 0.3}),
            ),
        )
        face.execute(  # s5 非对象 JSON（旧形态）：no_fair 计数不炸（F2 回归）
            """
            INSERT INTO backtest_predictions
            (run_id, match_key, competition, season, match_date, home_team,
             away_team, era, ftr, had_probs, fair_probs, fair_source,
             model_fingerprint, train_window_end)
            VALUES (?, 's5', 'E0', '2025-26', '2025-10-01', 'A', 'B', NULL,
                    'H', ?, 'null', 'psc', 'fp', '2025-09-25')
            """,
            (run_id, json.dumps(MODEL)),
        )
        payload = sig.build_signal_attribution_report(
            face, duck_con, store, run_id=run_id
        )
    assert payload["n_samples"] == 1
    assert payload["skipped"]["no_gold"] == 1
    assert payload["skipped"]["no_ftr"] == 1
    assert payload["skipped"]["no_fair"] == 2


def test_verdict_lines_dual_lists(gold_env):
    """结论双清单：无松动桶有声明行；市场错价桶按 |t|≥2 独立列出。"""
    payload = _build(gold_env, [_gold_row("s1", ah_line_drift=0.25)])
    lines = sig._verdict_lines(payload["faces"])
    # 单样本 n<100：两清单都是诚实声明行
    assert any("无松动桶" in line for line in lines)
    assert any("无市场错价桶" in line for line in lines)


def test_report_files_written(gold_env):
    """reports/ 双视图落盘。"""
    rows = [_gold_row("s1", ah_line_drift=0.25)]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s1")
        sig.build_signal_attribution_report(face, duck_con, store, run_id=run_id)
        reports = store.root / "reports"
        assert (reports / "signal-attribution.json").exists()
        md = (reports / "signal-attribution.md").read_text(encoding="utf-8")
    assert "归因结论" in md
    assert "市场错价桶" in md
    assert "总体面" in md
    assert "判读注意" in md
