"""结算编排用例：投影评估、批跑结算、开奖更正重算与影响预览（票 23/34/36）。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from goalx_backend.betting import store as bt_store
from goalx_backend.data import results as rs_store
from goalx_backend.db import atomic
from goalx_backend.models import BetStatus, DrawResultInput, SettlementInput
from goalx_backend.settlement import (
    LegSpec,
    ResultFacts,
    SettlementOutcome,
    detail_payload,
    settle_fixed_bet,
)


@dataclass(frozen=True)
class ResultChangePreview:
    """预览里一条结果变化（票 36）。"""

    fixture_id: int
    is_correction: bool
    previous: dict[str, Any] | None
    replacement: dict[str, Any]


@dataclass(frozen=True)
class AffectedBetPreview:
    """预览里一注受影响注的当前 vs 投影结算。"""

    bet_id: int
    mode: str
    purchased: bool
    status_current: str
    payout_current: float | None
    status_projected: str
    payout_projected: float | None
    delta_payout: float | None


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


def _proposed_facts(result: DrawResultInput) -> ResultFacts:
    """导入载荷 → 结算投影（影响预览用，不落库）。"""
    return ResultFacts(
        home_goals=result.home_goals,
        away_goals=result.away_goals,
        half_home_goals=result.half_home_goals,
        half_away_goals=result.half_away_goals,
        void=result.void,
        void_reason=result.void_reason,
    )


def _effective_terms(bet: sqlite3.Row) -> tuple[float, list[LegSpec]]:
    """
    一注的结算条款：实际执行条款优先，缺省回退建议条款（票 36）。

    兼容旧库/旧聚合行：actual_stake/actual_odds 缺失按建议条款结算。
    """
    fields = dict(bet)
    actual_stake = fields.get("actual_stake")
    stake = float(actual_stake) if actual_stake is not None else float(bet["stake"])
    specs = [
        LegSpec(
            fixture_id=int(leg["fixture_id"]),
            market_code=str(leg["market_code"]),
            selection_code=str(leg["selection_code"]),
            locked_odds=float(leg.get("actual_odds") or leg["locked_odds"]),
            goal_line=leg["goal_line"],
        )
        for leg in json.loads(bet["legs"])
    ]
    return stake, specs


def evaluate_bet(conn: sqlite3.Connection, bet: sqlite3.Row) -> SettlementOutcome:
    """Project a fixed bet against current results without changing stored facts."""
    stake, leg_specs = _effective_terms(bet)
    results = {
        int(fixture_id): _result_facts(row)
        for fixture_id, row in rs_store.draw_results_for_fixtures(
            conn, [spec.fixture_id for spec in leg_specs]
        ).items()
    }
    return settle_fixed_bet(stake, leg_specs, results)


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
            and round(float(stakes[0]["amount_cny"]), 2) == -round(outcome.stake, 2)
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


@dataclass(frozen=True)
class DrawResultPreview:
    """开奖导入影响预览载荷（results 变化 + 受影响注投影）。"""

    results: list[ResultChangePreview]
    affected_bets: list[AffectedBetPreview]


def preview_draw_result_change(
    conn: sqlite3.Connection, results: list[DrawResultInput]
) -> DrawResultPreview:
    """
    开奖导入影响预览（票 36：只读，不落库、不冲正）。

    复用结算引擎：对受影响注用「现有事实 + 提议事实」投影对比，
    展示当前 vs 投影结算与资金差，导入路径才真正执行冲正。
    """
    with_results: dict[int, sqlite3.Row] = rs_store.draw_results_for_fixtures(
        conn, [r.fixture_id for r in results]
    )
    overlaid: dict[int, ResultFacts] = {
        fid: _result_facts(row) for fid, row in with_results.items()
    }
    for result in results:
        overlaid[result.fixture_id] = _proposed_facts(result)

    changes: list[ResultChangePreview] = []
    for result in results:
        previous = with_results.get(result.fixture_id)
        changes.append(
            ResultChangePreview(
                fixture_id=result.fixture_id,
                is_correction=previous is not None,
                previous=dict(previous) if previous is not None else None,
                replacement=result.model_dump(
                    exclude={"correction_reason"}, mode="json"
                ),
            )
        )

    affected: list[AffectedBetPreview] = []
    for bet in bt_store.list_bets(conn):
        if bet["market_kind"] != "fixed":
            continue
        stake, leg_specs = _effective_terms(bet)
        if not any(spec.fixture_id in overlaid for spec in leg_specs):
            continue
        facts = {
            fid: overlaid[fid]
            for fid in (spec.fixture_id for spec in leg_specs)
            if fid in overlaid
        }
        projected = settle_fixed_bet(stake, leg_specs, facts)
        settled_now = bet["status"] != "open"
        affected.append(
            AffectedBetPreview(
                bet_id=int(bet["id"]),
                mode=str(bet["mode"]),
                purchased=bool(bet["purchased"]),
                status_current=str(bet["status"]),
                payout_current=float(bet["payout"] or 0) if settled_now else None,
                status_projected=projected.status if projected.settled else "open",
                payout_projected=projected.payout if projected.settled else None,
                delta_payout=(
                    round(projected.payout - float(bet["payout"] or 0), 2)
                    if projected.settled and settled_now
                    else None
                ),
            )
        )
    return DrawResultPreview(results=changes, affected_bets=affected)
