"""
Phase 1 验证门报告（票 55/56 切片 17）：三对账 + 五条量化门。

门=报告+用户点头，无自动放行（spec story 21/ADR-0011 决策 7）。产出两视图：
机器可读 JSON + 人读 MD（corpus 树 reports/，数据面资产不进 repo）。

- **门① 场次对账**：fdhist×源T fixture_universe，11 个重叠联赛（CorpusScope
  15 项 ∩ FD_COMPETITIONS，ADR-0010），窗口=源T 史窗 2016-01-01 起（对齐
  竞彩，PR#127）。匹配锚（2026-10-07 v3）：**名字身份轮优先**——先从
  比分+日期两步的高置信对学习 fdhist 队名→语料队名映射（≥2 票入选，
  同队异名并存），再用 名字+比分+日期±3 配对（吸收改期/日期基准差尾部，
  2026-10-07 实测歧义 2359→188、缺口 640→68、率 0.750→0.993）；无映射
  行退回比分+日期±1（北京墙钟 × 赛地本地日基准差）。比甲 playoff/苏超
  split 后轮次在 CorpusScope 口径外——单列 scope_excluded，不计分母。
  阈值：重叠匹配率 ≥99%，缺口逐场归因（分母只计夜班已完成日期 ±1 内
  的 fdhist 场次——未回填日期不计入，不虚增缺口）；行政判赛排除行
  （A4 admin_match_exclusions）在对账集入口统一剔除，行数单列不藏。
- **门② PSC×cid177**：fdhist PSC（收盘代理）×语料 cid177 赛前末可见价，
  逐Outcome 相对偏差；开球不足 6 个月的行 PSC 未收敛（A5 成熟度纪律），
  跳过比较单列。裁决口径（2026-10-07 v2）=**中位数 <1%**（尾部
  不敏感——晚场锚价/单场异常不再撬动裁决）；均值/p95/max 同报不藏尾，
  另按赛季分层（锚书 2023/24 起才报价，老季 usable 塌缩如实可见）+
  |偏差|>100% 异常清单供人工判读。
- **门③ xG 对账**：understat×源T xg_observation，五大 2025+，日期±1+比分
  锚定；MAE+相关系数+异常场清单（|差|>1.0，不设硬线供人工判读）。
- **门④ 转换完整性**：odds_change_event/_meta 的入账恒等式
  （源行=事件+心跳+坏时间+未映射+未解释缺口），未解释缺口必须为 0。
- **门⑤ 管线健康**：逐数据集 raw/bronze 键覆盖率（解析率代理，分母
  2026-10-07 起=**含当前解析模板标记的页**——站点约 2020 换模板，旧
  模板页解析器按设计跳过，单列 template_mismatch 不计入分母，是解析器
  v3 候选而非采集失败；伪 200 图页单列 pseudo_200=站点缺席内容）、
  夜班台账汇总（请求/停机原因）、Phase1 进度（done 日/任务清单）、
  silver 版本戳+树摘要（跨次报告比对即幂等佐证）。
- **coverage**：逐赛事×书商报价存在矩阵（design-15 §七 裁决归本报告物化）。

各门 verdict 按阈值如实判（insufficient=无可测值）；Phase1 进度 <100%
时报告 overall=provisional——数据完整性先行，不放行（门=报告+用户点头）。
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
# （duckdb 无官方 stub，同 corpus_duckdb 先例）

from __future__ import annotations

import gzip
import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any, cast

import duckdb

from goalx_backend import db
from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.hygiene import admin_exclusions, is_admin_excluded, psc_mature
from goalx_backend.data.ingest import srct
from goalx_backend.data.ingest.srct_night import phase1_dates
from goalx_backend.data.results import UNDERSTAT_LEAGUES

REPORTS_DIR = "reports"
REPORT_BASENAME = "phase1-gate"
# CorpusScope 中文联赛 ↔ fdhist 联赛码（ADR-0010：15 项中 11 项重叠）
LEAGUE_TO_FD: dict[str, str] = {
    "英超": "E0",
    "西甲": "SP1",
    "德甲": "D1",
    "意甲": "I1",
    "法甲": "F1",
    "英冠": "E1",
    "荷甲": "N1",
    "葡超": "P1",
    "土超": "T1",
    "比甲": "B1",
    "苏超": "SC0",
}
FD_TO_LEAGUE = {code: name for name, code in LEAGUE_TO_FD.items()}
# 门②比较锚：SHARP 尖货 cid177（跨源数据质量检查，不决定模型选书）
_ANCHOR_BOOK = "srct:1x2:177"


def _sql_in(values: Iterable[str]) -> str:
    """SQL IN 列表（值全部来自模块常量，非用户输入——S608 noqa 依此）。"""
    return ",".join("'" + v + "'" for v in values)


def _verdict(metric: float | None, passed: bool) -> str:
    """三态判定：无可测值=insufficient，否则按阈值 pass/fail。"""
    return "insufficient" if metric is None else "pass" if passed else "fail"


# 门③：understat 联盟码（fdhist 码映射）与赛季下界（2025+）
_XG_SEASON_MIN = 2025
_XG_ANOMALY_ABS = 1.0
_RATE_THRESHOLD = 0.99  # 门①重叠匹配率线
_PSC_THRESHOLD = 0.01  # 门②中位数相对偏差线（v2 裁决口径）
_PSC_OUTLIER_REL = 1.0  # 门②异常清单线（|相对偏差|>100%）
_MIN_CORR_POINTS = 2  # 相关系数最少点数
# 对账窗=源T 史窗（2016-01-01 起对齐竞彩，PR#127）；fdhist 自 2016-07-29
# 起，settled 面过滤自然截齐——早于语料窗的行不进分母
_WINDOW_START = date(2016, 1, 1)
# 门① v3 名字身份轮：学习映射最低票（1 票=误配噪声出局）与日期吸收窗
# （±3 天——改期/日期基准差尾部，2026-10-07 实测 365 缺口中 339 落 ±3 内）
_NAME_VOTE_MIN = 2
_NAME_ROUND_DAYS = 3
# CorpusScope 口径外（非采集失败，单列不计分母；扩不扩口径=用户裁决）：
# 比甲 playoff（4-6 月）与苏超 split 后轮次（5 月）不在 15 项常规联赛面
_SCOPE_EXCLUDED_MONTHS: dict[str, frozenset[str]] = {
    "比甲": frozenset(("04", "05", "06")),
    "苏超": frozenset(("05",)),
}
# 门⑤模板扫描：标记实测全在页首 29KB 内（asian 1.1KB/stats 28.5KB），
# 64KB 上限留 2× 余量；UTF-8 页字节级搜索与解析守卫解码后搜索等价
_TEMPLATE_SCAN_BYTES = 65536


@dataclass
class HistRow:
    """
    对账侧一（fdhist 料次或 understat 料次）。

    league_key=运行面联赛键（fdhist 码 / understat 联盟码经映射后的中文
    联赛名）——match_fixtures 内统一映射到语料侧键空间。
    """

    league_key: str
    match_date: str  # 赛地本地日 YYYY-MM-DD
    home: str
    away: str
    goals_home: int
    goals_away: int
    psc_home: float | None
    psc_draw: float | None
    psc_away: float | None


@dataclass
class FixtureRow:
    """
    语料 fixture_universe 料次（对账侧二）。

    league_key=运行面联赛键（fdhist 码 / understat 联盟码经映射后的中文
    联赛名）——match_fixtures 内统一映射到语料侧键空间。

    home/away/season 服务门①名字身份轮与门②赛季分层（门③ 比分+日期
    锚场景可缺省——名字轮遇空名自动跳过）。
    """

    sid: str
    league: str  # CorpusScope 中文
    kickoff_day: str  # 北京墙钟日 YYYY-MM-DD
    goals_home: int
    goals_away: int
    home: str = ""
    away: str = ""
    season: str | None = None


@dataclass
class MatchResult:
    """联赛+比分+日期±1 锚定的两侧配对结果（v3：名字轮+口径外单列）。"""

    pairs: list[tuple[FixtureRow, HistRow]] = field(default_factory=list)
    gaps: list[HistRow] = field(default_factory=list)  # fdhist 有而语料缺
    ambiguous: list[HistRow] = field(default_factory=list)  # 同键多候选，未自动配
    extra_fixtures: list[FixtureRow] = field(default_factory=list)  # 语料有 fdhist 无
    scope_excluded: list[HistRow] = field(default_factory=list)  # CorpusScope 口径外
    name_links_learned: int = 0  # 学得的 fdhist 队名→语料队名映射数
    matched_via_name: int = 0  # 名字身份轮配走的对数

    @property
    def rate(self) -> float | None:
        """重叠匹配率（分母=已覆盖日期面的 fdhist 场次；口径外/歧义不计分子）。"""
        denominator = len(self.pairs) + len(self.gaps) + len(self.ambiguous)
        if denominator == 0:
            return None
        return len(self.pairs) / denominator


def _nearby(day: str) -> list[str]:
    """日期 ±1（跨库日期基准吸收窗）。"""
    anchor = date.fromisoformat(day)
    return [(anchor + timedelta(days=delta)).isoformat() for delta in (-1, 0, 1)]


def _day_distance(a: str, b: str) -> int:
    """两 ISO 日的绝对日差（名字轮 ±3 吸收窗用）。"""
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def _cn_league(hist: HistRow) -> str:
    """HistRow 联赛键归一到语料中文联赛名。"""
    return FD_TO_LEAGUE.get(hist.league_key, hist.league_key)


def _scope_excluded(league_cn: str, day: str) -> bool:
    """CorpusScope 口径外判定（比甲 playoff/苏超 split——单列不计分母）。"""
    return day[5:7] in _SCOPE_EXCLUDED_MONTHS.get(league_cn, frozenset())


def _exact_day_round(
    hist: HistRow,
    by_key: dict[tuple[str, int, int, str], list[FixtureRow]],
    used: set[str],
) -> FixtureRow | None:
    """精确日唯一候选即配（多候选不配，留待窗口步判歧义）。"""
    league = _cn_league(hist)
    exact = [
        c
        for c in by_key.get(
            (league, hist.goals_home, hist.goals_away, hist.match_date), []
        )
        if c.sid not in used
    ]
    if len(exact) == 1:
        used.add(exact[0].sid)
        return exact[0]
    return None


def _window_round(
    hist: HistRow,
    by_key: dict[tuple[str, int, int, str], list[FixtureRow]],
    used: set[str],
) -> tuple[FixtureRow | None, bool]:
    """±1 窗唯一候选兜底（返回 (fixture, ambiguous)；多候选不硬配）。"""
    league = _cn_league(hist)
    candidates = [
        c
        for day in _nearby(hist.match_date)
        for c in by_key.get((league, hist.goals_home, hist.goals_away, day), [])
        if c.sid not in used
    ]
    if len(candidates) == 1:
        used.add(candidates[0].sid)
        return candidates[0], False
    return None, len(candidates) > 1


def _score_date_round(
    hist: HistRow,
    by_key: dict[tuple[str, int, int, str], list[FixtureRow]],
    used: set[str],
) -> tuple[FixtureRow | None, bool]:
    """
    比分+日期两步（逐条交错）：精确日唯一→±1 窗唯一；多候选=歧义不硬配。

    学习轮与兜底轮共用（同源谓词，防两轮口径漂移）。交错语义 2026-10-07
    终裁：真树十年实测比两遍结构（精确日全域优先）净多 528 对/门③ 多
    54 对——代价是乱序输入下漂移行可能先占邻日 fixture 挤出歧义（correctness
    反例，测试钉死该权衡）；真树输入按日期定序，精确日对天然先处理。
    """
    fixture = _exact_day_round(hist, by_key, used)
    if fixture is not None:
        return fixture, False
    return _window_round(hist, by_key, used)


def _learn_name_votes(
    active: list[HistRow],
    by_key: dict[tuple[str, int, int, str], list[FixtureRow]],
) -> dict[tuple[str, str], Counter[str]]:
    """比分+日期两步高置信对 → 队名映射票（空名侧不投票——门③场景）。"""
    used: set[str] = set()
    votes: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for hist in active:
        fixture, _ambiguous = _score_date_round(hist, by_key, used)
        if fixture is None or not (hist.home and hist.away):
            continue
        league = _cn_league(hist)
        if fixture.home:
            votes[(league, hist.home)][fixture.home] += 1
        if fixture.away:
            votes[(league, hist.away)][fixture.away] += 1
    return votes


def _name_round_candidates(
    hist: HistRow,
    links: dict[tuple[str, str], set[str]],
    by_name: dict[tuple[str, str, str, int, int], list[FixtureRow]],
    used: set[str],
) -> list[FixtureRow]:
    """名字身份轮候选（映射名×比分+日期±3 窗；无映射/无候选=空表）。"""
    league = _cn_league(hist)
    home_names = links.get((league, hist.home)) if hist.home else None
    away_names = links.get((league, hist.away)) if hist.away else None
    if not (home_names and away_names):
        return []
    return [
        fixture
        for home_name in home_names
        for away_name in away_names
        for fixture in by_name.get(
            (league, home_name, away_name, hist.goals_home, hist.goals_away), []
        )
        if fixture.sid not in used
        and _day_distance(hist.match_date, fixture.kickoff_day) <= _NAME_ROUND_DAYS
    ]


def _partition_scope(
    hists: list[HistRow],
) -> tuple[list[HistRow], list[HistRow]]:
    """(active, scope_excluded) 两分——口径外行不参与配对与分母。"""
    active: list[HistRow] = []
    excluded: list[HistRow] = []
    for hist in hists:
        bucket = (
            excluded if _scope_excluded(_cn_league(hist), hist.match_date) else active
        )
        bucket.append(hist)
    return active, excluded


def _build_indexes(
    fixtures: list[FixtureRow],
) -> tuple[
    dict[tuple[str, int, int, str], list[FixtureRow]],
    dict[tuple[str, str, str, int, int], list[FixtureRow]],
]:
    """(比分+日期键索引, 联赛+名字+比分键索引)——两轮配对共用。"""
    by_key: dict[tuple[str, int, int, str], list[FixtureRow]] = {}
    by_name: dict[tuple[str, str, str, int, int], list[FixtureRow]] = {}
    for fixture in fixtures:
        key = (
            fixture.league,
            fixture.goals_home,
            fixture.goals_away,
            fixture.kickoff_day,
        )
        by_key.setdefault(key, []).append(fixture)
        name_key = (
            fixture.league,
            fixture.home,
            fixture.away,
            fixture.goals_home,
            fixture.goals_away,
        )
        by_name.setdefault(name_key, []).append(fixture)
    return by_key, by_name


def _decide_round_one(
    hist: HistRow,
    links: dict[tuple[str, str], set[str]],
    by_name: dict[tuple[str, str, str, int, int], list[FixtureRow]],
    by_key: dict[tuple[str, int, int, str], list[FixtureRow]],
    used: set[str],
) -> tuple[FixtureRow | None, bool, bool]:
    """
    配对轮单条决断 → (配对 fixture, 是否名字轮所配, 是否歧义)。

    名字+比分+±3 唯一=配（多候选=歧义不硬配）；无映射/无候选退回
    比分+日期两步（交错）；fixture None + 歧义 False = 缺口。
    """
    candidates = _name_round_candidates(hist, links, by_name, used)
    if candidates:
        if len(candidates) == 1:
            used.add(candidates[0].sid)
            return candidates[0], True, False
        return None, False, True
    fixture, ambiguous = _score_date_round(hist, by_key, used)
    return fixture, False, ambiguous


def match_fixtures(fixtures: list[FixtureRow], hists: list[HistRow]) -> MatchResult:
    """
    门① v3 匹配锚：名字身份轮优先，比分+日期两步兜底（宁缺毋错）。

    两轮结构：
    - **学习轮**：比分+日期两步（交错）先跑一遍，从高置信对收 fdhist→
      语料队名映射票（≥2 票入选；同队站名变体并存——多映射集合）。
    - **配对轮**（逐条交错，2026-10-07 终裁——真树十年比两遍结构净多
      528 对/门③ 多 54 对；代价=乱序输入下漂移行可能先占邻日 fixture
      挤出歧义，correctness 反例已钉测试。真树输入按日期定序无此忧）：
      名字+比分+日期±3 优先（名字近唯一身份，±3 吸收改期/基准差尾部；
      多候选=歧义不硬配）→ 无映射/无候选退回比分+日期两步。配过的
      fixture 不重复使用；门③（understat，空名侧）自动跳过名字轮。

    CorpusScope 口径外（比甲 playoff/苏超 split）先行单列，不参与配对
    与分母——粒度=月（比甲 4-6 月含常规赛四月尾巴，跨源无轮次字段，
    精化留用户裁决）。输入确定性（列表序即处理序），报告幂等。
    """
    result = MatchResult()
    active, result.scope_excluded = _partition_scope(hists)
    by_key, by_name = _build_indexes(fixtures)
    votes = _learn_name_votes(active, by_key)
    links: dict[tuple[str, str], set[str]] = {
        key: {name for name, count in counter.items() if count >= _NAME_VOTE_MIN}
        for key, counter in votes.items()
    }
    result.name_links_learned = sum(len(names) for names in links.values())

    used: set[str] = set()
    for hist in active:
        fixture, via_name, ambiguous = _decide_round_one(
            hist, links, by_name, by_key, used
        )
        if fixture is not None:
            result.pairs.append((fixture, hist))
            result.matched_via_name += via_name
        elif ambiguous:
            result.ambiguous.append(hist)
        else:
            result.gaps.append(hist)
    result.extra_fixtures.extend(f for f in fixtures if f.sid not in used)
    return result


def _fetch_fixtures(con: duckdb.DuckDBPyConnection) -> list[FixtureRow]:
    leagues = ",".join("'" + n + "'" for n in LEAGUE_TO_FD)
    window_start = _WINDOW_START.isoformat()
    rows = con.execute(
        f"""
        SELECT sid, league, strftime(kickoff, '%Y-%m-%d') AS day,
               home_goals, away_goals, home, away, season
        FROM fixture_universe
        WHERE league IN ({leagues})
          AND kickoff >= TIMESTAMP '{window_start}'
        """  # noqa: S608 联赛码/窗口=模块常量
    ).fetchall()
    return [
        FixtureRow(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            int(r[3]),
            int(r[4]),
            home=str(r[5] or ""),
            away=str(r[6] or ""),
            season=str(r[7]) if r[7] is not None else None,
        )
        for r in rows
    ]


def _hist_covered(row: HistRow, done_dates: set[str]) -> bool:
    """Fdhist 行是否落在夜班已完成日期面（±1 窗内任一日 done）。"""
    return any(day in done_dates for day in _nearby(row.match_date))


def _fetch_hists(con: duckdb.DuckDBPyConnection, settled: set[str]) -> list[HistRow]:
    """运行面 hist_matches（11 联赛、对账窗 2016 起、已判定日期面，定序输出）。"""
    codes = _sql_in(LEAGUE_TO_FD.values())
    max_day = max(settled) if settled else _WINDOW_START.isoformat()
    cover_end = (date.fromisoformat(max_day) + timedelta(days=1)).isoformat()
    window_start = _WINDOW_START.isoformat()
    rows = con.execute(
        f"""
        SELECT competition, match_date, home_team, away_team, fthg, ftag,
               psc_home, psc_draw, psc_away
        FROM goalx.hist_matches
        WHERE competition IN ({codes})
          AND match_date >= '{window_start}'
          AND match_date <= '{cover_end}'
        ORDER BY match_date, competition, home_team, away_team
        """  # noqa: S608 联赛码/窗口=常量与已判定日期集
    ).fetchall()
    hists = [
        HistRow(
            league_key=str(r[0]),
            match_date=str(r[1])[:10],
            home=str(r[2]),
            away=str(r[3]),
            goals_home=int(r[4]),
            goals_away=int(r[5]),
            psc_home=r[6],
            psc_draw=r[7],
            psc_away=r[8],
        )
        for r in rows
        if r[4] is not None and r[5] is not None
    ]
    return [h for h in hists if _hist_covered(h, settled)]


def gate1_fixture_reconciliation(
    matched: MatchResult,
    hists: list[HistRow],
    done_dates: set[str],
    *,
    admin_excluded: int,
) -> dict[str, Any]:
    """
    门①：场次对账（重叠匹配率≥99%、缺口逐场归因——JSON 全量）。

    hists 应已剔除行政判赛排除行（A4，build 入口统一过滤后传入）；
    admin_excluded=被剔行数（必传——0 须是调用方显式声明，不设默认），
    报告单列不藏。
    """
    scope_ids = {id(h) for h in matched.scope_excluded}
    active = [h for h in hists if id(h) not in scope_ids]

    per_competition: dict[str, dict[str, int]] = {}

    def entry_of(hist: HistRow) -> dict[str, int]:
        league = _cn_league(hist)
        if league not in per_competition:
            per_competition[league] = {
                "fdhist": 0,
                "matched": 0,
                "gaps": 0,
                "ambiguous": 0,
            }
        return per_competition[league]

    for hist in active:
        entry_of(hist)["fdhist"] += 1
    for _fixture, hist in matched.pairs:
        entry_of(hist)["matched"] += 1
    for hist in matched.gaps:
        entry_of(hist)["gaps"] += 1
    for hist in matched.ambiguous:
        entry_of(hist)["ambiguous"] += 1
    rate = matched.rate
    gaps_sorted = sorted(
        matched.gaps, key=lambda g: (g.match_date, g.league_key, g.home, g.away)
    )

    def cause_of(hist: HistRow) -> str:
        # 差分归因：邻日有 done 页=夜班跑过而语料缺场；只有 not_found=源T 无日页
        # （比甲 playoff/苏超 split 已先行单列 scope_excluded，不在此归因）
        if any(day in done_dates for day in _nearby(hist.match_date)):
            return "语料缺场（该日期夜班已完成）"
        return "源T 日页缺席（not_found——站点无该日 CorpusScope 页）"

    return {
        "threshold": _RATE_THRESHOLD,
        "matched": len(matched.pairs),
        "matched_via_name": matched.matched_via_name,
        "name_links_learned": matched.name_links_learned,
        "admin_excluded": admin_excluded,
        "gaps": len(matched.gaps),
        "ambiguous": len(matched.ambiguous),
        "scope_excluded": len(matched.scope_excluded),
        "extra_fixtures": len(matched.extra_fixtures),
        "rate": rate,
        "per_competition": per_competition,
        "gap_attribution": [
            {
                "competition": _cn_league(h),
                "date": h.match_date,
                "match": f"{h.home} vs {h.away}",
                "score": f"{h.goals_home}-{h.goals_away}",
                "cause": cause_of(h),
            }
            for h in gaps_sorted
        ],
        "ambiguous_attribution": [
            {
                "competition": _cn_league(h),
                "date": h.match_date,
                "match": f"{h.home} vs {h.away}",
                "score": f"{h.goals_home}-{h.goals_away}",
            }
            for h in sorted(
                matched.ambiguous, key=lambda g: (g.match_date, g.league_key, g.home)
            )
        ],
        "scope_excluded_attribution": [
            {
                "competition": _cn_league(h),
                "date": h.match_date,
                "match": f"{h.home} vs {h.away}",
            }
            for h in sorted(
                matched.scope_excluded,
                key=lambda g: (g.match_date, g.league_key, g.home),
            )
        ],
        "verdict": _verdict(rate, rate is not None and rate >= _RATE_THRESHOLD),
    }


def _anchor_last_pre_kickoff(
    con: duckdb.DuckDBPyConnection, sids: list[str]
) -> dict[str, tuple[float, float, float]]:
    """门②锚价：cid177 赛前末可见三元组（last_pre_kickoff 代理）。"""
    if not sids:
        return {}
    quoted = _sql_in(sids)
    anchor = _ANCHOR_BOOK
    rows = con.execute(
        f"""
        SELECT sid,
               last(odds_home ORDER BY published_at, source_order)
                   FILTER (WHERE published_at < kickoff),
               last(odds_draw ORDER BY published_at, source_order)
                   FILTER (WHERE published_at < kickoff),
               last(odds_away ORDER BY published_at, source_order)
                   FILTER (WHERE published_at < kickoff)
        FROM odds_change_event
        WHERE market = '1x2' AND bookmaker_id = '{anchor}'
          AND sid IN ({quoted})
        GROUP BY sid
        """  # noqa: S608 锚书商=常量；sid=语料内值
    ).fetchall()
    return {
        str(r[0]): (float(r[1]), float(r[2]), float(r[3]))
        for r in rows
        if r[1] is not None and r[2] is not None and r[3] is not None
    }


def _dev_stats(devs: list[float]) -> dict[str, float | int | None]:
    """偏差分布摘要（n/均值/中位数/p95/max——裁决看中位数，尾部不藏）。"""
    ordered = sorted(devs)
    if not ordered:
        return {
            "comparisons": 0,
            "mean": None,
            "median": None,
            "p95": None,
            "max": None,
        }
    return {
        "comparisons": len(ordered),
        "mean": mean(ordered),
        "median": median(ordered),
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "max": ordered[-1],
    }


def gate2_psc_anchor(
    con: duckdb.DuckDBPyConnection,
    pairs: list[tuple[FixtureRow, HistRow]],
    *,
    today: date,
) -> dict[str, Any]:
    """
    门②：PSC×cid177 赛前末可见价偏差（v2：裁决=中位数<1%）。

    锚书 cid177 2023/24 起才报价（十年窗老季 usable 塌缩在 per_season 如实
    可见）；均值/p95/max 同报，|偏差|>100% 进异常清单供人工判读（晚场锚价
    陈旧/单场异常不再撬动裁决）。A5 成熟度纪律：开球不足 6 个月的行 PSC
    视为暂定（未收敛），跳过比较单列 immature_skipped——2025/26 门②
    median 1.3% 即 PSC 未成熟轨迹，纪律落地后自然消解。
    """
    anchors = _anchor_last_pre_kickoff(con, [f.sid for f, _ in pairs])
    usable = 0
    immature_skipped = 0
    devs_by_season: dict[str, list[float]] = defaultdict(list)
    outliers: list[dict[str, Any]] = []
    for fixture, hist in pairs:
        if not psc_mature(hist.match_date, today=today):
            immature_skipped += 1
            continue
        anchor = anchors.get(fixture.sid)
        raw_psc = (hist.psc_home, hist.psc_draw, hist.psc_away)
        if anchor is None or None in raw_psc:
            continue
        psc = cast("tuple[float, float, float]", raw_psc)
        usable += 1
        raw_season = fixture.season
        # 赛季标签归一：silver 缺季字段落 "_unknown" 分区（真值口径），
        # 与 None 一起回退开球年，防同季裂桶
        season = (
            raw_season
            if raw_season not in (None, "", "_unknown")
            else fixture.kickoff_day[:4]
        )
        for outcome, anchor_odds, psc_odds in (
            ("home", anchor[0], psc[0]),
            ("draw", anchor[1], psc[1]),
            ("away", anchor[2], psc[2]),
        ):
            if psc_odds <= 0:
                continue
            dev = abs(anchor_odds - psc_odds) / psc_odds
            devs_by_season[season].append(dev)
            if dev > _PSC_OUTLIER_REL:
                outliers.append(
                    {
                        "sid": fixture.sid,
                        "season": season,
                        "competition": fixture.league,
                        "day": fixture.kickoff_day,
                        "match": f"{hist.home} vs {hist.away}",
                        "outcome": outcome,
                        "anchor_odds": round(anchor_odds, 2),
                        "psc": psc_odds,
                        "relative_deviation": round(dev, 4),
                    }
                )
    devs = [d for season_devs in devs_by_season.values() for d in season_devs]
    stats = _dev_stats(devs)
    dev_median = stats.get("median")
    return {
        "threshold": _PSC_THRESHOLD,
        "verdict_metric": "median",
        "pairs": len(pairs),
        "usable": usable,
        "immature_skipped": immature_skipped,
        "outcome_comparisons": stats.get("comparisons", 0),
        "mean_relative_deviation": stats.get("mean"),
        "median_relative_deviation": dev_median,
        "p95_relative_deviation": stats.get("p95"),
        "max_relative_deviation": stats.get("max"),
        "per_season": {
            season: _dev_stats(season_devs)
            for season, season_devs in sorted(devs_by_season.items())
        },
        "outliers": sorted(
            outliers, key=lambda o: o["relative_deviation"], reverse=True
        ),
        "verdict": (
            "insufficient"
            if dev_median is None
            else "pass"
            if dev_median < _PSC_THRESHOLD
            else "fail"
        ),
    }


def gate3_xg_understat(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """门③：understat×源T xG 对账（五大 2025+；日期±1+比分锚定）。"""
    leagues = ",".join("'" + n + "'" for n in LEAGUE_TO_FD)
    corpus_rows = con.execute(
        f"""
        SELECT f.sid, f.league, strftime(f.kickoff, '%Y-%m-%d') AS day,
               f.home_goals, f.away_goals, x.xg_home, x.xg_away
        FROM fixture_universe f
        JOIN xg_observation x ON x.sid = f.sid
        WHERE f.league IN ({leagues}) AND x.has_xg
          AND x.xg_home IS NOT NULL AND x.xg_away IS NOT NULL
        """  # noqa: S608 联赛码=模块常量
    ).fetchall()
    corpus = [
        FixtureRow(str(r[0]), str(r[1]), str(r[2]), int(r[3]), int(r[4]))
        for r in corpus_rows
    ]
    xg_by_sid = {str(r[0]): (float(r[5]), float(r[6])) for r in corpus_rows}
    understat_codes = _sql_in(UNDERSTAT_LEAGUES.values())
    season_min = _XG_SEASON_MIN
    u_rows = con.execute(
        f"""
        SELECT league, strftime(CAST(datetime_utc AS TIMESTAMP), '%Y-%m-%d') AS d,
               goals_home, goals_away, npxg_home, npxg_away
        FROM goalx.understat_matches
        WHERE league IN ({understat_codes}) AND CAST(season AS INTEGER) >= {season_min}
          AND npxg_home IS NOT NULL AND npxg_away IS NOT NULL
          AND goals_home IS NOT NULL AND goals_away IS NOT NULL
        ORDER BY league, d, goals_home, goals_away
        """  # noqa: S608 联盟码/赛季下界=模块常量
    ).fetchall()
    u_league_by_code = {
        code: FD_TO_LEAGUE[fd] for fd, code in UNDERSTAT_LEAGUES.items()
    }
    hists = [
        HistRow(
            league_key=str(r[0]),
            match_date=str(r[1]),
            home="",
            away="",
            goals_home=int(r[2]),
            goals_away=int(r[3]),
            psc_home=None,
            psc_draw=None,
            psc_away=None,
        )
        for r in u_rows
    ]
    # understat 联盟码先映回中文联赛再走同一匹配锚
    for hist in hists:
        hist.league_key = u_league_by_code.get(hist.league_key, hist.league_key)
    matched = match_fixtures(corpus, hists)
    u_index = {
        (
            u_league_by_code.get(str(r[0]), str(r[0])),
            str(r[1]),
            int(r[2]),
            int(r[3]),
        ): (float(r[4]), float(r[5]))
        for r in u_rows
    }
    diffs: list[float] = []
    anomalies: list[dict[str, Any]] = []
    for fixture, hist in matched.pairs:
        xg_h, xg_a = xg_by_sid[fixture.sid]
        under = u_index[
            (hist.league_key, hist.match_date, hist.goals_home, hist.goals_away)
        ]
        for side, corpus_xg, under_xg in (
            ("home", xg_h, under[0]),
            ("away", xg_a, under[1]),
        ):
            diff = corpus_xg - under_xg
            diffs.append(abs(diff))
            if abs(diff) > _XG_ANOMALY_ABS:
                anomalies.append(
                    {
                        "sid": fixture.sid,
                        "day": fixture.kickoff_day,
                        "side": side,
                        "corpus_xg": round(corpus_xg, 3),
                        "understat_xg": round(under_xg, 3),
                        "diff": round(diff, 3),
                    }
                )
    mae = mean(diffs) if diffs else None
    correlation = _pearson(matched, xg_by_sid, u_index)
    return {
        "threshold": None,  # 不设硬线（双源异构），供人工判读
        "pairs": len(matched.pairs),
        "gaps_understat_unmatched": len(matched.gaps),
        "value_comparisons": len(diffs),
        "mae": mae,
        "pearson_r": correlation,
        "anomaly_abs_threshold": _XG_ANOMALY_ABS,
        "anomalies": sorted(anomalies, key=lambda a: abs(a["diff"]), reverse=True)[:50],
        "verdict": "insufficient" if mae is None else "report_only",
    }


def _pearson(
    matched: MatchResult,
    xg_by_sid: dict[str, tuple[float, float]],
    u_index: dict[tuple[str, str, int, int], tuple[float, float]],
) -> float | None:
    """配对场的语料 xG 与 understat npxG 的皮尔逊相关（双侧合并）。"""
    xs: list[float] = []
    ys: list[float] = []
    for fixture, hist in matched.pairs:
        under = u_index.get(
            (hist.league_key, hist.match_date, hist.goals_home, hist.goals_away)
        )
        if under is None:
            continue
        xg_h, xg_a = xg_by_sid[fixture.sid]
        xs.extend((xg_h, xg_a))
        ys.extend(under)
    if len(xs) < _MIN_CORR_POINTS:
        return None
    mx, my = mean(xs), mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    vy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def _silver_root(store: CorpusStore, dataset: str) -> Path:
    """语料 silver 数据集根（布局唯一落点在 CorpusStore.silver_path）。"""
    return store.silver_path(srct.SRCT_PROVIDER, dataset)


def _read_meta(store: CorpusStore, dataset: str) -> dict[str, Any]:
    path = _silver_root(store, dataset) / "_meta.json"
    if not path.exists():
        return {}
    return dict(json.loads(path.read_text(encoding="utf-8")))


def gate4_conversion(store: CorpusStore) -> dict[str, Any]:
    """门④：转换完整性（unexplained_gap 必须为 0）。"""
    odds_meta = _read_meta(store, "odds_change_event")
    book_meta = _read_meta(store, "bookmaker")
    gap = odds_meta.get("unexplained_gap")
    return {
        "threshold": 0,
        "odds_change_event": odds_meta,
        "bookmaker": book_meta,
        "unexplained_gap": gap,
        "verdict": "insufficient" if gap is None else "pass" if gap == 0 else "fail",
    }


def _is_temp_file(name: str) -> bool:
    """落盘中工件（ingest 原子写后缀 .part + 旧 .tmp）——计数与扫描都跳过。"""
    return name.endswith(".part") or name.endswith(".tmp")


def _count_raw_files(store: CorpusStore, dataset: str) -> int:
    root = store.raw_dir(srct.SRCT_PROVIDER, dataset)
    if not root.exists():
        return 0
    return sum(1 for p in root.iterdir() if p.is_file() and not _is_temp_file(p.name))


def _scan_template_pages(
    store: CorpusStore, dataset: str, markers: tuple[str, ...]
) -> tuple[set[str], int, int]:
    """
    Raw 页按解析守卫同源谓词分类 → (当前模板页 sid 集, 伪 200 页数, 不可读页数)。

    流式读页首 64KB 早退（标记实测全在 29KB 内）；UTF-8 页字节级搜索与
    解码后搜索等价。sid 取自文件名（去 .gz 与端点扩展名）。标记组任一
    命中=可解析模板页（detail/analysis v3 起两代模板并存）。三类互斥都出
    分母：缺全部标记=再改版页（template_mismatch 桶）；带伪 200 图=站点
    无此页（pseudo_200 桶，缺席内容非采集失败）；解压失败=撕裂/截断 gzip
    （unreadable 桶，真树 2026-10-03 撕裂史）——单页坏不炸全报告。返回
    sid 集供分子交集（bronze 残留行其 raw 非模板页时剔出分子，防覆盖>1）。
    """
    root = store.raw_dir(srct.SRCT_PROVIDER, dataset)
    if not root.exists():
        return set(), 0, 0
    marker_bodies = [m.encode() for m in markers]
    unavailable_bytes = srct.CONTENT_404_MARKER.encode()
    template_sids: set[str] = set()
    pseudo_200 = 0
    unreadable = 0
    for path in root.iterdir():
        if not path.is_file() or _is_temp_file(path.name):
            continue
        sid = path.name.removesuffix(".gz").removesuffix(".html").removesuffix(".htm")
        try:
            with gzip.open(path, "rb") as fh:
                head = fh.read(_TEMPLATE_SCAN_BYTES)
        except (OSError, EOFError):  # BadGzipFile/截断 member——bucket 不炸
            unreadable += 1
            continue
        if unavailable_bytes in head:
            pseudo_200 += 1
        elif any(m in head for m in marker_bodies):
            template_sids.add(sid)
    return template_sids, pseudo_200, unreadable


def _count_bronze_sids(store: CorpusStore, dataset: str) -> set[str]:
    """Bronze 去重 sid 集（流式逐行 json 只取 sid，不整载 payload）。"""
    path = store.bronze_path(srct.SRCT_PROVIDER, dataset)
    keys: set[str] = set()
    if not path.exists():
        return keys
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                keys.add(str(json.loads(line)["sid"]))
    return keys


def _silver_digest(store: CorpusStore, dataset: str) -> str:
    """数据集 parquet 摘要（跨次报告比对 = 重建幂等佐证）。"""
    sha = hashlib.sha256()
    root = _silver_root(store, dataset)
    for part in sorted(root.glob("**/data.parquet")) if root.exists() else []:
        sha.update(part.relative_to(root).as_posix().encode())
        sha.update(part.read_bytes())
    return sha.hexdigest()[:16]


def gate5_pipeline_health(
    store: CorpusStore, today: date, *, settled: set[str]
) -> dict[str, Any]:
    """
    门⑤：管线健康（解析率代理/夜班台账/Phase1 进度/silver 版本面）。

    键覆盖分母=含当前解析模板标记的页（2026-10-07 裁决）：标记组任一
    命中即入分母——detail/analysis v3 分发后两代模板页（2020 换模板前后）
    都可解析产 bronze；全组缺席=站点再改版，单列 template_mismatch；伪
    200 图页单列 pseudo_200（站点无此页，缺席内容）——两者均非采集失败。
    无模板标记数据集（day_page/odds_1x2d）分母维持 raw 全量。

    进度分母=phase1_dates 任务清单，分子=settled（done|not_found——夜班
    对两者的判定都算完成，与 pending_dates 口径一致）。数据集集从端点
    注册表派生（票 59 起：retired 留档数据集不进健康面）。
    """
    datasets = (
        srct.DAY_DATASET,
        *srct.match_endpoint_datasets(srct.DEPTH_FULL),
    )
    per_dataset: dict[str, dict[str, int | float | None]] = {}
    worst: float | None = None
    mismatch_total = 0
    pseudo_200_total = 0
    unreadable_total = 0
    for dataset in datasets:
        raw_count = _count_raw_files(store, dataset)
        bronze_sids = _count_bronze_sids(store, dataset)
        markers = srct.TEMPLATE_MARKERS.get(dataset)
        if markers:
            template_sids, pseudo_200, unreadable = _scan_template_pages(
                store, dataset, markers
            )
            template_count = len(template_sids)
            mismatch = raw_count - template_count - pseudo_200 - unreadable
            # 分子=bronze sid ∩ 模板页 sid（残留行其 raw 非模板页剔出）
            covered = len(bronze_sids & template_sids)
        else:
            template_count, pseudo_200, unreadable, mismatch = raw_count, 0, 0, 0
            covered = len(bronze_sids)
        mismatch_total += mismatch
        pseudo_200_total += pseudo_200
        unreadable_total += unreadable
        if template_count:
            rate = covered / template_count
        elif raw_count:
            # 有采集而无任何可解析模板页=站点再改版的红灯（空≠无），不与
            # 未采集（legit empty=None）混口径——correctness-review 反例
            rate = 0.0
        else:
            rate = None
        per_dataset[dataset] = {
            "raw_files": raw_count,
            "parser_template_files": template_count,
            "template_mismatch": mismatch,
            "pseudo_200": pseudo_200,
            "unreadable": unreadable,
            "bronze_keys": len(bronze_sids),
            "key_coverage": rate,
        }
        if rate is not None:
            worst = rate if worst is None else min(worst, rate)
    summaries = store.night_summaries(limit=1000)
    stopped: dict[str, int] = {}
    for row in summaries:
        reason = str(row.get("stopped") or "clean")
        stopped[reason] = stopped.get(reason, 0) + 1
    total_dates = len(phase1_dates(today))
    done_dates = store.day_status_dates("done")
    progress = len(settled) / total_dates if total_dates else None
    silver: dict[str, dict[str, Any]] = {}
    for dataset in (
        "fixture_universe",
        "xg_observation",
        "odds_change_event",
        "bookmaker",
    ):
        meta = _read_meta(store, dataset)
        silver[dataset] = {
            "silver_version": meta.get("silver_version"),
            "built_at": meta.get("built_at"),
            "digest": _silver_digest(store, dataset),
        }
    return {
        "threshold": _RATE_THRESHOLD,
        "per_dataset_key_coverage": per_dataset,
        "worst_key_coverage": worst,
        "template_mismatch_total": mismatch_total,
        "pseudo_200_total": pseudo_200_total,
        "unreadable_total": unreadable_total,
        "night_summaries": {
            "nights": len(summaries),
            "requests_total": sum(
                int(value)
                for r in summaries
                if isinstance(value := r.get("requests"), int)
            ),
            "stopped_reasons": stopped,
        },
        "phase1_progress": {
            "settled_dates": len(settled),
            "done_dates": len(done_dates),
            "not_found_dates": len(settled) - len(done_dates),
            "total_dates": total_dates,
            "progress": progress,
        },
        "silver": silver,
        "verdict": "insufficient"
        if worst is None
        else "pass"
        if worst >= _RATE_THRESHOLD
        else "fail",
    }


def coverage_matrix(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """逐赛事×书商报价存在矩阵（design-15 §七 裁决：门报告物化）。"""
    rows = con.execute(
        """
        SELECT f.league AS competition, e.bookmaker_id,
               count(DISTINCT e.sid) AS matches,
               count(DISTINCT e.sid) FILTER (
                   WHERE e.published_at < e.kickoff
               ) AS pre_matches
        FROM odds_change_event e
        JOIN fixture_universe f ON f.sid = e.sid
        GROUP BY f.league, e.bookmaker_id
        """
    ).fetchall()
    matrix: dict[str, dict[str, dict[str, int | float]]] = {}
    totals = con.execute(
        "SELECT league, count(*) FROM fixture_universe GROUP BY league"
    ).fetchall()
    comp_matches = {str(r[0]): int(r[1]) for r in totals}
    for competition, book_id, matches, pre_matches in rows:
        entry = matrix.setdefault(str(competition), {})
        total = comp_matches.get(str(competition), 0)
        entry[str(book_id)] = {
            "matches": int(matches),
            "presence_rate": int(matches) / total if total else 0.0,
            "pre_kickoff_matches": int(pre_matches),
        }
    return {"competitions": len(matrix), "matrix": matrix}


def build_phase1_gate_report(
    store: CorpusStore, settings: Settings, *, today: date | None = None
) -> dict[str, Any]:
    """
    跑五门+coverage，写 JSON/MD 两视图到语料树 reports/，返回报告 dict。

    Phase1 进度 <100% 时 overall=provisional（数据完整性先行，不放行）。
    前置条件：运行面库已迁移（门①读 admin_match_exclusions，缺表即炸
    fail-closed）；CLI 入口已前置 migrate，直接调用方自行确保。
    """
    resolved_today = today if today is not None else datetime.now().date()
    store.ensure_tree()
    # 新鲜度保证：报告读视图前先幂等重建 corpus.duckdb（防止 silver 重建后
    # 视图滞后——版本戳与数字解耦）
    corpus_duckdb.build_corpus_duckdb(store)
    done_dates = store.day_status_dates("done")
    settled_dates = done_dates | store.day_status_dates("not_found")
    con = corpus_duckdb.connect(settings)
    try:
        fixtures = _fetch_fixtures(con)
        all_hists = _fetch_hists(con, settled_dates)
        # A4：行政判赛排除行在对账集入口统一剔除（比分真、比赛假——
        # 训练集与对账集两侧同规则；行数单列进门①报告不藏）
        face = db.connect(settings.db_path, readonly=True)
        try:
            exclusions = admin_exclusions(face)
        finally:
            face.close()
        hists = [
            h
            for h in all_hists
            if not is_admin_excluded(
                h.league_key, h.home, h.away, h.match_date, exclusions
            )
        ]
        matched = match_fixtures(fixtures, hists)
        gate1 = gate1_fixture_reconciliation(
            matched,
            hists,
            done_dates,
            admin_excluded=len(all_hists) - len(hists),
        )
        gate2 = gate2_psc_anchor(con, matched.pairs, today=resolved_today)
        gate3 = gate3_xg_understat(con)
        coverage = coverage_matrix(con)
    finally:
        con.close()
    gate4 = gate4_conversion(store)
    gate5 = gate5_pipeline_health(store, resolved_today, settled=settled_dates)
    progress = gate5["phase1_progress"]["progress"]
    report: dict[str, Any] = {
        "generated_at": datetime.now(tz=None).isoformat(timespec="seconds"),
        "corpus_root": str(store.root),
        "phase1_progress": progress,
        "overall": "provisional" if progress != 1.0 else "pending_user",
        "gates": {
            "1_fixture_reconciliation": gate1,
            "2_psc_cid177": gate2,
            "3_xg_understat": gate3,
            "4_conversion": gate4,
            "5_pipeline_health": gate5,
        },
        "coverage": coverage,
    }
    reports = store.root / REPORTS_DIR
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{REPORT_BASENAME}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (reports / f"{REPORT_BASENAME}.md").write_text(
        _render_markdown(report), encoding="utf-8"
    )
    return report


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _render_markdown(report: dict[str, Any]) -> str:
    """人读视图：五门各一节 + coverage 摘要（矩阵细节在 JSON）。"""
    gates = report["gates"]
    gate1 = gates["1_fixture_reconciliation"]
    gate2 = gates["2_psc_cid177"]
    gate3 = gates["3_xg_understat"]
    gate4 = gates["4_conversion"]
    gate5 = gates["5_pipeline_health"]
    nights = gate5["night_summaries"]
    lines = [
        "# Phase 1 验证门报告",
        "",
        "生成："
        + str(report["generated_at"])
        + " · 语料根："
        + str(report["corpus_root"]),
        "Phase1 进度："
        + _fmt(report["phase1_progress"])
        + " · overall：**"
        + str(report["overall"])
        + "**",
        "",
        "门=报告+用户点头（无自动放行）；provisional = 回填未完成，"
        + "阈值判定只描述当前树。",
        "",
        "## 门① 场次对账 fdhist×源T（≥99%）",
        "",
        "- 匹配 "
        + str(gate1["matched"])
        + "（名字身份轮 "
        + str(gate1["matched_via_name"])
        + "，队名映射 "
        + str(gate1["name_links_learned"])
        + " 条）/ 行政判赛排除 "
        + str(gate1["admin_excluded"])
        + " / 缺口 "
        + str(gate1["gaps"])
        + " / 歧义 "
        + str(gate1["ambiguous"])
        + " / 口径外 "
        + str(gate1["scope_excluded"])
        + " / 语料多出 "
        + str(gate1["extra_fixtures"])
        + " · 匹配率 **"
        + _fmt(gate1["rate"])
        + "** · verdict `"
        + str(gate1["verdict"])
        + "`",
        *(
            [
                "  - 注：语料多出含行政判赛排除行的语料侧对应场次（fdhist 行"
                + "已剔除、其 fixture 落此处）——非真冗余，勿当采集重复排查"
            ]
            if gate1["admin_excluded"] > 0
            else []
        ),
        "",
        "| 联赛 | fdhist | matched | gaps | ambiguous |",
        "|---|---|---|---|---|",
    ]
    for competition, entry in sorted(gate1["per_competition"].items()):
        lines.append(
            f"| {competition} | {entry['fdhist']} | {entry['matched']} | "
            + f"{entry['gaps']} | {entry['ambiguous']} |"
        )
    gaps = gate1["gap_attribution"]
    if gaps:
        lines += ["", f"缺口归因（共 {len(gaps)}，前 50——全量见 JSON）："]
        lines += [
            f"- {g['date']} {g['competition']} {g['match']} {g['score']}——{g['cause']}"
            for g in gaps[:50]
        ]
    lines += [
        "",
        "## 门② PSC×cid177 赛前末可见价（中位数<1%）",
        "",
        "- 配对 "
        + str(gate2["pairs"])
        + "（可用 "
        + str(gate2["usable"])
        + "，PSC 未成熟跳过 "
        + str(gate2["immature_skipped"])
        + "（开球不足 6 个月，A5 纪律），Outcome 比较 "
        + str(gate2["outcome_comparisons"])
        + " 次）· 中位数偏差 **"
        + _fmt(gate2["median_relative_deviation"])
        + "** · 均值 "
        + _fmt(gate2["mean_relative_deviation"])
        + " · p95 "
        + _fmt(gate2["p95_relative_deviation"])
        + " · 最大 "
        + _fmt(gate2["max_relative_deviation"])
        + " · verdict `"
        + str(gate2["verdict"])
        + "`（裁决口径=中位数，尾部不敏感）",
        "",
        "| 赛季 | 比较 | 均值 | 中位数 | p95 | max |",
        "|---|---|---|---|---|---|",
    ]
    for season, entry in gate2["per_season"].items():
        lines.append(
            f"| {season} | {entry.get('comparisons', 0)} | {_fmt(entry.get('mean'))} "
            + f"| {_fmt(entry.get('median'))} | {_fmt(entry.get('p95'))} "
            + f"| {_fmt(entry.get('max'))} |"
        )
    outliers = gate2["outliers"]
    if outliers:
        shown = min(len(outliers), 20)
        lines += [
            "",
            f"异常清单（|偏差|>100%，共 {len(outliers)}，前 {shown}——全量见 JSON）：",
        ]
        lines += [
            f"- {o['day']} {o['competition']} {o['match']} {o['outcome']}: "
            + f"锚 {o['anchor_odds']} vs PSC {o['psc']}"
            + f"（偏差 {o['relative_deviation']}）"
            for o in outliers[:shown]
        ]
    lines += [
        "",
        "## 门③ xG 对账 understat×源T（不设硬线）",
        "",
        "- 配对 "
        + str(gate3["pairs"])
        + " · 值比较 "
        + str(gate3["value_comparisons"])
        + " · MAE **"
        + _fmt(gate3["mae"])
        + "** · Pearson r **"
        + _fmt(gate3["pearson_r"])
        + "** · 异常场（|差|>"
        + _fmt(gate3["anomaly_abs_threshold"])
        + "，前 50）：",
    ]
    anomalies = gate3["anomalies"]
    lines += (
        [
            f"- {a['day']} sid={a['sid']} {a['side']}: 语料 {a['corpus_xg']} vs "
            + f"understat {a['understat_xg']}（差 {a['diff']}）"
            for a in anomalies[:50]
        ]
        if anomalies
        else ["- （无）"]
    )
    lines += [
        "",
        "## 门④ 转换完整性（未解释缺口=0）",
        "",
        "- unexplained_gap = **"
        + str(gate4["unexplained_gap"])
        + "** · verdict `"
        + str(gate4["verdict"])
        + "`",
        "",
        "## 门⑤ 管线健康（键覆盖≥99%，分母=可解析模板页）",
        "",
        "- 最差键覆盖 **"
        + _fmt(gate5["worst_key_coverage"])
        + "** · verdict `"
        + str(gate5["verdict"])
        + "`",
        "- 分母外：旧模板页 "
        + str(gate5["template_mismatch_total"])
        + "（站点约 2020 换模板——解析器 v3 候选内容）· 伪 200 页 "
        + str(gate5["pseudo_200_total"])
        + "（站点无此页——缺席内容）· 不可读页 "
        + str(gate5["unreadable_total"])
        + "（撕裂/截断 gzip——采集层事故，另开票处理；三者均非解析失败）",
        "- 夜班 "
        + str(nights["nights"])
        + " 晚、请求 "
        + str(nights["requests_total"])
        + "、停机原因 "
        + str(nights["stopped_reasons"]),
        "- Phase1 进度 "
        + str(gate5["phase1_progress"]["done_dates"])
        + "/"
        + str(gate5["phase1_progress"]["total_dates"])
        + " 天",
    ]
    for dataset, entry in gate5["per_dataset_key_coverage"].items():
        lines.append(
            f"  - {dataset}: raw {entry['raw_files']} / 模板页 "
            + f"{entry['parser_template_files']}（模板外 {entry['template_mismatch']}"
            + f" · 伪 200 {entry['pseudo_200']}）"
            + f" / bronze 键 {entry['bronze_keys']}"
            + f" · 覆盖 {_fmt(entry['key_coverage'])}"
        )
    coverage = report["coverage"]
    lines += [
        "",
        "## coverage 摘要（矩阵详见 JSON）",
        "",
        "- 赛事 "
        + str(coverage["competitions"])
        + " 项；逐赛事×书商存在率在 reports/phase1-gate.json 的 coverage.matrix。",
    ]
    return "\n".join(lines) + "\n"
