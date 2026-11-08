"""
纸面账户 P&L 聚合面（auto-paper 票 04）：只读派生，不复制结算引擎。

口径全部来自 prereg B 节：账户=初始 ¥1000+Σ已结算 P&L；待结算浮仓单列
（彩池未开奖组合+未赛 open 注 stake）；EV 实现差（实现 ROI vs 决策 EV
均值，snap_ev_model 口径）为主指标；累计注数<200 只报曲线不判盈亏。
auto-v1 与人工纸面分列（handoff「不混成一个数字」纪律）。税后视图
复用 tax_on_prize（逐注奖金口径，frozen_tax_face 的计税组口径是冻结面
专用）。shortcut: haircut 面未接（评估侧按 source_era 校准，账户面接
haircut 随首个 haircut 消费票定口径）。
shortcut: 彩池已实现 P&L 尚无写入方（combinations.hit 无 setter，settle
保持待结算）——奖金导入落地时账户面须补 pool 已结算进 bankroll_now。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from goalx_backend.betting.auto_strategy import AUTO_V1
from goalx_backend.betting.tax import tax_on_prize
from goalx_backend.data.fixtures import beijing_business_date

INITIAL_BANKROLL = 1000.0  # prereg B：账户=初始 ¥1000+已结算 P&L
SAMPLE_FLOOR = 200  # prereg 判读规则：累计注数<200 只报曲线不判盈亏
MANUAL = "manual"


@dataclass(frozen=True)
class StrategyFace:
    """一个策略面（auto-v1 或人工纸面）的账户行。"""

    strategy: str
    bets_n: int
    settled_n: int
    open_n: int
    stake_settled: float
    profit_settled: float
    realized_roi: float | None
    mean_decision_ev: float | None
    ev_realization_gap: float | None
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
    floating_stake_fixed: float
    floating_stake_pool: float
    # market/bets_n/profit 三键行（API 层 PerMarketView 收紧类型）
    per_market: list[dict[str, float | int | str]]
    tax_total: float
    profit_after_tax: float


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


def strategy_face(conn: sqlite3.Connection, strategy: str) -> StrategyFace:
    """一个策略面的聚合行（含曲线与回撤）。"""
    rows = _strategy_rows(conn, strategy)
    settled = [r for r in rows if r["status"] != "open"]
    # ROI 分母剔除 void（退款非投注结果，correctness F2）；partial=更正真实结果保留
    stake_settled = sum(float(r["stake"]) for r in settled if r["status"] != "void")
    profit = sum(float(r["profit"] or 0.0) for r in settled)
    evs = [float(r["snap_ev_model"]) for r in settled if r["snap_ev_model"] is not None]
    realized_roi = profit / stake_settled if stake_settled else None
    mean_ev = sum(evs) / len(evs) if evs else None
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


def _after_tax(conn: sqlite3.Connection) -> tuple[float, float]:
    """税后已实现（复用 tax_on_prize 逐注奖金口径）：won 注奖金计税。"""
    rows = conn.execute(
        "SELECT status, stake, payout FROM bets WHERE mode='paper' AND status != 'open'"
    ).fetchall()
    tax_total = 0.0
    net_profit = 0.0
    for row in rows:
        stake = float(row["stake"])
        if row["status"] == "won":
            prize = float(row["payout"] or 0.0)
            tax, net = tax_on_prize(prize)
            tax_total += tax
            net_profit += net - stake
        elif row["status"] == "void":
            net_profit += 0.0
        else:
            net_profit -= stake
    return round(tax_total, 2), round(net_profit, 2)


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
    tax_total, after_tax = _after_tax(conn)
    return PaperAccountFace(
        initial_bankroll=INITIAL_BANKROLL,
        strategies=faces,
        total_bets_n=total_bets,
        total_profit_settled=total_profit,
        bankroll_now=round(INITIAL_BANKROLL + total_profit, 2),
        sample_sufficient=total_bets >= SAMPLE_FLOOR,
        floating_stake_fixed=floating_fixed,
        floating_stake_pool=floating_pool,
        per_market=_per_market(conn),
        tax_total=tax_total,
        profit_after_tax=after_tax,
    )


def auto_v1_pulse(conn: sqlite3.Connection) -> tuple[int, sqlite3.Row | None]:
    """
    auto-v1 今日新注数与最近锚行（晨检第 15 项；表归 betting 故在此）。

    shortcut: created_at 为 UTC，substr 日切与北京业务日在 00-08 点窗错归
    （观察项口径），升级=落注行带业务日列。
    """
    today = beijing_business_date()
    new_today = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM bets
            WHERE strategy_version = ? AND substr(created_at, 1, 10) = ?
            """,
            (AUTO_V1, today),
        ).fetchone()[0]
    )
    anchor = conn.execute(
        """
        SELECT business_date, anchor_label, bets, zero_note
        FROM auto_paper_runs ORDER BY ran_at DESC LIMIT 1
        """
    ).fetchone()
    return new_today, anchor
