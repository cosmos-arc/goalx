"""API 共享依赖：请求级 SQLite 连接（迁移由部署方/CLI 执行）。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Request

from goalx_backend.config import Settings, get_settings
from goalx_backend.db import connect


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Open a per-request connection (WAL 支持并发读写)。"""
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()
