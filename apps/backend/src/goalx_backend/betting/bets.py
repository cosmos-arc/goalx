"""Bet 生命周期用例：建注（同场串关校验）、资格判定与购买回录（票级，live 扣款）。"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3

from pydantic import BaseModel, Field

from goalx_backend.betting import store as bt_store
from goalx_backend.betting.snapshot import build_ev_snapshot
from goalx_backend.data import mapping, quote_evidence
from goalx_backend.data.quote_evidence import adjudicate_had_quote
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.models import (
    PRICE_SOURCE_MANUAL_RESEARCH,
    PRICE_SOURCE_SYSTEM_PAPER,
    PRICE_SOURCES,
    BetMode,
    LegInput,
    MarketKind,
)

# 价格来源三分类词表在 models.py（store 与本用例层共用，ADR-0008 归属）；
# 语义注释见彼处。OFFICIAL_PAPER 语义：
# - system_paper 系统纸面决策：服务器重取报价验证，腿级证据身份落
#   bet_legs.snapshot_id——正式纸面成绩面的完整成员；
# - manual_research 手工研究假设：明确排除出正式可执行性验证/成绩面；
# - 历史 NULL（人裁决 2026-11-02 不回溯打标）：维持既有面不追溯排除
#   （"仅新记录生效"），只排除显式 manual_research。
# 客户端价与服务器价相等容差（同一十进制字面量解析，只防浮点尾差）
_ODDS_EPSILON = 1e-9


def is_official_paper(row: sqlite3.Row) -> bool:
    """
    该注是否进正式纸面成绩面（消费侧判据：paper 且非 manual_research）。

    历史 NULL 注不回溯排除（裁决③"仅新记录生效"）；system_paper 完整
    成员、NULL 维持既有面。manual_research 由 review_views 与
    settled_purchased_bets 两处消费点强制排除。
    """
    return str(row["mode"]) == "paper" and (
        row["price_source"] != PRICE_SOURCE_MANUAL_RESEARCH
    )


class SameFixtureParlayError(ValueError):
    """竞彩禁止同场串关（研究 01）。"""


class BetEligibilityError(ValueError):
    """had 选择/锁定不满足服务端共享证据资格（票 36；界面禁用不替代本判定）。"""


class IntentConflictError(Exception):
    """
    同 client_request_id 已绑定不同提交意图（票 09）。

    域层重放判定（预检与 UNIQUE 竞态两路）发现既有注指纹与本次提交指纹
    不符时抛出；HTTP 层映射 409 并以 detail() 附原注摘要。
    """

    def __init__(self, bet_id: int, mode: str, stake: float) -> None:
        self.bet_id = bet_id
        self.mode = mode
        self.stake = stake
        super().__init__(
            f"client_request_id 已绑定另一提交意图：原注 #{bet_id}（{mode} ¥{stake}）"
            + "已建立，本次新意图未建注——换新键另建或按原意图重提"
        )

    def detail(self) -> dict[str, object]:
        """409 载荷（消息 + 原注摘要，恢复原结果用）。"""
        return {
            "message": str(self),
            "existing_bet": {
                "id": self.bet_id,
                "mode": self.mode,
                "stake": self.stake,
            },
        }


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
    # 客户端建注幂等键（票 06）：同键重复提交不落双行（撞唯一索引回带）
    client_request_id: str | None = Field(default=None, max_length=200)
    # 提交意图指纹（票 09）：键带则随行落库，重放比对防同键吞改
    intent_fingerprint: str | None = None


def intent_fingerprint(mode: BetMode, stake: float, legs: list[LegInput]) -> str:
    """
    提交意图规范化指纹（票 09，人裁决 2026-11-04）。

    业务字段规范形（mode/stake + 逐腿 fixture/selection/market，含
    goal_line 市场线）排序后哈希——腿序无关、不含 locked_odds（价随确认
    重绑，非意图）与 note 等自由文本。建注时由提交载荷计算一次落库；
    重放时重算比对，不读回绑后的行（比较对象=不可变原始提交快照）。
    stake 归一 float（int/float 同值同哈希）；goal_line 排序键 None 在前
    （None 与 float 不可比，correctness F1）。
    """
    canon = {
        "mode": mode.value,
        "stake": float(stake),
        "legs": sorted(
            (
                {
                    "fixture_id": leg.fixture_id,
                    "market_code": leg.market_code,
                    "selection_code": leg.selection_code,
                    "goal_line": leg.goal_line,
                }
                for leg in legs
            ),
            key=lambda item: (
                item["fixture_id"],
                item["market_code"],
                item["selection_code"],
                item["goal_line"] is None,
                item["goal_line"] or 0.0,
            ),
        ),
    }
    payload = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ActualLegOdds(BaseModel):
    """真实回录时一腿的实际赔率（按 fixture 定位；一注内 fixture 不重复）。"""

    fixture_id: int
    odds: float = Field(gt=0, allow_inf_nan=False)


class ActualTerms(BaseModel):
    """一注的实际执行条款；结算按实际条款，原建议快照保留可追溯。"""

    stake: float = Field(gt=0, allow_inf_nan=False)
    leg_odds: list[ActualLegOdds] = Field(default_factory=list)


class PriceMismatchError(ValueError):
    """
    纸面价与服务器当前有效报价不一致（票 04 裁决①：返回新价要求确认）。

    current_quotes=价变腿的服务器现价——调用方（HTTP 层）以 409 呈现，
    客户端确认后按 server_odds 重提（或显式 allow_manual_price 走研究
    假设类）。
    """

    def __init__(self, current_quotes: list[LegQuoteView]) -> None:
        self.current_quotes = current_quotes

        parts = ", ".join(
            f"{q.fixture_id} {q.selection_code}: {q.server_odds}"
            for q in current_quotes
        )  # 快照身份在结构化 current_quotes 里，消息只给人读
        super().__init__(f"报价已变: {parts}")


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
    conn: sqlite3.Connection,
    legs: list[LegInput],
    *,
    allow_update: bool,
    moment: str | None = None,
) -> list[LegInput]:
    """
    纸面锁定（票级回录）时的价重验（票 04 范围①"确认时"）。

    锁定即正式赛前决策：与建注间隔内报价可能已动。价变且未确认更新 →
    raise PriceMismatchError（409 返回新价）；allow_update=True = 客户端
    已确认按服务器价落库——locked_odds 刷新为当前有效价并重绑快照身份。
    不可绑腿维持原样（锁定时点资格校验另由 validate_had_selections 把关）。
    moment=决策时刻（票 09 五价记录传入同刻，保证 decision 阶段价与重绑
    同一 PIT 面；缺省取当下）。
    """
    moment = moment or utc_now_iso()
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
        raise PriceMismatchError(mismatches)
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

    幂等（票 06/09）：draft.client_request_id 带键且已有同键注 → 意图
    相符直接回带既有注 id（不落双行）；指纹不符（改金额/模式/选注）
    抛 IntentConflictError——预检与撞唯一索引的并发竞态两路同比对，
    竞态窗内不静默回旧。
    """
    if price_source is not None and price_source not in PRICE_SOURCES:
        raise ValueError(f"未知 price_source: {price_source}")
    if draft.client_request_id is not None:
        existing = _replayable_bet_id(conn, draft)
        if existing is not None:
            return existing
    seen: set[int] = set()
    for leg in draft.legs:
        if leg.fixture_id in seen:
            raise SameFixtureParlayError(f"fixture {leg.fixture_id} 重复出现在串关中")
        seen.add(leg.fixture_id)
    with atomic(conn):
        discovery_at = utc_now_iso()
        snapshot = build_ev_snapshot(
            conn, draft.legs, as_of=discovery_at, duck_con=duck_con
        )
        try:
            bet = bt_store.create_bet(
                conn,
                draft.mode,
                MarketKind.FIXED,
                draft.stake,
                strategy_version=draft.strategy_version,
                ev_snapshot=snapshot,
                price_source=price_source,
                client_request_id=draft.client_request_id,
                intent_fingerprint=draft.intent_fingerprint,
            )
        except sqlite3.IntegrityError as exc:
            existing = _existing_for_request_race(conn, exc, draft)
            if existing is None:
                raise
            return existing
        for leg in draft.legs:
            bt_store.add_leg(conn, bet, leg)
        if draft.mode is BetMode.PAPER:
            _record_discovery_stages(conn, bet, draft.legs, recorded_at=discovery_at)
    return bet


def _replayable_bet_id(conn: sqlite3.Connection, draft: BetDraft) -> int | None:
    """
    同键重放判定（票 06/09，域层）。

    既有注同意图（或无指纹可比对）→ 回带 id；异意图 →
    IntentConflictError（不静默回旧）；无既有注 → None。
    """
    existing_id = bt_store.bet_id_for_request(conn, draft.client_request_id or "")
    if existing_id is None:
        return None
    row = bt_store.get_bet(conn, existing_id)
    if row is None:
        return None
    stored = row["intent_fingerprint"]
    if (
        stored is not None
        and draft.intent_fingerprint is not None
        and stored != draft.intent_fingerprint
    ):
        raise IntentConflictError(int(row["id"]), str(row["mode"]), float(row["stake"]))
    return int(row["id"])


def _existing_for_request_race(
    conn: sqlite3.Connection, exc: sqlite3.IntegrityError, draft: BetDraft
) -> int | None:
    """
    并发竞态判别（票 06/09）：同 client_request_id 唯一冲突 → 回带既有注 id。

    判别 = UNIQUE 错误码 + 列名；非同键竞态（或键未带、回查落空）返回
    None 由调用方重抛——任何其他唯一冲突照常冒出。同键竞态与预检同一
    指纹比对（票 09）：并发窗内异意图同样抛 IntentConflictError，不静默回旧。
    """
    request_id = draft.client_request_id
    same_key_race = (
        request_id is not None
        and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_UNIQUE
        and "client_request_id" in str(exc)
    )
    if not same_key_race or request_id is None:
        return None
    return _replayable_bet_id(conn, draft)


def _record_discovery_stages(
    conn: sqlite3.Connection,
    bet_id: int,
    legs: list[LegInput],
    *,
    recorded_at: str,
) -> None:
    """
    五价·发现价（票 09 预注册 §3）落行。

    候选入篮提交时刻的承载价与建注绑定身份；manual/system 同记（记录
    无偏，读数侧分母过滤）。live 不记。
    """
    for leg in legs:
        bt_store.record_price_stage(
            conn,
            bet_id,
            leg.fixture_id,
            leg.selection_code,
            "discovery",
            odds=leg.locked_odds,
            snapshot_id=leg.snapshot_id,
            recorded_at=recorded_at,
        )


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
    if len({o.fixture_id for o in actual.leg_odds}) != len(actual.leg_odds):
        # correctness F1：重复 fixture 两处落库口径分裂（actual_odds 后写
        # 覆盖、时间线首写吸收）——同事件两份矛盾的账，fail-loud 拒收
        raise ValueError(f"bet {bet['id']} actuals 存在重复腿 fixture，拒绝回录")
    bt_store.set_actual_terms(
        conn,
        int(bet["id"]),
        actual.stake,
        {o.fixture_id: o.odds for o in actual.leg_odds},
    )
    for o in actual.leg_odds:
        # 五价·票面价（票 09 预注册 §3）：实际条款是人工录入，无快照身份
        # （snapshot_id 恒 NULL——诚实记缺，不虚构绑定）；recorded_at 恒
        # 服务器时钟——placed_at 是客户端可回填字段，不作阶段时刻
        # （correctness F3）
        bt_store.record_price_stage(
            conn,
            int(bet["id"]),
            o.fixture_id,
            _selection_for_fixture(bet, o.fixture_id),
            "ticket",
            odds=o.odds,
            snapshot_id=None,
            recorded_at=utc_now_iso(),
        )
    return actual.stake


def _selection_for_fixture(bet: sqlite3.Row, fixture_id: int) -> str:
    """一注内某 fixture 腿的 selection（actuals 校验已保证存在且唯一）。"""
    for leg in json.loads(bet["legs"]):
        if int(leg["fixture_id"]) == fixture_id:
            return str(leg["selection_code"])
    raise ValueError(f"bet {bet['id']} 无 fixture {fixture_id} 腿")


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


def _leg_binding(
    conn: sqlite3.Connection, leg: LegInput, moment: str
) -> quote_evidence.QuoteBinding | None:
    """该腿当前有效报价绑定（非 had / 绑不上返回 None）。"""
    if leg.market_code != "had":
        return None
    binding = quote_evidence.had_quote_binding(conn, leg.fixture_id, moment)
    return binding.get(leg.selection_code) if binding is not None else None


def _rebind_paper_legs(
    conn: sqlite3.Connection, bets: list[sqlite3.Row], allow_price_update: bool
) -> None:
    """
    锁定时价重验落库（票 04；调用方已在同票事务内）。

    仅 system_paper 注重验：价变未确认 raise PriceMismatchError（整票
    回滚）；确认则 locked_odds/snapshot_id 刷新为决策时点服务器价；
    价格一致也刷新快照身份（锁定时点才是决策时点）。手工研究假设/
    历史未分类自定价，不重验不改写。

    票 09：重验同刻落 decision 阶段行（服务器重绑有效价；绑不上记
    NULL 价——事件发生价缺）。409 未确认路径整票回滚不留时间线行
    （未确认=未决策，预注册 §8）。
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
        decision_at = utc_now_iso()
        rebound = rebind_paper_price(
            conn, legs, allow_update=allow_price_update, moment=decision_at
        )
        bet_id = int(bet["id"])
        for leg, new in zip(legs, rebound, strict=True):
            binding = _leg_binding(conn, leg, decision_at)
            bt_store.record_price_stage(
                conn,
                bet_id,
                leg.fixture_id,
                leg.selection_code,
                "decision",
                odds=binding.odds if binding is not None else None,
                snapshot_id=binding.snapshot_id if binding is not None else None,
                recorded_at=decision_at,
            )
            if leg != new:
                bt_store.update_leg_price_binding(
                    conn, bet_id, new.fixture_id, new.locked_odds, new.snapshot_id
                )


def _record_confirm_stages(
    conn: sqlite3.Connection, bets: list[sqlite3.Row], confirm_at: str
) -> None:
    """
    五价·确认价（票 09 预注册 §3）：锁票落库后按 bet_legs 终态记录。

    system_paper 与重验同刻同取价，恒等是设计事实（可审计证明确认价=
    服务器决策价）；manual/历史 NULL 承载客户端价（其 decision 行缺）。
    须在 _rebind_paper_legs 之后调用（bet_legs 已刷新为终态）。
    """
    for bet in bets:
        bet_id = int(bet["id"])
        for leg in bt_store.legs_for_bet(conn, bet_id):
            bt_store.record_price_stage(
                conn,
                bet_id,
                int(leg["fixture_id"]),
                str(leg["selection_code"]),
                "confirm",
                odds=float(leg["locked_odds"]),
                snapshot_id=leg["snapshot_id"],
                recorded_at=confirm_at,
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
            # 票 01：paper 价格链单一真相=服务器绑定价，实际条款只属于 live
            # 回录；手工情景价走 manual_research 分类建注，不经 actuals 旁路。
            # 在产生时间线/绑定票据之前拒绝，事务回滚保证零部分写入。
            if terms:
                raise ValueError(
                    "paper 注不接受实际条款(actuals); 手工情景走 manual_research 分类"
                )
            _rebind_paper_legs(conn, bets, allow_price_update)
            _record_confirm_stages(conn, bets, confirm_at=utc_now_iso())
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
