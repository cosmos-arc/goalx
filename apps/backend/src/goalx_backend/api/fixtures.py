"""今日页 API：竞彩场次对照表与赔率时序(票 22)。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from goalx_backend.api.deps import get_db
from goalx_backend.services import TodayFixtureView, build_today_view
from goalx_backend.store import fixtures as fx_store

router = APIRouter(prefix="/api/v1/fixtures", tags=["fixtures"])

CST = timezone(timedelta(hours=8))  # businessDate 为北京日期(spec §3 坑位备忘)
DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


def _today_business_date() -> str:
    """北京时间今天的销售日。"""
    return datetime.now(UTC).astimezone(CST).strftime("%Y-%m-%d")


@router.get(
    "/today",
    summary="今日竞彩场次对照表",
    response_model=list[TodayFixtureView],
)
async def get_today_fixtures(
    db: DbDep,
    date: Annotated[str | None, Query(description="业务日(北京日期, 默认今天)")] = None,
) -> list[TodayFixtureView]:
    """竞彩 vs 欧洲共识对照：赔率、隐含概率、EV、books 数、调盘时点。"""
    return build_today_view(db, date or _today_business_date())


class OddsSnapshotView(BaseModel):
    """一条赔率快照。"""

    id: int
    fixture_id: int
    market_code: str
    selection_code: str
    source: str
    odds: float
    captured_at: str


@router.get(
    "/{fixture_id}/odds",
    summary="场次赔率时序",
    response_model=list[OddsSnapshotView],
    responses={404: {"description": "fixture 不存在"}},
)
async def get_fixture_odds(
    fixture_id: int,
    db: DbDep,
    market: Annotated[str, Query(description="玩法 poolCode")] = "had",
) -> list[OddsSnapshotView]:
    """一场比赛某玩法的全部赔率快照时序(append-only, 票 19 验收)。"""
    if fx_store.get_fixture(db, fixture_id) is None:
        raise HTTPException(status_code=404, detail="fixture not found")
    return [
        OddsSnapshotView(
            id=int(row["id"]),
            fixture_id=fixture_id,
            market_code=market,
            selection_code=str(row["selection_code"]),
            source=str(row["source"]),
            odds=float(row["odds"]),
            captured_at=str(row["captured_at"]),
        )
        for row in fx_store.odds_history(db, fixture_id, market)
    ]


class ManualJoinPayload(BaseModel):
    """人工映射输入（时间窗 join 残余补齐，票 20）。"""

    event_id: str
    sport_key: str


@router.post(
    "/{fixture_id}/join",
    summary="人工映射欧赔事件",
    response_model=dict[str, str],
    responses={404: {"description": "fixture 不存在"}},
)
async def set_manual_join(
    fixture_id: int, payload: ManualJoinPayload, db: DbDep
) -> dict[str, str]:
    """把 fixture 手工映射到 The Odds API event（join_method=manual）。"""
    if fx_store.get_fixture(db, fixture_id) is None:
        raise HTTPException(status_code=404, detail="fixture not found")
    fx_store.set_odds_api_join(
        db, fixture_id, payload.event_id, payload.sport_key, "manual"
    )
    db.commit()
    return {"status": "joined", "method": "manual"}
