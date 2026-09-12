"""服务层：今日页视图、结算批跑与投注生命周期（API 与 flows 共用）。"""

from __future__ import annotations

import json
import sqlite3

from pydantic import BaseModel, Field

from goalx_backend import odds_math as om
from goalx_backend.db import utc_now_iso
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
    detail_payload,
    settle_fixed_bet,
    settle_pool_combination,
)
from goalx_backend.store import betting as bt_store
from goalx_backend.store import fixtures as fx_store
from goalx_backend.store import results as rs_store

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
    stake: float
    legs: list[LegInput]


def create_bet_with_legs(conn: sqlite3.Connection, draft: BetDraft) -> int:
    """建注（含校验：竞彩禁止同场串关）；返回 bet id。"""
    seen: set[int] = set()
    for leg in draft.legs:
        if leg.fixture_id in seen:
            raise SameFixtureParlayError(f"fixture {leg.fixture_id} 重复出现在串关中")
        seen.add(leg.fixture_id)
    bet = bt_store.create_bet(conn, draft.mode, MarketKind.FIXED, draft.stake)
    for leg in draft.legs:
        bt_store.add_leg(conn, bet, leg)
    conn.commit()
    return bet


def record_purchase(
    conn: sqlite3.Connection, bet_ids: list[int], placed_at: str | None = None
) -> int:
    """票级回录：勾选实际购买子集，生成一张票；live 注扣减 bankroll。"""
    at = placed_at or utc_now_iso()
    bets = [bt_store.get_bet(conn, bet_id) for bet_id in bet_ids]
    if any(bet is None for bet in bets):
        raise LookupError("bet id 不存在")
    modes = {bet["mode"] for bet in bets if bet is not None}
    if len(modes) != 1:
        raise ValueError("一张票内 mode 必须一致")
    mode = BetMode(modes.pop())
    slip = bt_store.create_slip(conn, mode, placed_at=at)
    bt_store.attach_bets_to_slip(conn, slip, bet_ids, at)
    if mode is BetMode.LIVE:
        for bet in bets:
            if bet is None:
                raise LookupError("bet id 不存在")
            bt_store.record_bankroll_event(
                conn,
                "bet_stake",
                -float(bet["stake"]),
                bet_id=int(bet["id"]),
                note=f"slip {slip}",
            )
    conn.commit()
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


def _settle_one_bet(conn: sqlite3.Connection, bet: sqlite3.Row) -> str:
    """结算一注（竞彩固定赔率）；返回结算后状态。"""
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
    outcome = settle_fixed_bet(float(bet["stake"]), leg_specs, results)
    if not outcome.settled:
        return "open"
    bt_store.save_settlement(
        conn,
        SettlementInput(
            bet_id=int(bet["id"]),
            status=BetStatus(outcome.status),
            stake=outcome.stake,
            payout=outcome.payout,
            profit=outcome.profit,
            detail=detail_payload(outcome),
        ),
    )
    if bet["mode"] == "live" and outcome.payout > 0:
        bt_store.record_bankroll_event(
            conn,
            "bet_payout",
            outcome.payout,
            bet_id=int(bet["id"]),
            note="settlement",
        )
    return outcome.status


def _settle_pool_slip(conn: sqlite3.Connection, slip_id: int) -> str:
    """结算一张池票：materialize 组合并逐个判定（票 23）。"""
    combos = conn.execute(
        "SELECT id, selections FROM combinations WHERE slip_id = ? ORDER BY seq",
        (slip_id,),
    ).fetchall()
    if not combos:
        bt_store.materialize_combinations(conn, slip_id)
        combos = conn.execute(
            "SELECT id, selections FROM combinations WHERE slip_id = ? ORDER BY seq",
            (slip_id,),
        ).fetchall()
    seq_to_fixture = {
        int(r["match_seq"]): (
            int(r["fixture_id"]) if r["fixture_id"] is not None else None
        )
        for r in conn.execute(
            "SELECT match_seq, fixture_id FROM pool_picks WHERE slip_id = ?", (slip_id,)
        )
    }
    all_fixture_ids = {fid for fid in seq_to_fixture.values() if fid is not None}
    results = {
        int(fid): _result_facts(row)
        for fid, row in rs_store.draw_results_for_fixtures(
            conn, sorted(all_fixture_ids)
        ).items()
    }
    if not results:
        return "open"
    hit_count = 0
    for row in combos:
        selections = json.loads(row["selections"])
        mapping: dict[int, str] = {}
        unmapped = False
        for pick in selections:
            fixture = seq_to_fixture.get(int(pick["match_seq"]))
            if fixture is None:
                unmapped = True
                break
            mapping[fixture] = str(pick["selection_code"])
        if unmapped:
            return "open"  # 场次未映射，无法判定
        hit, _notes = settle_pool_combination(mapping, results)
        if hit is None:
            return "open"
        if hit:
            hit_count += 1
    status = BetStatus.WON if hit_count else BetStatus.LOST
    bt_store.save_settlement(
        conn,
        SettlementInput(
            slip_id=slip_id,
            status=status,
            stake=0.0,
            payout=0.0,
            profit=0.0,
            detail={
                "combinations": len(combos),
                "hits": hit_count,
                "note": "prize_awaiting_official_pool_allocation",
            },
        ),
    )
    return status.value


def run_settlement(conn: sqlite3.Connection) -> dict[str, int]:
    """结算批跑：所有已开赛且有赛果的未结注/池票；返回统计。"""
    stats = {"settled": 0, "still_open": 0, "won": 0, "lost": 0, "void": 0}
    for bet in bt_store.list_bets(conn, only_open=True):
        status = _settle_one_bet(conn, bet)
        if status == "open":
            stats["still_open"] += 1
        else:
            stats["settled"] += 1
            stats[status] += 1
    slip_rows = conn.execute(
        """
        SELECT s.id FROM bet_slips s
        WHERE EXISTS (SELECT 1 FROM pool_picks p WHERE p.slip_id = s.id)
        AND NOT EXISTS (SELECT 1 FROM settlements st WHERE st.slip_id = s.id)
        """
    ).fetchall()
    for row in slip_rows:
        status = _settle_pool_slip(conn, int(row["id"]))
        if status == "open":
            stats["still_open"] += 1
        else:
            stats["settled"] += 1
            stats[status] = stats.get(status, 0) + 1
    conn.commit()
    return stats
