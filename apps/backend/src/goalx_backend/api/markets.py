"""玩法轴 API：进球类（ttg/crs）报价 + 矩阵推导概率/EV（票 wb-04）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from goalx_backend.api.deps import get_db
from goalx_backend.api.fixtures import MAX_WINDOW_DAYS
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.fixtures import beijing_business_date
from goalx_backend.data.today import business_dates_from
from goalx_backend.modelling.forecast import (
    forecast_matrix_from_payload,
    latest_forecast,
)
from goalx_backend.modelling.goals import (
    goals_selection_grid,
    goals_selection_rows,
)

router = APIRouter(prefix="/api/v1/markets", tags=["markets"])

DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


class GoalsSelectionView(BaseModel):
    """
    进球类玩法一个选项：竞彩价、矩阵推导概率与模型 EV。

    EV 口径 = 模型概率 × 竞彩价 − 1（进球类无欧赔共识，区别于 had 的
    共识 EV——had 信市场，进球类只有自家模型可依，见词典 model-prob）。
    无 Forecast 的场次 probability/ev 诚实为 null。
    """

    code: str
    odds: float | None = None
    probability: float | None = None
    ev: float | None = None


class GoalsMarketBlock(BaseModel):
    """一场比赛一个进球玩法（ttg/crs）的选项网格与销售/单固资格。"""

    selections: list[GoalsSelectionView] = Field(default_factory=list)
    single_eligible: bool | None = None
    sale_state: str | None = None
    updated_at: str | None = None


class GoalsFixtureView(BaseModel):
    """进球玩法页一行（票 wb-04）：场次信息 + ttg/crs 两块 + 模型出处。"""

    fixture_id: int
    match_code: str
    business_date: str
    competition: str
    tier: str
    home_team: str
    away_team: str
    kickoff_utc: str
    ttg: GoalsMarketBlock = Field(default_factory=GoalsMarketBlock)
    crs: GoalsMarketBlock = Field(default_factory=GoalsMarketBlock)
    model_version: str | None = None
    issued_at: str | None = None


def _market_block(
    conn: sqlite3.Connection,
    fixture_id: int,
    market_code: str,
    matrix_payload: dict[str, Any] | None,
    moment: str,
) -> GoalsMarketBlock:
    """组装一个进球玩法块：竞彩价时序最新值 + 矩阵概率 + 销售/单固资格。"""
    latest = fx_store.latest_odds_by_selection(
        conn, fixture_id, market_code, "sporttery"
    )
    odds = {sel: value[0] for sel, value in latest.items()}
    if matrix_payload is not None:
        selections = [
            GoalsSelectionView(**row)
            for row in goals_selection_rows(
                forecast_matrix_from_payload(matrix_payload), market_code, odds
            )
        ]
    else:
        # 无 Forecast：完整网格照常返回，概率/EV 诚实置空（不伪造）
        selections = [
            GoalsSelectionView(code=code, odds=odds.get(code))
            for code in goals_selection_grid(market_code)
        ]
    block = GoalsMarketBlock(
        selections=selections,
        updated_at=max((at for _, at in latest.values()), default=None),
    )
    sale = fx_store.latest_sale_status_asof(conn, fixture_id, market_code, moment)
    if sale is not None:
        block.sale_state = str(sale["sale_state"])
        block.single_eligible = (
            None if sale["single_eligible"] is None else bool(sale["single_eligible"])
        )
    return block


@router.get(
    "/goals",
    summary="进球玩法页读模型(ttg/crs 报价+矩阵推导概率/EV)",
    response_model=list[GoalsFixtureView],
)
async def get_goals_market(
    db: DbDep,
    date: Annotated[
        str | None, Query(description="起始业务日(北京日期, 默认今天)")
    ] = None,
    days: Annotated[
        int, Query(ge=1, le=MAX_WINDOW_DAYS, description="窗口天数(默认1=单日)")
    ] = 1,
) -> list[GoalsFixtureView]:
    """
    进球类玩法（总进球 ttg / 比分 crs）的场次行（票 wb-04/05）。

    每行带 ttg/crs 两块：官方选项网格 ×（竞彩在售价、比分矩阵推导概率、
    模型 EV=概率×竞彩价−1）+ 单固资格与销售状态。``days>1`` 返回业务日
    窗口（与场次列表同口径）。EV 口径为"模型×竞彩价"（进球类无欧共识），
    与 had 玩法页的共识 EV 不同源，页面须标注。
    """
    moment = datetime.now(UTC).isoformat(timespec="seconds")
    rows: list[GoalsFixtureView] = []
    for fixture in fx_store.fixtures_for_business_dates(
        db, business_dates_from(date or beijing_business_date(), days)
    ):
        fixture_id = int(fixture["id"])
        forecast = latest_forecast(db, fixture_id)
        payload = json.loads(forecast["payload"]) if forecast is not None else None
        view = GoalsFixtureView(
            fixture_id=fixture_id,
            match_code=str(fixture["match_code"]),
            business_date=str(fixture["business_date"]),
            competition=str(fixture["competition_name"]),
            tier=str(fixture["competition_tier"]),
            home_team=str(fixture["home_team"]),
            away_team=str(fixture["away_team"]),
            kickoff_utc=str(fixture["kickoff_utc"]),
            model_version=str(forecast["model_version"]) if forecast else None,
            issued_at=str(forecast["issued_at"]) if forecast else None,
        )
        view.ttg = _market_block(db, fixture_id, "ttg", payload, moment)
        view.crs = _market_block(db, fixture_id, "crs", payload, moment)
        rows.append(view)
    return rows
