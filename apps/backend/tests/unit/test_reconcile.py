"""
对账引擎测试（票 44）：参照源 vs draw_results 的比较纯函数 + run 元信息
append-only + coverage 维表语义。

比较矩阵覆盖：一致/全场不一致/半场宽严/void 双向不一致/终态缺事实/
参照未终态跳过。fixture 级数据用 fx_store helpers 构造。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from goalx_backend.data import reconcile
from goalx_backend.data.fixtures import (
    upsert_competition,
    upsert_fixture,
    upsert_team,
)
from goalx_backend.data.ingest.results import import_draw_results
from goalx_backend.data.reconcile import (
    ReconcileStats,
    ReferenceResult,
    reconcile_draw_results,
    record_reconciliation_run,
    upsert_source_coverage,
)
from goalx_backend.models import DrawResultInput


def _stats() -> ReconcileStats:
    return ReconcileStats(source="test", observed_at="2026-09-21T00:30:00+00:00")


def _ref(fixture_id: int, **kwargs: object) -> ReferenceResult:
    fields: dict[str, object] = {
        "fixture_id": fixture_id,
        "business_date": "2026-09-20",
        "code": "周日001",
        "home_goals": 2,
        "away_goals": 1,
    }
    fields.update(kwargs)
    return ReferenceResult(**fields)  # type: ignore[arg-type]


def _seed_fixture(db, kickoff: str = "2026-09-20T19:00:00+00:00") -> int:
    competition = upsert_competition(db, "英超")
    home = upsert_team(db, "阿森纳")
    away = upsert_team(db, "考文垂")
    return upsert_fixture(db, competition, kickoff, home, away)


def _store(db, fixture_id: int, **kwargs: object) -> None:
    fields: dict[str, object] = {
        "fixture_id": fixture_id,
        "home_goals": 2,
        "away_goals": 1,
    }
    fields.update(kwargs)
    import_draw_results(db, [DrawResultInput(**fields)])  # type: ignore[arg-type]


# --- 比较矩阵（纯函数） ---


def test_consistent_full_and_half(db) -> None:
    fid = _seed_fixture(db)
    _store(db, fid, half_home_goals=1, half_away_goals=0)
    stats = reconcile_draw_results(
        db, [_ref(fid, half_home_goals=1, half_away_goals=0)], _stats()
    )
    assert stats.compared == 1
    assert stats.consistent == 1
    assert stats.pending_manual == []


def test_score_mismatch_lists_both_sides(db) -> None:
    fid = _seed_fixture(db)
    _store(db, fid, home_goals=0)
    stats = reconcile_draw_results(db, [_ref(fid)], _stats())
    assert stats.score_mismatch == 1
    entry = stats.pending_manual[0]
    assert entry["reason"] == "score_mismatch"
    assert "2:1" in entry["detail"]
    assert "0:1" in entry["detail"]


def test_half_lenient_when_either_side_missing(db) -> None:
    fid = _seed_fixture(db)
    _store(db, fid)  # 库内无半场
    stats = reconcile_draw_results(db, [_ref(fid, half_home_goals=1)], _stats())
    assert stats.consistent == 1


def test_half_mismatch_when_both_present(db) -> None:
    fid = _seed_fixture(db)
    _store(db, fid, half_home_goals=0, half_away_goals=0)
    stats = reconcile_draw_results(
        db, [_ref(fid, half_home_goals=1, half_away_goals=0)], _stats()
    )
    assert stats.score_mismatch == 1
    assert stats.pending_manual[0]["reason"] == "half_score_mismatch"


def test_reference_void_vs_stored_result(db) -> None:
    fid = _seed_fixture(db)
    _store(db, fid)
    stats = reconcile_draw_results(
        db,
        [
            _ref(
                fid, home_goals=None, away_goals=None, void=True, void_reason="官方取消"
            )
        ],
        _stats(),
    )
    assert stats.void_mismatch == 1
    assert stats.pending_manual[0]["reason"] == "stored_result_vs_reference_void"


def test_void_both_sides_consistent(db) -> None:
    fid = _seed_fixture(db)
    _store(db, fid, home_goals=0, away_goals=0, void=True, void_reason="官方取消")
    stats = reconcile_draw_results(
        db, [_ref(fid, home_goals=None, away_goals=None, void=True)], _stats()
    )
    assert stats.consistent == 1


def test_stored_void_vs_reference_result(db) -> None:
    fid = _seed_fixture(db)
    _store(db, fid, home_goals=0, away_goals=0, void=True, void_reason="腰斩")
    stats = reconcile_draw_results(db, [_ref(fid)], _stats())
    assert stats.void_mismatch == 1
    assert stats.pending_manual[0]["reason"] == "stored_void_vs_reference_result"


def test_reference_final_missing_fact(db) -> None:
    fid = _seed_fixture(db)
    stats = reconcile_draw_results(db, [_ref(fid)], _stats())
    assert stats.missing_result == 1
    assert stats.pending_manual[0]["reason"] == "reference_final_missing_fact"


def test_reference_void_missing_fact(db) -> None:
    fid = _seed_fixture(db)
    stats = reconcile_draw_results(
        db, [_ref(fid, home_goals=None, away_goals=None, void=True)], _stats()
    )
    assert stats.missing_result == 1
    assert stats.pending_manual[0]["reason"] == "reference_void_missing_fact"


def test_not_final_reference_skipped(db) -> None:
    fid = _seed_fixture(db)
    _store(db, fid)
    stats = reconcile_draw_results(
        db, [_ref(fid, home_goals=None, away_goals=None)], _stats()
    )
    assert stats.compared == 0
    assert stats.pending_manual == []


# --- run 元信息（append-only）---


def test_reconciliation_run_append_only(db) -> None:
    stats = _stats()
    stats.business_dates = ["2026-09-20"]
    record_reconciliation_run(db, stats)
    row = reconcile.latest_reconciliation(db, "test")
    assert row is not None
    assert json.loads(row["business_dates"]) == ["2026-09-20"]
    assert reconcile.latest_reconciliation(db, "other") is None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE draw_reconciliation_runs SET compared = 99")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM draw_reconciliation_runs")


# --- coverage 维表（空≠无）---


def test_coverage_upsert_refreshes_state(db) -> None:
    upsert_source_coverage(
        db,
        source="500.com",
        coverage_date="2026-09-20",
        match_count=10,
        coverage_status="covered",
        observed_at="2026-09-20T01:00:00+00:00",
    )
    upsert_source_coverage(
        db,
        source="500.com",
        coverage_date="2026-09-20",
        match_count=12,
        coverage_status="covered",
        observed_at="2026-09-20T02:00:00+00:00",
    )
    upsert_source_coverage(
        db,
        source="500.com",
        coverage_date="2026-09-20",
        match_count=0,
        coverage_status="fetched_empty",
        observed_at="2026-09-20T03:00:00+00:00",
        league_key="32",
    )
    rows = db.execute(
        "SELECT league_key, match_count, coverage_status FROM source_coverage"
        " WHERE source = '500.com' ORDER BY league_key"
    ).fetchall()
    # 同键刷新现态（一行），异键新增
    assert [(r["league_key"], r["match_count"]) for r in rows] == [("", 12), ("32", 0)]
    assert rows[0]["coverage_status"] == "covered"
    assert rows[1]["coverage_status"] == "fetched_empty"
