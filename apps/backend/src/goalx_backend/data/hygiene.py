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
import re
import sqlite3
from dataclasses import dataclass
from datetime import date

PSC_MATURITY_MONTHS = 6
# 排除窗日期=字典序区间比较的前提：严格零填充 YYYY-MM-DD（缺零填充如
# '2023-2-6' 会让比较静默失效——append-only 触发器下纠错昂贵，取数口
# fail-closed 拒收）
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def _checked_day(value: str, team: str, field: str) -> str:
    """严格校验排除窗日期（零填充 YYYY-MM-DD 且真实存在），坏值即抛。"""
    if not _ISO_DAY.match(value):
        raise ValueError(
            f"admin_match_exclusions.{field}={value!r}（{team}）非法："
            + "须零填充 YYYY-MM-DD，否则字典序区间比较静默失效"
        )
    try:
        date.fromisoformat(value)  # 拒 2023-02-30 类假日期
    except ValueError as exc:
        raise ValueError(
            f"admin_match_exclusions.{field}={value!r}（{team}）非法日期"
        ) from exc
    return value


def admin_exclusions(conn: sqlite3.Connection) -> list[AdminExclusion]:
    """
    排除表全量行（运行面 sqlite，fail-closed 单一取数口）。

    表缺席=未迁移、窗口日期非法（非零填充 YYYY-MM-DD 或假日期）均抛错，
    覆盖判门①与 gold/引擎两侧消费面，不静默跳过排除纪律。
    """
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
            date_start=_checked_day(str(r["date_start"]), str(r["team"]), "date_start"),
            date_end=_checked_day(str(r["date_end"]), str(r["team"]), "date_end"),
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
