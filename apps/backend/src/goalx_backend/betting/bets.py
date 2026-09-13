"""Bet 生命周期用例：建注（同场串关校验）与购买回录（票级，live 扣款）。"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, Field

from goalx_backend.betting import store as bt_store
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.models import BetMode, LegInput, MarketKind


class SameFixtureParlayError(ValueError):
    """竞彩禁止同场串关（研究 01）。"""


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
            if bt_store.has_settlement_or_bankroll_event(conn, bet_id):
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
