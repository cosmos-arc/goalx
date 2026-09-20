"""
openfootball/football.json 对账源（票 44）：CC0 静态赛季文件，raw 直下。

定位（research/17 §四 + 票 44）：海外源仅对账不入结算管道——本模块只做
比分对账（对 draw_results），不落事实。特性与坑（2026-09-20 实测）：

- 文件 ``{base}/{赛季}/{联赛}.json``（如 ``2026-27/en.1.json``）：扁平
  ``matches`` 数组，``team1/team2`` 英文全名（带 FC/AFC 等法律后缀，
  team_alias 规范化正好剥除）、``date``（当地比赛日）、可选 ``time``；
- ``score`` 两种形态混用（票 44 坑）：dict ``{"ft": [h, a], "ht": [h, a]}``
  与 list ``[h, a]``（无半场），解析需兼容；
- 已踢场次比分回填约一轮滞后（87-91%）——无比分场不是异常，跳过即可；
- 覆盖 2026-27：五大 + 英冠 + 荷甲 + 荷乙 + 葡超 + 德乙 + 法乙（LEAGUE_FILES
  如实登记；未映射联赛不请求）。coverage_date=赛季键（源粒度是整季文件）。

join 走定则 1：开球日 ±1 天 + 双方队名经 team_aliases 规范化解析到同一
fixture（双方命中即唯一化）；无候选/多候选不硬配（多候选进人工）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import httpx
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data.reconcile import (
    ReconcileStats,
    ReferenceResult,
    reconcile_draw_results,
    record_reconciliation_run,
    upsert_source_coverage,
)
from goalx_backend.modelling.team_align import NameIndex

SOURCE = "openfootball"
PARSE_VERSION = "openfootball_v1"
_LOOKBACK_DAYS = 7
_PAIR_LEN = 2  # score 数组形态恒为 [主, 客]
_SEASON_START_MONTH = 7  # 跨年赛季分界（7 月起属新季）
_NOT_COVERED = 404  # 赛季文件不存在（联赛未覆盖）
# 联赛 → football.json 文件名（2026-27 实测有文件的竞彩联赛；未列=不覆盖）
LEAGUE_FILES: dict[str, str] = {
    "英超": "en.1",
    "英冠": "en.2",
    "德甲": "de.1",
    "德乙": "de.2",
    "意甲": "it.1",
    "西甲": "es.1",
    "法甲": "fr.1",
    "法乙": "fr.2",
    "荷甲": "nl.1",
    "荷乙": "nl.2",
    "葡超": "pt.1",
}


@dataclass
class OfMatch:
    """一条 openfootball 赛季文件赛果。"""

    match_date: str
    team1: str
    team2: str
    ft: tuple[int, int]
    ht: tuple[int, int] | None


def parse_score(
    score: object,
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """Score 两形态兼容：dict ``{"ft": [h,a], "ht": [h,a]}`` 或 list ``[h,a]``。"""
    if isinstance(score, dict):
        data = cast("dict[str, Any]", score)
        return _pair(data.get("ft")), _pair(data.get("ht"))
    if isinstance(score, list):
        return _pair(cast("list[Any]", score)), None
    return None, None


def _pair(value: object) -> tuple[int, int] | None:
    items = cast("list[Any]", value) if isinstance(value, list) else None
    if items is not None and len(items) == _PAIR_LEN:
        first, second = items
        if isinstance(first, int) and isinstance(second, int):
            return first, second
    return None


def parse_season(doc: dict[str, Any]) -> list[OfMatch]:
    """赛季文件 → 已回填比分的比赛列表（纯函数；无比分行跳过）。"""
    matches: list[OfMatch] = []
    for raw in cast("list[dict[str, Any]]", doc.get("matches") or []):
        ft, ht = parse_score(raw.get("score"))
        if ft is None:
            continue
        matches.append(
            OfMatch(
                match_date=str(raw.get("date") or ""),
                team1=str(raw.get("team1") or ""),
                team2=str(raw.get("team2") or ""),
                ft=ft,
                ht=ht,
            )
        )
    return matches


def season_key(match_date: str) -> str:
    """比赛日 → 赛季键（跨年赛季：7 月起属新季，如 2026-08 → "2026-27"）。"""
    day = date.fromisoformat(match_date)
    if day.month >= _SEASON_START_MONTH:
        return f"{day.year}-{(day.year + 1) % 100:02d}"
    return f"{day.year - 1}-{day.year % 100:02d}"


def fetch_season(
    client: httpx.Client, settings: Settings, season: str, file_name: str
) -> dict[str, Any] | None:
    """拉一个赛季文件；404/未覆盖返回 None（联赛缺文件不炸整跑）。"""
    response = client.get(
        f"{settings.openfootball_base_url}/{season}/{file_name}.json", timeout=25.0
    )
    if response.status_code == _NOT_COVERED:
        return None
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return None
    return cast("dict[str, Any]", payload)


def _window_fixtures(
    conn: sqlite3.Connection, floor: str, now_iso: str
) -> list[sqlite3.Row]:
    """待对账窗口内的竞彩场次（已开赛、带场次号；含赛果有无两种）。"""
    return conn.execute(
        """
        SELECT f.id, f.kickoff_utc, f.home_team_id, f.away_team_id,
               c.name AS league, mc.business_date, mc.code
        FROM fixtures f
        JOIN competitions c ON c.id = f.competition_id
        JOIN match_codes mc ON mc.fixture_id = f.id AND mc.kind = 'jingcai'
        WHERE f.kickoff_utc <= ? AND f.kickoff_utc >= ?
        ORDER BY f.kickoff_utc
        """,
        (now_iso, floor),
    ).fetchall()


def _dates_around(kickoff_utc: str) -> set[str]:
    """开球 UTC 日 ±1 天（时差与当地比赛日的错日兜底）。"""
    day = date.fromisoformat(kickoff_utc[:10])
    return {(day + timedelta(days=delta)).isoformat() for delta in (-1, 0, 1)}


def reconcile_openfootball(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    alias_index: NameIndex,
    now: datetime | None = None,
) -> ReconcileStats:
    """
    Openfootball 比分对账：窗口内竞彩场次 ↔ 赛季文件已回填比分。

    - 只比对已映射联赛（LEAGUE_FILES）且能唯一配对的场次；未配对计数
      不报警（回填滞后/覆盖缺口属常态，coverage 行可判别）；
    - 多候选配对进待人工（禁自信合并）；无比分行跳过；
    - 对账只报清单不落事实；run 行 append-only。
    """
    now_dt = now or datetime.now(UTC)
    now_iso = observed_at = now_dt.isoformat(timespec="seconds")
    floor = (now_dt - timedelta(days=_LOOKBACK_DAYS)).isoformat(timespec="seconds")
    stats = ReconcileStats(source=SOURCE, observed_at=observed_at)

    fixtures = _window_fixtures(conn, floor, now_iso)
    wanted = [row for row in fixtures if str(row["league"]) in LEAGUE_FILES]
    if not wanted:
        record_reconciliation_run(conn, stats)
        return stats

    seasons: dict[tuple[str, str], list[OfMatch] | None] = {}
    resolved: dict[str, int | None] = {}  # 英文名 → team_id（解析缓存）
    refs: list[ReferenceResult] = []

    def _team_id(name: str) -> int | None:
        if name not in resolved:
            resolved[name] = alias_index.resolve(name)
        return resolved[name]

    for row in wanted:
        league = str(row["league"])
        file_name = LEAGUE_FILES[league]
        season = season_key(str(row["kickoff_utc"])[:10])
        key = (season, file_name)
        if key not in seasons:
            doc = fetch_season(client, settings, season, file_name)
            matches = parse_season(doc) if doc is not None else None
            seasons[key] = matches
            upsert_source_coverage(
                conn,
                source=SOURCE,
                coverage_date=season,
                match_count=len(matches) if matches else 0,
                coverage_status=("covered" if matches is not None else "not_covered"),
                observed_at=observed_at,
                league_key=file_name,
            )
            if matches is None:
                logger.warning(
                    "openfootball 未覆盖 {} {}（跳过该联赛窗口）", league, season
                )
        matches = seasons[key]
        if matches is None:
            continue
        days = _dates_around(str(row["kickoff_utc"]))
        home_id, away_id = int(row["home_team_id"]), int(row["away_team_id"])
        candidates = [
            m
            for m in matches
            if m.match_date in days
            and _team_id(m.team1) == home_id
            and _team_id(m.team2) == away_id
        ]
        if not candidates:
            stats.unmatched += 1
            continue
        if len(candidates) > 1:
            stats.pending_manual.append(
                {
                    "business_date": str(row["business_date"]),
                    "code": str(row["code"]),
                    "reason": "openfootball_ambiguous_match",
                    "detail": f"{len(candidates)} 条同对阵同窗口候选",
                }
            )
            continue
        match = candidates[0]
        stats.business_dates.append(str(row["business_date"]))
        refs.append(
            ReferenceResult(
                fixture_id=int(row["id"]),
                business_date=str(row["business_date"]),
                code=f"of:{match.team1} v {match.team2}",
                home_goals=match.ft[0],
                away_goals=match.ft[1],
                half_home_goals=match.ht[0] if match.ht else None,
                half_away_goals=match.ht[1] if match.ht else None,
            )
        )

    reconcile_draw_results(conn, refs, stats)
    stats.business_dates = sorted(set(stats.business_dates))
    record_reconciliation_run(conn, stats, parse_version=PARSE_VERSION)
    return stats
