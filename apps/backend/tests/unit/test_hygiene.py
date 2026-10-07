"""训练集卫生谓词测试（backtest-decade A4/A5）。

PSC 成熟度边界（含端点/月末钳制/闰年）与行政判赛排除窗（双侧命中/
日期窗含端点/联赛不匹配不误伤）；排除表取数=迁移种子行 + 缺表 fail-closed。
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from goalx_backend.data import hygiene


def test_psc_mature_boundaries() -> None:
    """+6mo 当天可用（含端点）、前一天不可用；月末钳制与闰年各验一例。"""
    assert hygiene.psc_mature("2025-10-01", today=date(2026, 4, 1)) is True
    assert hygiene.psc_mature("2025-10-01", today=date(2026, 3, 31)) is False
    # 月末钳制：2024-08-31 + 6mo = 2025-02-28（非 03-03 也非 03-02）
    assert hygiene.psc_mature("2024-08-31", today=date(2025, 2, 28)) is True
    assert hygiene.psc_mature("2024-08-31", today=date(2025, 2, 27)) is False
    # 闰年：2023-08-31 + 6mo = 2024-02-29
    assert hygiene.psc_mature("2023-08-31", today=date(2024, 2, 29)) is True
    assert hygiene.psc_mature("2023-08-31", today=date(2024, 2, 28)) is False


def test_admin_exclusion_window_semantics() -> None:
    """队任一侧命中、日期窗含端点、联赛不匹配不误伤。"""
    exclusions = [
        hygiene.AdminExclusion(
            competition="T1",
            team="甲队",
            date_start="2023-02-06",
            date_end="2023-06-30",
            reason="测试",
        )
    ]
    assert hygiene.is_admin_excluded(
        "T1", "甲队", "客队", "2023-03-01", exclusions
    )  # 主侧命中
    assert hygiene.is_admin_excluded(
        "T1", "主队", "甲队", "2023-03-01", exclusions
    )  # 客侧命中
    assert hygiene.is_admin_excluded(
        "T1", "甲队", "客队", "2023-02-06", exclusions
    )  # 起点含端点
    assert hygiene.is_admin_excluded(
        "T1", "甲队", "客队", "2023-06-30", exclusions
    )  # 终点含端点
    assert not hygiene.is_admin_excluded(
        "T1", "甲队", "客队", "2023-02-05", exclusions
    )  # 窗前正常场
    assert not hygiene.is_admin_excluded(
        "T1", "甲队", "客队", "2023-07-01", exclusions
    )  # 窗后正常场
    assert not hygiene.is_admin_excluded(
        "E0", "甲队", "客队", "2023-03-01", exclusions
    )  # 同名队他联赛不连坐


def test_admin_exclusions_reads_migration_seed(db: sqlite3.Connection) -> None:
    """迁移种子行（土超三窗）就位且字段齐——真实排除面即刻生效。"""
    rows = hygiene.admin_exclusions(db)
    assert len(rows) == 3
    teams = {r.team for r in rows}
    assert teams == {"Hatayspor", "Gaziantep", "Ad. Demirspor"}
    assert all(r.competition == "T1" for r in rows)
    assert all(r.reason for r in rows)
    # 种子窗语义自检：2023 窗盖地震后退赛赛季尾、阿达纳窗=单场
    by_team = {r.team: r for r in rows}
    assert by_team["Hatayspor"].date_start == "2023-02-06"
    assert by_team["Ad. Demirspor"].date_end == by_team["Ad. Demirspor"].date_start


def test_admin_exclusions_missing_table_fails_closed() -> None:
    """未迁移库缺表 → 抛错（fail-closed），不静默当成零排除。"""
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError):
            hygiene.admin_exclusions(conn)
    finally:
        conn.close()
