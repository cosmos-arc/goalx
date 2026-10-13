"""
信号级归因报告（backtest-decade 票 21，spec S21）。

AH/OU 收盘族特征与模型残差的关系盘点——"模型≈市场"的旧结论是否在新
特征下松动。数据 = 十年 run 预测（模型概率 vs 同链公平概率 + ftr）×
gold 收盘族特征（漂移/共识/分歧/相位/水位/临场切片，只读桥取数，
PIT 纪律由 gold 构建面保证）。

口径（每场两个量，主队侧与平局侧分列）：

- ``model_edge_c = p_model(c) − p_fair(c)``：模型对市场的主张（残差）；
- ``realized_c = 1[ftr=c] − p_fair(c)``：市场的实际误差（结算标签侧）。

面（每特征×桶）：

- **市场可预测面**（真问题）：桶内 mean(realized_c) 显著≠0 = 市场在该
  桶系统性错价（未消化信息=策略空间）；
- **模型捕捉面**：pearson(model_edge_c, realized_c)——模型的残差方向
  是否与市场误差方向一致（>0=模型在该桶有超额信息，"模型≈市场"松动）；
- **松动判据**（探索性）：|t(realized)|≥2 且 capture>0 的桶列出，带
  n 与效应量（mean 与相关系数），不做多重比较校正（如实声明探索性）。

桶型：漂移类按符号（neg/zero/pos）；分布类（分歧度/书数/相位/水位）
按分位三分（low/mid/high）；临场切片按晚段位移符号。观察性报告：
不进引擎、不改参数、不新建 gold 列。
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from statistics import fmean, stdev
from typing import Any

import duckdb

from goalx_backend.data import gold_reader
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.evaluation.metrics import load_backtest_run

REPORT_BASENAME = "signal-attribution"
MIN_BUCKET_N = 100  # 桶内低于此 n 不下松动结论（探索纪律）
T_SIGNAL = 2.0  # realized 均值 |t| 过线才算市场错价
MIN_CORR_SAMPLES = 3  # 相关/t 统计最少样本
EPS = 1e-9
# 字面量拼接只为行宽；参数绑定注入面为零（S608 误报豁免）
_PREDICTIONS_SQL = (
    "SELECT match_key, era, ftr, had_probs, fair_probs"  # noqa: S608
    + " FROM backtest_predictions WHERE run_id = ? ORDER BY id"
)

# 特征清单：(特征键, gold 列, 桶型)；桶型 drift=符号三分，dist=分位三分。
# 特征键贯穿存储与分桶（Spec 审查：键/列错位曾致六轴静默消失）。
_FEATURES: tuple[tuple[str, str, str], ...] = (
    ("ah_line_drift", "ah_line_drift", "drift"),
    ("ah_anchor_drift", "ah_anchor_drift", "drift"),
    ("ou_line_drift", "ou_line_drift", "drift"),
    ("ah_dispersion", "ah_close_dispersion", "dist"),
    ("ou_dispersion", "ou_close_dispersion", "dist"),
    ("ah_n_books", "ah_n_books", "dist"),
    ("ou_n_books", "ou_n_books", "dist"),
    ("ah_phase_early", "ah_n_early", "dist"),
    ("ah_phase_pre", "ah_n_pre", "dist"),
    ("ah_phase_inplay", "ah_n_inplay", "dist"),
    ("ou_phase_early", "ou_n_early", "dist"),
    ("ou_phase_pre", "ou_n_pre", "dist"),
    ("ou_phase_inplay", "ou_n_inplay", "dist"),
    ("ah_home_water", "ah_close_home_water_med", "dist"),
    ("ou_over_water", "ou_close_over_water_med", "dist"),
)
# 派生特征（_signal_of 内计算，键=列名注册）：1x2 临场位移（1h/24h 两档）
# + AH 锚同线水位位移（两向 malay 归一，同法两端）
_DERIVED_FEATURES: tuple[tuple[str, str], ...] = (
    ("late_move_h", "drift"),
    ("late_move24_h", "drift"),
    ("late_move_ah_home", "drift"),
)
_SIDES = ("h", "d", "a")


@dataclass(frozen=True)
class _MatchSignal:
    """一场的归因样本（三向：主队/平局/客队侧）。"""

    era: str
    model_edges: dict[str, float]
    realized: dict[str, float]
    features: dict[str, float]


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """样本 Pearson r（样本不足或零方差 → None）。"""
    n = len(xs)
    if n < MIN_CORR_SAMPLES:
        return None
    mx, my = fmean(xs), fmean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    sxx = sum(v * v for v in dx)
    syy = sum(v * v for v in dy)
    if sxx <= EPS or syy <= EPS:
        return None
    return sum(a * b for a, b in zip(dx, dy, strict=True)) / math.sqrt(sxx * syy)


def _mean_t(values: list[float]) -> tuple[float | None, float | None]:
    """(均值, t=mean/se)；样本不足或零方差 → t=None。"""
    if not values:
        return None, None
    mean = fmean(values)
    if len(values) < MIN_CORR_SAMPLES:
        return mean, None
    std = stdev(values)
    if std <= EPS:
        return mean, None
    return mean, mean / (std / math.sqrt(len(values)))


def _bucket_of(value: float, kind: str, cuts: tuple[float, float]) -> str:
    """桶名：漂移按符号；分布类按分位切点。"""
    if kind == "drift":
        if value < -EPS:
            return "neg"
        if value > EPS:
            return "pos"
        return "zero"
    return "low" if value <= cuts[0] else ("mid" if value <= cuts[1] else "high")


def _tertile_cuts(values: list[float]) -> tuple[float, float]:
    """分位三分切点（q33/q67，排序索引法）。"""
    ordered = sorted(values)
    n = len(ordered)
    q33 = ordered[max(0, (n * 1) // 3 - 1)] if n else 0.0
    q67 = ordered[max(0, (n * 2) // 3 - 1)] if n else 0.0
    return q33, q67


def _bucket_stats(samples: list[_MatchSignal]) -> dict[str, Any]:
    """一桶的归因面：市场可预测面 + 模型捕捉面（三向，era 混布随行）。"""
    era_counts: dict[str, int] = defaultdict(int)
    for s in samples:
        era_counts[s.era] += 1
    face: dict[str, Any] = {
        "n": len(samples),
        "era_mix": dict(sorted(era_counts.items())),
    }
    for side in _SIDES:
        edges = [s.model_edges[side] for s in samples]
        real = [s.realized[side] for s in samples]
        mean, t_stat = _mean_t(real)
        face[f"market_bias_{side}"] = round(mean, 4) if mean is not None else None
        face[f"market_bias_{side}_t"] = round(t_stat, 2) if t_stat is not None else None
        face[f"model_edge_{side}_mean"] = round(fmean(edges), 4) if edges else None
        face[f"capture_{side}"] = _round_or_none(_pearson(edges, real))
    return face


def _round_or_none(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _verdict_lines(payload_faces: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    """
    结论句双清单（探索性）：

    - **松动桶**：|t|≥2 且模型捕捉 r>0（市场错价且模型残差同向=超额信息）；
    - **市场错价桶**：|t|≥2 即列（策略空间候选——市场未消化信息，
      模型未捕捉的也列出：特征预测市场误差可行，模型没吃到）。
    """
    loosened: list[str] = []
    mispriced: list[str] = []
    for feature, buckets in payload_faces.items():
        for bucket, stats in buckets.items():
            if stats["n"] < MIN_BUCKET_N:
                continue
            for side in _SIDES:
                t_stat = stats.get(f"market_bias_{side}_t")
                bias = stats.get(f"market_bias_{side}")
                capture = stats.get(f"capture_{side}")
                if t_stat is None or bias is None or abs(t_stat) < T_SIGNAL:
                    continue
                mispriced.append(
                    f"- {feature}:{bucket}（n={stats['n']}）{side} 侧："
                    + f"市场偏差 {bias:+.4f}（t={t_stat}）"
                )
                if capture is not None and capture > 0:
                    loosened.append(
                        f"- {feature}:{bucket}（n={stats['n']}）{side} 侧："
                        + f"市场偏差 {bias:+.4f}（t={t_stat}）"
                        + f"，模型捕捉 r={capture:+.4f} → 松动候选（探索性）"
                    )
    lines = ["**松动桶（模型≈市场松动，需偏差+捕捉双条件）**", ""]
    lines += loosened or ["- 无松动桶：全特征×桶上模型残差与市场误差无一致方向"]
    lines += [
        "",
        "**市场错价桶（策略空间候选：|t|≥2；模型未捕捉=下迭代特征候选）**",
        "",
    ]
    lines += mispriced or ["- 无市场错价桶：收盘在全部特征桶上无显著偏差"]
    return lines


def _collect_samples(
    pred_rows: list[sqlite3.Row],
    gold: dict[str, dict[str, Any]],
) -> tuple[list[_MatchSignal], dict[str, int]]:
    """Run 预测 × gold 特征 → 归因样本（缺口按类计数）。"""
    samples: list[_MatchSignal] = []
    skipped = {"no_ftr": 0, "no_gold": 0, "no_fair": 0}
    for row in pred_rows:
        if row["ftr"] is None:
            skipped["no_ftr"] += 1
            continue
        gold_row = gold.get(str(row["match_key"]))
        if gold_row is None:
            skipped["no_gold"] += 1
            continue
        fair_raw = json.loads(str(row["fair_probs"]))
        model_raw = json.loads(str(row["had_probs"]))
        if (
            not isinstance(fair_raw, dict)
            or not isinstance(model_raw, dict)
            or not all(
                isinstance(fair_raw.get(c), int | float)
                and isinstance(model_raw.get(c), int | float)
                for c in _SIDES
            )
        ):
            skipped["no_fair"] += 1
            continue
        fair: dict[str, Any] = fair_raw
        model: dict[str, Any] = model_raw
        samples.append(_signal_of(row, gold_row, fair, model))
    return samples, skipped


def _signal_of(
    row: sqlite3.Row,
    gold_row: dict[str, Any],
    fair: dict[str, Any],
    model: dict[str, Any],
) -> _MatchSignal:
    """单场归因样本（特征全为 PIT 列；realized 用赛果做标签）。"""
    ftr = str(row["ftr"]).lower()
    features: dict[str, float] = {}
    for key, column, _kind in _FEATURES:
        value = gold_row.get(column)
        if value is not None:
            features[key] = float(value)
    # 临场切片特征（1x2 锚族，比例 devig 两端同法）：1h/24h → close 主队位移
    close = _devig_probs(
        gold_row.get("close1x2_h"),
        gold_row.get("close1x2_d"),
        gold_row.get("close1x2_a"),
    )
    for ref, feature_key in (("q1h1x2", "late_move_h"), ("q24h1x2", "late_move24_h")):
        early = _devig_probs(
            gold_row.get(f"{ref}_h"), gold_row.get(f"{ref}_d"), gold_row.get(f"{ref}_a")
        )
        if early is not None and close is not None:
            features[feature_key] = close[0] - early[0]
    # AH 锚同线水位位移（q24h→close；两向 malay 水位归一，同法两端；
    # 线不同/水位越 (0,1) 带则缺——不跨线硬比）
    ah_early = _malay_home_prob(
        gold_row.get("q24hah_home_water"), gold_row.get("q24hah_away_water")
    )
    ah_close = _malay_home_prob(
        gold_row.get("ah_anchor_close_home_water"),
        gold_row.get("ah_anchor_close_away_water"),
    )
    same_line = gold_row.get("q24hah_line") is not None and (
        float(gold_row["q24hah_line"]) == float(gold_row["ah_anchor_close_line"])
        if gold_row.get("ah_anchor_close_line") is not None
        else False
    )
    if same_line and ah_early is not None and ah_close is not None:
        features["late_move_ah_home"] = ah_close - ah_early
    return _MatchSignal(
        era=str(gold_row["era"]) if gold_row["era"] is not None else "unknown",
        model_edges={side: float(model[side]) - float(fair[side]) for side in _SIDES},
        realized={
            side: (1.0 if ftr == side else 0.0) - float(fair[side]) for side in _SIDES
        },
        features=features,
    )


def _malay_home_prob(w_home: float | None, w_away: float | None) -> float | None:
    """两向 malay 水位归一的主队概率（w/(w_h+w_a)；水位越 (0,1) 带→None）。"""
    if w_home is None or w_away is None:
        return None
    if not (0.0 < w_home < 1.0 and 0.0 < w_away < 1.0):
        return None
    return w_home / (w_home + w_away)


def _feature_faces(
    samples: list[_MatchSignal],
) -> dict[str, dict[str, dict[str, Any]]]:
    """特征×桶分层面（漂移按符号三分；分布类按分位三分；键贯穿存取）。"""
    faces: dict[str, dict[str, dict[str, Any]]] = {}
    feature_defs = [(key, kind) for key, _column, kind in _FEATURES] + list(
        _DERIVED_FEATURES
    )
    for key, kind in feature_defs:
        present = [s for s in samples if key in s.features]
        if not present:
            continue
        values = [s.features[key] for s in present]
        cuts = _tertile_cuts(values) if kind == "dist" else (0.0, 0.0)
        groups: dict[str, list[_MatchSignal]] = defaultdict(list)
        for sample in present:
            groups[_bucket_of(sample.features[key], kind, cuts)].append(sample)
        faces[key] = {
            bucket: _bucket_stats(group) for bucket, group in sorted(groups.items())
        }
    return faces


def build_signal_attribution_report(
    conn: sqlite3.Connection,
    duck_con: duckdb.DuckDBPyConnection,
    store: CorpusStore,
    *,
    run_id: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """信号级归因：残差×收盘族特征分桶 → reports/ 双视图。"""
    rid, label, _params, run_status = load_backtest_run(conn, run_id)
    pred_rows = conn.execute(_PREDICTIONS_SQL, (rid,)).fetchall()
    sids = [str(r["match_key"]) for r in pred_rows if r["ftr"] is not None]
    gold = gold_reader.fetch_signal_rows_by_sids(duck_con, sids)

    samples, skipped = _collect_samples(pred_rows, gold)
    faces = _feature_faces(samples)

    payload = {
        "built_at": (today or date.today()).isoformat(),
        "run_id": rid,
        "label": label,
        "run_status": run_status,
        "n_predictions": len(pred_rows),
        "n_samples": len(samples),
        "skipped": skipped,
        "overall": _bucket_stats(samples) if samples else {"n": 0},
        "faces": faces,
        "note": (
            "探索性归因：桶结论带 n 与效应量、不做多重比较校正；"
            + "观察性报告不进引擎不改参数；realized 用赛果做标签"
            + "（结算侧），特征全为 PIT 列（gold 构建面断言保证）"
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


def _devig_probs(
    h: float | None, d: float | None, a: float | None
) -> tuple[float, float, float] | None:
    """三向比例 devig（任一缺失/≤1 → None）。"""
    odds: list[float] = []
    for value in (h, d, a):
        if value is None or float(value) <= 1.0:
            return None
        odds.append(float(value))
    inv = [1.0 / o for o in odds]
    total = sum(inv)
    if total <= 0 or not math.isfinite(total):
        return None
    p_h, p_d, p_a = (i / total for i in inv)
    return p_h, p_d, p_a


def _render_md(payload: dict[str, Any]) -> str:
    """人读视图：松动桶结论 + 特征面表（完整桶在 JSON）。"""
    status_note = (
        ""
        if payload["run_status"] == "done"
        else f"\n\n> ⚠ run 状态={payload['run_status']}：中途检视，不代表全量"
    )
    overall = payload["overall"]
    lines = [
        "# 信号级归因（票 21）",
        "",
        f"run={payload['run_id']}（{payload['label']}）"
        + f" 样本 {payload['n_samples']}/{payload['n_predictions']} 场"
        + status_note,
        "",
        "## 归因结论（|t|≥2 门；探索性）",
        "",
        *_verdict_lines(payload["faces"]),
        "",
        "## 总体面（不分桶基线）",
        "",
        "- 市场偏差 "
        + " ".join(
            f"{s}={overall.get(f'market_bias_{s}')}"
            + f"（t={overall.get(f'market_bias_{s}_t')}）"
            for s in _SIDES
        ),
        "- 模型捕捉 "
        + " ".join(f"r_{s}={overall.get(f'capture_{s}')}" for s in _SIDES)
        + "（>0=模型残差与市场误差同向）",
        "",
        "## 特征面（桶级摘要）",
        "",
        "| 特征 | 桶 | n | 偏差h(t) | 残差h | 捕获h | 偏差d(t) | 捕获d"
        + " | 偏差a(t) | 捕获a |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for feature, buckets in payload["faces"].items():
        for bucket, stats in buckets.items():
            if stats["n"] == 0:
                continue
            cells = [
                feature,
                bucket,
                str(stats["n"]),
                "{}({})".format(
                    stats.get("market_bias_h"), stats.get("market_bias_h_t")
                ),
                str(stats.get("model_edge_h_mean")),
                str(stats.get("capture_h")),
                "{}({})".format(
                    stats.get("market_bias_d"), stats.get("market_bias_d_t")
                ),
                str(stats.get("capture_d")),
                "{}({})".format(
                    stats.get("market_bias_a"), stats.get("market_bias_a_t")
                ),
                str(stats.get("capture_a")),
            ]
            lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "- 判读注意：" + payload["note"],
        "- 完整 era 混布与三向残差均值见 signal-attribution.json。",
    ]
    return "\n".join(lines) + "\n"
