"""
断点哨兵（票 34，设计稿=13-sentinel-design，E-29 B 件后半）。

主形态=第二源哨兵：srct（cid177，生产第一锚，只读不动）×
propline:pinnacle（免费层第二源）周级对账——对「生产 CLV 锚真实作用域」
（JC fixture ∩ FD 11 联赛）每周判三轴一致性，alert=留痕+晨检红面，
**不自动处置**（不换轨/不补采/不触发付费兜底）。srct 自监单源降级形态
（设计稿 §八）保留为文档段，不部署。

三轴阈值（初值=research/21 §六 逐条转录；第 8 周人裁重标定一次）：
- 1a 覆盖轴·第二源：propline:pinnacle 资格收盘在场率，**分母=cid177 资格场**
  （第一锚侧——勿用 propline 自身做分母，防自反性），<90% alert；
- 1b 锚供给轴·第一锚：cid177 资格收盘在场率，分母=fixture 侧（该周
  JC FD 11 联赛 fixtures 总数），<90% alert；
- 2 发散轴·赔率域：逐 outcome |srct_odds − pl_odds| / pl_odds 周级
  median >1% 且连续 2 周 → alert（单周 >1%=warn）；
- 3 发散轴·概率域：双侧同一 Shin 实现（om.shin_implied）逐 outcome
  |Δp| 周级 mean >0.5pp warn / >1.0pp alert。
- **signed 周均值/中位数禁用**（结构零：同场三向各自 Shin 归一 ⇒ signed
  和恒 0，对任何变质全盲——research 21 §三）；signed 逐向偏移仅作注记。

资格线=读侧 captured_at < kickoff（哨兵的「收盘」=kickoff 前最后一拍，
与生产 srct published_at ≤ kickoff 末笔同构；无历史端点，断采窗口永久缺，
覆盖轴显形不静默）。n<10 场的周该轴 insufficient（晨检黄不红）。

留痕：anchor_sentinel_weekly 表 append-only 追加（重跑取每轴最新行胜出），
周报 md 落语料树 reports/anchor-sentinel/<isoyear>-W<ww>.md。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any

import duckdb

from goalx_backend import db as db_mod
from goalx_backend import odds_math as om
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import mapping, quote_evidence
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.leagues import LEAGUE_TO_FD

# 三轴阈值（设计稿 §三逐条转录；阈值调整=人裁事件，不自动改线）
COVERAGE_ALERT = 0.90  # 1a/1b：<90% alert
DEV_ODDS_WARN = 0.01  # 轴 2：周级 median 相对偏差 >1%
DEV_PROB_WARN = 0.005  # 轴 3：周级 mean |Δp| >0.5pp（概率单位）
DEV_PROB_ALERT = 0.010  # 轴 3：>1.0pp
MIN_N_FIXTURES = 10  # 周=ISO 周，n<10 场该轴 insufficient

AXIS_COVERAGE_SECOND = "coverage_second"
AXIS_COVERAGE_ANCHOR = "coverage_anchor"
AXIS_DEV_ODDS = "dev_odds"
AXIS_DEV_PROB = "dev_prob"
AXES: tuple[str, ...] = (
    AXIS_COVERAGE_SECOND,
    AXIS_COVERAGE_ANCHOR,
    AXIS_DEV_ODDS,
    AXIS_DEV_PROB,
)

#: 第二源腿的快照源标签（单一事实源在 quote_evidence 读侧；采集面
#: anchor-sentinel-capture 的 pinnacle-only 写入与之对齐）
PROPLINE_PINNACLE_SOURCE = quote_evidence.PROPLINE_PINNACLE_SOURCE

#: srct 腿注入缝：fixture 行 → 资格收盘三元组（None=无资格收盘）
SrctReader = Callable[[sqlite3.Row], tuple[float, float, float] | None]

#: corpus.duckdb 只读连接（corpus_anchor 同款；duckdb 无官方 stub，clv 同例）
DuckCon = duckdb.DuckDBPyConnection


def iso_week_of_kickoff(kickoff_utc: str) -> tuple[int, int]:
    """该场 kickoff 的 ISO 周（北京墙钟日历周——repo 业务日同口径）。"""
    beijing_day = fx_store.beijing_naive(kickoff_utc).date()
    iso = beijing_day.isocalendar()
    return iso[0], iso[1]


def latest_complete_iso_week(now: datetime | None = None) -> tuple[int, int]:
    """最近完整 ISO 周（now−7 天所在周——周一 08:50 定拍即刚结束的那周）。"""
    moment = now or datetime.now(UTC)
    beijing = moment.astimezone(fx_store.CST) - timedelta(days=7)
    iso = beijing.date().isocalendar()
    return iso[0], iso[1]


def previous_iso_week(iso_year: int, iso_week: int) -> tuple[int, int]:
    """上一 ISO 周（周一 −7 天所在周；跨年经 fromisocalendar 钉死）。"""
    monday = date.fromisocalendar(iso_year, iso_week, 1) - timedelta(days=7)
    iso = monday.isocalendar()
    return iso[0], iso[1]


def week_fixtures(
    conn: sqlite3.Connection, iso_year: int, iso_week: int
) -> list[sqlite3.Row]:
    """
    该 ISO 周的对账域：JC fixture（match_codes jingcai）∩ FD 11 联赛。

    周桶=北京墙钟日历周（周一 00:00 至周日 24:00）；FD 11 联赛=
    LEAGUE_TO_FD 中文键（CorpusScope 重叠联赛全集，设计稿 §四）。
    表 SQL 走 fixtures 属主查询（ADR-0008）；ISO 周桶过滤在本层。
    """
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    # 北京 [周一00:00, 下周一00:00) → UTC 窗（kickoff_utc 恒带偏移，字典序可比）
    start_utc = datetime(
        monday.year, monday.month, monday.day, tzinfo=fx_store.CST
    ).astimezone(UTC)
    next_monday = monday + timedelta(days=7)
    end_utc = datetime(
        next_monday.year, next_monday.month, next_monday.day, tzinfo=fx_store.CST
    ).astimezone(UTC)
    rows = fx_store.jc_fixtures_by_competition_window(
        conn,
        competitions=tuple(LEAGUE_TO_FD),
        kickoff_from_iso=start_utc.isoformat(),
        kickoff_to_iso=end_utc.isoformat(),
    )
    return [
        row
        for row in rows
        if iso_week_of_kickoff(str(row["kickoff_utc"])) == (iso_year, iso_week)
    ]


def make_srct_reader(conn: sqlite3.Connection, duck_con: DuckCon) -> SrctReader:
    """
    生产 srct 腿：srct_pinnacle_closing_triplet 同一函数=同一语义（只读）。

    与 evaluation/clv.py srct_closing_prob 的收盘判定同源（票 75 语义：
    published_at ≤ kickoff 末笔）；sid 有物化链传 sid（票 77）。
    """

    def read(row: sqlite3.Row) -> tuple[float, float, float] | None:
        return quote_evidence.srct_pinnacle_closing_triplet(
            duck_con,
            str(row["home_name"]),
            str(row["away_name"]),
            str(row["kickoff_utc"]),
            sid=mapping.srct_sid_for_fixture(conn, int(row["id"])),
        )

    return read


def _p95(values: list[float]) -> float:
    """p95（corpus_gate._dev_stats 同式：序统计，尾索引封顶）。"""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def _round(value: float) -> float:
    """留痕读数取 6 位（读面断言与 md 稳定）。"""
    return round(value, 6)


@dataclass(frozen=True)
class AxisVerdict:
    """一轴判定（持久化与报告的公共形状）。"""

    axis: str
    status: str
    n_fixtures: int
    metrics: dict[str, Any]

    def as_row(
        self, iso_year: int, iso_week: int, computed_at: str
    ) -> tuple[int | str | None, ...]:
        """→ anchor_sentinel_weekly 行值。"""
        return (
            iso_year,
            iso_week,
            self.axis,
            self.status,
            self.n_fixtures,
            json.dumps(self.metrics, ensure_ascii=False),
            None,
            computed_at,
        )


def _coverage_verdict(axis: str, numerator: int, denominator: int) -> AxisVerdict:
    """覆盖轴判定：n<10 insufficient；<90% alert；否则 ok。"""
    if denominator < MIN_N_FIXTURES:
        return AxisVerdict(axis, "insufficient", denominator, {"rate": None})
    rate = numerator / denominator
    status = "alert" if rate < COVERAGE_ALERT else "ok"
    return AxisVerdict(
        axis, status, denominator, {"rate": _round(rate), "numerator": numerator}
    )


def _prev_week_odds_median(
    conn: sqlite3.Connection, iso_year: int, iso_week: int
) -> float | None:
    """上一 ISO 周 dev_odds 已留痕的 median（无行/insufficient=None）。"""
    prev_year, prev_week = previous_iso_week(iso_year, iso_week)
    row = conn.execute(
        """
        SELECT status, metrics_json FROM anchor_sentinel_weekly
        WHERE iso_year = ? AND iso_week = ? AND axis = ?
        ORDER BY id DESC LIMIT 1
        """,
        (prev_year, prev_week, AXIS_DEV_ODDS),
    ).fetchone()
    if row is None:
        return None
    median_value = json.loads(str(row["metrics_json"])).get("median")
    return float(median_value) if median_value is not None else None


def _deviation_verdicts(
    pairs: list[
        tuple[sqlite3.Row, tuple[float, float, float], tuple[float, float, float]]
    ],
    prev_median: float | None,
) -> tuple[AxisVerdict, AxisVerdict]:
    """发散两轴：赔率域 median（含 2 周持久性）+ 概率域 mean|Δp|。"""
    if len(pairs) < MIN_N_FIXTURES:
        insufficient = AxisVerdict(
            AXIS_DEV_ODDS, "insufficient", len(pairs), {"median": None}
        )
        insufficient_prob = AxisVerdict(
            AXIS_DEV_PROB, "insufficient", len(pairs), {"mean": None}
        )
        return insufficient, insufficient_prob
    rel_devs: list[float] = []
    abs_deltas: list[float] = []
    signed_deltas: list[float] = []  # 仅注记（禁令：不作判据）
    anomalies: list[dict[str, Any]] = []
    for row, srct_triplet, pl_triplet in pairs:
        p_srct = om.shin_implied(srct_triplet)
        p_pl = om.shin_implied(pl_triplet)
        fixture_devs: list[float] = []
        fixture_deltas: list[float] = []
        for i in range(3):
            dev = abs(srct_triplet[i] - pl_triplet[i]) / pl_triplet[i]
            delta = p_srct[i] - p_pl[i]
            rel_devs.append(dev)
            fixture_devs.append(dev)
            abs_deltas.append(abs(delta))
            fixture_deltas.append(abs(delta))
            signed_deltas.append(delta)
        anomalies.append(
            {
                "fixture_id": int(row["id"]),
                "competition": str(row["competition_name"]),
                "kickoff_utc": str(row["kickoff_utc"]),
                "srct": list(srct_triplet),
                "pl": list(pl_triplet),
                "max_rel_dev": _round(max(fixture_devs)),
                "max_abs_dp": _round(max(fixture_deltas)),
            }
        )
    week_median = median(rel_devs)
    persisted = prev_median is not None and prev_median > DEV_ODDS_WARN
    if week_median > DEV_ODDS_WARN:
        odds_status = "alert" if persisted else "warn"
    else:
        odds_status = "ok"
    week_mean = mean(abs_deltas)
    if week_mean > DEV_PROB_ALERT:
        prob_status = "alert"
    elif week_mean > DEV_PROB_WARN:
        prob_status = "warn"
    else:
        prob_status = "ok"
    odds = AxisVerdict(
        AXIS_DEV_ODDS,
        odds_status,
        len(pairs),
        {
            "median": _round(week_median),
            "mean": _round(mean(rel_devs)),
            "p95": _round(_p95(rel_devs)),
            "n_outcomes": len(rel_devs),
            "prev_week_median": None if prev_median is None else _round(prev_median),
            "persisted": persisted,
            "anomalies": sorted(
                anomalies, key=lambda a: a["max_rel_dev"], reverse=True
            )[:5],
        },
    )
    prob = AxisVerdict(
        AXIS_DEV_PROB,
        prob_status,
        len(pairs),
        {
            "mean": _round(week_mean),
            "p95": _round(_p95(abs_deltas)),
            "n_outcomes": len(abs_deltas),
            # 注记（设计稿 §三禁令：signed 周均值结构性恒零，永不作判据）
            "signed_mean_note": _round(mean(signed_deltas)),
        },
    )
    return odds, prob


def compute_week(
    conn: sqlite3.Connection,
    iso_year: int,
    iso_week: int,
    *,
    srct_reader: SrctReader,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    周级对账：三轴计算 + append-only 留痕（幂等=重跑追加行，读取最新行胜出）。

    生产第一锚零改动：srct 腿经注入 reader 只读消费（make_srct_reader）。
    """
    computed_at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    fixtures = week_fixtures(conn, iso_year, iso_week)
    srct_legs: dict[int, tuple[float, float, float] | None] = {}
    pl_legs: dict[int, tuple[float, float, float] | None] = {}
    for row in fixtures:
        fid = int(row["id"])
        srct_legs[fid] = srct_reader(row)
        pl_legs[fid] = quote_evidence.propline_pinnacle_closing_triplet(
            conn, fid, str(row["kickoff_utc"])
        )
    anchor_present = [fid for fid, leg in srct_legs.items() if leg is not None]
    pairs: list[
        tuple[sqlite3.Row, tuple[float, float, float], tuple[float, float, float]]
    ] = []
    for row in fixtures:
        fid = int(row["id"])
        srct_triplet = srct_legs[fid]
        pl_triplet = pl_legs[fid]
        if srct_triplet is None or pl_triplet is None:
            continue
        pairs.append((row, srct_triplet, pl_triplet))
    verdicts = [
        # 1a 分母=cid177 资格场（第一锚侧，防自反性）；1b 分母=fixture 侧
        _coverage_verdict(AXIS_COVERAGE_SECOND, len(pairs), len(anchor_present)),
        _coverage_verdict(
            AXIS_COVERAGE_ANCHOR,
            len(anchor_present),
            len(fixtures),
        ),
    ]
    prev_median = _prev_week_odds_median(conn, iso_year, iso_week)
    verdicts.extend(_deviation_verdicts(pairs, prev_median))
    with db_mod.atomic(conn):
        for verdict in verdicts:
            conn.execute(
                """
                INSERT INTO anchor_sentinel_weekly
                    (iso_year, iso_week, axis, status, n_fixtures,
                     metrics_json, note, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                verdict.as_row(iso_year, iso_week, computed_at),
            )
    return {
        "iso_year": iso_year,
        "iso_week": iso_week,
        "n_fixtures": len(fixtures),
        "computed_at": computed_at,
        "axes": {
            verdict.axis: {
                "status": verdict.status,
                "n_fixtures": verdict.n_fixtures,
                "metrics": verdict.metrics,
            }
            for verdict in verdicts
        },
    }


def latest_week_face(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """读取面：每轴最新留痕行（dict：status/n/metrics/周键/computed_at）。"""
    face: dict[str, dict[str, Any]] = {}
    for axis in AXES:
        row = conn.execute(
            """
            SELECT iso_year, iso_week, status, n_fixtures, metrics_json,
                   computed_at
            FROM anchor_sentinel_weekly
            WHERE axis = ?
            ORDER BY id DESC LIMIT 1
            """,
            (axis,),
        ).fetchone()
        if row is None:
            continue
        face[axis] = {
            "iso_year": int(row["iso_year"]),
            "iso_week": int(row["iso_week"]),
            "status": str(row["status"]),
            "n_fixtures": int(row["n_fixtures"]),
            "metrics": json.loads(str(row["metrics_json"])),
            "computed_at": str(row["computed_at"]),
        }
    return face


def latest_report(
    conn: sqlite3.Connection, *, iso_year: int, iso_week: int
) -> dict[str, Any]:
    """指定 ISO 周的报告读相（每轴最新行；缺轴=该周未完整计算）。"""
    report: dict[str, Any] = {
        "iso_year": iso_year,
        "iso_week": iso_week,
        "axes": {},
    }
    for axis in AXES:
        row = conn.execute(
            """
            SELECT status, n_fixtures, metrics_json, computed_at
            FROM anchor_sentinel_weekly
            WHERE iso_year = ? AND iso_week = ? AND axis = ?
            ORDER BY id DESC LIMIT 1
            """,
            (iso_year, iso_week, axis),
        ).fetchone()
        if row is None:
            continue
        report["axes"][axis] = {
            "status": str(row["status"]),
            "n_fixtures": int(row["n_fixtures"]),
            "metrics": json.loads(str(row["metrics_json"])),
        }
        report["computed_at"] = str(row["computed_at"])
    return report


REPORTS_DIR = "anchor-sentinel"
_AXIS_LABELS: dict[str, str] = {
    AXIS_COVERAGE_SECOND: "1a 覆盖轴·第二源（分母=cid177 资格场）",
    AXIS_COVERAGE_ANCHOR: "1b 锚供给轴·第一锚（分母=fixture 侧）",
    AXIS_DEV_ODDS: "2 发散轴·赔率域（median 相对偏差）",
    AXIS_DEV_PROB: "3 发散轴·概率域（mean |Δp|）",
}


def _axis_line(axis: str, face: dict[str, Any]) -> str:
    """一轴一行（check_edge_scan 同款字符串风格）。"""
    status = str(face["status"])
    n = int(face["n_fixtures"])
    metrics: dict[str, Any] = face["metrics"]
    if axis in (AXIS_COVERAGE_SECOND, AXIS_COVERAGE_ANCHOR):
        rate = metrics.get("rate")
        value = "" if rate is None else f" rate={rate:.3f}"
        return f"- {axis}={status}{value}(n={n}) · {_AXIS_LABELS[axis]}"
    if axis == AXIS_DEV_ODDS:
        med = metrics.get("median")
        value = "" if med is None else f" median={med:.4f}"
        persisted = metrics.get("persisted")
        persist_note = (
            ""
            if not persisted
            else f"（前周 median={metrics.get('prev_week_median')}，持续 2 周）"
        )
        return (
            f"- {axis}={status}{value}(n={n}, outcomes="
            f"{metrics.get('n_outcomes', 0)}){persist_note} · {_AXIS_LABELS[axis]}"
        )
    m = metrics.get("mean")
    value = "" if m is None else f" mean={m:.4f}"
    return (
        f"- {axis}={status}{value}(n={n}, outcomes="
        f"{metrics.get('n_outcomes', 0)}) · {_AXIS_LABELS[axis]}"
    )


def write_week_report(store: CorpusStore, report: dict[str, Any]) -> Path:
    """周报 md 落语料树 reports/anchor-sentinel/（corpus-gate md 惯例）。"""
    iso_year, iso_week = report["iso_year"], report["iso_week"]
    axes: dict[str, Any] = report["axes"]
    generated = str(report.get("computed_at", "—"))
    lines = [
        f"# 断点哨兵周报 {iso_year}-W{iso_week:02d}",
        "",
        f"生成：{generated} · 配对域=JC fixture ∩ FD 11 联赛（北京墙钟 ISO 周）",
        " · srct（cid177，第一锚，只读）× propline:pinnacle（第二源）",
        " · 告警动作=留痕+晨检红面，不自动处置（设计稿 13-sentinel-design）",
        "",
    ]
    for axis in AXES:
        face = axes.get(axis)
        if face is None:
            lines.append(f"- {axis}=missing · 该轴未计算")
        else:
            lines.append(_axis_line(axis, face))
    odds_face: dict[str, Any] = axes.get(AXIS_DEV_ODDS) or {}
    odds_metrics: dict[str, Any] = odds_face.get("metrics", {})
    anomalies: list[dict[str, Any]] = list(odds_metrics.get("anomalies", []))
    lines += ["", "## 异常 top5（按相对偏差，仅配对场）"]
    for a in anomalies:
        head = f"- #{a['fixture_id']} {a['competition']} {a['kickoff_utc']}"
        legs = f"srct {a['srct']} vs pl {a['pl']}"
        dev = f"（rel {a['max_rel_dev']}, |Δp| {a['max_abs_dp']}）"
        lines.append(f"{head} {legs} {dev}")
    if not anomalies:
        lines.append("- （无配对面或未越线）")
    reports_dir = store.root / "reports" / REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{iso_year}-W{iso_week:02d}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
