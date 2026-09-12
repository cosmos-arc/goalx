"""投注 API：注级建议、票级回录与复盘列表(票 23)。"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from goalx_backend.api.deps import get_db
from goalx_backend.models import BetMode, LegInput
from goalx_backend.services import BetDraft, create_bet_with_legs, record_purchase
from goalx_backend.store import betting as bt_store

router = APIRouter(tags=["bets"])
DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


class LegPayload(BaseModel):
    """一腿输入。"""

    fixture_id: int
    market_code: str
    selection_code: str
    locked_odds: float = Field(gt=0)
    goal_line: float | None = None


class BetCreate(BaseModel):
    """建注(建议或直接回录)输入。"""

    mode: BetMode
    stake: float = Field(gt=0)
    legs: list[LegPayload] = Field(min_length=1)


class SlipCreate(BaseModel):
    """票级回录输入：勾选实际购买的建议注子集。"""

    bet_ids: list[int] = Field(min_length=1)
    placed_at: str | None = None


class BetLegView(BaseModel):
    """一腿视图。"""

    fixture_id: int
    market_code: str
    selection_code: str
    locked_odds: float
    goal_line: float | None = None


class BetView(BaseModel):
    """一注的复盘视图。"""

    id: int
    slip_id: int | None
    mode: str
    market_kind: str
    purchased: bool
    stake: float
    placed_at: str | None
    created_at: str
    status: str
    payout: float | None
    profit: float | None
    settled_at: str | None
    legs: list[BetLegView]


class SlipView(BaseModel):
    """一张票的聚合视图。"""

    id: int
    mode: str
    placed_at: str | None
    note: str | None
    created_at: str
    bet_count: int
    stake_total: float
    profit_total: float


def _bet_view(row: sqlite3.Row) -> BetView:
    """行 → API 视图。"""
    return BetView(
        id=int(row["id"]),
        slip_id=row["slip_id"],
        mode=str(row["mode"]),
        market_kind=str(row["market_kind"]),
        purchased=bool(row["purchased"]),
        stake=float(row["stake"]),
        placed_at=row["placed_at"],
        created_at=str(row["created_at"]),
        status=str(row["status"]),
        payout=row["payout"],
        profit=row["profit"],
        settled_at=row["settled_at"],
        legs=[BetLegView(**leg) for leg in json.loads(row["legs"])],
    )


@router.post(
    "/api/v1/bets",
    summary="建注(注级建议)",
    status_code=201,
    response_model=BetView,
)
async def create_bet(payload: BetCreate, db: DbDep) -> BetView:
    """创建一注(paper/live；未购建议 purchased=false)。"""
    draft = BetDraft(
        mode=payload.mode,
        stake=payload.stake,
        legs=[
            LegInput(
                fixture_id=leg.fixture_id,
                market_code=leg.market_code,
                selection_code=leg.selection_code,
                locked_odds=leg.locked_odds,
                goal_line=leg.goal_line,
            )
            for leg in payload.legs
        ],
    )
    try:
        bet_id = create_bet_with_legs(db, draft)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = bt_store.get_bet(db, bet_id)
    if row is None:
        raise HTTPException(status_code=500, detail="bet vanished after insert")
    return _bet_view(row)


@router.get(
    "/api/v1/bets",
    summary="复盘列表(注级)",
    response_model=list[BetView],
)
async def list_bets(
    db: DbDep, mode: BetMode | None = None, only_open: bool = False
) -> list[BetView]:
    """全部投注记录：结算状态、盈亏、未购标记。"""
    return [
        _bet_view(row) for row in bt_store.list_bets(db, mode=mode, only_open=only_open)
    ]


@router.post(
    "/api/v1/bet-slips",
    summary="票级回录",
    status_code=201,
    response_model=SlipView,
)
async def create_slip(payload: SlipCreate, db: DbDep) -> SlipView:
    """把勾选的建议注合成一张实际投注票并标记已购。"""
    try:
        slip_id = record_purchase(db, payload.bet_ids, payload.placed_at)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = next((r for r in bt_store.list_slips(db) if r["id"] == slip_id), None)
    if row is None:
        raise HTTPException(status_code=500, detail="slip vanished after insert")
    return _slip_view(row)


def _slip_view(row: sqlite3.Row) -> SlipView:
    """票行 → API 视图。"""
    return SlipView(
        id=int(row["id"]),
        mode=str(row["mode"]),
        placed_at=row["placed_at"],
        note=row["note"],
        created_at=str(row["created_at"]),
        bet_count=int(row["bet_count"]),
        stake_total=float(row["stake_total"]),
        profit_total=float(row["profit_total"]),
    )


@router.get(
    "/api/v1/bet-slips",
    summary="票列表",
    response_model=list[SlipView],
)
async def list_slips(db: DbDep) -> list[SlipView]:
    """全部投注票及聚合。"""
    return [_slip_view(row) for row in bt_store.list_slips(db)]
