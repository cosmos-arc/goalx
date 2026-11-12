"""
六轴数据 ② 阵型族条件面：vision-build 票 09 落库，票 10 身份键全切源T。

（mining-loop 票 05 历史回填；spec-srct-cutover。）

条件面=族 × 赛季 × 教练任期联结：语料 ``match_detail`` 阵型（赛后面）经
**同站中文名折**（universe/match_detail 中文名 ↔ ``srct_club_seasons``
matchResult 逐季枚举，同站同季同语——data/srct_identity）折到源T team_id，
指派为 manager-of-the-day（任期空间=srct_coach_tenures，coach_key=英文名
优先键）。TM 六跳链（票 05 时代）已拆——身份主空间=源T，ADR-0014：

- 已链 sid（链接链，2026 起）仍带运行面 team 锚（``source_match_links`` →
  fixture team_id，仅作 team_id 列，不参与俱乐部身份）；俱乐部身份一律
  同站折。当季（detail 覆盖最大季）未链 sid 不入面（live_season_unlinked
  计数），冻结后自然并入——当季单元格继续由链接链独占，已交付行不被
  历史输入扰动。
- 非 GB 诚实收缩：源T tenure 语料 GB 双联赛先行，非 GB 侧折不出 →
  ``no_srct_identity`` 桶（facet 从全联赛面缩到 GB 覆盖面；批量扩域后
  零新基建回流，spec story 15）。

PIT 契约：本面来自赛后面 ``match_detail``（阵型赛后才知道），回答的问题
是「该教练在该赛季惯用什么族」——作特征时 kickoff 前可知性=教练任期+
历史惯用族（读缝 ``coach_family_prior`` 只取查询时点前已完结的格子，
绝不含当场面；prematch 拍是另一条前向腿，不在此面）。

写相=差分写（票 05）：逐格与库内行比对，业务字段全同的行原样保留
（rebuilt_at 不动），新格插入、消失格删除、漂移格改写——幂等重跑零
效果，回填不重写已交付行。票 10 换键（v62）后首次 derive 诚实报
preserved=0/inserted=N（全量重派口径，不得粉饰）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import cast

import duckdb

from goalx_backend.data.coach_changes import Tenure, assign_manager_day
from goalx_backend.data.leagues import LEAGUE_TO_FD
from goalx_backend.data.srct_identity import (
    facet_season_of,
    srct_club_index,
    srct_season_of,
    srct_tenures_by_team,
)
from goalx_backend.db import atomic, utc_now_iso

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
) -> tuple[list[dict[str, object]], int, int]:
    """
    语料侧观察面：match_detail × fixture_universe（sid 链）逐场两侧。

    返回 (行列表〔票 05 起 derive 物化消费：当季界=detail 覆盖最大季 +
    逐场归账，约 8 万 dict〕, 语料总场数, 开球缺失场数)。season 取
    universe hive 分区
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
    sides: list[dict[str, object]] = [
        {
            "sid": str(sid),
            "day": str(day),
            "home_formation": None if home_f is None else str(home_f),
            "away_formation": None if away_f is None else str(away_f),
            "season": str(season),
            "league": str(league),
            "home": str(home),
            "away": str(away),
        }
        for sid, day, home_f, away_f, season, league, home, away in rows
    ]
    return sides, total, kickoff_missing


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


#: 差分写的业务列（id/rebuilt_at 除外）；写键=(season, league_zh,
#: club_srct_id, coach_key, family)=表 UNIQUE 约束列（_CELL_COLS 的子序）
_CELL_COLS: tuple[str, ...] = (
    "season",
    "league_fd",
    "league_zh",
    "team_id",
    "club_srct_id",
    "club_name",
    "coach_key",
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
    不得重写）。票 10 换键后首拍 preserved≈0/inserted=N 是口径事实。
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
                int(row["club_srct_id"]),
                str(row["coach_key"]),
                str(row["family"]),
            )
        ] = row
    counts = {"preserved": 0, "inserted": 0, "updated": 0, "deleted": 0}
    for key in sorted(cells):
        season, league_zh, club_srct_id, coach, family = key
        cell = cells[key]
        values = (
            season,
            _LEAGUE_FD.get(league_zh),
            league_zh,
            cell.team_id,
            club_srct_id,
            cell.club_name,
            coach,
            group_last[(season, league_zh, club_srct_id, coach)][1],
            family,
            cell.n,
            group_known[(season, league_zh, club_srct_id, coach)],
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


def _side_list(
    match: dict[str, object],
    linked: tuple[int, int] | None,
) -> list[tuple[int | None, object, str]]:
    """
    一场的两侧输入：(team_id fd 锚|None, 阵型, corpus 中文名)。

    俱乐部身份对链接/未链一律走 corpus 中文名 → 同站折（票 10：不再有
    TM 侧身份链）；team 锚只来自链接链。
    """
    return [
        (
            linked[0] if linked is not None else None,
            match["home_formation"],
            str(match["home"]),
        ),
        (
            linked[1] if linked is not None else None,
            match["away_formation"],
            str(match["away"]),
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
                "no_srct_identity": 0,
                "unattributed": 0,
                "formation_unknown": 0,
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
    """一侧已同站折到源T 队后的聚合：任期指派 → 族格累加（drop 就地计数）。"""
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

    粒度=(season, league_zh, club_srct_id, coach_key, family)。俱乐部身份=
    corpus 中文名同站折（``srct_club_seasons`` 逐季枚举，data/srct_identity）；
    任期=``srct_coach_tenures`` manager-of-the-day。当季=detail 覆盖的最大季
    ——universe 预挂下季赛程不会提前翻界（detail 由夜班赛后采，出现即季已
    开踢），当季单元格归链接链独占（未链 sid 入 live_season_unlinked），
    已交付行不被历史输入扰动。丢弃口径（进 stats，诚实现态，除注明外均按
    **侧**计）：universe_missing=detail 场不在 universe（无赛季轴，按场）；
    kickoff_missing=开球缺失（无日期轴，correctness-review F8 独立分桶，
    按场）；live_season_unlinked=当季未链 sid（按侧）；no_srct_identity=
    该季枚举折不出（非 GB 面=语料范围外，spec story 15 扩域回流；GB 内
    为真缺口，人审线在 coverage report）；unattributed=当日无覆盖任期；
    formation_unknown=指派成功但阵型缺失（不进任何族分母）。空输入零行
    不炸。
    """
    rebuilt_at = now or utc_now_iso()
    match_rows, matches_seen, kickoff_missing = _corpus_sides(duck_con)
    links = _linked_sides(conn)
    club_index = srct_club_index(conn)
    accum = _Accum.fresh(srct_tenures_by_team(conn))
    live = max((str(m["season"]) for m in match_rows), default="")
    # 格子键=(season, league_zh, club_srct_id, coach, family) → 计数/首末日；
    # 组键=(season, league_zh, club_srct_id, coach) → 分母（已知阵型场数）与
    # 末场（角色取末场指派，源T role 域恒 manager——caretaker 分类随 TM 退役）
    season_sides: dict[str, dict[str, int]] = {}
    joined = 0
    for match in match_rows:
        joined += 1
        day = str(match["day"])
        season = str(match["season"])
        season_stat = season_sides.setdefault(season, {"sides": 0, "known": 0})
        # 逐季计数按场前置：阵型可得性与身份折成败无关（当季未链不压低比率）
        season_stat["sides"] += 2
        season_stat["known"] += (1 if match["home_formation"] else 0) + (
            1 if match["away_formation"] else 0
        )
        linked = links.get(str(match["sid"]))
        if linked is None and season >= live:
            accum.drops["live_season_unlinked"] += 2
            continue
        for team_id, formation, zh_name in _side_list(match, linked):
            club = club_index.resolve(srct_season_of(season), zh_name)
            if club is None:
                accum.drops["no_srct_identity"] += 1
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
        "srct_name_conflicts": club_index.conflicts,
        "live_season": live or None,
        "seasons": season_sides,
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
    dominant_family 并列取字典序稳定者。键空间（票 10 起）=源T
    coach_key（英文名优先）——参数名 ``coach_name`` 沿用（零接口改动，
    spec 缝策略）。
    """
    if on_day is not None:
        rows = conn.execute(
            """
            SELECT season, league_zh, club_srct_id, family, n_matches, n_known
            FROM formation_family_facet
            WHERE coach_key = ? AND season <= ? AND last_match_date < ?
            """,
            (coach_name, upto_season, on_day),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT season, league_zh, club_srct_id, family, n_matches, n_known
            FROM formation_family_facet
            WHERE coach_key = ? AND season < ?
            """,
            (coach_name, upto_season),
        ).fetchall()
    cells: dict[str, int] = {}
    group_known: dict[tuple[str, str, int], int] = {}
    for row in rows:
        cells[str(row["family"])] = cells.get(str(row["family"]), 0) + int(
            row["n_matches"]
        )
        group = (str(row["season"]), str(row["league_zh"]), int(row["club_srct_id"]))
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
    ——零行不是缺口是事实；零行季若早于源T 队籍窗起点，注 pre_srct_window）；
    totals 的丢弃计数取最近一次派生现态（face_derive_state；从未派生=
    derived_pending）。票 10 非 GB 收缩在 totals.no_srct_identity 显形
    （128 家非 GB 俱乐部行随切换消失，扩域回流，spec story 15）。
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
                   COUNT(DISTINCT club_srct_id) AS clubs,
                   COUNT(DISTINCT coach_key || '|' || season) AS coach_seasons,
                   SUM(n_matches) AS matches, SUM(n_known) AS known
            FROM formation_family_facet
            GROUP BY league_fd, league_zh
            ORDER BY league_fd IS NULL, league_fd, league_zh
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
    by_season_rows = {
        str(row["season"]): row
        for row in conn.execute(
            """
            SELECT season, COUNT(*) AS rows, SUM(n_matches) AS matches,
                   SUM(n_known) AS known
            FROM formation_family_facet
            GROUP BY season
            """
        ).fetchall()
    }
    # 逐季全集=面行 ∪ 派生态语料季——死区季零行也要出场（零行是事实不是
    # 缺口，票面验收③）
    seasons = [
        {
            "season": season_key,
            "rows": int(by_season_rows[season_key]["rows"])
            if season_key in by_season_rows
            else 0,
            "matches": int(by_season_rows[season_key]["matches"])
            if season_key in by_season_rows
            else 0,
            "known": int(by_season_rows[season_key]["known"])
            if season_key in by_season_rows
            else 0,
        }
        for season_key in sorted({*by_season_rows, *season_sides})
    ]
    srct_min_row = conn.execute(
        "SELECT MIN(season) AS s FROM srct_club_seasons"
    ).fetchone()
    srct_min = str(srct_min_row["s"]) if srct_min_row and srct_min_row["s"] else ""
    # 源T 队籍窗起点折季标（票 10：facet 身份链不再走 hist——窗前=同站
    # 枚举不存在的季，零行是窗前季，非死区）
    srct_min_season = facet_season_of(srct_min) if srct_min else ""
    for item in seasons:
        season_key = str(item["season"])
        sides_stat = season_sides.get(season_key, {})
        n_sides = sides_stat.get("sides", 0)
        ratio = sides_stat.get("known", 0) / n_sides if n_sides > 0 else None
        if item["rows"] == 0 and srct_min_season and season_key < srct_min_season:
            item["note"] = "pre_srct_window（源T 队籍窗前季，同站枚举不适用）"
        elif ratio is not None and ratio < DEAD_ZONE_KNOWN_RATIO:
            item["note"] = "formation_dead_zone（源侧缺名单死区，零行是事实）"
    totals: dict[str, object] = {
        "rows": sum(int(item["rows"]) for item in leagues),
        **{
            key: parsed.get(key, 0)
            for key in (
                "matches_seen",
                "live_season_unlinked",
                "no_srct_identity",
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
