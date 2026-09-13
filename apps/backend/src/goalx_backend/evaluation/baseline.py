"""
历史基准质检与分期/来源对照（票 34）。

- football-data.co.uk 的 PSC 列自 2025-07-23 起换源（spec §历史基准），
  以该日切期分别统计 PSC/AvgC 的数量、缺失、平均 overround 与两源
  Shin 概率差异——描述统计，不判定哪边是真概率；
- ``run_baseline_comparison`` 用 ``fair_source='psc'/'avgc'`` 各产生一个
  对照 run（旧 run 保留，不无条件改用任一来源）；
- 合成竞彩价实验只能作模型实验，不得称真实陈盘回放（run params 已标注
  price_model='simulated_jc'）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

from goalx_backend import odds_math as om
from goalx_backend.data import results as rs_store
from goalx_backend.evaluation import backtest as bt
from goalx_backend.markets import SELECTIONS

FD_PSC_REVIEW_DATE = "2025-07-23"


def _valid(odds: tuple[float | int | None, ...]) -> bool:
    return all(isinstance(o, (int, float)) and float(o) > 1.0 for o in odds)


def _period(match_date: str) -> str:
    if match_date >= FD_PSC_REVIEW_DATE:
        return f"since_{FD_PSC_REVIEW_DATE}"
    return f"before_{FD_PSC_REVIEW_DATE}"


def baseline_quality_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """分期 × 来源的基准数据质检（数量/缺失/overround/两源差异）。"""
    rows = rs_store.hist_close_odds_rows(conn)
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        period = _period(str(row["match_date"]))
        entry = buckets.setdefault(
            period,
            {
                "n": 0,
                "n_psc": 0,
                "n_avgc": 0,
                "n_both": 0,
                "n_neither": 0,
                "overround_psc": 0.0,
                "overround_avgc": 0.0,
                "prob_diff": 0.0,
                "n_diff": 0,
            },
        )
        entry["n"] += 1
        psc = (row["psc_home"], row["psc_draw"], row["psc_away"])
        avgc = (row["avgc_home"], row["avgc_draw"], row["avgc_away"])
        has_psc, has_avgc = _valid(psc), _valid(avgc)
        if has_psc:
            entry["n_psc"] += 1
            entry["overround_psc"] += sum(1.0 / float(o) for o in psc) - 1.0
        if has_avgc:
            entry["n_avgc"] += 1
            entry["overround_avgc"] += sum(1.0 / float(o) for o in avgc) - 1.0
        if has_psc and has_avgc:
            entry["n_both"] += 1
            p_probs = om.shin_implied(tuple(float(o) for o in psc))
            a_probs = om.shin_implied(tuple(float(o) for o in avgc))
            entry["prob_diff"] += sum(
                abs(p - a) for p, a in zip(p_probs, a_probs, strict=True)
            ) / len(SELECTIONS)
            entry["n_diff"] += 1
        if not has_psc and not has_avgc:
            entry["n_neither"] += 1
    report: dict[str, Any] = {"review_date": FD_PSC_REVIEW_DATE, "periods": {}}
    for period, entry in sorted(buckets.items()):
        n = entry["n"]
        report["periods"][period] = {
            "n": n,
            "n_psc": entry["n_psc"],
            "n_avgc": entry["n_avgc"],
            "n_both": entry["n_both"],
            "n_neither": entry["n_neither"],
            "psc_missing_rate": 1 - entry["n_psc"] / n if n else None,
            "avgc_missing_rate": 1 - entry["n_avgc"] / n if n else None,
            "mean_overround_psc": (
                entry["overround_psc"] / entry["n_psc"] if entry["n_psc"] else None
            ),
            "mean_overround_avgc": (
                entry["overround_avgc"] / entry["n_avgc"] if entry["n_avgc"] else None
            ),
            "mean_shin_prob_abs_diff_psc_vs_avgc": (
                entry["prob_diff"] / entry["n_diff"] if entry["n_diff"] else None
            ),
        }
    report["limitations"] = (
        "描述统计,不判定真概率;两源差异含噪声;合成价实验非真实陈盘回放"
    )
    return report


def run_baseline_comparison(
    conn: sqlite3.Connection, params: bt.BacktestParams, *, base_label: str = "baseline"
) -> dict[str, dict[str, Any]]:
    """fair_source=psc/avgc 各跑一个对照 run（旧 run 保留，票 34 验收 5）。"""
    results: dict[str, dict[str, Any]] = {}
    for source in ("psc", "avgc"):
        run_params = replace(params, fair_source=source)
        result = bt.run_backtest(conn, run_params, label=f"{base_label}-{source}")
        results[source] = {
            "run_id": result.run_id,
            "predictions": result.predictions,
            "bets": result.bets,
            "roi": round(result.roi, 6),
            "skipped": result.skipped,
        }
    return results
