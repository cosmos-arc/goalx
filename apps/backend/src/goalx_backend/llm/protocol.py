"""M3 证伪控制事件（票 13）：开关关闭状态每日一记（cost_ledger 存档）。"""

from __future__ import annotations

import sqlite3

from goalx_backend.config import Settings
from goalx_backend.data.results import category_note_count, record_cost
from goalx_backend.db import utc_now_iso

_CATEGORY = "m3_control"


def record_control_events(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """关闭的开关每日记一行处置事件（同 note 24h 内不重复）。"""
    events: list[str] = []
    disabled: list[str] = []
    if not settings.m3_analyst_enabled:
        disabled.append("analyst_disabled")
    if not settings.m3_fusion_enabled:
        disabled.append("fused_disabled")
    for note in disabled:
        if not category_note_count(conn, _CATEGORY, note, _day_start()):
            record_cost(conn, _CATEGORY, units=0.0, amount_cny=0.0, note=note)
            events.append(note)
    return events


def _day_start() -> str:
    return utc_now_iso()[:11] + "00:00:00+00:00"
