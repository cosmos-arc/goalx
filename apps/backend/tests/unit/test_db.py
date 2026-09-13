"""db.connect/migrate 行为测试。"""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend import db


def test_migrate_applies_v1_and_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert db.current_version(conn) == 0
    assert db.migrate(conn) == 2
    assert db.migrate(conn) == 2  # 重跑幂等

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    # 六域核心表（票 18 验收：全部实体建表）
    expected = {
        "competitions",
        "teams",
        "team_aliases",
        "fixtures",
        "match_codes",
        "markets",
        "selections",
        "odds_snapshots",
        "pool_periods",
        "pool_states",
        "public_shares",
        "forecasts",
        "match_intels",
        "divergences",
        "ev_assessments",
        "bet_slips",
        "bets",
        "bet_legs",
        "pool_picks",
        "combinations",
        "cost_ledger",
        "draw_results",
        "settlements",
        "bankroll_events",
        "hist_matches",
    }
    assert expected <= tables

    markets = {row["code"] for row in conn.execute("SELECT code FROM markets")}
    assert markets == {
        "had",
        "hhad",
        "crs",
        "ttg",
        "hafu",
        "ttt14",
        "pick9",
        "goals4",
        "htft6",
    }
    crs = conn.execute(
        "SELECT COUNT(*) AS n FROM selections WHERE market_code = 'crs'"
    ).fetchone()["n"]
    assert crs == 31  # 28 精确比分 + 三档「其他」
    conn.close()


def test_connect_sets_pragmas(tmp_path) -> None:
    conn = db.connect(tmp_path / "goalx.db")
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
    conn.close()


def test_connect_readonly(tmp_path) -> None:
    path = tmp_path / "ro.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    ro = db.connect(path, readonly=True)
    row = ro.execute("SELECT COUNT(*) AS n FROM markets").fetchone()
    assert row["n"] == 9
    ro.close()


def test_append_only_triggers_block_mutation() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    conn.execute(
        """
        INSERT INTO competitions (name, created_at) VALUES ('x', 't')
        """
    )
    conn.execute(
        """
        INSERT INTO teams (canonical_name, created_at) VALUES ('a', 't')
        """
    )
    conn.execute(
        """
        INSERT INTO teams (canonical_name, created_at) VALUES ('b', 't')
        """
    )
    conn.execute(
        """
        INSERT INTO fixtures
        (competition_id, kickoff_utc, home_team_id, away_team_id)
        VALUES (1, '2026-01-01', 1, 2)
        """
    )
    conn.execute(
        """
        INSERT INTO odds_snapshots
        (fixture_id, market_code, selection_code, source, odds, captured_at, created_at)
        VALUES (1, 'had', 'h', 'sporttery', 2.0, 't', 'x')
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE odds_snapshots SET odds = 3.0")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM odds_snapshots")
    conn.close()
