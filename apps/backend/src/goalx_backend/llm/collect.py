"""
情报采集编排（票 09）：免费组合的内部推导采集器 + 编排入口。

采集范围 v1：当期在售彩池期次（ttt14/pick9）的场次——有消费场景
（证据卡/任9 生成）的最小集合；Tier1 竞彩场次随票 10 scout 输入面扩。

伤停/阵容外部情报：源B（okooo）场次详情页为 AJAX 网关壳
（2026-09-19 实测：子路径 qingbao/analysis 全 405，主页无数据块），
直爬暂缓——按票 03 裁决双路预案，走 GLM web-search 兜底，
适配器随票 08 合入后实装（票 09 预留）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from loguru import logger

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.data.ingest import fdhist
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.llm.store import IntelDraft, insert_intel_observation
from goalx_backend.modelling.team_align import odds_api_aliases_for_team

# 竞彩 sport key → football-data 联赛码（六联赛，票 26 冻结范围）
_SPORT_KEY_TO_FD = {
    "soccer_epl": "E0",
    "soccer_germany_bundesliga": "D1",
    "soccer_spain_la_liga": "SP1",
    "soccer_italy_serie_a": "I1",
    "soccer_france_ligue_one": "F1",
    "soccer_netherlands_eredivisie": "N1",
}

_COLLECTOR = "internal-fdhist"
_RECENT_LIMIT = 6
_H2H_LIMIT = 6


@dataclass
class IntelSyncStats:
    """一次情报采集的计数（幂等重跑 inserted=0 属正常）。"""

    periods: int = 0
    matches_seen: int = 0
    with_fixture: int = 0
    unmapped_league: int = 0
    unmapped_teams: int = 0
    inserted: int = 0
    skipped_known: int = 0
    notes: list[str] = field(default_factory=list)


def _form_text(team: str, rows: list[sqlite3.Row]) -> str:
    """近况行 → 摘要文本（W/D/L + 进失球；从最新往回）。"""
    parts: list[str] = []
    goals_for = goals_against = 0
    for row in rows:
        home = str(row["home_team"])
        fthg, ftag, ftr = int(row["fthg"]), int(row["ftag"]), str(row["ftr"])
        gf, ga = (fthg, ftag) if home == team else (ftag, fthg)
        goals_for += gf
        goals_against += ga
        won = (ftr == "H" and home == team) or (ftr == "A" and home != team)
        parts.append("D" if ftr == "D" else ("W" if won else "L"))
    return f"近{len(rows)}轮 {''.join(parts)}（进{goals_for}失{goals_against}）"


def _h2h_line(row: sqlite3.Row) -> str:
    """交锋行 → '主 比分 客' 片段。"""
    return (
        f"{row['home_team']} {int(row['fthg'])}-{int(row['ftag'])} {row['away_team']}"
    )


def _hist_name_for(
    conn: sqlite3.Connection, team_id: int, team_name: str, fd_competition: str
) -> str | None:
    """球队 → hist 队名：odds_api 英文别名与 fd 队名集求交（无命中 None）。"""
    candidates = {team_name, *odds_api_aliases_for_team(conn, team_id)}
    known = set(fdhist.hist_team_names(conn, fd_competition))
    for name in candidates:
        if name in known:
            return name
    return None


def collect_fdhist_intel(
    conn: sqlite3.Connection, fixture_id: int, *, collected_at: str | None = None
) -> int:
    """一场的内部推导情报（近况×2 + H2H）：落库条数（无映射零行）。"""
    info = fx_store.fixture_team_info(conn, fixture_id)
    if info is None:
        return 0
    fd_competition = _SPORT_KEY_TO_FD.get(str(info["sport_key"] or ""))
    if fd_competition is None:
        return 0
    now = collected_at or utc_now_iso()
    inserted = 0
    sides = (
        ("home", int(info["home_team_id"]), str(info["home_name"])),
        ("away", int(info["away_team_id"]), str(info["away_name"])),
    )
    hist_names: dict[str, str] = {}
    for side, team_id, team_name in sides:
        hist_name = _hist_name_for(conn, team_id, team_name, fd_competition)
        if hist_name is None:
            continue  # 该侧无 fd 队名映射——诚实零行，不编数据
        hist_names[side] = hist_name
        rows = fdhist.recent_team_matches(
            conn, fd_competition, hist_name, limit=_RECENT_LIMIT
        )
        if not rows:
            continue
        draft = IntelDraft(
            kind="form",
            text=f"{team_name}（{hist_name}）{_form_text(hist_name, rows)}",
            source=f"fdhist:{fd_competition}",
            collected_at=now,
            collector=_COLLECTOR,
            raw_payload={
                "side": side,
                "team": hist_name,
                "fd_competition": fd_competition,
                "matches": [
                    {
                        "date": str(r["match_date"]),
                        "season": str(r["season"]),
                        "home": str(r["home_team"]),
                        "away": str(r["away_team"]),
                        "score": f"{int(r['fthg'])}-{int(r['ftag'])}",
                        "ftr": str(r["ftr"]),
                    }
                    for r in rows
                ],
            },
        )
        if insert_intel_observation(conn, fixture_id, draft):
            inserted += 1
    if "home" in hist_names and "away" in hist_names:
        h2h = fdhist.h2h_matches(
            conn,
            fd_competition,
            hist_names["home"],
            hist_names["away"],
            limit=_H2H_LIMIT,
        )
        if h2h:
            draft = IntelDraft(
                kind="h2h",
                text=(
                    f"近{len(h2h)}次同联赛交锋：" + "、".join(_h2h_line(r) for r in h2h)
                ),
                source=f"fdhist:{fd_competition}",
                collected_at=now,
                collector=_COLLECTOR,
                raw_payload={
                    "fd_competition": fd_competition,
                    "matches": [
                        {
                            "date": str(r["match_date"]),
                            "home": str(r["home_team"]),
                            "away": str(r["away_team"]),
                            "score": f"{int(r['fthg'])}-{int(r['ftag'])}",
                        }
                        for r in h2h
                    ],
                },
            )
            if insert_intel_observation(conn, fixture_id, draft):
                inserted += 1
    return inserted


def collect_pool_intel(
    conn: sqlite3.Connection, *, now: str | None = None
) -> IntelSyncStats:
    """当期在售彩池场次的情报采集（幂等；无桥接/无映射如实计数）。"""
    stats = IntelSyncStats()
    moment = now or utc_now_iso()
    with atomic(conn):
        for market_code in ("ttt14", "pick9"):
            for period in pool_store.list_pool_periods(conn, market_code):
                deadline = period["sales_deadline"]
                if deadline is None or str(deadline) < moment:
                    continue  # 已截止期次不采
                stats.periods += 1
                for row in pool_store.pool_matches_for_period(conn, int(period["id"])):
                    stats.matches_seen += 1
                    fixture_id = pool_store.match_fixture_id(
                        conn,
                        str(row["kickoff_utc"]),
                        str(row["home_team"]),
                        str(row["away_team"]),
                    )
                    if fixture_id is None:
                        note = (
                            f"{market_code} {period['period_no']}"
                            f" 第{row['match_seq']}场无 fixture 桥接"
                        )
                        stats.notes.append(note)
                        continue
                    stats.with_fixture += 1
                    before = stats.inserted
                    stats.inserted += collect_fdhist_intel(
                        conn, fixture_id, collected_at=moment
                    )
                    if stats.inserted == before:
                        info = fx_store.fixture_team_info(conn, fixture_id)
                        fd = (
                            _SPORT_KEY_TO_FD.get(str(info["sport_key"] or ""))
                            if info
                            else None
                        )
                        if fd is None:
                            stats.unmapped_league += 1
                        else:
                            stats.unmapped_teams += 1
    if stats.notes:
        logger.info("情报采集跳过（无 fixture 桥接）{} 场", len(stats.notes))
    return stats


def stats_dict(stats: IntelSyncStats) -> dict[str, object]:
    """统计转字典（flow 日志/CLI 输出用）。"""
    return {
        "periods": stats.periods,
        "matches_seen": stats.matches_seen,
        "with_fixture": stats.with_fixture,
        "unmapped_league": stats.unmapped_league,
        "unmapped_teams": stats.unmapped_teams,
        "inserted": stats.inserted,
        "skipped_no_fixture": stats.matches_seen - stats.with_fixture,
    }
