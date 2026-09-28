"""领域实体与仓储输入模型（Pydantic），与 CONTEXT.md 六域术语对应。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Tier(StrEnum):
    """赛事投入分层（票 17）。"""

    TIER1 = "tier1"
    TIER2 = "tier2"


class MarketKind(StrEnum):
    """固定赔率（竞彩）与奖池型（传统足彩）市场。"""

    FIXED = "fixed"
    POOL = "pool"


class BetMode(StrEnum):
    """ADR 0002：纸面与真金统一 Bet 实体，以 mode 区分。"""

    PAPER = "paper"
    LIVE = "live"


class BetStatus(StrEnum):
    """投注记录的结算状态。"""

    OPEN = "open"
    WON = "won"
    LOST = "lost"
    VOID = "void"
    PARTIAL = "partial"


class SnapshotPurpose(StrEnum):
    """OddsSnapshot 的采集用途。"""

    LIVE_CAPTURE = "live_capture"
    CLOSING = "closing"


# --- 赛程域 ---


class Fixture(BaseModel):
    """一场已排期的比赛（UTC 记时）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    competition_id: int
    kickoff_utc: str
    home_team_id: int
    away_team_id: int
    stage: str | None = None
    odds_api_event_id: str | None = None
    odds_api_sport_key: str | None = None
    join_method: str | None = None


# --- 市场域 ---


class ObservationPurpose(StrEnum):
    """一次报价观测的获取模式（票 35：实时观测与历史查询分开）。"""

    LIVE = "live"


class OddsSnapshot(BaseModel):
    """
    某时点、某来源、对某 Selection 的一次报价（append-only）。

    captured_at 的含义按源解释（票 35）：sporttery = 源调盘时间
    （= source_updated_at）；odds_api = 本机观测时间（= observed_at，
    仅当源时间缺失时）。旧行 observed_at/source_updated_at 为 NULL。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    fixture_id: int
    market_code: str
    selection_code: str
    source: str
    purpose: SnapshotPurpose = SnapshotPurpose.LIVE_CAPTURE
    odds: float
    captured_at: str
    observed_at: str | None = None
    source_updated_at: str | None = None
    observation_id: int | None = None
    meta: dict[str, str] | None = None


# --- 预测域 ---


class Forecast(BaseModel):
    """一次概率输出（10×10 比分矩阵，ADR 0006），内容哈希存证。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    fixture_id: int
    track: str
    model_version: str
    issued_at: str
    content_hash: str
    payload: dict[str, object]


# --- 事实与结算域 ---


class DrawResult(BaseModel):
    """官方开奖结果（系统唯一事实源，ADR 0001）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    fixture_id: int
    home_goals: int
    away_goals: int
    half_home_goals: int | None = None
    half_away_goals: int | None = None
    void: bool = False
    void_reason: str | None = None
    source: str
    published_at: str | None = None


# --- 投注域 ---


class BetLeg(BaseModel):
    """串关投注中的一腿：引用 Selection 与下注时锁定的赔率。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bet_id: int
    fixture_id: int
    market_code: str
    selection_code: str
    locked_odds: float
    snapshot_id: int | None = None
    goal_line: float | None = None


class Bet(BaseModel):
    """一次投注记录（纸面或真金，ADR 0002）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slip_id: int | None = None
    mode: BetMode
    market_kind: MarketKind
    purchased: bool = False
    stake: float
    placed_at: str | None = None
    created_at: str
    status: BetStatus = BetStatus.OPEN
    payout: float | None = None
    profit: float | None = None
    settled_at: str | None = None


class BetSlip(BaseModel):
    """一张实际投注票（复式票含多个 Combination）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    mode: BetMode
    source: str = "manual"
    pool_period_id: int | None = None
    placed_at: str | None = None
    note: str | None = None
    created_at: str


class Combination(BaseModel):
    """复式票中的一个具体组合（如任 9 的 9 场各一选）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slip_id: int
    seq: int
    stake: float
    selections: list[dict[str, object]]
    hit: bool | None = None
    payout: float | None = None


class Settlement(BaseModel):
    """按官方规则对一张 BetSlip/Bet 的兑付计算结果。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bet_id: int | None = None
    slip_id: int | None = None
    status: BetStatus
    stake: float
    payout: float
    profit: float
    detail: dict[str, object]
    computed_at: str


# --- 仓储输入模型（聚合多参数，保持仓储函数签名精简） ---


class MatchCodeInput(BaseModel):
    """upsert_match_code 的输入。"""

    fixture_id: int
    kind: str
    business_date: str
    code: str
    source_match_id: str | None = None
    is_single: bool | None = None


class ObservationInput(BaseModel):
    """record_quote_observation 的输入（raw_ref/摘要由采集方填）。"""

    source: str
    purpose: ObservationPurpose = ObservationPurpose.LIVE
    observed_at: str
    source_updated_at: str | None = None
    snapshot_at: str | None = None
    endpoint: str | None = None
    parse_version: str
    raw_sha256: str
    raw_ref: str | None = None
    summary: str | None = None


class SaleStatusInput(BaseModel):
    """append_sale_status 的输入。"""

    fixture_id: int
    market_code: str | None = None
    sale_state: str
    single_eligible: bool | None = None
    observed_at: str
    source_updated_at: str | None = None
    observation_id: int | None = None


class SnapshotInput(BaseModel):
    """insert_odds_snapshot 的输入。"""

    fixture_id: int
    market_code: str
    selection_code: str
    source: str
    odds: float
    captured_at: str
    purpose: SnapshotPurpose = SnapshotPurpose.LIVE_CAPTURE
    observed_at: str | None = None
    source_updated_at: str | None = None
    observation_id: int | None = None
    meta: dict[str, str] | None = None


class LegInput(BaseModel):
    """add_leg 的输入。"""

    fixture_id: int
    market_code: str
    selection_code: str
    locked_odds: float
    snapshot_id: int | None = None
    goal_line: float | None = None


class SettlementInput(BaseModel):
    """save_settlement 的输入。"""

    bet_id: int | None = None
    slip_id: int | None = None
    status: BetStatus
    stake: float
    payout: float
    profit: float
    detail: dict[str, object]


class DrawResultInput(BaseModel):
    """upsert_draw_result 的输入。"""

    fixture_id: int
    home_goals: int
    away_goals: int
    half_home_goals: int | None = None
    half_away_goals: int | None = None
    void: bool = False
    void_reason: str | None = None
    source: str = "manual"
    published_at: str | None = None
    correction_reason: str | None = None
