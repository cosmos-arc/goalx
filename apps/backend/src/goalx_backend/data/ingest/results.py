"""
DrawResult 导入接口（票 23）：官方开奖是系统唯一事实源（ADR 0001）。

三个上游共用本落库接口：API 手工导入（M1 起）、官方 uniform 自动同步
（票 44 切换后事实源，data/ingest/uniform）、更正冲正（betting 域经
validate_correction_targets 复用同一原子语义）。
"""

from __future__ import annotations

import sqlite3

from goalx_backend.betting.settle import (
    resettle_corrected_results,
    validate_correction_targets,
)
from goalx_backend.data import results as rs_store
from goalx_backend.db import atomic
from goalx_backend.models import DrawResultInput


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
