"""
DrawResult 导入接口（票 23）：官方开奖是系统唯一事实源（ADR 0001）。

M1 的导入通道：API 手工/半自动导入（结构化 payload）。
sporttery getMatchResultV1.qry 网关当前被拒（研究 01 实测），
500.com 彩果页解析留待后续接入——本模块即其落库接口。
"""

from __future__ import annotations

import sqlite3

from goalx_backend.models import DrawResultInput
from goalx_backend.store import results as rs_store


def import_draw_results(
    conn: sqlite3.Connection, results: list[DrawResultInput]
) -> int:
    """批量导入开奖结果（幂等，修正可覆盖）；返回导入数。"""
    for result in results:
        rs_store.upsert_draw_result(conn, result)
    conn.commit()
    return len(results)
