"""
JC audit 面（票 67）：TTG 粒度按年量化 + 存档最早年限 + 官方↔cid1129 对账。

消费票 70 bronze 与票 72 产物（silver 可选）——纯读侧，零采集代码：

- **TTG 按年密度**：老年仅 1 笔（2016/2019 实测）→ 当期多笔，粒度逐年
  变密需量化——回测窗口与口径的诚实依据（spec 69 story 4）。年份锚=
  首笔 updateDate 年（历史场无开球台账）。
- **存档最早年限**：二分探针（uniform 取该年 mid → fixedBonus 非空=
  存档活）；每步 2 请求，log2(25)≈5 步。
- **对账（官方 hadList vs 源T cid1129）**：同日 uniform（全名）× 日页
  bronze（中文名）前缀匹配 → (matchId, sid)；官方末笔 had SP vs 1129
  末笔三价——同源（都是官方价），一致率=对比源可用性画像（story 3）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from goalx_backend.config import Settings
from goalx_backend.data import pool as pool_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import jc, srct, uniform

CID_JC_OFFICIAL = "1129"  # 源T 1x2d 百家行里的竞彩官方行（对比源身份）
_TTG_MULTI_MIN_BEATS = 2  # 粒度密度信号：ttg ≥2 笔计多笔场
_SRCT_TIME_IDX = 3  # 1129 行 'H|D|A|MM-DD HH:MM|…' 的时间列位
_SRCT_TIME_RE = r"(\d{1,2})-(\d{1,2}) (\d{2}):(\d{2})"  # MM-DD HH:MM
_HAD_TRIPLE = 3
_SP_EPSILON = 0.001  # 末位舍入容差（同源镜像应逐值相等）


@dataclass
class YearDensity:
    """一年 JC 覆盖画像（had/hhad/ttg 逐场笔数）。"""

    matches: int = 0
    had_avg: float = 0.0
    hhad_avg: float = 0.0
    ttg_avg: float = 0.0
    ttg_matches_with_multi: int = 0  # ttg ≥2 笔场数（粒度密度信号）


@dataclass
class DensityReport:
    """TTG 粒度按年分布表（audit 落档产物）。"""

    by_year: dict[str, YearDensity] = field(default_factory=dict)


@dataclass
class ReconcileRow:
    """一场对账样本。"""

    date: str
    match_id: str
    sid: str | None  # 名字匹配失败 None（计入 unmatched 不进一致率分母）
    official_had: tuple[float, float, float] | None = None
    cid1129_had: tuple[float, float, float] | None = None
    consistent: bool | None = None  # None=无法比（一侧缺）


@dataclass
class ReconcileReport:
    """对账汇总（一致率=可比场中三价全等占比）。"""

    rows: list[ReconcileRow] = field(default_factory=list)
    matched: int = 0  # 成功配对 (matchId, sid)
    unmatched_names: int = 0
    comparable: int = 0
    consistent: int = 0

    @property
    def consistency_rate(self) -> float | None:
        """一致率（无可比场 None）。"""
        return self.consistent / self.comparable if self.comparable else None


def _latest_bronze(store: CorpusStore) -> dict[str, dict[str, object]]:
    """Jc bronze latest-per-matchId（拍/收口后行胜出）。"""
    latest: dict[str, dict[str, object]] = {}
    for row in store.read_bronze(jc.JC_PROVIDER, jc.SP_DATASET):
        if row.get("parser_version") != jc.BRONZE_VERSION:
            continue
        latest[str(row["sid"])] = row
    return latest


def _odds_history(row: dict[str, object]) -> dict[str, Any]:
    payload = cast("dict[str, object] | None", row.get("payload"))
    if payload is None:
        return {}
    return cast("dict[str, Any]", payload.get("oddsHistory") or {})


def ttg_density_by_year(  # noqa: C901 年锚判定分支随玩法数组累加
    store: CorpusStore,
) -> DensityReport:
    """JC bronze → 按年 had/hhad/ttg 逐场平均笔数（纯函数）。"""
    report = DensityReport()
    latest = _latest_bronze(store)
    years: dict[str, list[tuple[int, int, int]]] = {}
    for match_id in sorted(latest):
        odds_history = _odds_history(latest[match_id])
        if not odds_history:
            continue
        lists = {
            playtype: cast("list[object]", odds_history.get(f"{playtype}List") or [])
            for playtype in jc.PLAYTYPES
        }
        if not any(lists.values()):
            continue
        anchor_year: str | None = None
        for playtype in jc.PLAYTYPES:
            for raw in cast("list[dict[str, object]]", lists[playtype]):
                year = str(raw.get("updateDate") or "")[:4]
                if year.isdigit():
                    anchor_year = year
                    break
            if anchor_year:
                break
        if anchor_year is None:
            continue
        triple = (len(lists["had"]), len(lists["hhad"]), len(lists["ttg"]))
        years.setdefault(anchor_year, []).append(triple)
    for year in sorted(years):
        rows = years[year]
        entry = YearDensity(matches=len(rows))
        for idx, attr in enumerate(("had_avg", "hhad_avg", "ttg_avg")):
            setattr(entry, attr, round(sum(r[idx] for r in rows) / len(rows), 2))
        entry.ttg_matches_with_multi = sum(
            1 for r in rows if r[2] >= _TTG_MULTI_MIN_BEATS
        )
        report.by_year[year] = entry
    return report


def earliest_archive_year(
    client: httpx.Client, settings: Settings, *, lo: int = 2001, hi: int = 2026
) -> int | None:
    """
    FixedBonus 存档最早年限（二分探针；None=区间全空或探针不可用）。

    探针日取年中日（6-15，欧洲休赛期竞彩仍有亚非美赛事）；uniform 该日
    无场或全空 → 该年判死。
    """

    def alive(year: int) -> bool:
        try:
            rows = uniform.fetch_uniform_results(
                client, settings, f"{year}-06-15", f"{year}-06-15"
            )
            for row in rows:
                body = jc.fetch_fixed_bonus(client, settings, str(row["matchId"]))
                odds_history = _odds_history({"payload": jc.parse_fixed_bonus(body)})
                if odds_history:
                    return True
            return False
        except (httpx.HTTPError, RuntimeError, jc.JcContentError, ValueError):
            return False

    answer: int | None = None
    while lo <= hi:
        mid_year = (lo + hi) // 2
        if alive(mid_year):
            answer = mid_year
            hi = mid_year - 1
        else:
            lo = mid_year + 1
    return answer


def _cid1129_final_had(bronze: dict[str, object]) -> tuple[float, float, float] | None:
    """源T 1x2d bronze → cid1129 末笔 HDA 三价。"""
    payload = cast("dict[str, object] | None", bronze.get("payload"))
    if payload is None:
        return None
    game_map: dict[str, str] = {}
    for game in cast("list[object]", payload.get("game", [])):
        fields = str(game).split("|")
        if len(fields) > 1 and fields[0].strip() == CID_JC_OFFICIAL:
            game_map[fields[1]] = fields[0].strip()
    for entry in cast("list[object]", payload.get("game_detail", [])):
        gameid, sep, body = str(entry).partition("^")
        if not sep or game_map.get(gameid) != CID_JC_OFFICIAL:
            continue
        rows = [r for r in body.split(";") if r]
        if not rows:
            return None
        latest = max(rows, key=_srct_row_time_key)  # 源页行序非时间序（实测）
        values: list[float] = []
        for cell in latest.split("|")[:3]:
            try:
                values.append(float(cell))
            except ValueError:
                return None
        if len(values) == _HAD_TRIPLE:
            return (values[0], values[1], values[2])
    return None


def _srct_row_time_key(raw: str) -> tuple[int, int, int, int, int]:
    """1129 行 'H|D|A|MM-DD HH:MM|…|YYYY' → 可比时间键（坏行垫底）。"""
    fields = raw.split("|")
    found = (
        re.fullmatch(_SRCT_TIME_RE, fields[3].strip())
        if len(fields) > _SRCT_TIME_IDX
        else None
    )
    year = fields[-1].strip() if fields[-1].strip().isdigit() else "0"
    if found is None:
        return (0, 0, 0, 0, 0)
    month, day, hour, minute = (int(g) for g in found.groups())
    return (int(year), month, day, hour, minute)


def _official_final_had(
    odds_history: dict[str, Any],
) -> tuple[float, float, float] | None:
    """官方 hadList 末笔 HDA 三价。"""
    rows = cast("list[dict[str, Any]]", odds_history.get("hadList") or [])
    for raw in reversed(rows):
        try:
            return (float(raw["h"]), float(raw["d"]), float(raw["a"]))
        except (KeyError, TypeError, ValueError):
            continue
    return None


def reconcile_cid1129(  # noqa: C901, PLR0912 配对/比对分支随抽样口径累加
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    *,
    dates: list[str],
    per_date_cap: int = 10,
) -> ReconcileReport:
    """
    抽样对账：dates 各日 uniform×日页 名字配对 → 官方 vs cid1129 末笔三价。

    一致率分母=双方末笔均可解析的场；名字未配对计入 unmatched（诚实留痕
    不进分母）。
    """
    report = ReconcileReport()
    jc_latest = _latest_bronze(store)
    # 日页 sid → (home, away)（CorpusScope 完场清单）
    day_names: dict[str, dict[str, tuple[str, str]]] = {}
    for line in store.iter_bronze_lines(srct.SRCT_PROVIDER, srct.DAY_DATASET):
        row = json.loads(line)
        if row.get("parser_version") != srct.BRONZE_VERSIONS[srct.DAY_DATASET]:
            continue
        payload = cast("dict[str, object] | None", row.get("payload"))
        if payload is None:
            continue
        day_names[str(row["sid"])] = {
            str(m["sid"]): (str(m["home"]), str(m["away"]))
            for m in cast("list[dict[str, object]]", payload.get("matches", []))
        }
    odds_latest: dict[str, dict[str, object]] = {}
    for row in store.read_bronze(srct.SRCT_PROVIDER, srct.ODDS_DATASET):
        if row.get("parser_version") != srct.BRONZE_VERSIONS[srct.ODDS_DATASET]:
            continue
        odds_latest[str(row["sid"])] = row
    for day in dates:
        sids_today = day_names.get(day, {})
        if not sids_today:
            continue
        try:
            uniform_rows = uniform.fetch_uniform_results(client, settings, day, day)
        except (httpx.HTTPError, RuntimeError):
            continue
        taken = 0
        for urow in uniform_rows:
            if taken >= per_date_cap:
                break
            match_id = str(urow.get("matchId") or "")
            if match_id not in jc_latest:
                continue
            home_all = str(urow.get("allHomeTeam") or urow.get("homeTeam") or "")
            away_all = str(urow.get("allAwayTeam") or urow.get("awayTeam") or "")
            sid_hit: str | None = None
            for sid, (home, away) in sids_today.items():
                if pool_store.names_match(home_all, home) and pool_store.names_match(
                    away_all, away
                ):
                    sid_hit = sid
                    break
            row = ReconcileRow(
                date=day,
                match_id=match_id,
                sid=sid_hit,
                official_had=_official_final_had(_odds_history(jc_latest[match_id])),
                cid1129_had=(
                    _cid1129_final_had(odds_latest[sid_hit])
                    if sid_hit and sid_hit in odds_latest
                    else None
                ),
            )
            if sid_hit is None:
                report.unmatched_names += 1
                row.consistent = None
            elif row.official_had is None or row.cid1129_had is None:
                row.consistent = None
            else:
                report.matched += 1
                close = all(
                    abs(a - b) <= _SP_EPSILON
                    for a, b in zip(row.official_had, row.cid1129_had, strict=True)
                )
                row.consistent = close
                report.comparable += 1
                report.consistent += int(close)
            report.rows.append(row)
            taken += 1
    return report
