"""
情报存证仓储（票 09）：intel_observations append-only 写入与读取。

表 SQL 归 llm 域（ADR-0008）；唯一键 (fixture_id, collector, raw_hash)
天然幂等——重复采集同内容零新行（票 09 验收）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from goalx_backend.db import utc_now_iso


def raw_hash(payload: dict[str, Any]) -> str:
    """原始素材规范化 JSON 的 sha256（键排序 + ensure_ascii，确定性哈希）。"""
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IntelDraft:
    """一条待存证情报（采集器产出；collected_at 为情报本身的采集时点）。"""

    kind: str
    text: str
    source: str
    collected_at: str
    collector: str
    raw_payload: dict[str, Any]


def insert_intel_observation(
    conn: sqlite3.Connection, fixture_id: int, draft: IntelDraft
) -> int | None:
    """追加一条情报观测；同内容已存在时静默跳过（返回 None）。"""
    digest = raw_hash(draft.raw_payload)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO intel_observations
            (fixture_id, kind, text, source, collected_at, collector,
             raw_payload, raw_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fixture_id,
            draft.kind,
            draft.text,
            draft.source,
            draft.collected_at,
            draft.collector,
            json.dumps(draft.raw_payload, ensure_ascii=False, sort_keys=True),
            digest,
            utc_now_iso(),
        ),
    )
    row_id = cursor.lastrowid
    return row_id if cursor.rowcount and row_id is not None else None


def intel_for_fixture(conn: sqlite3.Connection, fixture_id: int) -> list[sqlite3.Row]:
    """一场的全部已存证情报（按采集时点升序）。"""
    return conn.execute(
        """
        SELECT * FROM intel_observations WHERE fixture_id = ?
        ORDER BY collected_at, id
        """,
        (fixture_id,),
    ).fetchall()


def intel_summary_by_fixture(
    conn: sqlite3.Connection, fixture_ids: list[int]
) -> dict[int, list[tuple[str, str]]]:
    """多场次的 (source, collected_at) 列表（评测报告情报质量列用）。"""
    out: dict[int, list[tuple[str, str]]] = {}
    for fixture_id in fixture_ids:
        rows = conn.execute(
            """
            SELECT source, collected_at FROM intel_observations
            WHERE fixture_id = ?
            """,
            (fixture_id,),
        ).fetchall()
        out[fixture_id] = [(str(r["source"]), str(r["collected_at"])) for r in rows]
    return out
