"""服务层：今日页视图、结算批跑与投注生命周期（API 与 flows 共用）。"""

from __future__ import annotations

import json
import sqlite3

from pydantic import BaseModel, Field

from goalx_backend import odds_math as om
from goalx_backend.betting import store as bt_store
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs_store
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.models import (
    BetMode,
    BetStatus,
    LegInput,
    MarketKind,
    SettlementInput,
)
from goalx_backend.settlement import (
    LegSpec,
    ResultFacts,
    SettlementOutcome,
    detail_payload,
    settle_fixed_bet,
)

EV_FLAG_THRESHOLD = 0.05  # 今日页 EV 偏差标记阈值（票 08 report 口径）
MIN_BOOKS_FOR_CONSENSUS = 3  # books 少于该数标记样本不足
SELECTIONS = ("h", "d", "a")


class SameFixtureParlayError(ValueError):
    """竞彩禁止同场串关（研究 01）。"""


class SelectionTriple(BaseModel):
    """主/平/客三元组（赔率、概率或 EV）。"""

    h: float | None = None
    d: float | None = None
    a: float | None = None


class TodayFixtureView(BaseModel):
    """今日页一行：竞彩 vs 欧洲共识对照（票 22）。"""

    fixture_id: int
    match_code: str
    competition: str
    tier: str
    home_team: str
    away_team: str
    kickoff_utc: str
    is_single: bool
    joined: bool
    jc_odds: SelectionTriple
    jc_updated_at: str | None = None
    books: int = 0
    eu_prob: SelectionTriple | None = None
    ev: SelectionTriple | None = None
    flags: list[str] = Field(default_factory=list)


def build_today_view(
    conn: sqlite3.Connection, business_date: str
) -> list[TodayFixtureView]:
    """组装竞彩场次对照表（竞彩 vs 欧洲共识、EV、books 数、调盘时点）。"""
    rows: list[TodayFixtureView] = []
    for fixture in fx_store.fixtures_for_business_date(conn, business_date):
        fixture_id = int(fixture["id"])
        jc = fx_store.latest_odds_by_selection(conn, fixture_id, "had", "sporttery")
        books = fx_store.eu_book_odds(conn, fixture_id)
        jc_odds = SelectionTriple(**{s: jc.get(s, (None,))[0] for s in SELECTIONS})
        view = TodayFixtureView(
            fixture_id=fixture_id,
            match_code=str(fixture["match_code"]),
            competition=str(fixture["competition_name"]),
            tier=str(fixture["competition_tier"]),
            home_team=str(fixture["home_team"]),
            away_team=str(fixture["away_team"]),
            kickoff_utc=str(fixture["kickoff_utc"]),
            is_single=bool(fixture["is_single"]),
            joined=fixture["odds_api_event_id"] is not None,
            jc_odds=jc_odds,
            jc_updated_at=max((at for _, at in jc.values()), default=None),
        )
        if all(s in books for s in SELECTIONS):
            consensus = om.consensus_odds([books[s] for s in SELECTIONS])
        else:
            consensus = None
        if consensus is not None:
            probs = om.shin_implied(consensus)
            view.books = max(len(prices) for prices in books.values())
            view.eu_prob = SelectionTriple(
                **{s: round(p, 4) for s, p in zip(SELECTIONS, probs, strict=True)}
            )
            view.ev = SelectionTriple(
                **{
                    s: round(om.expected_value(p, jc[s][0]), 4)
                    if s in jc and jc[s][0]
                    else None
                    for s, p in zip(SELECTIONS, probs, strict=True)
                }
            )
            ev_values = [v for v in (view.ev.h, view.ev.d, view.ev.a) if v is not None]
            if ev_values and max(abs(v) for v in ev_values) >= EV_FLAG_THRESHOLD:
                view.flags.append("ev_deviation")
        if not view.joined:
            view.flags.append("not_joined")
        if view.books and view.books < MIN_BOOKS_FOR_CONSENSUS:
            view.flags.append("few_books")
        rows.append(view)
    return rows


class BetDraft(BaseModel):
    """一注建议/回录的输入（API 反序列化后）。"""

    mode: BetMode
    stake: float = Field(gt=0, allow_inf_nan=False)
    legs: list[LegInput] = Field(min_length=1)


def create_bet_with_legs(conn: sqlite3.Connection, draft: BetDraft) -> int:
    """建注（含校验：竞彩禁止同场串关）；返回 bet id。"""
    seen: set[int] = set()
    for leg in draft.legs:
        if leg.fixture_id in seen:
            raise SameFixtureParlayError(f"fixture {leg.fixture_id} 重复出现在串关中")
        seen.add(leg.fixture_id)
    with atomic(conn):
        bet = bt_store.create_bet(conn, draft.mode, MarketKind.FIXED, draft.stake)
        for leg in draft.legs:
            bt_store.add_leg(conn, bet, leg)
    return bet


def record_purchase(
    conn: sqlite3.Connection, bet_ids: list[int], placed_at: str | None = None
) -> int:
    """票级回录：勾选实际购买子集，生成一张票；live 注扣减 bankroll。"""
    if not bet_ids or len(set(bet_ids)) != len(bet_ids):
        raise ValueError("bet_ids 必须非空且不能重复")
    with atomic(conn):
        at = placed_at or utc_now_iso()
        bets: list[sqlite3.Row] = []
        for bet_id in bet_ids:
            bet = bt_store.get_bet(conn, bet_id)
            if bet is None:
                raise LookupError(f"bet {bet_id} 不存在")
            if (
                bet["purchased"]
                or bet["slip_id"] is not None
                or bet["status"] != "open"
            ):
                raise ValueError(f"bet {bet_id} 已购、已绑定或已结, 不能再次回录")
            if (
                conn.execute(
                    """SELECT 1 FROM bankroll_events WHERE bet_id=? UNION ALL
                   SELECT 1 FROM settlements WHERE bet_id=?""",
                    (bet_id, bet_id),
                ).fetchone()
                is not None
            ):
                raise ValueError(f"bet {bet_id} 已有结算或资金记录, 请人工核查")
            if bet["market_kind"] != "fixed":
                raise ValueError("奖池奖金尚未支持, 不能回录")
            bets.append(bet)
        modes = {str(bet["mode"]) for bet in bets}
        if len(modes) != 1:
            raise ValueError("一张票内 mode 必须一致")
        mode = BetMode(modes.pop())
        slip = bt_store.create_slip(conn, mode, placed_at=at)
        bt_store.attach_bets_to_slip(conn, slip, bet_ids, at)
        if mode is BetMode.LIVE:
            for bet in bets:
                bt_store.record_bankroll_event(
                    conn,
                    "bet_stake",
                    -float(bet["stake"]),
                    bet_id=int(bet["id"]),
                    slip_id=slip,
                    note=f"slip {slip}",
                )
    return slip


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
    events = conn.execute(
        """SELECT kind, amount_cny, slip_id FROM bankroll_events
           WHERE bet_id = ?""",
        (bet["id"],),
    ).fetchall()
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
        if (
            bet["status"] == "open"
            and conn.execute(
                "SELECT 1 FROM settlements WHERE bet_id = ?", (bet["id"],)
            ).fetchone()
            is None
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
        stats["still_open"] += int(
            conn.execute(
                """SELECT COUNT(*) FROM bet_slips s
               WHERE EXISTS (SELECT 1 FROM pool_picks p WHERE p.slip_id = s.id)
               AND NOT EXISTS (SELECT 1 FROM settlements st WHERE st.slip_id = s.id)"""
            ).fetchone()[0]
        )
    return stats
