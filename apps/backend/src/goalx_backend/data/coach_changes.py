"""
换帅事件派生面（autoresearch 票 18；账本 §5-1 落地序①的判据底座）。

输入 = tm_coach_tenures × hist_matches（fd 英文侧十年窗），输出两张派生表
（coach_change_events / coach_change_samples，DELETE+INSERT 重建、非事实面）：

- 指派语义：一场一帅——date_from ≤ 当日且区间覆盖（开区间=现任）、
  date_from 最晚的任职行接管（主帅病假未结 + 代理重叠时代理在任）；
- 事件语义：同一俱乐部相邻两场（主客皆入流）的指派从 A 换到 B 即换帅
  边界（**只比教练名**——caretaker→manager 同人转正是连续段不是换帅，
  独立复核 F1；role 只进 payload），事件日 = B 段首战日（空窗场无指派、
  不计边界；窗首上任无前段、非事件）；
- 样本语义：新帅段头 1-3 场（§5-1 可证伪化补注「换帅后 1-3 场」样本面）。

identity（票面红线）：英文侧匹配——TM 俱乐部英文名先经三级 NameIndex
（normalize + 词元子集，identity-alias 票 01 方法复用）在 club 所属 fd
联赛内直接解析 hist 队名；词法无关对子（Hearts↔Heart of Midlothian 这
类，独立复核 F2）再经 team_aliases 英文别名桥（两侧各解析到 canonical
team_id 相接）兜底。跨联赛解析不一致或仍未解析的俱乐部诚实计数入
unresolved，不硬配（定则 1），解析率低于 IDENTITY_DEGRADED_RATE 报
identity_status=degraded（回填后门槛观测）。中文名映射只作用 JC 中文面。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from goalx_backend.data.ingest.tm_coach import TM_LEAGUES
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.modelling.team_align import NameIndex, alias_index

#: 换帅后样本窗（票面：新帅首 1-3 场）
SAMPLE_WINDOW = 3
#: identity 解析率守门（独立复核 F2）：低于此值 identity_status=degraded
#: （team_align ALIGNMENT_DEGRADED_COVERAGE=0.95 起步先例的宽松版——
#: TM↔fd 是新桥，先把缺口显性化再随别名补线收紧）
IDENTITY_DEGRADED_RATE = 0.7

_TM_CODE_TO_FD = {code: fd for fd, code in TM_LEAGUES.items()}


@dataclass(frozen=True)
class Tenure:
    """派生用任职行（角色+教练+区间；同键行同段；票 09 阵型族面共用）。"""

    role: str
    coach_name: str
    date_from: str
    date_to: str | None


@dataclass(frozen=True)
class _HistMatch:
    """派生用历史场次行。"""

    id: int
    competition: str
    season: str
    match_date: str
    home_team: str
    away_team: str


def _load_tenures(conn: sqlite3.Connection) -> dict[int, tuple[str, list[Tenure]]]:
    """club_tm_id → (club_name, 按 date_from 升序的任职行)。"""
    out: dict[int, tuple[str, list[Tenure]]] = {}
    rows = conn.execute(
        """
        SELECT club_tm_id, club_name, role, coach_name, date_from, date_to
        FROM tm_coach_tenures
        ORDER BY club_tm_id, date_from, coach_name
        """
    ).fetchall()
    for row in rows:
        club = int(row["club_tm_id"])
        tenures = out.setdefault(club, (str(row["club_name"]), []))[1]
        tenures.append(
            Tenure(
                role=str(row["role"]),
                coach_name=str(row["coach_name"]),
                date_from=str(row["date_from"]),
                date_to=str(row["date_to"]) if row["date_to"] is not None else None,
            )
        )
    return out


def _load_memberships(conn: sqlite3.Connection) -> dict[int, set[str]]:
    """club_tm_id → 其 TM 覆盖联赛对应的 fd 联赛码集。"""
    out: dict[int, set[str]] = {}
    for row in conn.execute(
        "SELECT DISTINCT club_tm_id, league_tm_code FROM tm_club_seasons"
    ).fetchall():
        fd = _TM_CODE_TO_FD.get(str(row["league_tm_code"]))
        if fd is not None:  # 覆盖面外的 TM 联赛不参与（不构成边）
            out.setdefault(int(row["club_tm_id"]), set()).add(fd)
    return out


def _load_hist(conn: sqlite3.Connection) -> list[_HistMatch]:
    """全量 hist 场次（比赛日+id 稳定序；fd 面 2016-2026 全覆盖）。"""
    return [
        _HistMatch(
            id=int(r["id"]),
            competition=str(r["competition"]),
            season=str(r["season"]),
            match_date=str(r["match_date"]),
            home_team=str(r["home_team"]),
            away_team=str(r["away_team"]),
        )
        for r in conn.execute(
            """
            SELECT id, competition, season, match_date, home_team, away_team
            FROM hist_matches
            ORDER BY match_date, id
            """
        ).fetchall()
    ]


def assign_manager_day(tenures: list[Tenure], day: str) -> tuple[str, str] | None:
    """当日指派：(coach_name, role) 或 None（空窗）——date_from 最晚者接管。"""
    for tenure in reversed(tenures):  # date_from 升序 → 反向首见即最晚上任
        if tenure.date_from <= day and (
            tenure.date_to is None or day <= tenure.date_to
        ):
            return (tenure.coach_name, tenure.role)
    return None


def _resolve_hist_names(
    clubs: Mapping[int, str],
    memberships: dict[int, set[str]],
    hist: list[_HistMatch],
    canonical: NameIndex,
) -> dict[int, str]:
    """
    club_tm_id → hist 队名（英文侧：三级直配 → team_aliases 桥兜底）。

    直接词法匹配（normalize + 词元子集）在俱乐部覆盖到的 fd 联赛索引内
    解析；词法无关对子（Hearts↔Heart of Midlothian 族）经别名桥：TM 名
    与 hist 名各自解析到同一 canonical team_id 即接通。各联赛解析结果
    必须一致；一 hist 名不得被两家 TM 俱乐部认领（互撞=双方都不硬配）。
    """
    unique_by_comp: dict[str, set[str]] = {}
    for match in hist:
        unique_by_comp.setdefault(match.competition, set()).update(
            (match.home_team, match.away_team)
        )
    lists_by_comp = {comp: sorted(names) for comp, names in unique_by_comp.items()}
    indexes = {
        comp: NameIndex.build({name: pos for pos, name in enumerate(names)})
        for comp, names in lists_by_comp.items()
    }
    # hist 名 → canonical team_id（桥兜底输入；未解析名不参与桥接）
    canonical_by_name: dict[str, int | None] = {
        name: canonical.resolve(name)
        for names in lists_by_comp.values()
        for name in names
    }
    resolved: dict[int, str] = {}
    claimed: dict[str, int] = {}
    for club, club_name in clubs.items():
        comps = sorted(memberships.get(club, ()))
        found = _lexical_matches(club_name, comps, indexes, lists_by_comp)
        if not found:  # 词法无关 → team_aliases 桥兜底（复核 F2）
            found = _bridge_matches(
                club_name, comps, canonical, canonical_by_name, lists_by_comp
            )
        if len(found) != 1:
            continue  # 未解析/跨联赛不一致 → 不入派生面（调用方计数）
        hist_name = next(iter(found))
        if hist_name in claimed and claimed[hist_name] != club:
            resolved.pop(claimed[hist_name], None)  # 同名互撞：双方放弃
            continue
        claimed[hist_name] = club
        resolved[club] = hist_name
    return resolved


def _lexical_matches(
    club_name: str,
    comps: list[str],
    indexes: dict[str, NameIndex],
    lists_by_comp: dict[str, list[str]],
) -> set[str]:
    """三级直配：TM 名在覆盖联赛的 hist 名索引内 normalize/词元子集命中。"""
    found: set[str] = set()
    for fd in comps:
        index = indexes.get(fd)
        if index is None:
            continue
        pos = index.resolve(club_name)
        if pos is not None:
            found.add(lists_by_comp[fd][pos])
    return found


def _bridge_matches(
    club_name: str,
    comps: list[str],
    canonical: NameIndex,
    canonical_by_name: dict[str, int | None],
    lists_by_comp: dict[str, list[str]],
) -> set[str]:
    """别名桥兜底：TM 名与 hist 名解析到同一 canonical team_id 即接通。"""
    tm_team = canonical.resolve(club_name)
    if tm_team is None:
        return set()
    found: set[str] = set()
    for fd in comps:
        for name in lists_by_comp.get(fd, ()):
            if canonical_by_name[name] == tm_team:
                found.add(name)
    return found


def resolve_club_hist_names(conn: sqlite3.Connection) -> dict[int, str]:
    """
    club_tm_id → hist 英文队名（票 09 复用缝：六轴④底蕴层的 TM 侧身份）。

    与 ``derive_coach_changes`` 同一实现（``_resolve_hist_names``：三级直配
    + 别名桥 + 互撞守卫），俱乐部全集取 tm_coach_tenures（换帅域口径）；
    成员面口径见 ``resolve_season_club_hist_names``。
    """
    clubs = {
        int(row["club_tm_id"]): str(row["club_name"])
        for row in conn.execute(
            "SELECT DISTINCT club_tm_id, club_name FROM tm_coach_tenures"
        ).fetchall()
    }
    return _resolve_hist_names(
        clubs, _load_memberships(conn), _load_hist(conn), alias_index(conn)
    )


def resolve_season_club_hist_names(conn: sqlite3.Connection) -> dict[int, str]:
    """成员面口径：tm_club_seasons 俱乐部全集 → hist 队名（票 09④派生源）。"""
    clubs = {
        int(row["club_tm_id"]): str(row["club_name"])
        for row in conn.execute(
            "SELECT DISTINCT club_tm_id, club_name FROM tm_club_seasons"
        ).fetchall()
    }
    return _resolve_hist_names(
        clubs, _load_memberships(conn), _load_hist(conn), alias_index(conn)
    )


def derive_coach_changes(
    conn: sqlite3.Connection, *, now: str | None = None
) -> dict[str, object]:
    """
    重建换帅事件派生面（幂等：DELETE+INSERT 全量重建，原子）。

    返回统计 dict（events/samples/unresolved 清单）；空输入零事件不炸。
    """
    rebuilt_at = now or utc_now_iso()
    tenures_by_club = _load_tenures(conn)
    memberships = _load_memberships(conn)
    hist = _load_hist(conn)
    canonical = alias_index(conn)
    club_names = {club: name for club, (name, _rows) in tenures_by_club.items()}
    resolved = _resolve_hist_names(club_names, memberships, hist, canonical)
    unresolved = sorted(
        name for club, (name, _rows) in tenures_by_club.items() if club not in resolved
    )
    total_clubs = len(tenures_by_club)
    resolution_rate = (len(resolved) / total_clubs) if total_clubs else 1.0

    # hist 队名 → 该队比赛流（主客皆收）
    resolved_names = set(resolved.values())
    streams: dict[str, list[_HistMatch]] = {}
    for match in hist:
        for team in (match.home_team, match.away_team):
            if team in resolved_names:
                streams.setdefault(team, []).append(match)

    events = 0
    samples = 0
    with atomic(conn):
        conn.execute("DELETE FROM coach_change_samples")
        conn.execute("DELETE FROM coach_change_events")
        for club in sorted(resolved):
            club_name, tenures = tenures_by_club[club]
            hist_name = resolved[club]
            # 前任=最近一段已知指派（空窗场不抹除前任——换帅真发生在空窗内）。
            # 边界只比教练名（复核 F1）：同人 caretaker→manager 转正是连续段，
            # role 只随 (name, role) 元组进 payload。
            prev_known: tuple[str, str] | None = None
            current_event: int | None = None
            games_counted = 0
            for match in streams.get(hist_name, ()):
                cur = assign_manager_day(tenures, match.match_date)
                if cur is not None and (prev_known is None or cur[0] != prev_known[0]):
                    if prev_known is not None:
                        cur_event = conn.execute(
                            """
                            INSERT INTO coach_change_events
                                (club_tm_id, club_name, hist_competition,
                                 hist_season, event_date, outgoing_coach,
                                 outgoing_is_caretaker, incoming_coach,
                                 incoming_is_caretaker, team_id, rebuilt_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                club,
                                club_name,
                                match.competition,
                                match.season,
                                match.match_date,
                                prev_known[0],
                                int(prev_known[1] == "caretaker"),
                                cur[0],
                                int(cur[1] == "caretaker"),
                                canonical.resolve(hist_name),
                                rebuilt_at,
                            ),
                        )
                        current_event = int(cur_event.lastrowid or 0)
                        games_counted = 1
                        events += 1
                        conn.execute(
                            """
                            INSERT INTO coach_change_samples
                                (event_id, hist_match_id, game_no, rebuilt_at)
                            VALUES (?, ?, 1, ?)
                            """,
                            (current_event, match.id, rebuilt_at),
                        )
                        samples += 1
                    else:  # 窗首上任（此前无已知段）：新段但非换帅事件
                        current_event = None
                        games_counted = 0
                elif (
                    cur is not None
                    and current_event is not None
                    and games_counted < SAMPLE_WINDOW
                ):
                    games_counted += 1
                    conn.execute(
                        """
                        INSERT INTO coach_change_samples
                            (event_id, hist_match_id, game_no, rebuilt_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (current_event, match.id, games_counted, rebuilt_at),
                    )
                    samples += 1
                if cur is not None:
                    prev_known = cur
    return {
        "clubs": len(resolved),
        "events": events,
        "samples": samples,
        "unresolved_clubs": unresolved,
        "resolution_rate": round(resolution_rate, 4),
        "identity_status": (
            "ok" if resolution_rate >= IDENTITY_DEGRADED_RATE else "degraded"
        ),
        "rebuilt_at": rebuilt_at,
    }


def coverage_report(conn: sqlite3.Connection) -> dict[str, object]:
    """
    覆盖报告（联赛×赛季）：成员面分母 + 有任职面分子 + 采集现态。

    只读；供 CLI/晨检消费。totals 附派生面现计数。
    """
    rows = conn.execute(
        """
        SELECT cs.league_tm_code AS tm_code, cs.season AS season,
               COUNT(DISTINCT cs.club_tm_id) AS clubs,
               COUNT(DISTINCT t.club_tm_id) AS tenured
        FROM tm_club_seasons cs
        LEFT JOIN tm_coach_tenures t ON t.club_tm_id = cs.club_tm_id
        GROUP BY cs.league_tm_code, cs.season
        ORDER BY cs.league_tm_code, cs.season
        """
    ).fetchall()
    status_by_key = {
        (str(r["league_key"]), str(r["coverage_date"])): str(r["coverage_status"])
        for r in conn.execute(
            """
            SELECT league_key, coverage_date, coverage_status
            FROM source_coverage WHERE source = 'transfermarkt'
            """
        ).fetchall()
    }
    leagues = [
        {
            "league": _TM_CODE_TO_FD.get(row["tm_code"], row["tm_code"]),
            "tm_code": str(row["tm_code"]),
            "season": str(row["season"]),
            "clubs": int(row["clubs"]),
            "clubs_with_tenures": int(row["tenured"]),
            "status": status_by_key.get(
                (str(row["tm_code"]), str(row["season"])), "unknown"
            ),
        }
        for row in rows
    ]
    events = conn.execute("SELECT COUNT(*) AS n FROM coach_change_events").fetchone()
    samples = conn.execute("SELECT COUNT(*) AS n FROM coach_change_samples").fetchone()
    return {
        "leagues": leagues,
        "totals": {
            "league_seasons": len(leagues),
            "events": int(events["n"]),
            "samples": int(samples["n"]),
        },
    }
