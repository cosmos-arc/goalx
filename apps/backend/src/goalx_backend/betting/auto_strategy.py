"""
auto-v1 策略落注引擎（auto-paper 票 03）：prereg B 节冻结口径的执行体。

参数全部来自 prereg（修订①-④），代码不内嵌裸常数——改参数=升版本。
官方性通道（2026-11-08 冲突裁定的推荐案先行，PR 评审可推翻）：auto
通道内逐腿对 sporttery 快照 as-of 运行时刻同比对——价一致→
system_paper+腿级快照身份落库；锚后价漂→该注弃置留痕（price_drift，
不按旧价下注）。web/API 路径的价绑定语义（仅 had）零变化。

shortcut: ttg 相邻档复式（修订④）需组合选择码与结算判别支持，现行腿
模型（每注腿必异场）不可表达——本版降级为单档，升级=腿模型支持组合
码。池面 v2 反火锅结构（修订②③）同理降级为既有全稳/贪心底注。

玩法资格链（edge-audit-20261109 票 14）：预筛与下游建注闸共用
bets.market_eligibility_problem 单源判定，按玩法（had/hhad/ttg/crs/
hafu）分别验在售状态与单关资格（单关候选须 poolList 池级单固，未知
即拒）；原「预筛硬编码 had 在售 + 非 had 腿跳过验证」的两处缺口退役
（外部审计缺陷 5）。资格未齐的历史记录不追溯改判——仅前向生效。
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Protocol

from goalx_backend.betting import pool_strategy, versioning
from goalx_backend.betting import store as bt_store
from goalx_backend.betting.bets import (
    BetDraft,
    BetEligibilityError,
    IntentConflictError,
    PriceMismatchError,
    SameFixtureParlayError,
    create_bet_with_legs,
    intent_fingerprint,
    market_eligibility_problem,
    validate_selections,
)
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.data.pool import PoolMatchView
from goalx_backend.data.quote_evidence import QuoteReads
from goalx_backend.db import atomic
from goalx_backend.modelling import dc_model
from goalx_backend.modelling.calibration import CALIBRATION_KINDS
from goalx_backend.modelling.forecast import (
    forecast_matrix_from_payload,
    latest_forecast_asof,
)
from goalx_backend.modelling.score_matrix import ScoreMatrix
from goalx_backend.models import PRICE_SOURCE_SYSTEM_PAPER, BetMode, LegInput

AUTO_V1 = "auto-v1"
BJ = timezone(timedelta(hours=8))
_SPORTTERY = "sporttery"
_ODDS_EPSILON = 1e-9


@dataclass(frozen=True)
class AutoV1Params:
    """
    prereg B 节冻结参数（修订①-④，2026-11-07/08）。

    可购判定=锚时在售且开球晚于锚+30min 缓冲+10min 开球前置（JC 逐场
    停售时刻不可查的近似：停售≈开球，凌晨场有高估风险，台账可见）。
    在售/单关按玩法行分别判（票 14 起，market_eligibility_problem 单
    源）：单关候选须该玩法池级单固在档，未知即拒。
    """

    initial_bankroll: float = 1000.0
    kelly_fraction: float = 0.25
    stake_cap: float = 2.0
    parlay_stake: float = 2.0
    top_singles: int = 3
    parlays: int = 5
    parlay_per_match: int = 2
    single_markets: tuple[str, ...] = ("crs", "ttg")  # 修订④
    parlay_markets: tuple[str, ...] = ("had", "hhad")  # 修订④
    sale_buffer_minutes: int = 30
    kickoff_guard_minutes: int = 10
    stake_per_combination: float = 2.0  # 池票每组合 ¥2（帽内单注）


AUTO_V1_PARAMS = AutoV1Params()


class CandidateScope(Protocol):
    """锚候选构建所需参数面（auto-v1/auto-v2 参数共用的结构契约）。"""

    @property
    def single_markets(self) -> tuple[str, ...]:
        """单关候选市场集。"""
        ...

    @property
    def parlay_markets(self) -> tuple[str, ...]:
        """串关候选市场集。"""
        ...

    @property
    def sale_buffer_minutes(self) -> int:
        """停售缓冲（分钟）。"""
        ...

    @property
    def kickoff_guard_minutes(self) -> int:
        """开球前置（分钟）。"""
        ...

    @property
    def top_singles(self) -> int:
        """单关注数上限。"""
        ...


class PoolSlipScope(Protocol):
    """池票落注所需参数面（v2 复用时只取每组合注额）。"""

    @property
    def stake_per_combination(self) -> float:
        """每组合注额（元）。"""
        ...


@dataclass(frozen=True)
class Pick:
    """一个可下注候选（单关原料；串关由 Pick 对组成）。"""

    fixture_id: int
    match_code: str
    market_code: str
    selection_code: str
    goal_line: float | None
    prob: float
    odds: float
    kickoff_utc: str

    @property
    def ev(self) -> float:
        """EV = p×odds − 1（prereg 口径）。"""
        return self.prob * self.odds - 1.0


def quarter_kelly_stake(
    prob: float, odds: float, bankroll: float, params: AutoV1Params
) -> float:
    """quarter-Kelly 注码（prereg：基准=账户现值，单注封顶）。"""
    kelly = (prob * odds - 1.0) / (odds - 1.0)
    sized = params.kelly_fraction * kelly * bankroll
    return round(max(0.0, min(params.stake_cap, sized)), 2)


def select_singles(candidates: list[Pick], params: CandidateScope) -> list[Pick]:
    """修订①：每场至多一注（取该场最优 EV），再 EV 降序 top-N。"""
    best: dict[int, Pick] = {}
    for pick in candidates:
        held = best.get(pick.fixture_id)
        if held is None or pick.ev > held.ev:
            best[pick.fixture_id] = pick
    ranked = sorted(best.values(), key=lambda p: -p.ev)
    return [p for p in ranked if p.ev > 0][: params.top_singles]


def select_parlays(
    candidates: list[Pick],
    params: AutoV1Params,
    *,
    prior_per_match: dict[int, int] | None = None,
) -> list[tuple[Pick, Pick]]:
    """
    修订④：两腿不同场、联合 EV 降序取前 5，单场**全日**最多入 2 张串。

    prior_per_match=当日先前锚已入串计数（跨锚累计，Spec 审查 Gap 1）。
    """

    def joint_ev(pair: tuple[Pick, Pick]) -> float:
        a, b = pair
        return (1.0 + a.ev) * (1.0 + b.ev) - 1.0

    positive = [p for p in candidates if p.ev > 0]
    pairs = [
        (a, b)
        for i, a in enumerate(positive)
        for b in positive[i + 1 :]
        if a.fixture_id != b.fixture_id
    ]
    pairs.sort(key=lambda pair: -joint_ev(pair))
    chosen: list[tuple[Pick, Pick]] = []
    per_match: dict[int, int] = dict(prior_per_match or {})
    for pair in pairs:
        if len(chosen) >= params.parlays or joint_ev(pair) <= 0:
            break
        if any(per_match.get(p.fixture_id, 0) >= params.parlay_per_match for p in pair):
            continue
        chosen.append(pair)
        for p in pair:
            per_match[p.fixture_id] = per_match.get(p.fixture_id, 0) + 1
    return chosen


def anchor_candidates(
    conn: sqlite3.Connection,
    *,
    anchor_at: datetime,
    params: CandidateScope,
) -> tuple[list[Pick], list[Pick], dict[str, int]]:
    """
    锚时点候选面：返回（单关候选, 串关候选, 排除账）。

    候选价=锚时 as-of 的 sporttery 有效观测价（与基准冻结同准入语义：
    观测时刻未知不进锚）；hhad 仅整数线（prereg）。
    """
    business_date = fx_store.beijing_business_date(anchor_at)
    anchor_iso = anchor_at.astimezone(UTC).isoformat()
    roster = fx_store.jingcai_roster(conn, business_date)
    min_kickoff = anchor_at + timedelta(
        minutes=params.sale_buffer_minutes + params.kickoff_guard_minutes
    )
    singles: list[Pick] = []
    parlay_pool: list[Pick] = []
    excluded: dict[str, int] = {}
    market_excluded: set[tuple[int, str]] = set()
    # 玩法资格判定缓存（同场同玩法同角色只查一次——crs 31 档不重复查库）
    eligibility: dict[tuple[int, str, bool], str | None] = {}
    for row in roster:
        fixture_id = int(row["fixture_id"])
        reason = _roster_exclusion_reason(
            row, anchor_at=anchor_at, min_kickoff=min_kickoff
        )
        if reason is not None:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        forecast = latest_forecast_asof(conn, fixture_id, anchor_iso)
        if forecast is None:
            excluded["no_model"] = excluded.get("no_model", 0) + 1
            continue
        matrix = forecast_matrix_from_payload(json.loads(forecast["payload"]))
        odds_by_market = {
            market: fx_store.latest_odds_evidence_asof(
                conn, fixture_id, market, anchor_iso, source=_SPORTTERY
            )
            for market in (*params.single_markets, *params.parlay_markets)
        }
        for pick in _fixture_picks(
            row, fixture_id, matrix, odds_by_market, params=params
        ):
            # 玩法级资格预筛（票 14）：按玩法分别验在售与单关资格——与
            # 下游建注闸同源（market_eligibility_problem 一处定义两处
            # 引用）。排除账按（场×玩法）去重计一次。
            requires_single = pick.market_code in params.single_markets
            cache_key = (fixture_id, pick.market_code, requires_single)
            if cache_key not in eligibility:
                eligibility[cache_key] = market_eligibility_problem(
                    conn,
                    fixture_id,
                    pick.market_code,
                    anchor_iso,
                    requires_single=requires_single,
                )
            problem = eligibility[cache_key]
            if problem is not None:
                if (fixture_id, pick.market_code) not in market_excluded:
                    market_excluded.add((fixture_id, pick.market_code))
                    key = f"market_{problem}"
                    excluded[key] = excluded.get(key, 0) + 1
                continue
            if requires_single:
                singles.append(pick)
            else:
                parlay_pool.append(pick)
    return singles, parlay_pool, excluded


def _fixture_picks(
    row: sqlite3.Row,
    fixture_id: int,
    matrix: ScoreMatrix,
    odds_by_market: dict[str, dict[str, sqlite3.Row]],
    *,
    params: CandidateScope,
) -> list[Pick]:
    """一场的四玩法候选（正价可观测项）；hhad 仅整数线（prereg）。"""
    views: dict[str, dict[str, float]] = {
        "had": matrix.had(),
        "ttg": matrix.ttg(),
        "crs": matrix.crs(),
    }
    hhad_line = _hhad_integer_line(odds_by_market["hhad"])
    if hhad_line is not None:
        views["hhad"] = matrix.hhad(hhad_line)
    picks: list[Pick] = []
    for market, probs in views.items():
        if market not in params.single_markets and market not in params.parlay_markets:
            continue
        for selection, prob in probs.items():
            evidence_row = odds_by_market.get(market, {}).get(selection)
            if evidence_row is None or float(evidence_row["odds"]) <= 1.0:
                continue
            picks.append(
                Pick(
                    fixture_id=fixture_id,
                    match_code=str(row["match_code"]),
                    market_code=market,
                    selection_code=selection,
                    goal_line=hhad_line if market == "hhad" else None,
                    prob=prob,
                    odds=float(evidence_row["odds"]),
                    kickoff_utc=str(row["kickoff_utc"]),
                )
            )
    return picks


def _request_exists(conn: sqlite3.Connection, request_id: str) -> bool:
    """同锚重跑幂等（票验收）：既有同键注=已落，不重复建也不计新建。"""
    return bt_store.bet_id_for_request(conn, request_id) is not None


def _roster_exclusion_reason(
    row: sqlite3.Row,
    *,
    anchor_at: datetime,
    min_kickoff: datetime,
) -> str | None:
    """
    锚时时间面预筛（可购判定近似口径见 AutoV1Params docstring）。

    只判开球可购窗口；在售/单关资格由 anchor_candidates 的玩法级闸
    （market_eligibility_problem，票 14）逐市场判定——原 had 硬编码
    检查退役（外部审计缺陷 5：非 had 票蹭 had 在售过闸）。
    """
    kickoff = _parse_utc(str(row["kickoff_utc"]))
    if kickoff is None or kickoff <= anchor_at or kickoff < min_kickoff:
        return "not_playable"
    return None


def place_single(  # noqa: PLR0913 - 引擎内单关落注，参数即通道
    conn: sqlite3.Connection,
    pick: Pick,
    *,
    stake: float,
    request_id: str,
    run_iso: str,
    reads: QuoteReads | None,
    strategy_version: str = AUTO_V1,
    edge_source: str | None = None,
) -> str:
    """单关落注结果：placed/replayed/drift/rejected（correctness F1 零注入拒绝）。"""
    if stake <= 0:
        return "rejected"
    if _request_exists(conn, request_id):
        return "replayed"
    leg = _verified_leg(conn, pick, run_iso)
    if leg is None:
        return "drift"
    return (
        "placed"
        if _place(
            conn,
            [leg],
            stake=stake,
            request_id=request_id,
            reads=reads,
            strategy_version=strategy_version,
            edge_source=edge_source,
        )
        else "rejected"
    )


def _parlay_counts_for_day(
    conn: sqlite3.Connection, business_date: str
) -> dict[int, int]:
    """
    当日 auto-v1 串关已入腿计数（修订④ 全日口径的跨锚累计）。

    串关注=client_request_id 形如 auto-v1:{business_date}:{anchor}:p:{i}。
    """
    rows = conn.execute(
        """
        SELECT l.fixture_id, COUNT(*) AS n FROM bets b
        JOIN bet_legs l ON l.bet_id = b.id
        WHERE b.strategy_version = ?
          AND b.client_request_id LIKE ?
        GROUP BY l.fixture_id
        """,
        (AUTO_V1, f"{AUTO_V1}:{business_date}:%:p:%"),
    ).fetchall()
    return {int(r["fixture_id"]): int(r["n"]) for r in rows}


def _hhad_integer_line(evidence: dict[str, sqlite3.Row]) -> float | None:
    """
    prereg：hhad 仅整数线；各选择行 meta 解析守卫 + 全行一致才采用。

    correctness F4：goal_line 是源串（非数值/畸形 JSON 不炸锚）；盘中
    换线（−1→−2）=观测混线，绑定任一线都会错配结算——保守排除该场。
    """
    lines: set[float] = set()
    for row in evidence.values():
        meta = row["meta"]
        if not meta:
            continue
        try:
            raw = json.loads(meta).get("goal_line")
            line = float(raw) if raw is not None else None
        except (ValueError, TypeError):
            continue
        if line is not None and line.is_integer():
            lines.add(line)
    if len(lines) == 1:
        return lines.pop()
    return None


def _parse_utc(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def account_bankroll(conn: sqlite3.Connection, params: AutoV1Params) -> float:
    """账户现值=初始+已结算 paper P&L（票 04 聚合未建前的票内口径）。"""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(profit), 0) AS settled FROM bets
        WHERE mode = 'paper' AND profit IS NOT NULL
        """
    ).fetchone()
    return params.initial_bankroll + float(row["settled"])


def _verified_leg(
    conn: sqlite3.Connection, pick: Pick, run_at_iso: str
) -> LegInput | None:
    """官方性通道：锚价 vs 运行时刻有效观测价同比对，漂价弃注。"""
    evidence = fx_store.latest_odds_evidence_asof(
        conn, pick.fixture_id, pick.market_code, run_at_iso, source=_SPORTTERY
    )
    row = evidence.get(pick.selection_code)
    if row is None:
        return None
    if not math.isclose(pick.odds, float(row["odds"]), rel_tol=_ODDS_EPSILON):
        return None
    return LegInput(
        fixture_id=pick.fixture_id,
        market_code=pick.market_code,
        selection_code=pick.selection_code,
        locked_odds=pick.odds,
        snapshot_id=int(row["id"]),
        goal_line=pick.goal_line,
    )


def _place(
    conn: sqlite3.Connection,
    legs: list[LegInput],
    *,
    stake: float,
    request_id: str,
    reads: QuoteReads | None,
    strategy_version: str = AUTO_V1,
    edge_source: str | None = None,
) -> bool:
    """经 create 闸门落一注；闸门拒绝（资格/价）返回 False 由调用方留痕。"""
    draft = BetDraft(
        mode=BetMode.PAPER,
        stake=stake,
        legs=legs,
        strategy_version=strategy_version,
        edge_source=edge_source,
        client_request_id=request_id,
        intent_fingerprint=intent_fingerprint(BetMode.PAPER, stake, legs),
    )
    try:
        # 资格闸全腿无条件过（票 14）：had 腿深链 + 非 had 腿玩法级
        # （market_eligibility_problem）——原「仅 had/hhad 在场才验」跳过
        # 了纯 ttg/crs 注。与预筛同源，前向生效（历史注不追溯改判）。
        validate_selections(conn, legs, reads=reads)
        create_bet_with_legs(
            conn, draft, reads=reads, price_source=PRICE_SOURCE_SYSTEM_PAPER
        )
        return True
    except IntentConflictError:
        # correctness F5：同键异意图=键复用异常（并发窗/账基变动）——
        # 保护性跳过留痕，不中止整锚
        return False
    except (
        BetEligibilityError,
        PriceMismatchError,
        SameFixtureParlayError,
        ValueError,
    ):
        return False


def place_anchor_bets(
    conn: sqlite3.Connection,
    *,
    anchor_label: str,
    business_date: str,
    run_at: datetime,
    params: AutoV1Params,
    reads: QuoteReads | None = None,
) -> dict[str, int]:
    """一锚的选票与落注：单关 top-3（quarter-Kelly 封顶）+ 串关前 5（各 ¥2）。"""
    singles_pool, parlay_pool, excluded = anchor_candidates(
        conn, anchor_at=anchor_datetime(anchor_label, run_at), params=params
    )
    run_iso = run_at.astimezone(UTC).isoformat()
    bankroll = account_bankroll(conn, params)
    prior_parlay_per_match = _parlay_counts_for_day(conn, business_date)
    created = drift = rejected = replayed = 0

    for pick in select_singles(singles_pool, params):
        stake = quarter_kelly_stake(pick.prob, pick.odds, bankroll, params)
        request_id = (
            f"{AUTO_V1}:{business_date}:{anchor_label}:"
            f"s:{pick.fixture_id}:{pick.market_code}"
        )
        outcome = place_single(
            conn, pick, stake=stake, request_id=request_id, run_iso=run_iso, reads=reads
        )
        if outcome == "placed":
            created += 1
        elif outcome == "replayed":
            replayed += 1
        elif outcome == "drift":
            drift += 1
        else:
            rejected += 1
    for pair in select_parlays(
        parlay_pool, params, prior_per_match=prior_parlay_per_match
    ):
        legs = [_verified_leg(conn, p, run_iso) for p in pair]
        if any(leg is None for leg in legs):
            drift += 1
            continue
        # correctness F2：内容键（排序腿 id）——晚到观测重排候选后
        # 同对串关仍撞同键，不双注不漏注
        pair_key = "-".join(str(fid) for fid in sorted(p.fixture_id for p in pair))
        request_id = f"{AUTO_V1}:{business_date}:{anchor_label}:p:{pair_key}"
        if _request_exists(conn, request_id):
            replayed += 1
        elif _place(
            conn,
            [leg for leg in legs if leg is not None],
            stake=params.parlay_stake,
            request_id=request_id,
            reads=reads,
        ):
            created += 1
        else:
            rejected += 1
    stats = {
        "candidates": len(singles_pool) + len(parlay_pool),
        "bets": created,
        "price_drift": drift,
        "gate_rejected": rejected,
        "replayed": replayed,
    }
    stats.update({f"excluded_{key}": value for key, value in excluded.items()})
    return stats


def place_pool_slips(
    conn: sqlite3.Connection,
    *,
    run_at: datetime,
    params: PoolSlipScope,
    source: str = AUTO_V1,
    stake_budget: float | None = None,
) -> dict[str, int]:
    """
    彩池每期次一票（待结算）：既有策略底注，source 幂等分账。

    v2 复用传 auto-v2（同 source 同期不双落）。stake_budget=剩余可落
    金额（None=无帽，v1 冻结口径；auto-v2 传 ¥20/晚测量帽余量）——
    不足一组合注额即整票跳过。
    shortcut: 帽按票粒度扣减（现结构每票恰 1 组合）；多组合复式票
    到来时须按物化后总额对账帽，升级=物化后回扣。

    shortcut: 池面 v2 反火锅结构（修订②③）未实现——本版 ttt14=全稳
    底注、pick9=贪心 9 场、goals4=联合边际 argmax，各 1 组合 ¥2；
    升级=prereg v2 结构票。
    """
    now_iso = run_at.astimezone(UTC).isoformat()
    placed = skipped = 0
    budget = stake_budget
    for market in ("ttt14", "pick9", "goals4"):
        period = _active_period(conn, market, now_iso)
        period_id = int(period["id"]) if period is not None else None
        if period_id is None or _period_slip_exists(conn, period_id, source):
            skipped += 1
            continue
        if budget is not None and budget < params.stake_per_combination:
            skipped += 1
            continue
        picks = _pool_picks(conn, period_id, market, now_iso)
        if not picks:
            skipped += 1
            continue
        try:
            by_match = bt_store.pool_pick_matrix(
                (seq, code) for seq, codes in picks.items() for code in codes
            )
            bt_store.require_pool_slip_creatable(by_match, market)
            with atomic(conn):
                slip = bt_store.create_slip(
                    conn,
                    BetMode.PAPER,
                    pool_period_id=period_id,
                    placed_at=now_iso,
                    note=f"{source} 期次一票（既有策略底注）",
                    source=source,
                )
                for match_seq, codes in picks.items():
                    for code in codes:
                        bt_store.add_pool_pick(conn, slip, match_seq, code)
                bt_store.materialize_combinations(
                    conn,
                    slip,
                    stake_per_combination=params.stake_per_combination,
                    market_code=market,
                )
            placed += 1
            if budget is not None:
                budget -= params.stake_per_combination
        except ValueError:
            skipped += 1
    return {"slips": placed, "slips_skipped": skipped}


def _active_period(
    conn: sqlite3.Connection, market: str, now_iso: str
) -> sqlite3.Row | None:
    """最新一期且销售截止晚于运行时刻（截止缺失=不可判，保守视为可投）。"""
    periods = pool_store.list_pool_periods(conn, market)
    if not periods:
        return None
    period = periods[0]
    deadline = _parse_utc(str(period["sales_deadline"] or ""))
    now_dt = _parse_utc(now_iso)
    if deadline is not None and now_dt is not None and deadline <= now_dt:
        return None
    return period


def _period_slip_exists(conn: sqlite3.Connection, period_id: int, source: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM bet_slips WHERE pool_period_id = ? AND source = ? LIMIT 1",
        (period_id, source),
    ).fetchone()
    return row is not None


def _pool_picks(
    conn: sqlite3.Connection, period_id: int, market: str, now_iso: str
) -> dict[int, list[str]]:
    views = pool_store.match_views(conn, period_id, now_iso)
    base = pool_strategy.default_base_picks(views)
    if market == "goals4":
        return _goals4_argmax_picks(views)
    if market == "pick9":
        top9 = sorted(
            base,
            key=lambda seq: (
                -max(
                    (s.prob or 0.0)
                    for v in views
                    if v.match_seq == seq
                    for s in v.selections
                )
            ),
        )[:9]
        return {seq: [base[seq]] for seq in top9}
    return {seq: [code] for seq, code in base.items()}


def _goals4_argmax_picks(views: list[PoolMatchView]) -> dict[int, list[str]]:
    """
    goals4：各场主档/客档各自 argmax（双侧码——单侧缺选组合不成形）。

    correctness F3：store._goals4_sides 要求每场主客两码齐；边际=联合
    求和（data/pool 注释：主客档相关不得连乘）。
    """
    picks: dict[int, list[str]] = {}
    for view in views:
        joint: dict[str, dict[str, float]] = view.goals4_joint or {}
        if not joint:
            continue
        home_marg = {code: sum(row.values()) for code, row in joint.items()}
        away_marg: dict[str, float] = {}
        for row in joint.values():
            for away_code, prob in row.items():
                away_marg[away_code] = away_marg.get(away_code, 0.0) + prob
        best_home = max(home_marg, key=lambda code: home_marg[code])
        best_away = max(away_marg, key=lambda code: away_marg[code])
        picks[int(view.match_seq)] = [best_home, best_away]
    return picks


def record_anchor_run(
    conn: sqlite3.Connection,
    business_date: str,
    anchor_label: str,
    *,
    stats: dict[str, int],
    strategy: str = AUTO_V1,
    zero_note: str | None = None,
) -> None:
    """
    锚台账 upsert（v46 起按 strategy 分行）。

    行存在=跑了；zero_note 可区分零注原因（票验收）。同引擎同锚重跑才
    MAX 合并；跨引擎分行，换栈日不混账（correctness F2）。zero_note
    显式入参=调用方已定案的零注原因（票 01 版本闸 version_not_live），
    优先于下方推断链。
    """
    if zero_note is None and stats.get("bets", 0) == 0 and stats.get("slips", 0) == 0:
        if stats.get("candidates", 0) == 0:
            zero_note = "no_candidates"
        elif stats.get("price_drift", 0) > 0:
            zero_note = "price_drift"
        elif stats.get("gate_rejected", 0) > 0:
            zero_note = "gate_rejected"
        else:
            zero_note = "no_positive_ev"
    with atomic(conn):
        conn.execute(
            """
            INSERT INTO auto_paper_runs
                (business_date, anchor_label, strategy, ran_at, candidates,
                 bets, slips, zero_note, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(business_date, anchor_label, strategy) DO UPDATE SET
              ran_at = excluded.ran_at, candidates = excluded.candidates,
              bets = MAX(auto_paper_runs.bets, excluded.bets),
              slips = MAX(auto_paper_runs.slips, excluded.slips),
              zero_note = CASE WHEN MAX(auto_paper_runs.bets, excluded.bets) > 0
                          THEN NULL ELSE excluded.zero_note END,
              detail_json = CASE WHEN auto_paper_runs.bets > 0
                            THEN auto_paper_runs.detail_json
                            ELSE excluded.detail_json END
            """,
            (
                business_date,
                anchor_label,
                strategy,
                datetime.now(UTC).isoformat(),
                stats.get("candidates", 0),
                stats.get("bets", 0),
                stats.get("slips", 0),
                zero_note,
                json.dumps(stats, ensure_ascii=False, sort_keys=True),
            ),
        )


def anchor_datetime(anchor_label: str, now: datetime) -> datetime:
    """锚标签（HH:MM，北京时）→ 运行时刻所在日的锚时点（UTC 感知）。"""
    hour, minute = (int(part) for part in anchor_label.split(":"))
    base = now.astimezone(BJ)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def engine_version_triple(params: object) -> tuple[str, str, str]:
    """
    引擎消费身份三元组（correctness F2）：注册与下注闸共用同一推导。

    - estimator：modelling 实现版本规范串（dc 工件 schema + 关键依赖
      版本，``dc_model.implementation_versions()`` 排序键值拼接）；
    - calibration：校准口径族（``CALIBRATION_KINDS``——口径集变化=换版本）；
    - params：参数指纹（``versioning.params_fingerprint``）。

    全维匹配防「同参数指纹不同估计器的未过梯版本借旧版本 live 放行」。
    """
    estimator = ";".join(
        f"{key}={value}"
        for key, value in sorted(dc_model.implementation_versions().items())
    )
    calibration = "+".join(CALIBRATION_KINDS)
    return estimator, calibration, versioning.params_fingerprint(params)


def refuse_if_version_not_live(
    conn: sqlite3.Connection,
    business_date: str,
    anchor_label: str,
    *,
    params: object,
    strategy: str,
) -> dict[str, int] | None:
    """
    版本闸（vision 票 01 消费端最小接线，v1/v2 引擎共用；fail-closed）。

    引擎实际三元组全维匹配的版本非 live-paper/live-real 态→整锚拒单
    留痕（zero_note=version_not_live），返回留痕 stats；未注册三元组
    同样拒（zero_note=version_unregistered——非 live 态即拒，回滚不得
    因换参数指纹重开缝，correctness F3）。放行返回 None 继续正常落注。
    """
    estimator, calibration, fingerprint = engine_version_triple(params)
    gate = versioning.engine_gate(
        conn,
        estimator_version=estimator,
        calibration_version=calibration,
        params_version=fingerprint,
    )
    if gate.allowed:
        return None
    stats = {
        "candidates": 0,
        "bets": 0,
        "slips": 0,
        "version_not_live": 1,
    }
    record_anchor_run(
        conn,
        business_date,
        anchor_label,
        stats=stats,
        strategy=strategy,
        zero_note=gate.reason,
    )
    return stats


def run_auto_v1(
    conn: sqlite3.Connection,
    *,
    run_at: datetime,
    anchor_label: str,
    params: AutoV1Params = AUTO_V1_PARAMS,
    reads: QuoteReads | None = None,
) -> dict[str, int]:
    """
    一锚完整执行（task 入口）：版本闸→落注+池票+台账；返回统计。

    锚标签由调用方解析（ADR-0008 分层：betting 不依赖 evaluation 的
    锚工具，tasks 层解析后下传）。版本闸语义见 refuse_if_version_not_live。
    """
    business_date = fx_store.beijing_business_date(run_at)
    refused = refuse_if_version_not_live(
        conn, business_date, anchor_label, params=params, strategy=AUTO_V1
    )
    if refused is not None:
        return refused
    stats = place_anchor_bets(
        conn,
        anchor_label=anchor_label,
        business_date=business_date,
        run_at=run_at,
        params=params,
        reads=reads,
    )
    stats.update(place_pool_slips(conn, run_at=run_at, params=params))
    record_anchor_run(conn, business_date, anchor_label, stats=stats)
    return stats
