"""预测域仓储：Forecast append-only 落库与查询（ADR 0001 哈希存证）。"""

from __future__ import annotations

import json
import sqlite3

from goalx_backend.db import utc_now_iso


def insert_forecast(
    conn: sqlite3.Connection,
    *,
    fixture_id: int,
    track: str,
    model_version: str,
    content_hash: str,
    payload: dict[str, object],
) -> int | None:
    """
    Append 一条 Forecast；同 (fixture_id, content_hash) 重复插入被吸收。

    返回新行 id；已存在返回 None（幂等重跑）。
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO forecasts
        (fixture_id, track, model_version, issued_at, content_hash, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            fixture_id,
            track,
            model_version,
            utc_now_iso(),
            content_hash,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    if cur.rowcount > 0 and cur.lastrowid:
        return int(cur.lastrowid)
    return None


def latest_forecasts_for_fixtures(
    conn: sqlite3.Connection, fixture_ids: list[int], *, track: str = "ml"
) -> dict[int, sqlite3.Row]:
    """每场最新一条 Forecast（按 issued_at, id 取最大）。"""
    if not fixture_ids:
        return {}
    placeholders = ", ".join("?" for _ in fixture_ids)
    rows = conn.execute(
        f"""
        SELECT f.* FROM forecasts f
        JOIN (SELECT fixture_id, MAX(id) AS max_id FROM forecasts
              WHERE track = ? AND fixture_id IN ({placeholders})
              GROUP BY fixture_id) latest ON latest.max_id = f.id
        """,  # noqa: S608
        (track, *fixture_ids),
    ).fetchall()
    return {int(row["fixture_id"]): row for row in rows}
