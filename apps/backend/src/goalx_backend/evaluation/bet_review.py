"""
注级复盘资格（票 36 规则，票 08 自 api/bets.py 下沉）。

前瞻纳入/排除 5 分类与 closing 完整性是领域规则，归 evaluation——前瞻
验证表所在域，规则同源（validation 门的纳入规则在本包 forward_validation）。
收连接与注行进、视图出，不带 HTTP 概念。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from pydantic import BaseModel

from goalx_backend.data import fixtures as fx_store
from goalx_backend.evaluation import clv as clv_mod


class BetReviewView(BaseModel):
    """复盘资格摘要（票 36）：前瞻纳入/排除原因与 closing 完整性。"""

    locked_pre_kickoff: bool | None = None
    closing_present: bool | None = None
    # included | excluded_unlocked | excluded_post_kickoff
    # | live_separate | missing_closing | unknown
    forward: str


def _parse_ts(value: str) -> datetime:
    """ISO 串 → aware datetime（naive 按 UTC；仅复盘比较用）。"""
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def review_views(
    conn: sqlite3.Connection, rows: list[sqlite3.Row]
) -> dict[int, BetReviewView]:
    """注级复盘资格：服务器记录的锁定时点 vs 开赛；closing 完整性（票 36）。"""
    fixture_ids = sorted(
        {int(leg["fixture_id"]) for row in rows for leg in json.loads(row["legs"])}
    )
    kickoffs = fx_store.kickoffs_for_fixtures(conn, fixture_ids)
    closing = clv_mod.closing_leg_counts(conn)
    views: dict[int, BetReviewView] = {}
    for row in rows:
        bet_id = int(row["id"])
        leg_fixtures = [int(leg["fixture_id"]) for leg in json.loads(row["legs"])]
        leg_kickoffs = [kickoffs[fid] for fid in leg_fixtures if fid in kickoffs]
        locked_at = row["locked_at"]
        pre_kickoff = (
            all(_parse_ts(locked_at) < _parse_ts(k) for k in leg_kickoffs)
            if locked_at and leg_kickoffs
            else None
        )
        closing_present = (
            closing.get(bet_id, 0) >= len(leg_fixtures) if leg_fixtures else None
        )
        if not row["purchased"]:
            forward = "excluded_unlocked"
        elif locked_at is None:
            forward = "unknown"
        elif str(row["mode"]) == "live":
            forward = "live_separate"
        elif pre_kickoff is not True:
            forward = "excluded_post_kickoff"
        elif closing_present is not True:
            forward = "missing_closing"
        else:
            forward = "included"
        views[bet_id] = BetReviewView(
            locked_pre_kickoff=pre_kickoff,
            closing_present=closing_present,
            forward=forward,
        )
    return views
