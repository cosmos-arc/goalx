"""源T 日页赛果物化测试（票 76：零请求主源、差集口径、覆盖报告）。"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import mapping
from goalx_backend.data import results as rs_store
from goalx_backend.data.ingest import srct_results
from goalx_backend.models import DrawResultInput, Tier

NOW = "2026-09-27T05:00:00+00:00"
KICKOFF_PAST = "2026-09-26T19:00:00+00:00"  # 北京 09-27 03:00（已开赛）


def seed_universe(
    rows: list[tuple[str, int | None, int | None]],
) -> duckdb.DuckDBPyConnection:
    """内存 duckdb 假 fixture_universe（物化只需 sid+比分）。"""
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE fixture_universe"
        " (sid VARCHAR, home_goals SMALLINT, away_goals SMALLINT)"
    )
    for sid, home_goals, away_goals in rows:
        con.execute(
            "INSERT INTO fixture_universe VALUES (?, ?, ?)",
            [sid, home_goals, away_goals],
        )
    return con


def seed_pending_fixture(
    db,
    *,
    home: str = "主队英超",
    away: str = "客队英超",
    kickoff: str = KICKOFF_PAST,
    league: str = "英超",
    tier: Tier = Tier.TIER1,
    sid: str | None = "2790001",
) -> int:
    """已开赛无开奖的竞彩场；sid 给定则落映射链（票 77 表）。"""
    competition = fx_store.upsert_competition(db, league, tier=tier)
    home_id = fx_store.upsert_team(db, home)
    away_id = fx_store.upsert_team(db, away)
    fixture_id = fx_store.upsert_fixture(db, competition, kickoff, home_id, away_id)
    if sid is not None:
        db.execute(
            """
            INSERT INTO source_match_links
                (fixture_id, source, source_match_id, method, status, meta, mapped_at)
            VALUES (?, 'srct', ?, 'primary_key', 'linked', '{}', '2026-09-27T00:00:00Z')
            """,
            (fixture_id, sid),
        )
        db.commit()
    return fixture_id


_NOW = datetime.fromisoformat(NOW)  # 固定钟：窗口测试不随墙钟腐烂


def test_materialize_imports_full_time_score(db) -> None:
    """已链+universe 有比分 → 落事实（source=srct，half 空、published 不伪造）。"""
    fixture_id = seed_pending_fixture(db)
    duck_con = seed_universe([("2790001", 2, 1)])
    stats = srct_results.materialize_results(db, duck_con, now=_NOW)
    assert stats.pending == 1
    assert stats.unmapped == 0
    assert stats.imported == 1
    stored = rs_store.get_draw_result(db, fixture_id)
    assert (stored["home_goals"], stored["away_goals"]) == (2, 1)
    assert stored["source"] == "srct"
    assert stored["half_home_goals"] is None
    assert stored["published_at"] is None


def test_materialize_no_overwrite_of_stored_result(db) -> None:
    """库内已有结果不冲正（no-冲正规则沿袭；uniform 事实不被源T 覆盖）。"""
    fixture_id = seed_pending_fixture(db)
    db.execute("DELETE FROM draw_results WHERE fixture_id = ?", (fixture_id,))
    rs_store.upsert_draw_result(
        db,
        DrawResultInput(
            fixture_id=fixture_id, home_goals=3, away_goals=3, source="uniform"
        ),
    )
    # 已有结果 → 不再进待出窗口
    stats = srct_results.materialize_results(
        db, seed_universe([("2790001", 2, 1)]), now=_NOW
    )
    assert stats.pending == 0
    stored = rs_store.get_draw_result(db, fixture_id)
    assert (stored["home_goals"], stored["away_goals"]) == (3, 3)


def test_materialize_buckets_diffset_and_awaiting(db) -> None:
    """差集（无链）不物化；已链无比分（silver 滞后）计 awaiting 不硬导。"""
    seed_pending_fixture(db, home="差集队", sid=None)
    seed_pending_fixture(db, home="滞后队", away="客队二", sid="2790099")
    duck_con = seed_universe([("2790001", 1, 0), ("2790001", 1, 0)])
    stats = srct_results.materialize_results(db, duck_con, now=_NOW)
    assert stats.pending == 2
    assert stats.unmapped == 1  # 差集队：uniform 兜底口径
    assert stats.awaiting_universe == 1  # 滞后队：universe 无行
    assert stats.imported == 0


def test_materialize_degraded_without_duck(db) -> None:
    """语料桥缺席 → 降级零动作（uniform 独走，票 44 行为不变）。"""
    seed_pending_fixture(db)
    stats = srct_results.materialize_results(db, None, now=_NOW)
    assert stats.degraded is not None
    assert stats.imported == 0


def test_materialize_future_fixture_not_pending(db) -> None:
    """未开赛场不进待出窗口（防前视：无赛果可物化）。"""
    seed_pending_fixture(db, kickoff="2026-09-28T19:00:00+00:00", sid=None)
    stats = srct_results.materialize_results(
        db, seed_universe([]), now=datetime(2026, 9, 27, 5, tzinfo=UTC)
    )
    assert stats.pending == 0


def test_coverage_diff_report_buckets(db) -> None:
    """覆盖差集报告：按联赛分桶 covered/awaiting/diffset（第一验收项）。"""
    seed_pending_fixture(db, home="覆盖队")  # linked+goals
    seed_pending_fixture(db, home="滞后队", away="客队二", sid="2790099")
    seed_pending_fixture(db, home="差集队", away="客队三", sid=None, league="日职")
    duck_con = seed_universe([("2790001", 2, 0)])
    report = srct_results.coverage_diff_report(db, duck_con, now=_NOW)
    assert report["pending"] == 3
    assert report["srct_covered"] == 1
    assert report["diffset_unmapped"] == 1
    assert report["per_competition"]["英超"]["covered"] == 1
    assert report["per_competition"]["英超"]["awaiting"] == 1
    assert report["per_competition"]["日职"]["diffset"] == 1


def test_coverage_report_degraded_honest(db) -> None:
    """语料桥缺席 → linked 维度诚实降 awaiting（不伪装已覆盖）。"""
    seed_pending_fixture(db)
    report = srct_results.coverage_diff_report(db, None, now=_NOW)
    assert report["universe_degraded"] is True
    assert report["per_competition"]["英超"]["awaiting"] == 1


def test_pending_query_windows_and_link_join(db) -> None:
    """mapping.pending_srct_results：窗口过滤（近 7 天已开赛）+ 链左连。"""
    seed_pending_fixture(db)
    rows = mapping.pending_srct_results(db, NOW)
    assert len(rows) == 1
    assert rows[0]["sid"] == "2790001"
    assert rows[0]["competition_name"] == "英超"
    stale = mapping.pending_srct_results(db, "2026-10-10T05:00:00+00:00")  # 窗口滑出
    assert stale == []
