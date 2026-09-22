"""
彩池 v2 搏冷结构性结论的十年样本外复验（票 51；纯读，不驱动投注）。

镜像 v2 口径（data/pool.py 彩池公式 + api/pool.py 搏冷判定）：
- **冷门候选** = 公众份额代理 < COLD_SHARE_MAX（25%，与 v2 判定同源常量）；
- **彩池镜 EV** = fair × 返奖率(65%) ÷ share − 1（data.pool.parimutuel_ev
  同式，未建模 price impact 与分彩风险，v1 三修正口径沿用）；
- **fair（模型侧镜像）**：PSC Shin（Pinnacle 收盘）；PSC 缺行 AvgC Shin
  兜底——psc/avgc 分期全程分开统计不混算（票 34 同则）；avgc 分期 fair
  与 share 同源 → 彩池 EV ≡ −35%（口径退化，仅标注不作证据）；
- **share（公众侧代理）**：AvgC Shin 隐含概率。live 侧为源B 人气分布，
  十年窗无存档——欧赔代理口径诚实标注（票 46/51；live 首验实证
  "欧指去水概率 ≈ 人气份额锁步"，支持 Shin 隐含作份额代理）；
- **固定赔率镜**：EV_fx = fair × AvgC 收盘价 − 1（欧赔代理价；翻案探针
  ——竞彩史价无存档，远端不得冒充真实竞彩价，票 34 同原则）；
- **早期镜（消失速度）**：fair_early = PSH Shin（Pinnacle 早期价，v19
  落列）；share 仍为收盘口径（早期 AvgC 缺列，票 46 增补裁决 soft 早期
  不做）——只测 sharp fair 早→收漂移对候选 EV 的侵蚀。

确定性：报告是库内容与入参的纯函数（无时间戳），同参数重跑逐字节一致。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from goalx_backend import odds_math as om
from goalx_backend.data import pool as pool_store
from goalx_backend.data import results as rs_store

# h/d/a 选向（与市场表 SELECTIONS 同序）；ftr 赛果字母对位
_SELECTIONS: tuple[str, str, str] = ("h", "d", "a")
_FTR_BY_SELECTION = {"h": "H", "d": "D", "a": "A"}

CALIBER_TEXT = (
    "冷门候选 = 份额代理(AvgC Shin) < 25%；彩池 EV = fair × 65% ÷ 份额 − 1；"
    "fair = PSC Shin(分期 psc) / AvgC Shin(分期 avgc，PSC 缺行兜底)；"
    "份额代理 = AvgC Shin 隐含(公众分布代理，非官方池份额——live 侧为源B 人气，"
    "十年窗无存档)；固定赔率镜 EV = fair × AvgC 收盘价 − 1(欧赔代理价，"
    "非真实竞彩史价)；早期镜 fair = PSH Shin，份额仍为收盘口径；"
    "未建模 price impact 与分彩风险。"
)

DEGENERATE_NOTE = (
    "avgc 分期 fair 与份额同源(AvgC Shin)：彩池 EV ≡ 65% − 1 = −35% 恒负，"
    "该分期彩池镜仅作口径标注不作结构性证据；结构性证据来自 psc 分期"
    "(PSC 与 AvgC 两独立书)。"
)


@dataclass(frozen=True)
class _Candidate:
    """一个冷门候选（场次 × 选向），携带三镜估值与分期标签。"""

    competition: str
    season: str
    home_team: str
    selection: str
    stage: str  # psc | avgc
    fair: float
    share: float
    pool_ev: float
    fx_ev: float
    hit: bool  # 赛果命中该选向
    early_fair: float | None
    early_pool_ev: float | None


def _odds_triple(row: sqlite3.Row, prefix: str) -> tuple[float, float, float] | None:
    """行 → (h, d, a) 三向赔率；任一缺/退化（≤1）整组不可用返回 None。"""
    raw = (row[f"{prefix}_home"], row[f"{prefix}_draw"], row[f"{prefix}_away"])
    odds: list[float] = []
    for value in raw:
        if value is None or float(value) <= 1.0:
            return None
        odds.append(float(value))
    home, draw, away = odds
    return home, draw, away


def _shin_or_none(
    odds: tuple[float, float, float] | None,
) -> tuple[float, float, float] | None:
    """三向赔率 → Shin 概率；缺列返回 None。"""
    if odds is None:
        return None
    home, draw, away = om.shin_implied(odds)
    return home, draw, away


def collect_candidates(
    conn: sqlite3.Connection,
    *,
    competitions: tuple[str, ...],
    seasons: tuple[str, ...],
) -> tuple[list[_Candidate], dict[str, int]]:
    """十年语料 → 冷门候选列表 + 覆盖统计（按库内排序遍历，确定性）。"""
    wanted = {(c, s) for c in competitions for s in seasons}
    coverage = {
        "rows": 0,
        "out_of_scope": 0,
        "no_share_avgc": 0,  # 份额代理缺列（AvgC 无 → 彩池/固定镜都不可算）
        "stage_psc": 0,
        "stage_avgc": 0,
        "psh_present": 0,
    }
    candidates: list[_Candidate] = []
    for row in rs_store.hist_pool_replay_rows(conn):
        coverage["rows"] += 1
        if (str(row["competition"]), str(row["season"])) not in wanted:
            coverage["out_of_scope"] += 1
            continue
        avgc_odds = _odds_triple(row, "avgc")
        share = _shin_or_none(avgc_odds)
        if share is None or avgc_odds is None:
            coverage["no_share_avgc"] += 1
            continue
        psc = _shin_or_none(_odds_triple(row, "psc"))
        if psc is not None:
            fair, stage = psc, "psc"
            coverage["stage_psc"] += 1
        else:
            fair, stage = share, "avgc"
            coverage["stage_avgc"] += 1
        early = _shin_or_none(_odds_triple(row, "psh"))
        if early is not None:
            coverage["psh_present"] += 1
        ftr = str(row["ftr"])
        for position, selection in enumerate(_SELECTIONS):
            share_i = share[position]
            if share_i >= pool_store.COLD_SHARE_MAX:
                continue
            fair_i = fair[position]
            pool_ev = pool_store.parimutuel_ev(fair_i, share_i)
            if pool_ev is None:  # pragma: no cover - Shin 概率恒正
                continue
            early_fair_i = early[position] if early is not None else None
            candidates.append(
                _Candidate(
                    competition=str(row["competition"]),
                    season=str(row["season"]),
                    home_team=str(row["home_team"]),
                    selection=selection,
                    stage=stage,
                    fair=fair_i,
                    share=share_i,
                    pool_ev=pool_ev,
                    fx_ev=fair_i * avgc_odds[position] - 1.0,
                    hit=ftr == _FTR_BY_SELECTION[selection],
                    early_fair=early_fair_i,
                    early_pool_ev=(
                        pool_store.parimutuel_ev(early_fair_i, share_i)
                        if early_fair_i is not None
                        else None
                    ),
                )
            )
    return candidates, coverage


def percentiles(values: list[float]) -> dict[str, float | None]:
    """确定性分位数（排序 + 线性插值，inclusive 口径）。"""
    if not values:
        return {"p50": None, "p90": None, "p99": None, "max": None, "min": None}
    xs = sorted(values)

    def pct(q: float) -> float:
        pos = q * (len(xs) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(xs) - 1)
        frac = pos - lo
        return xs[lo] * (1 - frac) + xs[hi] * frac

    return {
        "p50": round(pct(0.50), 4),
        "p90": round(pct(0.90), 4),
        "p99": round(pct(0.99), 4),
        "max": round(xs[-1], 4),
        "min": round(xs[0], 4),
    }


def _stage_stats(cands: list[_Candidate]) -> dict[str, Any]:
    """一组（同分期）候选 → 频率/分布/阈值贴近度统计。"""
    pool_pos = [c for c in cands if c.pool_ev > 0]
    fx_pos = [c for c in cands if c.fx_ev > 0]
    ratios = [c.fair / c.share for c in cands]
    return {
        "candidates": len(cands),
        "pool_ev_positive": len(pool_pos),
        "pool_ev_positive_rate": (
            round(len(pool_pos) / len(cands), 6) if cands else None
        ),
        "fx_ev_positive": len(fx_pos),
        "fx_ev_positive_rate": round(len(fx_pos) / len(cands), 6) if cands else None,
        "pool_ev_percentiles": percentiles([c.pool_ev for c in cands]),
        "fx_ev_percentiles": percentiles([c.fx_ev for c in cands]),
        "fair_over_share_percentiles": percentiles(ratios),
        # 彩池 EV>0 等价 fair/share > 1/返奖率 ≈ 1.538——实测贴近度即结论
        "pool_breakeven_ratio": round(1.0 / pool_store.POOL_RETURN_RATE, 4),
        "mean_fair_minus_share": (
            round(sum(c.fair - c.share for c in cands) / len(cands), 6)
            if cands
            else None
        ),
    }


def _grouped(
    candidates: list[_Candidate], key_fn: Callable[[_Candidate], str]
) -> dict[str, dict[str, dict[str, Any]]]:
    """按属性分组 → 每组内再分期的统计（分期不混算）。"""
    groups: dict[str, list[_Candidate]] = {}
    for cand in candidates:
        groups.setdefault(key_fn(cand), []).append(cand)
    return {
        name: {
            stage: _stage_stats([c for c in group if c.stage == stage])
            for stage in ("psc", "avgc")
            if any(c.stage == stage for c in group)
        }
        for name, group in sorted(groups.items())
    }


def _realized_calibration(candidates: list[_Candidate]) -> dict[str, Any]:
    """实测校准：候选池命中频率 vs fair 均值 vs 份额均值（锁步的直接检验）。"""
    out: dict[str, Any] = {}
    for stage in ("psc", "avgc"):
        stage_cands = [c for c in candidates if c.stage == stage]
        if not stage_cands:
            continue
        per_selection = {}
        for selection in _SELECTIONS:
            group = [c for c in stage_cands if c.selection == selection]
            if not group:
                continue
            per_selection[selection] = {
                "n": len(group),
                "hit_rate": round(sum(c.hit for c in group) / len(group), 4),
                "mean_fair": round(sum(c.fair for c in group) / len(group), 4),
                "mean_share": round(sum(c.share for c in group) / len(group), 4),
            }
        pooled_n = len(stage_cands)
        out[stage] = {
            "per_selection": per_selection,
            "pooled": {
                "n": pooled_n,
                "hit_rate": round(sum(c.hit for c in stage_cands) / pooled_n, 4),
                "mean_fair": round(sum(c.fair for c in stage_cands) / pooled_n, 4),
                "mean_share": round(sum(c.share for c in stage_cands) / pooled_n, 4),
            },
        }
    return out


def _early_lens(candidates: list[_Candidate]) -> dict[str, Any]:
    """消失速度：早期(PSH fair)正 EV 候选到收盘的存活率与 EV 侵蚀。"""
    out: dict[str, Any] = {}
    for stage in ("psc", "avgc"):
        known: list[tuple[_Candidate, float]] = []
        for cand in candidates:
            if cand.stage == stage and cand.early_pool_ev is not None:
                known.append((cand, cand.early_pool_ev))
        if not known:
            continue
        early_pos = [pair for pair in known if pair[1] > 0]
        persisted = [pair for pair in early_pos if pair[0].pool_ev > 0]
        out[stage] = {
            "early_window_candidates": len(known),
            "early_ev_positive": len(early_pos),
            "persisted_to_close": len(persisted),
            "persist_rate": (
                round(len(persisted) / len(early_pos), 6) if early_pos else None
            ),
            "mean_early_ev_of_positive": (
                round(sum(ev for _, ev in early_pos) / len(early_pos), 4)
                if early_pos
                else None
            ),
            "mean_close_ev_of_those": (
                round(sum(pair[0].pool_ev for pair in early_pos) / len(early_pos), 4)
                if early_pos
                else None
            ),
            # 反向：收盘才转正（早期不正）——晚现窗口计数
            "late_only_positive": sum(
                1 for cand, ev in known if cand.pool_ev > 0 and ev <= 0
            ),
        }
    return out


def pool_replay_report(
    conn: sqlite3.Connection,
    *,
    competitions: tuple[str, ...],
    seasons: tuple[str, ...],
) -> dict[str, Any]:
    """
    十年冷门 EV 复验报告（纯函数：库内容 × 入参，无时间戳）。

    输出契约：params/caliber/coverage/overall/by_season/by_competition/
    realized_calibration/early_lens/degenerate_note——统计全部按
    psc/avgc 分期分列，不混算。
    """
    candidates, coverage = collect_candidates(
        conn, competitions=competitions, seasons=seasons
    )
    return {
        "params": {
            "cold_share_max": pool_store.COLD_SHARE_MAX,
            "return_rate": pool_store.POOL_RETURN_RATE,
            "competitions": list(competitions),
            "seasons": list(seasons),
        },
        "caliber": CALIBER_TEXT,
        "coverage": coverage,
        "overall": {
            stage: _stage_stats([c for c in candidates if c.stage == stage])
            for stage in ("psc", "avgc")
            if any(c.stage == stage for c in candidates)
        },
        "by_season": _grouped(candidates, lambda c: c.season),
        "by_competition": _grouped(candidates, lambda c: c.competition),
        "realized_calibration": _realized_calibration(candidates),
        "early_lens": _early_lens(candidates),
        "degenerate_note": DEGENERATE_NOTE,
    }
