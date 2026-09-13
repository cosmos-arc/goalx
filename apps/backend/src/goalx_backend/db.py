"""SQLite 连接管理与迁移执行器（ADR 0003：单机 WAL + 外键）。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from goalx_backend.config import get_settings
from goalx_backend.migrations import MIGRATIONS


def utc_now_iso() -> str:
    """Current UTC time as a second-precision ISO-8601 string."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: str | Path = "", *, readonly: bool = False) -> sqlite3.Connection:
    """
    Open a goalx database connection with WAL and foreign keys on.

    readonly 以 URI 模式打开（不建目录、不写 WAL），调用方需已 migrate。
    """
    resolved = Path(db_path) if db_path else get_settings().db_path
    # check_same_thread=False：连接按请求/任务独占，ASGI 测试线程与
    # Prefect worker 都可能跨线程使用自己的连接（WAL 支持单写多读）。
    if readonly:
        uri = f"file:{resolved}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    else:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(resolved, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    """Latest applied schema version (0 on a fresh database)."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    applied = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
    )
    return int(applied.fetchone()["v"])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in order; return the resulting version."""
    conn.execute(
        """
            CREATE TABLE IF NOT EXISTS schema_migrations (
version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL)
        """
    )
    applied = {
        int(row["version"])
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version, apply_fn in MIGRATIONS:
        if version in applied:
            continue
        apply_fn(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, utc_now_iso()),
        )
        conn.commit()
    return current_version(conn)
