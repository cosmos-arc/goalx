"""
复核队列操作（票 13：赛后入队 + 结论三分类 + 盲评记录）。

赛后路：结算后"一对一错"场次（ML 对 LLM 错或反之）入队
route='post_settle'；结论三分类（情报关键贡献/无关/误导）只进评测集，
不改任何预测工件（票 05 冻结）。盲评：双周匿名二选一落 blind_reviews
（参考列）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from goalx_backend.data import results as rs_store
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.llm.gate import latest_triple

_VERDICTS = ("key_contribution", "irrelevant", "misleading")


@dataclass
class EnqueueStats:
    """一次赛后入队的计数。"""

    settled_checked: int = 0
    enqueued: int = 0
    skipped_known: int = 0


def _outcome_index(ftr: str) -> int:
    return {"H": 0, "D": 1, "A": 2}[ftr]


def enqueue_post_settle(
    conn: sqlite3.Connection, *, now: str | None = None
) -> EnqueueStats:
    """结算后一对一错场次入队（幂等：UNIQUE(fixture_id, route)）。"""
    stats = EnqueueStats()
    moment = now or utc_now_iso()
    draw_rows = rs_store.list_draw_results(conn)
    with atomic(conn):
        for draw in draw_rows:
            fixture_id = int(draw["fixture_id"])
            ftr = (
                "H"
                if int(draw["home_goals"]) > int(draw["away_goals"])
                else (
                    "D" if int(draw["home_goals"]) == int(draw["away_goals"]) else "A"
                )
            )
            if int(draw["void"]) == 1:
                continue
            ml = latest_triple(conn, fixture_id, "ml", as_of=moment)
            llm = latest_triple(conn, fixture_id, "llm", as_of=moment)
            if ml is None or llm is None:
                continue
            stats.settled_checked += 1
            actual = _outcome_index(ftr)
            ml_probs: tuple[float, float, float] = ml
            llm_probs: tuple[float, float, float] = llm
            ml_hit = max(range(3), key=lambda i: ml_probs[i]) == actual
            llm_hit = max(range(3), key=lambda i: llm_probs[i]) == actual
            if ml_hit == llm_hit:
                continue  # 同对同错——不构成"一对一错"
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO review_items
                    (fixture_id, route, js_value, status, created_at)
                VALUES (?, 'post_settle', NULL, 'open', ?)
                """,
                (fixture_id, moment),
            )
            if cur.rowcount:
                stats.enqueued += 1
            else:
                stats.skipped_known += 1
    return stats


def record_verdict(
    conn: sqlite3.Connection,
    review_item_id: int,
    verdict: str,
    *,
    note: str | None = None,
    now: str | None = None,
) -> bool:
    """复核结论三分类（open → done；结论只进评测集）。"""
    if verdict not in _VERDICTS:
        raise ValueError(f"verdict 须为 {_VERDICTS} 之一")
    moment = now or utc_now_iso()
    cur = conn.execute(
        """
        UPDATE review_items
        SET status = 'done', verdict = ?, note = COALESCE(?, note), decided_at = ?
        WHERE id = ? AND status = 'open'
        """,
        (verdict, note, moment, review_item_id),
    )
    return bool(cur.rowcount)


def record_blind_review(
    conn: sqlite3.Connection,
    *,
    cycle: str,
    fixture_id: int,
    choice: str,
    note: str | None = None,
    now: str | None = None,
) -> bool:
    """盲评匿名二选一（ml|llm）；同周期同场次幂等。"""
    if choice not in ("ml", "llm"):
        raise ValueError("choice 须为 'ml' | 'llm'")
    moment = now or utc_now_iso()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO blind_reviews
            (cycle, fixture_id, choice, note, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (cycle, fixture_id, choice, note, moment),
    )
    return bool(cur.rowcount)


def open_reviews(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """待复核清单（供 UI/人工入口）。"""
    return conn.execute(
        "SELECT * FROM review_items WHERE status = 'open' ORDER BY created_at"
    ).fetchall()


def verdict_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """已完成复核的结论三分类计数（评测报告读取入口）。"""
    rows = conn.execute(
        """
        SELECT verdict, COUNT(*) AS n FROM review_items
        WHERE status = 'done' AND verdict IS NOT NULL GROUP BY verdict
        """
    ).fetchall()
    return {str(r["verdict"]): int(r["n"]) for r in rows}


def blind_review_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """盲评二选一计数（评测报告读取入口）。"""
    rows = conn.execute(
        "SELECT choice, COUNT(*) AS n FROM blind_reviews GROUP BY choice"
    ).fetchall()
    return {str(r["choice"]): int(r["n"]) for r in rows}
