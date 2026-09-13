"""
DrawResult 导入接口（票 23）：官方开奖是系统唯一事实源（ADR 0001）。

M1 的导入通道：API 手工/半自动导入（结构化 payload）。
sporttery getMatchResultV1.qry 网关当前被拒（研究 01 实测），
500.com 彩果页解析留待后续接入——本模块即其落库接口。
"""

from __future__ import annotations

import sqlite3

from goalx_backend.data import results as rs_store
from goalx_backend.db import atomic
from goalx_backend.models import DrawResultInput
from goalx_backend.services import (
    resettle_corrected_results,
    validate_correction_targets,
)


def import_draw_results(
    conn: sqlite3.Connection, results: list[DrawResultInput]
) -> int:
    """批量导入开奖并原子重算更正; 相同事实幂等, 返回输入数。"""
    with atomic(conn):
        before = int(
            conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM draw_result_revisions"
            ).fetchone()[0]
        )
        changed: set[int] = set()
        for result in results:
            previous = rs_store.get_draw_result(conn, result.fixture_id)
            values = result.model_dump(exclude={"correction_reason"})
            if previous is not None and any(
                previous[key] != value for key, value in values.items()
            ):
                changed.add(result.fixture_id)
        validate_correction_targets(conn, changed)
        for result in results:
            rs_store.upsert_draw_result(conn, result)
        revisions = conn.execute(
            "SELECT id, fixture_id FROM draw_result_revisions WHERE id > ?", (before,)
        ).fetchall()
        if revisions:
            resettle_corrected_results(
                conn,
                {int(row["fixture_id"]) for row in revisions},
                reason="draw_result_revisions:"
                + ",".join(str(row["id"]) for row in revisions),
            )
    return len(results)
