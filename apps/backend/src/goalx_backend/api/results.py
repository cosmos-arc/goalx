"""事实与结算 API：开奖导入、结算批跑、bankroll(票 23)。"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from goalx_backend.api.deps import get_db
from goalx_backend.ingest.results import import_draw_results
from goalx_backend.models import DrawResultInput
from goalx_backend.services import run_settlement
from goalx_backend.store import betting as bt_store
from goalx_backend.store import fixtures as fx_store
from goalx_backend.store import results as rs_store

router = APIRouter(tags=["results"])
DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


class DrawResultPayload(BaseModel):
    """一条开奖结果输入。"""

    fixture_id: int
    home_goals: int = Field(ge=0)
    away_goals: int = Field(ge=0)
    half_home_goals: int | None = Field(default=None, ge=0)
    half_away_goals: int | None = Field(default=None, ge=0)
    void: bool = False
    void_reason: str | None = None
    published_at: str | None = None
    correction_reason: str | None = None


class DrawResultImport(BaseModel):
    """批量导入请求。"""

    results: list[DrawResultPayload] = Field(min_length=1)
    source: str = "manual"


class DrawResultView(BaseModel):
    """一条开奖结果。"""

    fixture_id: int
    home_goals: int
    away_goals: int
    half_home_goals: int | None
    half_away_goals: int | None
    void: bool
    void_reason: str | None
    source: str
    published_at: str | None


class ImportResultView(BaseModel):
    """导入统计。"""

    imported: int


class SettlementRunResponse(BaseModel):
    """结算批跑统计。"""

    settled: int
    still_open: int
    won: int
    lost: int
    void: int


class BankrollEventView(BaseModel):
    """一条 bankroll 流水。"""

    id: int
    occurred_at: str
    kind: str
    amount_cny: float
    balance_after: float
    bet_id: int | None
    note: str | None


class BankrollResponse(BaseModel):
    """资金页数据。"""

    balance: float | None
    events: list[BankrollEventView]


@router.post(
    "/api/v1/draw-results",
    summary="导入官方开奖(唯一事实源)",
    status_code=201,
    responses={
        400: {"description": "更正缺少原因或账务需人工核查"},
        404: {"description": "比赛不存在"},
    },
)
async def create_draw_results(payload: DrawResultImport, db: DbDep) -> ImportResultView:
    """批量导入开奖结果; 更正需原因, 保留历史并原子重算与冲正。"""
    for item in payload.results:
        if fx_store.get_fixture(db, item.fixture_id) is None:
            raise HTTPException(
                status_code=404, detail=f"fixture {item.fixture_id} not found"
            )
    try:
        imported = import_draw_results(
            db,
            [
                DrawResultInput(
                    fixture_id=item.fixture_id,
                    home_goals=item.home_goals,
                    away_goals=item.away_goals,
                    half_home_goals=item.half_home_goals,
                    half_away_goals=item.half_away_goals,
                    void=item.void,
                    void_reason=item.void_reason,
                    source=payload.source,
                    published_at=item.published_at,
                    correction_reason=item.correction_reason,
                )
                for item in payload.results
            ],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ImportResultView(imported=imported)


@router.get(
    "/api/v1/draw-results",
    summary="查询开奖结果",
    response_model=list[DrawResultView],
)
async def list_draw_results(
    db: DbDep, fixture_id: int | None = None
) -> list[DrawResultView]:
    """已导入的开奖结果(可按场次过滤)。"""
    if fixture_id is not None:
        row = rs_store.get_draw_result(db, fixture_id)
        rows = [row] if row is not None else []
    else:
        rows = db.execute("SELECT * FROM draw_results ORDER BY fixture_id").fetchall()
    return [
        DrawResultView(
            fixture_id=int(row["fixture_id"]),
            home_goals=int(row["home_goals"]),
            away_goals=int(row["away_goals"]),
            half_home_goals=row["half_home_goals"],
            half_away_goals=row["half_away_goals"],
            void=bool(row["void"]),
            void_reason=row["void_reason"],
            source=str(row["source"]),
            published_at=row["published_at"],
        )
        for row in rows
    ]


@router.post(
    "/api/v1/settlements/run",
    summary="结算批跑",
    responses={400: {"description": "旧账异常, 需人工核查"}},
)
async def run_settlements(db: DbDep) -> SettlementRunResponse:
    """对已开赛且有赛果的未结注执行官方规则结算。"""
    try:
        stats = run_settlement(db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SettlementRunResponse(**stats)


@router.get("/api/v1/bankroll", summary="资金池状态")
async def get_bankroll(db: DbDep) -> BankrollResponse:
    """Bankroll 余额与最近变动(仅 live 模式影响，ADR 0002)。"""
    events = [
        BankrollEventView(
            id=int(row["id"]),
            occurred_at=str(row["occurred_at"]),
            kind=str(row["kind"]),
            amount_cny=float(row["amount_cny"]),
            balance_after=float(row["balance_after"]),
            bet_id=row["bet_id"],
            note=row["note"],
        )
        for row in bt_store.list_bankroll_events(db)
    ]
    return BankrollResponse(balance=bt_store.bankroll_balance(db), events=events)
