"""
十年回测语料报表（票 46）：完整性 + 交叉验证汇总（只读、不落事实）。

- ``completeness_report``：逐联赛×赛季行数与 PSC/AvgC 收盘缺口——
  语料缺口的现态快照（哪些季哪些联赛没有收盘基准）。联赛/赛季清单由
  调用方传入（正典在 ingest/fdhist；evaluation 不上行依赖 ingest 层）。
- 用途边界（票面）：语料只做结构回测（公平概率 = Pinnacle 收盘 Shin），
  不做结算事实源；远期竞彩赔率无存档处以欧赔代理口径诚实标注。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from goalx_backend.data import results as rs_store


def completeness_report(
    conn: sqlite3.Connection,
    *,
    competitions: tuple[str, ...],
    seasons: tuple[str, ...],
) -> dict[str, Any]:
    """Fdhist 语料完整性：每联赛每季行数/收盘缺口；缺季即缺口（显式列出）。"""
    coverage = {
        (str(row["competition"]), str(row["season"])): row
        for row in rs_store.hist_season_coverage(conn, competitions)
    }
    per_competition: dict[str, dict[str, dict[str, int]]] = {}
    total_rows = 0
    for competition in competitions:
        entry: dict[str, dict[str, int]] = {}
        for season in seasons:
            row = coverage.get((competition, season))
            if row is None:
                entry[season] = {"rows": 0}
                continue
            total_rows += int(row["rows"])
            entry[season] = {
                "rows": int(row["rows"]),
                "psc_missing": int(row["psc_missing"]),
                "avgc_missing": int(row["avgc_missing"]),
            }
        per_competition[competition] = entry
    return {
        "seasons": list(seasons),
        "total_rows": total_rows,
        "per_competition": per_competition,
    }
