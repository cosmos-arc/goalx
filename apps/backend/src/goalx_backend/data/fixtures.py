"""赛程/市场域仓储：赛事、球队、场次、销售编号与赔率快照。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from goalx_backend.db import utc_now_iso
from goalx_backend.models import (
    MatchCodeInput,
    ObservationInput,
    SaleStatusInput,
    SnapshotInput,
    Tier,
)

CST = timezone(timedelta(hours=8))  # 竞彩官方时区：北京时间


def beijing_business_date(now: datetime | None = None) -> str:
    """业务日 = 北京时区日历日（spec §3；全仓唯一出处）。"""
    return (now or datetime.now(UTC)).astimezone(CST).strftime("%Y-%m-%d")


def _lookup_id(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    """Fetch an id by natural key; raises LookupError when absent."""
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise LookupError(f"upsert id lookup failed: {params}")
    return int(row["id"])


def upsert_competition(
    conn: sqlite3.Connection,
    name: str,
    *,
    tier: Tier = Tier.TIER2,
    odds_api_sport_key: str | None = None,
    api_football_league_id: int | None = None,
) -> int:
    """Insert or update a competition by name; return its id."""
    conn.execute(
        """
            INSERT INTO competitions
(name, tier, odds_api_sport_key,
            api_football_league_id, created_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(name)
            DO UPDATE SET tier=excluded.tier,
            odds_api_sport_key=COALESCE(
excluded.odds_api_sport_key,
            competitions.odds_api_sport_key),
            api_football_league_id=COALESCE(
excluded.api_football_league_id,
            competitions.api_football_league_id)
        """,
        (name, tier.value, odds_api_sport_key, api_football_league_id, utc_now_iso()),
    )
    return _lookup_id(conn, "SELECT id FROM competitions WHERE name = ?", (name,))


def find_team_by_name(
    conn: sqlite3.Connection, canonical_name: str
) -> sqlite3.Row | None:
    """按 canonical 队名查球队行（人工别名覆盖用）。"""
    return conn.execute(
        "SELECT id FROM teams WHERE canonical_name = ?", (canonical_name,)
    ).fetchone()


def upsert_team(conn: sqlite3.Connection, canonical_name: str) -> int:
    """Insert a team by canonical name (idempotent); return its id."""
    conn.execute(
        """
            INSERT INTO teams (canonical_name, created_at) VALUES (?, ?)
ON
            CONFLICT(canonical_name) DO NOTHING
        """,
        (canonical_name, utc_now_iso()),
    )
    return _lookup_id(
        conn, "SELECT id FROM teams WHERE canonical_name = ?", (canonical_name,)
    )


def upsert_fixture(
    conn: sqlite3.Connection,
    competition_id: int,
    kickoff_utc: str,
    home_team_id: int,
    away_team_id: int,
    *,
    stage: str | None = None,
) -> int:
    """Insert a fixture on its natural key (idempotent); return its id."""
    conn.execute(
        """
            INSERT INTO fixtures
(competition_id, kickoff_utc, home_team_id,
            away_team_id, stage)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(competition_id,
            kickoff_utc, home_team_id, away_team_id)
DO NOTHING
        """,
        (competition_id, kickoff_utc, home_team_id, away_team_id, stage),
    )
    return _lookup_id(
        conn,
        """
            SELECT id FROM fixtures WHERE competition_id = ? AND kickoff_utc = ?
AND
            home_team_id = ? AND away_team_id = ?
        """,
        (competition_id, kickoff_utc, home_team_id, away_team_id),
    )


def update_fixture_kickoff(
    conn: sqlite3.Connection, fixture_id: int, kickoff_utc: str
) -> None:
    """Reschedule: move an existing fixture's kickoff（票 35：改期不新造比赛）。"""
    conn.execute(
        "UPDATE fixtures SET kickoff_utc = ? WHERE id = ?", (kickoff_utc, fixture_id)
    )


def find_fixture_by_source_match(
    conn: sqlite3.Connection, kind: str, source_match_id: str
) -> sqlite3.Row | None:
    """Locate a fixture via a source-system match id (e.g. sporttery matchId)."""
    return conn.execute(
        """
            SELECT f.* FROM fixtures f JOIN match_codes mc ON mc.fixture_id = f.id
WHERE
            mc.kind = ? AND mc.source_match_id = ?
        """,
        (kind, source_match_id),
    ).fetchone()


def upsert_match_code(conn: sqlite3.Connection, code: MatchCodeInput) -> int:
    """Insert or refresh an official sales code for a fixture; return its id."""
    conn.execute(
        """
            INSERT INTO match_codes
(fixture_id, kind, business_date, code,
            source_match_id, is_single)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(kind,
            business_date, code) DO UPDATE SET
fixture_id=excluded.fixture_id,
            source_match_id=excluded.source_match_id,
is_single=excluded.is_single
        """,
        (
            code.fixture_id,
            code.kind,
            code.business_date,
            code.code,
            code.source_match_id,
            code.is_single,
        ),
    )
    return _lookup_id(
        conn,
        "SELECT id FROM match_codes WHERE kind = ? AND business_date = ? AND code = ?",
        (code.kind, code.business_date, code.code),
    )


def record_quote_observation(conn: sqlite3.Connection, obs: ObservationInput) -> int:
    """Append one raw-observation evidence row; return its id."""
    cur = conn.execute(
        """
            INSERT INTO quote_observations
(source, purpose, observed_at, source_updated_at, snapshot_at,
            endpoint, parse_version, raw_sha256, raw_ref, summary, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            obs.source,
            obs.purpose.value,
            obs.observed_at,
            obs.source_updated_at,
            obs.snapshot_at,
            obs.endpoint,
            obs.parse_version,
            obs.raw_sha256,
            obs.raw_ref,
            obs.summary,
            utc_now_iso(),
        ),
    )
    row_id = int(cur.lastrowid or 0)
    if row_id <= 0:
        raise RuntimeError("quote_observation insert failed")
    return row_id


def append_sale_status(conn: sqlite3.Connection, status: SaleStatusInput) -> int:
    """Append a sale-status observation (append-only 时序, 票 35)."""
    cur = conn.execute(
        """
            INSERT INTO sale_statuses
(fixture_id, market_code, sale_state, single_eligible, observed_at,
            source_updated_at, observation_id, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            status.fixture_id,
            status.market_code,
            status.sale_state,
            None if status.single_eligible is None else int(status.single_eligible),
            status.observed_at,
            status.source_updated_at,
            status.observation_id,
            utc_now_iso(),
        ),
    )
    row_id = int(cur.lastrowid or 0)
    if row_id <= 0:
        raise RuntimeError("sale_status insert failed")
    return row_id


def latest_sale_status_asof(
    conn: sqlite3.Connection,
    fixture_id: int,
    market_code: str,
    as_of: str,
) -> sqlite3.Row | None:
    """
    Latest known sale status for a fixture/market at a decision time.

    市场级行（market_code 精确匹配）优先于比赛级行（market_code IS NULL）；
    只取 observed_at 不晚于 as_of 的观测——之后的观测不能证明当时状态。
    """
    return conn.execute(
        """
            SELECT * FROM sale_statuses
            WHERE fixture_id = ? AND observed_at <= ?
              AND (market_code = ? OR market_code IS NULL)
            ORDER BY (market_code = ?) DESC, observed_at DESC, id DESC
            LIMIT 1
        """,
        (fixture_id, as_of, market_code, market_code),
    ).fetchone()


def insert_odds_snapshot(conn: sqlite3.Connection, snap: SnapshotInput) -> int | None:
    """
    Append an odds snapshot; returns new id, None when an identical row exists.

    Append-only（ADR 0001）：同自然键的重复采集以 ``INSERT OR IGNORE`` 吸收，
    永不修改既有行。observed_at/source_updated_at/observation_id 是票 35
    证据列；旧行为 NULL（时间语义按源解释，未知不倒填）。
    """
    cur = conn.execute(
        """
            INSERT OR IGNORE INTO odds_snapshots
            (fixture_id, market_code, selection_code, source, purpose, odds,
             captured_at, observed_at, source_updated_at, observation_id,
             meta, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snap.fixture_id,
            snap.market_code,
            snap.selection_code,
            snap.source,
            snap.purpose.value,
            snap.odds,
            snap.captured_at,
            snap.observed_at,
            snap.source_updated_at,
            snap.observation_id,
            json.dumps(snap.meta, ensure_ascii=False) if snap.meta else None,
            utc_now_iso(),
        ),
    )
    if cur.rowcount > 0 and cur.lastrowid:
        return int(cur.lastrowid)
    return None


def latest_odds_by_selection(
    conn: sqlite3.Connection,
    fixture_id: int,
    market_code: str,
    source: str,
) -> dict[str, tuple[float, str]]:
    """Latest snapshot per selection for one (fixture, market, source)."""
    rows = conn.execute(
        """
            SELECT selection_code, odds, captured_at FROM odds_snapshots
WHERE fixture_id
            = ? AND market_code = ? AND source = ?
ORDER BY captured_at, odds
        """,
        (fixture_id, market_code, source),
    ).fetchall()
    latest: dict[str, tuple[float, str]] = {}
    for row in rows:
        latest[str(row["selection_code"])] = (
            float(row["odds"]),
            str(row["captured_at"]),
        )
    return latest


def odds_history(
    conn: sqlite3.Connection,
    fixture_id: int,
    market_code: str,
    *,
    source: str | None = None,
) -> list[sqlite3.Row]:
    """Full append-only odds timeline for a fixture/market (票 19 验收)."""
    if source is not None:
        return conn.execute(
            """
                SELECT * FROM odds_snapshots WHERE fixture_id = ? AND market_code = ?
AND
                source = ? ORDER BY captured_at, id
            """,
            (fixture_id, market_code, source),
        ).fetchall()
    return conn.execute(
        """
            SELECT * FROM odds_snapshots WHERE fixture_id = ? AND market_code = ?
ORDER
            BY captured_at, id
        """,
        (fixture_id, market_code),
    ).fetchall()


def eu_book_odds(
    conn: sqlite3.Connection, fixture_id: int, market_code: str = "had"
) -> dict[str, dict[str, float]]:
    """
    Latest per-book odds for a fixture: ``selection -> {book: odds}``.

    Only ``odds_api:*`` sources are considered (欧洲多 bookmaker 共识输入)。
    """
    rows = conn.execute(
        """
            SELECT selection_code, source, odds, captured_at FROM odds_snapshots
WHERE
            fixture_id = ? AND market_code = ? AND source LIKE 'odds_api:%'
ORDER BY
            captured_at, odds
        """,
        (fixture_id, market_code),
    ).fetchall()
    latest: dict[str, dict[str, tuple[str, float]]] = {}
    for row in rows:
        sel = str(row["selection_code"])
        book = str(row["source"])
        latest.setdefault(sel, {})[book] = (str(row["captured_at"]), float(row["odds"]))
    return {
        sel: {book: captured[1] for book, captured in books.items()}
        for sel, books in latest.items()
    }


def fixtures_for_business_date(
    conn: sqlite3.Connection, business_date: str
) -> list[sqlite3.Row]:
    """All jingcai fixtures carrying a sales code on a business date."""
    return fixtures_for_business_dates(conn, [business_date])


def fixture_detail(conn: sqlite3.Connection, fixture_id: int) -> sqlite3.Row | None:
    """
    One fixture joined with sales code/competition/team names (研究页输入, 票 wb-02).

    与场次列表同构的对照行；无竞彩销售编号（非在售场次）返回 None。
    """
    return conn.execute(
        """
        SELECT f.*, mc.code AS match_code, mc.is_single, mc.business_date,
c.name AS
        competition_name, c.tier AS competition_tier,
ht.canonical_name AS home_team,
        at2.canonical_name AS away_team
FROM match_codes mc
JOIN fixtures f ON f.id
        = mc.fixture_id
JOIN competitions c ON c.id = f.competition_id
JOIN teams ht ON
        ht.id = f.home_team_id
JOIN teams at2 ON at2.id = f.away_team_id
WHERE
        mc.kind = 'jingcai' AND f.id = ?
ORDER BY mc.business_date DESC, mc.code
LIMIT 1
        """,
        (fixture_id,),
    ).fetchone()


def eu_book_quotes(
    conn: sqlite3.Connection, fixture_id: int, market_code: str = "had"
) -> dict[str, dict[str, tuple[float, str]]]:
    """
    Latest per-book quotes with capture time.

    ``book -> {selection: (odds, captured_at)}``。工作台 v2 票 02（研究页
    逐书赔率明细）；口径与 :func:`eu_book_odds` 一致，仅 ``odds_api:*`` 源
    参与， ``(fixture, market, book, selection)`` 取最新一条。
    """
    rows = conn.execute(
        """
            SELECT selection_code, source, odds, captured_at FROM odds_snapshots
WHERE
            fixture_id = ? AND market_code = ? AND source LIKE 'odds_api:%'
ORDER BY
            captured_at, odds
        """,
        (fixture_id, market_code),
    ).fetchall()
    latest: dict[str, dict[str, tuple[float, str]]] = {}
    for row in rows:
        book = str(row["source"])
        latest.setdefault(book, {})[str(row["selection_code"])] = (
            float(row["odds"]),
            str(row["captured_at"]),
        )
    return latest


def fixtures_for_business_dates(
    conn: sqlite3.Connection, business_dates: Sequence[str]
) -> list[sqlite3.Row]:
    """
    All jingcai fixtures carrying a sales code on any of the given dates.

    工作台 v2 票 01：场次列表 3 日化的多业务日查询；行内携带 ``mc.business_date``
    供调用方按日分组（单日查询同样带该列，响应形状一致）。
    """
    if not business_dates:
        return []
    placeholders = ", ".join("?" for _ in business_dates)
    return conn.execute(
        f"""
            SELECT f.*, mc.code AS match_code, mc.is_single, mc.source_match_id,
mc.business_date AS
            business_date, c.name
AS competition_name, c.tier AS competition_tier,
ht.canonical_name AS
            home_team, at2.canonical_name AS away_team
FROM match_codes mc
JOIN fixtures
            f ON f.id = mc.fixture_id
JOIN competitions c ON c.id = f.competition_id
JOIN teams ht ON ht.id = f.home_team_id
JOIN teams at2 ON at2.id =
            f.away_team_id
WHERE mc.kind = 'jingcai' AND mc.business_date IN ({placeholders})
ORDER BY
            f.kickoff_utc, mc.code
        """,  # noqa: S608
        tuple(business_dates),
    ).fetchall()


def set_odds_api_join(
    conn: sqlite3.Connection,
    fixture_id: int,
    event_id: str,
    sport_key: str,
    method: str,
) -> None:
    """Persist a sporttery↔Odds API fixture join (票 20 映射表)."""
    conn.execute(
        """
            UPDATE fixtures SET odds_api_event_id = ?, odds_api_sport_key = ?,
            join_method = ?, joined_at = ? WHERE id = ?
        """,
        (event_id, sport_key, method, utc_now_iso(), fixture_id),
    )


def unset_odds_api_join(conn: sqlite3.Connection, fixture_id: int) -> None:
    """Clear a stale join (re-join support)."""
    conn.execute(
        """
            UPDATE fixtures SET odds_api_event_id = NULL, odds_api_sport_key = NULL,
            join_method = NULL, joined_at = NULL WHERE id = ?
        """,
        (fixture_id,),
    )


def fixtures_for_events(
    conn: sqlite3.Connection, event_ids: list[str]
) -> list[sqlite3.Row]:
    """按 Odds API event id 批量取场次行（别名回填用）。"""
    if not event_ids:
        return []
    placeholders = ", ".join("?" for _ in event_ids)
    return conn.execute(
        f"""
        SELECT id, odds_api_event_id, home_team_id, away_team_id FROM fixtures
        WHERE odds_api_event_id IN ({placeholders})
        """,  # noqa: S608
        event_ids,
    ).fetchall()


def pending_jingcai_fixtures(
    conn: sqlite3.Connection, now_utc: str
) -> list[sqlite3.Row]:
    """Fixtures kicked off but without a draw result yet (结算扫描输入)."""
    return conn.execute(
        """
            SELECT f.* FROM fixtures f
WHERE f.kickoff_utc <= ?
AND f.id IN (SELECT
            fixture_id FROM match_codes WHERE kind = 'jingcai')
AND f.id NOT IN (SELECT
            fixture_id FROM draw_results)
ORDER BY f.kickoff_utc
        """,
        (now_utc,),
    ).fetchall()


def get_fixture(conn: sqlite3.Connection, fixture_id: int) -> sqlite3.Row | None:
    """Fetch one fixture row."""
    return conn.execute("SELECT * FROM fixtures WHERE id = ?", (fixture_id,)).fetchone()


def joined_fixtures_in_window(
    conn: sqlite3.Connection, start_utc: str, end_utc: str
) -> list[sqlite3.Row]:
    """已 join 且开赛时间落在 [start, end] 的场次（closing 前置检查，票 37）。"""
    return conn.execute(
        """
        SELECT * FROM fixtures
        WHERE odds_api_event_id IS NOT NULL
          AND kickoff_utc >= ? AND kickoff_utc <= ?
        ORDER BY kickoff_utc
        """,
        (start_utc, end_utc),
    ).fetchall()


def kickoffs_for_fixtures(
    conn: sqlite3.Connection, fixture_ids: list[int]
) -> dict[int, str]:
    """Bulk fetch kickoff_utc keyed by fixture id（复盘前瞻资格用，票 36）。"""
    if not fixture_ids:
        return {}
    placeholders = ", ".join("?" for _ in fixture_ids)
    rows = conn.execute(
        f"SELECT id, kickoff_utc FROM fixtures WHERE id IN ({placeholders})",  # noqa: S608
        fixture_ids,
    ).fetchall()
    return {int(row["id"]): str(row["kickoff_utc"]) for row in rows}
