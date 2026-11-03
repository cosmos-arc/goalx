"""Bet 生命周期用例：建注（同场串关校验）、资格判定与购买回录（票级，live 扣款）。"""

from __future__ import annotations

import json
import math
import sqlite3

from pydantic import BaseModel, Field

from goalx_backend.betting import store as bt_store
from goalx_backend.betting.snapshot import build_ev_snapshot
from goalx_backend.data import mapping, quote_evidence
from goalx_backend.data.quote_evidence import adjudicate_had_quote
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.models import BetMode, LegInput, MarketKind

# 价格来源三分类（boundary-hardening 票 04）：
# - system_paper 系统纸面决策：服务器重取报价验证，腿级证据身份落
#   bet_legs.snapshot_id——唯一进正式纸面成绩面的类；
# - live_backfill 真实票据回录：按实际条款结算，不要求等于系统报价
#   （live 路径不因本票收紧）；
# - manual_research 手工研究假设：服务器报价不可得或明确改用手工价，
#   不进正式可执行性验证。
PRICE_SOURCE_SYSTEM_PAPER = "system_paper"
PRICE_SOURCE_LIVE_BACKFILL = "live_backfill"
PRICE_SOURCE_MANUAL_RESEARCH = "manual_research"
PRICE_SOURCES = frozenset(
    {
        PRICE_SOURCE_SYSTEM_PAPER,
        PRICE_SOURCE_LIVE_BACKFILL,
        PRICE_SOURCE_MANUAL_RESEARCH,
    }
)
# 结算/CLV 侧消费口径（票 04 范围④）：正式纸面成绩面只认第一类——
# 历史 NULL（人裁决 2026-11-02 不回溯打标）与非第一类一律排除。
OFFICIAL_PAPER_PRICE_SOURCES = frozenset({PRICE_SOURCE_SYSTEM_PAPER})
# 客户端价与服务器价相等容差（同一十进制字面量解析，只防浮点尾差）
_ODDS_EPSILON = 1e-9


def is_official_paper(row: sqlite3.Row) -> bool:
    """该注是否进正式纸面成绩面（消费侧唯一判据：paper 且三分类第一类）。"""
    official = row["price_source"] in OFFICIAL_PAPER_PRICE_SOURCES
    return str(row["mode"]) == "paper" and official


class SameFixtureParlayError(ValueError):
    """竞彩禁止同场串关（研究 01）。"""


class BetEligibilityError(ValueError):
    """had 选择/锁定不满足服务端共享证据资格（票 36；界面禁用不替代本判定）。"""


class PriceMismatchError(ValueError):
    """
    纸面价与服务器当前有效报价不一致（票 04 裁决①：返回新价要求确认）。

    current_quotes 每腿：fixture_id/selection_code/server_odds/snapshot_id/
    observed_at——调用方（HTTP 层）以 409 呈现，客户端确认后按 server_odds
    重提（或显式 allow_manual_price 走研究假设类）。
    """

    def __init__(self, current_quotes: list[dict[str, object]]) -> None:
        self.current_quotes = current_quotes
        parts = ", ".join(
            "{} {}: {}（快照 #{}）".format(
                q["fixture_id"], q["selection_code"], q["server_odds"], q["snapshot_id"]
            )
            for q in current_quotes
        )
        super().__init__(f"报价已变: {parts}")


class LegQuoteView(BaseModel):
    """一腿的服务器当前报价（价变 409 载荷）。"""

    fixture_id: int
    selection_code: str
    server_odds: float
    snapshot_id: int
    observed_at: str


class BetDraft(BaseModel):
    """一注建议/回录的输入（API 反序列化后）。"""

    mode: BetMode
    stake: float = Field(gt=0, allow_inf_nan=False)
    legs: list[LegInput] = Field(min_length=1)
    strategy_version: str | None = None


class ActualLegOdds(BaseModel):
    """真实回录时一腿的实际赔率（按 fixture 定位；一注内 fixture 不重复）。"""

    fixture_id: int
    odds: float = Field(gt=0, allow_inf_nan=False)


class ActualTerms(BaseModel):
    """一注的实际执行条款；结算按实际条款，原建议快照保留可追溯。"""

    stake: float = Field(gt=0, allow_inf_nan=False)
    leg_odds: list[ActualLegOdds] = Field(default_factory=list)


def resolve_paper_price(
    conn: sqlite3.Connection, legs: list[LegInput]
) -> tuple[list[LegInput], str, list[LegQuoteView]]:
    """
    纸面建注价绑定（票 04）：按共享证据面重取当前有效 had 报价。

    返回 ``(腿(绑定后), price_source, 价变腿)``：

    - 全腿 had 且三向可绑且客户端价==服务器价 → system_paper，腿带
      snapshot_id（身份落库）；
    - 任一价变 → 价变腿列表非空（调用方未显式走手工假设时须 409）；
    - 任一腿不可绑（非 had 市场 / 无报价 / 三向不齐）→ manual_research
      （可绑腿仍绑 snapshot_id——证据只增不减，类标签决定官方性）。
    """
    moment = utc_now_iso()
    bound: list[LegInput] = []
    mismatches: list[LegQuoteView] = []
    all_bound = True
    for leg in legs:
        binding = (
            quote_evidence.had_quote_binding(conn, leg.fixture_id, moment)
            if leg.market_code == "had"
            else None
        )
        row = binding.get(leg.selection_code) if binding else None
        if row is None:
            all_bound = False
            bound.append(leg)
            continue
        bound.append(leg.model_copy(update={"snapshot_id": row.snapshot_id}))
        if not math.isclose(leg.locked_odds, row.odds, rel_tol=_ODDS_EPSILON):
            mismatches.append(
                LegQuoteView(
                    fixture_id=leg.fixture_id,
                    selection_code=leg.selection_code,
                    server_odds=row.odds,
                    snapshot_id=row.snapshot_id,
                    observed_at=row.observed_at,
                )
            )
    source = (
        PRICE_SOURCE_SYSTEM_PAPER
        if all_bound and not mismatches
        else PRICE_SOURCE_MANUAL_RESEARCH
    )
    return bound, source, mismatches


def rebind_paper_price(
    conn: sqlite3.Connection, legs: list[LegInput], *, allow_update: bool
) -> list[LegInput]:
    """
    纸面锁定（票级回录）时的价重验（票 04 范围①"确认时"）。

    锁定即正式赛前决策：与建注间隔内报价可能已动。价变且未确认更新 →
    raise PriceMismatchError（409 返回新价）；allow_update=True = 客户端
    已确认按服务器价落库——locked_odds 刷新为当前有效价并重绑快照身份。
    不可绑腿维持原样（锁定时点资格校验另由 validate_had_selections 把关）。
    """
    moment = utc_now_iso()
    mismatches: list[LegQuoteView] = []
    rebound: list[LegInput] = []
    for leg in legs:
        binding = (
            quote_evidence.had_quote_binding(conn, leg.fixture_id, moment)
            if leg.market_code == "had"
            else None
        )
        row = binding.get(leg.selection_code) if binding else None
        if row is None:
            rebound.append(leg)
            continue
        if math.isclose(leg.locked_odds, row.odds, rel_tol=_ODDS_EPSILON):
            rebound.append(leg.model_copy(update={"snapshot_id": row.snapshot_id}))
        else:
            mismatches.append(
                LegQuoteView(
                    fixture_id=leg.fixture_id,
                    selection_code=leg.selection_code,
                    server_odds=row.odds,
                    snapshot_id=row.snapshot_id,
                    observed_at=row.observed_at,
                )
            )
            rebound.append(
                leg.model_copy(
                    update={"locked_odds": row.odds, "snapshot_id": row.snapshot_id}
                )
            )
    if mismatches and not allow_update:
        raise PriceMismatchError([m.model_dump() for m in mismatches])
    return rebound


def create_bet_with_legs(
    conn: sqlite3.Connection,
    draft: BetDraft,
    *,
    duck_con: quote_evidence.DuckCon | None = None,
    price_source: str | None = None,
) -> int:
    """
    建注（含校验：竞彩禁止同场串关）；返回 bet id。

    建注即落注级 EV 快照（票 41）：锁定时刻的欧共识/模型双口径概率与
    EV——口径不可得存 None，事后不倒填。price_source 由 API 层经
    resolve_paper_price 裁定（live=live_backfill；缺省不落标签=旧行为，
    仅供既有内部调用）。
    """
    if price_source is not None and price_source not in PRICE_SOURCES:
        raise ValueError(f"未知 price_source: {price_source}")
    seen: set[int] = set()
    for leg in draft.legs:
        if leg.fixture_id in seen:
            raise SameFixtureParlayError(f"fixture {leg.fixture_id} 重复出现在串关中")
        seen.add(leg.fixture_id)
    with atomic(conn):
        snapshot = build_ev_snapshot(conn, draft.legs, duck_con=duck_con)
        bet = bt_store.create_bet(
            conn,
            draft.mode,
            MarketKind.FIXED,
            draft.stake,
            strategy_version=draft.strategy_version,
            ev_snapshot=snapshot,
            price_source=price_source,
        )
        for leg in draft.legs:
            bt_store.add_leg(conn, bet, leg)
    return bet


def validate_had_selections(
    conn: sqlite3.Connection,
    legs: list[LegInput],
    *,
    as_of: str | None = None,
    duck_con: quote_evidence.DuckCon | None = None,
) -> None:
    """
    Had 选择资格（票 36 选择入口/锁定再校验，消费票 35 判定）：

    - 任一 had 腿判定 rejected（停售/已开赛/三向不全/源过期/场次不存在）→ 拒绝；
    - 单关（仅一腿且为 had）须 single_eligible 为 True，未知按拒绝；
    - 串关在同一 as_of 判定全部腿，即共同可购买时点；非 had 市场暂不在
      证据契约内，不判定（保持既有行为）。
    """
    moment = as_of or utc_now_iso()
    had_legs = [leg for leg in legs if leg.market_code == "had"]
    problems: list[str] = []
    for leg in had_legs:
        verdict = adjudicate_had_quote(
            conn,
            leg.fixture_id,
            moment,
            duck_con=duck_con,
            sid=mapping.srct_sid_for_fixture(conn, leg.fixture_id),
        )
        if verdict.status == "rejected":
            problems.append(
                f"fixture {leg.fixture_id} 不可投: {', '.join(verdict.reasons)}"
            )
        elif (
            len(had_legs) == 1
            and len(legs) == 1
            and verdict.single_eligible is not True
        ):
            problems.append(
                f"fixture {leg.fixture_id} 非单固(单关须 single_eligible=true)"
            )
    if problems:
        raise BetEligibilityError("; ".join(problems))


def _apply_actual_terms(
    conn: sqlite3.Connection, bet: sqlite3.Row, actual: ActualTerms
) -> float:
    """记录一注的实际执行条款并返回有效注金（建议快照不动）。"""
    leg_fixtures = {int(leg["fixture_id"]) for leg in json.loads(bet["legs"])}
    unknown_legs = {o.fixture_id for o in actual.leg_odds} - leg_fixtures
    if unknown_legs:
        raise ValueError(
            f"bet {bet['id']} actuals 引用了不存在的腿 fixture: {sorted(unknown_legs)}"
        )
    bt_store.set_actual_terms(
        conn,
        int(bet["id"]),
        actual.stake,
        {o.fixture_id: o.odds for o in actual.leg_odds},
    )
    return actual.stake


def _load_purchasable_bets(
    conn: sqlite3.Connection, bet_ids: list[int]
) -> list[sqlite3.Row]:
    """取回可回录的建议注（存在、未购、未结、无资金/结算记录、固定奖金）。"""
    bets: list[sqlite3.Row] = []
    for bet_id in bet_ids:
        bet = bt_store.get_bet(conn, bet_id)
        if bet is None:
            raise LookupError(f"bet {bet_id} 不存在")
        if bet["purchased"] or bet["slip_id"] is not None or bet["status"] != "open":
            raise ValueError(f"bet {bet_id} 已购、已绑定或已结, 不能再次回录")
        if bt_store.has_settlement_or_bankroll_event(conn, bet_id):
            raise ValueError(f"bet {bet_id} 已有结算或资金记录, 请人工核查")
        if bet["market_kind"] != "fixed":
            raise ValueError("奖池奖金尚未支持, 不能回录")
        bets.append(bet)
    return bets


def _rebind_paper_legs(
    conn: sqlite3.Connection, bets: list[sqlite3.Row], allow_price_update: bool
) -> None:
    """
    锁定时价重验落库（票 04；调用方已在同票事务内）。

    仅 system_paper 注重验：价变未确认 raise PriceMismatchError（整票
    回滚）；确认则 locked_odds/snapshot_id 刷新为决策时点服务器价；
    价格一致也刷新快照身份（锁定时点才是决策时点）。手工研究假设/
    历史未分类自定价，不重验不改写。
    """
    for bet in bets:
        if bet["price_source"] != PRICE_SOURCE_SYSTEM_PAPER:
            continue
        legs = [
            LegInput(
                fixture_id=int(leg["fixture_id"]),
                market_code=str(leg["market_code"]),
                selection_code=str(leg["selection_code"]),
                locked_odds=float(leg["locked_odds"]),
                goal_line=leg["goal_line"],
            )
            for leg in json.loads(bet["legs"])
        ]
        rebound = rebind_paper_price(conn, legs, allow_update=allow_price_update)
        bet_id = int(bet["id"])
        for leg, new in zip(legs, rebound, strict=True):
            if leg != new:
                bt_store.update_leg_price_binding(
                    conn, bet_id, new.fixture_id, new.locked_odds, new.snapshot_id
                )


def record_purchase(
    conn: sqlite3.Connection,
    bet_ids: list[int],
    placed_at: str | None = None,
    actuals: dict[int, ActualTerms] | None = None,
    *,
    allow_price_update: bool = False,
) -> int:
    """
    票级回录：勾选实际购买子集，生成一张票；live 注按实际条款扣减 bankroll。

    paper 锁定即正式赛前决策（票 04）：同事务内重验服务器当前有效报价——
    价变且未确认（allow_price_update=False）raise PriceMismatchError 整票
    回滚；确认更新则 locked_odds/snapshot_id 刷新为决策时点服务器价。
    """
    if not bet_ids or len(set(bet_ids)) != len(bet_ids):
        raise ValueError("bet_ids 必须非空且不能重复")
    terms = actuals or {}
    with atomic(conn):
        at = placed_at or utc_now_iso()
        bets = _load_purchasable_bets(conn, bet_ids)
        modes = {str(bet["mode"]) for bet in bets}
        if len(modes) != 1:
            raise ValueError("一张票内 mode 必须一致")
        mode = BetMode(modes.pop())
        unknown = set(terms) - set(bet_ids)
        if unknown:
            raise ValueError(f"actuals 引用了不在本票内的 bet: {sorted(unknown)}")
        if mode is BetMode.PAPER:
            _rebind_paper_legs(conn, bets, allow_price_update)
        slip = bt_store.create_slip(conn, mode, placed_at=at)
        bt_store.attach_bets_to_slip(conn, slip, bet_ids, at)
        for bet in bets:
            bet_id = int(bet["id"])
            actual = terms.get(bet_id)
            effective_stake = (
                _apply_actual_terms(conn, bet, actual)
                if actual is not None
                else float(bet["stake"])
            )
            if mode is BetMode.LIVE:
                bt_store.record_bankroll_event(
                    conn,
                    "bet_stake",
                    -effective_stake,
                    bet_id=bet_id,
                    slip_id=slip,
                    note=f"slip {slip}",
                )
    return slip
