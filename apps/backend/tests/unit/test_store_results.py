"""事实域仓储测试。"""

from __future__ import annotations

import sqlite3

from goalx_backend.models import DrawResultInput
from goalx_backend.store import fixtures as fx
from goalx_backend.store import results as rs


def seed_fixtures(db: sqlite3.Connection, count: int) -> list[int]:
    """插入 count 场比赛，返回 fixture id 列表。"""
    comp = fx.upsert_competition(db, "英超")
    home = fx.upsert_team(db, "阿森纳")
    away = fx.upsert_team(db, "切尔西")
    return [
        fx.upsert_fixture(db, comp, f"2026-09-1{day}T10:00:00+00:00", home, away)
        for day in range(count)
    ]


def test_draw_result_upsert(db) -> None:
    fixture = seed_fixtures(db, 1)[0]
    first = rs.upsert_draw_result(
        db,
        DrawResultInput(
            fixture_id=fixture, home_goals=2, away_goals=1, source="official"
        ),
    )
    second = rs.upsert_draw_result(  # 修正覆盖
        db,
        DrawResultInput(
            fixture_id=fixture,
            home_goals=2,
            away_goals=2,
            source="official",
            correction_reason="official correction",
        ),
    )
    assert second == first
    row = rs.get_draw_result(db, fixture)
    assert row is not None
    assert (row["home_goals"], row["away_goals"]) == (2, 2)
    assert rs.get_draw_result(db, 999) is None


def test_draw_results_bulk(db) -> None:
    one, two = seed_fixtures(db, 2)
    rs.upsert_draw_result(
        db, DrawResultInput(fixture_id=one, home_goals=1, away_goals=0)
    )
    rs.upsert_draw_result(
        db,
        DrawResultInput(
            fixture_id=two, home_goals=0, away_goals=0, void=True, void_reason="腰斩"
        ),
    )
    got = rs.draw_results_for_fixtures(db, [one, two, 999])
    assert set(got) == {one, two}
    assert got[two]["void"] == 1


def test_hist_upsert_idempotent(db) -> None:
    row = {
        "competition": "E0",
        "season": "2425",
        "match_date": "2024-08-16",
        "home_team": "Man United",
        "away_team": "Fulham",
        "fthg": 1,
        "ftag": 0,
        "ftr": "H",
        "psc_home": 2.05,
        "psc_draw": 3.6,
        "psc_away": 4.1,
        "avgc_home": 2.1,
        "avgc_draw": 3.5,
        "avgc_away": 4.0,
    }
    assert rs.upsert_hist_matches(db, [row]) == 1
    assert rs.upsert_hist_matches(db, [dict(row, fthg=2)]) == 1  # 幂等覆盖
    stats = rs.hist_match_stats(db)
    assert stats == {"total": 1, "psc_present": 1, "avgc_present": 1}


def test_cost_ledger_credit_usage(db) -> None:
    rs.record_cost(
        db, "odds_api_credit", units=1.0, occurred_at="2026-09-01T00:00:00+00:00"
    )
    rs.record_cost(
        db, "odds_api_credit", units=2.0, occurred_at="2026-09-12T08:00:00+00:00"
    )
    rs.record_cost(db, "llm_call", units=1.0, amount_cny=0.02)
    assert rs.credit_usage(db, "2026-09-10T00:00:00+00:00") == 2.0
    assert rs.credit_usage(db, "2026-08-01T00:00:00+00:00") == 3.0
