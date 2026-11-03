"""API 共享依赖：请求级 SQLite 连接（迁移由部署方/CLI 执行）。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Request

from goalx_backend.config import Settings, get_settings
from goalx_backend.db import connect, connect_research


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Open a per-request connection (WAL 支持并发读写)。"""
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_research_db(request: Request) -> Iterator[sqlite3.Connection]:
    """
    请求级研究库连接（票 07②）：backtest 表读面。

    与运行面库分文件——研究长跑与运营写入互不占写锁。
    """
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    conn = connect_research(settings.research_db_path)
    try:
        yield conn
    finally:
        conn.close()
