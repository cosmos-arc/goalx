"""
六轴数据 ④ 底蕴硬层四因子面（vision-build 票 09；人裁 5 两层拆分的硬层）。

四因子独立列不强行合成（合成与否留给 facet 读数）：

- 顶级联赛连续年限：``tm_club_seasons`` 派生——**截至上一赛季**（PIT：
  该赛季开始前可知；观察窗 2016 起，窗首连续段为下界，诚实有界口径）；
  顶级口径=逐国顶级（fd 11 联赛除英格兰次级 E1 外均为本国顶级——
  Ajax/Celtic/Benfica 照常计年限，correctness-review F1）；
- 近 5 季积分百分位：``hist_matches`` 派生（无 standings 表，逐场 3-1-0
  算积分分享率 = 积分/(3×场次)，跨联赛可比）——只用该赛季前 5 季，无
  前季 NULL 不造率；
- 荣誉计数：``HONOURS`` 受控常量（免费静态源）：顶级联赛冠军数 +
  总重要荣誉数（=顶级冠军+欧战主要冠军[欧冠/欧联/欧洲超级杯/优胜者杯/
  洲际杯/世俱杯]；国内杯与 2024 起 FIFA 洲际杯新赛不计入，口径写死在
  常量）。**tier1_titles 为 as-of-season 值**（F4）：全史总数 − 窗内
  S'≥S 赛季冠军数（窗内冠军从 hist 派生 standings；2016 前的全史部分对
  任何窗内赛季都是过去，PIT 干净）；**major_honours 是静态先验**——
  无窗内逐年级免费源，窗内年份带前视增量，消费方（EM facet）签字可用
  （correctness-review F4 裁定）。种子未命中/互撞 → 两列 NULL（不写
  假 0，F2/F6）；
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
from goalx_backend.modelling.team_align import normalize_team_name

FACE_NAME = "club_pedigree"
#: 近 5 季窗口（票面：近 5 季积分百分位）
L5_WINDOW = 5

#: 顶级联赛 fd 码（逐国顶级：fd 11 联赛中除英格兰次级 E1 外均为本国顶级；
#: correctness-review F1——顶级口径=逐国顶级而非 dc_model 的五大建模优先序，
#: 荷/葡/土/比/苏俱乐部年限不再恒 0；data 域自建受控常量，不借 modelling）
_TOP_FLIGHT_FD: frozenset[str] = frozenset(fd for fd in TM_LEAGUES if fd != "E1")
#: 顶级联赛 TM 码（fd → TM 映射折算；tm_club_seasons.league_tm_code 键空间）
_TIER1_TM: frozenset[str] = frozenset(TM_LEAGUES[fd] for fd in _TOP_FLIGHT_FD)

# ---------------------------------------------------------------------------
# 荣誉静态源（受控常量；markets/selections 种子先例）
#
# 口径：顶级联赛冠军（各国顶级联赛全史；英格兰=老英甲+英超，德国=德甲
# 前+德甲，荷兰含 1956 前全国冠军，苏格兰含共享冠军）+ 欧战主要冠军
# （欧洲冠军杯/欧冠、联盟杯/欧联、欧洲超级杯、欧洲优胜者杯、洲际杯、
# 世俱杯）。国内杯与 2024 起 FIFA 洲际杯新赛不计。
# major_honours = 冠军数 + 欧战主要冠军数。
#
# 数字冻结 2026-11-11（含 2025-26 赛季收官）：联赛冠军数逐国核自
# Wikipedia 各国 champions 清单（en.wikipedia.org/wiki/List_of_*_
# football_champions，11 国）；欧战计数核自 UEFA/FIFA 纪录与俱乐部页
# （correctness-review F5 逐条复核：Real Madrid/Bayern/Forest 三条修正
# ——RM 世俱 5 非另有 2024 新赛
# https://en.wikipedia.org/wiki/Real_Madrid_CF；Bayern 补 1967 优胜者杯
# https://en.wikipedia.org/wiki/FC_Bayern_Munich；Forest 1980 洲际杯
# 决赛负不计 https://en.wikipedia.org/wiki/Nottingham_Forest_F.C.；
# Milan/Liverpool/Tottenham/Feyenoord/PSV/Sporting 六条复核无误）。
# 种子覆盖=fd 11 联赛 2016-2026 观察窗内出现过、且有上述任一荣誉的俱乐部
# （窗外俱乐部不种——面只 join 观察窗队；改数字/扩键=PR 人审）。
# 键名经 normalize_team_name 精确匹配到 fd hist 队名（缩写变体由
# ABBREVIATIONS 桥接 + fd 真名形态双键并列，correctness-review F2）。
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
        "Sheffield Weds": (4, 4),  # fd 真名形态（F2）
        "Newcastle United": (4, 4),
        "Newcastle": (4, 4),
        "Blackburn Rovers": (3, 3),
        "Blackburn": (3, 3),
        "Huddersfield Town": (3, 3),
        "Huddersfield": (3, 3),
        "Wolverhampton Wanderers": (3, 3),
        "Wolves": (3, 3),  # fd 真名形态（F2）
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
        # F5 复核（Forest 俱乐部页）：1980 洲际杯决赛负（0-1 Nacional）、
        # 1979 弃赛——洲际 0 非 1，=1+2UCL+1SC 共 4
        "Nottingham Forest": (1, 4),
        "Forest": (1, 4),
        "Leicester City": (1, 1),
        "Leicester": (1, 1),
        "West Ham United": (0, 1),  # 1965 优胜者杯
        "West Ham": (0, 1),
    },
    "SP1": {
        # F5 复核（RM 俱乐部页）：36 联赛+15UCL+2UEL+6SC+3 洲际+5 世俱
        # =67——世俱是 5 非 7（2000/2002 属洲际杯纪元）；2024 FIFA 洲际杯
        # 新赛未入定义不计
        "Real Madrid": (36, 67),
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
        "La Coruna": (1, 1),  # fd 真名形态（F2）
        "Depor": (1, 1),
        "Villarreal": (0, 1),  # 2021 欧联
    },
    "D1": {
        # F5 复核（Bayern 俱乐部页）：35+6UCL+1UEL+2SC+2 洲际+2 世俱
        # +1CWC(1967 优胜者杯，首版漏计) = 49
        "Bayern Munich": (35, 49),
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
        "Sp Lisbon": (21, 22),  # fd 真名形态（F2）
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
        "Buyuksehyr": (1, 1),  # fd 真名形态（F2）
    },
    "B1": {
        "Anderlecht": (34, 39),
        "Club Brugge": (20, 20),
        "Union Saint-Gilloise": (12, 12),
        # fd 常用缩写双键（同值并列不构成解析歧义）
        "Union SG": (12, 12),
        "Union St. Gilloise": (12, 12),
        "St Gilloise": (12, 12),  # fd 真名形态（F2）
        "Standard Liege": (10, 10),
        "Standard": (10, 10),  # fd 真名形态（F2）
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
) -> tuple[dict[str, tuple[int, int]], dict[str, list[str]], list[str]]:
    """
    **全局** hist 队名 → (冠军, 重要荣誉)（归一化精确匹配，不用词元子集）。

    词元子集会跨俱乐部泄漏（hist 短名 "Rovers" ⊂ 种子 "Blackburn Rovers"），
    故只做 normalize_team_name 后的精确等值——fd 缩写变体由种子的双键
    （"Dortmund"/"Borussia Dortmund"/fd 真名形态等并列）+ ABBREVIATIONS
    归一（"Man United"→"manchester united"）覆盖。**全局名池跨 comp 并查**
    （correctness-review F3）：同一俱乐部的荣誉不随当季联赛跳变——
    Blackburn 只在 E1 出现也拿得到 3 冠。名池内两 hist 名归一后相撞=歧义
    名（弃用）；同一 hist 名被不同值命中=互撞，该名落 NULL 并入
    conflicts（F6，与「不硬配」注释一致）；未解析键按种子 comp 列出
    （覆盖报告消费，人工补键走 PR）。
    """
    norm_to_name: dict[str, str] = {}
    ambiguous: set[str] = set()
    for _comp, _season, home, away, _hg, _ag in hist:
        for name in (home, away):
            norm = normalize_team_name(name)
            if norm in norm_to_name and norm_to_name[norm] != name:
                ambiguous.add(norm)  # 两 hist 名归一同形=歧义，弃用
            norm_to_name.setdefault(norm, name)
    for norm in ambiguous:
        del norm_to_name[norm]
    global_table: dict[str, tuple[int, int]] = {}
    conflicting: set[str] = set()
    unresolved: dict[str, list[str]] = {}
    for comp, seed in HONOURS.items():
        for key in sorted(seed):
            target = norm_to_name.get(normalize_team_name(key))
            if target is None:
                unresolved.setdefault(comp, []).append(key)
                continue
            value = seed[key]
            if target in conflicting:
                continue  # 已判互撞的名不再写回
            if target in global_table and global_table[target] != value:
                conflicting.add(target)  # 不同值互撞 → 整名弃配落 NULL
                del global_table[target]
                continue
            global_table[target] = value  # 同值多键/多 comp 命中天然合并
    return global_table, unresolved, sorted(conflicting)


def _window_champions(
    hist: list[tuple[str, str, str, str, int, int]],
) -> dict[str, list[str]]:
    """
    窗内逐季顶级联赛冠军（team → 冠军赛季键升序）——titles as-of 的减数。

    冠军判定=逐 (顶级 comp, season) 的 (积分, 净胜球, 进球, 队名) 字典序
    最高者（fd 各国决胜规则不统一，points+GD+GF 为通行近似，末位队名只
    保证确定性）；次级联赛（E1）冠军不是顶级冠军不计。hist 缺季则该季
    无冠军行（as-of 少减一分，有界诚实）。
    """
    table: dict[tuple[str, str], dict[str, tuple[int, int, int]]] = {}
    for comp, season, home, away, fthg, ftag in hist:
        if comp not in _TOP_FLIGHT_FD:
            continue
        bucket = table.setdefault((comp, season), {})
        home_key = bucket.get(home, (0, 0, 0))
        away_key = bucket.get(away, (0, 0, 0))
        bucket[home] = (
            home_key[0] + (3 if fthg > ftag else 1 if fthg == ftag else 0),
            home_key[1] + fthg - ftag,
            home_key[2] + fthg,
        )
        bucket[away] = (
            away_key[0] + (3 if ftag > fthg else 1 if ftag == fthg else 0),
            away_key[1] + ftag - fthg,
            away_key[2] + ftag,
        )
    champions: dict[str, list[str]] = {}
    for (_comp, season), bucket in table.items():
        best = max(bucket, key=lambda team: (*bucket[team], team))
        champions.setdefault(best, []).append(season)
    for seasons_list in champions.values():
        seasons_list.sort()
    return champions


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
    honours, honours_unresolved, honours_conflicts = _honours_face(hist)
    champions = _window_champions(hist)
    club_by_name = {
        name: club for club, name in resolve_season_club_hist_names(conn).items()
    }
    tm_seasons = _tm_season_face(conn)
    tm_years = [year for seasons in tm_seasons.values() for year in seasons]
    window = (min(tm_years), max(tm_years)) if tm_years else (0, -1)
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
            entry = honours.get(team)
            if entry is None:
                # 未命中=种子未覆盖或互撞（F2/F6）：NULL 诚实缺，不写假 0
                titles: int | None = None
                majors: int | None = None
            else:
                # titles as-of（F4）：全史总数 − 窗内 S'≥S 的冠军数（当季
                # 冠军季末才定，季内行也不计——严格 PIT）；major_honours=
                # 静态先验（无窗内逐年级免费源，窗内有前视增量，消费方
                # EM facet 签字可用）
                titles = entry[0] - sum(
                    1 for won in champions.get(team, ()) if won >= season
                )
                majors = entry[1]
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
            "honours_conflicts": honours_conflicts,
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
    行缺席（队该季不在 fd 面）返回 None，由调用方决定缺省。荣誉列语义：
    tier1_titles=as-of 该季（窗内冠军已扣减）；major_honours=静态先验，
    窗内年份带前视增量，消费方签字可用（correctness-review F4）。
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
