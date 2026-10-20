"""
冻结决策快照与执行对账（caliber-loop 票 06）。

每个漏斗合格卡（fixture×selection）每日一条冻结记录：决策
would_bet/not + 原因码 + 当时价值代理与 p_sharp——前瞻样本的规则
冻结可审计（规则版本号进记录）。

- **would_bet 门槛（裁决②）**：O_JC × p_sharp − 1 > 0 且两源报价新鲜
  （新鲜由漏斗 L5 已保证）——最低门槛先看分布再收紧；
- **不写 bets 表（裁决①）**：冻结记录先行观测 2-4 周再裁决是否落注；
- **执行对账**：would_bet 记录的锁后漂移（p_sharp_close−p_sharp，
  源T 主锚收盘经 srct_closing_prob 同口径）+赛果结算面（draw_results
  判定，平注记账）自动回填；纸面绩效按原门槛记账，允许零注。

表述纪律：正的价值代理≠盈利证明（收盘去水概率作估计）——报告判读
注意明写，需配来源稳健性与长期赛果验证。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from goalx_backend.betting.funnel import L_ELIGIBLE, funnel_day_row
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs_store
from goalx_backend.markets import SELECTIONS

RULE_VERSION = "frozen-v1"  # 规则版本（would_bet 门槛变更时升版）

# 原因码：漏斗级（ineligible/stale_quote/no_edge）+ 卡级（below_threshold/
# would_bet）；funnel loss_reason → 冻结原因码映射（L2/L3 同归 stale_quote
# ——数据面不可用与不新鲜同为"报价不可据"族）
_L2_PREFIXES = ("l2_",)
_L3_PREFIXES = ("l3_",)
_L5_PREFIXES = ("l5_",)

CloseProvider = Callable[[str, str, str, str], tuple[float, str, str] | None]


@dataclass
class FrozenDayReport:
    """一日冻结决策汇总（日终合并报告面）。"""

    business_date: str
    rule_version: str
    n_cards: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    n_would_bet: int = 0
    would_bet_proxy_stats: dict[str, float | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """平铺（日志/flow 返回）。"""
        return {
            "business_date": self.business_date,
            "rule_version": self.rule_version,
            "n_cards": self.n_cards,
            "by_reason": self.by_reason,
            "n_would_bet": self.n_would_bet,
            "would_bet_proxy_stats": self.would_bet_proxy_stats,
        }


def _fixture_reason(loss_reason: str | None, level_reached: int) -> str:
    """
    漏斗级流失 → 冻结原因码（按流失原因前缀，非层级）。

    l5_*→ineligible；l2_/l3_→stale_quote（报价缺与不新鲜同族）；
    无流失但估值面空→no_edge。
    """
    if loss_reason is None:
        return "" if level_reached >= L_ELIGIBLE else "no_edge"
    if loss_reason.startswith("l5_"):
        return "ineligible"
    if loss_reason.startswith("l2_") or loss_reason.startswith("l3_"):
        return "stale_quote"
    return "no_edge"


def record_frozen_decisions(
    conn: sqlite3.Connection,
    *,
    business_date: str | None = None,
    as_of: str | None = None,
    rule_version: str = RULE_VERSION,
) -> FrozenDayReport:
    """
    冻结一日决策（幂等：同业务日先删后插）。

    分母=当日漏斗明细全体（funnel_fixture_days）：逐场按漏斗级先判
    ineligible/stale_quote/no_edge（三选向同因），合格场逐 selection 按
    would_bet 门槛判 would_bet/below_threshold。
    """
    moment = as_of or datetime.now(UTC).isoformat(timespec="seconds")
    rows = conn.execute(
        " ".join(
            (
                "SELECT fixture_id, level_reached, loss_reason, jc_odds_h, jc_odds_d,",
                "jc_odds_a, p_sharp_h, p_sharp_d, p_sharp_a,",
                "value_proxy_h, value_proxy_d, value_proxy_a",
                "FROM funnel_fixture_days WHERE business_date = ?",
            )
        ),
        (business_date,),
    ).fetchall()
    report = FrozenDayReport(
        business_date=str(business_date), rule_version=rule_version
    )
    proxies: list[float] = []
    records: list[tuple[Any, ...]] = []
    for row in rows:
        fixture_reason = _fixture_reason(row["loss_reason"], int(row["level_reached"]))
        for sel in SELECTIONS:
            odds = row[f"jc_odds_{sel}"]
            sharp = row[f"p_sharp_{sel}"]
            proxy = row[f"value_proxy_{sel}"]
            if fixture_reason:
                reason = fixture_reason
                decision = "not"
            elif proxy is None:
                # 合格场但估值面空（防御面：L3 过必有 p_sharp，理论不可达）
                reason = "no_edge"
                decision = "not"
            elif float(proxy) > 0.0:
                reason = "would_bet"
                decision = "would_bet"
                proxies.append(float(proxy))
            else:
                reason = "below_threshold"
                decision = "not"
            records.append(
                (
                    business_date,
                    int(row["fixture_id"]),
                    sel,
                    decision,
                    reason,
                    rule_version,
                    moment,
                    odds,
                    sharp,
                    proxy,
                )
            )
            report.by_reason[reason] = report.by_reason.get(reason, 0) + 1
    report.n_cards = len(records)
    report.n_would_bet = report.by_reason.get("would_bet", 0)
    report.would_bet_proxy_stats = {
        "n": len(proxies),
        "mean": round(sum(proxies) / len(proxies), 4) if proxies else None,
        "max": round(max(proxies), 4) if proxies else None,
    }
    with conn:
        conn.execute(
            "DELETE FROM frozen_decisions WHERE business_date = ?",
            (business_date,),
        )
        conn.executemany(
            " ".join(
                (
                    "INSERT INTO frozen_decisions",
                    "(business_date, fixture_id, selection_code, decision,",
                    "reason_code, rule_version, as_of,",
                    "odds, p_sharp, value_proxy)",
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                )
            ),
            records,
        )
    return report


def _close_update(row: sqlite3.Row, close: tuple[float, str, str]) -> dict[str, Any]:
    """收盘字段构造（两路共用：未结算同步面 / 已结算晚到补齐）。"""
    update: dict[str, Any] = {"p_sharp_close": round(float(close[0]), 4)}
    if row["p_sharp"] is not None:
        update["drift_after_lock"] = round(
            update["p_sharp_close"] - float(row["p_sharp"]), 4
        )
    return update


def reconcile_frozen_decisions(
    conn: sqlite3.Connection,
    close_provider: CloseProvider,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """
    would_bet 记录执行对账（幂等）：结算面 + 收盘 p_sharp + 锁后漂移。

    结算与收盘解耦（measure-integrity 票 01）：扫描集含"已结算但缺收盘"
    的行——晚场主路径（23:30 盲结算先落，源T 收盘语料次晨才到）的收盘
    独立补齐；已结算行只补收盘面，不重复结算、不重复计数。

    close_provider(home, away, kickoff_utc, selection) → (p_close, 基准,
    sid)|None——生产=clv.srct_closing_prob 同口径（注入缝供测试）。
    同时回填 funnel_fixture_days 收盘列（p_sharp_close_* 可追溯至收盘）。
    """
    moment = now or datetime.now(UTC).isoformat(timespec="seconds")
    stats: dict[str, Any] = {
        "considered": 0,
        "settled": 0,
        "closed": 0,
        "close_backfilled": 0,
        "still_pending": 0,
        "funnel_close_healed": 0,
        "missing_close_total": 0,
        "paper_profit_flat": 0.0,
    }
    pending = conn.execute(
        " ".join(
            (
                "SELECT f.id, f.fixture_id, f.selection_code, f.odds, f.p_sharp,",
                "f.settled_outcome, f.p_sharp_close",
                "FROM frozen_decisions f",
                "WHERE f.decision = 'would_bet'",
                "AND (f.settled_outcome IS NULL OR f.p_sharp_close IS NULL)",
                "ORDER BY f.id",
            )
        )
    ).fetchall()
    for row in pending:
        stats["considered"] += 1
        fixture = fx_store.fixture_detail(conn, int(row["fixture_id"]))
        if fixture is None:
            continue
        kickoff = str(fixture["kickoff_utc"])
        home = str(fixture["home_team"])
        away = str(fixture["away_team"])
        sel = str(row["selection_code"])
        close = close_provider(home, away, kickoff, sel)
        if row["settled_outcome"] is not None:
            # 已结算晚到收盘：只补收盘面（不重复结算/重复计数）
            if close is not None:
                update = _close_update(row, close)
                stats["close_backfilled"] += 1
                _apply_reconcile(conn, int(row["id"]), update, moment)
            continue
        result = rs_store.get_draw_result(conn, int(row["fixture_id"]))
        update: dict[str, Any] = {}
        # 收盘写一次：已有 close 不被后续拍重写
        if close is not None and row["p_sharp_close"] is None:
            update = _close_update(row, close)
            stats["closed"] += 1
        if result is None or int(result["void"] or 0):
            stats["still_pending"] += 1
            if update:
                _apply_reconcile(conn, int(row["id"]), update, moment)
            continue
        home_goals, away_goals = int(result["home_goals"]), int(result["away_goals"])
        ftr = (
            "h"
            if home_goals > away_goals
            else ("a" if home_goals < away_goals else "d")
        )
        won = ftr == sel
        profit = (float(row["odds"]) - 1.0) if won else -1.0
        update["settled_outcome"] = "won" if won else "lost"
        update["settled_profit"] = round(profit, 4)
        stats["settled"] += 1
        stats["paper_profit_flat"] = round(
            float(stats["paper_profit_flat"]) + profit, 4
        )
        _apply_reconcile(conn, int(row["id"]), update, moment)
    # 漏斗收盘列存量治愈：frozen 已有收盘而漏斗列仍空的行一次补齐（单一
    # 传播点——覆盖同拍写入与崩溃窗口丢写的所有分叉类；列级 IS NULL 幂等）
    stats["funnel_close_healed"] = _heal_funnel_close(conn)
    stats["missing_close_total"] = missing_close_total(conn)
    return stats


def _apply_reconcile(
    conn: sqlite3.Connection,
    decision_id: int,
    update: dict[str, Any],
    moment: str,
) -> None:
    """回填一条冻结记录（列白名单构造，幂等；reconciled_at=最近一次对账触碰）。"""
    allowed = ("p_sharp_close", "drift_after_lock", "settled_outcome", "settled_profit")
    sets = [k for k in update if k in allowed]
    if not sets:
        return
    assignments = ", ".join(f"{k} = ?" for k in sets)
    values = [update[k] for k in sets]
    with conn:
        sql = (
            f"UPDATE frozen_decisions SET {assignments}, reconciled_at = ? "  # noqa: S608
            + "WHERE id = ?"
        )
        conn.execute(sql, (*values, moment, decision_id))


def _heal_funnel_close(conn: sqlite3.Connection) -> int:
    """
    漏斗收盘列治愈：frozen 已有收盘而漏斗列仍空的行按选向同键补齐。

    列级 IS NULL 幂等；崩溃窗口（frozen 收盘已落、漏斗列写丢）兜底。
    """
    healed = 0
    for sel in SELECTIONS:
        column = f"p_sharp_close_{sel}"
        match = " ".join(
            (
                "SELECT f.p_sharp_close FROM frozen_decisions f",
                "WHERE f.business_date = funnel_fixture_days.business_date",
                "AND f.fixture_id = funnel_fixture_days.fixture_id",
                f"AND f.selection_code = '{sel}'",
                "AND f.decision = 'would_bet' AND f.p_sharp_close IS NOT NULL",
            )
        )
        sql = " ".join(
            (
                f"UPDATE funnel_fixture_days SET {column} = ({match})",  # noqa: S608 白名单
                f"WHERE {column} IS NULL AND ({match}) IS NOT NULL",
            )
        )
        with conn:
            healed += conn.execute(sql).rowcount
    return healed


def missing_close_total(conn: sqlite3.Connection) -> int:
    """would_bet 缺收盘存量（全库口径诚实账；corpus 跳过拍也可查）。"""
    return int(
        conn.execute(
            " ".join(
                (
                    "SELECT COUNT(*) FROM frozen_decisions",
                    "WHERE decision = 'would_bet' AND p_sharp_close IS NULL",
                )
            )
        ).fetchone()[0]
    )


def frozen_summary(conn: sqlite3.Connection, business_date: str) -> dict[str, Any]:
    """日终合并报告面：冻结汇总+对账汇总。"""
    rows = conn.execute(
        " ".join(
            (
                "SELECT decision, reason_code, COUNT(*) n FROM frozen_decisions",
                "WHERE business_date = ? GROUP BY decision, reason_code",
            )
        ),
        (business_date,),
    ).fetchall()
    by_reason: dict[str, int] = {}
    for decision, reason, n in rows:
        by_reason[f"{decision}:{reason}"] = int(n)
    recon = conn.execute(
        " ".join(
            (
                "SELECT COUNT(*) n, SUM(settled_profit) pl,",
                "AVG(drift_after_lock) drift",
                "FROM frozen_decisions WHERE business_date = ?",
                "AND decision = 'would_bet' AND reconciled_at IS NOT NULL",
            )
        ),
        (business_date,),
    ).fetchone()
    missing_close = conn.execute(
        " ".join(
            (
                "SELECT COUNT(*) FROM frozen_decisions WHERE business_date = ?",
                "AND decision = 'would_bet' AND p_sharp_close IS NULL",
            )
        ),
        (business_date,),
    ).fetchone()[0]
    day = funnel_day_row(conn, business_date)
    return {
        "business_date": business_date,
        "frozen_by_reason": by_reason,
        "reconciliation": {
            "reconciled": int(recon[0] or 0),
            "paper_profit_flat": round(float(recon[1] or 0.0), 4),
            "mean_drift_after_lock": round(float(recon[2]), 4)
            if recon[2] is not None
            else None,
            "missing_close": int(missing_close),
        },
        "funnel_on_sale": int(day["n_on_sale"]) if day else None,
        "note": (
            "冻结决策不自动写 bets 表（人裁决：先行观测 2-4 周）；正的价值代理"
            "≠盈利证明（收盘去水概率作估计），需配来源稳健性与长期赛果验证"
        ),
    }
