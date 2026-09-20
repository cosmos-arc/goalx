"""事实与结算 API：开奖导入、影响预览、结算批跑、bankroll 与成本摘要(票 23/36)。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError

from goalx_backend.api.deps import get_db
from goalx_backend.betting import store as bt_store
from goalx_backend.betting.settle import preview_draw_result_change, run_settlement
from goalx_backend.config import Settings, get_settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs_store
from goalx_backend.data.ingest import uniform
from goalx_backend.data.ingest.oddsapi import polite_client
from goalx_backend.data.ingest.results import import_draw_results
from goalx_backend.models import DrawResultInput

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


class DrawResultChangeView(BaseModel):
    """预览里一条结果变化。"""

    fixture_id: int
    is_correction: bool
    previous: dict[str, Any] | None = None
    replacement: dict[str, Any]


class AffectedBetPreviewView(BaseModel):
    """预览里一注受影响注的当前 vs 投影结算。"""

    bet_id: int
    mode: str
    purchased: bool
    status_current: str
    payout_current: float | None
    status_projected: str
    payout_projected: float | None
    delta_payout: float | None


class DrawResultPreviewResponse(BaseModel):
    """开奖导入影响预览(只读；导入才执行重算与冲正)。"""

    results: list[DrawResultChangeView]
    affected_bets: list[AffectedBetPreviewView]


class CostItemView(BaseModel):
    """一类已记账成本。"""

    category: str
    units: float
    amount_cny: float
    entries: int


class CostSummaryView(BaseModel):
    """期间成本摘要(金额与 credits 分列；仅统计已记账成本)。"""

    since: str | None = None
    total_cny: float
    credits_used: float
    items: list[CostItemView]


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


class DepositPayload(BaseModel):
    """一笔入金输入。"""

    amount_cny: float = Field(gt=0, description="入金金额(人民币, 必须 > 0)")
    occurred_at: str | None = Field(
        default=None, description="发生时点(UTC ISO); 缺省取服务器当前时间"
    )
    note: str | None = None


class DepositCreatedView(BaseModel):
    """入金结果：新流水事件与入金后余额。"""

    event: BankrollEventView
    balance: float


class DrawSyncPendingItem(BaseModel):
    """待人工清单条目（票 42：无效场次/对不上/与库内不一致等 fail-closed 场次）。"""

    business_date: str
    code: str
    reason: str


class DrawSyncRunView(BaseModel):
    """一次赛果同步的元信息（来源/时点/场次数/待人工清单）。"""

    source: str
    observed_at: str
    business_dates: list[str]
    pages: int
    fetched: int
    imported: int
    unchanged: int
    unmatched: int
    pending_manual: list[DrawSyncPendingItem]


class DrawSyncStatusView(BaseModel):
    """同步状态（票 42）：上次同步 + 当前待出赛果数；从未同步时 last_run 为空。"""

    last_run: DrawSyncRunView | None
    pending_results: int = Field(description="已开赛、尚无开奖的竞彩场次数")


def _json_list(raw: str) -> list[Any]:
    """JSON 文本 → list（解析失败/非 list 按空值处理，fail-open 只影响展示）。"""
    try:
        value: object = json.loads(raw)
    except ValueError:
        return []
    if isinstance(value, list):
        return cast(list[Any], value)
    return []


def _pending_items(raw: str) -> list[DrawSyncPendingItem]:
    """JSON 文本 → 待人工清单（畸形条目跳过，不影响其余展示）。"""
    items: list[DrawSyncPendingItem] = []
    for entry in _json_list(raw):
        try:
            items.append(DrawSyncPendingItem.model_validate(entry))
        except ValidationError:
            continue
    return items


def _run_view(row: sqlite3.Row) -> DrawSyncRunView:
    """draw_sync_runs 行 → 视图。"""
    return DrawSyncRunView(
        source=str(row["source"]),
        observed_at=str(row["observed_at"]),
        business_dates=[str(item) for item in _json_list(str(row["business_dates"]))],
        pages=int(row["pages"]),
        fetched=int(row["fetched"]),
        imported=int(row["imported"]),
        unchanged=int(row["unchanged"]),
        unmatched=int(row["unmatched"]),
        pending_manual=_pending_items(str(row["pending_manual"])),
    )


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
            db, [_to_input(item, payload.source) for item in payload.results]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ImportResultView(imported=imported)


def _to_input(item: DrawResultPayload, source: str) -> DrawResultInput:
    """API 载荷 → 仓储输入。"""
    return DrawResultInput(
        fixture_id=item.fixture_id,
        home_goals=item.home_goals,
        away_goals=item.away_goals,
        half_home_goals=item.half_home_goals,
        half_away_goals=item.half_away_goals,
        void=item.void,
        void_reason=item.void_reason,
        source=source,
        published_at=item.published_at,
        correction_reason=item.correction_reason,
    )


@router.post(
    "/api/v1/draw-results/preview",
    summary="开奖导入影响预览(只读)",
    response_model=DrawResultPreviewResponse,
    responses={404: {"description": "比赛不存在"}},
)
async def preview_draw_results(
    payload: DrawResultImport, db: DbDep
) -> DrawResultPreviewResponse:
    """预览结果变化与受影响注的当前 vs 投影结算; 不落库、不冲正。"""
    for item in payload.results:
        if fx_store.get_fixture(db, item.fixture_id) is None:
            raise HTTPException(
                status_code=404, detail=f"fixture {item.fixture_id} not found"
            )
    preview = preview_draw_result_change(
        db, [_to_input(item, payload.source) for item in payload.results]
    )
    return DrawResultPreviewResponse(
        results=[
            DrawResultChangeView(
                fixture_id=change.fixture_id,
                is_correction=change.is_correction,
                previous=change.previous,
                replacement=change.replacement,
            )
            for change in preview.results
        ],
        affected_bets=[
            AffectedBetPreviewView(
                bet_id=bet.bet_id,
                mode=bet.mode,
                purchased=bet.purchased,
                status_current=bet.status_current,
                payout_current=bet.payout_current,
                status_projected=bet.status_projected,
                payout_projected=bet.payout_projected,
                delta_payout=bet.delta_payout,
            )
            for bet in preview.affected_bets
        ],
    )


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
        rows = rs_store.list_draw_results(db)
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
    "/api/v1/draw-sync/run",
    summary="触发一次赛果自动同步(官方 sporttery uniform 源)",
    response_model=DrawSyncStatusView,
    responses={502: {"description": "同步源不可达或返回异常"}},
)
async def run_draw_sync(request: Request, db: DbDep) -> DrawSyncStatusView:
    """
    同步待出赛果(已开赛、无开奖的竞彩场次, 近 7 天窗口)。

    官方终态(比分或无效判定)落库, 拒因行进待人工清单;
    与库内不一致不自动冲正(人工兜底通道, ADR 0001)。
    """
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    now = datetime.now(UTC)
    try:
        with polite_client() as client:
            uniform.sync_uniform_results(db, settings, client, now=now)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"draw sync source error: {exc}"
        ) from exc
    row = uniform.latest_sync_run(db)
    return DrawSyncStatusView(
        last_run=_run_view(row) if row is not None else None,
        pending_results=uniform.pending_result_count(db, now=now),
    )


@router.get(
    "/api/v1/draw-sync/status",
    summary="赛果同步状态",
    response_model=DrawSyncStatusView,
)
async def get_draw_sync_status(db: DbDep) -> DrawSyncStatusView:
    """上次同步元信息与当前待出赛果数; 从未同步时 last_run 为空。"""
    now = datetime.now(UTC)
    row = uniform.latest_sync_run(db)
    return DrawSyncStatusView(
        last_run=_run_view(row) if row is not None else None,
        pending_results=uniform.pending_result_count(db, now=now),
    )


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


@router.post(
    "/api/v1/bankroll/deposits",
    summary="记录入金(live 资金)",
    status_code=201,
    response_model=DepositCreatedView,
    responses={400: {"description": "入金金额非法(≤0)"}},
)
async def create_deposit(payload: DepositPayload, db: DbDep) -> DepositCreatedView:
    """记录一笔入金(kind=deposit)；返回入金事件与最新余额。无鉴权(单用户产品既定)。"""
    try:
        row = bt_store.record_deposit(
            db,
            payload.amount_cny,
            note=payload.note,
            occurred_at=payload.occurred_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    event = BankrollEventView(
        id=int(row["id"]),
        occurred_at=str(row["occurred_at"]),
        kind=str(row["kind"]),
        amount_cny=float(row["amount_cny"]),
        balance_after=float(row["balance_after"]),
        bet_id=row["bet_id"],
        note=row["note"],
    )
    return DepositCreatedView(event=event, balance=float(row["balance_after"]))


@router.get(
    "/api/v1/costs/summary",
    summary="期间成本摘要(金额/credits 分列)",
    response_model=CostSummaryView,
)
async def get_cost_summary(
    db: DbDep,
    since: Annotated[
        str | None, Query(description="期间起点(UTC ISO); 默认全部已记账")
    ] = None,
) -> CostSummaryView:
    """已记账成本聚合; 未记录的成本不在内, 不视为总成本已覆盖。"""
    rows = rs_store.cost_summary(db, since)
    items = [
        CostItemView(
            category=str(row["category"]),
            units=float(row["units"] or 0),
            amount_cny=float(row["amount_cny"] or 0),
            entries=int(row["entries"]),
        )
        for row in rows
    ]
    credits_total = next(
        (item.units for item in items if item.category == "odds_api_credit"), 0.0
    )
    return CostSummaryView(
        since=since,
        total_cny=round(sum(item.amount_cny for item in items), 2),
        credits_used=credits_total,
        items=items,
    )
