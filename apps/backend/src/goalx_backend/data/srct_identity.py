"""
源T 身份缝（票 10，spec-srct-cutover；ADR-0014：任期/队籍身份主空间=源T）。

facet/读缝的俱乐部键=srct team_id、教练键=coach_key（英文名优先，票 09
silver 表内建）。本模块是三件身份管道的唯一属主（data 域，ADR-0008）：

1. **同站中文名折**（``srct_club_index``，formations derive 消费）：
   corpus 侧（fixture_universe/match_detail 中文名）与 matchResult 逐季
   枚举（srct_club_seasons）同站同季同语——按 (srct 季码, 中文名) 直配，
   无 TM 六跳链。同站产品间名漂移受控吸收（2026-11-12 实测）：站内
   "(中)" 标记剥除 + 莱切斯特城/莱斯特城 单条受控别名；同名撞两队弃键
   （不硬配）。
2. **hist 英文名折**（``srct_hist_name_index``，six_axes 教练缝 + pedigree
   频谱/年限消费）：fd hist 英文名（Man United/West Brom 形态）→ srct
   team_id，三级词法（modelling/team_align NameIndex：normalize+缩写展开+
   词元子集唯一命中）+ 受控别名（词法不通的实测三例：West Brom/
   Sheffield Weds/Peterboro——2026-11-12 实测 55/58 原生命中，此三条例外）。
3. **任期装载**（``srct_tenures_by_team``，manager-of-the-day 输入形态）：
   srct_coach_tenures → coach_changes.Tenure 列表（date_from 升序，
   ``assign_manager_day`` 区间语义=缝契约，open-end=现任）。源T role 域
   （主教练/NULL，仅现任标主教练）无 caretaker 分类——一律折 manager。
   **幽灵教练守卫**：同一教练出现两个 playerInfo id（tdl coach 变量伪 id
   现象）时，同名同区间行折叠为一（coach_key 单键空间，换队保族不断裂）。

非 GB 面：源T 语料 GB 双联赛先行，非 GB 侧经 1/2 折不出 → None（诚实缺，
消费方自分桶；批量扩域后零新基建回流，spec story 15）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from goalx_backend.data.coach_changes import Tenure
from goalx_backend.modelling.team_align import NameIndex

#: 源T 顶级联赛 id（matchResult league_id；英超=36。英冠=37 为二级。
#: 批量扩域采集新联赛时在此补行——tier 语义是受控词表，语料内无可推）
SRCT_TIER1_LEAGUE_IDS: frozenset[int] = frozenset({36})

#: 同站中文名受控别名（universe/fixture 产品 ↔ matchResult 枚举产品；
#: 2026-11-12 全量实测唯一词条）
CN_NAME_ALIASES: dict[str, str] = {"莱切斯特城": "莱斯特城"}

#: 站内标记后缀（桑德兰(中)/赫尔城(中) 实测形态——源T 站内消歧标记，
#: 枚举侧不带）
ZH_NAME_MARKER_SUFFIX = "(中)"

#: hist fd 名受控别名（词法三级不通的实测三例，2026-11-12 对 58 个
#: E0/E1 hist 队名全量解析的缺口清单）
HIST_NAME_ALIASES: dict[str, str] = {
    "West Brom": "West Bromwich(WBA)",
    "Sheffield Weds": "Sheffield Wednesday",
    "Peterboro": "Peterborough United",
}

#: 源T role（银表域：主教练/NULL）→ 面内 role 受控域的折法
_ROLE_MANAGER = "manager"


def srct_season_of(facet_season: str) -> str:
    """Facet 季码（2015-16）→ 源T 季码（2015-2016）。"""
    start = facet_season.split("-", maxsplit=1)[0]
    return f"{start}-{int(start) + 1}"


def facet_season_of(srct_season: str) -> str:
    """源T 季码（2015-2016）→ facet 季码（2015-16）。"""
    start = srct_season.split("-", maxsplit=1)[0]
    return f"{start}-{srct_season.rsplit('-', maxsplit=1)[-1][2:]}"


def srct_season_year(srct_season: str) -> int:
    """源T 季码 → 起始年（2015-2016 → 2015）。"""
    return int(srct_season.split("-", maxsplit=1)[0])


def _canonical_zh_name(name: str) -> str:
    """同站中文名规范化：剥站内 (中) 标记 → 受控别名。"""
    if name.endswith(ZH_NAME_MARKER_SUFFIX):
        name = name[: -len(ZH_NAME_MARKER_SUFFIX)]
    return CN_NAME_ALIASES.get(name, name)


@dataclass(frozen=True)
class SrctClubIndex:
    """
    (源T 季码, 中文名) → (srct team_id, 英文名) 同站折索引。

    conflicts = 同季同名撞两队被弃的键数（诚实计数，不硬配）。
    """

    _by_key: dict[tuple[str, str], tuple[int, str]]
    conflicts: int

    @classmethod
    def build(cls, rows: list[tuple[str, str, int, str]]) -> SrctClubIndex:
        """
        Rows = (season, name_cn, team_id, name_en)（srct_club_seasons 读出）。

        同键撞多 team_id → 弃键（先到不独裁，双侧都不配）。
        """
        raw: dict[tuple[str, str], tuple[int, str]] = {}
        tombstoned: set[tuple[str, str]] = set()
        conflicts = 0
        for season, name, team_id, name_en in rows:
            key = (season, _canonical_zh_name(name))
            if key in tombstoned:
                continue
            existing = raw.get(key)
            if existing is not None and existing[0] != team_id:
                del raw[key]
                tombstoned.add(key)
                conflicts += 1
            else:
                raw.setdefault(key, (team_id, name_en))
        return cls(_by_key=raw, conflicts=conflicts)

    def resolve(self, season: str, name: str) -> tuple[int, str] | None:
        """(源T 季码, corpus 中文名) → (team_id, 英文名)；缺席=None。"""
        return self._by_key.get((season, _canonical_zh_name(name)))


def srct_club_index(conn: sqlite3.Connection) -> SrctClubIndex:
    """主库 srct_club_seasons → 同站折索引（只读）。"""
    return SrctClubIndex.build(
        [
            (
                str(row["season"]),
                str(row["team_name_cn"]),
                int(row["team_id"]),
                str(row["team_name_en"]),
            )
            for row in conn.execute(
                """
                SELECT season, team_name_cn, team_id, team_name_en
                FROM srct_club_seasons
                """
            ).fetchall()
        ]
    )


@dataclass(frozen=True)
class SrctHistNameIndex:
    """hist fd 英文名 → srct team_id（三级词法 + 受控别名）。"""

    _index: NameIndex

    def resolve(self, name: str) -> int | None:
        """Fd 名折 team_id；歧义/未知名=None（不硬配）。"""
        return self._index.resolve(HIST_NAME_ALIASES.get(name, name))


def srct_hist_name_index(conn: sqlite3.Connection) -> SrctHistNameIndex:
    """主库 srct_club_seasons 英文名全集 → hist 名折索引（只读）。"""
    names: dict[str, int] = {}
    for row in conn.execute(
        "SELECT DISTINCT team_id, team_name_en FROM srct_club_seasons"
    ).fetchall():
        names.setdefault(str(row["team_name_en"]), int(row["team_id"]))
    return SrctHistNameIndex(_index=NameIndex.build(names))


def srct_tenures_by_team(conn: sqlite3.Connection) -> dict[int, list[Tenure]]:
    """
    srct_coach_tenures → team_id → 任职行（date_from 升序，coach_key 平键）。

    ``assign_manager_day`` 的输入契约（coach_changes.tenures_by_club 同型）；
    幽灵教练守卫：同 (coach_key, date_from, date_to) 双 id 行折叠为一。
    """
    out: dict[int, list[Tenure]] = {}
    seen: set[tuple[int, str, str, str | None]] = set()
    for row in conn.execute(
        """
        SELECT team_id, coach_key, date_from, date_to
        FROM srct_coach_tenures
        ORDER BY team_id, date_from, coach_key
        """
    ).fetchall():
        team_id = int(row["team_id"])
        tenure = Tenure(
            role=_ROLE_MANAGER,
            coach_name=str(row["coach_key"]),
            date_from=str(row["date_from"]),
            date_to=str(row["date_to"]) if row["date_to"] is not None else None,
        )
        signature = (team_id, tenure.coach_name, tenure.date_from, tenure.date_to)
        if signature in seen:  # 双 id 现象：同一教练一条任期
            continue
        seen.add(signature)
        out.setdefault(team_id, []).append(tenure)
    return out
