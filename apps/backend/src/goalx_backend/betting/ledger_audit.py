"""Read-only legacy ledger inspection; never infer or repair a purchase."""

from __future__ import annotations

import sqlite3

from goalx_backend.betting.settle import evaluate_bet
from goalx_backend.db import current_version

REVISION_SCHEMA_VERSION = 4


def audit_ledger(conn: sqlite3.Connection) -> dict[str, object]:
    """Return manual review evidence and correction history without writes."""
    findings: list[dict[str, object]] = []
    balance = 0.0
    for event in conn.execute("SELECT * FROM bankroll_events ORDER BY id"):
        balance += float(event["amount_cny"])
        if round(balance - float(event["balance_after"]), 2):
            findings.append(
                {
                    "kind": "balance_mismatch",
                    "event_id": event["id"],
                    "expected": round(balance, 2),
                    "stored": event["balance_after"],
                }
            )
    for bet in conn.execute(
        """
        SELECT b.*, s.created_at AS locked_at,
        (SELECT json_group_array(json_object(
            'fixture_id', l.fixture_id, 'market_code', l.market_code,
            'selection_code', l.selection_code, 'locked_odds', l.locked_odds,
            'goal_line', json_extract(l.meta, '$.goal_line')))
         FROM bet_legs l WHERE l.bet_id = b.id) AS legs
        FROM bets b LEFT JOIN bet_slips s ON s.id = b.slip_id ORDER BY b.id
        """
    ):
        events = conn.execute(
            "SELECT * FROM bankroll_events WHERE bet_id=? ORDER BY id", (bet["id"],)
        ).fetchall()
        issues = _bet_issues(conn, bet, events)
        if issues:
            findings.append(
                {
                    "bet_id": bet["id"],
                    "issues": issues,
                    "bet": dict(bet),
                    "events": [dict(e) for e in events],
                }
            )
    for row in conn.execute("SELECT * FROM settlements WHERE slip_id IS NOT NULL"):
        findings.append(
            {"kind": "unsupported_pool_finalization", "settlement": dict(row)}
        )
    history: dict[str, object] = {}
    if current_version(conn) >= REVISION_SCHEMA_VERSION:
        for table in ("draw_result_revisions", "settlement_revisions"):
            history[table] = [
                dict(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608
            ]
    return {
        "schema_version": current_version(conn),
        "manual_review": findings,
        "revision_history": history,
        "repairs_applied": False,
    }


def _bet_issues(
    conn: sqlite3.Connection, bet: sqlite3.Row, events: list[sqlite3.Row]
) -> list[str]:
    """Check one recorded bet against its ledger and the current rules."""
    issues: list[str] = []
    real = bet["mode"] == "live" and bool(bet["purchased"])
    stakes = [e for e in events if e["kind"] == "bet_stake"]
    paid = sum(float(e["amount_cny"]) for e in events if e["kind"] == "bet_payout")
    if not real and events:
        issues.append("events_without_live_purchase")
    if real:
        if len(stakes) != 1 or round(
            sum(float(e["amount_cny"]) for e in stakes) + float(bet["stake"]), 2
        ):
            issues.append("missing_or_duplicate_stake")
        if round(paid - float(bet["payout"] or 0), 2):
            issues.append("payout_mismatch")
    if bet["purchased"] and bet["slip_id"] is None:
        issues.append("purchase_without_slip")
    if any(e["slip_id"] is not None and e["slip_id"] != bet["slip_id"] for e in events):
        issues.append("slip_binding_mismatch")
    if bet["market_kind"] == "fixed" and bet["status"] != "open":
        outcome = evaluate_bet(conn, bet)
        if outcome.status != bet["status"] or round(
            outcome.payout - float(bet["payout"] or 0), 2
        ):
            issues.append("settlement_differs_from_current_rules_or_result")
    return issues
