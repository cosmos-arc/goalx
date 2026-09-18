"""Bet 生命周期用例：建注（同场串关校验）、资格判定与购买回录（票级，live 扣款）。"""

from __future__ import annotations

import json
import sqlite3

from pydantic import BaseModel, Field

from goalx_backend.betting import store as bt_store
from goalx_backend.betting.snapshot import build_ev_snapshot
from goalx_backend.data.quote_evidence import adjudicate_had_quote
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.models import BetMode, LegInput, MarketKind


class SameFixtureParlayError(ValueError):
    """竞彩禁止同场串关（研究 01）。"""


class BetEligibilityError(ValueError):
    """had 选择/锁定不满足服务端共享证据资格（票 36；界面禁用不替代本判定）。"""


class BetDraft(BaseModel):
    """一注建议/回录的输入（API 反序列化后）。"""

    mode: BetMode
    stake: float = Field(gt=0, allow_inf_nan=False)
    legs: list[LegInput] = Field(min_length=1)
    strategy_version: str | None = None


class ActualLegOdds(BaseModel):
    """真实回录时一腿的实际赔率（按 fixture 定位；一注内 fixture 不重复）。"""

    fixture_id: int
    odds: float = Field(gt=0, allow_inf_nan=False)


class ActualTerms(BaseModel):
    """一注的实际执行条款；结算按实际条款，原建议快照保留可追溯。"""

    stake: float = Field(gt=0, allow_inf_nan=False)
    leg_odds: list[ActualLegOdds] = Field(default_factory=list)


def create_bet_with_legs(conn: sqlite3.Connection, draft: BetDraft) -> int:
    """
    建注（含校验：竞彩禁止同场串关）；返回 bet id。

    建注即落注级 EV 快照（票 41）：锁定时刻的欧共识/模型双口径概率与
    EV——口径不可得存 None，事后不倒填。
    """
    seen: set[int] = set()
    for leg in draft.legs:
        if leg.fixture_id in seen:
            raise SameFixtureParlayError(f"fixture {leg.fixture_id} 重复出现在串关中")
        seen.add(leg.fixture_id)
    with atomic(conn):
        snapshot = build_ev_snapshot(conn, draft.legs)
        bet = bt_store.create_bet(
            conn,
            draft.mode,
            MarketKind.FIXED,
            draft.stake,
            strategy_version=draft.strategy_version,
            ev_snapshot=snapshot,
        )
        for leg in draft.legs:
            bt_store.add_leg(conn, bet, leg)
    return bet


def validate_had_selections(
    conn: sqlite3.Connection,
    legs: list[LegInput],
    *,
    as_of: str | None = None,
) -> None:
    """
    Had 选择资格（票 36 选择入口/锁定再校验，消费票 35 判定）：

    - 任一 had 腿判定 rejected（停售/已开赛/三向不全/源过期/场次不存在）→ 拒绝；
    - 单关（仅一腿且为 had）须 single_eligible 为 True，未知按拒绝；
    - 串关在同一 as_of 判定全部腿，即共同可购买时点；非 had 市场暂不在
      证据契约内，不判定（保持既有行为）。
    """
    moment = as_of or utc_now_iso()
    had_legs = [leg for leg in legs if leg.market_code == "had"]
    problems: list[str] = []
    for leg in had_legs:
        verdict = adjudicate_had_quote(conn, leg.fixture_id, moment)
        if verdict.status == "rejected":
            problems.append(
                f"fixture {leg.fixture_id} 不可投: {', '.join(verdict.reasons)}"
            )
        elif (
            len(had_legs) == 1
            and len(legs) == 1
            and verdict.single_eligible is not True
        ):
            problems.append(
                f"fixture {leg.fixture_id} 非单固(单关须 single_eligible=true)"
            )
    if problems:
        raise BetEligibilityError("; ".join(problems))


def _apply_actual_terms(
    conn: sqlite3.Connection, bet: sqlite3.Row, actual: ActualTerms
) -> float:
    """记录一注的实际执行条款并返回有效注金（建议快照不动）。"""
    leg_fixtures = {int(leg["fixture_id"]) for leg in json.loads(bet["legs"])}
    unknown_legs = {o.fixture_id for o in actual.leg_odds} - leg_fixtures
    if unknown_legs:
        raise ValueError(
            f"bet {bet['id']} actuals 引用了不存在的腿 fixture: {sorted(unknown_legs)}"
        )
    bt_store.set_actual_terms(
        conn,
        int(bet["id"]),
        actual.stake,
        {o.fixture_id: o.odds for o in actual.leg_odds},
    )
    return actual.stake


def _load_purchasable_bets(
    conn: sqlite3.Connection, bet_ids: list[int]
) -> list[sqlite3.Row]:
    """取回可回录的建议注（存在、未购、未结、无资金/结算记录、固定奖金）。"""
    bets: list[sqlite3.Row] = []
    for bet_id in bet_ids:
        bet = bt_store.get_bet(conn, bet_id)
        if bet is None:
            raise LookupError(f"bet {bet_id} 不存在")
        if bet["purchased"] or bet["slip_id"] is not None or bet["status"] != "open":
            raise ValueError(f"bet {bet_id} 已购、已绑定或已结, 不能再次回录")
        if bt_store.has_settlement_or_bankroll_event(conn, bet_id):
            raise ValueError(f"bet {bet_id} 已有结算或资金记录, 请人工核查")
        if bet["market_kind"] != "fixed":
            raise ValueError("奖池奖金尚未支持, 不能回录")
        bets.append(bet)
    return bets


def record_purchase(
    conn: sqlite3.Connection,
    bet_ids: list[int],
    placed_at: str | None = None,
    actuals: dict[int, ActualTerms] | None = None,
) -> int:
    """票级回录：勾选实际购买子集，生成一张票；live 注按实际条款扣减 bankroll。"""
    if not bet_ids or len(set(bet_ids)) != len(bet_ids):
        raise ValueError("bet_ids 必须非空且不能重复")
    terms = actuals or {}
    with atomic(conn):
        at = placed_at or utc_now_iso()
        bets = _load_purchasable_bets(conn, bet_ids)
        modes = {str(bet["mode"]) for bet in bets}
        if len(modes) != 1:
            raise ValueError("一张票内 mode 必须一致")
        mode = BetMode(modes.pop())
        unknown = set(terms) - set(bet_ids)
        if unknown:
            raise ValueError(f"actuals 引用了不在本票内的 bet: {sorted(unknown)}")
        slip = bt_store.create_slip(conn, mode, placed_at=at)
        bt_store.attach_bets_to_slip(conn, slip, bet_ids, at)
        for bet in bets:
            bet_id = int(bet["id"])
            actual = terms.get(bet_id)
            effective_stake = (
                _apply_actual_terms(conn, bet, actual)
                if actual is not None
                else float(bet["stake"])
            )
            if mode is BetMode.LIVE:
                bt_store.record_bankroll_event(
                    conn,
                    "bet_stake",
                    -effective_stake,
                    bet_id=bet_id,
                    slip_id=slip,
                    note=f"slip {slip}",
                )
    return slip
