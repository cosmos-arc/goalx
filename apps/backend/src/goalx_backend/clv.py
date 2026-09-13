"""
CLV 跟踪与收盘对账（票 32，研究 03 §5）。

- CLV_proxy（概率域）= Shin(欧洲收盘) − 1/竞彩买入价；收盘代理优先取
  purpose=closing 的 odds_api 快照（多 book 共识 Shin），fd PSC 历史行
  接入后可作补充来源；
- 对账对象：已结算（won/lost/void）的 paper/live 注单（单腿 had；串关
  逐腿记录）；
- beat rate 分桶报表（玩法/联赛/距开赛时间）与「CLV_proxy × 结算收益」
  横截面回归斜率（代理线有效性检验，Kaunitz 式移植）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from goalx_backend import odds_math as om
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import SELECTIONS

MINUTES_BUCKETS = ((0, 10), (10, 30), (30, 10**9))
CLOSE_LOOKBACK_MINUTES = 90  # closing 快照须落在开球前后该窗口内
CLOSE_EARLIEST_BEFORE = -30.0  # 开球后 30 分钟内的快照仍算收盘窗口
MIN_REGRESSION_SAMPLES = 3  # 回归最少样本


@dataclass
class ReconcileStats:
    """一次对账的统计。"""

    recorded: int = 0
    skipped: list[str] = field(default_factory=list)


def _closing_prob(
    conn: sqlite3.Connection, fixture_id: int, selection: str
) -> tuple[float, str] | None:
    """该场 had 选择的收盘公允概率（closing 快照多 book 共识 Shin）。"""
    rows = conn.execute(
        """
        SELECT selection_code, source, odds, captured_at, f.kickoff_utc
        FROM odds_snapshots s JOIN fixtures f ON f.id = s.fixture_id
        WHERE s.fixture_id = ? AND s.market_code = 'had'
          AND s.purpose = 'closing' AND s.source LIKE 'odds_api:%'
        ORDER BY s.captured_at, s.id
        """,
        (fixture_id,),
    ).fetchall()
    if not rows:
        return None
    kickoff = str(rows[0]["kickoff_utc"])
    latest_by_book: dict[str, dict[str, float]] = {}
    for row in rows:
        sel = str(row["selection_code"])
        book = str(row["source"])
        if _within_close_window(str(row["captured_at"]), kickoff):
            latest_by_book.setdefault(book, {})[sel] = float(row["odds"])
    books_with_all = [
        book
        for book, prices in latest_by_book.items()
        if all(sel in prices for sel in SELECTIONS)
    ]
    if not books_with_all:
        return None
    consensus = tuple(
        sum(latest_by_book[book][sel] for book in books_with_all) / len(books_with_all)
        for sel in SELECTIONS
    )
    probs = om.shin_implied(consensus)
    return probs[SELECTIONS.index(selection)], "odds_api_closing"


def _within_close_window(captured_at: str, kickoff_utc: str) -> bool:
    """快照须在 kickoff ±CLOSE_LOOKBACK_MINUTES 内（过期共识不算收盘）。"""
    captured = datetime.fromisoformat(captured_at)
    kickoff = datetime.fromisoformat(kickoff_utc)
    delta = (kickoff - captured).total_seconds() / 60.0
    return CLOSE_EARLIEST_BEFORE <= delta <= CLOSE_LOOKBACK_MINUTES


def _minutes_to_kickoff(placed_at: str | None, kickoff_utc: str) -> float | None:
    """下注时距开赛分钟数（无 placed_at 返回 None）。"""
    if not placed_at:
        return None
    placed = datetime.fromisoformat(str(placed_at))
    kickoff = datetime.fromisoformat(kickoff_utc)
    return (kickoff - placed).total_seconds() / 60.0


def reconcile_clv(conn: sqlite3.Connection) -> ReconcileStats:
    """
    已结算注单自动对账（幂等：UNIQUE(bet_id, fixture_id) 吸收重跑）。

    只处理 had 单腿注（v1 可映射口径）；串关注单逐腿写入 clv_records。
    """
    stats = ReconcileStats()
    rows = conn.execute(
        """
        SELECT b.id AS bet_id, b.status, b.placed_at,
               l.fixture_id, l.market_code, l.selection_code, l.locked_odds,
               f.kickoff_utc
        FROM bets b
        JOIN bet_legs l ON l.bet_id = b.id
        JOIN fixtures f ON f.id = l.fixture_id
        WHERE b.status IN ('won', 'lost', 'void') AND l.market_code = 'had'
          AND b.purchased = 1
        """
    ).fetchall()
    for row in rows:
        fixture_id = int(row["fixture_id"])
        selection = str(row["selection_code"])
        close = _closing_prob(conn, fixture_id, selection)
        if close is None:
            stats.skipped.append(f"no_close:bet={row['bet_id']}")
            continue
        close_prob, close_source = close
        taken_odds = float(row["locked_odds"])
        clv = close_prob - 1.0 / taken_odds
        minutes = _minutes_to_kickoff(
            str(row["placed_at"]) if row["placed_at"] else None,
            str(row["kickoff_utc"]),
        )
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO clv_records
            (bet_id, fixture_id, market_code, selection_code, taken_odds,
             close_prob, clv_prob, close_source, minutes_to_kickoff, computed_at)
            VALUES (?, ?, 'had', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["bet_id"]),
                fixture_id,
                selection,
                taken_odds,
                close_prob,
                clv,
                close_source,
                minutes,
                utc_now_iso(),
            ),
        )
        if cur.rowcount > 0:
            stats.recorded += 1
    conn.commit()
    return stats


def _bucket_minutes(minutes: float | None) -> str:
    """距开赛时间桶标签。"""
    if minutes is None:
        return "unknown"
    for low, high in MINUTES_BUCKETS:
        if low <= minutes < high:
            return f"[{low},{'inf' if high > 10**6 else high})min"
    return "unknown"


def clv_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """
    Beat rate 分桶报表 + 回归斜率（CLV_proxy vs 结算收益）。

    beat 定义：clv_prob > 0（收盘概率高于买入隐含概率 = 买到了好价）。
    """
    rows = conn.execute(
        """
        SELECT r.*, b.profit, c.name AS competition
        FROM clv_records r
        JOIN bets b ON b.id = r.bet_id
        JOIN fixtures f ON f.id = r.fixture_id
        JOIN competitions c ON c.id = f.competition_id
        WHERE b.purchased = 1 AND b.status IN ('won', 'lost', 'void')
        """
    ).fetchall()
    buckets: dict[str, dict[str, float]] = {}
    by_market: dict[str, dict[str, float]] = {}
    xs: list[float] = []
    ys: list[float] = []
    for row in rows:
        clv = float(row["clv_prob"])
        beat = clv > 0
        for key, store in (
            (_bucket_minutes(row["minutes_to_kickoff"]), buckets),
            (f"{row['market_code']}:{row['competition']}", by_market),
        ):
            entry = store.setdefault(key, {"n": 0, "beats": 0})
            entry["n"] += 1
            entry["beats"] += 1 if beat else 0
        if row["profit"] is not None:
            xs.append(clv)
            ys.append(float(row["profit"]))
    slope, r_squared = _ols_slope(xs, ys)
    return {
        "n_records": len(rows),
        "beat_rate_overall": (
            sum(1 for r in rows if float(r["clv_prob"]) > 0) / len(rows)
            if rows
            else None
        ),
        "by_minutes_bucket": {
            key: {"n": int(v["n"]), "beat_rate": v["beats"] / v["n"]}
            for key, v in sorted(buckets.items())
        },
        "by_market_league": {
            key: {"n": int(v["n"]), "beat_rate": v["beats"] / v["n"]}
            for key, v in sorted(by_market.items())
        },
        "regression": {"n": len(xs), "slope": slope, "r_squared": r_squared},
    }


def _ols_slope(xs: list[float], ys: list[float]) -> tuple[float | None, float | None]:
    """一元 OLS 斜率与 R²（样本不足返回 None）。"""
    n = len(xs)
    if n < MIN_REGRESSION_SAMPLES:
        return None, None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None, None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx
    syy = sum((y - mean_y) ** 2 for y in ys)
    r_squared = (sxy**2 / (sxx * syy)) if syy > 0 else None
    return slope, r_squared


def clv_json(conn: sqlite3.Connection) -> str:
    """报表 JSON 序列化（API/CLI 展示用）。"""
    return json.dumps(clv_report(conn), ensure_ascii=False, indent=2)
