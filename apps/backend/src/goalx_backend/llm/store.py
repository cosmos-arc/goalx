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
from datetime import UTC, datetime
from typing import Any

from goalx_backend.db import utc_now_iso


def normalize_collected_at(value: str) -> str:
    """
    采集时点规范化为秒精度 UTC（票 04 I1 + 票 10②）。

    naive 视为 UTC；带偏移输入换算 UTC 后落库（原 isoformat() 保留偏移，
    `12:30-04:00` 字典序穿透 as_of 边界——实为 16:30 UTC）；非 ISO
    fail-loud 拒绝。+00:00 统一字面是 collected_at <= as_of 谓词按
    字典序比较的前提。
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


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
    """
    追加一条情报观测；同内容已存在时静默跳过（返回 None）。

    collected_at 写入前规范化为秒精度 UTC（normalize_collected_at，
    票 04 I1 + 票 10②：带偏移输入换算不穿透 as_of 边界）。
    """
    normalized = normalize_collected_at(draft.collected_at)
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
            normalized,
            draft.collector,
            json.dumps(draft.raw_payload, ensure_ascii=False, sort_keys=True),
            digest,
            utc_now_iso(),
        ),
    )
    row_id = cursor.lastrowid
    return row_id if cursor.rowcount and row_id is not None else None


def intel_for_fixture(
    conn: sqlite3.Connection, fixture_id: int, *, as_of: str | None = None
) -> list[sqlite3.Row]:
    """
    一场的已存证情报（按采集时点升序）。

    as_of = 时点界（collected_at <= as_of，票 04 决策时刻语义）；不传 =
    全量——实时面现行为零改动。回放链路传各自 moment 即读不到未来情报。
    """
    return conn.execute(
        """
        SELECT * FROM intel_observations
        WHERE fixture_id = ? AND (? IS NULL OR collected_at <= ?)
        ORDER BY collected_at, id
        """,
        (fixture_id, as_of, as_of),
    ).fetchall()


def intel_summary_by_fixture(
    conn: sqlite3.Connection, fixture_ids: list[int], *, as_of: str | None = None
) -> dict[int, list[tuple[str, str]]]:
    """多场次的 (source, collected_at) 列表（评测报告情报质量列用；as_of 同上）。"""
    out: dict[int, list[tuple[str, str]]] = {}
    for fixture_id in fixture_ids:
        rows = conn.execute(
            """
            SELECT source, collected_at FROM intel_observations
            WHERE fixture_id = ? AND (? IS NULL OR collected_at <= ?)
            """,
            (fixture_id, as_of, as_of),
        ).fetchall()
        out[fixture_id] = [(str(r["source"]), str(r["collected_at"])) for r in rows]
    return out
