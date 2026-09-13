"""SQLite schema 迁移：v1 建 CONTEXT.md 六域全部实体表并播种参考数据。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

MigrationFn = Callable[[sqlite3.Connection], None]

_V1_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS competitions (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        tier TEXT NOT NULL DEFAULT 'tier2'
            CHECK (tier IN ('tier1', 'tier2', 'excluded')),
        odds_api_sport_key TEXT,
        api_football_league_id INTEGER,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS teams (
        id INTEGER PRIMARY KEY,
        canonical_name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS team_aliases (
        id INTEGER PRIMARY KEY,
        team_id INTEGER NOT NULL REFERENCES teams(id),
        source TEXT NOT NULL,
        alias TEXT NOT NULL,
        UNIQUE (source, alias)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fixtures (
        id INTEGER PRIMARY KEY,
        competition_id INTEGER NOT NULL REFERENCES competitions(id),
        kickoff_utc TEXT NOT NULL,
        home_team_id INTEGER NOT NULL REFERENCES teams(id),
        away_team_id INTEGER NOT NULL REFERENCES teams(id),
        stage TEXT,
        odds_api_event_id TEXT,
        odds_api_sport_key TEXT,
        join_method TEXT
            CHECK (join_method IN ('time_window', 'manual') OR join_method IS NULL),
        joined_at TEXT,
        UNIQUE (competition_id, kickoff_utc, home_team_id, away_team_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fixtures_kickoff ON fixtures(kickoff_utc)",
    """
    CREATE TABLE IF NOT EXISTS match_codes (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        kind TEXT NOT NULL CHECK (kind IN ('jingcai', 'pool')),
        business_date TEXT NOT NULL,
        code TEXT NOT NULL,
        source_match_id TEXT,
        is_single INTEGER,
        UNIQUE (kind, business_date, code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_match_codes_fixture ON match_codes(fixture_id)",
    """
    CREATE TABLE IF NOT EXISTS markets (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('fixed', 'pool'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS selections (
        id INTEGER PRIMARY KEY,
        market_code TEXT NOT NULL REFERENCES markets(code),
        code TEXT NOT NULL,
        label TEXT NOT NULL,
        UNIQUE (market_code, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS odds_snapshots (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        market_code TEXT NOT NULL,
        selection_code TEXT NOT NULL,
        source TEXT NOT NULL,
        purpose TEXT NOT NULL DEFAULT 'live_capture'
            CHECK (purpose IN ('live_capture', 'closing', 'backtest')),
        odds REAL NOT NULL CHECK (odds > 0),
        captured_at TEXT NOT NULL,
        meta TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (fixture_id, market_code, selection_code, source, captured_at, odds),
        FOREIGN KEY (market_code, selection_code)
            REFERENCES selections(market_code, code)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
        ON odds_snapshots(fixture_id, market_code, source, captured_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS pool_periods (
        id INTEGER PRIMARY KEY,
        market_code TEXT NOT NULL REFERENCES markets(code),
        period_no TEXT NOT NULL,
        sales_deadline TEXT,
        UNIQUE (market_code, period_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pool_states (
        pool_period_id INTEGER PRIMARY KEY REFERENCES pool_periods(id),
        sales_amount REAL,
        rollover_in REAL,
        prize_tiers TEXT,
        published_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS public_shares (
        id INTEGER PRIMARY KEY,
        pool_period_id INTEGER NOT NULL REFERENCES pool_periods(id),
        match_seq INTEGER NOT NULL,
        selection_code TEXT NOT NULL,
        share REAL NOT NULL,
        origin TEXT NOT NULL CHECK (origin IN ('estimated', 'published')),
        source TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        UNIQUE (pool_period_id, match_seq, selection_code, origin, source, captured_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS forecasts (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        track TEXT NOT NULL CHECK (track IN ('ml', 'llm', 'fused')),
        model_version TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        payload TEXT NOT NULL,
        UNIQUE (fixture_id, content_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS match_intels (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        agent TEXT NOT NULL,
        created_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS divergences (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        metric TEXT NOT NULL,
        reference TEXT NOT NULL,
        value REAL NOT NULL,
        computed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ev_assessments (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER REFERENCES fixtures(id),
        pool_period_id INTEGER REFERENCES pool_periods(id),
        market_code TEXT NOT NULL,
        selection_code TEXT NOT NULL,
        ev REAL NOT NULL,
        ci_low REAL,
        ci_high REAL,
        cost_adjusted_ev REAL,
        kelly_fraction REAL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bet_slips (
        id INTEGER PRIMARY KEY,
        mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
        source TEXT NOT NULL DEFAULT 'manual',
        pool_period_id INTEGER REFERENCES pool_periods(id),
        placed_at TEXT,
        note TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY,
        slip_id INTEGER REFERENCES bet_slips(id),
        mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
        market_kind TEXT NOT NULL CHECK (market_kind IN ('fixed', 'pool')),
        purchased INTEGER NOT NULL DEFAULT 0,
        stake REAL NOT NULL CHECK (stake >= 0),
        placed_at TEXT,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'won', 'lost', 'void', 'partial')),
        payout REAL,
        profit REAL,
        settled_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bet_legs (
        id INTEGER PRIMARY KEY,
        bet_id INTEGER NOT NULL REFERENCES bets(id),
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        market_code TEXT NOT NULL,
        selection_code TEXT NOT NULL,
        locked_odds REAL NOT NULL CHECK (locked_odds > 0),
        snapshot_id INTEGER REFERENCES odds_snapshots(id),
        meta TEXT,
        FOREIGN KEY (market_code, selection_code)
            REFERENCES selections(market_code, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pool_picks (
        id INTEGER PRIMARY KEY,
        slip_id INTEGER NOT NULL REFERENCES bet_slips(id),
        match_seq INTEGER NOT NULL,
        fixture_id INTEGER REFERENCES fixtures(id),
        selection_code TEXT NOT NULL,
        UNIQUE (slip_id, match_seq, selection_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS combinations (
        id INTEGER PRIMARY KEY,
        slip_id INTEGER NOT NULL REFERENCES bet_slips(id),
        seq INTEGER NOT NULL,
        stake REAL NOT NULL,
        selections TEXT NOT NULL,
        hit INTEGER,
        payout REAL,
        UNIQUE (slip_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cost_ledger (
        id INTEGER PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        category TEXT NOT NULL,
        units REAL NOT NULL DEFAULT 1,
        amount_cny REAL NOT NULL DEFAULT 0,
        note TEXT,
        meta TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS draw_results (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER NOT NULL UNIQUE REFERENCES fixtures(id),
        home_goals INTEGER NOT NULL CHECK (home_goals >= 0),
        away_goals INTEGER NOT NULL CHECK (away_goals >= 0),
        half_home_goals INTEGER,
        half_away_goals INTEGER,
        void INTEGER NOT NULL DEFAULT 0,
        void_reason TEXT,
        source TEXT NOT NULL,
        published_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settlements (
        id INTEGER PRIMARY KEY,
        bet_id INTEGER UNIQUE REFERENCES bets(id),
        slip_id INTEGER UNIQUE REFERENCES bet_slips(id),
        status TEXT NOT NULL CHECK (status IN ('won', 'lost', 'void', 'partial')),
        stake REAL NOT NULL,
        payout REAL NOT NULL,
        profit REAL NOT NULL,
        detail TEXT NOT NULL,
        computed_at TEXT NOT NULL,
        CHECK ((bet_id IS NULL) <> (slip_id IS NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bankroll_events (
        id INTEGER PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        kind TEXT NOT NULL
            CHECK (kind IN ('deposit', 'withdraw', 'bet_stake', 'bet_payout', 'cost')),
        amount_cny REAL NOT NULL,
        balance_after REAL NOT NULL,
        bet_id INTEGER REFERENCES bets(id),
        slip_id INTEGER REFERENCES bet_slips(id),
        note TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hist_matches (
        id INTEGER PRIMARY KEY,
        competition TEXT NOT NULL,
        season TEXT NOT NULL,
        match_date TEXT NOT NULL,
        home_team TEXT NOT NULL,
        away_team TEXT NOT NULL,
        fthg INTEGER NOT NULL,
        ftag INTEGER NOT NULL,
        ftr TEXT NOT NULL CHECK (ftr IN ('H', 'D', 'A')),
        psc_home REAL,
        psc_draw REAL,
        psc_away REAL,
        avgc_home REAL,
        avgc_draw REAL,
        avgc_away REAL,
        UNIQUE (competition, season, match_date, home_team, away_team)
    )
    """,
)

# 竞彩比分（crs/goals4）精确比分集合（28 个，取自官方网关实际盘口）——
# 不在集合内的高比分落入“其他”档。
CRS_AWAY_RANGE: tuple[tuple[int, int], ...] = (
    (0, 5),
    (1, 5),
    (2, 5),
    (3, 3),
    (4, 2),
    (5, 2),
)
CRS_EXACT_SCORES: tuple[tuple[int, int], ...] = tuple(
    (home, away) for home, max_away in CRS_AWAY_RANGE for away in range(max_away + 1)
)


def _selection_rows() -> list[tuple[str, str, str]]:
    """Build the reference selection seed (market_code, code, label)."""
    rows: list[tuple[str, str, str]] = [
        ("had", "h", "主胜"),
        ("had", "d", "平"),
        ("had", "a", "客胜"),
        ("hhad", "h", "让球主胜"),
        ("hhad", "d", "让球平"),
        ("hhad", "a", "让球客胜"),
    ]
    crs_rows: list[tuple[str, str, str]] = [
        ("crs", f"{home}:{away}", f"比分 {home}:{away}")
        for home, away in CRS_EXACT_SCORES
    ] + [
        ("crs", "h_other", "胜其他"),
        ("crs", "d_other", "平其他"),
        ("crs", "a_other", "负其他"),
    ]
    rows += crs_rows
    ttg_labels = ["0 球", "1 球", "2 球", "3 球", "4 球", "5 球", "6 球", "7+ 球"]
    rows += [("ttg", str(n), label) for n, label in enumerate(ttg_labels)]
    hafu_rows = [
        ("hafu", code, label)
        for code, label in {
            "hh": "胜胜",
            "hd": "胜平",
            "ha": "胜负",
            "dh": "平胜",
            "dd": "平平",
            "da": "平负",
            "ah": "负胜",
            "ad": "负平",
            "aa": "负负",
        }.items()
    ]
    rows += hafu_rows
    for market in ("ttt14", "pick9"):
        rows += [
            (market, "3", "胜"),
            (market, "1", "平"),
            (market, "0", "负"),
        ]
    rows += [("goals4", code, label) for _, code, label in crs_rows]
    rows += [("htft6", code, label) for _, code, label in hafu_rows]
    return rows


def _apply_v1(conn: sqlite3.Connection) -> None:
    """Create the v1 domain schema and seed reference data."""
    for statement in _V1_STATEMENTS:
        conn.execute(statement)
    conn.executemany(
        "INSERT OR IGNORE INTO markets (code, name, kind) VALUES (?, ?, ?)",
        [
            ("had", "胜平负", "fixed"),
            ("hhad", "让球胜平负", "fixed"),
            ("crs", "比分", "fixed"),
            ("ttg", "总进球", "fixed"),
            ("hafu", "半全场", "fixed"),
            ("ttt14", "胜负彩十四场", "pool"),
            ("pick9", "任选九", "pool"),
            ("goals4", "四场进球", "pool"),
            ("htft6", "六场半全场", "pool"),
        ],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO selections (market_code, code, label) VALUES (?, ?, ?)",
        _selection_rows(),
    )


_APPEND_ONLY_TABLES = ("odds_snapshots", "forecasts")


def _apply_v2(conn: sqlite3.Connection) -> None:
    """v2：append-only 触发器（ADR 0001）——禁止修改/删除时点快照。"""
    for table in _APPEND_ONLY_TABLES:
        for action in ("UPDATE", "DELETE"):
            sql = (
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                f"BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
            )
            conn.execute(sql)


MIGRATIONS: tuple[tuple[int, MigrationFn], ...] = ((1, _apply_v1), (2, _apply_v2))
