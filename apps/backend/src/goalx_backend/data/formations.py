"""
六轴数据 ② 阵型族条件面：vision-build 票 09 落库，mining-loop 票 05 历史回填。

条件面=族 × 赛季 × 教练任期联结：语料 ``match_detail`` 阵型（赛后面）按
两条身份链折到 TM 俱乐部，指派为 manager-of-the-day（教练任期空间
=tm_coach_tenures，autoresearch 票 18 已落）——中英文教练名无映射
（coach_crosscheck 口径），故教练身份一律走 TM 英文名；源T 中文教练名
不入面（PIT 前向的教练可知性=TM 任期现任，非赛后面）：

- 链接链（现行，运行面）：sid → ``source_match_links`` → fixture →
  canonical team → ``team_aliases(transfermarkt)`` → TM 俱乐部。只有
  运行面有 fixture（2026 起），当季行带 team 锚。
- 历史链（票 05，冻结季）：universe 中文名 →（票 78 ``build_name_bridge``
  配对桥，语料侧日=UTC 日）→ hist 英文名 →（``resolve_club_hist_names``
  逆）→ TM 俱乐部。历史行 team_id=NULL 是事实不是缺（读缝键=coach×season）。
  当季（universe 最大季）未链 sid 不走历史链（live_season_unlinked 计数），
  冻结后自然并入——当季单元格继续由链接链独占，已交付行不被历史输入扰动。

PIT 契约：本面来自赛后面 ``match_detail``（阵型赛后才知道），回答的问题
是「该教练在该赛季惯用什么族」——作特征时 kickoff 前可知性=教练任期+
历史惯用族（读缝 ``coach_family_prior`` 只取查询时点前已完结的格子，
绝不含当场面；prematch 拍是另一条前向腿，不在此面）。

写相=差分写（票 05）：逐格与库内行比对，业务字段全同的行原样保留
（rebuilt_at 不动），新格插入、消失格删除、漂移格改写——幂等重跑零
效果，回填不重写已交付行。

全联赛落库（fd 11 项映射外 league_fd=NULL 照落），切片报告只出英超+英冠
（票 08 人裁=全范围英超优先探索）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import cast

import duckdb

from goalx_backend.data.coach_changes import (
    Tenure,
    assign_manager_day,
    resolve_club_hist_names,
)
from goalx_backend.data.ingest.elo_silver import FD_LEAGUE_ZH
from goalx_backend.data.leagues import LEAGUE_TO_FD
from goalx_backend.data.mapping import load_universe_results
from goalx_backend.data.silver import season_of
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.modelling import elo
from goalx_backend.modelling.team_align import transfermarkt_aliases_by_team

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

#: 逐季死区标注阈：阵型已知侧占比低于此值 = 源侧缺名单死区段（事实非缺口）
DEAD_ZONE_KNOWN_RATIO = 0.5

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
    """一个条件格子的聚合态（计数+首末日+队锚；历史链行无 team 锚）。"""

    n: int
    first: str
    last: str
    team_id: int | None
    club_name: str


def _corpus_sides(
    duck_con: DuckCon,
) -> tuple[Iterator[dict[str, object]], int, int]:
    """
    语料侧观察流：match_detail × fixture_universe（sid 链）逐场两侧。

    返回 (行迭代, 语料总场数, 开球缺失场数)。season 取 universe hive 分区
    （8 月界），league 取中文名（fd 映射在落库时折算）；day 取
    match_detail 开球 UTC 日期（duckdb 会话时区会影响 TIMESTAMPTZ 直
    CAST，须 AT TIME ZONE 'UTC' 归一——欧洲晚场 20:00Z 不漂日）。
    universe 缺席的孤儿 sid 计入总数但不出行（无赛季轴，诚实丢弃——
    universe_missing）；kickoff NULL 场独立分桶（correctness-review F8，
    不混入 universe_missing）。
    """
    rows = duck_con.execute(
        """
        SELECT d.sid AS sid,
               CAST(d.kickoff AT TIME ZONE 'UTC' AS DATE) AS day,
               d.home_formation AS home_formation,
               d.away_formation AS away_formation,
               f.season AS season,
               f.league AS league,
               f.home AS home,
               f.away AS away
        FROM match_detail d
        JOIN fixture_universe f ON f.sid = d.sid
        WHERE d.kickoff IS NOT NULL
        ORDER BY d.kickoff, d.sid
        """
    ).fetchall()
    seen = duck_con.execute("SELECT COUNT(*) FROM match_detail").fetchone()
    total = int(seen[0]) if seen is not None else 0
    nulls = duck_con.execute(
        "SELECT COUNT(*) FROM match_detail WHERE kickoff IS NULL"
    ).fetchone()
    kickoff_missing = int(nulls[0]) if nulls is not None else 0

    def stream() -> Iterator[dict[str, object]]:
        for sid, day, home_f, away_f, season, league, home, away in rows:
            yield {
                "sid": str(sid),
                "day": str(day),
                "home_formation": None if home_f is None else str(home_f),
                "away_formation": None if away_f is None else str(away_f),
                "season": str(season),
                "league": str(league),
                "home": str(home),
                "away": str(away),
            }

    return stream(), total, kickoff_missing


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


def _tm_clubs(
    conn: sqlite3.Connection,
) -> tuple[dict[int, tuple[str, ...]], dict[str, int]]:
    """
    TM 身份两查（team→TM 俱乐部名全集 / 俱乐部名→club_tm_id）。

    别名桥经 team_align 读口（transfermarkt，ADR-0008 属主）；club_name
    撞多名俱乐部（TM 重命名窗口等）→ 不硬配，整名丢弃（计数）。team 的
    多别名由派生侧逐名试解（correctness-review F7：旧名先落库但任期面
    只挂新名时不再整队 no_club）。
    """
    aliases = transfermarkt_aliases_by_team(conn)
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
    return aliases, club_ids


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


def _bridge_zh_to_club(
    conn: sqlite3.Connection,
    duck_con: DuckCon,
) -> tuple[dict[str, tuple[int, str]], set[str], str | None, dict[str, object]]:
    """
    历史链身份折（票 05）：universe 中文名 → TM 俱乐部。

    链=票 78 ``build_name_bridge``（配对键=联赛+日+比分，双侧唯一才投，
    fd→zh 得票唯一）→ 逆折 zh→fd（fd 名撞多 zh 名者弃）→
    ``resolve_club_hist_names`` 逆（hist 名 → club，互撞守卫同源）。
    返回 (zh→club 映射, 桥通名集合〔=zh_to_fd 键域，供 hist_no_bridge/
    hist_no_club 分桶〕, 当季=universe 最大季〔冻结界；语料空=None〕,
    桥报告)。语料侧日=**UTC 日**（kickoff 北京墙钟 −8h；票 78 桥原调用的
    语料侧北京日不与 hist 欧洲日配晚间场，此处按欧洲日重投）。当季不走
    MAX(season) 聚合——duckdb 统计传播器对 hive 列聚合有内部断言坑
    （vector index 越界），逐行 max(kickoff) 折季同值且稳。
    """
    universe_rows = load_universe_results(duck_con)
    corpus_rows = [
        elo.BridgeRow(
            league=row.league,
            day=(row.kickoff_bj - timedelta(hours=8)).date(),
            home=row.home,
            away=row.away,
            home_goals=int(row.home_goals),
            away_goals=int(row.away_goals),
        )
        for row in universe_rows
        if row.home_goals is not None and row.away_goals is not None
    ]
    live_kickoff = max((row.kickoff_bj for row in universe_rows), default=None)
    live_season = season_of(live_kickoff) if live_kickoff is not None else None
    hist_rows = [
        elo.BridgeRow(
            league=league,
            day=date.fromisoformat(str(row["match_date"])),
            home=str(row["home_team"]),
            away=str(row["away_team"]),
            home_goals=int(row["fthg"]),
            away_goals=int(row["ftag"]),
        )
        for row in conn.execute(
            """
            SELECT competition, match_date, home_team, away_team, fthg, ftag
            FROM hist_matches
            """
        ).fetchall()
        if (league := FD_LEAGUE_ZH.get(str(row["competition"]))) is not None
    ]
    fd_to_zh, bridge_report = elo.build_name_bridge(hist_rows, corpus_rows)
    fd_by_zh: dict[str, set[str]] = {}
    for fd_name, zh_name in fd_to_zh.items():
        fd_by_zh.setdefault(zh_name, set()).add(fd_name)
    zh_to_fd = {
        zh_name: next(iter(fd_names))
        for zh_name, fd_names in fd_by_zh.items()
        if len(fd_names) == 1
    }
    # hist 名 → (club_tm_id, club_name)：resolver 逆 + 任期面行数众数名
    club_names: dict[int, str] = {}
    name_votes: dict[int, dict[str, int]] = {}
    for row in conn.execute(
        """
        SELECT club_tm_id, club_name, COUNT(*) AS n
        FROM tm_coach_tenures GROUP BY 1, 2
        """
    ).fetchall():
        club = int(row["club_tm_id"])
        name_votes.setdefault(club, {})[str(row["club_name"])] = int(row["n"])
    for club, votes in name_votes.items():
        club_names[club] = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    zh_to_club: dict[str, tuple[int, str]] = {}
    club_of_hist_name = {
        hist_name: club for club, hist_name in resolve_club_hist_names(conn).items()
    }
    for zh_name, fd_name in zh_to_fd.items():
        club = club_of_hist_name.get(fd_name)
        if club is not None and club in club_names:
            zh_to_club[zh_name] = (club, club_names[club])
    stats: dict[str, object] = {
        "fd_to_zh": len(fd_to_zh),
        "zh_to_fd_unique": len(zh_to_fd),
        "zh_to_club": len(zh_to_club),
        **{k: v for k, v in bridge_report.items() if k != "vote_distribution"},
    }
    return zh_to_club, set(zh_to_fd), live_season, stats


#: 差分写的业务列（id/rebuilt_at 除外）——键序=UNIQUE 约束列序
_CELL_COLS: tuple[str, ...] = (
    "season",
    "league_fd",
    "league_zh",
    "team_id",
    "club_tm_id",
    "club_name",
    "coach_name",
    "coach_role",
    "family",
    "n_matches",
    "n_known",
    "first_match_date",
    "last_match_date",
)


def _write_facet(
    conn: sqlite3.Connection,
    cells: dict[tuple[str, str, int, str, str], _Cell],
    group_known: dict[tuple[str, str, int, str], int],
    group_last: dict[tuple[str, str, int, str], tuple[str, str]],
    rebuilt_at: str,
) -> dict[str, int]:
    """
    差分写（票 05）。

    业务字段全同的行原样保留，新格插入、消失格删除、漂移格改写。返回
    {preserved, inserted, updated, deleted}——幂等重跑=preserved 全量、
    余三零；preserved 行 rebuilt_at 逐字节不动（票面验收：既有交付行
    不得重写）。
    """
    col_list = ", ".join(_CELL_COLS)
    existing: dict[tuple[str, str, int, str, str], sqlite3.Row] = {}
    for row in conn.execute(
        f"SELECT id, {col_list} FROM formation_family_facet"  # noqa: S608 — 列名=模块常量
    ).fetchall():
        existing[
            (
                str(row["season"]),
                str(row["league_zh"]),
                int(row["club_tm_id"]),
                str(row["coach_name"]),
                str(row["family"]),
            )
        ] = row
    counts = {"preserved": 0, "inserted": 0, "updated": 0, "deleted": 0}
    for key in sorted(cells):
        season, league_zh, club_tm_id, coach, family = key
        cell = cells[key]
        values = (
            season,
            _LEAGUE_FD.get(league_zh),
            league_zh,
            cell.team_id,
            club_tm_id,
            cell.club_name,
            coach,
            group_last[(season, league_zh, club_tm_id, coach)][1],
            family,
            cell.n,
            group_known[(season, league_zh, club_tm_id, coach)],
            cell.first,
            cell.last,
        )
        old = existing.pop(key, None)
        if old is None:
            conn.execute(
                f"""
                INSERT INTO formation_family_facet ({col_list}, rebuilt_at)
                VALUES ({", ".join("?" * len(_CELL_COLS))}, ?)
                """,  # noqa: S608 — 表/列名=模块常量，无用户输入
                (*values, rebuilt_at),
            )
            counts["inserted"] += 1
            continue
        if any(
            old[name] != value for name, value in zip(_CELL_COLS, values, strict=True)
        ):
            conn.execute(
                f"""
                UPDATE formation_family_facet
                SET {", ".join(f"{name} = ?" for name in _CELL_COLS)}, rebuilt_at = ?
                WHERE id = ?
                """,  # noqa: S608 — 列名=模块常量，无用户输入
                (*values, rebuilt_at, int(old["id"])),
            )
            counts["updated"] += 1
        else:
            counts["preserved"] += 1
    for row in existing.values():
        conn.execute(
            "DELETE FROM formation_family_facet WHERE id = ?", (int(row["id"]),)
        )
        counts["deleted"] += 1
    return counts


def _resolve_club(
    team_id: int,
    aliases: dict[int, tuple[str, ...]],
    club_ids: dict[str, int],
    cache: dict[int, tuple[int, str]],
) -> tuple[str, tuple[int, str] | None]:
    """
    Team → TM 俱乐部（多别名逐名试解，correctness-review F7）。

    返回 (drops 键, 结果)：``"ok"``+``(club_tm_id, 命中名)``——任一别名指
    到唯一俱乐部即接通（TM 改名窗口内旧名先落库、任期面挂新名不再整队
    no_club）；无别名=``no_alias``；全别名不中=``no_club``（键与 drops
    桶同名，直接计数）。队级缓存——决议一场内不变。
    """
    cached = cache.get(team_id)
    if cached is not None:
        return ("ok", cached)
    names = aliases.get(team_id)
    if not names:
        return ("no_alias", None)
    for name in names:
        if name in club_ids:
            club = (club_ids[name], name)
            cache[team_id] = club
            return ("ok", club)
    return ("no_club", None)


def _side_club(
    team_id: int | None,
    zh_name: str | None,
    hist_club: tuple[int, str] | None,
    aliases_by_team: dict[int, tuple[str, ...]],
    club_ids: dict[str, int],
    resolved_club: dict[int, tuple[int, str]],
    zh_bridged: set[str],
) -> tuple[tuple[int, str] | None, str | None]:
    """
    单侧 → TM 俱乐部（两链合一）：返回 (俱乐部, 丢弃桶键|None)。

    链接链（team_id 有值）失败桶=no_alias/no_club；历史链（team_id=None）
    失败桶=hist_no_bridge（桥都没通）/hist_no_club（桥通但 hist 名无 TM
    俱乐部认领——TM 宇宙现役快照缺口，票 06 映射率报告同根因）。
    """
    if team_id is not None:
        status, club = _resolve_club(team_id, aliases_by_team, club_ids, resolved_club)
        return (club if status == "ok" else None), (None if status == "ok" else status)
    if hist_club is None:
        bridged = zh_name in zh_bridged if zh_name is not None else False
        return None, ("hist_no_club" if bridged else "hist_no_bridge")
    return hist_club, None


def _match_side_list(
    match: dict[str, object],
    linked: tuple[int, int] | None,
    zh_club: dict[str, tuple[int, str]],
) -> list[tuple[int | None, object, str | None, tuple[int, str] | None]]:
    """
    一场的两侧输入：(team_id|None, 阵型, universe 中文名, 历史链俱乐部|None)。

    链接链（linked 有值）带 team 锚；历史链带 universe 中文名与其桥接
    俱乐部（未通=None，由 ``_side_club`` 分桶）。
    """
    if linked is not None:
        return [
            (linked[0], match["home_formation"], None, None),
            (linked[1], match["away_formation"], None, None),
        ]
    return [
        (
            None,
            match["home_formation"],
            str(match["home"]),
            zh_club.get(str(match["home"])),
        ),
        (
            None,
            match["away_formation"],
            str(match["away"]),
            zh_club.get(str(match["away"])),
        ),
    ]


@dataclass
class _Accum:
    """derive 的聚合态（格子/分母/末场/丢弃计数/任期面，全就地累加）。"""

    cells: dict[tuple[str, str, int, str, str], _Cell]
    group_known: dict[tuple[str, str, int, str], int]
    group_last: dict[tuple[str, str, int, str], tuple[str, str]]
    tenures: dict[int, list[Tenure]]
    drops: dict[str, int]

    @classmethod
    def fresh(cls, tenures: dict[int, list[Tenure]]) -> _Accum:
        return cls(
            cells={},
            group_known={},
            group_last={},
            tenures=tenures,
            drops={
                "no_alias": 0,
                "no_club": 0,
                "unattributed": 0,
                "formation_unknown": 0,
                "hist_no_bridge": 0,
                "hist_no_club": 0,
                "live_season_unlinked": 0,
            },
        )


def _absorb_side(
    accum: _Accum,
    *,
    day: str,
    season: str,
    league: str,
    team_id: int | None,
    formation: object,
    club: tuple[int, str],
) -> None:
    """一侧已解析到俱乐部后的聚合：任期指派 → 族格累加（drop 就地计数）。"""
    assigned = assign_manager_day(accum.tenures.get(club[0], []), day)
    if assigned is None:
        accum.drops["unattributed"] += 1
        return
    coach, role = assigned
    group = (season, league, club[0], coach)
    accum.group_last[group] = (day, role)
    family = family_of(formation if isinstance(formation, str) else None)
    if family is None:
        accum.drops["formation_unknown"] += 1
        return
    accum.group_known[group] = accum.group_known.get(group, 0) + 1
    cell = accum.cells.setdefault(
        (*group, family),
        _Cell(n=0, first=day, last=day, team_id=team_id, club_name=club[1]),
    )
    cell.n += 1
    cell.first = min(cell.first, day)
    cell.last = max(cell.last, day)


def derive_formation_family_facet(
    conn: sqlite3.Connection,
    duck_con: DuckCon,
    *,
    now: str | None = None,
) -> dict[str, object]:
    """
    重建阵型族条件面（差分写幂等：业务字段全同行保留，见 ``_write_facet``）。

    粒度=(season, league_zh, club_tm_id, coach_name, family)。链接链（sid→
    fixture→team→TM，现行）之外，未链 sid 在**冻结季**（season < 当季=
    universe 最大季）走历史链（zh→桥→hist 名→TM，票 05）。丢弃口径（进
    stats，诚实现态，除注明外均按**侧**计）：universe_missing=detail 场不在
    universe（无赛季轴，按场）；kickoff_missing=开球缺失（无日期轴，
    correctness-review F8 独立分桶，按场）；live_season_unlinked=当季未链
    sid（当季单元格归链接链独占，冻结后并入，按侧）；hist_no_bridge=冻结
    季中文名不在配对桥；hist_no_club=桥通但 hist 名无 TM 俱乐部认领；
    no_alias/no_club=链接链别名缺口（原口径）；unattributed=当日无覆盖任期；
    formation_unknown=指派成功但阵型缺失（不进任何族分母）。空输入零行不炸。
    """
    rebuilt_at = now or utc_now_iso()
    sides, matches_seen, kickoff_missing = _corpus_sides(duck_con)
    links = _linked_sides(conn)
    aliases_by_team, club_ids = _tm_clubs(conn)
    accum = _Accum.fresh(_tenures_by_club(conn))
    zh_club, zh_bridged, live, bridge_stats = _bridge_zh_to_club(conn, duck_con)
    live = live or ""
    # 格子键=(season, league_zh, club_tm_id, coach, family) → 计数/首末日；
    # 组键=(season, league_zh, club_tm_id, coach) → 分母（已知阵型场数）与
    # 末场（角色取末场指派，同人转正口径与 coach_changes 一致）
    resolved_club: dict[int, tuple[int, str]] = {}  # team_id → (club_tm_id, 名)
    season_sides: dict[str, dict[str, int]] = {}
    joined = 0
    for match in sides:
        joined += 1
        day = str(match["day"])
        season = str(match["season"])
        season_stat = season_sides.setdefault(season, {"sides": 0, "known": 0})
        linked = links.get(str(match["sid"]))
        if linked is None and season >= live:
            accum.drops["live_season_unlinked"] += 2
            season_stat["sides"] += 2
            continue
        for team_id, formation, zh_name, hist_club in _match_side_list(
            match, linked, zh_club
        ):
            season_stat["sides"] += 1
            if formation:
                season_stat["known"] += 1
            club, drop_key = _side_club(
                team_id,
                zh_name,
                hist_club,
                aliases_by_team,
                club_ids,
                resolved_club,
                zh_bridged,
            )
            if club is None:
                accum.drops[drop_key or "no_club"] += 1
                continue
            _absorb_side(
                accum,
                day=day,
                season=season,
                league=str(match["league"]),
                team_id=team_id,
                formation=formation,
                club=club,
            )
    stats: dict[str, object] = {
        "matches_seen": matches_seen,
        **accum.drops,
        "kickoff_missing": kickoff_missing,
        "universe_missing": matches_seen - kickoff_missing - joined,
        "rows": len(accum.cells),
        "club_season_leagues": len(accum.group_known),
        "coach_club_pairs": len({(k[2], k[3]) for k in accum.group_known}),
        "live_season": live or None,
        "seasons": season_sides,
        **{f"bridge_{k}": v for k, v in bridge_stats.items()},
        "rebuilt_at": rebuilt_at,
    }
    with atomic(conn):
        write_counts = _write_facet(
            conn, accum.cells, accum.group_known, accum.group_last, rebuilt_at
        )
        stats.update(write_counts)
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
    覆盖报告（只读）：全联赛行数 + 英超/英冠切片 + 逐季行数（票 05 验收③）。

    leagues 按 (league_fd, league_zh) 分桶（fd 映射外 league_fd=NULL 照报）；
    seasons 逐季行数/场数 + 死区标注（known/sides < 0.5 = 源侧无名单死区段
    ——零行不是缺口是事实；零行季若早于 hist 窗起点，注 pre_hist_window）；
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
    seasons = [
        {
            "season": str(row["season"]),
            "rows": int(row["rows"]),
            "matches": int(row["matches"]),
            "known": int(row["known"]),
        }
        for row in conn.execute(
            """
            SELECT season, COUNT(*) AS rows, SUM(n_matches) AS matches,
                   SUM(n_known) AS known
            FROM formation_family_facet
            GROUP BY season ORDER BY season
            """
        ).fetchall()
    ]
    state = conn.execute(
        "SELECT stats FROM face_derive_state WHERE face = ?", (FACE_NAME,)
    ).fetchone()
    derived_pending = state is None
    parsed: dict[str, object] = (
        json.loads(str(state["stats"])) if state is not None else {}
    )
    raw_sides = parsed.get("seasons")
    season_sides = (
        cast("dict[str, dict[str, int]]", raw_sides)
        if isinstance(raw_sides, dict)
        else {}
    )
    hist_min_row = conn.execute("SELECT MIN(match_date) FROM hist_matches").fetchone()
    hist_min = str(hist_min_row[0]) if hist_min_row and hist_min_row[0] else ""
    # hist 窗起点折季标（8 月界，与 silver.season_of 同界）：起点季之前的
    # universe 季按定义无 hist 对侧 → 历史链零行是窗前季，非死区
    hist_min_season = season_of(datetime.fromisoformat(hist_min)) if hist_min else ""
    for item in seasons:
        season_key = str(item["season"])
        sides_stat = season_sides.get(season_key, {})
        n_sides = sides_stat.get("sides", 0)
        ratio = sides_stat.get("known", 0) / n_sides if n_sides > 0 else None
        if item["rows"] == 0 and hist_min_season and season_key < hist_min_season:
            item["note"] = "pre_hist_window（hist 窗前季，历史链不适用）"
        elif ratio is not None and ratio < DEAD_ZONE_KNOWN_RATIO:
            item["note"] = "formation_dead_zone（源侧缺名单死区，零行是事实）"
    totals: dict[str, object] = {
        "rows": sum(int(item["rows"]) for item in leagues),
        **{
            key: parsed.get(key, 0)
            for key in (
                "matches_seen",
                "live_season_unlinked",
                "hist_no_bridge",
                "hist_no_club",
                "no_alias",
                "no_club",
                "unattributed",
                "formation_unknown",
            )
        },
        "rebuilt_at": parsed.get("rebuilt_at"),
    }
    return {
        "leagues": leagues,
        "seasons": seasons,
        "slice": list(SLICE_COMPETITIONS),
        # 优先切片（票 08 人裁：英超优先探索、英冠=可购面最大）——
        # E0/E1 的切片行单独成节，消费方免再过滤
        "priority_slice": [
            item for item in leagues if item["league_fd"] in SLICE_COMPETITIONS
        ],
        "totals": totals,
        "derived_pending": derived_pending,
    }
