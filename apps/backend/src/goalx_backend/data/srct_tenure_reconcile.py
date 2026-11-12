"""
源T×TM 任期两源对账（票 09，spec-srct-cutover；ADR-0012：只产报告不硬配）。

TM 降级为对账/备用源后的第一次全量对账，四张面：

1. **俱乐部桥**（club_tm_id ↔ 源T team_id）双路合流：
   - 词法路：TM 俱乐部英文名 → NameIndex（normalize+词元子集，
     modelling/team_align 三级匹配，coach_changes 同款消费）对源T
     matchResult 枚举英文名；
   - 结构路：corpus match_detail × source_match_links × fixtures 的已链
     场按侧投票（源T中文名→fd team，fd team 经 team_aliases
     （transfermarkt）→ TM 俱乐部名 → club_tm_id（formations._tm_clubs
     先例）。任一跳撞多名 → 弃双侧并计数（不硬配）；双路对同一俱乐部
     结论冲突 → 弃并计数。
2. **任期日期 diff**（重叠俱乐部）：同俱乐部内教练按规范化名配对
   （中英文名域不同，用桥；配不上自成桶）。TM 侧 caretaker→manager 拆段
   与源T 单段按「跨度并集」比较（同人连续任职 TM 拆两段不误报单源缺）。
   四桶：同任两档（±容差）/ 错位 / 单源缺 / 名字不可对齐；大 diff 入
   人审清单；无分母不造率。
3. **枚举完整性**（spec user story 12）：match_detail 银层
   (队, 日期, 教练名) 观测 vs 任期语料教练集合 → (team, season) 格缺口
   量化（票 08 锚点荒区的诚实测量；缺口教练名无 id → 暂不可采，只报告）。
4. **facet 俱乐部覆盖**（喂票 10 键迁移决策）：169 家 facet 俱乐部 →
   映射到源T team_id 的 clean / 需别名工作 / 无对应 分桶。

只读纪律：源T 侧=bronze 语料直接内存解析（对账校验采集数据本身）；
TM 侧=运行面库只读连接；语料 duckdb 缺席 → 结构路与观测面降级
（不发请求）。

另承 **教练面映射率内存重放**（票 11，research/10 §3 面的可复算化）：
语料 bronze → silver 纯函数 → 六轴教练缝逻辑（hist 英文名折 +
``assign_manager_day`` 当日指派）在内存重放一个 hist 季窗——主库
srct 两表物化前（合并后运维步）的预估读数，判定可审计（run_params
留痕，spec story 14）。
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, TypedDict, cast

from goalx_backend.data import results as results_store
from goalx_backend.data import srct_tenure_silver
from goalx_backend.data.coach_changes import Tenure, assign_manager_day
from goalx_backend.data.srct_identity import SrctHistNameIndex, fold_tenures_by_team
from goalx_backend.db import utc_now_iso
from goalx_backend.modelling.team_align import (
    NameIndex,
    normalize_team_name,
    transfermarkt_aliases_by_team,
)

if TYPE_CHECKING:
    import duckdb

    from goalx_backend.data.corpus_store import CorpusStore

#: 同任两档判定容差（日）：两端差均 ≤ 此值 → 吻合桶
DATE_TOLERANCE_DAYS = 7
#: 人审清单阈值（日）：任一端差超此值 → 人审
REVIEW_DELTA_DAYS = 30
#: delta 分布桶上限（日）：within_90 档的界
DELTA_BUCKET_90_DAYS = 90
#: 词元共享判据的最短词元长度（字符）
TOKEN_MIN_LENGTH = 4
#: GB 赛季起始月（7 月开季，跨年）
SEASON_START_MONTH = 7
#: TM 侧 GB 双联赛代码（tm_club_seasons.league_tm_code；fd E0/E1 → GB1/GB2）
TM_GB_LEAGUE_CODES = frozenset({"GB1", "GB2"})
#: facet 的 GB 联赛面（league_zh）
FACET_GB_LEAGUES = frozenset({"英超", "英冠"})


@dataclass(frozen=True)
class TenureInterval:
    """一源一任期区间（对账的最小单元）。"""

    coach: str
    date_from: str
    date_to: str | None  # None = 现任开区间


def normalize_coach_name(name: str) -> str:
    """
    教练名规范化（配对键）：NFKD 去重音 → casefold → 分隔符归空格。

    中文侧吸收 `·`/`.`/空格 差写；拉丁侧吸收大小写/重音差异
    （René↔Rene）。不去词元（人名无法定法律词元）。
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = stripped.casefold()
    cleaned = "".join(c if c.isalnum() else " " for c in lowered)
    return " ".join(t for t in cleaned.split() if t)


def _days(later: str, earlier: str) -> int:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def _overlaps(a: TenureInterval, b: TenureInterval) -> bool:
    """闭开区间重叠（None 端 = 无穷）。"""
    a_end = a.date_to or "9999-12-31"
    b_end = b.date_to or "9999-12-31"
    return a.date_from <= b_end and b.date_from <= a_end


def _delta_bucket(delta: int) -> str:
    """端点差分布桶（trust basis 面：exact→gt_90 五档）。"""
    delta = abs(delta)
    if delta == 0:
        return "exact"
    if delta <= DATE_TOLERANCE_DAYS:
        return "within_7"
    if delta <= REVIEW_DELTA_DAYS:
        return "within_30"
    if delta <= DELTA_BUCKET_90_DAYS:
        return "within_90"
    return "gt_90"


def _counter_dict() -> dict[str, int]:
    """计数字典工厂（dataclass default_factory 不得内联调用，RUF009）。"""
    return defaultdict(int)


@dataclass
class DiffCounters:
    """diff 四桶计数 + 分布 + 人审清单（一报告一份）。"""

    agree: int = 0
    misaligned: int = 0
    srct_only: int = 0
    tm_only: int = 0
    matched_coaches: int = 0
    name_unmatched: int = 0
    name_unmatched_in_window: int = 0
    srct_only_in_window: int = 0
    tm_only_in_window: int = 0
    misaligned_open_mismatch: int = 0
    matched_via_subset: int = 0
    srct_only_names: list[dict[str, object]] = field(default_factory=list)
    tm_only_in_window_names: list[str] = field(default_factory=list)
    start_dist: dict[str, int] = field(default_factory=_counter_dict)
    end_dist: dict[str, int] = field(default_factory=_counter_dict)
    review: list[dict[str, object]] = field(default_factory=list)


def _interval_in_window(interval: TenureInterval, window_from: str | None) -> bool:
    """
    区间是否与语料窗相交（window_from None=不限）。

    相交口径=端点落窗内即算：2013→2019 的任期在 2015 窗内活动过，
    只看起点会把「跨窗老帅」漏成窗外（2026-11-12 实缴修正）。
    """
    if window_from is None:
        return True
    return interval.date_to is None or interval.date_to >= window_from


def _pair_name_group(  # noqa: PLR0913 一名组配对参数集（容差/人审阈值随报告参数走）
    srct_intervals: Sequence[TenureInterval],
    tm_intervals: Sequence[TenureInterval],
    club: int,
    team: int,
    counters: DiffCounters,
    *,
    tolerance_days: int,
    review_days: int,
    window_from: str | None = None,
) -> None:
    """
    一名（规范化名双侧都在）区间配对：源T 逐段 vs TM 重叠段并集。

    TM 拆段（caretaker→manager 同人连续）按与源T 段重叠的段并集成一个
    跨度比较；未被任何源T 段消耗的 TM 段 → tm_only。
    """
    consumed: set[int] = set()
    for srct_interval in sorted(srct_intervals, key=lambda i: i.date_from):
        cluster = [
            pos
            for pos, tm_interval in enumerate(tm_intervals)
            if pos not in consumed and _overlaps(srct_interval, tm_interval)
        ]
        if not cluster:
            counters.srct_only += 1
            continue
        consumed.update(cluster)
        tm_from = min(tm_intervals[pos].date_from for pos in cluster)
        tm_tos = [tm_intervals[pos].date_to for pos in cluster]
        tm_to = (
            None
            if any(to is None for to in tm_tos)
            else max(to for to in tm_tos if to is not None)
        )
        start_delta = _days(tm_from, srct_interval.date_from)
        counters.start_dist[_delta_bucket(start_delta)] += 1
        if srct_interval.date_to is None and tm_to is None:
            end_delta: int | None = None
            end_ok = True
        elif srct_interval.date_to is not None and tm_to is not None:
            end_delta = _days(tm_to, srct_interval.date_to)
            counters.end_dist[_delta_bucket(end_delta)] += 1
            end_ok = abs(end_delta) <= tolerance_days
        else:  # 一端开一端闭：端点错位（诚实计，不猜）
            end_delta = None
            end_ok = False
        start_ok = abs(start_delta) <= tolerance_days
        if start_ok and end_ok:
            counters.agree += 1
        else:
            counters.misaligned += 1
            if srct_interval.date_to is None or tm_to is None:
                # 源T 开区间（离任=下一任起点）vs TM 合同期末（含未来）语义差
                counters.misaligned_open_mismatch += 1
        worst = max(abs(start_delta), abs(end_delta) if end_delta is not None else 0)
        if worst > review_days or (not end_ok and start_ok):
            counters.review.append(
                {
                    "club_tm_id": club,
                    "team_id": team,
                    "coach": srct_interval.coach,
                    "srct_date_from": srct_interval.date_from,
                    "srct_date_to": srct_interval.date_to,
                    "tm_date_from": tm_from,
                    "tm_date_to": tm_to,
                    "start_delta_days": start_delta,
                    "end_delta_days": end_delta,
                }
            )
    leftovers = [
        tm_interval
        for pos, tm_interval in enumerate(tm_intervals)
        if pos not in consumed
    ]
    counters.tm_only += len(leftovers)
    counters.tm_only_in_window += sum(
        1 for i in leftovers if _interval_in_window(i, window_from)
    )


def _bucket_leftover_name(
    name: str,
    by_name_srct: dict[str, list[TenureInterval]],
    by_name_tm: dict[str, list[TenureInterval]],
    club: int,
    team: int,
    counters: DiffCounters,
    window_from: str | None,
) -> None:
    """配不上的残余名：单源区间桶 + 名字不可对齐计数 + 窗内分段 + 清单。"""
    counters.name_unmatched += 1
    srct_miss = by_name_srct.get(name, ())
    tm_miss = by_name_tm.get(name, ())
    if any(_interval_in_window(i, window_from) for i in (*srct_miss, *tm_miss)):
        counters.name_unmatched_in_window += 1
    counters.srct_only += len(srct_miss)
    counters.tm_only += len(tm_miss)
    counters.srct_only_in_window += sum(
        1 for i in srct_miss if _interval_in_window(i, window_from)
    )
    counters.tm_only_in_window += sum(
        1 for i in tm_miss if _interval_in_window(i, window_from)
    )
    if srct_miss:
        counters.srct_only_names.append(
            {"team_id": team, "club_tm_id": club, "name": srct_miss[0].coach}
        )
    if tm_miss and any(_interval_in_window(i, window_from) for i in tm_miss):
        counters.tm_only_in_window_names.append(tm_miss[0].coach)


def _subset_match_names(
    misses_srct: set[str], misses_tm: set[str]
) -> list[tuple[str, str]]:
    """
    未配名第二级匹配：词元子集（双向）且两侧均唯一命中才配（不硬配）。

    词元=规范化名按空格切分；{mikel, arteta} ⊂ {amatriain, arteta, mikel}
    → 同人常用名/全法定名桥。歧义（多对一/一对多）→ 留给单源桶。
    """
    tokens_srct = {
        name: frozenset(name.split()) for name in misses_srct if name.split()
    }
    tokens_tm = {name: frozenset(name.split()) for name in misses_tm if name.split()}
    pairs: list[tuple[str, str]] = []
    used_tm: set[str] = set()
    for name_srct, tokens in sorted(tokens_srct.items()):
        hits = [
            name_tm
            for name_tm, other in tokens_tm.items()
            if name_tm not in used_tm and (tokens < other or other < tokens)
        ]
        if len(hits) != 1:
            continue
        candidate = hits[0]
        # 反向唯一：该 TM 名不得子集命中其他未配源T 名（防 Smith 类姓氏吞并）
        back_hits = [
            other_srct
            for other_srct, other_tokens in tokens_srct.items()
            if other_srct != name_srct
            and (
                other_tokens < tokens_tm[candidate]
                or tokens_tm[candidate] < other_tokens
            )
        ]
        if back_hits:
            continue
        used_tm.add(candidate)
        pairs.append((name_srct, candidate))
    return pairs


def tenure_diff(
    srct_by_team: Mapping[int, Sequence[TenureInterval]],
    tm_by_club: Mapping[int, Sequence[TenureInterval]],
    club_bridge: Mapping[int, int],
    *,
    tolerance_days: int = DATE_TOLERANCE_DAYS,
    review_days: int = REVIEW_DELTA_DAYS,
    window_from: str | None = None,
) -> dict[str, object]:
    """
    重叠俱乐部的任期日期 diff（纯函数；桶计数 + 人审清单 + delta 分布）。

    桶互斥：每条区间恰好落入 agree / misaligned / srct_only / tm_only 之
    一；name_unmatched 按教练计（其区间已计入单源桶，不重复计率）。
    率只带分母（None=无分母不造率）。window_from=语料窗起点（源T 逐季
    文件最早季开球口径）：TM 侧俱乐部史横跨数十年，窗内分段计数把
    「语料窗之外的 TM 历史教练」（构造性不可比）与窗内缺口分开。
    """
    counters = DiffCounters()

    for club, team in sorted(club_bridge.items()):
        by_name_srct: dict[str, list[TenureInterval]] = defaultdict(list)
        by_name_tm: dict[str, list[TenureInterval]] = defaultdict(list)
        for interval in srct_by_team.get(team, ()):
            by_name_srct[normalize_coach_name(interval.coach)].append(interval)
        for interval in tm_by_club.get(club, ()):
            by_name_tm[normalize_coach_name(interval.coach)].append(interval)
        misses_srct: set[str] = set()
        misses_tm: set[str] = set()
        for name in set(by_name_srct) | set(by_name_tm):
            if name not in by_name_tm or name not in by_name_srct:
                misses_srct.add(name) if name in by_name_srct else misses_tm.add(name)
                continue
            counters.matched_coaches += 1
            _pair_name_group(
                by_name_srct[name],
                by_name_tm[name],
                club,
                team,
                counters,
                tolerance_days=tolerance_days,
                review_days=review_days,
                window_from=window_from,
            )
        # 第二级：词元子集唯一命中（源T 档案页英文名=全法定名，如
        # "Amatriain Arteta Mikel"↔TM 常用名 "Mikel Arteta"）；唯一才配
        matched_pairs = _subset_match_names(misses_srct, misses_tm)
        for name_srct, name_tm in matched_pairs:
            misses_srct.discard(name_srct)
            misses_tm.discard(name_tm)
            counters.matched_coaches += 1
            counters.matched_via_subset += 1
            _pair_name_group(
                by_name_srct[name_srct],
                by_name_tm[name_tm],
                club,
                team,
                counters,
                tolerance_days=tolerance_days,
                review_days=review_days,
                window_from=window_from,
            )
        # 残余名 → 单源缺 + 名字不可对齐
        for name in misses_srct | misses_tm:
            _bucket_leftover_name(
                name, by_name_srct, by_name_tm, club, team, counters, window_from
            )
    agree, misaligned = counters.agree, counters.misaligned
    paired = agree + misaligned
    coaches = counters.matched_coaches
    unmatched_coaches = counters.name_unmatched
    unmatched_in_window = counters.name_unmatched_in_window
    return {
        "clubs_compared": len(club_bridge),
        "window_from": window_from,
        "agree_intervals": agree,
        "misaligned_intervals": misaligned,
        "misaligned_open_mismatch": counters.misaligned_open_mismatch,
        "srct_only_intervals": counters.srct_only,
        "tm_only_intervals": counters.tm_only,
        "srct_only_in_window": counters.srct_only_in_window,
        "tm_only_in_window": counters.tm_only_in_window,
        "matched_coaches": coaches,
        "matched_via_subset": counters.matched_via_subset,
        "name_unmatched_coaches": unmatched_coaches,
        "name_unmatched_in_window": unmatched_in_window,
        "interval_agreement_rate": (agree / paired) if paired else None,
        "coach_name_match_rate": (
            coaches / (coaches + unmatched_coaches)
            if coaches + unmatched_coaches
            else None
        ),
        "coach_name_match_rate_in_window": (
            coaches / (coaches + unmatched_in_window)
            if coaches + unmatched_in_window
            else None
        ),
        "tolerance_days": tolerance_days,
        "review_days": review_days,
        "delta_distribution": {
            "start": dict(counters.start_dist),
            "end": dict(counters.end_dist),
        },
        "review_list": counters.review,
        "srct_only_names": counters.srct_only_names,
        "tm_only_in_window_names": sorted(set(counters.tm_only_in_window_names)),
        "method": (
            "同俱乐部教练按规范化名配对（NFKD去重音+casefold+分隔符归空格）；"
            "TM caretaker/manager 拆段按跨度并集与源T 单段比；"
            "两端差均≤容差=吻合，任一端超=错位；一端开一端闭=端点错位；"
            "第二级=词元子集唯一命中（全法定名↔常用名桥）；"
            "window_from 分段把 TM 俱乐部史里语料窗外的教练与窗内缺口分开"
        ),
    }


def enumeration_gap(
    observations: Iterable[tuple[int, str, str]],
    corpus_coaches_by_team: Mapping[int, Iterable[str]],
) -> dict[str, object]:
    """
    (team, season, 教练名) 观测 vs 语料教练集合 → 缺口格量化（纯函数）。

    观测名与语料名均规范化后比对（同人异写 `·`/`.` 不误报）；缺口教练
    名无源T id → 暂不可采（补充种子候选，报告 only）。
    """
    raw_observations = sum(1 for obs in observations if obs[2])
    cells: dict[tuple[int, str], dict[str, str]] = defaultdict(dict)
    for team_id, season, coach in observations:
        if coach:
            cells[(team_id, season)].setdefault(normalize_coach_name(coach), coach)
    corpus: dict[int, set[str]] = {
        team_id: {normalize_coach_name(name) for name in names}
        for team_id, names in corpus_coaches_by_team.items()
    }
    gap_cells: list[dict[str, object]] = []
    missing_names: set[str] = set()
    for (team_id, season), observed in sorted(cells.items()):
        known = corpus.get(team_id, set())
        missing = sorted(observed[key] for key in set(observed) - known)
        if missing:
            gap_cells.append({"team_id": team_id, "season": season, "missing": missing})
            missing_names.update(missing)
    total = len(cells)
    return {
        "raw_observations": raw_observations,
        "distinct_name_observations": sum(len(names) for names in cells.values()),
        "cells_total": total,
        "cells_with_gap": len(gap_cells),
        "gap_rate": (len(gap_cells) / total) if total else None,
        "distinct_missing_coaches": len(missing_names),
        "gap_cells": gap_cells,
        "method": (
            "match_detail 银层 (队,日期,教练名) 规范化观测 vs 任期语料教练"
            "名集合；锚点密度 2022-23 起才有，2015-16→2021-22 荒区本面"
            "不可测（票 08 已知缺口，另由种子履历回溯覆盖）"
        ),
    }


# —— 俱乐部桥（词法 + 结构双路） ———


def lexical_club_bridge(
    tm_club_names: Mapping[int, str],
    srct_teams: Mapping[int, tuple[str, str, str]],
) -> tuple[dict[int, int], list[str]]:
    """
    词法路：TM 俱乐部名 → NameIndex 解析到源T team_id（纯函数）。

    返回 (club→team, 未配名清单)；两名撞同一 team → 弃双侧（不硬配，
    计入未配）。
    """
    index = NameIndex.build(
        {names[2]: team_id for team_id, names in srct_teams.items()}
    )
    raw: dict[int, int] = {}
    for club, name in sorted(tm_club_names.items()):
        team_id = index.resolve(name)
        if team_id is not None:
            raw[club] = team_id
    claimed: dict[int, int] = {}
    collisions: set[int] = set()
    for club, team_id in raw.items():
        if team_id in claimed.values():
            collisions.add(club)
            collisions.add(_claimed_reverse(claimed, team_id))
        else:
            claimed[club] = team_id
    for club in collisions:
        claimed.pop(club, None)
    unmatched = sorted(
        name for club, name in tm_club_names.items() if club not in claimed
    )
    return claimed, unmatched


def _claimed_reverse(claimed: Mapping[int, int], team_id: int) -> int:
    """team_id 已占用的 club（collisions 标注用；单占前提下首个）。"""
    return next(c for c, t in claimed.items() if t == team_id)


def structural_club_bridge(
    link_rows: Iterable[tuple[str, int]],
    srct_teams: Mapping[int, tuple[str, str, str]],
    fd_aliases: Mapping[int, Iterable[str]],
    tm_name_to_id: Mapping[str, int],
) -> tuple[dict[int, int], dict[str, int]]:
    """
    结构路：已链场侧投票 源T名→fd team，别名桥→club（纯函数）。

    每跳撞多名 → 弃该键（不硬配）并计数。link_rows = 已链场按侧的
    (源T队名, fd team_id) 观察对。
    """
    cn_to_team: dict[str, int] = {}
    cn_conflicts: set[str] = set()
    for team_id, (name_cn, _tw, _en) in srct_teams.items():
        if name_cn in cn_to_team and cn_to_team[name_cn] != team_id:
            cn_conflicts.add(name_cn)
        cn_to_team.setdefault(name_cn, team_id)
    votes: dict[str, set[int]] = defaultdict(set)
    for srct_name, fd_team in link_rows:
        votes[srct_name].add(fd_team)
    name_to_fd = {
        name: next(iter(teams))
        for name, teams in votes.items()
        if len(teams) == 1 and name not in cn_conflicts
    }
    collisions = {
        "name_votes_dropped": sum(1 for t in votes.values() if len(t) > 1),
        "name_fd_conflicts_dropped": 0,
        "fd_club_conflicts_dropped": 0,
    }
    fd_to_club: dict[int, int] = {}
    for fd_team in set(name_to_fd.values()):
        clubs = {
            tm_name_to_id[alias]
            for alias in fd_aliases.get(fd_team, ())
            if alias in tm_name_to_id
        }
        if len(clubs) == 1:
            fd_to_club[fd_team] = clubs.pop()
        elif len(clubs) > 1:
            collisions["fd_club_conflicts_dropped"] += 1
    bridge: dict[int, int] = {}
    for srct_name, fd_team in name_to_fd.items():
        team_id = cn_to_team.get(srct_name)
        club = fd_to_club.get(fd_team)
        if team_id is not None and club is not None:
            existing = bridge.get(club)
            if existing is not None and existing != team_id:
                collisions["fd_club_conflicts_dropped"] += 1
                continue
            bridge[club] = team_id
    return bridge, collisions


def assemble_club_bridge(
    lexical: Mapping[int, int], structural: Mapping[int, int]
) -> dict[str, object]:
    """双路合流：一致→keep；单路→keep+注记；冲突→弃（不硬配）。"""
    bridge: dict[int, int] = {}
    both = only_lexical = conflicts = 0
    for club in set(lexical) | set(structural):
        lex = lexical.get(club)
        struct = structural.get(club)
        if lex is not None and struct is not None:
            if lex == struct:
                bridge[club] = lex
                both += 1
            else:
                conflicts += 1
        else:
            bridge[club] = lex if lex is not None else int(struct)  # type: ignore[arg-type]
            if struct is None:
                only_lexical += 1
    return {
        "bridge": bridge,
        "both_routes": both,
        "only_lexical": only_lexical,
        "only_structural": len(bridge) - both - only_lexical,
        "route_conflicts": conflicts,
    }


# —— facet 俱乐部覆盖（喂票 10） ———


def facet_club_coverage(
    facet_rows: Iterable[tuple[str, int, str]],
    srct_teams: Mapping[int, tuple[str, str, str]],
    *,
    gb_leagues: frozenset[str] = FACET_GB_LEAGUES,
) -> dict[str, object]:
    """
    Facet 俱乐部 → 源T team_id 覆盖分桶（纯函数）。

    GB 面（league_zh∈GB 双联赛）：clean=词法路直配；alias_work=未配但
    与某源T 队名共享词元（≥4 字符）——候选别名清单；unmapped=无词元
    交集。非 GB 联赛俱乐部=语料范围外（GB 语料先行，spec story 15）。
    """
    gb_clubs: dict[int, str] = {}
    maybe_non_gb: set[int] = set()
    for league_zh, club, name in facet_rows:
        if league_zh in gb_leagues:
            gb_clubs.setdefault(club, str(name))
        else:
            maybe_non_gb.add(club)
    # 同一俱乐部可兼有 GB 联赛行与欧战行：非 GB 桶只收纯非 GB 俱乐部
    non_gb = maybe_non_gb - set(gb_clubs)
    mapped, _unmatched = lexical_club_bridge(gb_clubs, srct_teams)
    srct_tokens: dict[int, set[str]] = {
        team_id: set(_meaningful_tokens(names[2]))
        for team_id, names in srct_teams.items()
    }
    alias_work: list[str] = []
    unmapped: list[str] = []
    for club, name in sorted(gb_clubs.items()):
        if club in mapped:
            continue
        tokens = set(_meaningful_tokens(name))
        if any(tokens & team_tokens for team_tokens in srct_tokens.values()):
            alias_work.append(name)
        else:
            unmapped.append(name)
    gb_total = len(gb_clubs)
    return {
        "facet_clubs": len(gb_clubs) + len(non_gb),
        "gb_clubs": gb_total,
        "gb_mapped_clean": len(mapped),
        "gb_mapped_rate": (len(mapped) / gb_total) if gb_total else None,
        "gb_alias_work": alias_work,
        "gb_unmapped": unmapped,
        "non_gb_clubs": len(non_gb),
        "note": "非 GB 联赛俱乐部=源T GB 语料范围外（二批扩域，spec story 15）",
    }


def _meaningful_tokens(name: str) -> list[str]:
    """≥ TOKEN_MIN_LENGTH 字符词元（短缩写/法律词元噪声已被 normalize 剥离）。"""
    return [t for t in normalize_team_name(name).split() if len(t) >= TOKEN_MIN_LENGTH]


# —— TM 侧只读取数（运行面 sqlite） ———


def load_tm_side(conn: sqlite3.Connection) -> TmSide:
    """
    TM 侧输入（只读）：GB 俱乐部全集 + 名→id 映射 + 任期区间 + 别名桥。

    club_name 撞多 club → 弃名（tm_coach_tenures 模式名众数，formations
    先例）。
    """
    gb_clubs = {
        int(row[0])
        for row in conn.execute(
            f"""
            SELECT DISTINCT club_tm_id FROM tm_club_seasons
            WHERE league_tm_code IN ({",".join("?" * len(TM_GB_LEAGUE_CODES))})
            """,  # noqa: S608 占位符全参数化；代码集=模块常量非用户输入
            tuple(sorted(TM_GB_LEAGUE_CODES)),
        )
    }
    name_votes: dict[int, dict[str, int]] = defaultdict(dict)
    name_to_club: dict[str, int] = {}
    name_conflicts: set[str] = set()
    for club_name, club, n in conn.execute(
        """
        SELECT club_name, club_tm_id, COUNT(*) AS n
        FROM tm_coach_tenures GROUP BY 1, 2
        """
    ):
        name_votes.setdefault(int(club), {})[str(club_name)] = int(n)
        if str(club_name) in name_to_club and name_to_club[str(club_name)] != int(club):
            name_conflicts.add(str(club_name))
        name_to_club.setdefault(str(club_name), int(club))
    for name in name_conflicts:
        name_to_club.pop(name, None)
    canonical = {
        club: sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        for club, votes in name_votes.items()
    }
    tenures: dict[int, list[TenureInterval]] = defaultdict(list)
    for club, coach, date_from, date_to in conn.execute(
        """
        SELECT club_tm_id, coach_name, date_from, date_to
        FROM tm_coach_tenures ORDER BY club_tm_id, date_from
        """
    ):
        tenures[int(club)].append(
            TenureInterval(str(coach), str(date_from), date_to or None)
        )
    # fd team → TM 俱乐部名全集：team_align 读口（transfermarkt 别名桥属主，
    # formations._tm_clubs 同款消费；team_aliases SQL 不越域直读）
    fd_aliases = {
        team_id: list(aliases)
        for team_id, aliases in transfermarkt_aliases_by_team(conn).items()
    }
    return TmSide(
        gb_clubs=gb_clubs,
        canonical_names=canonical,
        name_to_club=name_to_club,
        tenures_by_club=dict(tenures),
        fd_aliases=fd_aliases,
    )


# —— 教练面映射率内存重放（票 11，research/10 §3 面可复算化） ——

#: 重放缺省窗=S7 搜索窗（six_axes.SEARCH_WINDOW 同界；data 域不 import
#: evaluation，值在此钉面并由测试钉住）
REPLAY_DEFAULT_WINDOW: tuple[str, str] = ("1617", "2122")
#: 重放缺省联赛域=GB 双联赛（fd E0/E1；six_axes 联结面同域）
REPLAY_DEFAULT_COMPETITIONS: tuple[str, ...] = ("E0", "E1")
#: 重放面标识（run_params.replay；区别于主库物化后的 edge-scan s7 读数）
REPLAY_METHOD = "in_memory_bronze"


def replay_inputs_from_bronze(
    careers: Mapping[str, Mapping[str, object]],
    pages: Mapping[str, Mapping[str, object]],
    leagues: Mapping[str, Mapping[str, object]],
) -> tuple[dict[int, list[Tenure]], SrctHistNameIndex]:
    """
    Bronze 三族 →（tenures_by_team, hist 名折索引）——重放的两件输入。

    与主库读缝（srct_identity.srct_tenures_by_team / srct_hist_name_index）
    同语义不同源：此处从 bronze 内存投影（silver 纯函数），供主库 srct 两表
    物化前的预估——幽灵教练守卫（同 coach_key 同区间双 playerInfo id 折叠
    为一）与 date_from 升序契约照旧（折叠单属主=srct_identity.
    fold_tenures_by_team，与主库读缝同一实现）。
    """
    tenure_rows = srct_tenure_silver.tenure_rows_from_bronze(careers, pages)
    season_rows = srct_tenure_silver.club_season_rows_from_bronze(leagues)
    tenures = fold_tenures_by_team(
        (row["team_id"], row["coach_key"], row["date_from"], row["date_to"])
        for row in sorted(
            tenure_rows, key=lambda r: (r["team_id"], r["date_from"], r["coach_key"])
        )
    )
    names: dict[str, int] = {}
    for row in season_rows:
        names.setdefault(row["team_name_en"], row["team_id"])
    return tenures, SrctHistNameIndex(_index=NameIndex.build(names))


def replay_coach_mapping(
    matches: Sequence[tuple[str, str, str, str]],
    tenures_by_team: Mapping[int, Sequence[Tenure]],
    resolve: Callable[[str], int | None],
) -> dict[str, object]:
    """
    Hist 场次窗 → 教练面映射率重放（纯函数；六轴教练缝同逻辑）。

    matches = (season, day, home_name, away_name)；每侧经 resolve 折 team_id
    后按 ``assign_manager_day`` 当日指派。侧级三桶：unresolved_name（折不出
    队）/ no_tenure（队折出、当日无任期覆盖——荒区形态）/ assigned。率带
    分母（matches；零场 None 不造率）；逐季分桶供荒区结构报告。
    """
    total = len(matches)
    both = any_side = home_hits = away_hits = 0
    sides_resolved = sides_no_tenure = sides_unresolved = 0
    unresolved_names: dict[str, int] = defaultdict(int)
    season_cells: dict[str, dict[str, int]] = defaultdict(
        lambda: {"matches": 0, "both_sides": 0, "any_side": 0, "sides_no_tenure": 0}
    )
    for season, day, home_name, away_name in matches:
        cell = season_cells[season]
        cell["matches"] += 1
        home_hit = away_hit = False
        for side, name in (("home", home_name), ("away", away_name)):
            team_id = resolve(name)
            if team_id is None:
                sides_unresolved += 1
                unresolved_names[name] += 1
                continue
            sides_resolved += 1
            tenures = tenures_by_team.get(team_id)
            assigned = assign_manager_day(list(tenures), day) if tenures else None
            if assigned is None:
                sides_no_tenure += 1
                cell["sides_no_tenure"] += 1
                continue
            if side == "home":
                home_hit = True
            else:
                away_hit = True
        if home_hit:
            home_hits += 1
        if away_hit:
            away_hits += 1
        if home_hit or away_hit:
            any_side += 1
            cell["any_side"] += 1
        if home_hit and away_hit:
            both += 1
            cell["both_sides"] += 1
    by_season = [
        {
            "season": season,
            "matches": cell["matches"],
            "both_sides": cell["both_sides"],
            "any_side": cell["any_side"],
            "sides_no_tenure": cell["sides_no_tenure"],
            "both_sides_rate": (
                cell["both_sides"] / cell["matches"] if cell["matches"] else None
            ),
        }
        for season, cell in sorted(season_cells.items())
    ]
    return {
        "run_params": {
            "replay": REPLAY_METHOD,
            "assignment": "assign_manager_day",
        },
        "totals": {
            "matches": total,
            "both_sides": both,
            "any_side": any_side,
            "home_side": home_hits,
            "away_side": away_hits,
            "both_sides_rate": (both / total) if total else None,
            "any_side_rate": (any_side / total) if total else None,
            "home_side_rate": (home_hits / total) if total else None,
            "away_side_rate": (away_hits / total) if total else None,
            "sides_resolved": sides_resolved,
            "sides_no_tenure": sides_no_tenure,
            "sides_unresolved_name": sides_unresolved,
        },
        "unresolved_team_names": dict(
            sorted(unresolved_names.items(), key=lambda kv: -kv[1])
        ),
        "by_season": by_season,
    }


class SrctSide(TypedDict):
    """源T 侧对账输入（load_srct_side 产物）。"""

    teams: dict[int, tuple[str, str, str]]
    intervals_by_team: dict[int, list[TenureInterval]]
    coaches_cn_by_team: dict[int, set[str]]
    tenure_rows: int
    club_season_rows: int
    window_from: str | None


class TmSide(TypedDict):
    """TM 侧对账输入（load_tm_side 产物）。"""

    gb_clubs: set[int]
    canonical_names: dict[int, str]
    name_to_club: dict[str, int]
    tenures_by_club: dict[int, list[TenureInterval]]
    fd_aliases: dict[int, list[str]]


def load_srct_side(store: CorpusStore) -> SrctSide:
    """
    源T 侧输入（只读 bronze，四件：队枚举/任期/中文名域/行数）。

    GB 队枚举 + 任期区间（英文名域）+ 每队教练中文名集合（枚举观测
    比对域）。解析与 silver 物化同一纯函数（对账校验采集数据本身，
    非物化副本）。
    """
    bronze = srct_tenure_silver.load_tenure_bronze(store)
    tenure_rows = srct_tenure_silver.tenure_rows_from_bronze(
        bronze["careers"], bronze["pages"]
    )
    season_rows = srct_tenure_silver.club_season_rows_from_bronze(bronze["leagues"])
    teams: dict[int, tuple[str, str, str]] = {}
    for row in season_rows:
        teams.setdefault(
            row["team_id"],
            (row["team_name_cn"], row["team_name_tw"], row["team_name_en"]),
        )
    intervals: dict[int, list[TenureInterval]] = defaultdict(list)
    coaches_cn: dict[int, set[str]] = defaultdict(set)
    for row in tenure_rows:
        if row["coach_name_cn"]:
            coaches_cn[row["team_id"]].add(row["coach_name_cn"])
        intervals[row["team_id"]].append(
            TenureInterval(row["coach_key"], row["date_from"], row["date_to"])
        )
    # 语料窗起点：源T 逐季文件最早季的开球月（GB 7 月开季）1 日
    seasons = sorted({row["season"] for row in season_rows})
    window_from = f"{seasons[0].split('-')[0]}-07-01" if seasons else None
    return SrctSide(
        teams=teams,
        intervals_by_team=dict(intervals),
        coaches_cn_by_team={k: set(v) for k, v in coaches_cn.items()},
        tenure_rows=len(tenure_rows),
        club_season_rows=len(season_rows),
        window_from=window_from,
    )


def _season_of(day: str) -> str:
    """GB 赛季（7 月跨年→次年 6 月，源T `YYYY-YYYY` 形）。"""
    year, month = int(day[:4]), int(day[5:7])
    start = year if month >= SEASON_START_MONTH else year - 1
    return f"{start}-{start + 1}"


def reconcile_report(
    conn: sqlite3.Connection,
    store: CorpusStore,
    duck_con: duckdb.DuckDBPyConnection | None,
) -> dict[str, object]:
    """
    两源对账整装（只读；ADR-0012：报告不硬配，无分母不造率）。

    conn=运行面库只读连接；store=语料树；duck_con=corpus.duckdb 只读
    连接（None → 结构路与观测面降级 corpus_available=False）。
    """
    srct_side = load_srct_side(store)
    tm_side = load_tm_side(conn)
    srct_teams = srct_side["teams"]
    gb_cn_names = {cn: team for team, (cn, _tw, _en) in srct_teams.items()}

    structural: dict[int, int] = {}
    structural_collisions: dict[str, int] = {}
    observations: list[tuple[int, str, str]] = []
    corpus_available = duck_con is not None
    if duck_con is not None:
        link_rows = duck_con.execute(
            """
            SELECT d.home AS home, d.away AS away,
                   f.home_team_id AS hid, f.away_team_id AS aid
            FROM match_detail d
            JOIN goalx.source_match_links l
              ON l.source_match_id = d.sid AND l.source = 'srct'
             AND l.status = 'linked'
            JOIN goalx.fixtures f ON f.id = l.fixture_id
            """
        ).fetchall()
        structural, structural_collisions = structural_club_bridge(
            (
                pair
                for home, away, hid, aid in link_rows
                for pair in ((str(home), int(hid)), (str(away), int(aid)))
            ),
            srct_teams,
            tm_side["fd_aliases"],
            tm_side["name_to_club"],
        )
        obs_rows = duck_con.execute(
            """
            SELECT CAST(d.kickoff AT TIME ZONE 'UTC' AS DATE) AS day,
                   d.home AS home, d.away AS away,
                   d.home_coach AS hc, d.away_coach AS ac
            FROM match_detail d
            """
        ).fetchall()
        for day, home, away, hc, ac in obs_rows:
            day_str = str(day)
            for name, coach in ((str(home), hc), (str(away), ac)):
                if name in gb_cn_names and coach:
                    observations.append(
                        (gb_cn_names[name], _season_of(day_str), str(coach))
                    )

    tm_club_names = {
        club: tm_side["canonical_names"][club]
        for club in sorted(tm_side["gb_clubs"])
        if club in tm_side["canonical_names"]
    }
    lexical, lexical_unmatched = lexical_club_bridge(tm_club_names, srct_teams)
    assembled = assemble_club_bridge(lexical, structural)
    bridge = cast("dict[int, int]", assembled["bridge"])

    diff = tenure_diff(
        srct_side["intervals_by_team"],
        tm_side["tenures_by_club"],
        bridge,
        window_from=srct_side["window_from"],
    )
    enumeration = enumeration_gap(observations, srct_side["coaches_cn_by_team"])
    facet_rows = [
        (str(r[0]), int(r[1]), str(r[2]))
        for r in conn.execute(
            """
            SELECT league_zh, club_srct_id, club_name FROM formation_family_facet
            """
        )
    ]
    coverage = facet_club_coverage(facet_rows, srct_teams)
    bridged_teams = set(bridge.values())
    srct_only_teams = {
        team_id: names[0]
        for team_id, names in srct_teams.items()
        if team_id not in bridged_teams
    }
    return {
        "club_bridge": {
            "tm_gb_clubs": len(tm_club_names),
            "srct_gb_teams": len(srct_teams),
            "bridged": len(bridge),
            "lexical_unmatched": lexical_unmatched,
            "structural_collisions": structural_collisions,
            **{k: v for k, v in assembled.items() if k != "bridge"},
            "method": (
                "词法路=TM 俱乐部英文名→NameIndex(词元子集)→源T枚举英文名；"
                "结构路=已链场侧投票 源T中文名→fd team→transfermarkt 别名→"
                "club_tm_id；任一跳/双路冲突弃双侧（不硬配）"
            ),
        },
        "tenure_diff": diff,
        "enumeration": {
            "corpus_available": corpus_available,
            **enumeration,
        },
        "facet_coverage": coverage,
        "snapshot_gap": {
            # TM 现役名单快照缺口（构造性，spec problem statement 的对侧账）
            "srct_teams_without_tm": len(srct_only_teams),
            "teams": dict(sorted(srct_only_teams.items())),
        },
        "tm_side": {
            "tm_tenure_rows": sum(len(v) for v in tm_side["tenures_by_club"].values()),
            "tm_gb_clubs": len(tm_club_names),
        },
        "srct_side": {
            "tenure_rows": srct_side["tenure_rows"],
            "club_season_rows": srct_side["club_season_rows"],
        },
        "adr": "ADR-0012：对账只产报告不硬配；无分母不造率",
    }


def coach_mapping_replay_report(
    conn: sqlite3.Connection,
    store: CorpusStore,
    *,
    season_from: str = REPLAY_DEFAULT_WINDOW[0],
    season_to: str = REPLAY_DEFAULT_WINDOW[1],
    competitions: Sequence[str] = REPLAY_DEFAULT_COMPETITIONS,
    now: str | None = None,
) -> dict[str, object]:
    """
    教练面映射率重放整装（只读；主库 srct 两表物化前的预估读数）。

    语料 bronze 内存解析（silver 纯函数）+ 主库 hist 只读——不依赖主库
    srct_* 物化进度，合并前后同一命令可复算（数字随语料走）。run_params
    留窗/联赛域/语料计数（spec story 14：判定可审计）。
    """
    bronze = srct_tenure_silver.load_tenure_bronze(store)
    tenures, index = replay_inputs_from_bronze(
        bronze["careers"], bronze["pages"], bronze["leagues"]
    )
    hist_rows = results_store.hist_rows_in_season_window(
        conn,
        season_from=season_from,
        season_to=season_to,
        competitions=tuple(competitions),
    )
    matches = [
        (
            str(row["season"]),
            str(row["match_date"]),
            str(row["home_team"]),
            str(row["away_team"]),
        )
        for row in hist_rows
    ]
    report = replay_coach_mapping(matches, tenures, index.resolve)
    run_params = cast("dict[str, object]", report["run_params"])
    run_params.update(
        {
            "window": {"season_from": season_from, "season_to": season_to},
            "competitions": list(competitions),
            "corpus": {
                "coach_career_rows": len(bronze["careers"]),
                "coach_page_rows": len(bronze["pages"]),
                "league_season_rows": len(bronze["leagues"]),
                "tenure_teams": len(tenures),
            },
            "silver_version": srct_tenure_silver.SILVER_VERSION,
            "generated_at": now or utc_now_iso(),
        }
    )
    return report
