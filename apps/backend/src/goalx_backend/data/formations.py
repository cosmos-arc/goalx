"""
六轴数据 ② 阵型族条件面（vision-build 票 09；账本六轴②的 EM facet 底座）。

条件面=族 × 赛季 × 教练任期联结：语料 ``match_detail`` 阵型（赛后面）经
sid 链折到运行面 fixture → canonical team，再经 ``team_aliases(source=
'transfermarkt')`` 桥到 TM 俱乐部，指派为 manager-of-the-day（教练任期空间
=tm_coach_tenures，autoresearch 票 18 已落）——中英文教练名无映射
（coach_crosscheck 口径），故教练身份一律走 TM 英文名；源T 中文教练名
不入面（PIT 前向的教练可知性=TM 任期现任，非赛后面）。

PIT 契约：本面来自赛后面 ``match_detail``（阵型赛后才知道），回答的问题
是「该教练在该赛季惯用什么族」——作特征时 kickoff 前可知性=教练任期+
历史惯用族（读缝 ``coach_family_prior`` 只取查询时点前已完结的格子，
绝不含当场面；prematch 拍是另一条前向腿，不在此面）。

全联赛落库（fd 11 项映射外 league_fd=NULL 照落），切片报告只出英超+英冠
（票 08 人裁=全范围英超优先探索）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

import duckdb

from goalx_backend.data.coach_changes import Tenure, assign_manager_day
from goalx_backend.data.leagues import LEAGUE_TO_FD
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.modelling.team_align import transfermarkt_alias_by_team

#: 受控族全集（迁移 v53 CHECK 字面同源；「other」只由兜底产生，不经映射）
FAMILIES: tuple[str, ...] = (
    "4-4-2",
    "4-2-3-1",
    "4-3-3",
    "4-5-1",
    "3-5-2",
    "3-4-3",
    "back-five",
    "other",
)

#: 阵型串 → 族（受控字典：语义聚类=防线结构+中场形态；未列串归 other）
FAMILY_MAP: dict[str, str] = {
    "4-4-2": "4-4-2",
    "4-4-1-1": "4-4-2",
    "4-2-3-1": "4-2-3-1",
    "4-3-3": "4-3-3",
    "4-5-1": "4-5-1",
    "4-1-4-1": "4-5-1",
    "3-5-2": "3-5-2",
    "3-5-1-1": "3-5-2",
    "3-4-3": "3-4-3",
    "3-4-2-1": "3-4-3",
    "5-3-2": "back-five",
    "5-4-1": "back-five",
}

#: 覆盖报告切片（票 08 人裁：可购面英冠最大，报告只出英超+英冠）
SLICE_COMPETITIONS: tuple[str, ...] = ("E0", "E1")

#: 源T 联赛中文名 → fd 联赛码（LEAGUE_TO_FD 即 zh→fd，直用；映射外 NULL）
_LEAGUE_FD: dict[str, str] = dict(LEAGUE_TO_FD)
FACE_NAME = "formation_family_facet"

DuckCon = duckdb.DuckDBPyConnection


def family_of(formation: str | None) -> str | None:
    """阵型串 → 族；未匹配归 ``other`` 不炸；空值=None（未知，不进族分母）。"""
    if formation is None or formation == "":
        return None
    return FAMILY_MAP.get(formation, "other")


@dataclass
class _Cell:
    """一个条件格子的聚合态（计数+首末日+队锚）。"""

    n: int
    first: str
    last: str
    team_id: int


def _corpus_sides(duck_con: DuckCon) -> tuple[Iterator[dict[str, object]], int]:
    """
    语料侧观察流：match_detail × fixture_universe（sid 链）逐场两侧。

    返回 (行迭代, 语料总场数)。season 取 universe hive 分区（8 月界），
    league 取中文名（fd 映射在落库时折算）；day 取 match_detail 开球 UTC
    日期（duckdb 会话时区会影响 TIMESTAMPTZ 直 CAST，须 AT TIME ZONE
    'UTC' 归一——欧洲晚场 20:00Z 不漂日）；universe 缺席的孤儿 sid 计入
    总数但不出行（无赛季轴，诚实丢弃——覆盖报告的 universe_missing）。
    """
    rows = duck_con.execute(
        """
        SELECT d.sid AS sid,
               CAST(d.kickoff AT TIME ZONE 'UTC' AS DATE) AS day,
               d.home_formation AS home_formation,
               d.away_formation AS away_formation,
               f.season AS season,
               f.league AS league
        FROM match_detail d
        JOIN fixture_universe f ON f.sid = d.sid
        WHERE d.kickoff IS NOT NULL
        ORDER BY d.kickoff, d.sid
        """
    ).fetchall()
    seen = duck_con.execute("SELECT COUNT(*) FROM match_detail").fetchone()
    total = int(seen[0]) if seen is not None else 0

    def stream() -> Iterator[dict[str, object]]:
        for sid, day, home_f, away_f, season, league in rows:
            yield {
                "sid": str(sid),
                "day": str(day),
                "home_formation": None if home_f is None else str(home_f),
                "away_formation": None if away_f is None else str(away_f),
                "season": str(season),
                "league": str(league),
            }

    return stream(), total


def _linked_sides(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """Sid → (home_team_id, away_team_id)（已链场；未链不进键）。"""
    return {
        str(row["source_match_id"]): (
            int(row["home_team_id"]),
            int(row["away_team_id"]),
        )
        for row in conn.execute(
            """
            SELECT l.source_match_id AS source_match_id,
                   f.home_team_id AS home_team_id, f.away_team_id AS away_team_id
            FROM source_match_links l
            JOIN fixtures f ON f.id = l.fixture_id
            WHERE l.source = 'srct' AND l.status = 'linked'
            """
        ).fetchall()
    }


def _tm_clubs(conn: sqlite3.Connection) -> tuple[dict[int, str], dict[str, int]]:
    """
    TM 身份两查（team→TM 俱乐部名 / 俱乐部名→club_tm_id）。

    别名桥经 team_align 读口（transfermarkt，ADR-0008 属主）；club_name
    撞多名俱乐部（TM 重命名窗口等）→ 不硬配，整名丢弃（计数）。
    """
    alias_to_club = transfermarkt_alias_by_team(conn)
    club_ids: dict[str, int] = {}
    club_conflicts: set[str] = set()
    for row in conn.execute(
        "SELECT DISTINCT club_name, club_tm_id FROM tm_coach_tenures"
    ).fetchall():
        name = str(row["club_name"])
        if name in club_ids and club_ids[name] != int(row["club_tm_id"]):
            club_conflicts.add(name)
        club_ids[name] = int(row["club_tm_id"])
    for name in club_conflicts:
        club_ids.pop(name, None)
    return alias_to_club, club_ids


def _tenures_by_club(conn: sqlite3.Connection) -> dict[int, list[Tenure]]:
    """club_tm_id → 任职行（date_from 升序；assign_manager_day 反向首见语义）。"""
    out: dict[int, list[Tenure]] = {}
    for row in conn.execute(
        """
        SELECT club_tm_id, role, coach_name, date_from, date_to
        FROM tm_coach_tenures
        ORDER BY club_tm_id, date_from, coach_name
        """
    ).fetchall():
        out.setdefault(int(row["club_tm_id"]), []).append(
            Tenure(
                role=str(row["role"]),
                coach_name=str(row["coach_name"]),
                date_from=str(row["date_from"]),
                date_to=str(row["date_to"]) if row["date_to"] is not None else None,
            )
        )
    return out


def derive_formation_family_facet(
    conn: sqlite3.Connection,
    duck_con: DuckCon,
    *,
    now: str | None = None,
) -> dict[str, object]:
    """
    重建阵型族条件面（幂等：DELETE+INSERT 全量重建，原子）。

    粒度=(season, league_zh, club_tm_id, coach_name, family)。丢弃口径
    （进 stats，诚实现态）：universe_missing=detail 场不在 universe（无赛季
    轴）；unlinked=无 sid 链；no_alias=链到 team 但无 transfermarkt 别名；
    no_club=别名名不指唯一 TM 俱乐部；unattributed=当日无覆盖任期（空窗
    场）；formation_unknown=指派成功但阵型缺失（不进任何族分母）。
    空输入零行不炸。
    """
    rebuilt_at = now or utc_now_iso()
    sides, matches_seen = _corpus_sides(duck_con)
    links = _linked_sides(conn)
    alias_to_club, club_ids = _tm_clubs(conn)
    tenures = _tenures_by_club(conn)
    drops = {
        "unlinked": 0,
        "no_alias": 0,
        "no_club": 0,
        "unattributed": 0,
        "formation_unknown": 0,
    }
    # 格子键=(season, league_zh, club_tm_id, coach, family) → 计数/首末日；
    # 组键=(season, league_zh, club_tm_id, coach) → 分母（已知阵型场数）与
    # 末场（角色取末场指派，同人转正口径与 coach_changes 一致）
    cells: dict[tuple[str, str, int, str, str], _Cell] = {}
    group_known: dict[tuple[str, str, int, str], int] = {}
    group_last: dict[tuple[str, str, int, str], tuple[str, str]] = {}
    joined = 0
    for match in sides:
        joined += 1
        linked = links.get(str(match["sid"]))
        if linked is None:
            drops["unlinked"] += 1
            continue
        day = str(match["day"])
        for team_id, formation in (
            (linked[0], match["home_formation"]),
            (linked[1], match["away_formation"]),
        ):
            club_name = alias_to_club.get(team_id)
            if club_name is None:
                drops["no_alias"] += 1
                continue
            club_tm_id = club_ids.get(club_name)
            if club_tm_id is None:
                drops["no_club"] += 1
                continue
            assigned = assign_manager_day(tenures.get(club_tm_id, []), day)
            if assigned is None:
                drops["unattributed"] += 1
                continue
            coach, role = assigned
            group = (str(match["season"]), str(match["league"]), club_tm_id, coach)
            group_last[group] = (day, role)
            family = family_of(formation if isinstance(formation, str) else None)
            if family is None:
                drops["formation_unknown"] += 1
                continue
            group_known[group] = group_known.get(group, 0) + 1
            cell = cells.setdefault(
                (*group, family), _Cell(n=0, first=day, last=day, team_id=team_id)
            )
            cell.n += 1
            cell.first = min(cell.first, day)
            cell.last = max(cell.last, day)
    with atomic(conn):
        conn.execute("DELETE FROM formation_family_facet")
        for key in sorted(cells):
            season, league_zh, club_tm_id, coach, family = key
            group = (season, league_zh, club_tm_id, coach)
            cell = cells[key]
            conn.execute(
                """
                INSERT INTO formation_family_facet
                    (season, league_fd, league_zh, team_id, club_tm_id, club_name,
                     coach_name, coach_role, family, n_matches, n_known,
                     first_match_date, last_match_date, rebuilt_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    season,
                    _LEAGUE_FD.get(league_zh),
                    league_zh,
                    cell.team_id,
                    club_tm_id,
                    alias_to_club[cell.team_id],
                    coach,
                    group_last[group][1],
                    family,
                    cell.n,
                    group_known[group],
                    cell.first,
                    cell.last,
                    rebuilt_at,
                ),
            )
        stats: dict[str, object] = {
            "matches_seen": matches_seen,
            **drops,
            "universe_missing": matches_seen - joined,
            "rows": len(cells),
            "club_season_leagues": len(group_known),
            "coach_club_pairs": len({(k[2], k[3]) for k in group_known}),
            "rebuilt_at": rebuilt_at,
        }
        conn.execute(
            """
            INSERT INTO face_derive_state (face, rebuilt_at, stats)
            VALUES (?, ?, ?)
            ON CONFLICT(face) DO UPDATE SET
                rebuilt_at = excluded.rebuilt_at, stats = excluded.stats
            """,
            (FACE_NAME, rebuilt_at, json.dumps(stats, ensure_ascii=False)),
        )
    return stats


def coach_family_prior(
    conn: sqlite3.Connection,
    coach_name: str,
    *,
    upto_season: str,
    on_day: str | None = None,
) -> dict[str, object]:
    """
    Facet 扫描器读缝（PIT 安全）：该教练截至 (upto_season, on_day) 的惯用族。

    窗口语义：给 ``on_day``=只取 ``last_match_date < on_day`` 且
    ``season <= upto_season`` 的已完结格子（季中视角）；不给=只取
    ``season < upto_season`` 的整季格子（季首视角）——两种视角都不含
    查询当场的阵型（当场面赛后才知道）。n_known=窗口内该教练已知阵型
    场数（按 季×俱乐部 组去重后求和，避免组内族行重复计分母）；
    dominant_family 并列取字典序稳定者。
    """
    if on_day is not None:
        rows = conn.execute(
            """
            SELECT season, league_zh, club_tm_id, family, n_matches, n_known
            FROM formation_family_facet
            WHERE coach_name = ? AND season <= ? AND last_match_date < ?
            """,
            (coach_name, upto_season, on_day),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT season, league_zh, club_tm_id, family, n_matches, n_known
            FROM formation_family_facet
            WHERE coach_name = ? AND season < ?
            """,
            (coach_name, upto_season),
        ).fetchall()
    cells: dict[str, int] = {}
    group_known: dict[tuple[str, str, int], int] = {}
    for row in rows:
        cells[str(row["family"])] = cells.get(str(row["family"]), 0) + int(
            row["n_matches"]
        )
        group = (str(row["season"]), str(row["league_zh"]), int(row["club_tm_id"]))
        group_known[group] = max(group_known.get(group, 0), int(row["n_known"]))
    ordered = sorted(cells.items(), key=lambda item: (-item[1], item[0]))
    return {
        "coach": coach_name,
        "upto_season": upto_season,
        "on_day": on_day,
        "cells": cells,
        "n_known": sum(group_known.values()),
        "dominant_family": ordered[0][0] if ordered else None,
    }


def coverage_report(conn: sqlite3.Connection) -> dict[str, object]:
    """
    覆盖报告（只读）：全联赛行数 + 英超/英冠切片（票面验收①）。

    leagues 按 (league_fd, league_zh) 分桶（fd 映射外 league_fd=NULL 照报）；
    totals 的丢弃计数取最近一次派生现态（face_derive_state；从未派生=
    derived_pending）。
    """
    leagues = [
        {
            "league_fd": row["league_fd"],
            "league_zh": str(row["league_zh"]),
            "rows": int(row["rows"]),
            "clubs": int(row["clubs"]),
            "coach_seasons": int(row["coach_seasons"]),
            "matches": int(row["matches"]),
            "known": int(row["known"]),
        }
        for row in conn.execute(
            """
            SELECT league_fd, league_zh, COUNT(*) AS rows,
                   COUNT(DISTINCT club_tm_id) AS clubs,
                   COUNT(DISTINCT coach_name || '|' || season) AS coach_seasons,
                   SUM(n_matches) AS matches, SUM(n_known) AS known
            FROM formation_family_facet
            GROUP BY league_fd, league_zh
            ORDER BY league_fd IS NULL, league_fd, league_zh
            """
        ).fetchall()
    ]
    totals: dict[str, object] = {
        "rows": sum(int(item["rows"]) for item in leagues),
    }
    state = conn.execute(
        "SELECT stats FROM face_derive_state WHERE face = ?", (FACE_NAME,)
    ).fetchone()
    derived_pending = state is None
    if state is not None:
        parsed = json.loads(str(state["stats"]))
        totals.update(
            {
                "matches_seen": parsed.get("matches_seen", 0),
                "unlinked": parsed.get("unlinked", 0),
                "no_alias": parsed.get("no_alias", 0),
                "no_club": parsed.get("no_club", 0),
                "unattributed": parsed.get("unattributed", 0),
                "formation_unknown": parsed.get("formation_unknown", 0),
                "rebuilt_at": parsed.get("rebuilt_at"),
            }
        )
    return {
        "leagues": leagues,
        "slice": list(SLICE_COMPETITIONS),
        # 优先切片（票 08 人裁：英超优先探索、英冠=可购面最大）——
        # E0/E1 的切片行单独成节，消费方免再过滤
        "priority_slice": [
            item for item in leagues if item["league_fd"] in SLICE_COMPETITIONS
        ],
        "totals": totals,
        "derived_pending": derived_pending,
    }
