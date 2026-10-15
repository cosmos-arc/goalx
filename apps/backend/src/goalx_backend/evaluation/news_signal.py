"""
事件信号 PIT 回放评测（caliber-loop 票 01 重构：场次级口径）。

N1 事件（silver guardian/news_event）× run 预测残差——统计单位=场次：
每场（fixture）每侧至多贡献一个 realized 残差（1[ftr=side]−p_fair），
同场多事件只计一次；主分母=独立场次数，headline 先报独立场次数。

- **主面（场次级匹配对照）**：事件场（侧锚定 realized）vs 对照组=
  同联赛+同季+近比赛周（±2 ISO 周）+相近市场概率档（五分位，与残差
  同尺度的收盘公允 fair_probs）的无事件场；匹配不上的事件场剔除并
  计数；不确定性按比赛周分块 bootstrap（fixture 聚类由单位自然满足）。
- **次面（事件加权，旧口径降级）**：每条事件一个残差——非独立样本，
  只作参考不作主结论。
- **事件面（描述）**：时距=北京墙钟−8h 真 UTC（与抽取器
  ``beijing_naive_to_utc`` 单一真相源；旧报告标签替换恒高估 8h）。
- **price-in 面**：事件场临场 1h→收盘主队位移（只覆盖反应窗末段，
  方向面，不作"未 price-in"结论）。

时间错位/事件置换对照=保留面（本票未做）。观察性报告：不进模型不改
参数。纯读面（事件 parquet + gold 只读桥 + run 预测），报告落
reports/news-signal.{json,md}。
"""

from __future__ import annotations

# pyarrow.parquet 存根不全（data/gold 同例放宽）
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
import json
import math
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import fmean, quantiles
from typing import Any

import duckdb
import pyarrow.parquet as pq

from goalx_backend.data import gold_reader
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.fixtures import beijing_naive_to_utc
from goalx_backend.evaluation.metrics import load_backtest_run
from goalx_backend.markets import SELECTIONS

REPORT_BASENAME = "news-signal"
MATCH_WEEK_WINDOW = 2  # 对照匹配：ISO 周差 ≤2（近比赛周）
PROB_BAND_WIDTH = 0.2  # 市场概率档宽度=五分位（粒度裁决见票 01 Answer）
DOSE_SPLIT = 2  # 剂量分层界：1 条 vs 2+ 条
BOOTSTRAP_DRAWS = 1000
BOOTSTRAP_SEED = 20261016
MATCH_PARAMS: dict[str, Any] = {
    "league": "equal",
    "season": "equal",
    "week_window": MATCH_WEEK_WINDOW,
    "prob_band_width": PROB_BAND_WIDTH,
    "prob_source": "fair_probs（收盘公允，psc/avgc era 链；与残差同尺度）",
    "unmatched_policy": "剔除并计数（不放宽档位）",
}
_PREDICTIONS_SQL = (
    "SELECT match_key, ftr, fair_probs, competition, season, match_date"
    " FROM backtest_predictions WHERE run_id = ? ORDER BY id"
)
_OUTCOME_SIDES = ("h", "a")
_SIDE_MAP = {"home": "h", "away": "a"}  # 事件队伍侧 → 结果侧


def _read_events(store: CorpusStore) -> list[dict[str, Any]]:
    """事件 parquet → dict 行（缺数据集=空如实）。"""
    path = store.silver_path("guardian", "news_event") / "all"
    if not path.exists():
        return []
    return pq.read_table(path).to_pylist()


@dataclass(frozen=True)
class _Pred:
    """一场的预测面（残差+公允概率+匹配键）。"""

    realized: dict[str, float]  # side → 1[ftr]−p_fair
    fair: dict[str, float]
    league: str
    season: str
    week_monday: date  # match_date 所在 ISO 周的周一（周匹配/分块共用键）


def _predictions(conn: sqlite3.Connection, run_id: int) -> dict[str, _Pred]:
    """Run 预测 → {sid: 残差/公允/匹配键}（无赛果场次剔除）。"""
    rows = conn.execute(_PREDICTIONS_SQL, (run_id,)).fetchall()
    out: dict[str, _Pred] = {}
    for match_key, ftr, fair_json, competition, season, match_date in rows:
        if ftr is None:
            continue
        fair = json.loads(str(fair_json))
        ftr_lower = str(ftr).lower()
        try:
            iso = date.fromisoformat(str(match_date)[:10]).isocalendar()
        except ValueError:
            continue
        out[str(match_key)] = _Pred(
            realized={
                side: (1.0 if ftr_lower == side else 0.0) - float(fair[side])
                for side in SELECTIONS
            },
            fair={side: float(fair[side]) for side in SELECTIONS},
            league=str(competition),
            season=str(season),
            week_monday=date.fromisocalendar(iso[0], iso[1], 1),
        )
    return out


@dataclass
class _Group:
    """一个残差组的累计。"""

    values: list[float] = field(default_factory=list)

    def stats(self) -> dict[str, Any]:
        """n/均值（效应量为主，票面样本量诚实纪律）。"""
        if not self.values:
            return {"n": 0}
        return {"n": len(self.values), "mean": round(fmean(self.values), 4)}


@dataclass(frozen=True)
class _Sample:
    """主面样本：fixture×side 唯一（同场同侧多事件只计一次）。"""

    sid: str
    outcome_side: str  # h/a（事件队伍侧映射）
    value: float
    pred: _Pred
    dose: int  # 该场该侧事件条数（辅助剂量面）


def _fixture_samples(
    events: list[dict[str, Any]], preds: dict[str, _Pred]
) -> list[_Sample]:
    """事件 → fixture×side 唯一样本（主分母=独立场次数；dict 键构造保证唯一）。"""
    dose: dict[tuple[str, str], int] = {}
    for event in events:
        sid = str(event["sid"])
        outcome_side = _SIDE_MAP.get(str(event["side"]))
        if outcome_side is None or sid not in preds:
            continue
        key = (sid, outcome_side)
        dose[key] = dose.get(key, 0) + 1
    return [
        _Sample(
            sid=sid,
            outcome_side=side,
            value=preds[sid].realized[side],
            pred=preds[sid],
            dose=n,
        )
        for (sid, side), n in dose.items()
    ]


def _match_control(event: _Pred, pred: _Pred, side: str) -> bool:
    """对照匹配谓词：同联赛+同季+近比赛周+同概率档（可复现，进报告 params）。"""
    if pred.league != event.league or pred.season != event.season:
        return False
    if abs((pred.week_monday - event.week_monday).days) > MATCH_WEEK_WINDOW * 7:
        return False
    return _band(event.fair[side]) == _band(pred.fair[side])


def _dose_bucket(dose: int) -> str:
    """剂量分层桶（1 条 vs 2+ 条）。"""
    return "2+" if dose >= DOSE_SPLIT else "1"


def _band(prob: float) -> int:
    """概率档（五分位；1.0 收口到顶档）。"""
    return min(4, int(prob / PROB_BAND_WIDTH))


def _side_face(
    samples: list[_Sample],
    preds: dict[str, _Pred],
    event_sids: set[str],
    side: str,
) -> dict[str, Any]:
    """单侧主面：匹配对照 + 剂量分层 + 周分块 bootstrap。"""
    side_samples = [s for s in samples if s.outcome_side == side]
    matched: list[_Sample] = []
    control_sids: set[str] = set()
    for sample in side_samples:
        hits = [
            sid
            for sid, pred in preds.items()
            if sid not in event_sids and _match_control(sample.pred, pred, side)
        ]
        if hits:
            matched.append(sample)
            control_sids.update(hits)
    control_values = [
        (preds[sid].realized[side], preds[sid].week_monday) for sid in control_sids
    ]
    event_values = [(s.value, s.pred.week_monday) for s in matched]
    dose_groups = {
        bucket: _Group(
            [s.value for s in side_samples if _dose_bucket(s.dose) == bucket]
        ).stats()
        for bucket in ("1", "2+")
    }
    face: dict[str, Any] = {
        "event": _Group([v for v, _ in event_values]).stats(),
        "control": _Group([v for v, _ in control_values]).stats(),
        "unmatched_event_samples": len(side_samples) - len(matched),
        "dose": dose_groups,
    }
    if face["event"]["n"] and face["control"]["n"]:
        face["diff"] = round(face["event"]["mean"] - face["control"]["mean"], 4)
        face["week_block_bootstrap"] = _week_block_bootstrap(
            event_values, control_values
        )
    else:
        face["diff"] = None
        face["week_block_bootstrap"] = None
    return face


def _week_block_bootstrap(
    event: list[tuple[float, date]], control: list[tuple[float, date]]
) -> dict[str, Any]:
    """比赛周分块 bootstrap：周为聚类单元重抽，diff 的 2.5–97.5 分位。"""
    e_by_week: dict[date, list[float]] = {}
    for value, week in event:
        e_by_week.setdefault(week, []).append(value)
    c_by_week: dict[date, list[float]] = {}
    for value, week in control:
        c_by_week.setdefault(week, []).append(value)
    e_weeks = sorted(e_by_week)
    c_weeks = sorted(c_by_week)
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311
    diffs = []
    for _ in range(BOOTSTRAP_DRAWS):
        e_vals = [v for w in rng.choices(e_weeks, k=len(e_weeks)) for v in e_by_week[w]]
        c_vals = [v for w in rng.choices(c_weeks, k=len(c_weeks)) for v in c_by_week[w]]
        diffs.append(fmean(e_vals) - fmean(c_vals))
    cuts = quantiles(diffs, n=40)
    return {
        "p2_5": round(cuts[0], 4),
        "p97_5": round(cuts[-1], 4),
        "draws": BOOTSTRAP_DRAWS,
        "unit": "iso_week",
    }


def build_news_signal_report(
    conn: sqlite3.Connection,
    duck_con: duckdb.DuckDBPyConnection,
    store: CorpusStore,
    *,
    run_id: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """事件信号回放（场次级口径）→ reports/ 双视图。"""
    rid, _label, _params, status = load_backtest_run(conn, run_id)
    events = _read_events(store)
    preds = _predictions(conn, rid)
    event_sids = {str(e["sid"]) for e in events} & set(preds)
    samples = _fixture_samples(events, preds)

    # 事件面（描述基线；时距=真 UTC，与抽取器共用单一真相源）
    by_type: dict[str, int] = {}
    for event in events:
        by_type[str(event["event_type"])] = by_type.get(str(event["event_type"]), 0) + 1
    hours_to_kickoff = []
    for event in events:
        try:
            pub = datetime.fromisoformat(
                str(event["published_at"]).replace("Z", "+00:00")
            )
        except ValueError:
            continue
        kickoff = beijing_naive_to_utc(str(event["kickoff_iso"]))
        if kickoff is None:
            continue
        hours_to_kickoff.append(round((kickoff - pub).total_seconds() / 3600, 1))
    hours = sorted(h for h in hours_to_kickoff if h >= 0)
    event_face = {
        "n_events": len(events),
        "n_fixtures_with_events": len(event_sids),
        "by_type": dict(sorted(by_type.items())),
        "hours_to_kickoff_median": hours[len(hours) // 2] if hours else None,
        "hours_to_kickoff_p90": hours[int(len(hours) * 0.9)] if hours else None,
        "kickoff_basis": "beijing_wall_clock−8h→UTC（beijing_naive_to_utc 单一真相源）",
    }

    # 主面：场次级匹配对照（headline 先报独立场次数）
    fixture_face = {
        "n_fixtures": len({s.sid for s in samples}),
        "n_samples": len(samples),
        "by_side": {
            side: _side_face(samples, preds, event_sids, side)
            for side in _OUTCOME_SIDES
        },
    }

    # 次面：事件加权（旧口径降级，非独立样本）
    weighted = _Group()
    by_side_weighted = {s: _Group() for s in _OUTCOME_SIDES}
    for event in events:
        sid = str(event["sid"])
        outcome_side = _SIDE_MAP.get(str(event["side"]))
        if outcome_side and sid in preds:
            value = preds[sid].realized[outcome_side]
            weighted.values.append(value)
            by_side_weighted[outcome_side].values.append(value)
    event_weighted_secondary = {
        "label": "事件加权，非独立样本（旧口径降级面，只作参考不作主结论）",
        "event_side_anchored": weighted.stats(),
        "by_side": {s: g.stats() for s, g in by_side_weighted.items()},
    }

    price_in = _price_in_face(duck_con, sorted(event_sids))

    payload = {
        "built_at": (today or date.today()).isoformat(),
        "run_id": rid,
        "run_status": status,
        "match_params": MATCH_PARAMS,
        "event_face": event_face,
        "fixture_face": fixture_face,
        "event_weighted_secondary": event_weighted_secondary,
        "price_in": price_in,
        "note": (
            "主分母=独立场次数（fixture×side 至多一个样本，headline 先报"
            + " n_fixtures）；事件加权旧口径降为非独立样本次面；对照=同联赛"
            + "+同季+近比赛周(±2)+概率档(五分位，收盘公允 fair_probs)匹配的"
            + "无事件场，匹配不上剔除并计数；不确定性=比赛周分块 bootstrap"
            + "（fixture 聚类由样本单位自然满足）；时距=北京墙钟−8h 真 UTC"
            + "（与抽取器共用单一真相源；旧口径标签替换恒高估 8h）；"
            + "price-in 面只覆盖临场 1h→收盘（发布→1h "
            + "主反应窗未测，不作未 price-in 结论）；时间错位/事件置换对照="
            + "保留面（本票未做）；试点=2024-25 完整季+2025-26 进行中季；"
            + "观察性报告不进模型不改参数；全量回填=门后另裁"
        ),
    }
    reports = store.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{REPORT_BASENAME}.md").write_text(
        _render_md(payload), encoding="utf-8"
    )
    (reports / f"{REPORT_BASENAME}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def _price_in_face(
    duck_con: duckdb.DuckDBPyConnection, sids: list[str]
) -> dict[str, Any]:
    """事件场临场 1h→收盘主队位移（只覆盖反应窗末段，方向面）。"""
    signal_rows = gold_reader.fetch_signal_rows_by_sids(duck_con, sids)
    moves = []
    for sid in sids:
        row = signal_rows.get(sid)
        if row is None:
            continue
        early = _devig_home(
            row.get("q1h1x2_h"), row.get("q1h1x2_d"), row.get("q1h1x2_a")
        )
        close = _devig_home(
            row.get("close1x2_h"), row.get("close1x2_d"), row.get("close1x2_a")
        )
        if early is not None and close is not None:
            moves.append(close - early)
    return {"n": len(moves), "mean": round(fmean(moves), 5) if moves else None}


def _devig_home(h: float | None, d: float | None, a: float | None) -> float | None:
    """三向比例 devig 主队概率（缺端/≤1 → None）。"""
    values = [h, d, a]
    if any(v is None for v in values):
        return None
    clean = [float(v) for v in values if v is not None]
    if any(v <= 1.0 or not math.isfinite(v) for v in clean):
        return None
    inv = [1.0 / v for v in clean]
    return inv[0] / sum(inv)


def _render_md(payload: dict[str, Any]) -> str:
    """人读视图（headline 先报独立场次数）。"""
    face = payload["event_face"]
    fixture = payload["fixture_face"]
    weighted = payload["event_weighted_secondary"]
    lines = [
        "# 事件信号回放（场次级口径）",
        "",
        f"run={payload['run_id']} 事件 {face['n_events']} 条 / "
        + f"**独立场次 {fixture['n_fixtures']}**（fixture×side 样本 "
        + f"{fixture['n_samples']}）",
        f"类型分布 {face['by_type']}",
        f"距开球（真 UTC）中位 {face['hours_to_kickoff_median']}h / p90 "
        + f"{face['hours_to_kickoff_p90']}h（口径：{face['kickoff_basis']}）",
        "",
        "## 残差对照（场次级主面：匹配对照）",
        "",
        f"匹配谓词 {payload['match_params']}",
        "",
    ]
    for side, label in (("h", "主队侧"), ("a", "客队侧")):
        sf = fixture["by_side"][side]
        boot = sf.get("week_block_bootstrap")
        boot_text = (
            f"周分块 bootstrap 2.5–97.5% [{boot['p2_5']}, {boot['p97_5']}]"
            if boot
            else "bootstrap=样本不足"
        )
        dose = sf["dose"]
        lines.append(
            f"- {label}：事件 n={sf['event'].get('n')} 均值 {sf['event'].get('mean')}"
            + f" vs 匹配对照 n={sf['control'].get('n')} 均值 "
            + f"{sf['control'].get('mean')} → diff {sf.get('diff')}"
            + f"（{boot_text}；匹配不上剔除 {sf['unmatched_event_samples']}"
            + " 样本(fixture×side)；"
            + f"剂量 1 条 n={dose['1'].get('n')}/均值 {dose['1'].get('mean')}，"
            + f"2+ 条 n={dose['2+'].get('n')}/均值 {dose['2+'].get('mean')}）"
        )
    anchored = weighted["event_side_anchored"]
    sides_w = " / ".join(
        f"{s}:{weighted['by_side'][s].get('mean')}" for s in _OUTCOME_SIDES
    )
    lines += [
        "",
        "## 次面：事件加权（非独立样本，旧口径降级）",
        "",
        f"- 事件侧锚定 realized 均值 {anchored.get('mean')}"
        + f"（n={anchored.get('n')} 条事件——同场多事件重复计入，只作参考）",
        f"- 分侧：{sides_w}",
        "",
        "## price-in 面（事件场临场 1h→收盘主队位移）",
        "",
        f"- n={payload['price_in']['n']} 均值 {payload['price_in']['mean']}"
        + "（只覆盖反应窗末 1h；发布→1h 主反应窗未测，不作未 price-in 结论）",
        "",
        "- 判读注意：" + payload["note"],
    ]
    return "\n".join(lines) + "\n"
