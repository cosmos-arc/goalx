"""
自算 Elo silver 物化（票 78）：热身→桥→折叠→``elo_self`` 数据集。

链路（全本地零请求）：fd hist 十一联赛（英文名空间，2016/17 起、
era 边界前）热身 → 重叠期确定性配对桥（fd 英文名 → 源T 中文名）把
热身末态搬运进语料名空间 → fixture_universe 完场序列（中文名空间）
折叠出逐场赛前 Elo → 语料树 ``silver/goalx/elo_self``（单 parquet，
(kickoff, sid) 确定性排序 → 幂等重建字节级一致）+ corpus.duckdb 视图
（注册见 corpus_duckdb._SILVER_VIEWS）。

消费即 join fixture_universe（sid 键），零跨源映射（票 74 的
clubelo 英文名消费侧挂账由本数据集接管）。全量重算无增量：语料扩
（Phase1 历史回填/新季）后重跑即扩覆盖，参数变更递增 elo.ELO_VERSION。

fd 联赛代码 ↔ 源T 联赛中文名：静态 11 项（E0/E1/D1/I1/SP1/F1/N1/
P1/T1/SC0/B1）；瑞超/挪超/欧战不在 fd——语料起点自热身并接受首季
贬值（票面裁决）。
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
# （pyarrow 无官方 stub，同 srct_silver/jc_silver 先例）

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import duckdb
import pyarrow as pa

from goalx_backend.data import mapping
from goalx_backend.data import results as rs_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.silver import write_dataset_meta, write_partition
from goalx_backend.db import utc_now_iso
from goalx_backend.modelling import elo

PROVIDER = "elo"  # 自算派生层身份（语料树独立目录，不挂在采集源下）
DATASET = "elo_self"

# fd 联赛代码 → 源T 联赛中文名（票 78 静态桥；CorpusScope 交集 11 项）
FD_LEAGUE_ZH: dict[str, str] = {
    "E0": "英超",
    "E1": "英冠",
    "D1": "德甲",
    "I1": "意甲",
    "SP1": "西甲",
    "F1": "法甲",
    "N1": "荷甲",
    "P1": "葡超",
    "T1": "土超",
    "SC0": "苏超",
    "B1": "比甲",
}

# 1X2 基准概率（RPS 对照用常数；文献主场胜/平/客胜典型带）
_BASELINE_PROBS = (0.45, 0.26, 0.29)

DuckCon = duckdb.DuckDBPyConnection

_SCHEMA = pa.schema(
    [
        pa.field("sid", pa.string()),
        pa.field("league", pa.string()),
        pa.field("kickoff", pa.timestamp("ms")),  # 北京墙钟 naive（同 universe）
        pa.field("home", pa.string()),
        pa.field("away", pa.string()),
        pa.field("elo_home_pre", pa.float64()),
        pa.field("elo_away_pre", pa.float64()),
        pa.field("version", pa.string()),
    ]
)


@dataclass
class SeasonRps:
    """一留出季的 RPS 对照（Elo vs 常数基准；体面基线带，不追市场）。"""

    season: int
    matches: int = 0
    rps_elo: float = 0.0
    rps_baseline: float = 0.0
    elo_edge: float = 0.0  # rps_baseline − rps_elo（正=Elo 优）


@dataclass
class EloBuildReport:
    """一次 elo_self 全量重算的报告（CLI/审计面）。"""

    built_at: str = ""
    warmup_rows: int = 0
    overlap_rows: int = 0
    corpus_rows: int = 0
    written: int = 0
    bridge: dict[str, object] = field(default_factory=dict)
    params: dict[str, float] = field(default_factory=dict)
    season_rps: list[SeasonRps] = field(default_factory=list)
    degraded: str | None = None
    top_ratings: list[dict[str, object]] = field(default_factory=list)


SEASON_START_MONTH = 8  # 赛季 8 月界（与 srct_silver 同界）


def _season_of(day: date) -> int:
    """赛季年标（8 月界）：2025-08 起 → 2025。"""
    return day.year if day.month >= SEASON_START_MONTH else day.year - 1


def _hist_bridge_rows(rows: list[sqlite3.Row]) -> list[elo.BridgeRow]:
    """Hist 行 → 桥投票行（联赛代码归一；不在 11 项映射内的跳过）。"""
    out: list[elo.BridgeRow] = []
    for row in rows:
        league = FD_LEAGUE_ZH.get(str(row["competition"]))
        if league is None:
            continue
        out.append(
            elo.BridgeRow(
                league=league,
                day=date.fromisoformat(str(row["match_date"])),
                home=str(row["home_team"]),
                away=str(row["away_team"]),
                home_goals=int(row["fthg"]),
                away_goals=int(row["ftag"]),
            )
        )
    return out


def _corpus_matches(
    rows: list[mapping.UniverseResultRow],
) -> tuple[list[elo.EloMatch], list[elo.BridgeRow]]:
    """Universe 赛果行 → Elo 输入 + 桥投票行（无比分行两用皆跳过）。"""
    matches: list[elo.EloMatch] = []
    bridge: list[elo.BridgeRow] = []
    for row in rows:
        if row.home_goals is None or row.away_goals is None:
            continue
        day = row.kickoff_bj.date()
        matches.append(
            elo.EloMatch(
                home=row.home,
                away=row.away,
                home_goals=row.home_goals,
                away_goals=row.away_goals,
                kickoff=day,
                seq=row.sid,
            )
        )
        bridge.append(
            elo.BridgeRow(
                league=row.league,
                day=day,
                home=row.home,
                away=row.away,
                home_goals=row.home_goals,
                away_goals=row.away_goals,
            )
        )
    return matches, bridge


def _rps(probs: tuple[float, float, float], outcome: int) -> float:
    """1X2 的 ranked probability score（outcome=0/1/2 → H/D/A）。"""
    cum_p, cum_o, total = 0.0, 0.0, 0.0
    for i, prob in enumerate(probs):
        cum_p += prob
        cum_o += 1.0 if i == outcome else 0.0
        total += (cum_p - cum_o) ** 2
    return 0.5 * total


def _season_rps_report(
    corpus: list[mapping.UniverseResultRow],
    pres: list[elo.EloPre],
    params: elo.EloParams,
) -> list[SeasonRps]:
    """逐留出季 RPS：Elo 赛前值→1X2 概率 vs 常数基准（报告非门禁）。"""
    pre_by_sid = {pre.seq: pre for pre in pres}
    buckets: dict[int, list[tuple[tuple[float, float, float], int]]] = {}
    for row in corpus:
        if row.home_goals is None or row.away_goals is None:
            continue
        pre = pre_by_sid.get(row.sid)
        if pre is None:
            continue
        e_home = elo.expected_score(pre.elo_home_pre, pre.elo_away_pre, params.hfa)
        probs = elo.one_x_two_probs(e_home, params.draw_nu)
        outcome = (
            0
            if row.home_goals > row.away_goals
            else (1 if row.home_goals == row.away_goals else 2)
        )
        buckets.setdefault(_season_of(row.kickoff_bj.date()), []).append(
            (probs, outcome)
        )
    out: list[SeasonRps] = []
    for season in sorted(buckets):
        entries = buckets[season]
        rps_elo = sum(_rps(probs, outcome) for probs, outcome in entries) / len(entries)
        rps_base = sum(_rps(_BASELINE_PROBS, outcome) for _, outcome in entries) / len(
            entries
        )
        out.append(
            SeasonRps(
                season=season,
                matches=len(entries),
                rps_elo=round(rps_elo, 4),
                rps_baseline=round(rps_base, 4),
                elo_edge=round(rps_base - rps_elo, 4),
            )
        )
    return out


def _dataset_root(store: CorpusStore) -> Path:
    return store.silver_path(PROVIDER, DATASET)


def build_elo_self(
    store: CorpusStore,
    conn: sqlite3.Connection,
    duck_con: DuckCon,
    *,
    now: datetime | None = None,
) -> EloBuildReport:
    """
    全量重算并物化 elo_self（幂等；确定性排序 → parquet 字节级一致）。

    语料桥缺席由调用方降级（本函数要求数据面齐：universe 视图 + hist 表）。
    """
    report = EloBuildReport(
        built_at=utc_now_iso() if now is None else now.isoformat(timespec="seconds"),
        params={
            "base": elo.DEFAULT_PARAMS.base,
            "k_base": elo.DEFAULT_PARAMS.k_base,
            "hfa": elo.DEFAULT_PARAMS.hfa,
            "draw_nu": elo.DEFAULT_PARAMS.draw_nu,
        },
    )
    corpus_rows = mapping.load_universe_results(duck_con)
    if not corpus_rows:
        report.degraded = "fixture_universe empty"
        write_dataset_meta(
            _dataset_root(store),
            {"silver_version": elo.ELO_VERSION, "degraded": report.degraded},
        )
        return report
    corpus_start = min(row.kickoff_bj.date() for row in corpus_rows)
    hist_all = rs_store.hist_result_rows(conn)
    warmup = [
        row
        for row in hist_all
        if date.fromisoformat(str(row["match_date"])) < corpus_start
    ]
    overlap = [
        row
        for row in hist_all
        if date.fromisoformat(str(row["match_date"])) >= corpus_start
    ]
    report.warmup_rows = len(warmup)
    report.overlap_rows = len(overlap)
    corpus_matches, corpus_bridge = _corpus_matches(corpus_rows)
    report.corpus_rows = len(corpus_matches)
    mapping_names, bridge_report = elo.build_name_bridge(
        _hist_bridge_rows(overlap), corpus_bridge
    )
    report.bridge = bridge_report
    warmup_state = elo.ratings_asof(
        _warmup_matches(warmup), date.max, elo.DEFAULT_PARAMS
    )
    initial = elo.transfer_state(warmup_state, mapping_names)
    pres = elo.compute_elo(corpus_matches, elo.DEFAULT_PARAMS, initial=initial)
    report.season_rps = _season_rps_report(corpus_rows, pres, elo.DEFAULT_PARAMS)
    row_by_sid = {row.sid: row for row in corpus_rows}
    out_rows: list[tuple[datetime, str, dict[str, object]]] = []
    for pre in pres:
        src = row_by_sid[pre.seq]
        out_rows.append(
            (
                src.kickoff_bj,
                pre.seq,
                {
                    "sid": pre.seq,
                    "league": src.league,
                    "kickoff": src.kickoff_bj,
                    "home": pre.home,
                    "away": pre.away,
                    "elo_home_pre": round(pre.elo_home_pre, 2),
                    "elo_away_pre": round(pre.elo_away_pre, 2),
                    "version": elo.ELO_VERSION,
                },
            )
        )
    out_rows.sort(key=lambda item: (item[0], item[1]))
    rows = [payload for _, _, payload in out_rows]
    root = _dataset_root(store)
    write_partition(root / "all", rows, _SCHEMA)
    write_dataset_meta(
        root,
        {
            "silver_version": elo.ELO_VERSION,
            "built_at": report.built_at,
            "rows": len(rows),
            "warmup_rows": report.warmup_rows,
            "bridge": report.bridge,
            "params": report.params,
            "partitioning": "single",
        },
    )
    report.written = len(rows)
    state = elo.ratings_asof(corpus_matches, date.max, elo.DEFAULT_PARAMS)
    top = sorted(state.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
    report.top_ratings = [{"team": name, "elo": round(value, 1)} for name, value in top]
    return report


def _warmup_matches(rows: list[sqlite3.Row]) -> list[elo.EloMatch]:
    """热身 hist 行 → EloMatch（英文名空间；联赛不限——状态按队名累积）。"""
    return [
        elo.EloMatch(
            home=str(row["home_team"]),
            away=str(row["away_team"]),
            home_goals=int(row["fthg"]),
            away_goals=int(row["ftag"]),
            kickoff=date.fromisoformat(str(row["match_date"])),
        )
        for row in rows
    ]
