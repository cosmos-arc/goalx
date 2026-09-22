"""
开→收漂移复现（research/18 §一/§三 承重问题；票 54；纯读，不驱动投注）。

数据面：PSH/PSD/PSA（Pinnacle 早期价，v19 落列）vs PSC（收盘）——同书同
口径（票 46 增补裁决），十年 × 11 联赛（票 51 已回填 38,460 行）。

三视角口径：
- **公允基准** = PSC Shin 收盘（票 34/46/51 同则；"收盘最有效"是待验假设
  而非前提——实测视角独立检验它）；
- **早锁视角**（CLV 框架）：EV_early = p_close × o_early − 1——早价以
  收盘公允评估；
- **等待视角**：EV_close = p_close × o_close − 1 ≡ Pinnacle Shin 水位
  （同书自评，恒 ≈ −2% 量级，作对照非策略）；
- **实测视角**（不依赖"收盘即真"）：ftr 命中 → ROI_early/ROI_close 的
  实现值——早锁 vs 等待的主判口径。

Buchdahl"价值蒸发"复现：早窗价值组（EV_early 超阈）ex-ante EV 从早到收
的残存比例 + 价格 steamed（o_close < o_early）分布。

限制（诚实标注）：fd 列不含 PSH 挂牌时点元数据，"早期"口径 = 源侧早期
价（通常周初挂牌）；PSH/PSC 同书自比隔离了跨书差异，但与 soft 书早价
（无列，票 46 裁决不导）不可比。竞彩无历史价——本复验是欧赔机制研究，
时机策略结论映射到竞彩须走代理口径（票 34 同原则）。

确定性：报告是库内容与入参的纯函数（无时间戳），同参数重跑逐字节一致。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from goalx_backend import odds_math as om
from goalx_backend.data import results as rs_store
from goalx_backend.evaluation.pool_replay import percentiles

_SELECTIONS: tuple[str, str, str] = ("h", "d", "a")
_FTR_BY_SELECTION = {"h": "H", "d": "D", "a": "A"}

# 早窗价值组阈值（EV_early 超阈入组）；0=全正组，0.05/0.10=典型价值带
VALUE_THRESHOLDS: tuple[float, ...] = (0.0, 0.05, 0.10)

CALIBER_TEXT = (
    "公允 = PSC Shin 收盘；EV_early = p_close × PSH 早价 − 1（CLV 框架，"
    "早价以收盘公允评估）；EV_close = p_close × PSC 收价 − 1（同书自评"
    "对照）；实测 ROI = ftr 命中 × 价 − 1 的均值（不依赖收盘即真假设）；"
    "价值组 = EV_early 超阈；蒸发比 = 1 − EV_close/EV_early（组均值口径）；"
    "steamed = 收价低于早价。PSH/PSC 同书同口径；早期价无挂牌时点元数据。"
)


@dataclass(frozen=True)
class _DriftRow:
    """一个（场次 × 选向）的早/收两价与实测结果。"""

    competition: str
    season: str
    selection: str
    odds_early: float
    odds_close: float
    p_close: float  # PSC Shin
    hit: bool

    @property
    def ev_early(self) -> float:
        return self.p_close * self.odds_early - 1.0

    @property
    def ev_close(self) -> float:
        return self.p_close * self.odds_close - 1.0


def _triple(row: sqlite3.Row, prefix: str) -> tuple[float, float, float] | None:
    """行 → 三向赔率；任一缺/退化（≤1）整组不可用返回 None。"""
    raw = (row[f"{prefix}_home"], row[f"{prefix}_draw"], row[f"{prefix}_away"])
    odds: list[float] = []
    for value in raw:
        if value is None or float(value) <= 1.0:
            return None
        odds.append(float(value))
    home, draw, away = odds
    return home, draw, away


def collect_drift_rows(
    conn: sqlite3.Connection,
    *,
    competitions: tuple[str, ...],
    seasons: tuple[str, ...],
) -> tuple[list[_DriftRow], dict[str, int]]:
    """十年语料 → 早/收双全的漂移行 + 覆盖统计（确定性遍历）。"""
    wanted = {(c, s) for c in competitions for s in seasons}
    coverage = {
        "rows": 0,
        "out_of_scope": 0,
        "no_psc": 0,  # 收盘基准缺（2526 部分/2627 全部）→ 无法评早价
        "no_psh": 0,  # 早期价缺 → 无漂移可测
        "usable": 0,  # PSH+PSC 双全
    }
    out: list[_DriftRow] = []
    for row in rs_store.hist_pool_replay_rows(conn):
        coverage["rows"] += 1
        if (str(row["competition"]), str(row["season"])) not in wanted:
            coverage["out_of_scope"] += 1
            continue
        psc = _triple(row, "psc")
        psh = _triple(row, "psh")
        if psc is None:
            coverage["no_psc"] += 1
            continue
        if psh is None:
            coverage["no_psh"] += 1
            continue
        coverage["usable"] += 1
        probs = om.shin_implied(psc)
        ftr = str(row["ftr"])
        for position, selection in enumerate(_SELECTIONS):
            out.append(
                _DriftRow(
                    competition=str(row["competition"]),
                    season=str(row["season"]),
                    selection=selection,
                    odds_early=psh[position],
                    odds_close=psc[position],
                    p_close=probs[position],
                    hit=ftr == _FTR_BY_SELECTION[selection],
                )
            )
    return out, coverage


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _roi(rows: list[_DriftRow], *, early: bool) -> float | None:
    """实测 ROI：单位注命中按价派彩的均值（1 × o × hit − 1）。"""
    if not rows:
        return None
    odds_key = "odds_early" if early else "odds_close"
    return sum((getattr(r, odds_key) if r.hit else 0.0) - 1.0 for r in rows) / len(rows)


def _cohort(rows: list[_DriftRow], threshold: float) -> dict[str, Any]:
    """一个价值阈值组：ex-ante 早/收 EV、蒸发比、steamed、实测双口径。"""
    group = [r for r in rows if r.ev_early > threshold]
    if not group:
        return {"n": 0}
    ev_early_mean = _mean([r.ev_early for r in group])
    ev_close_mean = _mean([r.ev_close for r in group])
    return {
        "n": len(group),
        "ev_early_mean": round(ev_early_mean, 4) if ev_early_mean else None,
        "ev_close_mean": round(ev_close_mean, 4) if ev_close_mean else None,
        # Buchdahl 蒸发比：正值=早窗 EV 到收盘残存的比例为 1−蒸发比
        "evaporation_ratio": (
            round(1 - ev_close_mean / ev_early_mean, 4)
            if ev_early_mean and ev_close_mean is not None
            else None
        ),
        "steamed_rate": round(
            sum(1 for r in group if r.odds_close < r.odds_early) / len(group), 4
        ),
        "mean_drift_close_over_early": _mean(
            [r.odds_close / r.odds_early - 1 for r in group]
        ),
        "realized_hit_rate": round(sum(r.hit for r in group) / len(group), 4),
        "mean_p_close": round(_mean([r.p_close for r in group]) or 0.0, 4),
        "roi_early_realized": round(_roi(group, early=True) or 0.0, 4),
        "roi_close_realized": round(_roi(group, early=False) or 0.0, 4),
    }


def _summary_blocks(rows: list[_DriftRow]) -> dict[str, Any]:
    """全池概览 + 分选向块（drift_replay_report 的装配半体）。"""
    overall = {
        "n": len(rows),
        "ev_early_mean": round(_mean([r.ev_early for r in rows]) or 0.0, 6),
        "ev_early_percentiles": percentiles([r.ev_early for r in rows]),
        "ev_close_mean": round(_mean([r.ev_close for r in rows]) or 0.0, 6),
        "drift_percentiles": percentiles(
            [r.odds_close / r.odds_early - 1 for r in rows]
        ),
        "steamed_rate": round(
            sum(1 for r in rows if r.odds_close < r.odds_early) / len(rows), 4
        )
        if rows
        else None,
        # 全选实测基线（无选择策略，作水位参照）
        "roi_early_realized_all": round(_roi(rows, early=True) or 0.0, 4),
        "roi_close_realized_all": round(_roi(rows, early=False) or 0.0, 4),
    }
    by_selection = {
        selection: {
            "n": len(group),
            "ev_early_mean": round(_mean([r.ev_early for r in group]) or 0.0, 4),
            "roi_early_realized": round(_roi(group, early=True) or 0.0, 4),
            "roi_close_realized": round(_roi(group, early=False) or 0.0, 4),
            "realized_hit_rate": round(sum(r.hit for r in group) / len(group), 4),
            "mean_p_close": round(_mean([r.p_close for r in group]) or 0.0, 4),
        }
        for selection in _SELECTIONS
        for group in [[r for r in rows if r.selection == selection]]
        if group
    }
    return {"overall": overall, "by_selection": by_selection}


def drift_replay_report(
    conn: sqlite3.Connection,
    *,
    competitions: tuple[str, ...],
    seasons: tuple[str, ...],
) -> dict[str, Any]:
    """
    十年开→收漂移复验报告（纯函数：库内容 × 入参，无时间戳）。

    输出契约：params/caliber/coverage/overall/by_selection/by_season/
    value_cohorts——实测 ROI 为主判，ex-ante EV 为 CLV 对照。
    """
    rows, coverage = collect_drift_rows(
        conn, competitions=competitions, seasons=seasons
    )
    blocks = _summary_blocks(rows)
    by_season: dict[str, dict[str, Any]] = {}
    for season in sorted({r.season for r in rows}):
        group = [r for r in rows if r.season == season]
        by_season[season] = {
            "n": len(group),
            "ev_early_mean": round(_mean([r.ev_early for r in group]) or 0.0, 4),
            "value_cohort_5pct": {
                k: v
                for k, v in _cohort(group, 0.05).items()
                if k in ("n", "roi_early_realized", "roi_close_realized")
            },
        }
    value_cohorts = {
        f"ev_early_gt_{threshold:.2f}": _cohort(rows, threshold)
        for threshold in VALUE_THRESHOLDS
    }
    return {
        "params": {
            "value_thresholds": list(VALUE_THRESHOLDS),
            "competitions": list(competitions),
            "seasons": list(seasons),
        },
        "caliber": CALIBER_TEXT,
        "coverage": coverage,
        "overall": blocks["overall"],
        "by_selection": blocks["by_selection"],
        "by_season": by_season,
        "value_cohorts": value_cohorts,
    }
