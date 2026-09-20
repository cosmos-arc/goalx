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


def list_draw_results(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """全部已导入的开奖结果（按场次稳定排序）。"""
    return conn.execute("SELECT * FROM draw_results ORDER BY fixture_id").fetchall()


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


def latest_hist_date(conn: sqlite3.Connection, competition: str) -> str | None:
    """某联赛历史底座的最新比赛日（训练 as-of 上界的缺省值）。"""
    row = conn.execute(
        "SELECT MAX(match_date) AS d FROM hist_matches WHERE competition = ?",
        (competition,),
    ).fetchone()
    return str(row["d"]) if row and row["d"] is not None else None


def hist_rows_through(
    conn: sqlite3.Connection, competition: str, as_of: str
) -> list[sqlite3.Row]:
    """某联赛 as_of（含）之前的全部历史行（防前视：上界由调用方给出）。"""
    return conn.execute(
        """
        SELECT match_date, home_team, away_team, fthg, ftag
        FROM hist_matches
        WHERE competition = ? AND match_date <= ?
        ORDER BY match_date
        """,
        (competition, as_of),
    ).fetchall()


def settled_jingcai_fixtures(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """已结算（非无效）的竞彩场次：前瞻评分集合的底盘（票 34）。"""
    return conn.execute(
        """
        SELECT f.id AS fixture_id, f.kickoff_utc, c.name AS competition,
               d.home_goals, d.away_goals
        FROM fixtures f
        JOIN match_codes mc ON mc.fixture_id = f.id AND mc.kind = 'jingcai'
        JOIN competitions c ON c.id = f.competition_id
        JOIN draw_results d ON d.fixture_id = f.id AND d.void = 0
        ORDER BY f.kickoff_utc
        """
    ).fetchall()


def ftr_for_hist_ids(conn: sqlite3.Connection, ids: list[int]) -> dict[int, str]:
    """历史行的全场胜负（H/D/A），按 hist id 索引（指标评分用）。"""
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, ftr FROM hist_matches WHERE id IN ({placeholders})",  # noqa: S608
        ids,
    ).fetchall()
    return {int(r["id"]): str(r["ftr"]) for r in rows}


def hist_team_names(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """历史底座的逐联赛队名清单（对齐覆盖率报告用）。"""
    return conn.execute(
        """
        SELECT competition, home_team AS team FROM hist_matches
        UNION
        SELECT competition, away_team AS team FROM hist_matches
        ORDER BY competition, team
        """
    ).fetchall()


def hist_rows_in_seasons(
    conn: sqlite3.Connection, competitions: tuple[str, ...], seasons: tuple[str, ...]
) -> list[sqlite3.Row]:
    """回测范围的历史行（联赛×赛季过滤，按联赛、日期排序）。"""
    comp_ph = ", ".join("?" for _ in competitions)
    season_ph = ", ".join("?" for _ in seasons)
    return conn.execute(
        f"""
        SELECT * FROM hist_matches
        WHERE competition IN ({comp_ph}) AND season IN ({season_ph})
        ORDER BY competition, match_date
        """,  # noqa: S608
        (*competitions, *seasons),
    ).fetchall()


def hist_close_odds_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """全量历史行的收盘基准列（PSC/AvgC，基准分期质检用）。"""
    return conn.execute(
        """
        SELECT competition, season, match_date, psc_home, psc_draw, psc_away,
               avgc_home, avgc_draw, avgc_away FROM hist_matches
        ORDER BY competition, match_date
        """
    ).fetchall()


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


def category_note_count(
    conn: sqlite3.Connection, category: str, note: str, since_utc: str
) -> int:
    """某类别某 note 自时点起的行数（M3 控制事件去重入口）。"""
    row = conn.execute(
        """
            SELECT COUNT(*) AS n FROM cost_ledger
            WHERE category = ? AND note = ? AND occurred_at >= ?
        """,
        (category, note, since_utc),
    ).fetchone()
    if row is None:
        raise RuntimeError("category_note_count 查询失败")
    return int(row["n"])


def category_spend_cny(
    conn: sqlite3.Connection, category: str, since_utc: str
) -> float:
    """指定类别自某时点起的金额合计（LLM 月预算熔断等跨域读取入口）。"""
    row = conn.execute(
        """
            SELECT COALESCE(SUM(amount_cny), 0) AS s FROM cost_ledger
            WHERE category = ? AND occurred_at >= ?
        """,
        (category, since_utc),
    ).fetchone()
    if row is None:
        raise RuntimeError("category_spend_cny 查询失败")
    return float(row["s"])


def cost_summary(
    conn: sqlite3.Connection, since_utc: str | None = None
) -> list[sqlite3.Row]:
    """已记账成本按类别聚合（金额与 units 分列；票 36 期间成本摘要）。"""
    sql = """
            SELECT category, SUM(units) AS units, SUM(amount_cny) AS amount_cny,
                   COUNT(*) AS entries
            FROM cost_ledger
        """
    params: tuple[str, ...] = ()
    if since_utc is not None:
        sql += " WHERE occurred_at >= ?"
        params = (since_utc,)
    sql += " GROUP BY category ORDER BY category"
    return conn.execute(sql, params).fetchall()


# --- Understat xG 特征读取（票 45；表归 data 域，ingest 写 / 各层读经本模块）---

# fd 历史底座代码 → understat slug（票面覆盖=五大；俄超 rfpl 按需追加）
UNDERSTAT_LEAGUES: dict[str, str] = {
    "E0": "epl",
    "SP1": "la_liga",
    "I1": "serie_a",
    "D1": "bundesliga",
    "F1": "ligue_1",
}
UNDERSTAT_DEFAULT_LEAGUES: tuple[str, ...] = tuple(UNDERSTAT_LEAGUES.values())


def understat_compare_rows(
    conn: sqlite3.Connection, league: str, seasons: tuple[str, ...]
) -> list[sqlite3.Row]:
    """某联赛多季场次行，按开球时刻排序（对比语料读取；season 过滤在内存）。"""
    wanted = set(seasons)
    return [
        row
        for row in conn.execute(
            """
            SELECT match_id, season, datetime_utc, home_team_id, away_team_id,
                   goals_home, goals_away, npxg_home, npxg_away,
                   forecast_w, forecast_d, forecast_l
            FROM understat_matches
            WHERE league = ?
            ORDER BY datetime_utc
            """,
            (league,),
        ).fetchall()
        if str(row["season"]) in wanted
    ]
