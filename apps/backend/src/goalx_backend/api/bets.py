"""投注 API：注级建议、票级回录与复盘列表(票 23/36)。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from goalx_backend.api.deps import get_db
from goalx_backend.betting import store as bt_store
from goalx_backend.betting.bets import (
    ActualLegOdds,
    ActualTerms,
    BetDraft,
    BetEligibilityError,
    create_bet_with_legs,
    record_purchase,
    validate_had_selections,
)
from goalx_backend.data import fixtures as fx_store
from goalx_backend.db import atomic
from goalx_backend.evaluation import clv as clv_mod
from goalx_backend.models import BetMode, LegInput

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
    strategy_version: str | None = None


class ActualLegOddsPayload(BaseModel):
    """真实回录一腿的实际赔率。"""

    fixture_id: int
    odds: float = Field(gt=0)


class ActualTermsPayload(BaseModel):
    """一注的实际执行条款（结算按实际，建议快照保留）。"""

    stake: float = Field(gt=0)
    leg_odds: list[ActualLegOddsPayload] = Field(default_factory=list)


class SlipCreate(BaseModel):
    """票级回录输入：勾选实际购买的建议注子集；live 可附实际条款。"""

    bet_ids: list[int] = Field(min_length=1)
    placed_at: str | None = None
    actuals: dict[str, ActualTermsPayload] = Field(default_factory=dict)


class PoolPickPayload(BaseModel):
    """复式票一格的一选。"""

    match_seq: int
    selection_code: str
    fixture_id: int | None = None


class PoolSlipCreate(BaseModel):
    """创建池票（任9/14 场复式）：picks 笛卡尔积 materialize 为组合。"""

    mode: BetMode
    pool_period_id: int | None = None
    note: str | None = None
    stake_per_combination: float = Field(default=2.0, gt=0)
    picks: list[PoolPickPayload] = Field(min_length=1)


class BetLegView(BaseModel):
    """一腿视图。"""

    fixture_id: int
    market_code: str
    selection_code: str
    locked_odds: float
    actual_odds: float | None = None
    goal_line: float | None = None


class BetReviewView(BaseModel):
    """复盘资格摘要（票 36）：前瞻纳入/排除原因与 closing 完整性。"""

    locked_pre_kickoff: bool | None = None
    closing_present: bool | None = None
    # included | excluded_unlocked | excluded_post_kickoff
    # | live_separate | missing_closing | unknown
    forward: str


class BetView(BaseModel):
    """一注的复盘视图。"""

    id: int
    slip_id: int | None
    mode: str
    market_kind: str
    purchased: bool
    stake: float
    actual_stake: float | None = None
    strategy_version: str | None = None
    placed_at: str | None
    locked_at: str | None = None
    created_at: str
    status: str
    payout: float | None
    profit: float | None
    settled_at: str | None
    legs: list[BetLegView]
    review: BetReviewView | None = None


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


def _parse_ts(value: str) -> datetime:
    """ISO 串 → aware datetime（naive 按 UTC；仅复盘比较用）。"""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _review_view(
    conn: sqlite3.Connection, rows: list[sqlite3.Row]
) -> dict[int, BetReviewView]:
    """注级复盘资格：服务器记录的锁定时点 vs 开赛；closing 完整性（票 36）。"""
    fixture_ids = sorted(
        {int(leg["fixture_id"]) for row in rows for leg in json.loads(row["legs"])}
    )
    kickoffs = fx_store.kickoffs_for_fixtures(conn, fixture_ids)
    closing = clv_mod.closing_leg_counts(conn)
    views: dict[int, BetReviewView] = {}
    for row in rows:
        bet_id = int(row["id"])
        leg_fixtures = [int(leg["fixture_id"]) for leg in json.loads(row["legs"])]
        leg_kickoffs = [kickoffs[fid] for fid in leg_fixtures if fid in kickoffs]
        locked_at = row["locked_at"]
        pre_kickoff = (
            all(_parse_ts(locked_at) < _parse_ts(k) for k in leg_kickoffs)
            if locked_at and leg_kickoffs
            else None
        )
        closing_present = (
            closing.get(bet_id, 0) >= len(leg_fixtures) if leg_fixtures else None
        )
        if not row["purchased"]:
            forward = "excluded_unlocked"
        elif locked_at is None:
            forward = "unknown"
        elif str(row["mode"]) == "live":
            forward = "live_separate"
        elif pre_kickoff is not True:
            forward = "excluded_post_kickoff"
        elif closing_present is not True:
            forward = "missing_closing"
        else:
            forward = "included"
        views[bet_id] = BetReviewView(
            locked_pre_kickoff=pre_kickoff,
            closing_present=closing_present,
            forward=forward,
        )
    return views


def _bet_view(row: sqlite3.Row, review: BetReviewView | None = None) -> BetView:
    """行 → API 视图。"""
    return BetView(
        id=int(row["id"]),
        slip_id=row["slip_id"],
        mode=str(row["mode"]),
        market_kind=str(row["market_kind"]),
        purchased=bool(row["purchased"]),
        stake=float(row["stake"]),
        actual_stake=(
            float(row["actual_stake"]) if row["actual_stake"] is not None else None
        ),
        strategy_version=row["strategy_version"],
        placed_at=row["placed_at"],
        locked_at=row["locked_at"],
        created_at=str(row["created_at"]),
        status=str(row["status"]),
        payout=row["payout"],
        profit=row["profit"],
        settled_at=row["settled_at"],
        legs=[BetLegView(**leg) for leg in json.loads(row["legs"])],
        review=review,
    )


@router.post(
    "/api/v1/bets",
    summary="建注(注级建议; 服务端校验 had 资格)",
    status_code=201,
    response_model=BetView,
    responses={
        400: {"description": "非法串关(同场多腿)或 had 资格不满足(停售/已开赛/非单固)"}
    },
)
async def create_bet(payload: BetCreate, db: DbDep) -> BetView:
    """创建一注(paper/live；未购建议 purchased=false)；had 腿按共享证据判定资格。"""
    legs = [
        LegInput(
            fixture_id=leg.fixture_id,
            market_code=leg.market_code,
            selection_code=leg.selection_code,
            locked_odds=leg.locked_odds,
            goal_line=leg.goal_line,
        )
        for leg in payload.legs
    ]
    try:
        validate_had_selections(db, legs)
        bet_id = create_bet_with_legs(
            db,
            BetDraft(
                mode=payload.mode,
                stake=payload.stake,
                legs=legs,
                strategy_version=payload.strategy_version,
            ),
        )
    except BetEligibilityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = bt_store.get_bet(db, bet_id)
    if row is None:
        raise HTTPException(status_code=500, detail="bet vanished after insert")
    return _bet_view(row, _review_view(db, [row]).get(bet_id))


@router.get(
    "/api/v1/bets",
    summary="复盘列表(注级; 含前瞻资格与 closing 完整性)",
    response_model=list[BetView],
)
async def list_bets(
    db: DbDep, mode: BetMode | None = None, only_open: bool = False
) -> list[BetView]:
    """全部投注记录：结算状态、盈亏、建议 vs 实际条款、复盘资格。"""
    rows = bt_store.list_bets(db, mode=mode, only_open=only_open)
    reviews = _review_view(db, rows)
    return [_bet_view(row, reviews.get(int(row["id"]))) for row in rows]


@router.post(
    "/api/v1/bet-slips",
    summary="票级回录(paper 提交时重新校验停售/过期; live 可附实际条款)",
    status_code=201,
    response_model=SlipView,
    responses={
        400: {
            "description": "mode 混用或 paper 锁定 had 资格不满足(停售/已开赛/非单固)"
        },
        404: {"description": "bet 不存在"},
    },
)
async def create_slip(payload: SlipCreate, db: DbDep) -> SlipView:
    """
    把勾选的建议注合成一张票并标记已购。

    paper 锁定是正式赛前决策，服务器按共享证据再校验停售/过期；live
    回录是事后记账（赛后仍可入账），由前瞻资格规则排除，不再校验。
    """
    actuals: dict[int, ActualTerms] = {
        int(bet_id): ActualTerms(
            stake=terms.stake,
            leg_odds=[
                ActualLegOdds(fixture_id=leg.fixture_id, odds=leg.odds)
                for leg in terms.leg_odds
            ],
        )
        for bet_id, terms in payload.actuals.items()
    }
    try:
        bets_by_id: dict[int, sqlite3.Row] = {}
        for bet_id in payload.bet_ids:
            row = bt_store.get_bet(db, bet_id)
            if row is None:
                raise LookupError(f"bet {bet_id} 不存在")
            bets_by_id[bet_id] = row
        if all(str(row["mode"]) == "paper" for row in bets_by_id.values()):
            for row in bets_by_id.values():
                validate_had_selections(
                    db,
                    [
                        LegInput(
                            fixture_id=int(leg["fixture_id"]),
                            market_code=str(leg["market_code"]),
                            selection_code=str(leg["selection_code"]),
                            locked_odds=float(leg["locked_odds"]),
                            goal_line=leg["goal_line"],
                        )
                        for leg in json.loads(row["legs"])
                    ],
                )
        slip_id = record_purchase(
            db, payload.bet_ids, payload.placed_at, actuals=actuals
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BetEligibilityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = bt_store.get_slip(db, slip_id)
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


@router.post(
    "/api/v1/pool-slips",
    summary="创建待结算的 paper 池票草稿",
    responses={400: {"description": "暂不支持 live 奖池票"}},
    status_code=201,
    response_model=SlipView,
)
async def create_pool_slip(payload: PoolSlipCreate, db: DbDep) -> SlipView:
    """按 picks 建 paper 复式草稿; 奖金引擎未实现, 保持待结算。"""
    if payload.mode is BetMode.LIVE:
        raise HTTPException(
            status_code=400, detail="奖池奖金未实现, 暂不支持 live 池票"
        )
    with atomic(db):
        slip = bt_store.create_slip(
            db,
            payload.mode,
            pool_period_id=payload.pool_period_id,
            note=payload.note,
            source="manual",
        )
        for pick in payload.picks:
            bt_store.add_pool_pick(
                db,
                slip,
                pick.match_seq,
                pick.selection_code,
                fixture_id=pick.fixture_id,
            )
        bt_store.materialize_combinations(
            db, slip, stake_per_combination=payload.stake_per_combination
        )
    row = bt_store.get_slip(db, slip)
    if row is None:
        raise HTTPException(status_code=500, detail="slip vanished after insert")
    return _slip_view(row)
