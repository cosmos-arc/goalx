"""错价桶方向映射限定复核（caliber-loop 票 03）：映射验证/方向/等量分层断言。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from goalx_backend.data import gold as gold_mod
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.evaluation import bucket_reaudit as bkt
from goalx_backend.evaluation import signal_attribution as sig

DAY = "2025-10-01"
MODEL = {"h": 0.50, "d": 0.30, "a": 0.20}
FAIR = {"h": 0.40, "d": 0.30, "a": 0.30}


def _gold_row(
    sid: str,
    *,
    late_move: float | None = None,
    n_pre: float | None = None,
    ftr_gold: tuple[int, int] = (2, 1),
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "sid": sid,
        "league": "英超",
        "kickoff": datetime.fromisoformat(f"{DAY}T20:00:00"),
        "season": "2025-26",
        "home": "A",
        "away": "B",
        "home_goals": ftr_gold[0],
        "away_goals": ftr_gold[1],
        "stage": "常规轮",
        "era": gold_mod.ERA_TRAJECTORY,
        "admin_excluded": False,
        "version": gold_mod.GOLD_VERSION,
    }
    if late_move is not None:
        # late_move_h = close_h − q1h_h（devig 主队位移）：构造正值=pos 桶
        row |= {
            "close1x2_h": 2.0,
            "close1x2_d": 3.4,
            "close1x2_a": 3.8,
            "q1h1x2_h": _odds_for_move(late_move),
            "q1h1x2_d": 3.4,
            "q1h1x2_a": 3.8,
        }
    if n_pre is not None:
        row["ah_n_pre"] = n_pre
    return row


def _odds_for_move(late_move: float) -> float:
    """close 主队概率 p_c 与 q1h 概率 p_e 满足 p_c−p_e=late_move 的近似价。"""
    p_c = 1.0 / 2.0
    p_e = p_c - late_move
    return 1.0 / p_e


def _seed_run(face: sqlite3.Connection, haircut: float = 0.1) -> int:
    cur = face.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at)"
        " VALUES ('t03', ?, 'done', '2026-10-16T00:00:00+00:00')",
        (json.dumps({"haircut": haircut, "markets": ["had"]}),),
    )
    return int(cur.lastrowid or 0)


_WEEKS = ("2025-09-24", "2025-10-01", "2025-10-08", "2025-10-15")


def _seed_pred(
    face: sqlite3.Connection,
    run_id: int,
    key: str,
    *,
    ftr: str = "H",
    fair: dict[str, float] | None = None,
    match_date: str = "2025-10-01",
) -> None:
    face.execute(
        """
        INSERT INTO backtest_predictions
        (run_id, match_key, competition, season, match_date, home_team, away_team,
         era, ftr, had_probs, fair_probs, fair_source, model_fingerprint,
         train_window_end)
        VALUES (?, ?, 'E0', '2025-26', ?, 'A', 'B', NULL, ?, ?, ?,
                'psc', 'fp', '2025-09-25')
        """,
        (run_id, key, match_date, ftr, json.dumps(MODEL), json.dumps(fair or FAIR)),
    )


def _write_source_report(
    store: CorpusStore,
    run_id: int,
    *,
    late_move_d_bias: float = 0.0164,
    late_move_d_t: float = 2.39,
    extra_positive: bool = False,
) -> None:
    """合成源报告（faces 两命名桶 + 可选第三正侧行）。"""
    faces: dict[str, dict[str, dict[str, Any]]] = {
        "late_move_h": {
            "pos": {
                "n": 4022,
                "market_bias_h": -0.0188,
                "market_bias_h_t": -2.6,
                "market_bias_d": late_move_d_bias,
                "market_bias_d_t": late_move_d_t,
                "market_bias_a": 0.0024,
                "market_bias_a_t": 0.36,
            }
        },
        "ah_phase_pre": {
            "low": {
                "n": 11307,
                "market_bias_h": 0.0116,
                "market_bias_h_t": 2.62,
                "market_bias_d": -0.0058,
                "market_bias_d_t": -1.43,
                "market_bias_a": -0.0057,
                "market_bias_a_t": -1.27,
            },
            "mid": {
                "n": 100,
                "market_bias_h": -0.0097,
                "market_bias_h_t": -2.27,
            },
        },
    }
    if extra_positive:
        faces["ou_line_drift"] = {
            "pos": {
                "n": 8107,
                "market_bias_h": 0.0118,
                "market_bias_h_t": 2.39,
            }
        }
    reports = store.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "signal-attribution.json").write_text(
        json.dumps({"run_id": run_id, "faces": faces}, ensure_ascii=False),
        encoding="utf-8",
    )


def _gold_rows(count_lm: int, count_pre: int, count_pool: int) -> list[dict[str, Any]]:
    """late_move=pos 桶 count_lm 场 + ah_phase_pre=low 桶 count_pre 场 + 池场。"""
    rows = []
    for i in range(count_lm):  # 桶臂 1：late_move_h:pos → 买 d，多平局
        rows.append(_gold_row(f"lm{i}", late_move=0.02, ftr_gold=(1, 1)))
    for i in range(count_pre):  # 桶臂 2：ah_phase_pre:low → 买 h，多主胜
        rows.append(_gold_row(f"pre{i}", n_pre=0.0, ftr_gold=(3, 0)))
    for i in range(count_pool):  # 对照池：无特征（桶标签空=非映射桶）
        rows.append(_gold_row(f"pool{i}", ftr_gold=(0, 1)))
    return rows


_FTR_OF = {"lm": "D", "pre": "H", "pool": "A"}


def _build(gold_env, rows, *, haircut: float = 0.1, **report_kwargs):
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face, haircut)
        for i, row in enumerate(rows):
            ftr = _FTR_OF[str(row["sid"]).rstrip("0123456789")]
            _seed_pred(face, run_id, str(row["sid"]), ftr=ftr, match_date=_WEEKS[i % 4])
        # 池场 fair 同构——对照注赔率同档（分层可配对）
        _write_source_report(store, run_id, **report_kwargs)
        payload = bkt.build_bucket_reaudit_report(face, duck_con, store, run_id=run_id)
        return payload, store


def test_reaudit_direction_and_equal_stratification(gold_env):
    """方向：桶臂按映射侧买（d/h）；等量分层计数差=0；平注无 EV/Kelly。"""
    rows = _gold_rows(count_lm=20, count_pre=15, count_pool=200)
    payload, store = _build(gold_env, rows)
    # 桶臂=桶成员全体（20 late_move + 15 ah_pre=low），不经 EV 门槛
    assert payload["arm"]["n"] == 35
    assert payload["control"]["n"] == 35  # 等量
    assert payload["dim_count_diffs"] == {}  # 分维计数差=0（验收）
    # lm 桶全平局 → 买 d 全中；pre 桶全主胜 → 买 h 全中 → roi 为正
    assert payload["by_predicate"]["late_move_h:pos→d"]["hit_rate"] == 1.0
    assert payload["by_predicate"]["ah_phase_pre:low→h"]["hit_rate"] == 1.0
    assert payload["arm"]["roi_flat"] > payload["control"]["roi_flat"]
    assert payload["conclusion"] == "方向映射后探索性一致"
    assert payload["week_joint_bootstrap"] is not None
    assert payload["params"]["kelly"] is None
    assert payload["params"]["parlay"] is None
    assert Path(store.root / "reports" / "bucket-reaudit.md").exists()
    store.close()


def test_reaudit_mapping_fails_loud_on_negative_side(gold_env):
    """映射验证：命名侧偏差非正=fail-loud（方向映射错=票面失败）。"""
    rows = _gold_rows(5, 5, 20)
    with pytest.raises(ValueError, match="方向映射违例"):
        _build(gold_env, rows, late_move_d_bias=-0.0188, late_move_d_t=-2.6)


def test_reaudit_mapping_fails_loud_on_weak_t(gold_env):
    """映射验证：|t|<门=fail-loud（不入复核面）。"""
    rows = _gold_rows(5, 5, 20)
    with pytest.raises(ValueError, match=r"\|t\|="):
        _build(gold_env, rows, late_move_d_bias=0.0164, late_move_d_t=1.2)


def test_reaudit_extra_positive_flagged_not_run(gold_env):
    """第三正侧候选（ou_line_drift:pos→h）：进裁决排除清单，不进桶臂。"""
    rows = _gold_rows(5, 5, 20)
    payload, store = _build(gold_env, rows, extra_positive=True)
    excluded = payload["mapping_provenance"]["excluded_by_ruling"]
    assert any(e["feature"] == "ou_line_drift" and e["side"] == "h" for e in excluded)
    assert payload["arm"]["n"] == 10  # 只有两命名谓词的桶成员
    store.close()


def test_reaudit_source_run_mismatch_fails(gold_env):
    """源报告 run 与复核 run 不一致=fail-loud（真相源对齐）。"""
    rows = _gold_rows(5, 5, 20)
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        for row in rows:
            _seed_pred(face, run_id, str(row["sid"]))
        _write_source_report(store, run_id + 999)
        with pytest.raises(ValueError, match="不一致"):
            bkt.build_bucket_reaudit_report(face, duck_con, store, run_id=run_id)
        store.close()


def test_bucket_labels_match_source_convention():
    """桶标签与源报告同口径：drift 符号三分、dist 全集分位切点。"""
    samples = [
        _sample_of({"late_move_h": 0.01}, "x1"),
        _sample_of({"late_move_h": -0.01}, "x2"),
        _sample_of({"late_move_h": 0.0}, "x3"),
    ]
    labels = bkt._bucket_labels(samples)
    assert [labels[s.sid]["late_move_h"] for s in samples] == ["pos", "neg", "zero"]


def _sample_of(features: dict[str, float], sid: str = "x") -> sig.MatchSample:
    signal = sig._MatchSignal(
        era="trajectory",
        model_edges={"h": 0.0, "d": 0.0, "a": 0.0},
        realized={"h": 0.0, "d": 0.0, "a": 0.0},
        features=features,
    )
    return sig.MatchSample(
        sid=sid,
        competition="E0",
        match_date="2025-10-01",
        ftr="h",
        fair=dict(FAIR),
        signal=signal,
    )
