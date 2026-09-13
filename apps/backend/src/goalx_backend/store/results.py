"""事实域仓储：DrawResult、历史回测底座（hist_matches）与 CostLedger。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from goalx_backend.db import utc_now_iso
from goalx_backend.models import DrawResultInput


def upsert_draw_result(conn: sqlite3.Connection, result: DrawResultInput) -> int:
    """Import an official draw result (唯一事实源，ADR 0001；修正可覆盖)。"""
    previous = get_draw_result(conn, result.fixture_id)
    replacement = result.model_dump(exclude={"correction_reason"})
    if previous is not None:
        old = {key: previous[key] for key in replacement}
        if old == replacement:
            return int(previous["id"])
        if not result.correction_reason or not result.correction_reason.strip():
            raise ValueError("更正已有开奖结果必须提供 correction_reason")
        conn.execute(
            """INSERT INTO draw_result_revisions
               (fixture_id, previous, replacement, reason, recorded_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                result.fixture_id,
                json.dumps(dict(previous)),
                json.dumps(replacement),
                result.correction_reason.strip(),
                utc_now_iso(),
            ),
        )
    conn.execute(
        """
            INSERT INTO draw_results
(fixture_id, home_goals, away_goals,
            half_home_goals, half_away_goals,
void, void_reason, source, published_at,
            created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(fixture_id) DO
            UPDATE SET home_goals=excluded.home_goals,
away_goals=excluded.away_goals,
            half_home_goals=excluded.half_home_goals,
            half_away_goals=excluded.half_away_goals, void=excluded.void,
            void_reason=excluded.void_reason, source=excluded.source,
            published_at=excluded.published_at, created_at=excluded.created_at
        """,
        (
            result.fixture_id,
            result.home_goals,
            result.away_goals,
            result.half_home_goals,
            result.half_away_goals,
            int(result.void),
            result.void_reason,
            result.source,
            result.published_at,
            utc_now_iso(),
        ),
    )
    row = conn.execute(
        "SELECT id FROM draw_results WHERE fixture_id = ?", (result.fixture_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("draw_result upsert 后未找到行")
    return int(row["id"])


def get_draw_result(conn: sqlite3.Connection, fixture_id: int) -> sqlite3.Row | None:
    """Fetch the draw result for one fixture, if imported."""
    return conn.execute(
        "SELECT * FROM draw_results WHERE fixture_id = ?", (fixture_id,)
    ).fetchone()


def draw_results_for_fixtures(
    conn: sqlite3.Connection, fixture_ids: list[int]
) -> dict[int, sqlite3.Row]:
    """Bulk fetch draw results keyed by fixture id."""
    if not fixture_ids:
        return {}
    placeholders = ", ".join("?" for _ in fixture_ids)
    rows = conn.execute(
        f"SELECT * FROM draw_results WHERE fixture_id IN ({placeholders})",  # noqa: S608
        fixture_ids,
    ).fetchall()
    return {int(row["fixture_id"]): row for row in rows}


def upsert_hist_matches(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    """Upsert football-data.co.uk history rows (幂等重跑，票 21 验收)。"""
    count = 0
    for row in rows:
        cur = conn.execute(
            """
                INSERT INTO hist_matches
(competition, season, match_date, home_team,
                away_team,
fthg, ftag, ftr, psc_home, psc_draw, psc_away,
avgc_home,
                avgc_draw, avgc_away)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(competition, season, match_date, home_team, away_team)
DO
                UPDATE SET fthg=excluded.fthg, ftag=excluded.ftag, ftr=excluded.ftr,
                psc_home=excluded.psc_home, psc_draw=excluded.psc_draw,
                psc_away=excluded.psc_away, avgc_home=excluded.avgc_home,
                avgc_draw=excluded.avgc_draw, avgc_away=excluded.avgc_away
            """,
            (
                row["competition"],
                row["season"],
                row["match_date"],
                row["home_team"],
                row["away_team"],
                row["fthg"],
                row["ftag"],
                row["ftr"],
                row["psc_home"],
                row["psc_draw"],
                row["psc_away"],
                row["avgc_home"],
                row["avgc_draw"],
                row["avgc_away"],
            ),
        )
        if cur.rowcount > 0:
            count += 1
    return count


def hist_match_stats(
    conn: sqlite3.Connection, season: str | None = None
) -> dict[str, int]:
    """Row counts and Pinnacle-close coverage for the backtest base."""
    if season:
        sql = """
            SELECT COUNT(*) AS total,
            SUM(CASE WHEN psc_home IS NOT NULL AND psc_draw IS NOT NULL
                AND psc_away IS NOT NULL THEN 1 ELSE 0 END) AS psc_ok,
            SUM(CASE WHEN avgc_home IS NOT NULL AND avgc_draw IS NOT NULL
                AND avgc_away IS NOT NULL THEN 1 ELSE 0 END) AS avgc_ok
            FROM hist_matches WHERE season = ?
        """
        params: tuple[str, ...] = (season,)
    else:
        sql = """
            SELECT COUNT(*) AS total,
            SUM(CASE WHEN psc_home IS NOT NULL AND psc_draw IS NOT NULL
                AND psc_away IS NOT NULL THEN 1 ELSE 0 END) AS psc_ok,
            SUM(CASE WHEN avgc_home IS NOT NULL AND avgc_draw IS NOT NULL
                AND avgc_away IS NOT NULL THEN 1 ELSE 0 END) AS avgc_ok
            FROM hist_matches
        """
        params = ()
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise RuntimeError("hist_match_stats 查询失败")
    return {
        "total": int(row["total"]),
        "psc_present": int(row["psc_ok"] or 0),
        "avgc_present": int(row["avgc_ok"] or 0),
    }


def record_cost(
    conn: sqlite3.Connection,
    category: str,
    *,
    units: float = 1.0,
    amount_cny: float = 0.0,
    note: str | None = None,
    meta: dict[str, object] | None = None,
    occurred_at: str | None = None,
) -> int:
    """Append a CostLedger entry (数据订阅/LLM 调用/API credit)。"""
    cur = conn.execute(
        """
            INSERT INTO cost_ledger
(occurred_at, category, units, amount_cny, note,
            meta)
VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            occurred_at or utc_now_iso(),
            category,
            units,
            amount_cny,
            note,
            json.dumps(meta, ensure_ascii=False) if meta else None,
        ),
    )
    if not cur.lastrowid:
        raise RuntimeError("cost_ledger INSERT 未产生 rowid")
    return int(cur.lastrowid)


def credit_usage(conn: sqlite3.Connection, since_utc: str) -> float:
    """Sum recorded odds_api credit usage since a timestamp."""
    row = conn.execute(
        """
            SELECT COALESCE(SUM(units), 0) AS used FROM cost_ledger
WHERE category =
            'odds_api_credit' AND occurred_at >= ?
        """,
        (since_utc,),
    ).fetchone()
    if row is None:
        raise RuntimeError("credit_usage 查询失败")
    return float(row["used"])
