"""The Odds API 采集、join 映射与 credit 护栏测试。"""

from __future__ import annotations

import json

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs_store
from goalx_backend.data.ingest import oddsapi, sporttery

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
    # 票 35：同公司完整三向才可比较——缺 Draw 的 book 整体丢弃
    assert "bet365" not in event.books


def test_parse_events_keeps_source_update_time() -> None:
    from datetime import UTC, datetime

    ts = 1789000000
    expected = datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")
    event_with_ts = [dict(EVENTS[0], last_update=ts)]
    (event,) = oddsapi.parse_events("soccer_epl", event_with_ts)
    assert event.last_update_utc == expected
    # 缺失/非法源时间 → None（不伪造）
    assert oddsapi.parse_events("soccer_epl", EVENTS)[0].last_update_utc is None
    (bad,) = oddsapi.parse_events(
        "soccer_epl", [dict(EVENTS[0], last_update="not-a-ts")]
    )
    assert bad.last_update_utc is None


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
    assert stats.snapshots == 3  # 1 book × (h,d,a)；缺三向的 bet365 被丢弃
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
    assert stats.snapshots == 3
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


# --- 票 35：逐请求预算记账 / join 身份交叉核对 ---


def test_totals_market_rejected_without_paid_request(db) -> None:
    with pytest.raises(ValueError, match="h2h"):
        oddsapi.fetch_and_store_odds(
            db, Settings(odds_api_key="k"), _no_call_client(), markets=("h2h", "totals")
        )


def _no_call_client() -> httpx.Client:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发出任何付费请求")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_reserve_blocks_near_monthly_limit_conservatively(db) -> None:
    settings = Settings(
        odds_api_daily_credit_budget=100, odds_api_monthly_credit_budget=5
    )
    oddsapi.record_credits(db, 4, "test")
    # 临近月限额：再预留 2 会超 → 拒绝且不记账
    with pytest.raises(oddsapi.CreditBudgetExceeded, match="monthly"):
        oddsapi.reserve_credits(db, settings, 2, "near-limit")
    assert rs_store.credit_usage(db, "2000-01-01") == 4.0
    # 预留 1 不超 → 入账
    oddsapi.reserve_credits(db, settings, 1, "ok")
    assert rs_store.credit_usage(db, "2000-01-01") == 5.0


def test_refund_credits_nets_out_failed_request(db) -> None:
    oddsapi.reserve_credits(db, Settings(), 1, "sport=x")
    oddsapi.refund_credits(db, 1, "sport=x request failed")
    assert rs_store.credit_usage(db, "2000-01-01") == 0.0
    rows = db.execute(
        "SELECT units FROM cost_ledger WHERE category='odds_api_credit'"
    ).fetchall()
    assert [r["units"] for r in rows] == [1.0, -1.0]  # 账目留痕可审计


def test_partial_failure_keeps_completed_request_credits(db) -> None:
    """第二个 sport 请求失败：第一个的消耗已入账，失败的已退回（票 35 验收 6）。"""
    seed_jingcai(db)
    settings = Settings(odds_api_key="k")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sports"):
            return httpx.Response(
                200,
                json=[
                    {"key": "soccer_epl", "group": "Soccer", "title": "EPL"},
                    {"key": "soccer_spain_la_liga", "group": "Soccer", "title": "LL"},
                ],
            )
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=EVENTS)
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        oddsapi.fetch_and_store_odds(db, settings, client)
    # sport1 成功消耗 1（保留），sport2 失败退回：净 1，不漏记不重复
    assert rs_store.credit_usage(db, "2000-01-01") == 1.0


def _seed_with_alias(db, source_match_id: int, kickoff: str) -> int:
    """入库一场竞彩并把主客队的 odds_api 英文名映射好。"""
    payload = {
        "errorCode": "0",
        "value": {
            "matchInfoList": [
                {
                    "businessDate": "2026-09-12",
                    "subMatchList": [
                        {
                            "matchId": source_match_id,
                            "matchNumStr": f"周六{source_match_id:03d}",
                            "leagueAbbName": "英超",
                            "homeTeamAllName": f"主队{source_match_id}",
                            "awayTeamAllName": f"客队{source_match_id}",
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
    row = db.execute(
        "SELECT f.* FROM fixtures f JOIN match_codes mc ON mc.fixture_id=f.id"
        " WHERE mc.source_match_id = ?",
        (str(source_match_id),),
    ).fetchone()
    for team_id, alias in (
        (row["home_team_id"], "Arsenal"),
        (row["away_team_id"], "Chelsea"),
    ):
        db.execute(
            "INSERT OR IGNORE INTO team_aliases (team_id, source, alias)"
            " VALUES (?, 'odds_api', ?)",
            (team_id, alias),
        )
    db.execute("UPDATE fixtures SET kickoff_utc = ? WHERE id = ?", (kickoff, row["id"]))
    db.commit()
    return int(row["id"])


def test_join_rejects_home_away_swap(db) -> None:
    """已知队名时，主客互换的事件不能仅凭时间窗 join（票 35 验收 3）。"""
    _seed_with_alias(db, 7, "2026-09-12T18:00:00+00:00")
    swapped = dict(
        EVENTS[0],
        home_team="Chelsea",
        away_team="Arsenal",
        bookmakers=[EVENTS[0]["bookmakers"][0]],
    )
    report = oddsapi.join_fixtures(db, oddsapi.parse_events("soccer_epl", [swapped]))
    assert report.joined == 0
    assert report.unmatched[0]["reason"] == "team_swap_mismatch"
    assert db.execute("SELECT odds_api_event_id FROM fixtures").fetchone()[0] is None


def test_join_rejects_team_name_conflict(db) -> None:
    _seed_with_alias(db, 8, "2026-09-12T18:00:00+00:00")
    other = {
        "id": "sp1",
        "sport_key": "soccer_epl",
        "commence_time": "2026-09-12T18:00:00Z",
        "home_team": "Tottenham",
        "away_team": "West Ham",
        "bookmakers": [
            {
                "key": "pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Tottenham", "price": 1.9},
                            {"name": "Draw", "price": 3.5},
                            {"name": "West Ham", "price": 4.0},
                        ],
                    }
                ],
            }
        ],
    }
    report = oddsapi.join_fixtures(db, oddsapi.parse_events("soccer_epl", [other]))
    assert report.unmatched[0]["reason"] == "team_name_conflict"


def test_join_alias_match_wins_over_time_only(db) -> None:
    """时间更近但队名对不上的事件让位于队名一致的事件。"""
    _seed_with_alias(db, 9, "2026-09-12T18:00:00+00:00")
    nearer = dict(
        EVENTS[0],
        id="near1",
        commence_time="2026-09-12T18:05:00Z",
        home_team="Tottenham",
        away_team="West Ham",
        bookmakers=[EVENTS[0]["bookmakers"][0]],
    )
    matching = dict(EVENTS[0], id="far1", commence_time="2026-09-12T18:15:00Z")
    report = oddsapi.join_fixtures(
        db, oddsapi.parse_events("soccer_epl", [nearer, matching])
    )
    assert report.joined == 1
    row = db.execute("SELECT odds_api_event_id FROM fixtures").fetchone()
    assert row["odds_api_event_id"] == "far1"


def test_join_duplicate_external_event_not_reused(db) -> None:
    """同一外部事件不能被两个 fixture 占用（重复外部事件，票 35 验收 3）。"""
    first = _seed_with_alias(db, 10, "2026-09-12T18:00:00+00:00")
    second = _seed_with_alias(db, 11, "2026-09-12T18:00:00+00:00")
    events = oddsapi.parse_events("soccer_epl", EVENTS)
    report = oddsapi.join_fixtures(db, events)
    assert report.joined == 1
    rows = {
        int(r["id"]): r["odds_api_event_id"]
        for r in db.execute("SELECT id, odds_api_event_id FROM fixtures").fetchall()
    }
    assert rows[first] == "abc123"
    assert rows[second] is None
    reasons = {u["reason"] for u in report.unmatched}
    assert reasons == {"no_free_event_in_window"}


# --- 票 37：冻结采集范围 / closing 窗口前置检查 ---


def test_frozen_sport_scope_skips_discovery_and_paid_calls(db) -> None:
    """冻结范围：不动态发现、只拉声明的 sport key，credit 按范围计。"""
    seed_jingcai(db)
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sports"):
            raise AssertionError("冻结范围不应调用 /sports 发现")
        if "odds" in request.url.path:
            fetched.append(str(request.url))
            return httpx.Response(
                200, json=EVENTS if "soccer_epl" in str(request.url) else []
            )
        raise AssertionError(f"意外请求: {request.url}")

    settings = Settings(
        odds_api_key="k",
        odds_api_sport_scope="soccer_epl, soccer_italy_serie_a",
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = oddsapi.fetch_and_store_odds(db, settings, client)
    assert len(fetched) == 2
    assert stats.credits_used == 2
    assert rs_store.credit_usage(db, "2000-01-01T00:00:00+00:00") == 2.0


def test_closing_window_skips_when_no_joined_fixture_in_window(db) -> None:
    """无窗口内已 join 场次：零请求零 credit（票 37 不空耗预算）。"""
    from datetime import UTC, datetime

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发出任何请求")

    settings = Settings(odds_api_key="k")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = oddsapi.fetch_closing_window(
        db, settings, client, now=datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
    )
    assert stats.credits_used == 0
    assert stats.events == 0


def test_closing_window_runs_when_joined_fixture_upcoming(db) -> None:
    seed_jingcai(db)
    from datetime import UTC, datetime

    db.execute("UPDATE fixtures SET odds_api_event_id='abc123', join_method='manual'")
    db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sports"):
            return httpx.Response(200, json=SPORTS[:1])
        if "odds" in request.url.path:
            return httpx.Response(200, json=EVENTS)
        raise AssertionError(f"意外请求: {request.url}")

    settings = Settings(odds_api_key="k")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    # 02:00 北京 = 前日 18:00 UTC；17:40 UTC 距开赛 20 分钟，在 35 分钟窗口内
    stats = oddsapi.fetch_closing_window(
        db, settings, client, now=datetime(2026, 9, 12, 17, 40, tzinfo=UTC)
    )
    assert stats.credits_used == 1


# --- 票 47：双锚定向拉取（eventIds 批量 + meta 锚标） ---


def test_fetch_anchor_snapshots_empty_targets_zero_cost(db) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("空目标不应发请求")

    stats = oddsapi.fetch_anchor_snapshots(
        db,
        Settings(odds_api_key="k"),
        httpx.Client(transport=httpx.MockTransport(handler)),
        anchor=oddsapi.ANCHOR_KICKOFF,
        targets=[],
    )
    assert stats.credits_used == 0
    assert stats.events == 0


def test_fetch_anchor_snapshots_batches_and_stamps(db) -> None:
    seed_jingcai(db)
    fx_store.set_odds_api_join(db, 1, "abc123", "soccer_epl", "manual")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "soccer_epl" in request.url.path:
            assert "eventIds=abc123" in str(request.url)
            return httpx.Response(200, json=EVENTS)
        return httpx.Response(200, json=[])

    stats = oddsapi.fetch_anchor_snapshots(
        db,
        Settings(odds_api_key="k"),
        httpx.Client(transport=httpx.MockTransport(handler)),
        anchor=oddsapi.ANCHOR_SALE_STOP,
        targets=[
            ("abc123", "soccer_epl"),
            ("abc123", "soccer_epl"),  # 同 sport 去重
            ("zzz999", "soccer_spain_la_liga"),
        ],
    )
    # 每 sport 一个请求（eventIds 批量不另计费）→ 2 credits
    assert stats.credits_used == 2
    assert len([u for u in seen if "/odds" in u]) == 2
    # 快照 purpose=closing 且带锚标
    rows = db.execute(
        "SELECT DISTINCT purpose, meta FROM odds_snapshots"
        " WHERE source LIKE 'odds_api:%'"
    ).fetchall()
    assert rows
    for row in rows:
        assert row["purpose"] == "closing"
        assert json.loads(row["meta"]) == {"anchor": "sale_stop"}
    # credit 台账 note 可审计（锚+事件定向）
    notes = [
        str(r["note"])
        for r in db.execute(
            "SELECT note FROM cost_ledger WHERE category='odds_api_credit'"
        ).fetchall()
    ]
    assert any("abc123" in n for n in notes)
    assert any("anchor=sale_stop" in n for n in notes)


def test_kickoff_bucket_bounds_grid_aligned() -> None:
    """桶界对齐 5 分钟网格且抖动不变；整点开球属上一桶（秒粒度半开）。"""
    from datetime import UTC, datetime

    for moment in (
        datetime(2026, 9, 12, 18, 57, 3, tzinfo=UTC),  # 桶内抖动
        datetime(2026, 9, 12, 18, 59, 58, tzinfo=UTC),  # 桶尾抖动
    ):
        start, end = oddsapi.kickoff_bucket_bounds(moment)
        assert (start, end) == (
            "2026-09-12T18:55:01+00:00",
            "2026-09-12T19:00:00+00:00",
        )
