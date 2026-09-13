"""结算编排用例：投影评估、批跑结算与开奖更正重算（票 23/34）。"""

from __future__ import annotations

import json
import sqlite3

from goalx_backend.betting import store as bt_store
from goalx_backend.data import results as rs_store
from goalx_backend.db import atomic
from goalx_backend.models import BetStatus, SettlementInput
from goalx_backend.settlement import (
    LegSpec,
    ResultFacts,
    SettlementOutcome,
    detail_payload,
    settle_fixed_bet,
)


def _result_facts(row: sqlite3.Row) -> ResultFacts:
    """draw_results 行 → 结算投影。"""
    return ResultFacts(
        home_goals=int(row["home_goals"]),
        away_goals=int(row["away_goals"]),
        half_home_goals=(
            int(row["half_home_goals"]) if row["half_home_goals"] is not None else None
        ),
        half_away_goals=(
            int(row["half_away_goals"]) if row["half_away_goals"] is not None else None
        ),
        void=bool(row["void"]),
        void_reason=row["void_reason"],
    )


def evaluate_bet(conn: sqlite3.Connection, bet: sqlite3.Row) -> SettlementOutcome:
    """Project a fixed bet against current results without changing stored facts."""
    legs_raw = json.loads(bet["legs"])
    leg_specs = [
        LegSpec(
            fixture_id=int(leg["fixture_id"]),
            market_code=str(leg["market_code"]),
            selection_code=str(leg["selection_code"]),
            locked_odds=float(leg["locked_odds"]),
            goal_line=leg["goal_line"],
        )
        for leg in legs_raw
    ]
    results = {
        int(fixture_id): _result_facts(row)
        for fixture_id, row in rs_store.draw_results_for_fixtures(
            conn, [int(leg["fixture_id"]) for leg in legs_raw]
        ).items()
    }
    return settle_fixed_bet(float(bet["stake"]), leg_specs, results)


def _settle_one_bet(
    conn: sqlite3.Connection, bet: sqlite3.Row, *, reason: str = "settlement"
) -> str:
    """Persist settlement and append only the change in purchased live entitlement."""
    outcome = evaluate_bet(conn, bet)
    if not outcome.settled and bet["status"] == "open":
        return "open"
    real = bet["mode"] == "live" and bool(bet["purchased"])
    previous_payout = float(bet["payout"] or 0)
    events = bt_store.bankroll_events_for_bet(conn, int(bet["id"]))
    stakes = [event for event in events if event["kind"] == "bet_stake"]
    paid = sum(
        float(event["amount_cny"]) for event in events if event["kind"] == "bet_payout"
    )
    if real:
        consistent = (
            len(stakes) == 1
            and round(float(stakes[0]["amount_cny"]), 2)
            == -round(float(bet["stake"]), 2)
            and bet["slip_id"] is not None
            and stakes[0]["slip_id"] in (None, bet["slip_id"])
            and round(paid - previous_payout, 2) == 0
        )
    else:
        consistent = not events
    if not consistent:
        raise ValueError(f"bet {bet['id']} 旧账异常, 请先执行 audit-ledger 并人工核查")
    delta = round(outcome.payout - previous_payout, 2)
    bt_store.save_settlement(
        conn,
        SettlementInput(
            bet_id=int(bet["id"]),
            status=BetStatus(outcome.status) if outcome.settled else BetStatus.PARTIAL,
            stake=outcome.stake,
            payout=outcome.payout,
            profit=outcome.profit,
            detail=detail_payload(outcome),
        ),
        reason=reason,
    )
    if real and delta:
        bt_store.record_bankroll_event(
            conn,
            "bet_payout",
            delta,
            bet_id=int(bet["id"]),
            slip_id=int(bet["slip_id"]),
            note=reason,
        )
    return outcome.status


def validate_correction_targets(
    conn: sqlite3.Connection, fixture_ids: set[int]
) -> None:
    """Refuse to silently repair legacy settlement errors while correcting facts."""
    for bet in bt_store.list_bets(conn):
        if bet["status"] == "open" or bet["market_kind"] != "fixed":
            continue
        if not any(
            int(leg["fixture_id"]) in fixture_ids for leg in json.loads(bet["legs"])
        ):
            continue
        prior = evaluate_bet(conn, bet)
        if prior.status != bet["status"] or round(
            prior.payout - float(bet["payout"] or 0), 2
        ):
            raise ValueError(
                f"bet {bet['id']} 原结算与当前规则/事实不符, 请先 audit-ledger"
            )


def resettle_corrected_results(
    conn: sqlite3.Connection, fixture_ids: set[int], *, reason: str
) -> None:
    """Recompute affected finalized bets inside the result import transaction."""
    for bet in bt_store.list_bets(conn):
        if bet["market_kind"] != "fixed":
            continue
        if bet["status"] == "open" and not bt_store.settlement_exists(
            conn, int(bet["id"])
        ):
            continue
        affected = any(
            int(leg["fixture_id"]) in fixture_ids for leg in json.loads(bet["legs"])
        )
        if affected:
            _settle_one_bet(conn, bet, reason=reason)


def run_settlement(conn: sqlite3.Connection) -> dict[str, int]:
    """结算批跑：所有已开赛且有赛果的未结注/池票；返回统计。"""
    stats = {"settled": 0, "still_open": 0, "won": 0, "lost": 0, "void": 0}
    with atomic(conn):
        for bet in bt_store.list_bets(conn, only_open=True):
            status = (
                _settle_one_bet(conn, bet) if bet["market_kind"] == "fixed" else "open"
            )
            if status == "open":
                stats["still_open"] += 1
            else:
                stats["settled"] += 1
                stats[status] += 1
        # Pool awards are unsupported: keep drafts pending, never finalize at zero.
        stats["still_open"] += bt_store.count_pending_pool_slips(conn)
    return stats
