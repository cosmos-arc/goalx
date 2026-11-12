"""
源T 任期/队籍 silver 面（票 09，spec-srct-cutover）。

bronze 语料（票 08 四数据集，data/ingest/srct_tenure.py）→ 主库两表
（v61，data 域归属 ADR-0008）：

- ``srct_coach_tenures``：教练任期行。coach_id=canonical playerInfo id
  （首日三钉①：恰两处可信来源——match_detail 锚点 href 第二段、tdl
  ``lineupDetail`` 主教练行首列；tdl ``coach`` 变量 id 系统性错配永不用于
  身份）；coach_key=英文名优先（来源=教练档案页英文名行，三钉②）、缺则
  id 串——票 10 教练键切换的落点；
- ``srct_club_seasons``：matchResult 逐季球队枚举 → (league, season,
  team) 队籍行。按构造完整：踢过该季联赛的队必然出现在枚举文件里。

**coachCount 竞赛分行折叠**（2026-11-12 语料实测）：履历 jsData 的
``coachCount`` 同一任期按竞赛拆多行（总分行 + 杯赛分项行；bronze 载荷
未保留竞赛列）。折叠规则（诚实战绩，不造数）：

1. 同组内完全相同（名字+战绩）的行折叠（源端整行重复，79 组实测）；
2. 单行组直取；
3. 多行组找「总分行」= 四项战绩各自等于**其余行之和**的那行（唯一才取，
   502 组）；
4. 次优：matches 严格最大且 ≥ 其余行和的**支配行**（47 组——总分行的
   总计含未逐项列出的竞赛，严格和性质不成立但支配成立）；
5. 都不满足 → 任期行保留（起止/队是事实），战绩四列置 NULL（9 组，如
   实计数入报告）。

重建纪律：DELETE+INSERT 全量一事务（派生面，pedigree 先例）；空语料零行
不炸。主库物化是合并后运维步（票 10 前置），工作树永不写生产库。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import TypedDict, cast

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_tenure
from goalx_backend.data.silver import latest_bronze_rows
from goalx_backend.db import atomic, utc_now_iso

SILVER_VERSION = "srct_tenure_silver_v1"

_STATS_KEYS = ("matches", "wins", "draws", "losses")
_ROLE_HEAD_COACH = "主教练"


class TenureEntry(TypedDict):
    """coachCount 一行（bronze 载荷形态，票 08 解析器保证）。"""

    start: str
    end: str | None
    team_id: int
    name_cn: str
    name_tw: str
    name_en: str
    matches: int
    wins: int
    draws: int
    losses: int


class SelectedTenure(TypedDict):
    """折叠后的任期行（战绩可空=残组诚实置空）。"""

    start: str
    end: str | None
    team_id: int
    name_cn: str
    name_en: str
    matches: int | None
    wins: int | None
    draws: int | None
    losses: int | None


class CareerPayload(TypedDict):
    """coach_career bronze 载荷。"""

    once_team: str | None
    now_team: list[list[str]]
    transfers: list[dict[str, object]]
    tenures: list[TenureEntry]
    last_update: str | None


class PagePayload(TypedDict):
    """coach_page bronze 载荷。"""

    name_cn: str | None
    name_tw: str | None
    name_en: str | None
    career_cid: str | None
    career_version: str | None


class TeamEntry(TypedDict):
    """matchResult arrTeam 一行。"""

    team_id: int
    name_cn: str
    name_tw: str
    name_en: str


class LeagueMeta(TypedDict):
    """matchResult arrLeague 头。"""

    league_id: int
    name_cn: str
    name_tw: str
    name_en: str
    season: str


class LeaguePayload(TypedDict):
    """league_season bronze 载荷。"""

    league: LeagueMeta
    teams: list[TeamEntry]
    table_rows: int
    last_update: str | None


class TenureRow(TypedDict):
    """srct_coach_tenures 一行（stats_via 为物化统计中转，入库前弹出）。"""

    coach_id: int
    coach_key: str
    coach_name_cn: str | None
    coach_name_tw: str | None
    coach_name_en: str | None
    team_id: int
    team_name_cn: str
    team_name_en: str
    date_from: str
    date_to: str | None
    role: str | None
    games: int | None
    wins: int | None
    draws: int | None
    losses: int | None
    stats_via: str
    parse_version: str
    materialized_at: str


class ClubSeasonRow(TypedDict):
    """srct_club_seasons 一行。"""

    league_id: int
    season: str
    team_id: int
    team_name_cn: str
    team_name_tw: str
    team_name_en: str
    league_name_cn: str
    league_name_en: str
    source_key: str
    parse_version: str
    materialized_at: str


def _payload(row: Mapping[str, object]) -> dict[str, object]:
    """Bronze 行 → 载荷 dict（缺失/非 dict 空 dict；形态由票 08 解析器保证）。"""
    payload = row.get("payload")
    if isinstance(payload, dict):
        return cast("dict[str, object]", payload)
    return {}


def _stats_tuple(tenure: TenureEntry) -> tuple[int, int, int, int]:
    return (tenure["matches"], tenure["wins"], tenure["draws"], tenure["losses"])


def _select_tenure_row(group: Sequence[TenureEntry]) -> tuple[SelectedTenure, str]:
    """
    一组同 (start, end, team_id) 的 coachCount 行 → (选中行, 战绩来源)。

    返回行的起止/队/名字恒为组内共识（组键保证）；战绩按折叠规则选出，
    选不出时战绩置 None（来源 ``stats_null``）。
    """
    # 1. 完全相同（名字+战绩）的行折叠
    unique: list[TenureEntry] = []
    seen: set[tuple[str, str, tuple[int, int, int, int]]] = set()
    for row in group:
        signature = (row["name_cn"], row["name_en"], _stats_tuple(row))
        if signature not in seen:
            seen.add(signature)
            unique.append(row)
    if len(unique) == 1:
        return _selected(unique[0]), "single"

    # 3. 总分行：四项战绩各自等于其余行之和（唯一才可信）
    totals = tuple(sum(r[k] for r in unique) for k in _STATS_KEYS)
    aggregate = [
        r
        for r in unique
        if all(r[k] == totals[i] - r[k] for i, k in enumerate(_STATS_KEYS))
    ]
    if len(aggregate) == 1:
        return _selected(aggregate[0]), "exact_sum"

    # 4. 支配行：matches 严格最大且 ≥ 其余行之和（总计含未列出竞赛）
    max_matches = max(r["matches"] for r in unique)
    dominant = [r for r in unique if r["matches"] == max_matches]
    if len(dominant) == 1 and max_matches >= totals[0] - max_matches:
        return _selected(dominant[0]), "dominant"

    # 5. 残组：起止/队保留，战绩诚实置空
    return _selected(unique[0], blank_stats=True), "stats_null"


def _selected(row: TenureEntry, *, blank_stats: bool = False) -> SelectedTenure:
    return {
        "start": row["start"],
        "end": row["end"],
        "team_id": row["team_id"],
        "name_cn": row["name_cn"],
        "name_en": row["name_en"],
        "matches": None if blank_stats else row["matches"],
        "wins": None if blank_stats else row["wins"],
        "draws": None if blank_stats else row["draws"],
        "losses": None if blank_stats else row["losses"],
    }


def tenure_rows_from_bronze(
    career_rows: Mapping[str, Mapping[str, object]],
    page_rows: Mapping[str, Mapping[str, object]],
    *,
    now: str | None = None,
) -> list[TenureRow]:
    """
    coach_career + coach_page 最新 bronze 行 → 任期行（纯函数）。

    coach_page 按 ``career_cid`` 供名（一人多页取名字一致者；冲突弃名）；
    缺页/缺英文名 → coach_key 回退 id 串（三钉②）。
    """
    names: dict[str, tuple[str | None, str | None, str | None]] = {}
    for page in page_rows.values():
        entry = cast("PagePayload", _payload(page))
        cid = entry.get("career_cid") or ""
        if not cid:
            continue
        triple = (entry.get("name_cn"), entry.get("name_tw"), entry.get("name_en"))
        if cid in names and names[cid] != triple:
            names[cid] = (None, None, None)
        else:
            names.setdefault(cid, triple)
    materialized = now or utc_now_iso()
    parse_version = srct_tenure.TENURE_BRONZE_VERSIONS[srct_tenure.COACH_CAREER_DATASET]
    rows: list[TenureRow] = []
    for cid, bronze in sorted(career_rows.items()):
        career = cast("CareerPayload", _payload(bronze))
        name_cn, name_tw, name_en = names.get(cid, (None, None, None))
        coach_key = (name_en or "").strip() or cid
        current_team_cn: str | None = None
        if career["now_team"] and career["now_team"][0]:
            current_team_cn = career["now_team"][0][0] or None
        groups: dict[tuple[str, str | None, int], list[TenureEntry]] = {}
        for tenure in career["tenures"]:
            if not tenure["start"]:
                continue
            key = (tenure["start"], tenure["end"], tenure["team_id"])
            groups.setdefault(key, []).append(tenure)
        for (_start, end, _team), group in sorted(groups.items()):
            picked, via = _select_tenure_row(group)
            rows.append(
                {
                    "coach_id": int(cid),
                    "coach_key": coach_key,
                    "coach_name_cn": name_cn,
                    "coach_name_tw": name_tw,
                    "coach_name_en": name_en,
                    "team_id": picked["team_id"],
                    "team_name_cn": picked["name_cn"],
                    "team_name_en": picked["name_en"],
                    "date_from": picked["start"],
                    "date_to": end or None,
                    "role": (
                        _ROLE_HEAD_COACH
                        if end in (None, "")
                        and current_team_cn is not None
                        and current_team_cn == picked["name_cn"]
                        else None
                    ),
                    "games": picked["matches"],
                    "wins": picked["wins"],
                    "draws": picked["draws"],
                    "losses": picked["losses"],
                    "stats_via": via,
                    "parse_version": parse_version,
                    "materialized_at": materialized,
                }
            )
    return rows


def club_season_rows_from_bronze(
    league_rows: Mapping[str, Mapping[str, object]],
    *,
    now: str | None = None,
) -> list[ClubSeasonRow]:
    """league_season 最新 bronze 行 → 队籍行（纯函数；每文件=当季全枚举）。"""
    materialized = now or utc_now_iso()
    parse_version = srct_tenure.TENURE_BRONZE_VERSIONS[
        srct_tenure.LEAGUE_SEASON_DATASET
    ]
    rows: list[ClubSeasonRow] = []
    for key, bronze in sorted(league_rows.items()):
        entry = cast("LeaguePayload", _payload(bronze))
        if not entry.get("league") or not entry.get("teams"):
            continue
        league = entry["league"]
        for team in entry["teams"]:
            rows.append(
                {
                    "league_id": league["league_id"],
                    "season": league["season"],
                    "team_id": team["team_id"],
                    "team_name_cn": team["name_cn"],
                    "team_name_tw": team["name_tw"],
                    "team_name_en": team["name_en"],
                    "league_name_cn": league["name_cn"],
                    "league_name_en": league["name_en"],
                    "source_key": key,
                    "parse_version": parse_version,
                    "materialized_at": materialized,
                }
            )
    return rows


def rebuild_srct_tenure_silver(
    conn: sqlite3.Connection, store: CorpusStore, *, now: str | None = None
) -> dict[str, object]:
    """
    全量重建两表（幂等：DELETE+INSERT 一事务；空语料零行）。

    只读语料、只写运行面两表——采集班与本函数互不直写（表内容=语料的
    确定性投影，重放同语料字节级同结果）。
    """
    careers = latest_bronze_rows(
        store,
        srct.SRCT_PROVIDER,
        srct_tenure.COACH_CAREER_DATASET,
        srct_tenure.TENURE_BRONZE_VERSIONS[srct_tenure.COACH_CAREER_DATASET],
    )
    pages = latest_bronze_rows(
        store,
        srct.SRCT_PROVIDER,
        srct_tenure.COACH_PAGE_DATASET,
        srct_tenure.TENURE_BRONZE_VERSIONS[srct_tenure.COACH_PAGE_DATASET],
    )
    leagues = latest_bronze_rows(
        store,
        srct.SRCT_PROVIDER,
        srct_tenure.LEAGUE_SEASON_DATASET,
        srct_tenure.TENURE_BRONZE_VERSIONS[srct_tenure.LEAGUE_SEASON_DATASET],
    )
    tenures = tenure_rows_from_bronze(careers, pages, now=now)
    club_seasons = club_season_rows_from_bronze(leagues, now=now)
    stats_via = {"single": 0, "exact_sum": 0, "dominant": 0, "stats_null": 0}
    for row in tenures:
        stats_via[row["stats_via"]] += 1
    # 入库参数剥离计数中转列（stats_via）
    insertable = [{k: v for k, v in row.items() if k != "stats_via"} for row in tenures]
    with atomic(conn):
        conn.execute("DELETE FROM srct_coach_tenures")
        conn.execute("DELETE FROM srct_club_seasons")
        conn.executemany(
            """
            INSERT INTO srct_coach_tenures (
                coach_id, coach_key, coach_name_cn, coach_name_tw, coach_name_en,
                team_id, team_name_cn, team_name_en, date_from, date_to, role,
                games, wins, draws, losses, parse_version, materialized_at)
            VALUES (
                :coach_id, :coach_key, :coach_name_cn, :coach_name_tw,
                :coach_name_en, :team_id, :team_name_cn, :team_name_en,
                :date_from, :date_to, :role, :games, :wins, :draws, :losses,
                :parse_version, :materialized_at)
            """,
            insertable,
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO srct_club_seasons (
                league_id, season, team_id, team_name_cn, team_name_tw,
                team_name_en, league_name_cn, league_name_en, source_key,
                parse_version, materialized_at)
            VALUES (
                :league_id, :season, :team_id, :team_name_cn, :team_name_tw,
                :team_name_en, :league_name_cn, :league_name_en, :source_key,
                :parse_version, :materialized_at)
            """,
            club_seasons,
        )
    return {
        "silver_version": SILVER_VERSION,
        "tenure_rows": len(tenures),
        "club_season_rows": len(club_seasons),
        "coaches": len({row["coach_id"] for row in tenures}),
        "teams": len({row["team_id"] for row in tenures}),
        "open_tenures": sum(1 for row in tenures if row["date_to"] is None),
        "stats_via": stats_via,
        # 三钉②残集：缺档案页英文名 → 键回退 id 串的教练数
        "name_fallback": len(
            {
                row["coach_id"]
                for row in tenures
                if row["coach_key"] == str(row["coach_id"])
            }
        ),
    }
