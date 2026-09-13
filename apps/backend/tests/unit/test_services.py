"""服务层测试：今日视图、投注生命周期与结算批跑（纸面闭环，票 23 验收）。"""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend.db import utc_now_iso
from goalx_backend.ingest import oddsapi, sporttery
from goalx_backend.models import DrawResultInput, LegInput
from goalx_backend.services import (
    BetDraft,
    build_today_view,
    create_bet_with_legs,
    record_purchase,
    run_settlement,
)
from goalx_backend.store import betting as bt_store

JINGCAI_PAYLOAD = {
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
                            "d": "5.00",
                            "h": "6.50",
                            "updateDate": "2026-09-12",
                            "updateTime": "20:00:00",
                        },
                    },
                    {
                        "matchId": 2,
                        "matchNumStr": "周六002",
                        "leagueAbbName": "英超",
                        "homeTeamAllName": "利物浦",
                        "awayTeamAllName": "曼城",
                        "matchDate": "2026-09-13",
                        "matchTime": "04:00:00",
                        "bettingSingle": 0,
                        "had": {
                            "a": "2.20",
                            "d": "3.40",
                            "h": "3.00",
                            "updateDate": "2026-09-12",
                            "updateTime": "21:00:00",
                        },
                    },
                ],
            }
        ]
    },
}


def seed(db: sqlite3.Connection) -> list[int]:
    """入库两场英超竞彩 + 一场欧赔 join（阿森纳场）。"""
    sporttery.store_matches(db, sporttery.parse_matches(JINGCAI_PAYLOAD))
    events = oddsapi.parse_events(
        "soccer_epl",
        [
            {
                "id": "e1",
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
                                    {"name": "Arsenal", "price": 6.0},
                                    {"name": "Draw", "price": 5.0},
                                    {"name": "Chelsea", "price": 1.30},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    )
    oddsapi.join_fixtures(db, events)
    oddsapi.store_events(db, events)
    rows = db.execute("SELECT id FROM fixtures ORDER BY id").fetchall()
    return [int(r["id"]) for r in rows]


def test_build_today_view(db: sqlite3.Connection) -> None:
    seed(db)
    view = build_today_view(db, "2026-09-12")
    assert len(view) == 2
    first = view[0]
    assert first.match_code == "周六001"
    assert first.tier == "tier1"
    assert (first.jc_odds.h, first.jc_odds.d, first.jc_odds.a) == (6.5, 5.0, 1.3)
    assert first.jc_updated_at == "2026-09-12T12:00:00+00:00"
    assert first.joined is True
    assert first.books == 1
    assert set(first.flags) == {"ev_deviation", "few_books"}  # jc 6.5 vs 欧共识 6.0
    # 欧赔共识去晦后：EV = p*odds-1（h: p≈0.157*6.5-1 ≈ +2%… 精确值由 Shin 决定）
    assert first.eu_prob is not None
    assert sum(v for v in first.eu_prob.model_dump().values()) == 1.0
    second = view[1]
    assert second.joined is False
    assert second.eu_prob is None
    assert "not_joined" in second.flags


def test_create_bet_rejects_same_fixture_parlay(db: sqlite3.Connection) -> None:
    fixture_ids = seed(db)
    draft = BetDraft(
        mode="paper",  # type: ignore[arg-type]
        stake=2.0,
        legs=[
            LegInput(
                fixture_id=fixture_ids[0],
                market_code="had",
                selection_code="h",
                locked_odds=6.5,
            ),
            LegInput(
                fixture_id=fixture_ids[0],
                market_code="hhad",
                selection_code="a",
                locked_odds=1.9,
            ),
        ],
    )
    with pytest.raises(ValueError, match=r"同场|重复"):
        create_bet_with_legs(db, draft)


def test_paper_full_loop(db: sqlite3.Connection) -> None:
    """票 23 验收：建议 → 回录 → 开奖导入 → 结算 → 复盘列表。"""
    fixture_ids = seed(db)
    arsenal, liverpool = fixture_ids

    # 建议：阿森纳主胜单关 + 利物浦×阿森纳 2串1
    single = create_bet_with_legs(
        db,
        BetDraft(
            mode="paper",  # type: ignore[arg-type]
            stake=100.0,
            legs=[
                LegInput(
                    fixture_id=arsenal,
                    market_code="had",
                    selection_code="h",
                    locked_odds=6.5,
                )
            ],
        ),
    )
    parlay = create_bet_with_legs(
        db,
        BetDraft(
            mode="paper",  # type: ignore[arg-type]
            stake=2.0,
            legs=[
                LegInput(
                    fixture_id=arsenal,
                    market_code="had",
                    selection_code="h",
                    locked_odds=6.5,
                ),
                LegInput(
                    fixture_id=liverpool,
                    market_code="had",
                    selection_code="a",
                    locked_odds=2.2,
                ),
            ],
        ),
    )
    suggestions = bt_store.list_bets(db)
    assert len(suggestions) == 2
    assert all(row["purchased"] == 0 for row in suggestions)  # 含未购标记

    # 回录：只实际购买其中一注
    slip = record_purchase(db, [single], "2026-09-12T19:00:00+00:00")
    rows = {row["id"]: row for row in bt_store.list_bets(db)}
    assert rows[single]["purchased"] == 1
    assert rows[single]["slip_id"] == slip
    assert rows[parlay]["purchased"] == 0

    # 结算前：无赛果 → open
    stats = run_settlement(db)
    assert stats["still_open"] == 2

    # 官方开奖导入：阿森纳 3:1、利物浦 0:2
    from goalx_backend.ingest import results as results_ingest

    imported = results_ingest.import_draw_results(
        db,
        [
            DrawResultInput(
                fixture_id=arsenal, home_goals=3, away_goals=1, source="manual"
            ),
            DrawResultInput(
                fixture_id=liverpool, home_goals=0, away_goals=2, source="manual"
            ),
        ],
    )
    assert imported == 2

    stats = run_settlement(db)
    assert stats == {"settled": 2, "still_open": 0, "won": 2, "lost": 0, "void": 0}
    settled = {row["id"]: row for row in bt_store.list_bets(db)}
    assert settled[single]["payout"] == 650.0
    assert settled[parlay]["payout"] == 2.0 * 6.5 * 2.2
    assert settled[single]["profit"] == 550.0

    # 复盘列表（结算状态/盈亏可查）
    history = bt_store.list_bets(db)
    assert {row["status"] for row in history} == {"won"}


def test_live_bankroll_flow(db: sqlite3.Connection) -> None:
    fixture_ids = seed(db)
    arsenal = fixture_ids[0]
    bt_store.record_bankroll_event(db, "deposit", 5000.0, note="初始资金")
    bet = create_bet_with_legs(
        db,
        BetDraft(
            mode="live",  # type: ignore[arg-type]
            stake=100.0,
            legs=[
                LegInput(
                    fixture_id=arsenal,
                    market_code="had",
                    selection_code="a",
                    locked_odds=1.3,
                )
            ],
        ),
    )
    record_purchase(db, [bet])
    assert bt_store.bankroll_balance(db) == 4900.0
    from goalx_backend.ingest import results as results_ingest

    results_ingest.import_draw_results(
        db, [DrawResultInput(fixture_id=arsenal, home_goals=3, away_goals=1)]
    )
    # 阿森纳主胜 → 客胜未中
    stats = run_settlement(db)
    assert stats["lost"] == 1
    assert bt_store.bankroll_balance(db) == 4900.0  # 输光本金，无 payout 事件


def test_settlement_with_void_fixture(db: sqlite3.Connection) -> None:
    fixture_ids = seed(db)
    arsenal, liverpool = fixture_ids[0], fixture_ids[1]
    parlay = create_bet_with_legs(
        db,
        BetDraft(
            mode="paper",  # type: ignore[arg-type]
            stake=2.0,
            legs=[
                LegInput(
                    fixture_id=arsenal,
                    market_code="had",
                    selection_code="h",
                    locked_odds=6.5,
                ),
                LegInput(
                    fixture_id=liverpool,
                    market_code="had",
                    selection_code="a",
                    locked_odds=2.2,
                ),
            ],
        ),
    )
    from goalx_backend.ingest import results as results_ingest

    results_ingest.import_draw_results(
        db,
        [
            DrawResultInput(fixture_id=arsenal, home_goals=3, away_goals=1),
            DrawResultInput(
                fixture_id=liverpool,
                home_goals=0,
                away_goals=0,
                void=True,
                void_reason="腰斩",
            ),
        ],
    )
    run_settlement(db)
    row = bt_store.get_bet(db, parlay)
    assert row is not None
    # 剩余有效腿赔率 6.5 命中。
    assert row["status"] == "won"
    assert row["payout"] == 13.0
    assert utc_now_iso()  # sanity


def test_pool_slip_settlement_flow(db: sqlite3.Connection) -> None:
    """任9 复式：pool_picks → 组合 materialize → 逐组合判定。"""
    fixture_ids = seed(db)
    arsenal, liverpool = fixture_ids

    from goalx_backend.models import BetMode
    from goalx_backend.store import betting as bt

    slip = bt.create_slip(db, BetMode.PAPER, pool_period_id=None)
    bt.add_pool_pick(db, slip, 1, "3", fixture_id=arsenal)
    bt.add_pool_pick(db, slip, 1, "1", fixture_id=arsenal)  # 复式双选
    bt.add_pool_pick(db, slip, 2, "0", fixture_id=liverpool)
    n = bt.materialize_combinations(db, slip)
    assert n == 2

    # 场次未映射 → open
    unmapped = bt.create_slip(db, BetMode.PAPER)
    bt.add_pool_pick(db, unmapped, 1, "3")  # 无 fixture_id
    stats = run_settlement(db)
    assert stats["still_open"] >= 1

    from goalx_backend.ingest import results as results_ingest

    results_ingest.import_draw_results(
        db,
        [
            DrawResultInput(fixture_id=arsenal, home_goals=3, away_goals=1),  # 胜
            DrawResultInput(fixture_id=liverpool, home_goals=0, away_goals=2),  # 负
        ],
    )
    stats = run_settlement(db)
    assert stats["settled"] == 0
    assert stats["still_open"] == 2
    settled = db.execute(
        "SELECT status, detail FROM settlements WHERE slip_id = ?", (slip,)
    ).fetchone()
    assert settled is None  # 未实现奖金分配, 不得用 0 元标记已完成


def test_record_purchase_errors(db: sqlite3.Connection) -> None:
    fixture_ids = seed(db)
    import pytest

    from goalx_backend.models import BetMode
    from goalx_backend.store import betting as bt

    with pytest.raises(LookupError):
        record_purchase(db, [999])
    paper = bt.create_bet(db, BetMode.PAPER, from_kind_market_kind(), 2.0)
    live = bt.create_bet(db, BetMode.LIVE, from_kind_market_kind(), 2.0)
    with pytest.raises(ValueError, match="mode"):
        record_purchase(db, [paper, live])
    assert fixture_ids  # sanity


def from_kind_market_kind():
    """MarketKind.FIXED 便捷引用。"""
    from goalx_backend.models import MarketKind

    return MarketKind.FIXED


def test_live_won_bet_records_payout_event(db: sqlite3.Connection) -> None:
    fixture_ids = seed(db)
    arsenal = fixture_ids[0]
    from goalx_backend.models import BetMode
    from goalx_backend.store import betting as bt

    bt.record_bankroll_event(db, "deposit", 5000.0)
    bet = create_bet_with_legs(
        db,
        BetDraft(
            mode=BetMode.LIVE,
            stake=100.0,
            legs=[
                LegInput(
                    fixture_id=arsenal,
                    market_code="had",
                    selection_code="h",
                    locked_odds=6.5,
                )
            ],
        ),
    )
    record_purchase(db, [bet])
    assert bt.bankroll_balance(db) == 4900.0
    from goalx_backend.ingest import results as results_ingest

    results_ingest.import_draw_results(
        db, [DrawResultInput(fixture_id=arsenal, home_goals=2, away_goals=0)]
    )
    run_settlement(db)
    assert bt.bankroll_balance(db) == 4900.0 + 650.0  # payout 事件入账
