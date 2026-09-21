"""彩池 v2 搏冷十年复验票 51 测试：候选口径镜像/分期/早期镜/确定性。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from goalx_backend.data import results as rs_store
from goalx_backend.evaluation import pool_replay

# 赔率组合经 Shin 实算选定（注释值为该组赔率的期望信号）：
# ROW_COLD   'a' 端彩池 EV +0.2313 / 固定镜 +0.6199 / 早期镜 +0.2602（早正收正）
# ROW_DEGEN  无 PSC → avgc 分期：候选 EV ≡ −0.35（fair=share 同源退化）
# ROW_NOCOLD 三向份额均 ≥25% → 零候选
# ROW_DIES   'a' 早期镜 +0.4039 → 收盘 −0.0667（正 EV 冷门消失样本）
# ROW_LATE   'a' 早期镜 −0.1417 → 收盘 +0.2986（晚现窗口样本）
_ROWS: list[dict[str, Any]] = [
    {
        "competition": "E0",
        "season": "2425",
        "match_date": "2025-05-10",
        "home_team": "Man United",
        "away_team": "Chelsea",
        "fthg": 0,
        "ftag": 2,
        "ftr": "A",
        "psc_home": 1.75,
        "psc_draw": 3.80,
        "psc_away": 4.20,
        "psh_home": 1.70,
        "psh_draw": 3.90,
        "psh_away": 4.00,
        "avgc_home": 1.40,
        "avgc_draw": 4.50,
        "avgc_away": 7.50,
    },
    {
        "competition": "E0",
        "season": "2425",
        "match_date": "2025-05-11",
        "home_team": "Arsenal",
        "away_team": "Everton",
        "fthg": 1,
        "ftag": 1,
        "ftr": "D",
        "psc_home": None,
        "psc_draw": None,
        "psc_away": None,
        "psh_home": None,
        "psh_draw": None,
        "psh_away": None,
        "avgc_home": 1.50,
        "avgc_draw": 4.40,
        "avgc_away": 6.50,
    },
    {
        "competition": "E0",
        "season": "2425",
        "match_date": "2025-05-12",
        "home_team": "Liverpool",
        "away_team": "Brighton",
        "fthg": 3,
        "ftag": 0,
        "ftr": "H",
        "psc_home": 2.55,
        "psc_draw": 3.35,
        "psc_away": 2.75,
        "psh_home": None,
        "psh_draw": None,
        "psh_away": None,
        "avgc_home": 2.60,
        "avgc_draw": 3.30,
        "avgc_away": 2.70,
    },
    {
        "competition": "E0",
        "season": "2425",
        "match_date": "2025-05-13",
        "home_team": "Newcastle",
        "away_team": "Wolves",
        "fthg": 2,
        "ftag": 1,
        "ftr": "H",
        "psc_home": 1.48,
        "psc_draw": 4.50,
        "psc_away": 7.20,
        "psh_home": 1.60,
        "psh_draw": 4.20,
        "psh_away": 4.60,
        "avgc_home": 1.30,
        "avgc_draw": 5.00,
        "avgc_away": 9.00,
    },
    {
        "competition": "E0",
        "season": "2324",
        "match_date": "2024-05-19",
        "home_team": "Man City",
        "away_team": "West Ham",
        "fthg": 0,
        "ftag": 2,
        "ftr": "A",
        "psc_home": 1.70,
        "psc_draw": 3.90,
        "psc_away": 4.30,
        "psh_home": 1.45,
        "psh_draw": 4.60,
        "psh_away": 6.40,
        "avgc_home": 1.35,
        "avgc_draw": 4.80,
        "avgc_away": 8.00,
    },
]


def _seed(db: sqlite3.Connection) -> None:
    assert rs_store.upsert_hist_matches(db, _ROWS) == 5


def _cold_row(cands: list[Any], home: str) -> dict[str, Any]:
    """按主队隔离一场的 psc 分期候选（选向 → 候选）。"""
    return {c.selection: c for c in cands if c.stage == "psc" and c.home_team == home}


def test_collect_candidates_psc_stage_cold_signs(db) -> None:
    _seed(db)
    cands, coverage = pool_replay.collect_candidates(
        db, competitions=("E0",), seasons=("2324", "2425")
    )
    assert coverage == {
        "rows": 5,
        "out_of_scope": 0,
        "no_share_avgc": 0,
        "stage_psc": 4,
        "stage_avgc": 1,
        "psh_present": 3,
    }
    cold = _cold_row(cands, "Man United")
    # 2525-05-10 场：'a' 正 EV 冷门（fair/份额 越过 1/0.65 盈亏线）
    a = cold["a"]
    assert a.share < 0.25
    assert a.pool_ev > 0
    assert a.fx_ev > 0  # 固定赔率镜：soft 价高于 sharp fair → 同向为正
    assert a.early_pool_ev is not None  # 早正收正
    assert a.early_pool_ev > 0
    assert a.hit is True  # ftr=A
    d = cold["d"]
    assert d.pool_ev < 0  # 同场另一冷选项未越盈亏线的对照
    assert d.hit is False


def test_avgc_stage_degenerate_ev(db) -> None:
    _seed(db)
    cands, _ = pool_replay.collect_candidates(
        db, competitions=("E0",), seasons=("2425",)
    )
    degen = [c for c in cands if c.stage == "avgc"]
    assert {c.selection for c in degen} == {"d", "a"}
    for cand in degen:
        # fair=share 同源：EV ≡ 65% − 1（口径退化的精确验证）
        assert cand.pool_ev == pytest.approx(-0.35)
        assert cand.early_pool_ev is None


def test_no_candidates_when_shares_high(db) -> None:
    _seed(db)
    cands, _ = pool_replay.collect_candidates(
        db, competitions=("E0",), seasons=("2425",)
    )
    # 全部种子场主胜份额均 ≥25%（含三向均衡的 NOCOLD 场）→ 主胜从不成候选
    assert not [c for c in cands if c.selection == "h"]


def test_early_lens_persistence_and_late_only(db) -> None:
    _seed(db)
    cands, _ = pool_replay.collect_candidates(
        db, competitions=("E0",), seasons=("2324", "2425")
    )
    lens = pool_replay._early_lens(cands)["psc"]
    assert lens["early_window_candidates"] == 6  # 三场 PSH 全有 × 每场 d/a 两候选
    assert lens["early_ev_positive"] == 2  # COLD'a' + DIES'a'
    assert lens["persisted_to_close"] == 1  # 只有 COLD'a' 收盘仍正
    assert lens["persist_rate"] == 0.5
    assert lens["late_only_positive"] == 1  # LATE'a' 收盘才转正
    assert lens["mean_close_ev_of_those"] < lens["mean_early_ev_of_positive"]  # 侵蚀


def test_report_shape_stages_and_determinism(db) -> None:
    _seed(db)
    report = pool_replay.pool_replay_report(
        db, competitions=("E0",), seasons=("2324", "2425")
    )
    assert set(report) >= {
        "params",
        "caliber",
        "coverage",
        "overall",
        "by_season",
        "by_competition",
        "realized_calibration",
        "early_lens",
        "degenerate_note",
    }
    # 分期不混算：psc/avgc 各自独立统计
    assert set(report["overall"]) == {"psc", "avgc"}
    assert report["overall"]["psc"]["candidates"] == 6
    assert report["overall"]["psc"]["pool_ev_positive"] == 2
    assert report["overall"]["psc"]["pool_breakeven_ratio"] == 1.5385
    assert report["overall"]["avgc"]["pool_ev_positive"] == 0
    assert set(report["by_season"]["2425"]) == {"psc", "avgc"}
    assert set(report["by_season"]["2324"]) == {"psc"}
    # 实测校准：psc 候选池命中 2/6（COLD'a' 与 LATE'a'）
    pooled = report["realized_calibration"]["psc"]["pooled"]
    assert pooled["n"] == 6
    assert pooled["hit_rate"] == 0.3333
    # 确定性：同参数重跑逐字节一致（验收）
    again = pool_replay.pool_replay_report(
        db, competitions=("E0",), seasons=("2324", "2425")
    )
    assert json.dumps(again, sort_keys=False) == json.dumps(report, sort_keys=False)
