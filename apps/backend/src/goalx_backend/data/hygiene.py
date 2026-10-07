"""
训练集卫生谓词（backtest-decade A4/A5，spec User Story 4/5）。

- 行政判赛排除表（A4）：运行面显式表 ``admin_match_exclusions``
  （联赛×队×日期窗×缘由，续添只 INSERT）——这些场"比分是真的、比赛是
  假的"（行政判 3-0，从未真踢）。判门①对账集已接线；gold 训练行/引擎
  训练集（B 相/票 14）接线时也必须从本模块引，不各自散写 SQL。
- PSC 成熟度（A5 硬规则）：开球后不足 6 个月的 fdhist 行 PSC/PSH/AvgC
  视为暂定（源回填滞后约 5 个月，未收敛数值污染 fair 基准）——fair
  基准与判门②共用同一谓词，不各写一份。
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass
from datetime import date

PSC_MATURITY_MONTHS = 6


@dataclass(frozen=True)
class AdminExclusion:
    """一行行政判赛排除窗（队任一侧命中即剔除，日期窗含端点）。"""

    competition: str
    team: str
    date_start: str
    date_end: str
    reason: str


def _plus_months(day: date, months: int) -> date:
    """日历月加法，日溢出钳到目标月末日（2024-08-31 + 6mo = 2025-02-28）。"""
    total = day.month - 1 + months
    year = day.year + total // 12
    month = total % 12 + 1
    return day.replace(
        year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1])
    )


def psc_mature(match_date: str, *, today: date) -> bool:
    """PSC 成熟度谓词：开球日 + 6 个月 ≤ today 才可用。"""
    return _plus_months(date.fromisoformat(match_date), PSC_MATURITY_MONTHS) <= today


def admin_exclusions(conn: sqlite3.Connection) -> list[AdminExclusion]:
    """排除表全量行（运行面 sqlite；表缺席=未迁移，抛错优于静默跳过）。"""
    rows = conn.execute(
        """
        SELECT competition, team, date_start, date_end, reason
        FROM admin_match_exclusions
        ORDER BY competition, team, date_start
        """
    ).fetchall()
    return [
        AdminExclusion(
            competition=str(r["competition"]),
            team=str(r["team"]),
            date_start=str(r["date_start"]),
            date_end=str(r["date_end"]),
            reason=str(r["reason"]),
        )
        for r in rows
    ]


def is_admin_excluded(
    competition: str,
    home: str,
    away: str,
    match_date: str,
    exclusions: list[AdminExclusion],
) -> bool:
    """一场 fdhist 行是否落在任一排除窗（联赛精确匹配 + 队任一侧 + 日期窗含端点）。"""
    return any(
        ex.competition == competition
        and ex.team in (home, away)
        and ex.date_start <= match_date <= ex.date_end
        for ex in exclusions
    )
