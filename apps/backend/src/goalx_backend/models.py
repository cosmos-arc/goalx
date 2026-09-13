"""领域实体与仓储输入模型（Pydantic），与 CONTEXT.md 六域术语对应。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Tier(StrEnum):
    """赛事投入分层（票 17）。"""

    TIER1 = "tier1"
    TIER2 = "tier2"
    EXCLUDED = "excluded"


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
    BACKTEST = "backtest"


# --- 赛程域 ---


class Competition(BaseModel):
    """一项赛事（联赛或杯赛），带投入分层标签。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    tier: Tier = Tier.TIER2
    odds_api_sport_key: str | None = None
    api_football_league_id: int | None = None


class Team(BaseModel):
    """一支球队；别名经 TeamAlias 映射到 canonical 实体。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    canonical_name: str


class TeamAlias(BaseModel):
    """外部源球队名 → canonical 球队的持久化映射。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    team_id: int
    source: str
    alias: str


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


class MatchCode(BaseModel):
    """官方销售编号（竞彩“周二 301”），附属于 Fixture。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    fixture_id: int
    kind: str
    business_date: str
    code: str
    source_match_id: str | None = None
    is_single: bool | None = None


# --- 市场域 ---


class OddsSnapshot(BaseModel):
    """某时点、某来源、对某 Selection 的一次报价（append-only）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    fixture_id: int
    market_code: str
    selection_code: str
    source: str
    purpose: SnapshotPurpose = SnapshotPurpose.LIVE_CAPTURE
    odds: float
    captured_at: str
    meta: dict[str, str] | None = None


# --- 奖池域 ---


class PoolPeriod(BaseModel):
    """一个奖池型玩法的销售期（如胜负彩第 26029 期）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    market_code: str
    period_no: str
    sales_deadline: str | None = None


class PoolState(BaseModel):
    """一个 PoolPeriod 的资金状态（销售额/滚存转入/奖级分配）。"""

    model_config = ConfigDict(from_attributes=True)

    pool_period_id: int
    sales_amount: float | None = None
    rollover_in: float | None = None
    prize_tiers: dict[str, object] | None = None
    published_at: str | None = None


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


class CostEntry(BaseModel):
    """CostLedger 记账行（数据订阅/LLM 调用/API credit）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: str
    category: str
    units: float = 1.0
    amount_cny: float = 0.0
    note: str | None = None
    meta: dict[str, object] | None = None


class BankrollEvent(BaseModel):
    """Bankroll 变动事件（仅 live 模式影响，ADR 0002）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: str
    kind: str
    amount_cny: float
    balance_after: float
    bet_id: int | None = Field(default=None)
    slip_id: int | None = None
    note: str | None = None


# --- 历史回测底座（票 21）---


class HistMatch(BaseModel):
    """football-data.co.uk 历史行（ADR 0007 回测基准输入）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    competition: str
    season: str
    match_date: str
    home_team: str
    away_team: str
    fthg: int
    ftag: int
    ftr: str
    psc_home: float | None = None
    psc_draw: float | None = None
    psc_away: float | None = None
    avgc_home: float | None = None
    avgc_draw: float | None = None
    avgc_away: float | None = None


# --- 仓储输入模型（聚合多参数，保持仓储函数签名精简） ---


class MatchCodeInput(BaseModel):
    """upsert_match_code 的输入。"""

    fixture_id: int
    kind: str
    business_date: str
    code: str
    source_match_id: str | None = None
    is_single: bool | None = None


class SnapshotInput(BaseModel):
    """insert_odds_snapshot 的输入。"""

    fixture_id: int
    market_code: str
    selection_code: str
    source: str
    odds: float
    captured_at: str
    purpose: SnapshotPurpose = SnapshotPurpose.LIVE_CAPTURE
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
