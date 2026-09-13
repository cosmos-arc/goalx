"""赛程/市场域仓储测试。"""

from __future__ import annotations

import sqlite3
from typing import Any

from goalx_backend.data import fixtures as fx
from goalx_backend.models import MatchCodeInput, SnapshotInput, Tier


def seed_fixture(
    db: sqlite3.Connection, kickoff: str = "2026-09-13T10:00:00+00:00"
) -> int:
    """插入一场比赛的全部关联实体，返回 fixture id。"""
    comp = fx.upsert_competition(
        db, "英超", tier=Tier.TIER1, odds_api_sport_key="soccer_epl"
    )
    home = fx.upsert_team(db, "阿森纳")
    away = fx.upsert_team(db, "切尔西")
    return fx.upsert_fixture(db, comp, kickoff, home, away)


def match_code(fixture: int, **overrides: object) -> MatchCodeInput:
    """构造竞彩销售编号输入。"""
    base: dict[str, Any] = {
        "fixture_id": fixture,
        "kind": "jingcai",
        "business_date": "2026-09-12",
        "code": "周六026",
        "source_match_id": "2041430",
        "is_single": False,
    }
    base.update(overrides)
    return MatchCodeInput(**base)


def snap(fixture: int, **overrides: object) -> SnapshotInput:
    """构造赔率快照输入。"""
    base: dict[str, Any] = {
        "fixture_id": fixture,
        "market_code": "had",
        "selection_code": "h",
        "source": "sporttery",
        "odds": 6.60,
        "captured_at": "2026-09-12T14:29:36+00:00",
    }
    base.update(overrides)
    return SnapshotInput(**base)


def test_upsert_fixture_idempotent(db: sqlite3.Connection) -> None:
    first = seed_fixture(db)
    second = seed_fixture(db)
    assert first == second


def test_team_alias_roundtrip(db: sqlite3.Connection) -> None:
    team = fx.upsert_team(db, "阿森纳")
    fx.upsert_team_alias(db, team, "odds_api", "Arsenal")
    fx.upsert_team_alias(db, team, "odds_api", "Arsenal")  # 幂等
    assert fx.resolve_team(db, "odds_api", "Arsenal") == team
    assert fx.resolve_team(db, "odds_api", "Nope") is None


def test_match_code_and_source_lookup(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    fx.upsert_match_code(db, match_code(fixture))
    row = fx.find_fixture_by_source_match(db, "jingcai", "2041430")
    assert row is not None
    assert row["id"] == fixture


def test_snapshot_append_only_and_dedupe(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    first = fx.insert_odds_snapshot(db, snap(fixture))
    dup = fx.insert_odds_snapshot(db, snap(fixture))
    assert first is not None
    assert first > 0
    assert dup is None  # 同自然键重复采集被吸收（append-only）
    fx.insert_odds_snapshot(
        db, snap(fixture, odds=6.40, captured_at="2026-09-12T16:00:00+00:00")
    )
    latest = fx.latest_odds_by_selection(db, fixture, "had", "sporttery")
    assert latest["h"] == (6.40, "2026-09-12T16:00:00+00:00")
    assert len(fx.odds_history(db, fixture, "had")) == 2


def test_snapshot_persisted_meta(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    fx.insert_odds_snapshot(
        db,
        snap(
            fixture,
            market_code="hhad",
            odds=2.95,
            captured_at="2026-09-12T14:00:00+00:00",
            meta={"goal_line": "+1"},
        ),
    )
    row = fx.odds_history(db, fixture, "hhad")[0]
    assert row["meta"] == '{"goal_line": "+1"}'
    assert row["purpose"] == "live_capture"


def test_eu_book_odds_latest_per_book(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    for book, odds in (("odds_api:pinnacle", 1.85), ("odds_api:bet365", 1.90)):
        fx.insert_odds_snapshot(
            db,
            snap(
                fixture,
                selection_code="a",
                source=book,
                odds=odds,
                captured_at="2026-09-12T15:00:00+00:00",
            ),
        )
    # pinnacle 后续调价
    fx.insert_odds_snapshot(
        db,
        snap(
            fixture,
            selection_code="a",
            source="odds_api:pinnacle",
            odds=1.80,
            captured_at="2026-09-12T16:00:00+00:00",
        ),
    )
    books = fx.eu_book_odds(db, fixture)
    assert books["a"] == {"odds_api:pinnacle": 1.80, "odds_api:bet365": 1.90}


def test_fixtures_for_business_date(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    fx.upsert_match_code(db, match_code(fixture))
    rows = fx.fixtures_for_business_date(db, "2026-09-12")
    assert len(rows) == 1
    assert rows[0]["match_code"] == "周六026"
    assert rows[0]["competition_name"] == "英超"
    assert rows[0]["home_team"] == "阿森纳"
    assert fx.fixtures_for_business_date(db, "2026-09-13") == []


def test_odds_api_join_persisted(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    fx.set_odds_api_join(db, fixture, "evt123", "soccer_epl", "time_window")
    row = fx.get_fixture(db, fixture)
    assert row is not None
    assert row["odds_api_event_id"] == "evt123"
    assert row["join_method"] == "time_window"
    fx.unset_odds_api_join(db, fixture)
    row = fx.get_fixture(db, fixture)
    assert row is not None
    assert row["odds_api_event_id"] is None


def test_pending_jingcai_fixtures(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db, kickoff="2026-09-13T10:00:00+00:00")
    fx.upsert_match_code(db, match_code(fixture, source_match_id="1"))
    pending = fx.pending_jingcai_fixtures(db, "2026-09-13T12:00:00+00:00")
    assert [row["id"] for row in pending] == [fixture]
    assert fx.pending_jingcai_fixtures(db, "2026-09-13T09:00:00+00:00") == []
