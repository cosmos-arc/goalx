"""
srct 教练名交叉核验（autoresearch 票 18；srct coach 2024 起为交叉面）。

只读报告：TM 换帅事件 × 源T 详情页教练名（match_detail 银层，中文）的
**结构性一致率**——事件日前后的最近两场已覆盖场里，源T 侧教练名是否也
换人。中英文教练名无映射可用（中文名映射只作用 JC 中文面、教练侧无此
面），故只判「换没换」不判「是谁」；方法注记随报告输出。

窗口：前侧=事件日前 window_days 内最近一场有教练名的场；后侧=事件日起
window_days 内最近一场。覆盖缺失（前或后无场/无名）如实 skipped，不造
分母（诚实小样本）。duckdb 消费走 corpus_duckdb.reader()（corpus 缺席
降级报告 corpus_available=False，不发请求）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from loguru import logger

from goalx_backend.config import Settings, get_settings
from goalx_backend.data import corpus_duckdb

DEFAULT_WINDOW_DAYS = 45

_METHOD_NOTE = (
    "结构性一致：TM 换帅边界前后源T 侧教练名是否换人（中文名无英文映射，"
    "不核名只核换；前=事件日前窗口内最近有名师，后=事件日起窗口内最近有名师）"
)


@dataclass(frozen=True)
class EventLite:
    """交叉核验用换帅事件行。"""

    team_id: int | None
    club_name: str
    event_date: str


def agreement_from_rows(
    events: Sequence[EventLite],
    by_team: Mapping[int, Sequence[tuple[str, str | None]]],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, object]:
    """
    事件 × 该队 srct 观察（(day, coach) 序列）→ 一致率报告（纯函数）。

    输入序列允许乱序（内部按日稳定排序）；coach None 的观察跳过但窗口
    继续向更远处找。无分母时 agreement_rate=None（不造率）。
    """
    agree = 0
    disagree = 0
    skipped = 0
    unresolved = 0
    for event in events:
        if event.team_id is None:
            unresolved += 1
            continue
        observations = sorted(
            ((day, coach) for day, coach in by_team.get(event.team_id, ()) if coach),
            key=lambda item: item[0],
        )
        window_start = (
            date.fromisoformat(event.event_date) - timedelta(days=window_days)
        ).isoformat()
        window_end = (
            date.fromisoformat(event.event_date) + timedelta(days=window_days)
        ).isoformat()
        before = next(
            (
                coach
                for day, coach in reversed(observations)
                if window_start <= day < event.event_date
            ),
            None,
        )
        after = next(
            (
                coach
                for day, coach in observations
                if event.event_date <= day <= window_end
            ),
            None,
        )
        if before is None or after is None:
            skipped += 1
            continue
        if before == after:
            disagree += 1
        else:
            agree += 1
    eligible = agree + disagree
    return {
        "events": len(events),
        "eligible": eligible,
        "agree": agree,
        "disagree": disagree,
        "skipped_no_coverage": skipped,
        "unresolved_team": unresolved,
        "agreement_rate": (agree / eligible) if eligible else None,
        "window_days": window_days,
        "method": _METHOD_NOTE,
    }


def _load_events(conn: sqlite3.Connection) -> list[EventLite]:
    """换帅事件（含 canonical team_id；未解析队留 None 由报告计数）。"""
    return [
        EventLite(
            team_id=int(r["team_id"]) if r["team_id"] is not None else None,
            club_name=str(r["club_name"]),
            event_date=str(r["event_date"]),
        )
        for r in conn.execute(
            """
            SELECT team_id, club_name, event_date FROM coach_change_events
            ORDER BY event_date
            """
        ).fetchall()
    ]


def srct_coach_agreement(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, object]:
    """
    只读交叉核验入口：corpus match_detail 银层（经 sid 链）折到事件队一侧。

    corpus 缺席/被锁：降级 corpus_available=False（不发请求、不造率）。
    kickoff 为 UTC 时间戳，取 UTC 日期与 fd 比赛日对齐（±1 日的本地差由
    窗口吸收，方法注记已含）。
    """
    events = _load_events(conn)
    report: dict[str, object] = {
        "events": len(events),
        "corpus_available": False,
        "method": _METHOD_NOTE,
    }
    with corpus_duckdb.reader(settings or get_settings()) as con:
        if con is None:
            return report
        raw_rows = con.execute(
            """
            SELECT CAST(d.kickoff AS DATE) AS day,
                   f.home_team_id AS home_team_id,
                   f.away_team_id AS away_team_id,
                   d.home_coach AS home_coach,
                   d.away_coach AS away_coach
            FROM goalx.source_match_links l
            JOIN goalx.fixtures f ON f.id = l.fixture_id
            JOIN match_detail d ON d.sid = l.source_match_id
            WHERE l.source = 'srct' AND l.status = 'linked'
            """
        ).fetchall()
    report["corpus_available"] = True
    report["srct_covered_matches"] = len(raw_rows)
    by_team: dict[int, list[tuple[str, str | None]]] = {}
    days: list[str] = []
    for row in raw_rows:
        day_str = str(row[0])
        days.append(day_str)
        home_id = int(row[1])
        away_id = int(row[2])
        home_coach = str(row[3]) if row[3] is not None else None
        away_coach = str(row[4]) if row[4] is not None else None
        by_team.setdefault(home_id, []).append((day_str, home_coach))
        by_team.setdefault(away_id, []).append((day_str, away_coach))
    if days:
        earliest = min(days)
        report["srct_face_from"] = earliest
        events = [e for e in events if e.event_date >= earliest]
        report["events_in_face"] = len(events)
    else:
        report["srct_face_from"] = None
        report["events_in_face"] = 0
    report.update(agreement_from_rows(events, by_team, window_days=window_days))
    logger.info("srct 教练交叉核验: {}", report)
    return report
