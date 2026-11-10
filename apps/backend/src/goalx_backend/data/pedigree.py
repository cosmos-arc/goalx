"""
六轴数据 ④ 底蕴硬层四因子面（vision-build 票 09；人裁 5 两层拆分的硬层）。

四因子独立列不强行合成（合成与否留给 facet 读数）：

- 顶级联赛连续年限：``tm_club_seasons`` 派生——**截至上一赛季**（PIT：
  该赛季开始前可知；观察窗 2016 起，窗首连续段为下界，诚实有界口径）；
- 近 5 季积分百分位：``hist_matches`` 派生（无 standings 表，逐场 3-1-0
  算积分分享率 = 积分/(3×场次)，跨联赛可比）——只用该赛季前 5 季，无
  前季 NULL 不造率；
- 荣誉计数：``HONOURS`` 受控常量（免费静态源，全史口径任一时点可知）：
  顶级联赛冠军数 + 总重要荣誉数（=顶级冠军+欧战主要冠军[欧冠/欧联/
  欧洲超级杯/优胜者杯/洲际杯/世俱杯]；国内杯不计入，口径写死在常量）；
- 升降级频谱：``tm_club_seasons`` 派生——观察窗（库内 tm 赛季全局 min/
  max）内的 tier1 出入计数，**纯后视窗**（转换对 t→t+1 只计 t+1 ≤ 上一
  赛季者）：tier1→非 tier1（含离开受采面）=降级类，反向=升级类；升级进
  本季的信号落在下一季行上（保守口径，PIT 无歧义）。

行触发 = (team, season) 在 hist 出现（fd 面键空间=hist 英文队名，backtest
EM 扫描直读）；TM 侧身份经 ``coach_changes.resolve_season_club_hist_names``
（三级直配+别名桥+互撞守卫）解析，未解析队年限/频谱=NULL（诚实缺，不当
0 用），积分/荣誉照常。
"""

from __future__ import annotations

import json
import sqlite3

from goalx_backend.data.coach_changes import resolve_season_club_hist_names
from goalx_backend.data.ingest.tm_coach import TM_LEAGUES
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.modelling.dc_model import TIER1_COMPETITIONS
from goalx_backend.modelling.team_align import normalize_team_name

FACE_NAME = "club_pedigree"
#: 近 5 季窗口（票面：近 5 季积分百分位）
L5_WINDOW = 5

#: TM 顶级联赛码（TIER1_COMPETITIONS fd 码 → TM 码；真源=dc_model 常量）
_TIER1_TM: frozenset[str] = frozenset(TM_LEAGUES[fd] for fd in TIER1_COMPETITIONS)

# ---------------------------------------------------------------------------
# 荣誉静态源（受控常量；markets/selections 种子先例）
#
# 口径：顶级联赛冠军（各国顶级联赛全史；英格兰=老英甲+英超，德国=德甲
# 前+德甲，荷兰含 1956 前全国冠军，苏格兰含共享冠军）+ 欧战主要冠军
# （欧洲冠军杯/欧冠、联盟杯/欧联、欧洲超级杯、欧洲优胜者杯、洲际杯、
# 世俱杯）。国内杯不计。major_honours = 冠军数 + 欧战主要冠军数。
#
# 数字冻结 2026-11-11（含 2025-26 赛季收官）：手工整理自公开冠军榜与
# UEFA/FIFA 赛事纪录（Wikipedia 各国 champions 清单 + 2025-26 欧战决赛
# 核验：欧冠/欧联 PSG/Aston Villa、欧洲超级杯 PSG、2025 世俱杯 Chelsea）。
# 种子覆盖=fd 11 联赛 2016-2026 观察窗内出现过、且有上述任一荣誉的俱乐部
# （窗外俱乐部不种——面只 join 观察窗队；改数字/扩键=PR 人审）。
# 键名经 NameIndex 三级解析到 fd hist 队名（缩写变体由 ABBREVIATIONS 桥
# 接：Man United→Manchester United 等）；少数 fd 拼写不确定处双键并列。
# ---------------------------------------------------------------------------
HONOURS: dict[str, dict[str, tuple[int, int]]] = {
    # (顶级联赛冠军, 冠军+欧战主要冠军)
    "E0": {
        "Manchester United": (20, 28),
        "Liverpool": (20, 34),
        "Arsenal": (14, 15),
        "Manchester City": (10, 14),
        "Everton": (9, 10),
        "Aston Villa": (7, 10),
        "Sunderland": (6, 6),
        "Chelsea": (6, 16),
        "Sheffield Wednesday": (4, 4),
        "Sheffield Wed": (4, 4),
        "Newcastle United": (4, 4),
        "Newcastle": (4, 4),
        "Blackburn Rovers": (3, 3),
        "Blackburn": (3, 3),
        "Huddersfield Town": (3, 3),
        "Huddersfield": (3, 3),
        "Wolverhampton Wanderers": (3, 3),
        "Leeds United": (3, 3),
        "Leeds": (3, 3),
        "Preston North End": (2, 2),
        "Preston": (2, 2),
        "Burnley": (2, 2),
        "Portsmouth": (2, 2),
        "Tottenham Hotspur": (2, 6),
        "Tottenham": (2, 6),
        "Derby County": (2, 2),
        "Derby": (2, 2),
        "Sheffield United": (1, 1),
        "West Bromwich Albion": (1, 1),
        "West Brom": (1, 1),
        "Ipswich Town": (1, 2),
        "Ipswich": (1, 2),
        "Nottingham Forest": (1, 5),
        "Forest": (1, 5),
        "Leicester City": (1, 1),
        "Leicester": (1, 1),
        "West Ham United": (0, 1),  # 1965 优胜者杯
        "West Ham": (0, 1),
    },
    "SP1": {
        "Real Madrid": (36, 69),
        "Barcelona": (29, 46),
        "Atletico Madrid": (11, 19),
        "Athletic Bilbao": (8, 8),
        "Valencia": (6, 10),
        "Real Sociedad": (2, 2),
        "Sociedad": (2, 2),
        "Sevilla": (1, 9),
        "Real Betis": (1, 1),
        "Betis": (1, 1),
        "Deportivo La Coruna": (1, 1),
        "Depor": (1, 1),
        "Villarreal": (0, 1),  # 2021 欧联
    },
    "D1": {
        "Bayern Munich": (35, 48),
        "Borussia Dortmund": (8, 11),
        "Dortmund": (8, 11),
        "Schalke 04": (7, 8),
        "Hamburger SV": (6, 8),
        "VfB Stuttgart": (5, 5),
        "Stuttgart": (5, 5),
        "Borussia Monchengladbach": (5, 7),
        "Werder Bremen": (4, 5),
        "Bremen": (4, 5),
        "FC Koln": (3, 3),
        "Bayer Leverkusen": (1, 2),
        "Leverkusen": (1, 2),
        "Eintracht Frankfurt": (1, 3),
        "Frankfurt": (1, 3),
    },
    "I1": {
        "Juventus": (36, 46),
        "Inter Milan": (21, 31),
        "AC Milan": (19, 37),
        "Genoa": (9, 9),
        "Torino": (7, 7),
        "Bologna": (7, 7),
        "Napoli": (4, 5),
        "Roma": (3, 3),
        "Lazio": (2, 4),
        "Fiorentina": (2, 3),
        "Sampdoria": (1, 2),
        "Hellas Verona": (1, 1),
        "Verona": (1, 1),
        "Cagliari": (1, 1),
        "Parma": (0, 5),
        "Atalanta": (0, 1),  # 2024 欧联
    },
    "F1": {
        "Paris Saint-Germain": (14, 19),
        "Saint-Etienne": (10, 10),
        "Marseille": (9, 10),
        "Monaco": (8, 8),
        "Nantes": (8, 8),
        "Lyon": (7, 7),
        "Bordeaux": (6, 6),
        "Reims": (6, 6),
        "Lille": (5, 5),
        "Nice": (4, 4),
        "Lens": (1, 1),
        "Montpellier": (1, 1),
        "Auxerre": (1, 1),
        "Strasbourg": (1, 1),
    },
    "N1": {
        "Ajax": (36, 46),
        "PSV Eindhoven": (27, 29),
        "PSV": (27, 29),
        "Feyenoord": (16, 20),
        "Sparta Rotterdam": (6, 6),
        "Sparta": (6, 6),
        "Go Ahead Eagles": (4, 4),
        "Go Ahead": (4, 4),
        "Willem II": (3, 3),
        "AZ Alkmaar": (2, 2),
        "AZ": (2, 2),
        "Heracles": (2, 2),
        "FC Twente": (1, 1),
        "Twente": (1, 1),
    },
    "P1": {
        "Benfica": (38, 40),
        "FC Porto": (31, 38),
        "Porto": (31, 38),
        "Sporting CP": (21, 22),
        "Sporting": (21, 22),
        "Belenenses": (1, 1),
        "Boavista": (1, 1),
    },
    "T1": {
        "Galatasaray": (26, 28),
        "Fenerbahce": (19, 19),
        "Besiktas": (16, 16),
        "Trabzonspor": (7, 7),
        "Bursaspor": (1, 1),
        "Istanbul Basaksehir": (1, 1),
        "Basaksehir": (1, 1),
    },
    "B1": {
        "Anderlecht": (34, 39),
        "Club Brugge": (20, 20),
        "Union Saint-Gilloise": (12, 12),
        # fd 常用缩写双键（同值并列不构成解析歧义）
        "Union SG": (12, 12),
        "Union St. Gilloise": (12, 12),
        "Standard Liege": (10, 10),
        "Royal Antwerp": (5, 5),
        "Antwerp": (5, 5),
        "KV Mechelen": (4, 6),
        "Mechelen": (4, 6),
        "Lierse": (4, 4),
        "Genk": (4, 4),
        "Cercle Brugge": (3, 3),
        "Beveren": (2, 2),
        "Gent": (1, 1),
    },
    "SC0": {
        "Celtic": (56, 57),
        "Rangers": (55, 56),
        "Aberdeen": (4, 6),
        "Heart of Midlothian": (4, 4),
        "Hearts": (4, 4),  # fd 常用缩写，双键并列
        "Hibernian": (4, 4),
        "Motherwell": (1, 1),
        "Kilmarnock": (1, 1),
        "Dundee": (1, 1),
        "Dundee United": (1, 1),
    },
}


#: fd 赛季键形态常量（"2425" 两段各 2 位；起始年域=21 世纪窗）
_FD_SEASON_LEN = 4
_FD_SEASON_YEAR_MIN = 2000
_FD_SEASON_YEAR_MAX = 2099


def _fd_season_year(season: str) -> int | None:
    """Fd 赛季键 "2425" → 起始年 2024（tm_club_seasons 键同构）；非形态 None。"""
    if len(season) != _FD_SEASON_LEN or not season.isdigit():
        return None
    year = int(f"20{season[:2]}")
    return year if _FD_SEASON_YEAR_MIN <= year <= _FD_SEASON_YEAR_MAX else None


def _load_hist(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, str, str, int, int]]:
    """Hist 全量 (comp, season, home, away, fthg, ftag)（match_date 序稳定）。"""
    return [
        (
            str(r["competition"]),
            str(r["season"]),
            str(r["home_team"]),
            str(r["away_team"]),
            int(r["fthg"]),
            int(r["ftag"]),
        )
        for r in conn.execute(
            """
            SELECT competition, season, home_team, away_team, fthg, ftag
            FROM hist_matches
            ORDER BY match_date, id
            """
        ).fetchall()
    ]


def _points_face(
    hist: list[tuple[str, str, str, str, int, int]],
) -> tuple[dict[tuple[str, str], float], set[tuple[str, str]]]:
    """
    (team, season) → 积分分享率（3-1-0 / 可得满分）与出现集。

    跨联赛聚合同季分享率（队该季所有 fd 场次；升转队自然衔接两联赛口径）。
    """
    points: dict[tuple[str, str], int] = {}
    played: dict[tuple[str, str], int] = {}
    appearances: set[tuple[str, str]] = set()

    def _observe(team: str, season: str, scored: int, conceded: int) -> None:
        key = (team, season)
        appearances.add(key)
        played[key] = played.get(key, 0) + 1
        if scored > conceded:
            points[key] = points.get(key, 0) + 3
        elif scored == conceded:
            points[key] = points.get(key, 0) + 1

    for _comp, season, home, away, fthg, ftag in hist:
        _observe(home, season, fthg, ftag)
        _observe(away, season, ftag, fthg)
    share = {
        key: points.get(key, 0) / (3 * played[key]) for key in played if played[key] > 0
    }
    return share, appearances


def _honours_face(
    hist: list[tuple[str, str, str, str, int, int]],
) -> tuple[dict[str, dict[str, tuple[int, int]]], dict[str, list[str]]]:
    """
    Comp → hist 队名 → (冠军, 重要荣誉)（归一化精确匹配，不用词元子集）。

    词元子集会跨俱乐部泄漏（hist 短名 "Rovers" ⊂ 种子 "Blackburn Rovers"），
    故只做 normalize_team_name 后的精确等值——fd 缩写变体由种子的双键
    （"Dortmund"/"Borussia Dortmund" 等并列）+ ABBREVIATIONS 归一（
    "Man United"→"manchester united"）覆盖。同一 hist 名被不同值的多键
    命中=互撞不硬配（同值双键天然合并）；未解析键按 comp 列出（覆盖
    报告消费，人工补键走 PR）。
    """
    names_by_comp: dict[str, set[str]] = {}
    for comp, _season, home, away, _hg, _ag in hist:
        names_by_comp.setdefault(comp, set()).update((home, away))
    resolved: dict[str, dict[str, tuple[int, int]]] = {}
    unresolved: dict[str, list[str]] = {}
    for comp, seed in HONOURS.items():
        names = names_by_comp.get(comp)
        if not names:
            unresolved[comp] = sorted(seed)  # 该联赛 hist 无行（面外）
            continue
        norm_to_name: dict[str, str] = {}
        for name in sorted(names):
            norm_to_name.setdefault(normalize_team_name(name), name)
        table: dict[str, tuple[int, int]] = {}
        for key in sorted(seed):
            target = norm_to_name.get(normalize_team_name(key))
            if target is None:
                unresolved.setdefault(comp, []).append(key)
                continue
            value = seed[key]
            if target in table and table[target] != value:
                continue  # 互撞：不同值双配=不硬配（同值双键天然合并）
            table[target] = value
        resolved[comp] = table
    return resolved, unresolved


def _tm_season_face(
    conn: sqlite3.Connection,
) -> dict[int, dict[int, str]]:
    """club_tm_id → {tm 赛季年: league_tm_code}（成员面；年=int 化）。"""
    face: dict[int, dict[int, str]] = {}
    for row in conn.execute(
        "SELECT club_tm_id, season, league_tm_code FROM tm_club_seasons"
    ).fetchall():
        try:
            year = int(str(row["season"]))
        except ValueError:
            continue  # 非数形 tm 赛季键不参与（采集侧已 4 位钉死，防御）
        face.setdefault(int(row["club_tm_id"]), {})[year] = str(row["league_tm_code"])
    return face


def _streak(seasons: dict[int, str], prior_year: int) -> int:
    """连续 tier1 年数，截至 prior_year（不在则 0；观察窗内连续段）。"""
    streak = 0
    year = prior_year
    while seasons.get(year) in _TIER1_TM:
        streak += 1
        year -= 1
    return streak


def _tier1_flux(
    seasons: dict[int, str], prior_year: int, window: tuple[int, int]
) -> tuple[int, int]:
    """
    观察窗内 tier1 出入计数（promo, releg）——纯后视窗 t+1 ≤ prior_year。

    窗=库内 tm 赛季全局 [min, max]：窗内缺席季=离开受采面（tier1→缺席=
    降级类、缺席→tier1=升级类；窗缘外的缺席不构成转换）。
    """
    lo, hi = window
    promo = 0
    releg = 0
    for step_to in range(lo + 1, min(prior_year, hi) + 1):
        was = seasons.get(step_to - 1) in _TIER1_TM
        now = seasons.get(step_to) in _TIER1_TM
        if was and not now:
            releg += 1
        elif now and not was:
            promo += 1
    return promo, releg


def derive_club_pedigree(
    conn: sqlite3.Connection, *, now: str | None = None
) -> dict[str, object]:
    """
    重建底蕴硬层四因子面（幂等：DELETE+INSERT 全量重建，原子）。

    行=(team, season) hist 出现集；四因子 PIT 语义见模块 docstring。
    空输入零行不炸；stats 附 tm 未解析队清单与种子未解析键（人工补线面）。
    """
    rebuilt_at = now or utc_now_iso()
    hist = _load_hist(conn)
    share, appearances = _points_face(hist)
    honours, honours_unresolved = _honours_face(hist)
    club_by_name = {
        name: club for club, name in resolve_season_club_hist_names(conn).items()
    }
    tm_seasons = _tm_season_face(conn)
    tm_years = [year for seasons in tm_seasons.values() for year in seasons]
    window = (min(tm_years), max(tm_years)) if tm_years else (0, -1)
    comps_by_team_season: dict[tuple[str, str], set[str]] = {}
    for comp, season, home, away, _hg, _ag in hist:
        comps_by_team_season.setdefault((home, season), set()).add(comp)
        comps_by_team_season.setdefault((away, season), set()).add(comp)
    tm_unresolved: set[str] = set()
    prior_seasons_by_team: dict[str, list[tuple[int, str]]] = {}
    for team, season in appearances:
        year = _fd_season_year(season)
        if year is not None:
            prior_seasons_by_team.setdefault(team, []).append((year, season))
    for seasons_list in prior_seasons_by_team.values():
        seasons_list.sort()
    with atomic(conn):
        conn.execute("DELETE FROM club_pedigree")
        for team, season in sorted(appearances):
            season_year = _fd_season_year(season)
            l5 = [
                share[(team, prior)]
                for year, prior in prior_seasons_by_team.get(team, [])
                if season_year is not None and year < season_year
            ][-L5_WINDOW:]
            club_tm_id = club_by_name.get(team)
            streak: int | None = None
            promo: int | None = None
            releg: int | None = None
            if club_tm_id is None:
                tm_unresolved.add(team)
            elif season_year is not None:
                club_seasons = tm_seasons.get(club_tm_id, {})
                streak = _streak(club_seasons, season_year - 1)
                promo, releg = _tier1_flux(club_seasons, season_year - 1, window)
            titles = 0
            majors = 0
            for comp in sorted(comps_by_team_season.get((team, season), ())):
                entry = honours.get(comp, {}).get(team)
                if entry is not None:
                    titles, majors = entry
                    break
            conn.execute(
                """
                INSERT INTO club_pedigree
                    (team, season, top_flight_streak, points_pct_l5, l5_seasons,
                     tier1_titles, major_honours, promo_to_tier1, releg_from_tier1,
                     club_tm_id, rebuilt_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    team,
                    season,
                    streak,
                    round(sum(l5) / len(l5), 6) if l5 else None,
                    len(l5),
                    titles,
                    majors,
                    promo,
                    releg,
                    club_tm_id,
                    rebuilt_at,
                ),
            )
        stats: dict[str, object] = {
            "rows": len(appearances),
            "teams": len({team for team, _season in appearances}),
            "seasons": len({season for _team, season in appearances}),
            "tm_unresolved_teams": sorted(tm_unresolved),
            "honours_unresolved_keys": honours_unresolved,
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


def derive_state(conn: sqlite3.Connection) -> dict[str, object] | None:
    """最近一次派生现态（face_derive_state 的属主读口；从未派生=None）。"""
    row = conn.execute(
        "SELECT stats FROM face_derive_state WHERE face = ?", (FACE_NAME,)
    ).fetchone()
    if row is None:
        return None
    parsed: dict[str, object] = json.loads(str(row["stats"]))
    return parsed


def pedigree_for(
    conn: sqlite3.Connection, team: str, season: str
) -> dict[str, object] | None:
    """
    Facet 扫描器读缝：按 (hist 队名, fd 赛季键) 直读四因子行。

    消费演示=EM facet 对 hist 场次按 (home_team|away_team, season) 联结；
    行缺席（队该季不在 fd 面）返回 None，由调用方决定缺省。
    """
    row = conn.execute(
        """
        SELECT team, season, top_flight_streak, points_pct_l5, l5_seasons,
               tier1_titles, major_honours, promo_to_tier1, releg_from_tier1,
               club_tm_id
        FROM club_pedigree WHERE team = ? AND season = ?
        """,
        (team, season),
    ).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}  # noqa: SIM118
