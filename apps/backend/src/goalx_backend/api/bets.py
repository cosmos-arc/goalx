"""投注 API：注级建议、票级回录与复盘列表(票 23/36)。"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from goalx_backend.api.deps import get_db
from goalx_backend.betting import store as bt_store
from goalx_backend.betting.bets import (
    ActualTerms,
    BetDraft,
    BetEligibilityError,
    IntentConflictError,
    PriceMismatchError,
    create_bet_with_legs,
    intent_fingerprint,
    record_purchase,
    replay_bet_id,
    resolve_paper_price,
    validate_had_selections,
)
from goalx_backend.betting.staking import StakeSuggestion, suggest_stake
from goalx_backend.data import corpus_duckdb
from goalx_backend.data.pool import pool_period_exists
from goalx_backend.db import atomic
from goalx_backend.evaluation.bet_review import BetReviewView, review_views
from goalx_backend.markets import LEG_MARKETS, LEG_SELECTIONS
from goalx_backend.models import (
    PRICE_SOURCE_LIVE_BACKFILL,
    BetMode,
    IsoTimestamp,
    LegInput,
    MoneyCny,
)

router = APIRouter(tags=["bets"])
DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


class LegPayload(BaseModel):
    """一腿输入（市场/选项按 markets 闭集拒收，审计批B #7）。"""

    fixture_id: int
    market_code: str
    selection_code: str
    locked_odds: float = Field(gt=0)
    goal_line: float | None = None

    @field_validator("market_code")
    @classmethod
    def _check_market(cls, value: str) -> str:
        if value not in LEG_MARKETS:
            raise ValueError(f"unsupported_market:{value}")
        return value

    @field_validator("selection_code")
    @classmethod
    def _check_selection(cls, value: str, info: ValidationInfo) -> str:
        allowed = LEG_SELECTIONS.get(str(info.data.get("market_code")))
        if allowed is not None and value not in allowed:
            raise ValueError(f"unsupported_selection:{value}")
        return value


class BetCreate(BaseModel):
    """建注(建议或直接回录)输入。"""

    mode: BetMode
    stake: MoneyCny
    legs: list[LegPayload] = Field(min_length=1)
    strategy_version: str | None = None
    # 手工研究假设通道(票 04 三分类③):客户端价不被系统报价约束,
    # 落 manual_research 标签,不进正式纸面成绩面(None/False=系统价通道)
    allow_manual_price: bool | None = None
    # 客户端幂等键(票 06):同键重复提交不落双行(回带既有注);
    # 腿级单关=腿稳定键,串关=腿键有序拼接;不传=旧行为
    client_request_id: str | None = Field(default=None, min_length=1, max_length=200)


class SlipCreate(BaseModel):
    """票级回录输入：勾选实际购买的建议注子集；live 可附实际条款。"""

    bet_ids: list[int] = Field(min_length=1)
    placed_at: IsoTimestamp | None = None
    # 域模型直用（审计批C 附录⑦ 收敛）：ActualTerms 金额约束单一来源，
    # API 层不再维护同形副本
    actuals: dict[str, ActualTerms] = Field(default_factory=dict)
    # 价变确认(票 04 裁决①):锁定时服务器报价已变,409 返回新价后,
    # 客户端确认按服务器价落库则置 true 重提(None/False=未确认)
    allow_price_update: bool | None = None


class PoolPickPayload(BaseModel):
    """复式票一格的一选。"""

    match_seq: int
    selection_code: str
    fixture_id: int | None = None


class PoolSlipCreate(BaseModel):
    """
    创建池票（任9/14 场/goals4 复式）；market_code 为票级 Market 口径（票 01/10）。

    market_code=pick9：组合 = 全部 9 场子集 × 各子集内逐场 picks 之积
    （需 ≥9 场）；ttt14：整体所选场连乘（14 腿不动）；goals4：4 场固定、
    每场主/客档各一选（复式=|主档|×|客档| 积，票 10）。缺省 pick9——
    任9 页是默认池票 UI；期次承载赛程，Market 归票不归期次。
    """

    mode: BetMode
    pool_period_id: int | None = None
    market_code: Literal["pick9", "ttt14", "goals4"] = "pick9"
    note: str | None = None
    stake_per_combination: MoneyCny = 2.0
    # picks 上限（correctness F1）：存在性探测 IN 列表有界——>32766 去重 id
    # 超 SQLite 主机参数上限裸 500；64 覆盖 goals4 复式 4×8 极值
    picks: list[PoolPickPayload] = Field(min_length=1, max_length=64)


class PoolSlipPreviewRequest(BaseModel):
    """复式组合预览输入（票 01/10）：与创建同载荷减 mode/note/期次（只读）。"""

    market_code: Literal["pick9", "ttt14", "goals4"] = "pick9"
    stake_per_combination: MoneyCny = 2.0
    picks: list[PoolPickPayload] = Field(min_length=1, max_length=64)


class PoolSlipPreview(BaseModel):
    """复式组合预览（票 01）：注数/总金额/组合流摘要——展示与落库同源。"""

    market_code: str
    combination_count: int
    stake_per_combination: float
    total_stake: float
    combinations_sha256: str


class BetLegView(BaseModel):
    """一腿视图。"""

    fixture_id: int
    market_code: str
    selection_code: str
    locked_odds: float
    actual_odds: float | None = None
    goal_line: float | None = None
    snapshot_id: int | None = None  # 服务器报价快照身份(票 04;未绑定为 null)


class BetEvSnapshotView(BaseModel):
    """
    注级 EV 快照（票 41）：建注时刻双口径概率与 EV；存量注无快照为 null。

    票 04 注记：锁定价重验（allow_price_update）刷新 locked_odds 但 EV
    快照不倒填——EV 口径恒为建注时刻，对照实绩时留意价已重绑。
    """

    prob_consensus: float | None = None
    prob_model: float | None = None
    ev_consensus: float | None = None
    ev_model: float | None = None


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
    ev_snapshot: BetEvSnapshotView | None = None
    # 价格来源三分类(票 04 裁决② 面板可见维度): system_paper/
    # live_backfill/manual_research;历史注 null
    price_source: str | None = None
    # 客户端幂等键(票 06,永久保留——对账需要);未带键的历史注 null
    client_request_id: str | None = None


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
    # 池票复式组合回读（票 01）：活口径计数/金额（隔离史行已排除）+
    # 隔离行计数（对账注记，人裁决：原行保留不改写）；固定票恒 0。
    combination_count: int
    pool_stake_total: float
    quarantined_combinations: int


class LegQuotePayload(BaseModel):
    """价变 409 载荷一腿：服务器当前有效报价（含快照身份）。"""

    fixture_id: int
    selection_code: str
    server_odds: float
    snapshot_id: int
    observed_at: str


def _price_mismatch_schema() -> dict[str, object]:
    """
    价变 409 载荷 schema（契约建模，票 04 裁决①）。

    由 LegQuotePayload 自建（无嵌套模型、无 $defs）——FastAPI 0.13x 的
    openapi() 不接受 responses content 内的 model 类，嵌套 $defs 又无法
    在响应路径下解析。外层 detail=HTTPException 包装（correctness F2：
    wire 实形与契约对齐）。
    """
    return {
        "type": "object",
        "properties": {
            "detail": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "current_quotes": {
                        "type": "array",
                        "items": LegQuotePayload.model_json_schema(),
                    },
                },
                "required": ["message", "current_quotes"],
            }
        },
        "required": ["detail"],
    }


def _bet_conflict_schema() -> dict[str, object]:
    """
    建注 409 载荷 schema（票 04 价变 + 票 09 意图冲突两形态，oneOf 互斥）。

    价变 → current_quotes 必有（票 04 裁决①语义不放宽）；同 client_request_id
    异意图 → existing_bet（原注摘要，恢复原结果用）。两形态均 message 必有。
    外层 detail=HTTPException 包装（correctness F2：wire 实形与契约对齐）。
    """
    quote_schema = LegQuotePayload.model_json_schema()
    return {
        "type": "object",
        "properties": {
            "detail": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                            "current_quotes": {"type": "array", "items": quote_schema},
                        },
                        "required": ["message", "current_quotes"],
                    },
                    {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                            "existing_bet": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "integer"},
                                    "mode": {
                                        "type": "string",
                                        "enum": ["paper", "live"],
                                    },
                                    "stake": {
                                        "type": "number",
                                        "exclusiveMinimum": 0,
                                    },
                                },
                                "required": ["id", "mode", "stake"],
                            },
                        },
                        "required": ["message", "existing_bet"],
                    },
                ]
            }
        },
        "required": ["detail"],
    }


class StakeAdviceRequest(BaseModel):
    """建议仓位输入（票 wb-06）：串关由调用方传联合赔率/联合 EV（整注口径）。"""

    mode: BetMode
    bankroll: float = Field(ge=0)
    ev: float
    odds: float = Field(gt=1)
    cap_fraction: float = Field(default=0.05, gt=0, le=0.05)


@router.post(
    "/api/v1/stake-advice",
    summary="建议仓位(只读): 纸面 flat / 真金 ¼Kelly + 单注上限截断",
    response_model=StakeSuggestion,
)
async def stake_advice(payload: StakeAdviceRequest) -> StakeSuggestion:
    """
    只读注额建议（票 wb-06，不自动改单）。

    EV≤0 → ¥0；paper 一律 flat（红线，与组合引擎 flat 档同口径）；
    live = ¼ fractional Kelly（f* = EV/(odds-1) 取 1/4）截断单注 cap%；
    不足 1% 下限→竞彩最低注 ¥2 参与或跳过（人裁决，measure-09）。
    串关注额=单关口径（传联合赔率/联合 EV，整注一个 Kelly，不分腿）。
    """
    return suggest_stake(
        payload.mode, payload.bankroll, payload.ev, payload.odds, payload.cap_fraction
    )


def _ev_snapshot_view(row: sqlite3.Row) -> BetEvSnapshotView | None:
    """行 → 注级 EV 快照视图；四列全空（存量注/口径外注）返回 None。"""
    snapshot = BetEvSnapshotView(
        prob_consensus=row["snap_prob_consensus"],
        prob_model=row["snap_prob_model"],
        ev_consensus=row["snap_ev_consensus"],
        ev_model=row["snap_ev_model"],
    )
    fields = (
        snapshot.prob_consensus,
        snapshot.prob_model,
        snapshot.ev_consensus,
        snapshot.ev_model,
    )
    return snapshot if any(value is not None for value in fields) else None


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
        ev_snapshot=_ev_snapshot_view(row),
        price_source=row["price_source"],
        client_request_id=row["client_request_id"],
    )


def _replay_or_conflict(
    db: sqlite3.Connection,
    client_request_id: str,
    fingerprint: str | None,
) -> BetView | None:
    """
    同键重放判定（票 06/09，API 视图层）。

    判定单源在域层 replay_bet_id（审计批C 附录⑦ 收敛），本函数只补
    BetView 呈现：同意图 → 既有注视图（201 同体回带）；异意图 →
    IntentConflictError（HTTP 层统一映射 409，不静默吞改）；无既有注/
    存量无指纹行 → None 走正常建注（v37 期旧行无可比对，降级旧回带）。
    并发竞态窗由域层同一异常兜底。
    """
    existing_id = replay_bet_id(db, client_request_id, fingerprint)
    if existing_id is None:
        return None
    existing = bt_store.get_bet(db, existing_id)
    if existing is None:
        return None
    return _bet_view(existing, review_views(db, [existing]).get(existing_id))


def _persist_new_bet(
    db: sqlite3.Connection,
    payload: BetCreate,
    legs: list[LegInput],
    fingerprint: str | None,
) -> int:
    """资格校验 + 价绑定 + 落库（create_bet 的建新注内块；返回 bet id）。"""
    with corpus_duckdb.reader() as duck_con:
        validate_had_selections(db, legs, duck_con=duck_con)
        if payload.mode is BetMode.LIVE:
            # live 回录独立路径：按实际条款结算，不要求等于系统报价
            bound, price_source = legs, PRICE_SOURCE_LIVE_BACKFILL
        else:
            bound, price_source, mismatches = resolve_paper_price(db, legs)
            if mismatches and not payload.allow_manual_price:
                raise PriceMismatchError(mismatches)
            if mismatches:
                # 显式手工价通道：客户端价保留，类标签③
                price_source = "manual_research"
        return create_bet_with_legs(
            db,
            BetDraft(
                mode=payload.mode,
                stake=payload.stake,
                legs=bound,
                strategy_version=payload.strategy_version,
                client_request_id=payload.client_request_id,
                intent_fingerprint=fingerprint,
            ),
            duck_con=duck_con,
            price_source=price_source,
        )


@router.post(
    "/api/v1/bets",
    summary="建注(注级建议; 校验 had 资格+纸面价绑定; 同键同意图回带/异意图409)",
    status_code=201,
    response_model=BetView,
    responses={
        400: {"description": "非法串关(同场多腿)或 had 资格不满足(停售/已开赛/非单固)"},
        409: {
            "description": (
                "纸面价与服务器当前有效报价不一致(返回新价; "
                "按 server_odds 重提,或 allow_manual_price 走研究假设); "
                "或 client_request_id 已绑定不同提交意图(返回 existing_bet "
                "原注摘要; 换新键按新意图另建)"
            ),
            "content": {"application/json": {"schema": _bet_conflict_schema()}},
        },
    },
)
def create_bet(payload: BetCreate, db: DbDep) -> BetView:
    """
    创建一注(paper/live；未购建议 purchased=false)；had 腿按共享证据判定资格。

    live 注单唯一动线=赛前建行(本端点,开赛后新建被资格门拒)+赛后
    POST /bet-slips slip 回录(事后记账,不重校验)——开赛后全新建 live
    行不是受支持语义(审计 #10 裁决, 2026-11-05)。

    纸面价绑定(boundary-hardening 票 04)：paper 建注时服务器重取当前有效
    had 报价与客户端价比对——价变 409 返回新价要求确认(裁决①)；一致则
    system_paper 标签+腿级快照身份落库；报价不可得(或非 had)为
    manual_research(不进正式成绩面)；live 一律 live_backfill 不收紧。

    幂等(票 06/09)：client_request_id 同键重复提交——同意图回带既有注
    (201 同体)不落双行；异意图(改金额/模式/选注)409 带原注摘要，不静默
    吞改。意图指纹=提交载荷业务字段规范形哈希(建注时一次落库)，比对
    对象是不可变原始提交，不读锁票重绑后的行。存量无指纹行(v37 期)
    无可比对 → 维持旧回带(降级)。重放在资格/价绑校验**之前**短路：
    原决策已落库，腿事后停售/价变不改变"该请求已建注"的事实。
    """
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
    fingerprint = (
        intent_fingerprint(payload.mode, payload.stake, legs)
        if payload.client_request_id is not None
        else None
    )
    try:
        # 重放预检在资格/价绑之前（原决策已落库短路）；异意图在此或域层
        # 并发竞态窗内抛 IntentConflictError，统一映射 409
        if payload.client_request_id is not None:
            replayed = _replay_or_conflict(db, payload.client_request_id, fingerprint)
            if replayed is not None:
                return replayed
        bet_id = _persist_new_bet(db, payload, legs, fingerprint)
    except IntentConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc
    except PriceMismatchError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "current_quotes": [q.model_dump() for q in exc.current_quotes],
            },
        ) from exc
    except BetEligibilityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = bt_store.get_bet(db, bet_id)
    if row is None:
        raise HTTPException(status_code=500, detail="bet vanished after insert")
    return _bet_view(row, review_views(db, [row]).get(bet_id))


@router.get(
    "/api/v1/bets",
    summary="复盘列表(注级; 含前瞻资格与 closing 完整性)",
    response_model=list[BetView],
)
async def list_bets(
    db: DbDep, mode: BetMode | None = None, only_open: bool = False
) -> list[BetView]:
    """全部投注记录：结算状态、盈亏、建议 vs 实际条款、复盘资格。"""
    # 票 04 D11：列表端点有界（最新 100，bankroll 先例同量级）；截断不改形
    rows = bt_store.list_bets(db, mode=mode, only_open=only_open, limit=100)
    reviews = review_views(db, rows)
    return [_bet_view(row, reviews.get(int(row["id"]))) for row in rows]


@router.post(
    "/api/v1/bet-slips",
    summary="票级回录(paper 提交时重新校验停售/过期+价重验; live 可附实际条款)",
    status_code=201,
    response_model=SlipView,
    responses={
        400: {
            "description": (
                "mode 混用、paper 携带实际条款(actuals 仅限 live 回录)、或 paper "
                "锁定 had 资格不满足(停售/已开赛/非单固)"
            )
        },
        404: {"description": "bet 不存在"},
        409: {
            "description": (
                "system_paper 注锁定时服务器报价已变(返回新价; "
                "确认按服务器价落库则 allow_price_update=true 重提)"
            ),
            "content": {"application/json": {"schema": _price_mismatch_schema()}},
        },
    },
)
def create_slip(payload: SlipCreate, db: DbDep) -> SlipView:
    """
    把勾选的建议注合成一张票并标记已购。

    paper 锁定是正式赛前决策，服务器按共享证据再校验停售/过期；system_paper
    注另做价重验（票 04）：报价已变 409 返回新价，确认后按服务器价落库；
    live 回录是事后记账（赛后仍可入账），由前瞻资格规则排除，不再校验。
    """
    try:
        # actuals 键推导在 try 内（审计批B #6）：非整数键 int() 的
        # ValueError 复用下方 400 映射，此前在 try 外裸 500
        actuals: dict[int, ActualTerms] = {
            int(bet_id): terms for bet_id, terms in payload.actuals.items()
        }
        # had 资格校验在购买事务内（审计批C 附录④，域层 record_purchase
        # 承担）：此前在事务外预检，校验与落库之间留 TOCTOU 窗；bet 存在
        # 性同样由域层 _load_purchasable_bets 判（404 映射不变）
        with corpus_duckdb.reader() as duck_con:
            slip_id = record_purchase(
                db,
                payload.bet_ids,
                payload.placed_at,
                actuals=actuals,
                allow_price_update=bool(payload.allow_price_update),
                duck_con=duck_con,
            )
    except PriceMismatchError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "current_quotes": [q.model_dump() for q in exc.current_quotes],
            },
        ) from exc
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
        # SUM 聚合字段统一 round 到分（票 01 三方一致；浮点尾差不外漏——
        # 此前只 round 了 pool 字段，stake/profit 裸 float 会渗出 8.000000000000002）
        stake_total=round(float(row["stake_total"]), 2),
        profit_total=round(float(row["profit_total"]), 2),
        combination_count=int(row["combination_count"]),
        pool_stake_total=round(float(row["pool_stake_total"]), 2),
        quarantined_combinations=int(row["quarantined_combinations"]),
    )


@router.get(
    "/api/v1/bet-slips",
    summary="票列表",
    response_model=list[SlipView],
)
async def list_slips(db: DbDep) -> list[SlipView]:
    """全部投注票及聚合（票 04 D11：最新 100——每行 6 个相关子查询）。"""
    return [_slip_view(row) for row in bt_store.list_slips(db, limit=100)]


@router.post(
    "/api/v1/pool-slips/preview",
    summary="复式组合预览(任9 9选子积/14场连乘/goals4双侧积): 注数/总金额/摘要",
    response_model=PoolSlipPreview,
    responses={
        400: {
            "description": "选择码不在闭集内(任9/ttt14=3/1/0, goals4=h0..h3/a0..a3)",
        },
        422: {
            "description": "复式组合数超上限(10万);载荷校验失败",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
                }
            },
        },
    },
)
def preview_pool_slip(payload: PoolSlipPreviewRequest, db: DbDep) -> PoolSlipPreview:
    """
    只读组合预览（票 01）：与创建同源——三方一致。

    同一套矩阵/计数/展开：前端展示、落库组合、SlipView 回读共用；
    超上限同创建口径 422 拒绝；任9 已选 <9 场 → 组合数 0（未成票的
    诚实读数，非错误；创建端点对 <9 场直接 400）。选择码闭集与创建
    同校验（票 08）：非法码 400——"未选完整"可预览，非法载荷不可。
    """
    by_match = bt_store.pool_pick_matrix(
        (pick.match_seq, pick.selection_code) for pick in payload.picks
    )
    try:
        bt_store.validate_pool_selection_codes(by_match, payload.market_code)
        bt_store.require_fixtures_exist(db, [pick.fixture_id for pick in payload.picks])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # "未成票"=诚实 0（审计批C #9）：判定单源在 store（ttt14 与创建侧
    # 同判 len != 14），不再走通用连乘算出"可建的 N 注"再吃创建 400
    if bt_store.pool_slip_incomplete(by_match, payload.market_code):
        return PoolSlipPreview(
            market_code=payload.market_code,
            combination_count=0,
            stake_per_combination=payload.stake_per_combination,
            total_stake=0.0,
            combinations_sha256=bt_store.EMPTY_SELECTIONS_SHA256,
        )
    try:
        count = bt_store.count_pool_combinations(by_match, payload.market_code)
    except bt_store.CombinationLimitExceeded as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PoolSlipPreview(
        market_code=payload.market_code,
        combination_count=count,
        stake_per_combination=payload.stake_per_combination,
        total_stake=round(count * payload.stake_per_combination, 2),
        combinations_sha256=bt_store.pool_selections_sha256(
            by_match, payload.market_code
        ),
    )


@router.post(
    "/api/v1/pool-slips",
    summary="创建待结算的 paper 池票草稿",
    responses={
        400: {
            "description": (
                "暂不支持 live 奖池票; 任9 已选不足 9 场; ttt14 需 14 场; "
                "选择码不在闭集; 场次不属于所指定期次"
            )
        },
        422: {
            "description": "复式组合数超上限(10万);载荷校验失败",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
                }
            },
        },
    },
    status_code=201,
    response_model=SlipView,
)
def create_pool_slip(payload: PoolSlipCreate, db: DbDep) -> SlipView:
    """
    按 picks 建 paper 复式草稿; 奖金引擎未实现, 保持待结算。

    组合按票级 Market 展开（票 01）：pick9 = 全部 9 场子集 × 各子集内
    picks 之积（<9 场 400）；ttt14 = 整体连乘（票 08：需 14 场完备）。
    载荷守卫（票 08）：选择码闭集（预览/创建同校验）+ 已指定期次的场次
    归属；超上限 422 整票拒绝（人裁决：拒绝不截断）。

    组合物化（上限内也 up to 10 万行 json 序列化）是同步重活——def 走
    线程池不占事件循环（correctness F6，同票 07①）。
    """
    if payload.mode is BetMode.LIVE:
        raise HTTPException(
            status_code=400, detail="奖池奖金未实现, 暂不支持 live 池票"
        )
    if (
        payload.pool_period_id is not None
        and pool_period_exists(db, payload.pool_period_id) is False
    ):
        # 前后端贯穿并校验（票 04 范围③）：非法期次身份 4xx 而非 FK 裸 500
        raise HTTPException(
            status_code=404, detail=f"pool period {payload.pool_period_id} 不存在"
        )
    by_match = bt_store.pool_pick_matrix(
        (pick.match_seq, pick.selection_code) for pick in payload.picks
    )
    try:
        # 预览/创建同校验（票 08）：闭集 + ttt14 14 场完备——创建不收
        # "未选完整"票；预览端点的宽松态只在预览侧保留
        bt_store.require_pool_slip_creatable(by_match, payload.market_code)
        # pick 的 fixture_id 须真实存在（票 08 预存注记收口：裸 FK 500 → 400）
        bt_store.require_fixtures_exist(db, [pick.fixture_id for pick in payload.picks])
        if payload.pool_period_id is not None:
            # 已指定期次 → 场次归属（票 08）：跨期 match_seq 不落库
            bt_store.validate_pool_period_ownership(
                db, by_match, payload.pool_period_id
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
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
                db,
                slip,
                stake_per_combination=payload.stake_per_combination,
                market_code=payload.market_code,
            )
    except bt_store.CombinationLimitExceeded as exc:
        # 票 07④：组合数超上限=4xx 拒绝（atomic 回滚,不落半截票）。
        # 专用异常而非裸 ValueError——同块其他来源的 ValueError 不被误标
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        # 票 01：任9 <9 场 / 未知 Market = 400（atomic 回滚）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = bt_store.get_slip(db, slip)
    if row is None:
        raise HTTPException(status_code=500, detail="slip vanished after insert")
    return _slip_view(row)
