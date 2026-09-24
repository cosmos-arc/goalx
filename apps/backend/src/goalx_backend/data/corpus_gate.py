"""
Phase 1 验证门报告（票 55/56 切片 17）：三对账 + 五条量化门。

门=报告+用户点头，无自动放行（spec story 21/ADR-0011 决策 7）。产出两视图：
机器可读 JSON + 人读 MD（corpus 树 reports/，数据面资产不进 repo）。

- **门① 场次对账**：fdhist×源T fixture_universe，11 个重叠联赛（CorpusScope
  15 项 ∩ FD_COMPETITIONS，ADR-0010）。匹配键=联赛+比分+日期±1（身份绑定
  后置——定则 1，确定性键数值交叉验证钉死后才落；两侧日期基准不同：源T
  北京墙钟、fdhist 赛地本地日，±1 窗吸收）。阈值：重叠匹配率 ≥99%，
  缺口逐场归因（分母只计夜班已完成日期 ±1 内的 fdhist 场次——未回填
  日期不计入，不虚增缺口）。
- **门② PSC×cid177**：fdhist PSC（收盘代理）×语料 cid177 赛前末可见价，
  逐Outcome 相对偏差；阈值：均值 <1%（PSC 与 last_pre_kickoff_visible 均
  为代理口径，报告同时给分布不藏尾部）。
- **门③ xG 对账**：understat×源T xg_observation，五大 2025+，日期±1+比分
  锚定；MAE+相关系数+异常场清单（|差|>1.0，不设硬线供人工判读）。
- **门④ 转换完整性**：odds_change_event/_meta 的入账恒等式
  （源行=事件+心跳+坏时间+未映射+未解释缺口），未解释缺口必须为 0。
- **门⑤ 管线健康**：逐数据集 raw/bronze 键覆盖率（解析率代理）、夜班
  台账汇总（请求/停机原因）、Phase1 进度（done 日/任务清单）、silver
  版本戳+树摘要（跨次报告比对即幂等佐证）。
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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, cast

import duckdb

from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb
from goalx_backend.data.corpus_store import CorpusStore
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
_PSC_THRESHOLD = 0.01  # 门②均值相对偏差线
_MIN_CORR_POINTS = 2  # 相关系数最少点数
_PHASE1_WINDOW_START = date(2023, 8, 1)


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
    """语料 fixture_universe 料次（对账侧二）。"""

    sid: str
    league: str  # CorpusScope 中文
    kickoff_day: str  # 北京墙钟日 YYYY-MM-DD
    goals_home: int
    goals_away: int


@dataclass
class MatchResult:
    """联赛+比分+日期±1 锚定的两侧配对结果。"""

    pairs: list[tuple[FixtureRow, HistRow]] = field(default_factory=list)
    gaps: list[HistRow] = field(default_factory=list)  # fdhist 有而语料缺
    ambiguous: list[HistRow] = field(default_factory=list)  # 同键多候选，未自动配
    extra_fixtures: list[FixtureRow] = field(default_factory=list)  # 语料有 fdhist 无

    @property
    def rate(self) -> float | None:
        """重叠匹配率（分母=已覆盖日期面的 fdhist 场次；ambiguous 不计入分子）。"""
        denominator = len(self.pairs) + len(self.gaps) + len(self.ambiguous)
        if denominator == 0:
            return None
        return len(self.pairs) / denominator


def _nearby(day: str) -> list[str]:
    """日期 ±1（跨库日期基准吸收窗）。"""
    anchor = date.fromisoformat(day)
    return [(anchor + timedelta(days=delta)).isoformat() for delta in (-1, 0, 1)]


def match_fixtures(fixtures: list[FixtureRow], hists: list[HistRow]) -> MatchResult:
    """
    联赛+比分+日期±1 匹配（三对账共用锚；确定性键·定则 1）。

    同 (联赛, 比分, 日期窗) 多候选 → ambiguous 不硬配（宁缺毋错）；
    配过的 fixture 不重复使用（比分重复场次各占一位）。
    """
    result = MatchResult()
    by_key: dict[tuple[str, int, int, str], list[FixtureRow]] = {}
    for fixture in fixtures:
        key = (
            fixture.league,
            fixture.goals_home,
            fixture.goals_away,
            fixture.kickoff_day,
        )
        by_key.setdefault(key, []).append(fixture)
    used: set[str] = set()
    for hist in hists:
        league = FD_TO_LEAGUE.get(hist.league_key, hist.league_key)
        candidates = [
            candidate
            for day in _nearby(hist.match_date)
            for candidate in by_key.get(
                (league, hist.goals_home, hist.goals_away, day), []
            )
        ]
        free = [c for c in candidates if c.sid not in used]
        if not free:
            result.gaps.append(hist)
        elif len(free) > 1:
            result.ambiguous.append(hist)
        else:
            used.add(free[0].sid)
            result.pairs.append((free[0], hist))
    result.extra_fixtures.extend(f for f in fixtures if f.sid not in used)
    return result


def _fetch_fixtures(con: duckdb.DuckDBPyConnection) -> list[FixtureRow]:
    leagues = ",".join("'" + n + "'" for n in LEAGUE_TO_FD)
    rows = con.execute(
        f"""
        SELECT sid, league, strftime(kickoff, '%Y-%m-%d') AS day,
               home_goals, away_goals
        FROM fixture_universe
        WHERE league IN ({leagues})
        """  # noqa: S608 联赛码=模块常量
    ).fetchall()
    return [
        FixtureRow(str(r[0]), str(r[1]), str(r[2]), int(r[3]), int(r[4])) for r in rows
    ]


def _hist_covered(row: HistRow, done_dates: set[str]) -> bool:
    """Fdhist 行是否落在夜班已完成日期面（±1 窗内任一日 done）。"""
    return any(day in done_dates for day in _nearby(row.match_date))


def _fetch_hists(con: duckdb.DuckDBPyConnection, settled: set[str]) -> list[HistRow]:
    """运行面 hist_matches（11 联赛、Phase1 窗、已判定日期面，定序输出）。"""
    codes = _sql_in(LEAGUE_TO_FD.values())
    max_day = max(settled) if settled else _PHASE1_WINDOW_START.isoformat()
    cover_end = (date.fromisoformat(max_day) + timedelta(days=1)).isoformat()
    window_start = _PHASE1_WINDOW_START.isoformat()
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
) -> dict[str, Any]:
    """门①：场次对账（重叠匹配率≥99%、缺口逐场归因——JSON 全量）。"""

    def league_of(hist: HistRow) -> str:
        return FD_TO_LEAGUE.get(hist.league_key, hist.league_key)

    per_competition: dict[str, dict[str, int]] = {}
    for hist in hists:
        entry = per_competition.setdefault(
            league_of(hist), {"fdhist": 0, "matched": 0, "gaps": 0, "ambiguous": 0}
        )
        entry["fdhist"] += 1
    for _fixture, hist in matched.pairs:
        per_competition[league_of(hist)]["matched"] += 1
    for hist in matched.gaps:
        per_competition[league_of(hist)]["gaps"] += 1
    for hist in matched.ambiguous:
        per_competition[league_of(hist)]["ambiguous"] += 1
    rate = matched.rate
    gaps_sorted = sorted(
        matched.gaps, key=lambda g: (g.match_date, g.league_key, g.home, g.away)
    )

    def cause_of(hist: HistRow) -> str:
        # 差分归因：邻日有 done 页=夜班跑过而语料缺场；只有 not_found=源T 无日页
        if any(day in done_dates for day in _nearby(hist.match_date)):
            return "语料缺场（该日期夜班已完成）"
        return "源T 日页缺席（not_found——站点无该日 CorpusScope 页）"

    return {
        "threshold": _RATE_THRESHOLD,
        "matched": len(matched.pairs),
        "gaps": len(matched.gaps),
        "ambiguous": len(matched.ambiguous),
        "extra_fixtures": len(matched.extra_fixtures),
        "rate": rate,
        "per_competition": per_competition,
        "gap_attribution": [
            {
                "competition": league_of(h),
                "date": h.match_date,
                "match": f"{h.home} vs {h.away}",
                "score": f"{h.goals_home}-{h.goals_away}",
                "cause": cause_of(h),
            }
            for h in gaps_sorted
        ],
        "ambiguous_attribution": [
            {
                "competition": league_of(h),
                "date": h.match_date,
                "match": f"{h.home} vs {h.away}",
                "score": f"{h.goals_home}-{h.goals_away}",
            }
            for h in sorted(
                matched.ambiguous, key=lambda g: (g.match_date, g.league_key, g.home)
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


def gate2_psc_anchor(
    con: duckdb.DuckDBPyConnection, pairs: list[tuple[FixtureRow, HistRow]]
) -> dict[str, Any]:
    """门②：PSC×cid177 赛前末可见价偏差（均值<1%）。"""
    anchors = _anchor_last_pre_kickoff(con, [f.sid for f, _ in pairs])
    diffs: list[float] = []
    usable = 0
    for fixture, hist in pairs:
        anchor = anchors.get(fixture.sid)
        raw_psc = (hist.psc_home, hist.psc_draw, hist.psc_away)
        if anchor is None or None in raw_psc:
            continue
        psc = cast("tuple[float, float, float]", raw_psc)
        usable += 1
        diffs.extend(abs(a - p) / p for a, p in zip(anchor, psc, strict=True) if p > 0)
    avg = mean(diffs) if diffs else None
    return {
        "threshold": _PSC_THRESHOLD,
        "pairs": len(pairs),
        "usable": usable,
        "outcome_comparisons": len(diffs),
        "mean_relative_deviation": avg,
        "max_relative_deviation": max(diffs) if diffs else None,
        "verdict": (
            "insufficient"
            if avg is None
            else "pass"
            if avg < _PSC_THRESHOLD
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
    """语料 silver 数据集根（路径布局知识的唯一落点）。"""
    return store.root / "silver" / srct.SRCT_PROVIDER / dataset


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


def _count_bronze_keys(store: CorpusStore, dataset: str) -> int:
    """去重键数（流式逐行 json 只取 sid，不整载 payload）。"""
    path = store.bronze_path(srct.SRCT_PROVIDER, dataset)
    keys: set[str] = set()
    if not path.exists():
        return 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                keys.add(str(json.loads(line)["sid"]))
    return len(keys)


def _count_raw_files(store: CorpusStore, dataset: str) -> int:
    root = store.root / "raw" / srct.SRCT_PROVIDER / dataset
    if not root.exists():
        return 0
    return sum(1 for p in root.iterdir() if p.is_file() and not p.name.endswith(".tmp"))


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

    进度分母=phase1_dates 任务清单，分子=settled（done|not_found——夜班
    对两者的判定都算完成，与 pending_dates 口径一致）。
    """
    datasets = (
        srct.DAY_DATASET,
        srct.ODDS_DATASET,
        srct.HANDICAP_DATASET,
        srct.STATS_DATASET,
    )
    per_dataset: dict[str, dict[str, int | float | None]] = {}
    worst: float | None = None
    for dataset in datasets:
        raw_count = _count_raw_files(store, dataset)
        bronze_keys = _count_bronze_keys(store, dataset)
        rate = bronze_keys / raw_count if raw_count else None
        per_dataset[dataset] = {
            "raw_files": raw_count,
            "bronze_keys": bronze_keys,
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
        hists = _fetch_hists(con, settled_dates)
        matched = match_fixtures(fixtures, hists)
        gate1 = gate1_fixture_reconciliation(matched, hists, done_dates)
        gate2 = gate2_psc_anchor(con, matched.pairs)
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
        + " / 缺口 "
        + str(gate1["gaps"])
        + " / 歧义 "
        + str(gate1["ambiguous"])
        + " / 语料多出 "
        + str(gate1["extra_fixtures"])
        + " · 匹配率 **"
        + _fmt(gate1["rate"])
        + "** · verdict `"
        + str(gate1["verdict"])
        + "`",
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
        "## 门② PSC×cid177 赛前末可见价（均值<1%）",
        "",
        "- 配对 "
        + str(gate2["pairs"])
        + "（可用 "
        + str(gate2["usable"])
        + "，Outcome 比较 "
        + str(gate2["outcome_comparisons"])
        + " 次）· 均值偏差 **"
        + _fmt(gate2["mean_relative_deviation"])
        + "** · 最大 "
        + _fmt(gate2["max_relative_deviation"])
        + " · verdict `"
        + str(gate2["verdict"])
        + "`",
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
        "## 门⑤ 管线健康（键覆盖≥99%）",
        "",
        "- 最差键覆盖 **"
        + _fmt(gate5["worst_key_coverage"])
        + "** · verdict `"
        + str(gate5["verdict"])
        + "`",
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
            f"  - {dataset}: raw {entry['raw_files']} / bronze 键 "
            + f"{entry['bronze_keys']} · 覆盖 {_fmt(entry['key_coverage'])}"
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
