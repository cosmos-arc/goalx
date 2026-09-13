"""The Odds API 采集、join 映射与 credit 护栏测试。"""

from __future__ import annotations

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.ingest import oddsapi, sporttery
from goalx_backend.store import results as rs_store

SPORTS = [
    {"key": "soccer_epl", "group": "Soccer", "title": "EPL"},
    {"key": "soccer_epl_winner", "group": "Soccer", "title": "EPL Winner"},
    {"key": "soccer_uefa_champs_league", "group": "Soccer", "title": "UCL"},
    {"key": "basketball_nba", "group": "Basketball", "title": "NBA"},
]

EVENTS = [
    {
        "id": "abc123",
        "sport_key": "soccer_epl",
        "commence_time": "2026-09-12T18:00:00Z",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "bookmakers": [
            {
                "key": "pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Arsenal", "price": 1.85},
                            {"name": "Draw", "price": 3.6},
                            {"name": "Chelsea", "price": 4.2},
                        ],
                    }
                ],
            },
            {
                "key": "bet365",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Arsenal", "price": 1.9},
                            {"name": "Chelsea", "price": 4.0},
                        ],
                    }
                ],
            },
        ],
    }
]


def test_discover_sport_keys_filters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "apiKey" in str(request.url)
        return httpx.Response(200, json=SPORTS)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    keys = oddsapi.discover_sport_keys(Settings(odds_api_key="k"), client)
    assert keys == ["soccer_epl", "soccer_uefa_champs_league"]


def test_parse_events_maps_home_away_draw() -> None:
    events = oddsapi.parse_events("soccer_epl", EVENTS)
    assert len(events) == 1
    event = events[0]
    assert event.books["pinnacle"] == {"h": 1.85, "d": 3.6, "a": 4.2}
    assert event.books["bet365"] == {"h": 1.9, "a": 4.0}  # 无 Draw 容忍


def seed_jingcai(db) -> int:
    """入库一场英超竞彩（kickoff 与 EVENTS[0] 一致）。"""
    payload = {
        "errorCode": "0",
        "value": {
            "matchInfoList": [
                {
                    "businessDate": "2026-09-12",
                    "subMatchList": [
                        {
                            "matchId": 1,
                            "matchNumStr": "周六001",
                            "leagueAbbName": "英超",
                            "homeTeamAllName": "阿森纳",
                            "awayTeamAllName": "切尔西",
                            "matchDate": "2026-09-13",
                            "matchTime": "02:00:00",
                            "bettingSingle": 1,
                            "had": {
                                "a": "1.30",
                                "d": "5.0",
                                "h": "6.5",
                                "updateDate": "2026-09-12",
                                "updateTime": "20:00:00",
                            },
                        }
                    ],
                }
            ]
        },
    }
    sporttery.store_matches(db, sporttery.parse_matches(payload))
    row = db.execute("SELECT id FROM fixtures").fetchone()
    return int(row["id"])


def test_join_fixtures_time_window(db) -> None:
    fixture = seed_jingcai(db)
    events = oddsapi.parse_events("soccer_epl", EVENTS)
    report = oddsapi.join_fixtures(db, events)
    assert report.joined == 1
    assert report.tier1_joined == 1
    assert report.tier1_total == 1
    row = db.execute("SELECT * FROM fixtures WHERE id = ?", (fixture,)).fetchone()
    assert row["odds_api_event_id"] == "abc123"
    assert row["join_method"] == "time_window"
    # join 后 store_events 落快照
    stats = oddsapi.store_events(db, events)
    assert stats.snapshots == 5  # 2 books × (h,d,a 缺 d 的只 2)
    assert stats.duplicate_snapshots == 0


def test_join_no_event_in_window_reports_gap(db) -> None:
    seed_jingcai(db)
    far_event = dict(EVENTS[0])
    far_event["commence_time"] = "2026-09-15T18:00:00Z"
    report = oddsapi.join_fixtures(db, oddsapi.parse_events("soccer_epl", [far_event]))
    assert report.joined == 0
    assert report.unmatched == [{"fixture_id": "1", "reason": "no_event_in_window"}]


def test_credit_budget_guard(db) -> None:
    settings = Settings(
        odds_api_daily_credit_budget=2, odds_api_monthly_credit_budget=100
    )
    oddsapi.check_credit_budget(db, settings)  # 未超
    oddsapi.record_credits(db, 2, "test")
    with pytest.raises(oddsapi.CreditBudgetExceeded, match="daily"):
        oddsapi.check_credit_budget(db, settings)


def test_credit_monthly_guard(db) -> None:
    settings = Settings(
        odds_api_daily_credit_budget=100, odds_api_monthly_credit_budget=2
    )
    oddsapi.record_credits(db, 2, "test")
    with pytest.raises(oddsapi.CreditBudgetExceeded, match="monthly"):
        oddsapi.check_credit_budget(db, settings)


def test_month_and_day_start_utc() -> None:
    from datetime import UTC, datetime

    moment = datetime(2026, 9, 13, 15, 30, tzinfo=UTC)
    assert oddsapi.month_start_utc(moment) == "2026-09-01T00:00:00+00:00"
    assert oddsapi.day_start_utc(moment) == "2026-09-13T00:00:00+00:00"


def test_fetch_and_store_end_to_end(db) -> None:
    seed_jingcai(db)
    settings = Settings(odds_api_key="k")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sports"):
            return httpx.Response(200, json=SPORTS[:1])
        if "odds" in request.url.path:
            assert "regions=eu" in str(request.url)
            return httpx.Response(200, json=EVENTS)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = oddsapi.fetch_and_store_odds(db, settings, client)
    assert stats.credits_used == 1
    assert stats.snapshots == 5
    assert rs_store.credit_usage(db, "2000-01-01T00:00:00+00:00") == 1.0


def test_join_ambiguous_not_persisted(db) -> None:
    """时间窗内两个同刻事件 → 歧义不落库，留给人工映射。"""
    seed_jingcai(db)
    twin = dict(EVENTS[0], id="evt2")
    report = oddsapi.join_fixtures(
        db, oddsapi.parse_events("soccer_epl", [EVENTS[0], twin])
    )
    assert report.joined == 0
    assert report.unmatched == [{"fixture_id": "1", "reason": "ambiguous_time_window"}]
    row = db.execute("SELECT odds_api_event_id FROM fixtures").fetchone()
    assert row["odds_api_event_id"] is None
