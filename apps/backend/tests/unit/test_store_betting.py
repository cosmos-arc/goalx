"""投注域仓储测试。"""

from __future__ import annotations

import json
import sqlite3

from goalx_backend.models import (
    BetMode,
    BetStatus,
    LegInput,
    MarketKind,
    SettlementInput,
)
from goalx_backend.store import betting as bt
from goalx_backend.store import fixtures as fx


def seed_fixtures(db: sqlite3.Connection, count: int) -> list[int]:
    """插入 count 场比赛，返回 fixture id 列表。"""
    comp = fx.upsert_competition(db, "英超")
    home = fx.upsert_team(db, "阿森纳")
    away = fx.upsert_team(db, "切尔西")
    return [
        fx.upsert_fixture(db, comp, f"2026-09-1{day}T10:00:00+00:00", home, away)
        for day in range(count)
    ]


def test_create_bet_with_legs_and_list(db: sqlite3.Connection) -> None:
    first_fixture, second_fixture, _ = seed_fixtures(db, 3)
    bet = bt.create_bet(db, BetMode.PAPER, MarketKind.FIXED, 2.0)
    bt.add_leg(
        db,
        bet,
        LegInput(
            fixture_id=first_fixture,
            market_code="had",
            selection_code="h",
            locked_odds=2.10,
        ),
    )
    bt.add_leg(
        db,
        bet,
        LegInput(
            fixture_id=second_fixture,
            market_code="had",
            selection_code="a",
            locked_odds=3.40,
            goal_line=-0.5,
        ),
    )
    rows = bt.list_bets(db)
    assert len(rows) == 1
    legs = json.loads(rows[0]["legs"])
    assert [leg["fixture_id"] for leg in legs] == [first_fixture, second_fixture]
    assert legs[1]["goal_line"] == -0.5


def test_list_bets_filter_mode_and_open(db: sqlite3.Connection) -> None:
    bt.create_bet(db, BetMode.PAPER, MarketKind.FIXED, 2.0)
    live = bt.create_bet(db, BetMode.LIVE, MarketKind.FIXED, 2.0, purchased=True)
    assert len(bt.list_bets(db, mode=BetMode.LIVE)) == 1
    assert len(bt.list_bets(db, only_open=True)) == 2
    bt.save_settlement(
        db,
        SettlementInput(
            bet_id=live,
            status=BetStatus.WON,
            stake=2.0,
            payout=4.2,
            profit=2.2,
            detail={},
        ),
    )
    assert len(bt.list_bets(db, only_open=True)) == 1


def test_settlement_updates_bet_and_upserts(db: sqlite3.Connection) -> None:
    bet = bt.create_bet(db, BetMode.PAPER, MarketKind.FIXED, 2.0)
    bt.save_settlement(
        db,
        SettlementInput(
            bet_id=bet,
            status=BetStatus.WON,
            stake=2.0,
            payout=4.2,
            profit=2.2,
            detail={"k": "v"},
        ),
    )
    bt.save_settlement(
        db,
        SettlementInput(
            bet_id=bet,
            status=BetStatus.LOST,
            stake=2.0,
            payout=0.0,
            profit=-2.0,
            detail={"k": "v2"},
        ),
    )
    row = bt.get_bet(db, bet)
    assert row is not None
    assert row["status"] == "lost"
    count = db.execute("SELECT COUNT(*) AS n FROM settlements").fetchone()["n"]
    assert count == 1  # upsert 而非重复行


def test_slip_attach_and_mark_purchased(db: sqlite3.Connection) -> None:
    suggestion = bt.create_bet(db, BetMode.PAPER, MarketKind.FIXED, 2.0)
    other = bt.create_bet(db, BetMode.PAPER, MarketKind.FIXED, 4.0)
    slip = bt.create_slip(db, BetMode.PAPER, placed_at=None, note="周六")
    attached = bt.attach_bets_to_slip(
        db, slip, [suggestion], "2026-09-13T02:00:00+00:00"
    )
    assert attached == 1
    rows = {row["id"]: row for row in bt.list_bets(db)}
    assert rows[suggestion]["purchased"] == 1
    assert rows[suggestion]["slip_id"] == slip
    assert rows[other]["purchased"] == 0
    slips = bt.list_slips(db)
    assert slips[0]["bet_count"] == 1


def test_materialize_combinations(db: sqlite3.Connection) -> None:
    slip = bt.create_slip(db, BetMode.PAPER)
    for sel in ("3", "1"):
        bt.add_pool_pick(db, slip, 1, sel)
    bt.add_pool_pick(db, slip, 2, "0")
    n = bt.materialize_combinations(db, slip)
    assert n == 2  # 2 × 1
    rows = db.execute(
        "SELECT selections FROM combinations WHERE slip_id = ? ORDER BY seq", (slip,)
    ).fetchall()
    first = json.loads(rows[0]["selections"])
    assert first == [
        {"match_seq": 1, "selection_code": "1"},
        {"match_seq": 2, "selection_code": "0"},
    ]
    # 重跑替换而非累加
    assert bt.materialize_combinations(db, slip) == 2
    count = db.execute(
        "SELECT COUNT(*) AS n FROM combinations WHERE slip_id = ?", (slip,)
    ).fetchone()["n"]
    assert count == 2


def test_bankroll_events(db: sqlite3.Connection) -> None:
    bet = bt.create_bet(db, BetMode.LIVE, MarketKind.FIXED, 2.0)
    assert bt.bankroll_balance(db) is None
    bt.record_bankroll_event(db, "deposit", 5000.0, note="初始资金")
    bt.record_bankroll_event(db, "bet_payout", 4.2, bet_id=bet)
    assert bt.bankroll_balance(db) == 5004.2
    events = bt.list_bankroll_events(db)
    assert events[0]["kind"] == "bet_payout"
    assert events[1]["balance_after"] == 5000.0
