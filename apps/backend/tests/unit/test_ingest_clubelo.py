"""clubelo Elo 层采集测试（票 74）。"""

from __future__ import annotations

import sqlite3
from datetime import date

import httpx
import pytest

from goalx_backend.data.ingest import clubelo

SNAPSHOT_CSV = (
    "Club,Country,Level,Elo,From,To\n"
    "Arsenal,England,1,2012.3,2026-09-20,2026-09-25\n"
    "Bayern Munich,Germany,1,2025.9,2026-09-21,2026-09-25\n"
)
HISTORY_CSV = (
    "Club,Country,Level,Elo,From,To\n"
    "Arsenal,England,1,1890.1,2026-09-01,2026-09-19\n"
    "Arsenal,England,1,2012.3,2026-09-20,\n"
    ",England,1,1700.0,2026-09-20,2026-09-25\n"  # 缺 Club → 跳过
    "Bayern Munich,Germany,1,not-a-number,2026-09-20,\n"  # 缺 Elo → 跳过
)


def test_parse_ratings_maps_and_cleans() -> None:
    rows = clubelo.parse_ratings(HISTORY_CSV)
    assert len(rows) == 2  # 缺 Club / 坏 Elo 各跳一行
    first = rows[0]
    assert first["club"] == "Arsenal"
    assert first["country"] == "England"
    assert first["level"] == 1
    assert first["elo"] == 1890.1
    assert first["valid_from"] == "2026-09-01"
    assert first["valid_to"] == "2026-09-19"
    assert rows[1]["valid_to"] is None  # To 空容忍


def test_sync_snapshot_upserts_idempotently(db) -> None:
    day = date(2026, 9, 25)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SNAPSHOT_CSV)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    first = clubelo.sync_snapshot(db, client, day)
    assert first.snapshot_rows == 2
    assert first.written == 2
    second = clubelo.sync_snapshot(db, client, day)  # 幂等重跑
    assert second.snapshot_rows == 2
    assert second.written == 2  # upsert 冲突更新（区间延长），不翻倍
    rows = db.execute("SELECT COUNT(*) AS n FROM elo_ratings").fetchone()
    assert rows["n"] == 2


def test_upsert_extends_valid_to_on_conflict(db) -> None:
    rows = clubelo.parse_ratings(
        "Club,Country,Level,Elo,From,To\n"
        "Arsenal,England,1,2012.3,2026-09-20,2026-09-24\n"
    )
    clubelo.upsert_ratings(db, rows)
    rows[0]["valid_to"] = "2026-09-26"  # 日拍重见同区间，右端延长
    clubelo.upsert_ratings(db, rows)
    row = db.execute(
        "SELECT elo, valid_to FROM elo_ratings WHERE club = 'Arsenal'"
    ).fetchone()
    assert row["valid_to"] == "2026-09-26"
    total = db.execute("SELECT COUNT(*) AS n FROM elo_ratings").fetchone()
    assert total["n"] == 1


def test_backfill_history_walks_clubs(db) -> None:
    """回填 = 当日快照建清单 → 逐队全历史；单队失败跳过不中断。"""
    day = date(2026, 9, 25)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url.endswith("/2026-09-25"):
            return httpx.Response(200, text=SNAPSHOT_CSV)
        if url.endswith("/Arsenal"):
            return httpx.Response(200, text=HISTORY_CSV)
        raise httpx.ConnectError("boom")  # Bayern 拉取失败

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = clubelo.backfill_history(db, client, day)
    assert stats.failed_clubs == ["Bayern Munich"]
    assert stats.history_rows == 2
    row = db.execute(
        "SELECT COUNT(*) AS n FROM elo_ratings WHERE club = 'Arsenal'"
    ).fetchone()
    assert row["n"] == 2  # 快照行 + 历史区间行
    assert any(url.endswith("/Arsenal") for url in calls)


def test_sync_snapshot_raises_on_site_down() -> None:
    """站点不可达（2026-09-26 探针现实）→ 异常上抛由调用方处置。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("502 from proxy")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.ConnectError):
        clubelo.sync_snapshot(
            sqlite3.connect(":memory:"),
            client,
            date(2026, 9, 25),
        )
