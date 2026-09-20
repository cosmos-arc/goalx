"""
证据面 API（票 14）：存量工件渲染——证据卡/证据链/复核队列/盲评。

端点只读 llm/modelling/data 域已存证工件（append-only），不产生任何
预测/融合写入；复核结论只进评测集（票 05 冻结）。诚实降级契约：
无情报场次 ``state=no_intel`` 且无 forecast——前端明说"不出概率"，
仅展示官方份额（不装懂）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Literal

from ag_ui.core import RunAgentInput
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from goalx_backend.api.deps import get_db
from goalx_backend.config import Settings, get_settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.db import utc_now_iso
from goalx_backend.llm.ask import ask_analyst_events, extract_question
from goalx_backend.llm.gate import latest_divergence
from goalx_backend.llm.review import (
    open_reviews,
    record_blind_review,
    record_verdict,
    review_items_for_fixture,
)
from goalx_backend.llm.store import intel_for_fixture
from goalx_backend.modelling.forecast import (
    forecast_matrix_from_payload,
    latest_forecast,
)

router = APIRouter(tags=["evidence"])
DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


class IntelItemView(BaseModel):
    """一条已存证情报（来源/时点随条目——徽章展示口径）。"""

    kind: str  # form | h2h | formation …（采集器定义）
    text: str
    source: str
    collected_at: str


class TrackTripleView(BaseModel):
    """一条轨道预测的三项与发出时点（llm 轨带 rationale/analyst 标记）。"""

    track: str  # ml | llm | fused
    h: float
    d: float
    a: float
    issued_at: str
    model_version: str
    rationale: str | None = None
    analyst: bool = False


class DivergenceView(BaseModel):
    """ML×LLM 分歧读数：JS 散度 + 是否已路由复核（pre_match 项存在）。"""

    js: float | None = None
    routed: bool = False


class EvidenceMatchView(BaseModel):
    """
    彩池证据卡一行（一场）。

    state 语义（LLM 证据线）：analyst_done=analyst 已复核；scout_done=scout
    已出概率；no_forecast=有情报但无 LLM 概率产出（scout 未跑/解析失败，
    宁缺毋假）；no_intel=无情报无产出——诚实降级，前端只展示官方份额。
    """

    match_seq: int
    fixture_id: int | None = None
    home_team: str
    away_team: str
    league: str
    kickoff_utc: str
    forecast: TrackTripleView | None = None  # 融合线优先，退 LLM 轨
    intel_count: int = 0
    intels: list[IntelItemView] = Field(default_factory=list)
    divergence: DivergenceView = Field(default_factory=DivergenceView)
    state: str  # analyst_done | scout_done | no_forecast | no_intel


class EvidenceSummaryView(BaseModel):
    """期次证据卡汇总（一 fetch 渲染整期，V1 组件消费）。"""

    period_no: str
    market_code: str
    generated_at: str
    caliber: str
    matches: list[EvidenceMatchView]


EVIDENCE_CALIBER_TEXT = (
    "证据卡 = 已存证工件渲染：情报条目带来源与采集时点；概率 = 融合线"
    "（fused，未生成时退 LLM 轨 scout/analyst 产出），ML 轨概率见场次按钮；"
    "JS = ML×LLM 散度（>0.06 且 Tier1 入复核）；无情报场次不出概率，"
    "仅展示官方份额（不装懂）。"
)


def _triple_view(row: sqlite3.Row, track: str) -> TrackTripleView:
    """Forecast 行 → 三项视图（ml 轨 payload 为矩阵，推 had 边际）。"""
    payload = json.loads(str(row["payload"]))
    analyst = False
    rationale: str | None = None
    if track == "ml":
        had = forecast_matrix_from_payload(payload).had()
        h, d, a = had["h"], had["d"], had["a"]
    else:
        h = float(payload["h"])
        d = float(payload["d"])
        a = float(payload["a"])
        rationale = str(payload.get("rationale") or "") or None
        analyst = bool(payload.get("analyst", False))
    return TrackTripleView(
        track=track,
        h=round(h, 4),
        d=round(d, 4),
        a=round(a, 4),
        issued_at=str(row["issued_at"]),
        model_version=str(row["model_version"]),
        rationale=rationale,
        analyst=analyst,
    )


def _latest_track(
    conn: sqlite3.Connection, fixture_id: int, track: str
) -> TrackTripleView | None:
    """该场某轨最新预测三项；无预测或坏 payload 返回 None（宁缺毋假）。"""
    row = latest_forecast(conn, fixture_id, track)
    if row is None:
        return None
    try:
        return _triple_view(row, track)
    except (KeyError, TypeError, ValueError):
        return None


def _intel_views(rows: list[sqlite3.Row]) -> list[IntelItemView]:
    return [
        IntelItemView(
            kind=str(r["kind"]),
            text=str(r["text"]),
            source=str(r["source"]),
            collected_at=str(r["collected_at"]),
        )
        for r in rows
    ]


def _match_state(llm_track: TrackTripleView | None, intel_count: int) -> str:
    if llm_track is not None:
        return "analyst_done" if llm_track.analyst else "scout_done"
    return "no_forecast" if intel_count else "no_intel"


def _match_evidence_view(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    fixture_id: int | None,
) -> EvidenceMatchView:
    """彩池对阵行 → 证据卡视图（无桥接 fixture 时整体诚实降级）。"""
    intels = _intel_views(intel_for_fixture(conn, fixture_id)) if fixture_id else []
    llm_track = _latest_track(conn, fixture_id, "llm") if fixture_id else None
    fused_track = _latest_track(conn, fixture_id, "fused") if fixture_id else None
    divergence = DivergenceView()
    if fixture_id is not None:
        js = latest_divergence(conn, fixture_id)
        divergence = DivergenceView(
            js=round(js, 6) if js is not None else None,
            routed=any(
                r["route"] == "pre_match"
                for r in review_items_for_fixture(conn, fixture_id)
            ),
        )
    return EvidenceMatchView(
        match_seq=int(row["match_seq"]),
        fixture_id=fixture_id,
        home_team=str(row["home_team"]),
        away_team=str(row["away_team"]),
        league=str(row["league"] or ""),
        kickoff_utc=str(row["kickoff_utc"]),
        forecast=fused_track or llm_track,
        intel_count=len(intels),
        intels=intels,
        divergence=divergence,
        state=_match_state(llm_track, len(intels)),
    )


@router.get(
    "/api/v1/pool/periods/{period_no}/evidence-summary",
    summary="彩池期次证据卡汇总(存量渲染,票 14)",
    response_model=EvidenceSummaryView,
    responses={404: {"description": "期次不存在"}},
)
async def get_pool_evidence_summary(
    period_no: str,
    db: DbDep,
    market_code: str = "ttt14",
) -> EvidenceSummaryView:
    """一期 14 场的证据卡数据（情报/概率/分歧/状态一次取齐）。"""
    pool_period_id = pool_store.pool_period_id(db, market_code, period_no)
    if pool_period_id is None:
        raise HTTPException(status_code=404, detail=f"period {period_no} not found")
    matches = [
        _match_evidence_view(
            db,
            row,
            pool_store.match_fixture_id(
                db,
                str(row["kickoff_utc"]),
                str(row["home_team"]),
                str(row["away_team"]),
            ),
        )
        for row in pool_store.pool_matches_for_period(db, pool_period_id)
    ]
    return EvidenceSummaryView(
        period_no=period_no,
        market_code=market_code,
        generated_at=utc_now_iso(),
        caliber=EVIDENCE_CALIBER_TEXT,
        matches=matches,
    )


class ReviewItemView(BaseModel):
    """一场的复核项（赛前 JS 路由 / 赛后一对一错，双路可并存）。"""

    route: str  # pre_match | post_settle
    status: str  # open | done
    verdict: str | None = None  # key_contribution | irrelevant | misleading
    js_value: float | None = None
    created_at: str
    decided_at: str | None = None


class FixtureEvidenceView(BaseModel):
    """场次证据链（V2 区块消费）：三轨对照 + 情报时间线 + 复核状态。"""

    fixture_id: int
    generated_at: str
    caliber: str
    tracks: dict[str, TrackTripleView | None]  # ml/llm/fused，缺轨 None
    divergence: DivergenceView
    intels: list[IntelItemView]
    reviews: list[ReviewItemView]


@router.get(
    "/api/v1/fixtures/{fixture_id}/evidence",
    summary="场次证据链(三轨对照+情报+复核,票 14)",
    response_model=FixtureEvidenceView,
    responses={404: {"description": "fixture 不存在"}},
)
async def get_fixture_evidence(fixture_id: int, db: DbDep) -> FixtureEvidenceView:
    """一场的完整证据链（存量工件只读；无数据轨为 None 不虚构）。"""
    if fx_store.get_fixture(db, fixture_id) is None:
        raise HTTPException(status_code=404, detail="fixture not found")
    tracks = {t: _latest_track(db, fixture_id, t) for t in ("ml", "llm", "fused")}
    js = latest_divergence(db, fixture_id)
    return FixtureEvidenceView(
        fixture_id=fixture_id,
        generated_at=utc_now_iso(),
        caliber=EVIDENCE_CALIBER_TEXT,
        tracks=tracks,
        divergence=DivergenceView(
            js=round(js, 6) if js is not None else None,
            routed=any(
                r["route"] == "pre_match"
                for r in review_items_for_fixture(db, fixture_id)
            ),
        ),
        intels=_intel_views(intel_for_fixture(db, fixture_id)),
        reviews=[
            ReviewItemView(
                route=str(r["route"]),
                status=str(r["status"]),
                verdict=str(r["verdict"]) if r["verdict"] else None,
                js_value=float(r["js_value"]) if r["js_value"] is not None else None,
                created_at=str(r["created_at"]),
                decided_at=str(r["decided_at"]) if r["decided_at"] else None,
            )
            for r in review_items_for_fixture(db, fixture_id)
        ],
    )


class ReviewQueueItemView(BaseModel):
    """复核队列一行（含对阵信息，免前端二次取数）。"""

    id: int
    fixture_id: int
    home_team: str
    away_team: str
    competition: str
    kickoff_utc: str
    route: str
    js_value: float | None
    status: str
    created_at: str


class ReviewQueueView(BaseModel):
    """待复核清单。"""

    items: list[ReviewQueueItemView]


@router.get(
    "/api/v1/review/queue",
    summary="复核队列(open 项,票 13 闭环人工面)",
    response_model=ReviewQueueView,
)
async def get_review_queue(db: DbDep) -> ReviewQueueView:
    """Open 状态复核项（赛前路由 + 赛后一对一错）按入队时点升序。"""
    items: list[ReviewQueueItemView] = []
    for row in open_reviews(db):
        info = fx_store.fixture_team_info(db, int(row["fixture_id"]))
        items.append(
            ReviewQueueItemView(
                id=int(row["id"]),
                fixture_id=int(row["fixture_id"]),
                home_team=str(info["home_name"]) if info else "?",
                away_team=str(info["away_name"]) if info else "?",
                competition=str(info["competition_name"]) if info else "?",
                kickoff_utc=str(info["kickoff_utc"]) if info else "",
                route=str(row["route"]),
                js_value=float(row["js_value"])
                if row["js_value"] is not None
                else None,
                status=str(row["status"]),
                created_at=str(row["created_at"]),
            )
        )
    return ReviewQueueView(items=items)


class VerdictPayload(BaseModel):
    """复核结论提交（三分类只进评测集，票 05 冻结）。"""

    classification: Literal["key_contribution", "irrelevant", "misleading"]
    note: str | None = None


@router.post(
    "/api/v1/review/items/{item_id}/verdict",
    summary="提交复核结论(三分类,open→done)",
    response_model=dict[str, bool],
    responses={404: {"description": "复核项不存在或已裁决"}},
)
async def submit_review_verdict(
    item_id: int, payload: VerdictPayload, db: DbDep
) -> dict[str, bool]:
    """结论三分类落库（不改任何预测工件）。"""
    if not record_verdict(db, item_id, payload.classification, note=payload.note):
        raise HTTPException(
            status_code=404, detail="review item not found or already decided"
        )
    db.commit()
    return {"recorded": True}


class BlindReviewPayload(BaseModel):
    """盲评提交（双周匿名二选一，参考列）。"""

    cycle: str = Field(min_length=1)  # 双周期次（前端生成，如 2026-B19）
    fixture_id: int
    choice: Literal["ml", "llm"]
    note: str | None = None


@router.post(
    "/api/v1/blind-reviews",
    summary="提交盲评(双周匿名二选一,幂等)",
    response_model=dict[str, bool],
)
async def submit_blind_review(
    payload: BlindReviewPayload, db: DbDep
) -> dict[str, bool]:
    """同周期同场次重复提交被吸收（recorded=false 语义化返回）。"""
    recorded = record_blind_review(
        db,
        cycle=payload.cycle,
        fixture_id=payload.fixture_id,
        choice=payload.choice,
        note=payload.note,
    )
    db.commit()
    return {"recorded": recorded}


@router.post(
    "/api/v1/fixtures/{fixture_id}/ask",
    summary="追问 analyst(AG-UI 1.0 事件流,票 15)",
    responses={
        404: {"description": "fixture 不存在"},
        422: {"description": "请求体非 AG-UI RunAgentInput 或无用户问题"},
    },
)
async def ask_analyst(fixture_id: int, request: Request, db: DbDep) -> Response:
    """
    追问端点：请求体 = AG-UI ``RunAgentInput``。

    schema 由 ag-ui-protocol 1.0 规范定义，不在本契约内复制；响应 =
    AG-UI 1.0 事件 SSE。prompt 只注入该场已存证情报（回答只引存证
    条目）；无情报场次走诚实降级话术（不调模型，零成本）；每次调用
    按 usage 记 cost_ledger（用途标签 ask）。追问是增量交互面——不落
    任何预测/证据工件。
    """
    if fx_store.get_fixture(db, fixture_id) is None:
        raise HTTPException(status_code=404, detail="fixture not found")
    try:
        payload = RunAgentInput.model_validate(await request.json())
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid AG-UI RunAgentInput: {exc}"
        ) from exc
    question = extract_question(payload.messages)
    if not question:
        raise HTTPException(status_code=422, detail="messages 中无用户问题")
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    return StreamingResponse(
        ask_analyst_events(
            db,
            settings,
            fixture_id,
            question,
            thread_id=payload.thread_id or f"fixture-{fixture_id}",
        ),
        media_type="text/event-stream",
    )
