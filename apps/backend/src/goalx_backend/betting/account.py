"""
纸面账户 P&L 聚合面（auto-paper 票 04）：只读派生，不复制结算引擎。

口径全部来自 prereg B 节：账户=初始 ¥1000+Σ已结算 P&L；待结算浮仓单列
（彩池未开奖组合+未赛 open 注 stake）；EV 实现差（实现 ROI vs 决策 EV
均值，snap_ev_model 口径）为主指标；独立暴露（已结算、合格、按场独立）
<200 只报曲线不判盈亏。auto-v1 与人工纸面分列（handoff「不混成一个
数字」纪律）。

edge-audit-20261109 票 13 三修（外部审计反例转正）：
- 税后视图复用 tax_on_prize 的**计税组归集**口径（同人+同期+同游戏合并
  后套税函数，2024 年第 12 号公告；竞彩期=票内最晚比赛编号日期）——
  旧逐注口径下同期同游戏两笔 6000 奖金漏税 2400。
- sample_sufficient 分母=独立暴露数（已结算、非 void、按场去重），非
  全部 paper 注数——未结注/同场重复锚不虚增样本充足性。
- CLV 滚动统计按场簇并一（同场多锚先并成场均值再跨场平均），标准误
  为跨场样本标准误——按行独立会虚增同场复制行的证据精度。
- ROI 双栏：等注口径（realized_roi，stake 加权）与按场口径
  （realized_roi_per_match，场 ROI 先并再跨场等权）并列呈现。
shortcut: haircut 面未接（评估侧按 source_era 校准，账户面接 haircut
随首个 haircut 消费票定口径）。
shortcut: 彩池已实现 P&L 尚无写入方（combinations.hit 无 setter，settle
保持待结算）——奖金导入落地时账户面须补 pool 已结算进 bankroll_now。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from goalx_backend.betting.auto_strategy import AUTO_V1
from goalx_backend.betting.probes import AUTO_V2
from goalx_backend.betting.tax import tax_on_prize
from goalx_backend.data.fixtures import beijing_business_date, jingcai_business_dates
from goalx_backend.data.pool import pool_period_meta

INITIAL_BANKROLL = 1000.0  # prereg B：账户=初始 ¥1000+已结算 P&L
# prereg 判读规则（票 13 收窄语义）：独立暴露（已结算、合格、按场独立）
# <200 只报曲线不判盈亏——非全部 paper 注数
SAMPLE_FLOOR = 200
MANUAL = "manual"
CLV_ROLLING_WINDOW = 50  # CLV 滚动窗=回滚触发器 A 窗（vision 票 03 人裁）


@dataclass(frozen=True)
class StrategyFace:
    """
    一个策略面（auto-v1 或人工纸面）的账户行。

    ROI 双栏（edge-audit 票 13，资金规则一致性）：realized_roi=等注口径
    （Σ利润/Σ注金，stake 加权）；realized_roi_per_match=按场口径（单场注
    先并成场 ROI，再跨场等权平均——「每场固定预算」读法）。跨场串关不属
    按场口径（注金跨场无单一归属），不入其分母分子。
    """

    strategy: str
    bets_n: int
    settled_n: int
    open_n: int
    stake_settled: float
    profit_settled: float
    realized_roi: float | None
    mean_decision_ev: float | None
    ev_realization_gap: float | None
    # 按场口径 ROI（无可归场注=None，与 realized_roi 缺省语义一致）
    realized_roi_per_match: float | None = None
    curve: list[tuple[str, float]] = field(default_factory=list)  # (结算日, 累计)
    max_drawdown: float = 0.0


@dataclass(frozen=True)
class PaperAccountFace:
    """全账视图：两策略分列 + 全账合计 + 浮仓/分玩法/税后。"""

    initial_bankroll: float
    strategies: list[StrategyFace]
    total_bets_n: int
    total_profit_settled: float
    bankroll_now: float
    sample_sufficient: bool
    # 分母=已结算、合格（非 void）、按场独立的暴露数（票 13；≠total_bets_n）
    independent_exposures_n: int
    floating_stake_fixed: float
    floating_stake_pool: float
    # market/bets_n/profit 三键行（API 层 PerMarketView 收紧类型）
    per_market: list[dict[str, float | int | str]]
    tax_total: float
    profit_after_tax: float
    # 分来源账户行（edge-program 票 01）：edge_source 非空纸面注
    per_edge_source: list[EdgeSourceFace] = field(default_factory=list)


def _strategy_rows(conn: sqlite3.Connection, strategy: str) -> list[sqlite3.Row]:
    where = "strategy_version = ?" if strategy != MANUAL else "strategy_version IS NULL"
    return conn.execute(
        f"""
        SELECT b.stake, b.status, b.profit, b.settled_at, b.snap_ev_model,
               (SELECT COUNT(*) FROM bet_legs l WHERE l.bet_id = b.id) AS leg_count
        FROM bets b WHERE b.mode = 'paper' AND {where}
        ORDER BY b.settled_at, b.id
        """,  # noqa: S608 — where 为二选一常量
        (strategy,) if strategy != MANUAL else (),
    ).fetchall()


def _curve_and_drawdown(
    rows: list[sqlite3.Row],
) -> tuple[list[tuple[str, float]], float]:
    """按结算日累计（含初始）+ 最大回撤（峰值到谷底，绝对额）。"""
    by_day: dict[str, float] = {}
    for row in rows:
        if row["status"] == "open" or not row["settled_at"]:
            continue
        day = str(row["settled_at"])[:10]
        by_day[day] = by_day.get(day, 0.0) + float(row["profit"] or 0.0)
    curve: list[tuple[str, float]] = []
    running = INITIAL_BANKROLL
    peak = INITIAL_BANKROLL
    drawdown = 0.0
    for day in sorted(by_day):
        running += by_day[day]
        curve.append((day, round(running, 2)))
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    return curve, round(drawdown, 2)


def _roi_per_match(conn: sqlite3.Connection, strategy: str) -> float | None:
    """
    按场口径 ROI：单场注（全部腿同一场）按场并账，场 ROI 跨场等权平均。

    void 不入分母（同等注口径）；多腿跨场串关不属按场口径。
    """
    where = "strategy_version = ?" if strategy != MANUAL else "strategy_version IS NULL"
    rows = conn.execute(
        f"""
        SELECT b.stake, b.profit,
               (SELECT GROUP_CONCAT(DISTINCT l.fixture_id) FROM bet_legs l
                WHERE l.bet_id = b.id) AS fixtures
        FROM bets b
        WHERE b.mode = 'paper' AND b.status != 'open' AND b.status != 'void'
          AND {where}
        """,  # noqa: S608 — where 为二选一常量
        (strategy,) if strategy != MANUAL else (),
    ).fetchall()
    by_fixture: dict[int, list[float]] = {}
    stakes: dict[int, float] = {}
    for row in rows:
        raw = row["fixtures"]
        if raw is None:
            continue
        fixture_ids = {int(part) for part in str(raw).split(",")}
        if len(fixture_ids) != 1:
            continue  # 跨场串关无单一场归属
        fid = fixture_ids.pop()
        by_fixture.setdefault(fid, []).append(float(row["profit"] or 0.0))
        stakes[fid] = stakes.get(fid, 0.0) + float(row["stake"])
    rois = [
        sum(profits) / stakes[fid]
        for fid, profits in by_fixture.items()
        if stakes[fid] > 0
    ]
    return sum(rois) / len(rois) if rois else None


def strategy_face(conn: sqlite3.Connection, strategy: str) -> StrategyFace:
    """一个策略面的聚合行（含曲线与回撤；ROI 双栏）。"""
    rows = _strategy_rows(conn, strategy)
    settled = [r for r in rows if r["status"] != "open"]
    # ROI 分母剔除 void（退款非投注结果，correctness F2）；partial=更正真实结果保留
    stake_settled = sum(float(r["stake"]) for r in settled if r["status"] != "void")
    profit = sum(float(r["profit"] or 0.0) for r in settled)
    evs = [float(r["snap_ev_model"]) for r in settled if r["snap_ev_model"] is not None]
    realized_roi = profit / stake_settled if stake_settled else None
    mean_ev = sum(evs) / len(evs) if evs else None
    roi_per_match = _roi_per_match(conn, strategy)
    curve, drawdown = _curve_and_drawdown(rows)
    return StrategyFace(
        strategy=strategy,
        bets_n=len(rows),
        settled_n=len(settled),
        open_n=len(rows) - len(settled),
        stake_settled=round(stake_settled, 2),
        profit_settled=round(profit, 2),
        realized_roi=round(realized_roi, 4) if realized_roi is not None else None,
        mean_decision_ev=round(mean_ev, 4) if mean_ev is not None else None,
        ev_realization_gap=(
            round(realized_roi - mean_ev, 4)
            if realized_roi is not None and mean_ev is not None
            else None
        ),
        realized_roi_per_match=(
            round(roi_per_match, 4) if roi_per_match is not None else None
        ),
        curve=curve,
        max_drawdown=drawdown,
    )


def _per_market(conn: sqlite3.Connection) -> list[dict[str, float | int | str]]:
    """分玩法盈亏：单腿注=其玩法；多腿=parlay；彩池票=pool（组合面）。"""
    rows = conn.execute(
        """
        SELECT b.id, b.profit, b.status,
               (SELECT COUNT(*) FROM bet_legs l WHERE l.bet_id = b.id) AS leg_count,
               (SELECT l.market_code FROM bet_legs l WHERE l.bet_id = b.id LIMIT 1)
                   AS first_market
        FROM bets b WHERE b.mode = 'paper'
        """
    ).fetchall()
    pool_rows = conn.execute(
        """
        SELECT COALESCE(SUM(c.stake), 0) AS stake FROM combinations c
        JOIN bet_slips s ON s.id = c.slip_id WHERE c.hit IS NULL
        """
    ).fetchone()
    agg: dict[str, dict[str, float | int | str]] = {}
    for row in rows:
        if row["status"] == "open":
            continue
        market = (
            "parlay" if int(row["leg_count"]) > 1 else str(row["first_market"] or "?")
        )
        bucket = agg.setdefault(market, {"market": market, "bets_n": 0, "profit": 0.0})
        bucket["bets_n"] = int(bucket["bets_n"]) + 1
        bucket["profit"] = round(
            float(bucket["profit"]) + float(row["profit"] or 0.0), 2
        )
    pool_open_stake = float(pool_rows["stake"] or 0.0)
    if pool_open_stake > 0:
        agg.setdefault("pool", {"market": "pool", "bets_n": 0, "profit": 0.0})
    return sorted(agg.values(), key=lambda item: str(item["market"]))


def _floating(conn: sqlite3.Connection) -> tuple[float, float]:
    """待结算浮仓：open 注 stake + 彩池未开奖组合 stake。"""
    fixed = conn.execute(
        """
        SELECT COALESCE(SUM(stake), 0) AS s FROM bets
        WHERE mode = 'paper' AND status = 'open'
        """
    ).fetchone()
    pool = conn.execute(
        """
        SELECT COALESCE(SUM(c.stake), 0) AS s FROM combinations c
        JOIN bet_slips s ON s.id = c.slip_id WHERE c.hit IS NULL
        """
    ).fetchone()
    return round(float(fixed["s"] or 0.0), 2), round(float(pool["s"] or 0.0), 2)


def _after_tax(ctx: dict[int, _SettledBetCtx]) -> tuple[float, float]:
    """
    税后已实现（edge-audit 票 13）：同人+同期+同游戏奖金归集计税。

    2024 年第 12 号公告：同一人同一期同一游戏的中奖收入合并为一次中奖
    收入（≤1 万免、超限全额 20%，复用 tax_on_prize）。竞彩期=票内最晚
    比赛编号日期；池票按（池游戏, 期号）归集；无期次信息的注保守自成
    一计税组（不与他注合并）。纸面面以系统自身为单一纳税人（tax.py
    边界(2) 假设）。返回 (税额合计, 税后净利)。
    """
    prizes: dict[tuple[str, str], float] = {}
    for bet_id, bet in ctx.items():
        if bet.status != "won":
            continue
        key = (bet.game, bet.period or f"bet:{bet_id}")
        prizes[key] = prizes.get(key, 0.0) + float(bet.payout or 0.0)
    tax_total = sum(tax_on_prize(prize)[0] for prize in prizes.values())
    net_profit = -tax_total
    for bet in ctx.values():
        if bet.status == "void":
            continue  # 退款非投注结果（同账户面口径）
        if bet.status == "won":
            net_profit += float(bet.payout or 0.0)
        net_profit -= bet.stake
    return round(tax_total, 2), round(net_profit, 2)


@dataclass(frozen=True)
class _SettledBetCtx:
    """已结算纸面注的归集上下文（税期归集/独立暴露分母共用，票 13）。"""

    status: str
    stake: float
    payout: float | None
    # 计税组两键（同人=纸面单一纳税人假设）：游戏与期次
    game: str  # 固定="jc"；池="pool:<market>"（缺期次面="pool-slip:<id>"）
    period: str | None  # 竞彩=票内最晚编号业务日；池=期号；缺=None
    fixtures: frozenset[int]  # 票内场次集（按场独立暴露/按场 ROI 的聚类键）


def _settled_bet_context(conn: sqlite3.Connection) -> dict[int, _SettledBetCtx]:
    """已结算（含 void）纸面注 → {bet_id: 归集上下文}（一次查询面，只读）。"""
    bets = conn.execute(
        """
        SELECT b.id, b.status, b.stake, b.payout, b.market_kind, b.slip_id
        FROM bets b WHERE b.mode = 'paper' AND b.status != 'open'
        ORDER BY b.settled_at, b.id
        """
    ).fetchall()
    if not bets:
        return {}
    legs: dict[int, set[int]] = {}
    for leg in conn.execute(
        """
        SELECT l.bet_id, l.fixture_id FROM bet_legs l
        JOIN bets b ON b.id = l.bet_id
        WHERE b.mode = 'paper' AND b.status != 'open'
        """
    ):
        legs.setdefault(int(leg["bet_id"]), set()).add(int(leg["fixture_id"]))
    jc_dates = jingcai_business_dates(
        conn, sorted({fid for fids in legs.values() for fid in fids})
    )
    slip_periods = {
        int(row["slip_id"]): int(row["pid"])
        for row in conn.execute(
            """
            SELECT id AS slip_id, pool_period_id AS pid
            FROM bet_slips WHERE pool_period_id IS NOT NULL
            """
        )
    }
    period_meta = pool_period_meta(conn, sorted(set(slip_periods.values())))
    pool_rows = {
        slip_id: period_meta[pid]
        for slip_id, pid in slip_periods.items()
        if pid in period_meta
    }
    context: dict[int, _SettledBetCtx] = {}
    for row in bets:
        bet_id = int(row["id"])
        market_kind = str(row["market_kind"])
        fixtures = frozenset(legs.get(bet_id, ()))
        if market_kind == "pool":
            slip_id = row["slip_id"]
            pool_meta = pool_rows.get(int(slip_id)) if slip_id is not None else None
            if pool_meta is not None:
                game = f"pool:{pool_meta[0]}"
                period = pool_meta[1]
            else:  # 无期次面：保守自成一计税组（绝不与他注合并）
                game = f"pool-bet:{bet_id}"
                period = None
        else:
            game = "jc"  # 竞彩足球单场竞猜=同一游戏（系统仅足球面）
            period = max(
                (jc_dates[fid] for fid in fixtures if fid in jc_dates), default=None
            )
        context[bet_id] = _SettledBetCtx(
            status=str(row["status"]),
            stake=float(row["stake"]),
            payout=None if row["payout"] is None else float(row["payout"]),
            game=game,
            period=period,
            fixtures=fixtures,
        )
    return context


def paper_account_face(conn: sqlite3.Connection) -> PaperAccountFace:
    """全账视图（票 04 交付 1 的读面单源）。"""
    faces = [strategy_face(conn, AUTO_V1), strategy_face(conn, MANUAL)]
    # correctness F1：开放策略集——非 auto-v1 的具名策略（未来 auto-v2/exp-*）
    # 显式成面，不静默从账户消失（prereg 账户=初始+Σ已结算 P&L 不漏项）
    named = {
        str(row["strategy_version"])
        for row in conn.execute(
            """
            SELECT DISTINCT strategy_version FROM bets
            WHERE mode = "paper" AND strategy_version IS NOT NULL
            """
        )
    }
    faces.extend(strategy_face(conn, name) for name in sorted(named - {AUTO_V1}))
    total_bets = sum(f.bets_n for f in faces)
    total_profit = round(sum(f.profit_settled for f in faces), 2)
    floating_fixed, floating_pool = _floating(conn)
    settled_ctx = _settled_bet_context(conn)
    tax_total, after_tax = _after_tax(settled_ctx)
    # 分母收窄（票 13）：已结算、合格（非 void 退款）、按场独立的暴露数——
    # 未结注与同场多锚不虚增「样本充足」（prereg 判读规则的独立暴露语义）。
    # partial（赛果更正回待开，correctness F1）非最终结算，不入分母
    exposures = len(
        {
            fid
            for bet in settled_ctx.values()
            if bet.status in ("won", "lost")
            for fid in bet.fixtures
        }
    )
    return PaperAccountFace(
        initial_bankroll=INITIAL_BANKROLL,
        strategies=faces,
        total_bets_n=total_bets,
        total_profit_settled=total_profit,
        bankroll_now=round(INITIAL_BANKROLL + total_profit, 2),
        sample_sufficient=exposures >= SAMPLE_FLOOR,
        independent_exposures_n=exposures,
        floating_stake_fixed=floating_fixed,
        floating_stake_pool=floating_pool,
        per_market=_per_market(conn),
        per_edge_source=edge_source_faces(conn),
        tax_total=tax_total,
        profit_after_tax=after_tax,
    )


@dataclass(frozen=True)
class EdgeSourceFace:
    """
    一个边缘来源的账户行（edge-program 票 01：分来源 P&L + CLV 滚动）。

    clv_* 只统计已结算且 clv 非空行；clv_n=注行数。均值与标准误按场簇
    并一（edge-audit 票 13）：同场多锚先并成场均值，再跨场平均——按行
    独立会把同场复制行当独立证据，精度虚增。last50=按结算序最近
    CLV_ROLLING_WINDOW 注行（回滚触发器 A 窗）并一后统计；se=跨场样本
    标准误（<2 场 None）——持续正值=六步门第③步证据。
    """

    source: str
    bets_n: int
    settled_n: int
    stake_settled: float
    profit_settled: float
    clv_n: int
    clv_mean: float | None
    clv_mean_last50: float | None
    clv_se_last50: float | None


@dataclass
class _SourceAcc:
    """来源级累计器（可变）；EdgeSourceFace 的字段由此归约。"""

    bets_n: int = 0
    settled_n: int = 0
    stake: float = 0.0
    profit: float = 0.0
    # (场簇键, clv)：注=其票内场次集（单场注=该场；同场次集的串关注彼此
    # 相关故共簇）；无腿注自成簇（bet id）
    clvs: list[tuple[tuple[int, ...], float]] = field(default_factory=list)


def _clustered_clv_stats(
    clvs: list[tuple[tuple[int, ...], float]],
) -> tuple[float | None, float | None]:
    """
    CLV 按场簇并一统计：同簇均值→跨簇（场）均值与跨场标准误。

    依赖结构口径（票 13）：一场=一个独立证据单位。不足两场无标准误
    （诚实 None，不退化为按行独立）。
    """
    if not clvs:
        return None, None
    by_cluster: dict[tuple[int, ...], list[float]] = {}
    for cluster_key, value in clvs:
        by_cluster.setdefault(cluster_key, []).append(value)
    cluster_means = [sum(values) / len(values) for values in by_cluster.values()]
    mean = sum(cluster_means) / len(cluster_means)
    if len(cluster_means) > 1:
        var = sum((m - mean) ** 2 for m in cluster_means) / (len(cluster_means) - 1)
        se = (var / len(cluster_means)) ** 0.5
    else:
        se = None
    return mean, se


def edge_source_faces(conn: sqlite3.Connection) -> list[EdgeSourceFace]:
    """分来源聚合（只读派生）：edge_source 非空的纸面注按来源成行。"""
    rows = conn.execute(
        """
        SELECT b.id, b.edge_source AS source, b.status, b.stake, b.profit, b.clv,
               (SELECT GROUP_CONCAT(DISTINCT l.fixture_id) FROM bet_legs l
                WHERE l.bet_id = b.id) AS fixtures
        FROM bets b
        WHERE b.mode = 'paper' AND b.edge_source IS NOT NULL
        ORDER BY b.settled_at, b.id
        """
    ).fetchall()
    acc: dict[str, _SourceAcc] = {}
    for row in rows:
        entry = acc.setdefault(str(row["source"]), _SourceAcc())
        entry.bets_n += 1
        status = str(row["status"])
        if status == "open":
            continue
        entry.settled_n += 1
        if status != "void":  # void 退款非投注结果，不入 stake 分母（同账户面口径）
            entry.stake = round(entry.stake + float(row["stake"]), 2)
        entry.profit = round(entry.profit + float(row["profit"] or 0.0), 2)
        if row["clv"] is not None:
            raw = row["fixtures"]
            # shortcut: 聚类键=票内场次集合精确相等，非按场归属（单关(A)与
            # 串关(A,B)共享 A 仍算两簇）；当前唯一写入方 auto-v2 探针=单腿+
            # 每场至多一注（修订①），前提成立。当探针下串关或 v1 注打
            # edge_source 标签时升级为按场归属聚类（correctness F2）
            cluster_key = (
                (int(row["id"]),)
                if raw is None
                else tuple(sorted(int(part) for part in str(raw).split(",")))
            )
            entry.clvs.append((cluster_key, float(row["clv"])))
    faces: list[EdgeSourceFace] = []
    for source, entry in sorted(acc.items()):
        mean, _ = _clustered_clv_stats(entry.clvs)
        mean50, se50 = _clustered_clv_stats(entry.clvs[-CLV_ROLLING_WINDOW:])
        faces.append(
            EdgeSourceFace(
                source=source,
                bets_n=entry.bets_n,
                settled_n=entry.settled_n,
                stake_settled=entry.stake,
                profit_settled=entry.profit,
                clv_n=len(entry.clvs),
                clv_mean=round(mean, 6) if mean is not None else None,
                clv_mean_last50=round(mean50, 6) if mean50 is not None else None,
                clv_se_last50=round(se50, 6) if se50 is not None else None,
            )
        )
    return faces


def auto_v1_pulse(conn: sqlite3.Connection) -> tuple[int, sqlite3.Row | None]:
    """
    Auto 引擎今日新注数与最近锚行（晨检第 15 项；表归 betting 故在此）。

    auto-v2 接管槽位后按版本集聚合（v1 史行+v2 新注），台账行本身
    版本无关。shortcut: created_at 为 UTC，substr 日切与北京业务日在
    00-08 点窗错归（观察项口径），升级=落注行带业务日列。
    """
    today = beijing_business_date()
    new_today = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM bets
            WHERE strategy_version IN (?, ?) AND substr(created_at, 1, 10) = ?
            """,
            (AUTO_V1, AUTO_V2, today),
        ).fetchone()[0]
    )
    anchor = conn.execute(
        """
        SELECT business_date, anchor_label, bets, zero_note
        FROM auto_paper_runs ORDER BY ran_at DESC LIMIT 1
        """
    ).fetchone()
    return new_today, anchor
