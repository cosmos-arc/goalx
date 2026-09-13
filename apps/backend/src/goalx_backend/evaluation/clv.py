"""
CLV 跟踪与收盘对账（票 32；票 34 重订纳入边界与口径）。

- CLV_proxy（概率域）= close_prob − 1/竞彩买入价；close 侧取 purpose=closing
  的 odds_api 快照（多 book 完整三向共识 Shin）；
- 纳入边界（票 34/handoff）：closing 必须实际在该腿开赛前观测——
  有效观测时间（observed_at，旧行按源解释）> kickoff 的迟到快照一律排除；
- 口径（票 34）：
  - 单关与 2串1 分开、paper/live 分开报告，不混成一个通过数字；
  - 串关按票级联合概率（两腿 close_prob 连乘，标独立性假设），
    腿级 CLV 仅诊断；回归只用单关，不复制票级 profit 做独立样本；
  - 分母按冻结的决策身份去重（同 mode+同选项组合+同锁定赔率+同 placed_at
    的重试/拆分金额只计一次），原始 Bet 数另报；不用腿数凑 200 注分母。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from goalx_backend import odds_math as om
from goalx_backend.data.quote_evidence import effective_observed_at
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import SELECTIONS

MINUTES_BUCKETS = ((0, 10), (10, 30), (30, 10**9))
CLOSE_LOOKBACK_MINUTES = 90  # closing 快照须落在开球前该窗口内
MIN_REGRESSION_SAMPLES = 3  # 回归最少样本


@dataclass
class ReconcileStats:
    """一次对账的统计。"""

    recorded: int = 0
    skipped: list[str] = field(default_factory=list)


def _closing_prob(
    conn: sqlite3.Connection, fixture_id: int, selection: str
) -> tuple[float, str] | None:
    """
    该场 had 选择的收盘公允概率（closing 快照多 book 完整三向共识 Shin）。

    有效快照 = 观测时间（observed_at，旧行按源解释为 captured_at）不晚于
    kickoff 且在开球前 CLOSE_LOOKBACK_MINUTES 内——开赛后才查到的快照
    只能作历史研究，不能倒填前瞻 closing（票 34）。
    """
    rows = conn.execute(
        """
        SELECT selection_code, source, odds, captured_at, observed_at,
               f.kickoff_utc
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
        observed = effective_observed_at(row)
        if observed is None or not _before_kickoff_window(observed, kickoff):
            continue
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


def _before_kickoff_window(observed_at: str, kickoff_utc: str) -> bool:
    """观测须严格早于 kickoff，且距开赛不超过回看窗（迟到 closing 排除）。"""
    observed = datetime.fromisoformat(observed_at)
    kickoff = datetime.fromisoformat(kickoff_utc)
    minutes_to_kickoff = (kickoff - observed).total_seconds() / 60.0
    return 0 < minutes_to_kickoff <= CLOSE_LOOKBACK_MINUTES


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

    只处理 had 腿（v1 可映射口径）；串关逐腿写记录，票级指标在报表层聚合。
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
            stats.skipped.append(f"no_pre_kickoff_close:bet={row['bet_id']}")
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


@dataclass(frozen=True)
class _BetView:
    """报表层的注级视图（决策身份去重后）。"""

    bet_id: int
    mode: str
    placed_at: str | None
    legs: tuple[
        tuple[int, str, float, float, float], ...
    ]  # (fixture, sel, taken, close_p, clv)
    profit: float | None
    minutes_to_kickoff: float | None

    @property
    def kind(self) -> str:
        return "single" if len(self.legs) == 1 else "parlay2"

    @property
    def decision_key(
        self,
    ) -> tuple[str, tuple[tuple[int, str, float], ...], str | None]:
        """冻结的决策身份：mode + 选项组合与锁定赔率 + 锁定时点（不含金额）。"""
        combo = tuple(sorted((fx, sel, odds) for fx, sel, odds, _, _ in self.legs))
        return (self.mode, combo, self.placed_at)

    @property
    def clv_ticket(self) -> float:
        """票级 CLV：单关即腿值；串关为联合概率 − 联合隐含。"""
        joint_close = 1.0
        joint_implied = 1.0
        for _, _, taken, close_p, _ in self.legs:
            joint_close *= close_p
            joint_implied *= 1.0 / taken
        return joint_close - joint_implied


def _collect_bets(conn: sqlite3.Connection) -> tuple[list[_BetView], dict[str, int]]:
    """
    已结算已购注 → 去重前的注级视图 + 分母统计。

    只收全部腿均为 had 且腿数为 1（单关）或 2（2串1）的注——
    含非 had 腿或多腿串关注单整注排除并计数（v1 可映射口径之外）。
    """
    rows = conn.execute(
        """
        SELECT b.id AS bet_id, b.mode, b.status, b.placed_at, b.profit,
               l.fixture_id, l.market_code, l.selection_code, l.locked_odds,
               r.close_prob, r.clv_prob, r.minutes_to_kickoff
        FROM bets b
        JOIN bet_legs l ON l.bet_id = b.id
        LEFT JOIN clv_records r ON r.bet_id = b.id AND r.fixture_id = l.fixture_id
        WHERE b.status IN ('won', 'lost', 'void') AND b.purchased = 1
        ORDER BY b.id, l.id
        """
    ).fetchall()
    by_bet: dict[int, dict[str, Any]] = {}
    stats = {
        "settled_bets": 0,
        "legs": 0,
        "no_close_bets": 0,
        "unsupported_bets": 0,
    }
    for row in rows:
        bet_id = int(row["bet_id"])
        entry = by_bet.setdefault(
            bet_id,
            {
                "mode": str(row["mode"]),
                "placed_at": row["placed_at"],
                "profit": row["profit"],
                "minutes": row["minutes_to_kickoff"],
                "legs": [],
                "n_legs_total": 0,
                "has_close": True,
                "supported": True,
            },
        )
        entry["n_legs_total"] += 1
        stats["legs"] += 1
        if str(row["market_code"]) != "had":
            entry["supported"] = False
        if row["close_prob"] is None:
            entry["has_close"] = False
            continue
        entry["legs"].append(
            (
                int(row["fixture_id"]),
                str(row["selection_code"]),
                float(row["locked_odds"]),
                float(row["close_prob"]),
                float(row["clv_prob"]),
            )
        )
    views: list[_BetView] = []
    for bet_id, entry in by_bet.items():
        stats["settled_bets"] += 1
        supported_shape = entry["supported"] and entry["n_legs_total"] in (1, 2)
        if not supported_shape:
            stats["unsupported_bets"] += 1
            continue
        if not entry["has_close"] or not entry["legs"]:
            stats["no_close_bets"] += 1
            continue
        views.append(
            _BetView(
                bet_id=bet_id,
                mode=str(entry["mode"]),
                placed_at=(
                    str(entry["placed_at"]) if entry["placed_at"] is not None else None
                ),
                legs=tuple(entry["legs"]),
                profit=(
                    float(entry["profit"]) if entry["profit"] is not None else None
                ),
                minutes_to_kickoff=(
                    float(entry["minutes"]) if entry["minutes"] is not None else None
                ),
            )
        )
    return views, stats


def _dedup_by_decision(
    views: list[_BetView],
) -> tuple[list[_BetView], int]:
    """按决策身份去重（重试/拆分金额只计一次）；返回 (唯一注, 重复数)。"""
    seen: dict[
        tuple[str, tuple[tuple[int, str, float], ...], str | None], _BetView
    ] = {}
    duplicates = 0
    for view in views:
        key = view.decision_key
        if key in seen:
            duplicates += 1
        else:
            seen[key] = view
    return sorted(seen.values(), key=lambda v: v.bet_id), duplicates


def _group_stats(group: list[_BetView]) -> dict[str, Any]:
    """一组的 beat/CLV 汇总（票级口径）。"""
    if not group:
        return {"n_bets": 0, "beat_rate": None, "avg_clv": None}
    clvs = [v.clv_ticket for v in group]
    beats = sum(1 for c in clvs if c > 0)
    return {
        "n_bets": len(group),
        "beat_rate": beats / len(group),
        "avg_clv": sum(clvs) / len(clvs),
    }


def clv_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """
    票级 CLV 报表：单关/2串1 × paper/live 分组 + 去重分母 + 单关回归。

    beat 定义：票级 clv > 0（串关按联合概率，独立性假设已声明）。
    """
    views, denom = _collect_bets(conn)
    unique, duplicates = _dedup_by_decision(views)
    denom |= {
        "raw_bets": len(views) + duplicates,
        "reconciled_bets": len(views),
        "unique_bets": len(unique),
        "deduped_duplicates": duplicates,
        "fixtures": len({fx for v in unique for fx, *_ in v.legs}),
    }
    groups: dict[str, dict[str, dict[str, Any]]] = {"single": {}, "parlay2": {}}
    for kind, sections in groups.items():
        for mode in ("paper", "live"):
            sections[mode] = _group_stats(
                [v for v in unique if v.kind == kind and v.mode == mode]
            )
    buckets: dict[str, dict[str, float]] = {}
    for view in (v for v in unique if v.kind == "single"):
        key = _bucket_minutes(view.minutes_to_kickoff)
        entry = buckets.setdefault(key, {"n": 0, "beats": 0})
        entry["n"] += 1
        entry["beats"] += 1 if view.clv_ticket > 0 else 0
    singles = [
        (v.clv_ticket, v.profit)
        for v in unique
        if v.kind == "single" and v.profit is not None
    ]
    slope, r_squared = _ols_slope(
        [clv for clv, _ in singles], [float(profit) for _, profit in singles]
    )
    return {
        "singles": groups["single"],
        "parlay2": groups["parlay2"],
        # 串关票级联合概率按两腿独立连乘（票 34：声明假设，不作回归样本）
        "independence_assumed": True,
        "denominator": denom,
        "by_minutes_bucket_single": {
            key: {"n": int(v["n"]), "beat_rate": v["beats"] / v["n"]}
            for key, v in sorted(buckets.items())
            if v["n"]
        },
        "regression": {
            "n": len(singles),
            "slope": slope,
            "r_squared": r_squared,
            "note": "singles only",
        },
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
