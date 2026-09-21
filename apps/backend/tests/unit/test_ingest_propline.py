"""PropLine 采集、解析映射与请求预算测试（票 50；数据形状取自实测响应）。"""

from __future__ import annotations

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data import results as rs_store
from goalx_backend.data.ingest import propline, sporttery

# 实测形状（2026-09-21 UCL 抓包）：美式赔率、outcome 队名不归一、
# last_update 为 ISO 含微秒、sport_key 回显请求别名。
EVENTS = [
    {
        "id": "212101",
        "sport_key": "soccer_uefa_champions_league",
        "commence_time": "2026-09-12T18:00:00Z",
        "home_team": "FC Sabah",
        "away_team": "Slavia Prague",
        "last_update": "2026-09-21T05:22:18.570206Z",
        "bookmakers": [
            {  # pinnacle：Sabah FK 归一化等值 home，Slavia Prague 精确 away
                "key": "pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Sabah FK", "price": 261},
                            {"name": "Draw", "price": 274},
                            {"name": "Slavia Prague", "price": -103},
                        ],
                    }
                ],
            },
            {  # betus：Sabah Baku / Slavia Praha 两名皆不可判 → 整书丢弃
                "key": "betus",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Sabah Baku", "price": 250},
                            {"name": "Slavia Praha", "price": -110},
                            {"name": "Draw", "price": 280},
                        ],
                    }
                ],
            },
            {  # smarkets：a+d 已定，Sabah Baku 由消去法定 h
                "key": "smarkets",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Draw", "price": 270},
                            {"name": "Slavia Prague", "price": -105},
                            {"name": "Sabah Baku", "price": 255},
                        ],
                    }
                ],
            },
            {  # onexbet：缺 Draw → 三向不齐丢弃（票 35）
                "key": "onexbet",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Sabah FK", "price": 250},
                            {"name": "Slavia Prague", "price": -105},
                        ],
                    }
                ],
            },
        ],
    }
]


def test_american_to_decimal() -> None:
    assert propline.american_to_decimal(261) == 3.61
    assert propline.american_to_decimal(-103) == 1.9709
    assert propline.american_to_decimal(100) == 2.0
    assert propline.american_to_decimal(-100) == 2.0
    assert propline.american_to_decimal(0) is None


def test_normalize_name_strips_club_tokens_and_accents() -> None:
    assert propline._normalize_name("FC Sabah") == "sabah"
    assert propline._normalize_name("Sabah FK") == "sabah"
    assert propline._normalize_name("Málaga") == "malaga"
    assert propline._normalize_name("Slavia Praha") == "slavia praha"


def test_parse_events_maps_prices_and_drops_unmappable_books() -> None:
    (event,) = propline.parse_events("soccer_uefa_champions_league", EVENTS)
    assert event.books["pinnacle"] == {"h": 3.61, "d": 3.74, "a": 1.9709}
    assert event.books["smarkets"] == {"h": 3.55, "d": 3.7, "a": 1.9524}
    assert "betus" not in event.books  # 两名皆不可判
    assert "onexbet" not in event.books  # 缺 Draw
    # ISO 源时间含微秒 → 秒级；sport_key 回显别名
    assert event.last_update_utc == "2026-09-21T05:22:18+00:00"
    assert event.sport_key == "soccer_uefa_champions_league"


def test_parse_events_missing_last_update_is_none() -> None:
    stripped = [dict(EVENTS[0], last_update=None)]
    (no_ts,) = propline.parse_events("soccer_epl", stripped)
    assert no_ts.last_update_utc is None


def test_duplicate_side_mapping_conflict_drops_book() -> None:
    """两 outcome 归一化后同侧（同队名重复）→ 整书不可信丢弃。"""
    event = dict(
        EVENTS[0],
        bookmakers=[
            {
                "key": "weird",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Sabah FK", "price": 250},
                            {"name": "FC Sabah", "price": 255},
                            {"name": "Slavia Prague", "price": -105},
                        ],
                    }
                ],
            }
        ],
    )
    assert "weird" not in propline._outcome_prices(event)


def seed_jingcai(db) -> int:
    """入库一场欧冠竞彩（kickoff 与 EVENTS[0] 一致）。"""
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
                            "leagueAbbName": "欧冠",
                            "homeTeamAllName": "萨巴赫",
                            "awayTeamAllName": "布拉格斯拉维亚",
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
    row = db.execute("SELECT id, competition_id FROM fixtures").fetchone()
    db.execute(
        "UPDATE competitions SET odds_api_sport_key = 'soccer_uefa_champions_league'"
        " WHERE id = ?",
        (row["competition_id"],),
    )
    db.commit()
    return int(row["id"])


def test_join_writes_propline_columns_and_keeps_oddsapi_free(db) -> None:
    fixture = seed_jingcai(db)
    events = propline.parse_events("soccer_uefa_champions_league", EVENTS)
    report = propline.join_fixtures(db, events)
    assert report.joined == 1
    row = db.execute("SELECT * FROM fixtures WHERE id = ?", (fixture,)).fetchone()
    assert row["propline_event_id"] == "212101"
    assert row["propline_sport_key"] == "soccer_uefa_champions_league"
    # 互备独立性：oddsapi join 列不受影响
    assert row["odds_api_event_id"] is None
    # join 命中回填 propline 侧别名（源标签区分 provenance）
    aliases = {
        str(r["source"])
        for r in db.execute("SELECT DISTINCT source FROM team_aliases").fetchall()
    }
    assert "propline" in aliases
    # 入库快照 source 带书商标签
    stats = propline.store_events(db, events)
    assert stats.snapshots == 6  # 2 books × (h,d,a)
    sources = {
        str(r["source"])
        for r in db.execute("SELECT DISTINCT source FROM odds_snapshots").fetchall()
    }
    # 竞彩种子自身带 sporttery 快照；此处只验 propline 侧
    assert {"propline:pinnacle", "propline:smarkets"} <= sources


def test_join_rejects_home_away_swap(db) -> None:
    seed_jingcai(db)
    row = db.execute("SELECT home_team_id, away_team_id FROM fixtures").fetchone()
    pairs = (
        (row["home_team_id"], "FC Sabah"),
        (row["away_team_id"], "Slavia Prague"),
    )
    for team_id, alias in pairs:
        db.execute(
            "INSERT OR IGNORE INTO team_aliases (team_id, source, alias)"
            " VALUES (?, 'odds_api', ?)",
            (team_id, alias),
        )
    db.commit()
    swapped = dict(
        EVENTS[0],
        home_team="Slavia Prague",
        away_team="FC Sabah",
        bookmakers=[EVENTS[0]["bookmakers"][0]],
    )
    report = propline.join_fixtures(
        db, propline.parse_events("soccer_uefa_champions_league", [swapped])
    )
    assert report.unmatched[0]["reason"] == "team_swap_mismatch"
    assert db.execute("SELECT propline_event_id FROM fixtures").fetchone()[0] is None


def test_discover_sport_keys_from_competitions_and_freeze(db) -> None:
    seed_jingcai(db)
    settings = Settings()
    assert propline.discover_sport_keys(db, settings) == [
        "soccer_uefa_champions_league"
    ]
    frozen = Settings(propline_sport_scope="soccer_epl, soccer_italy_serie_a")
    assert propline.discover_sport_keys(db, frozen) == [
        "soccer_epl",
        "soccer_italy_serie_a",
    ]


def test_request_budget_guard(db) -> None:
    settings = Settings(propline_daily_request_budget=2)
    propline.check_request_budget(db, settings)  # 未超
    propline.record_request(db, "test")
    propline.record_request(db, "test")
    with pytest.raises(propline.ProplineBudgetExceeded, match="daily"):
        propline.check_request_budget(db, settings)
    row = db.execute(
        "SELECT SUM(units) AS u FROM cost_ledger WHERE category = 'propline_request'"
    ).fetchone()
    assert row["u"] == 2.0


def test_unknown_sport_degrades_without_raising(db) -> None:
    seed_jingcai(db)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-API-Key") == "k"
        return httpx.Response(404, json={"detail": {"error": "unknown_sport"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = propline.fetch_and_store_odds(db, Settings(propline_api_key="k"), client)
    assert stats.unavailable_sports == ["soccer_uefa_champions_league"]
    assert stats.ingest.snapshots == 0
    # 404 也记账（保守：到达服务端即 1 请求）
    assert rs_store.credit_usage(db, "2000-01-01") == 0.0
    row = db.execute(
        "SELECT SUM(units) AS u FROM cost_ledger WHERE category = 'propline_request'"
    ).fetchone()
    assert row["u"] == 1.0


def test_daily_remaining_stops_early(db) -> None:
    seed_jingcai(db)
    db.execute(
        "INSERT INTO competitions (name, tier, odds_api_sport_key, created_at)"
        " VALUES ('英超', 'tier1', 'soccer_epl', '2026-09-21T00:00:00+00:00')"
    )
    db.commit()
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url.path))
        return httpx.Response(200, json=[], headers={"X-Daily-Remaining": "5"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = propline.fetch_and_store_odds(db, Settings(propline_api_key="k"), client)
    assert stats.daily_remaining == 5
    # 5 < floor 20 → 第二个 sport 不再请求
    assert len(fetched) == 1


def test_provider_daily_limit_raises_budget_exceeded(db) -> None:
    seed_jingcai(db)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"detail": {"error": "daily_limit_exceeded"}},
            headers={"Retry-After": "3600"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(propline.ProplineBudgetExceeded, match="daily limit"):
        propline.fetch_and_store_odds(db, Settings(propline_api_key="k"), client)


def test_fetch_and_store_end_to_end(db) -> None:
    seed_jingcai(db)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-API-Key") == "k"
        assert "markets=h2h" in str(request.url)
        return httpx.Response(200, json=EVENTS, headers={"X-Daily-Remaining": "900"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = propline.fetch_and_store_odds(db, Settings(propline_api_key="k"), client)
    assert stats.requests_used == 1
    assert stats.ingest.credits_used == 1
    assert stats.ingest.events == 1
    assert stats.ingest.snapshots == 6  # join 后 2 books × 3 向
    assert stats.daily_remaining == 900
    # 观测证据行落在 propline 源
    obs = db.execute(
        "SELECT source, endpoint, parse_version FROM quote_observations"
    ).fetchone()
    assert obs["source"] == "propline"
    assert obs["endpoint"] == "/sports/soccer_uefa_champions_league/odds"
    assert obs["parse_version"] == "propline_h2h_v1"
    # captured_at = 源 last_update（票 35 三时间语义）
    snap = db.execute(
        "SELECT captured_at, source_updated_at FROM odds_snapshots"
        " WHERE source = 'propline:pinnacle'"
    ).fetchone()
    assert snap["captured_at"] == "2026-09-21T05:22:18+00:00"
    assert snap["source_updated_at"] == "2026-09-21T05:22:18+00:00"
