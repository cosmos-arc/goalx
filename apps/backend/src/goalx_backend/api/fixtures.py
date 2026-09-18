"""场次轴 API：竞彩场次对照表、赔率时序与单场研究视图(票 22 / wb-01 / wb-02)。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from goalx_backend import odds_math as om
from goalx_backend.api.deps import get_db
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.fixtures import beijing_business_date
from goalx_backend.data.quote_evidence import (
    DEFAULT_FRESHNESS_SECONDS,
    DEFAULT_PAIR_GAP_SECONDS,
    adjudicate_had_quote,
)
from goalx_backend.data.today import (
    HadQuoteStatus,
    SelectionTriple,
    TodayFixtureView,
    build_today_view,
)
from goalx_backend.markets import SELECTIONS
from goalx_backend.modelling.forecast import (
    forecast_matrix_from_payload,
    latest_forecast,
)

router = APIRouter(prefix="/api/v1/fixtures", tags=["fixtures"])

DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


MAX_WINDOW_DAYS = 7  # 场次列表日期窗口上限（票 wb-01：前端 3 日，留余量）


@router.get(
    "/today",
    summary="竞彩场次对照表(按业务日窗口)",
    response_model=list[TodayFixtureView],
)
async def get_today_fixtures(
    db: DbDep,
    date: Annotated[
        str | None, Query(description="起始业务日(北京日期, 默认今天)")
    ] = None,
    days: Annotated[
        int, Query(ge=1, le=MAX_WINDOW_DAYS, description="窗口天数(默认1=单日)")
    ] = 1,
) -> list[TodayFixtureView]:
    """
    竞彩 vs 欧洲共识对照：赔率、隐含概率、EV、books 数、调盘时点。

    ``days>1`` 时返回 ``[date, date+days-1]`` 业务日窗口内的场次，行内
    ``business_date`` 标记归属日（票 wb-01 场次列表 3 日化）。
    """
    return build_today_view(db, date or beijing_business_date(), days=days)


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


class BookQuoteView(BaseModel):
    """
    研究页一行：某欧赔 book 的最新三向报价。

    ``book`` 为快照 source 原值（``odds_api:<book>``，前端剥前缀展示）；
    ``captured_at`` 取该 book 三向中最新的捕获时点。
    """

    book: str
    odds: SelectionTriple
    captured_at: str | None = None


class ConsensusView(BaseModel):
    """
    去水共识（Shin）与参与 book 数（口径与场次列表页一致）。

    ``low_confidence``（票 39）：books < 阈值（默认 4，odds_math 常量）时共识
    可信度不足——展示层据此打琥珀低置信标注，不改概率本身。
    """

    books: int
    low_confidence: bool
    probability: SelectionTriple


class ModelForecastView(BaseModel):
    """该场最新 ML Forecast 的三向概率与模型 EV（模型概率 × 竞彩价 − 1）。"""

    model_version: str
    issued_at: str
    probability: SelectionTriple
    ev: SelectionTriple | None = None


class FixtureResearchView(BaseModel):
    """单场研究页读模型（票 wb-02）：逐书赔率 + 共识 + 模型 + 资格判定。"""

    fixture_id: int
    match_code: str
    business_date: str
    competition: str
    tier: str
    home_team: str
    away_team: str
    kickoff_utc: str
    is_single: bool
    joined: bool
    jc_odds: SelectionTriple
    jc_updated_at: str | None = None
    books: list[BookQuoteView] = Field(default_factory=list)
    consensus: ConsensusView | None = None
    model: ModelForecastView | None = None
    had_quote: HadQuoteStatus | None = None


@router.get(
    "/{fixture_id}/research",
    summary="单场研究页读模型",
    response_model=FixtureResearchView,
    responses={404: {"description": "fixture 不存在或无竞彩销售编号"}},
)
async def get_fixture_research(
    fixture_id: int,
    db: DbDep,
    as_of: Annotated[
        str | None, Query(description="判定时点(UTC ISO);默认现在")
    ] = None,
) -> FixtureResearchView:
    """
    一场比赛的研究视图。

    各欧赔 book 最新 H/D/A 与捕获时间、去水共识概率、模型（DC Forecast）
    概率与模型 EV、had 资格判定与开赛信息（票 wb-02）。

    共识口径与场次列表页一致（同 fixture 两页不出现两个共识）。
    """
    detail = fx_store.fixture_detail(db, fixture_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="fixture not found")
    moment = as_of or datetime.now(UTC).isoformat(timespec="seconds")
    jc = fx_store.latest_odds_by_selection(db, fixture_id, "had", "sporttery")
    jc_odds = SelectionTriple(**{s: jc.get(s, (None,))[0] for s in SELECTIONS})
    quotes = fx_store.eu_book_quotes(db, fixture_id)
    view = FixtureResearchView(
        fixture_id=fixture_id,
        match_code=str(detail["match_code"]),
        business_date=str(detail["business_date"]),
        competition=str(detail["competition_name"]),
        tier=str(detail["competition_tier"]),
        home_team=str(detail["home_team"]),
        away_team=str(detail["away_team"]),
        kickoff_utc=str(detail["kickoff_utc"]),
        is_single=bool(detail["is_single"]),
        joined=detail["odds_api_event_id"] is not None,
        jc_odds=jc_odds,
        jc_updated_at=max((at for _, at in jc.values()), default=None),
    )
    verdict = adjudicate_had_quote(db, fixture_id, moment)
    view.had_quote = HadQuoteStatus(
        as_of=moment,
        status=verdict.status,
        reasons=verdict.reasons,
        sale_state=verdict.sale_state,
        single_eligible=verdict.single_eligible,
        jc_source_updated_at=verdict.jc_source_updated_at,
        eu_books=verdict.eu_books,
    )
    view.books = [
        BookQuoteView(
            book=book,
            odds=SelectionTriple(**{s: sels.get(s, (None,))[0] for s in SELECTIONS}),
            captured_at=max((at for _, at in sels.values()), default=None),
        )
        for book, sels in sorted(quotes.items())
    ]
    by_selection: dict[str, dict[str, float]] = {s: {} for s in SELECTIONS}
    for book, sels in quotes.items():
        for sel, (odds, _) in sels.items():
            if sel in by_selection:
                by_selection[sel][book] = odds
    if all(by_selection[s] for s in SELECTIONS):
        consensus = om.consensus_odds([by_selection[s] for s in SELECTIONS])
        if consensus is not None:
            probs = om.shin_implied(consensus)
            books = max(len(prices) for prices in by_selection.values())
            view.consensus = ConsensusView(
                books=books,
                low_confidence=om.consensus_low_confidence(books),
                probability=SelectionTriple(
                    **{s: round(p, 4) for s, p in zip(SELECTIONS, probs, strict=True)}
                ),
            )
    forecast = latest_forecast(db, fixture_id)
    if forecast is not None:
        had = forecast_matrix_from_payload(json.loads(forecast["payload"])).had()
        view.model = ModelForecastView(
            model_version=str(forecast["model_version"]),
            issued_at=str(forecast["issued_at"]),
            probability=SelectionTriple(**{s: round(had[s], 4) for s in SELECTIONS}),
            ev=SelectionTriple(
                **{
                    s: round(om.expected_value(had[s], jc[s][0]), 4)
                    if s in jc and jc[s][0]
                    else None
                    for s in SELECTIONS
                }
            ),
        )
    return view
