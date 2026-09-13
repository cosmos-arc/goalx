"""今日页 API：竞彩场次对照表与赔率时序(票 22)。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from goalx_backend.api.deps import get_db
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.quote_evidence import (
    DEFAULT_FRESHNESS_SECONDS,
    DEFAULT_PAIR_GAP_SECONDS,
    adjudicate_had_quote,
)
from goalx_backend.data.today import TodayFixtureView, build_today_view

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


class HadQuoteVerdictView(BaseModel):
    """某 as-of 时点的 had 报价判定（票 35 交接契约）。"""

    fixture_id: int
    as_of: str
    kickoff_utc: str
    status: str
    reasons: list[str]
    sale_state: str | None = None
    single_eligible: bool | None = None
    jc_odds: dict[str, float]
    jc_source_updated_at: str | None = None
    jc_age_seconds: float | None = None
    eu_books: int = 0
    eu_probabilities: dict[str, float] | None = None
    eu_fair_odds: dict[str, float] | None = None
    eu_source_updated_at: str | None = None
    pair_gap_seconds: float | None = None
    sources: list[str]


@router.get(
    "/{fixture_id}/had-quote",
    summary="as-of had 报价证据判定",
    response_model=HadQuoteVerdictView,
    responses={404: {"description": "fixture 不存在"}},
)
async def get_had_quote_verdict(
    fixture_id: int,
    db: DbDep,
    as_of: Annotated[
        str | None, Query(description="决策时点(UTC ISO);默认现在")
    ] = None,
    freshness_seconds: Annotated[
        float, Query(ge=0, description="源新鲜度上限(秒),工程初值300")
    ] = DEFAULT_FRESHNESS_SECONDS,
    max_pair_gap_seconds: Annotated[
        float, Query(ge=0, description="两源时差上限(秒),工程初值300")
    ] = DEFAULT_PAIR_GAP_SECONDS,
) -> HadQuoteVerdictView:
    """按 as-of 取 had 报价证据，返回 有效/未知/拒绝、原因、age 与两源时差。"""
    if fx_store.get_fixture(db, fixture_id) is None:
        raise HTTPException(status_code=404, detail="fixture not found")
    verdict = adjudicate_had_quote(
        db,
        fixture_id,
        as_of or datetime.now(UTC).isoformat(timespec="seconds"),
        freshness_seconds=freshness_seconds,
        max_pair_gap_seconds=max_pair_gap_seconds,
    )
    return HadQuoteVerdictView(
        fixture_id=verdict.fixture_id,
        as_of=verdict.as_of,
        kickoff_utc=verdict.kickoff_utc,
        status=verdict.status,
        reasons=verdict.reasons,
        sale_state=verdict.sale_state,
        single_eligible=verdict.single_eligible,
        jc_odds=verdict.jc_odds,
        jc_source_updated_at=verdict.jc_source_updated_at,
        jc_age_seconds=verdict.jc_age_seconds,
        eu_books=verdict.eu_books,
        eu_probabilities=verdict.eu_probabilities,
        eu_fair_odds=verdict.eu_fair_odds,
        eu_source_updated_at=verdict.eu_source_updated_at,
        pair_gap_seconds=verdict.pair_gap_seconds,
        sources=verdict.sources,
    )
