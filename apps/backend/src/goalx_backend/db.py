"""SQLite 连接管理与迁移执行器（ADR 0003：单机 WAL + 外键）。"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from goalx_backend.config import get_settings
from goalx_backend.migrations import MIGRATIONS, RESEARCH_MIGRATIONS, MigrationFn


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


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """表存在探测（sqlite_master 系统目录查询归 infra，票 07②）。"""
    hit = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return hit is not None


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
    return _apply_chain(conn, MIGRATIONS)


def migrate_research(conn: sqlite3.Connection) -> int:
    """
    研究库迁移（boundary-hardening 票 07②）：连接时即席执行。

    链内只有幂等 CREATE IF NOT EXISTS——研究库无部署方迁移步骤，免一个
    运维前置（运行面库迁移由部署方/CLI 执行的既有约定不变）。
    """
    return _apply_chain(conn, RESEARCH_MIGRATIONS)


def _apply_chain(
    conn: sqlite3.Connection, chain: tuple[tuple[int, MigrationFn], ...]
) -> int:
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
    for version, apply_fn in chain:
        if version in applied:
            continue
        # 并发迁移竞态（审计批C #11）：applied 集在事务外读——定拍多任务同
        # 分钟各自 migrate 时双双过检、都执行非幂等 ALTER，输家 duplicate
        # column 崩一次。每版本 check+apply+记账包 BEGIN IMMEDIATE（atomic
        # 先例），写锁内复查未记账才施用；applied 集只是免开写事务的快道。
        with atomic(conn):
            done = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if done is not None:
                continue
            apply_fn(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at)"  # noqa: S608
                + " VALUES (?, ?)",
                (version, utc_now_iso()),
            )
    return current_version(conn)


def connect_research(db_path: str | Path = "") -> sqlite3.Connection:
    """
    打开研究库连接（WAL + 自动迁移，票 07②）。

    全新库并发首开（如前端首屏两个请求各开一连接）时，journal_mode
    切换可能瞬时报 database is locked（correctness F2）——短退避重试，
    另一连接通常已把模式置为 WAL；迁移记账 OR IGNORE + CREATE 幂等使
    双开最终一致。
    """
    resolved = Path(db_path) if db_path else get_settings().research_db_path
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _wal_attempts = 3
    for attempt in range(_wal_attempts):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError:
            if attempt == _wal_attempts - 1:
                logger.warning("研究库 WAL 设置让位（并发首开）：{}", resolved)
            time.sleep(0.1)
    conn.execute("PRAGMA foreign_keys=ON")
    migrate_research(conn)
    return conn


@contextmanager
def atomic(conn: sqlite3.Connection) -> Generator[None]:
    """Serialize read/write decisions and roll back the entire operation on failure."""
    nested = conn.in_transaction
    conn.execute("SAVEPOINT lifecycle" if nested else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            conn.execute("ROLLBACK TO lifecycle")
            conn.execute("RELEASE lifecycle")
        else:
            conn.rollback()
        raise
    else:
        if nested:
            conn.execute("RELEASE lifecycle")
        else:
            conn.commit()


def table_sql(conn: sqlite3.Connection, name: str) -> str | None:
    """系统目录里的建表原文（不存在=None）。目录读取归 infra 本职。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return str(row[0]) if row is not None else None


def trigger_sqls(conn: sqlite3.Connection, names: tuple[str, ...]) -> dict[str, str]:
    """按名取触发器建文原文（供一次性修正摘防线后按原文重建）。"""
    originals: dict[str, str] = {}
    for name in names:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name = ?",
            (name,),
        ).fetchone()
        if row is not None:
            originals[name] = str(row[0])
    return originals
