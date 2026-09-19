"""彩池 API（票 43）：期次/对阵/分布视图、同步触发、AI 代采入口。"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from httpx import HTTPError
from pydantic import BaseModel, Field

from goalx_backend.api.deps import get_db
from goalx_backend.config import Settings, get_settings
from goalx_backend.data import pool as pool_store
from goalx_backend.data.ingest import zucai
from goalx_backend.data.ingest.oddsapi import polite_client
from goalx_backend.db import utc_now_iso

router = APIRouter(tags=["pool"])
DbDep = Annotated[sqlite3.Connection, Depends(get_db)]

# 一场一选的三向顺序：官方池码 + 中文标签 + had 字母（前端展示用）
_POOL_TRIPLE = (("3", "胜", "h"), ("1", "平", "d"), ("0", "负", "a"))


class PoolSelectionView(BaseModel):
    """一场一选的彩池口径三向数据。"""

    code: str  # h/d/a
    label: str
    prob: float | None = None  # 概率（模型口径优先，退化为欧指去水）
    prob_source: str  # model | euro_devig | none
    share: float | None = None  # 公众份额（源B 人气，estimated）
    implied_odds: float | None = None  # 估计派彩赔率 = 返奖率/share
    ev: float | None = None  # p × 估计赔率 − 1（缺份额 None）


class PoolMatchView(BaseModel):
    """一期一场对阵。"""

    match_seq: int
    source_match_id: str | None = None
    league: str
    kickoff_utc: str
    home_team: str
    away_team: str
    fixture_id: int | None = None  # 映射到的竞彩场次（无则 None）
    selections: list[PoolSelectionView]


class PoolStateView(BaseModel):
    """一期资金状态（官方销量仅 AI 代采可得，无则整体 None）。"""

    sales_amount: float | None = None
    rollover_in: float | None = None
    published_at: str | None = None
    source: str | None = None


class PoolPeriodView(BaseModel):
    """期次列表一行。"""

    period_no: str
    sales_deadline: str | None = None
    match_count: int
    first_kickoff: str | None = None
    last_kickoff: str | None = None
    shares_captured_at: str | None = None
    state: PoolStateView | None = None
    status: str  # on_sale | finished | unknown（按最后开赛时间）


class PoolPeriodDetailView(BaseModel):
    """期次详情：对阵三向（概率/份额/估计赔率/EV）+ 资金状态 + 口径说明。"""

    period_no: str
    sales_deadline: str | None = None
    matches: list[PoolMatchView]
    state: PoolStateView | None = None
    shares_captured_at: str | None = None
    caliber: str  # 口径说明（前端原样展示）


CALIBER_TEXT = (
    "概率 = 模型（映射场次有赛前 Forecast 时）或期次页三向欧指去水；"
    "份额 = 第三方人气分布（公众分布代理，非官方池份额）；"
    "估计派彩赔率 = 返奖率 65% ÷ 份额（抽水折算）；"
    "EV = 概率 × 估计赔率 − 1，未建模 price impact 与分彩风险。"
)


class PoolSyncRunView(BaseModel):
    """一次彩池同步的元信息。"""

    source: str
    observed_at: str
    period_nos: list[str]
    pages: int
    matches: int
    share_rows: int
    missing_shares: int


class PoolSyncStatusView(BaseModel):
    """彩池同步状态（触发后/查询）。"""

    last_run: PoolSyncRunView | None = None
    period_count: int


class PoolStateImportPayload(BaseModel):
    """
    AI 代采入口（票 43 兜底层）：代理读官方公布销量/滚存 → 结构化提交。

    字段自描述、幂等（同值重放无效果）、无鉴权（单用户既定）；
    与自动采集的第三方源以 source 区分（本端点固定 agent）。
    """

    period_no: str = Field(min_length=1)
    sales_amount: float | None = Field(default=None, ge=0)
    rollover_in: float | None = Field(default=None, ge=0)
    prize_tiers: dict[str, Any] | None = None
    published_at: str | None = None


class PoolStateImportView(BaseModel):
    """代采导入结果。"""

    period_no: str
    imported: bool


def _status_for(first: str | None, last: str | None, now: str) -> str:
    """期次状态：最后一场开赛过后 = finished（诚实口径：按开赛时间判）。"""
    if not last:
        return "unknown"
    return "finished" if last <= now else "on_sale"


def _state_view(row: sqlite3.Row | None) -> PoolStateView | None:
    """pool_states 行 → 视图（写入方仅 AI 代采，source 固定 agent）。"""
    if row is None:
        return None
    return PoolStateView(
        sales_amount=row["sales_amount"],
        rollover_in=row["rollover_in"],
        published_at=row["published_at"],
        source="agent",
    )


def _state_view_from_list(row: sqlite3.Row) -> PoolStateView | None:
    """期次列表 JOIN 行 → 资金状态视图（无销量数据时列全 NULL）。"""
    if row["sales_amount"] is None and row["rollover_in"] is None:
        return None
    return PoolStateView(
        sales_amount=row["sales_amount"],
        rollover_in=row["rollover_in"],
        published_at=row["sales_published_at"],
        source="agent",
    )


@router.get(
    "/api/v1/pool/periods",
    summary="彩池期次列表(传统足彩)",
    response_model=list[PoolPeriodView],
)
async def list_pool_periods(
    db: DbDep, market_code: str = "ttt14"
) -> list[PoolPeriodView]:
    """已采集期次（期次/对阵/分布来自源B 同步；销量列无官方直接源）。"""
    now = utc_now_iso()
    return [
        PoolPeriodView(
            period_no=str(row["period_no"]),
            sales_deadline=row["sales_deadline"],
            match_count=int(row["match_count"] or 0),
            first_kickoff=row["first_kickoff"],
            last_kickoff=row["last_kickoff"],
            shares_captured_at=row["shares_captured_at"],
            state=_state_view_from_list(row),
            status=_status_for(row["first_kickoff"], row["last_kickoff"], now),
        )
        for row in pool_store.list_pool_periods(db, market_code)
    ]


@router.get(
    "/api/v1/pool/periods/{period_no}",
    summary="彩池期次详情(对阵/分布/EV)",
    response_model=PoolPeriodDetailView,
    responses={404: {"description": "期次不存在"}},
)
async def get_pool_period(
    period_no: str, db: DbDep, market_code: str = "ttt14"
) -> PoolPeriodDetailView:
    """一场一选三向：概率(模型/欧指去水)、份额、估计派彩赔率、EV。"""
    pool_period_id = pool_store.pool_period_id(db, market_code, period_no)
    if pool_period_id is None:
        raise HTTPException(status_code=404, detail=f"period {period_no} not found")
    now = utc_now_iso()
    views = _match_views(db, pool_period_id, now)
    return PoolPeriodDetailView(
        period_no=period_no,
        sales_deadline=pool_store.pool_period_deadline(db, pool_period_id),
        matches=views,
        state=_state_view(pool_store.pool_state_for_period(db, pool_period_id)),
        shares_captured_at=pool_store.shares_captured_at(db, pool_period_id),
        caliber=CALIBER_TEXT,
    )


def _match_views(
    db: sqlite3.Connection, pool_period_id: int, now: str
) -> list[PoolMatchView]:
    """期次 → 三向视图列表（概率/份额/估计赔率/EV 装配；详情与生成器共用）。"""
    matches = pool_store.pool_matches_for_period(db, pool_period_id)
    shares = pool_store.latest_shares_for_period(db, pool_period_id)
    views: list[PoolMatchView] = []
    for row in matches:
        seq = int(row["match_seq"])
        fixture_id = pool_store.match_fixture_id(
            db, str(row["kickoff_utc"]), str(row["home_team"]), str(row["away_team"])
        )
        model_prob = (
            pool_store.model_prob_for_fixture(db, fixture_id, now)
            if fixture_id is not None
            else None
        )
        devig = pool_store.devig_euro_odds(
            (row["euro_odds_h"], row["euro_odds_d"], row["euro_odds_a"])
        )
        selection_views: list[PoolSelectionView] = []
        for position, (code, label, had) in enumerate(_POOL_TRIPLE):
            if model_prob is not None:
                prob, source = model_prob[had], "model"
            elif devig is not None:
                prob, source = devig[position], "euro_devig"
            else:
                prob, source = None, "none"
            share = shares.get(seq, {}).get(code)
            implied = pool_store.parimutuel_odds(share) if share else None
            ev = (
                pool_store.parimutuel_ev(prob, share)
                if prob is not None and share is not None
                else None
            )
            selection_views.append(
                PoolSelectionView(
                    code=had,
                    label=label,
                    prob=round(prob, 4) if prob is not None else None,
                    prob_source=source,
                    share=round(share, 4) if share is not None else None,
                    implied_odds=round(implied, 2) if implied else None,
                    ev=round(ev, 4) if ev is not None else None,
                )
            )
        views.append(
            PoolMatchView(
                match_seq=seq,
                source_match_id=row["source_match_id"],
                league=str(row["league"] or ""),
                kickoff_utc=str(row["kickoff_utc"]),
                home_team=str(row["home_team"]),
                away_team=str(row["away_team"]),
                fixture_id=fixture_id,
                selections=selection_views,
            )
        )
    return views


# ---- 搏冷生成器（票 pool-v2/02）：纯函数贪心，口径与详情页一致 ----

COLD_SHARE_MAX = 0.25  # 冷门阈值（与前端判定同口径）


class ColdSwapView(BaseModel):
    """一处冷门替换。"""

    match_seq: int
    from_code: str
    to_code: str
    ev_gain: float  # to.ev − from.ev


class ColdTicketView(BaseModel):
    """一张票的估值：命中概率 / 估计派彩赔率 / EV。"""

    picks: dict[str, str]  # str(seq) → h/d/a
    swaps: list[ColdSwapView] = []
    hit_prob: float | None = None  # ∏p；任一场缺概率 → None
    est_odds: float | None = None  # 0.65 ÷ ∏share（单次抽水口径）
    ev: float | None = None  # hit_prob × est_odds − 1


class ColdVariantsPayload(BaseModel):
    """生成器入参：基础票缺省 = 各场最高概率向。"""

    period_no: str = Field(min_length=1)
    market_code: str = "ttt14"
    base_picks: dict[str, str] | None = None
    coldness: int = Field(default=2, ge=1, le=3)


class ColdVariantsView(BaseModel):
    """基础票 + 冷度 1..N 的贪心变体；口径说明前端原样展示。"""

    period_no: str
    base: ColdTicketView
    variants: list[ColdTicketView]
    caliber: str


COLD_CALIBER_TEXT = (
    "变体 = 基础票按「冷选项 EV − 基础向 EV」降序贪心替换 1..N 处"
    "（冷选项 = 份额 <25% 且 EV 为正）；"
    "命中概率 = 各场概率连乘；估计派彩赔率 = 返奖率 65% ÷ 各场份额连乘"
    "（单次抽水近似，未建模分彩与 price impact）；EV = 概率 × 赔率 − 1。"
)


def _ticket_view(
    picks: dict[int, str],
    views: list[PoolMatchView],
    swaps: list[ColdSwapView] | None = None,
) -> ColdTicketView:
    """票面 → 估值视图（任一场缺概率/份额时概率/赔率/EV 为 None，picks 照返）。"""
    by_seq = {v.match_seq: v for v in views}
    hit_prob: float | None = 1.0
    est_odds: float | None = None
    share_prod = 1.0
    for seq, code in sorted(picks.items()):
        sel = next((s for s in by_seq[seq].selections if s.code == code), None)
        if sel is None or sel.prob is None or sel.share is None:
            hit_prob, est_odds = None, None
            break
        hit_prob *= sel.prob
        share_prod *= sel.share
    if hit_prob is not None:
        est_odds = 0.65 / share_prod
    ev = (
        hit_prob * est_odds - 1
        if (hit_prob is not None and est_odds is not None)
        else None
    )
    return ColdTicketView(
        picks={str(seq): code for seq, code in sorted(picks.items())},
        swaps=swaps or [],
        hit_prob=round(hit_prob, 6) if hit_prob is not None else None,
        est_odds=round(est_odds, 2) if est_odds is not None else None,
        ev=round(ev, 4) if ev is not None else None,
    )


def _cold_variants(
    views: list[PoolMatchView],
    base_picks: dict[int, str],
    coldness: int,
) -> tuple[ColdTicketView, list[ColdTicketView]]:
    """贪心：全局按 EV 增益降序候选，各场至多一处替换，输出冷度 1..N 变体。"""
    base = _ticket_view(base_picks, views)
    by_seq = {v.match_seq: v for v in views}
    candidates: list[tuple[float, int, str, str]] = []  # (ev_gain, seq, from, to)
    for seq, from_code in base_picks.items():
        from_ev = next(
            (
                s.ev
                for s in by_seq[seq].selections
                if s.code == from_code and s.ev is not None
            ),
            None,
        )
        if from_ev is None:
            continue
        for sel in by_seq[seq].selections:
            if (
                sel.code == from_code
                or sel.share is None
                or sel.share >= COLD_SHARE_MAX
            ):
                continue
            if sel.ev is None or sel.ev <= 0 or sel.ev <= from_ev:
                continue
            candidates.append((round(sel.ev - from_ev, 6), seq, from_code, sel.code))
    candidates.sort(reverse=True)
    variants: list[ColdTicketView] = []
    taken: dict[int, tuple[str, str, float]] = {}
    for ev_gain, seq, from_code, to_code in candidates:
        if len(taken) >= coldness:
            break
        if seq in taken:
            continue
        taken[seq] = (from_code, to_code, ev_gain)
        picks = dict(base_picks)
        swaps: list[ColdSwapView] = []
        for swap_seq, (swap_from, swap_to, swap_gain) in taken.items():
            picks[swap_seq] = swap_to
            swaps.append(
                ColdSwapView(
                    match_seq=swap_seq,
                    from_code=swap_from,
                    to_code=swap_to,
                    ev_gain=round(swap_gain, 4),
                )
            )
        variants.append(_ticket_view(picks, views, swaps))
    return base, variants


@router.post(
    "/api/v1/pool/cold-variants",
    summary="搏冷变体生成(贪心,票 pool-v2/02)",
    response_model=ColdVariantsView,
    responses={
        404: {"description": "期次不存在"},
        422: {"description": "基础票场次/选项非法"},
    },
)
async def generate_cold_variants(
    payload: ColdVariantsPayload, db: DbDep
) -> ColdVariantsView:
    """基础票 + 冷度 1..N 贪心冷门变体（估值口径与期次详情一致）。"""
    pool_period_id = pool_store.pool_period_id(
        db, payload.market_code, payload.period_no
    )
    if pool_period_id is None:
        raise HTTPException(
            status_code=404, detail=f"period {payload.period_no} not found"
        )
    views = _match_views(db, pool_period_id, utc_now_iso())
    by_seq = {v.match_seq: v for v in views}
    if payload.base_picks is not None:
        for seq_str, code in payload.base_picks.items():
            if not seq_str.isdigit() or int(seq_str) not in by_seq:
                raise HTTPException(422, detail=f"unknown match_seq {seq_str}")
            if code not in {"h", "d", "a"}:
                raise HTTPException(422, detail=f"unknown selection {code}")
        base_picks = {int(seq): code for seq, code in payload.base_picks.items()}
    else:
        base_picks = {}
        for view in views:
            prob_sel = max(
                (s for s in view.selections if s.prob is not None),
                key=lambda s: s.prob or 0,
                default=None,
            )
            if prob_sel is not None:
                base_picks[view.match_seq] = prob_sel.code
        if not base_picks:
            raise HTTPException(422, detail="期次无可用概率")
    base, variants = _cold_variants(views, base_picks, payload.coldness)
    return ColdVariantsView(
        period_no=payload.period_no,
        base=base,
        variants=variants,
        caliber=COLD_CALIBER_TEXT,
    )


@router.post(
    "/api/v1/pool-sync/run",
    summary="触发一次彩池同步(源B 期次/对阵/人气)",
    response_model=PoolSyncStatusView,
    responses={502: {"description": "同步源不可达或返回异常"}},
)
async def run_pool_sync(request: Request, db: DbDep) -> PoolSyncStatusView:
    """拉取最新期次页与人气分布（幂等）；期次/对阵刷新、份额追加。"""
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    try:
        with polite_client() as client:
            zucai.sync_pool_data(db, settings, client)
    except (HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"pool sync source error: {exc}"
        ) from exc
    return _sync_status(db)


@router.get(
    "/api/v1/pool-sync/status",
    summary="彩池同步状态",
    response_model=PoolSyncStatusView,
)
async def get_pool_sync_status(db: DbDep) -> PoolSyncStatusView:
    """上次同步元信息与已采集期次数；从未同步时 last_run 为空。"""
    return _sync_status(db)


def _sync_status(db: sqlite3.Connection) -> PoolSyncStatusView:
    row = pool_store.latest_pool_sync_run(db)
    last = (
        PoolSyncRunView(
            source=str(row["source"]),
            observed_at=str(row["observed_at"]),
            period_nos=json.loads(str(row["period_nos"])),
            pages=int(row["pages"]),
            matches=int(row["matches"]),
            share_rows=int(row["share_rows"]),
            missing_shares=int(row["missing_shares"]),
        )
        if row is not None
        else None
    )
    return PoolSyncStatusView(
        last_run=last, period_count=pool_store.pool_period_count(db)
    )


@router.post(
    "/api/v1/pool-states",
    summary="AI 代采导入: 官方彩池销量/滚存",
    status_code=201,
    response_model=PoolStateImportView,
    responses={404: {"description": "期次不存在(先运行同步建期次)"}},
)
async def import_pool_state(
    payload: PoolStateImportPayload, db: DbDep
) -> PoolStateImportView:
    """
    彩池资金状态代采入口（票 43 兜底层；官方销量四源直接 GET 不可得）。

    代理用浏览器读官方公布页 → 结构化 → 本端点；source 记 agent；
    幂等：同值重放不改库，新值覆盖（状态表语义=最新公布为准）。
    触发方式=用户命令，不做自动定时。
    """
    pool_period_id = pool_store.pool_period_id(
        db, zucai.PERIOD_MARKET, payload.period_no
    )
    if pool_period_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"period {payload.period_no} not found; run pool-sync first",
        )
    pool_store.upsert_pool_state(
        db,
        pool_period_id,
        sales_amount=payload.sales_amount,
        rollover_in=payload.rollover_in,
        prize_tiers=payload.prize_tiers,
        published_at=payload.published_at,
        source="agent",
    )
    db.commit()
    return PoolStateImportView(period_no=payload.period_no, imported=True)
