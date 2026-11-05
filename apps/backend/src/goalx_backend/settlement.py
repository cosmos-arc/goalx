"""
Settlement 引擎：按官方规则兑付（纯函数，无存储依赖）。

规则来源（研究 01 / 票 06 边界裁决）：
- 赛果口径：全场 90 分钟（含补时），不含加时/点球。
- 无效场次（取消/延期/腰斩未重赛）：单关退款；串关该腿赔率按 1 计算、
  其余腿正常结算；全部无效才整单退款。
- 竞彩串关奖金 = 各腿赔率连乘 × 注金；禁止同场串关（写入侧校验）。
- 传统足彩（任9/14场）：无效场次以官方摇奖公告为准——M1 以「任意选择算
  命中」近似并在 detail 标注，等官方公告数值接入后替换。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from goalx_backend.markets import CRS_EXACT_SCORES, GOALS4_CODES

CRS_EXACT_SET = frozenset(CRS_EXACT_SCORES)
_POOL_WDL = {"h": "3", "d": "1", "a": "0"}


@dataclass(frozen=True, kw_only=True)
class ResultFacts:
    """一场比赛结算所需的事实（DrawResult 的结算投影）。"""

    home_goals: int
    away_goals: int
    half_home_goals: int | None = None
    half_away_goals: int | None = None
    void: bool = False
    void_reason: str | None = None


@dataclass(frozen=True, kw_only=True)
class LegSpec:
    """一腿：市场、选项与下注时锁定信息。"""

    fixture_id: int
    market_code: str
    selection_code: str
    locked_odds: float
    goal_line: float | None = None


@dataclass(kw_only=True)
class LegOutcome:
    """单腿判定结果。"""

    fixture_id: int
    selection_code: str
    hit: bool | None  # None = 开赛果未出/无法判定
    void: bool = False
    note: str | None = None


@dataclass(kw_only=True)
class SettlementOutcome:
    """一注/一张票的结算结果。"""

    status: str  # open | won | lost | void | partial
    stake: float
    payout: float
    profit: float
    legs: list[LegOutcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def settled(self) -> bool:
        """Whether the outcome is final (not awaiting results)."""
        return self.status != "open"


def _full_time_code(home: float, away: float) -> str:
    """胜平负字母编码：h/d/a（接受小数以支持让球线比较）。"""
    if home > away:
        return "h"
    if home < away:
        return "a"
    return "d"


def _crs_code(home: int, away: int) -> str:
    """比分选项编码（超出精确网格落入「其他」档）。"""
    if (home, away) in CRS_EXACT_SET:
        return f"{home}:{away}"
    return f"{_full_time_code(home, away)}_other"


def _ttg_code(home: int, away: int) -> str:
    """总进球选项编码（7+ 档封顶）。"""
    return str(min(home + away, 7))


def _hafu_code(result: ResultFacts) -> str | None:
    """半全场选项编码；缺半场比分时无法判定。"""
    if result.half_home_goals is None or result.half_away_goals is None:
        return None
    half = _full_time_code(result.half_home_goals, result.half_away_goals)
    full = _full_time_code(result.home_goals, result.away_goals)
    return f"{half}{full}"


def _actual_selection(
    leg: LegSpec, result: ResultFacts
) -> tuple[str | None, str | None]:
    """计算该腿市场的实际选项编码；返回 (code, 不可判定原因)，二者互斥。"""
    code: str | None = None
    note: str | None = None
    full = _full_time_code(result.home_goals, result.away_goals)
    market = leg.market_code
    if market == "had":
        code = full
    elif market == "hhad":
        if leg.goal_line is None:
            note = "missing_goal_line"
        else:
            code = _full_time_code(result.home_goals + leg.goal_line, result.away_goals)
    elif market == "crs":
        code = _crs_code(result.home_goals, result.away_goals)
    elif market == "ttg":
        code = _ttg_code(result.home_goals, result.away_goals)
    elif market == "hafu":
        code = _hafu_code(result)
        note = None if code else "missing_half_score"
    elif market in ("ttt14", "pick9"):
        code = _POOL_WDL[full]
    elif market == "goals4":
        code, note = _goals4_band_code(leg, result)
    elif market == "htft6":
        code = _hafu_code(result)
        note = None if code else "missing_half_score"
    else:
        # 新市场接入时同步 markets.LEG_MARKETS/LEG_SELECTIONS（API 载荷闭集）
        note = f"unsupported_market:{market}"
    return code, note


def _goals4_band_code(
    leg: LegSpec, result: ResultFacts
) -> tuple[str | None, str | None]:
    """
    goals4 池票档位码判定（票 10，correctness W4）。

    池票存档位码（每场主/客各一行）——按所选侧取该侧进球档（3+ 聚尾）；
    与 crs 精确比分码不同空间（crs 口径会对档位腿恒 miss）。闭集外码
    不可判定不误判（构票侧已拒）。
    """
    sel = str(leg.selection_code)
    if sel not in GOALS4_CODES:
        return None, f"unsupported_selection:{sel}"
    side = sel[0]
    goals = result.home_goals if side == "h" else result.away_goals
    return f"{side}{min(goals, 3)}", None


def judge_leg(leg: LegSpec, result: ResultFacts) -> LegOutcome:
    """判定一腿：返回命中/无效/待开（不抛异常，状态由上层聚合）。"""
    if result.void:
        return LegOutcome(
            fixture_id=leg.fixture_id,
            selection_code=leg.selection_code,
            hit=None,
            void=True,
            note="invalid_fixture",
        )
    actual, note = _actual_selection(leg, result)
    if actual is None:
        return LegOutcome(
            fixture_id=leg.fixture_id,
            selection_code=leg.selection_code,
            hit=None,
            note=note,
        )
    return LegOutcome(
        fixture_id=leg.fixture_id,
        selection_code=leg.selection_code,
        hit=actual == leg.selection_code,
    )


def judge_leg_with_results(
    leg: LegSpec, results: Mapping[int, ResultFacts]
) -> LegOutcome:
    """判定一腿；赛果未导入时返回待开而非误判。"""
    result = results.get(leg.fixture_id)
    if result is None:
        return LegOutcome(
            fixture_id=leg.fixture_id,
            selection_code=leg.selection_code,
            hit=None,
            note="awaiting_result",
        )
    return judge_leg(leg, result)


def settle_fixed_bet(
    stake: float, legs: list[LegSpec], results: Mapping[int, ResultFacts]
) -> SettlementOutcome:
    """
    结算一注竞彩（单关或串关）。

    - 任一腿没有对应赛果 → 整注 open（等待开奖）。
    - 单关或串关全部场次无效 → 退款；剩一腿仍按其结果及赔率计奖。
    - 串关无效腿赔率按 1；其余腿全中按有效腿连乘兑付，任一未中则输。
    """
    if not legs:
        return SettlementOutcome(
            status="void",
            stake=stake,
            payout=round(stake, 2),  # 与流水 delta 同口径（审计批B #5）
            profit=0.0,
            notes=["empty_bet_refund"],
        )
    judged = [(leg, judge_leg_with_results(leg, results)) for leg in legs]
    leg_outcomes = [outcome for _, outcome in judged]
    if any(outcome.hit is None and not outcome.void for outcome in leg_outcomes):
        return SettlementOutcome(
            status="open",
            stake=stake,
            payout=0.0,
            profit=0.0,
            legs=leg_outcomes,
            notes=["awaiting_results"],
        )
    void_legs = [leg for leg, outcome in judged if outcome.void]
    live_legs = [(leg, outcome) for leg, outcome in judged if not outcome.void]

    single_void = len(legs) == 1 and bool(void_legs)
    if not live_legs:
        note = "single_void_refund" if single_void else "all_void_refund"
        return SettlementOutcome(
            status="void",
            stake=stake,
            payout=round(stake, 2),  # 退款与流水 round 同口径（审计批B #5）
            profit=0.0,
            legs=leg_outcomes,
            notes=[note],
        )

    if len(live_legs) == 1:
        leg, outcome = live_legs[0]
        if outcome.hit is None:
            raise ValueError("open 检查后仍出现未判定腿")
        payout = round(stake * leg.locked_odds, 2) if outcome.hit else 0.0
        return SettlementOutcome(
            status="won" if outcome.hit else "lost",
            stake=stake,
            payout=payout,
            profit=round(payout - stake, 2),
            legs=leg_outcomes,
        )

    if any(outcome.hit is False for _, outcome in live_legs):
        return SettlementOutcome(
            status="lost",
            stake=stake,
            payout=0.0,
            profit=round(-stake, 2),  # 与流水 delta 同口径（审计批B #5）
            legs=leg_outcomes,
            notes=["leg_missed"],
        )
    product = 1.0
    for leg, _ in live_legs:
        product *= leg.locked_odds
    payout = round(stake * product, 2)
    notes = [f"void_legs_odds_1:{len(void_legs)}"] if void_legs else []
    return SettlementOutcome(
        status="won",
        stake=stake,
        payout=payout,
        profit=round(payout - stake, 2),
        legs=leg_outcomes,
        notes=notes,
    )


def detail_payload(outcome: SettlementOutcome) -> dict[str, Any]:
    """Serialize a settlement outcome for the settlements.detail column."""
    return {
        "status": outcome.status,
        "stake": outcome.stake,
        "payout": outcome.payout,
        "profit": outcome.profit,
        "notes": outcome.notes,
        "legs": [
            {
                "fixture_id": leg.fixture_id,
                "selection_code": leg.selection_code,
                "hit": leg.hit,
                "void": leg.void,
                "note": leg.note,
            }
            for leg in outcome.legs
        ],
    }
