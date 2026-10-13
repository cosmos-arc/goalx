"""
CLV 十年仪表盘（backtest-decade 票 20，spec S20）。

下注价 vs 收盘价的系统性蒸发/增值分解，分母=十年 run 的全部腿：

- **结构性校验面**：模拟价=(1/p_mkt)×(1−haircut) 由收盘构造——had 腿
  sim_odds×fair_p（引擎同链公平概率）应 ≡ 1−haircut，全量实证；hhad/ttg
  隐含 p=(1−haircut)/odds 给分布合理性（带内计数），构造恒等式如实声明。
- **早锁价值面**（真蒸发/增值问题）：模型选中 selection 的**同族**早晚
  价对——era1=PSH（开盘）→PSC（收盘代理，fdhist 族）；era2=q24h/q1h
  1x2 锚切片→close1x2 锚（cid177 族）。比例 devig 两端同法；口径沿
  clv.py：clv=p_close−p_early，beat=clv>0（市场向选中侧前移=模型领先，
  早入场价更优）。had 腿专属（hhad/ttg 的选择是联合矩阵派生，无同族
  早价可比——覆盖缺口如实计数，不硬凑）。
- **线漂移描述面**：被下注场次 vs 全预测场次的 AH/OU 线漂移对照
  （市场在模型选中的场次是否移线更多）。

观察性报告：不回写引擎参数（haircut 已裁维持）；era1 PSC≠轨迹末价的
已知局限显著标注。报告落语料树 reports/ MD+JSON。
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from statistics import fmean, median
from typing import Any

import duckdb

from goalx_backend.data import gold_reader
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.evaluation.backtest import MIN_MARKET_PROB
from goalx_backend.evaluation.metrics import load_backtest_run

REPORT_BASENAME = "clv-decade"
MIN_EARLY_SAMPLES = 30  # 早锁面低于此 n 只计数不下结论
BEAT_THRESHOLD = 0.5  # beat 率过半=市场系统向选中侧前移
ODDS_SANITY = 1.0  # devig 前提：三向赔率均 >1
# 结构性恒等式容差（腿价由浮点构造，非逐位比较）
STRUCT_TOL = 0.005

# 1x2 族列后缀：era2 切片/锚用字母，fdhist 族用全名
_LETTER = {"h": "h", "d": "d", "a": "a"}
_FULL = {"h": "home", "d": "draw", "a": "away"}


@dataclass
class _StructFace:
    """一个玩法的结构性面累计。"""

    n: int = 0
    had_fair_products: list[float] = field(default_factory=list)  # odds×fair_p
    implied_probs: list[float] = field(default_factory=list)  # (1−h)/odds

    def as_dict(self, haircut: float) -> dict[str, Any]:
        """had：恒等式偏离计数；全玩法：隐含概率分布带内计数。"""
        face: dict[str, Any] = {"n": self.n}
        if self.had_fair_products:
            band = 1.0 - haircut
            outside = [v for v in self.had_fair_products if abs(v - band) > STRUCT_TOL]
            face |= {
                "identity_median": round(median(self.had_fair_products), 6),
                "identity_expected": round(band, 6),
                "identity_outside": len(outside),
            }
        if self.implied_probs:
            # 法定下界=护栏倒数恒等式：odds≤1/MIN ⇔ implied=(1−h)/odds≥(1−h)×MIN
            legal_floor = (1.0 - haircut) * MIN_MARKET_PROB
            face |= {
                "implied_median": round(median(self.implied_probs), 4),
                "implied_min": round(min(self.implied_probs), 4),
                "implied_max": round(max(self.implied_probs), 4),
                "implied_out_of_band": sum(
                    1 for p in self.implied_probs if p < legal_floor or p > 1.0
                ),
            }
        return face


@dataclass
class _EarlyFace:
    """一个 (早参考,era) 的早锁面累计（clv=p_close−p_early，beat=clv>0）。"""

    n: int = 0
    beats: int = 0
    clvs: list[float] = field(default_factory=list)

    def add(self, clv: float) -> None:
        self.n += 1
        self.beats += int(clv > 0)
        self.clvs.append(clv)

    def as_dict(self) -> dict[str, Any]:
        """早锁面行：beat 率 + 概率位移（中位/均值）。"""
        return {
            "n": self.n,
            "beat_rate": round(self.beats / self.n, 4) if self.n else None,
            "clv_mean": round(fmean(self.clvs), 6) if self.clvs else None,
            "clv_median": round(median(self.clvs), 6) if self.clvs else None,
        }


def _prop_devig(odds: tuple[float, float, float]) -> tuple[float, float, float] | None:
    """三向比例 devig（两端同法）；任一赔率 ≤1 或非有限 → None。"""
    if any(not math.isfinite(o) or o <= ODDS_SANITY for o in odds):
        return None
    inv = [1.0 / o for o in odds]
    total = sum(inv)
    if not math.isfinite(total) or total <= 0:
        return None
    p_h, p_d, p_a = (i / total for i in inv)
    return p_h, p_d, p_a


def _probs_of(row: dict[str, Any], family: str) -> tuple[float, float, float] | None:
    """一个 1x2 族的 devig 概率（族内任一列缺失 → 整族不可用）。"""
    suffixes = _LETTER if family in ("q24h1x2", "q1h1x2", "close1x2") else _FULL
    odds: list[float] = []
    for sfx in suffixes.values():
        value = row.get(f"{family}_{sfx}")
        if value is None:
            return None
        odds.append(float(value))
    return _prop_devig((odds[0], odds[1], odds[2]))


def _early_pairs(
    gold: dict[str, dict[str, Any]],
) -> dict[tuple[str, str, str], tuple[float, float]]:
    """
    每场可用的 (sid, 早参考, selection) → (p_early, p_close) 同族对。

    早参考族内自比对：q24h/q1h 切片对 close1x2 锚（cid177 族）；
    psh_open 对 psc（fdhist 族）。同一切片族多个可用时各成对（面按
    参考分列，不合并口径）。
    """
    pairs: dict[tuple[str, str, str], tuple[float, float]] = {}
    for sid, row in gold.items():
        for ref, early_family, close_family in (
            ("q24h", "q24h1x2", "close1x2"),
            ("q1h", "q1h1x2", "close1x2"),
            ("psh_open", "psh", "psc"),
        ):
            early = _probs_of(row, early_family)
            close = _probs_of(row, close_family)
            if early is None or close is None:
                continue
            for sel, p_e, p_c in zip(("h", "d", "a"), early, close, strict=True):
                pairs[(sid, ref, sel)] = (p_e, p_c)
    return pairs


def _drift_stats(values: list[float]) -> dict[str, Any]:
    """线漂移描述统计（n/均值/中位数/绝对值中位数）。"""
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(fmean(values), 4),
        "median": round(median(values), 4),
        "abs_median": round(median([abs(v) for v in values]), 4),
    }


def _structural_face(
    legs: list[dict[str, Any]],
    fair_of: dict[str, dict[str, float]],
    haircut: float,
) -> dict[str, _StructFace]:
    """结构性校验面（全玩法；had 恒等式=odds×fair_p ≡ 1−haircut）。"""
    struct: dict[str, _StructFace] = defaultdict(_StructFace)
    for leg in legs:
        market = str(leg["market_code"])
        odds = float(leg["odds"])
        face = struct[market]
        face.n += 1
        face.implied_probs.append((1.0 - haircut) / odds)
        if market == "had":
            fair = fair_of.get(str(leg["match_key"]))
            sel = str(leg["selection_code"])
            if fair is not None and fair.get(sel) is not None:
                face.had_fair_products.append(odds * float(fair[sel]))
    return struct


def _early_lock_face(
    legs: list[dict[str, Any]],
    gold: dict[str, dict[str, Any]],
    pairs: dict[tuple[str, str, str], tuple[float, float]],
) -> tuple[dict[tuple[str, str], _EarlyFace], dict[str, dict[str, int]], int]:
    """早锁价值面（had 腿，同 (sid,sel) 去重）+ 覆盖计数；返回 (面, 覆盖, 选样数)。"""
    early: dict[tuple[str, str], _EarlyFace] = defaultdict(_EarlyFace)
    coverage: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    seen_pick: set[tuple[str, str]] = set()
    for leg in legs:
        sid = str(leg["match_key"])
        market = str(leg["market_code"])
        sel = str(leg["selection_code"])
        era = str(gold[sid]["era"]) if sid in gold else "no_gold"
        coverage[f"{market}:{era}"]["legs"] += 1
        if market != "had":
            coverage[f"{market}:{era}"]["early_n_a"] += 1  # 无同族早价可比
            continue
        if (sid, sel) in seen_pick:
            continue
        seen_pick.add((sid, sel))
        for ref in ("q24h", "q1h", "psh_open"):
            pair = pairs.get((sid, ref, sel))
            if pair is None:
                continue
            p_early, p_close = pair
            early[(ref, era)].add(p_close - p_early)
            coverage[f"{market}:{era}"][f"ref:{ref}"] += 1
    return early, coverage, len(seen_pick)


def _drift_face(
    gold: dict[str, dict[str, Any]], picked: set[str], universe: set[str]
) -> dict[str, dict[str, Any]]:
    """线漂移描述面（被下注 vs 全预测场次）。"""
    cols = ("ah_line_drift", "ah_anchor_drift", "ou_line_drift")

    def stats_of(sids: set[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for col in cols:
            values = [
                float(row[col])
                for sid, row in gold.items()
                if sid in sids and row.get(col) is not None
            ]
            out[col] = _drift_stats(values)
        return out

    return {"picked": stats_of(picked), "all": stats_of(universe)}


def build_clv_decade_report(
    conn: sqlite3.Connection,
    duck_con: duckdb.DuckDBPyConnection,
    store: CorpusStore,
    *,
    run_id: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """CLV 十年仪表盘：结构校验 + 早锁价值 + 线漂移 → reports/ 双视图。"""
    rid, label, params, run_status = load_backtest_run(conn, run_id)
    haircut = float(params.get("haircut", 0.10))
    pred_rows = conn.execute(
        "SELECT match_key, era, fair_probs FROM backtest_predictions WHERE run_id = ?",
        (rid,),
    ).fetchall()
    fair_of = {str(r["match_key"]): json.loads(str(r["fair_probs"])) for r in pred_rows}
    bet_rows = conn.execute(
        "SELECT legs FROM backtest_bets WHERE run_id = ?", (rid,)
    ).fetchall()
    legs = [leg for bet in bet_rows for leg in json.loads(str(bet["legs"]))]

    sids = sorted({str(leg["match_key"]) for leg in legs} | set(fair_of))
    gold = gold_reader.fetch_clv_rows_by_sids(duck_con, sids)
    picked_sids = {str(leg["match_key"]) for leg in legs}

    struct = _structural_face(legs, fair_of, haircut)
    early, coverage, n_picks = _early_lock_face(legs, gold, _early_pairs(gold))

    payload = {
        "built_at": (today or date.today()).isoformat(),
        "run_id": rid,
        "label": label,
        "run_status": run_status,
        "haircut": haircut,
        "n_predictions": len(pred_rows),
        "n_legs": len(legs),
        "n_unique_had_picks": n_picks,
        "structural": {
            market: face.as_dict(haircut) for market, face in sorted(struct.items())
        },
        "early_lock": {
            f"{ref}:{era}": face.as_dict() for (ref, era), face in sorted(early.items())
        },
        "coverage": {key: dict(c) for key, c in sorted(coverage.items())},
        "drift": _drift_face(gold, picked_sids, set(fair_of)),
        "note": (
            "hhad/ttg 选择=联合矩阵派生，无同族早价可比（early 面覆盖缺口）；"
            + "era1 PSC 为收盘代理（PSC≠轨迹末价）；观察性报告不回写引擎参数"
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


def _render_md(payload: dict[str, Any]) -> str:
    """人读视图：结论句 + 三面表。"""
    status_note = (
        ""
        if payload["run_status"] == "done"
        else f"\n\n> ⚠ run 状态={payload['run_status']}：中途检视，不代表全量"
    )
    lines = [
        "# CLV 十年仪表盘（票 20）",
        "",
        f"run={payload['run_id']}（{payload['label']}）"
        + f" haircut={payload['haircut']}"
        + f" 腿 {payload['n_legs']}（had 独立选样 {payload['n_unique_had_picks']}）"
        + status_note,
        "",
        "## 结构性校验（sim 价由收盘构造）",
        "",
        "| 玩法 | n | 恒等式中位(期望) | 恒等式偏离 | 隐含p中位 | 带外 |",
        "|---|---|---|---|---|---|",
    ]
    for market, face in payload["structural"].items():
        identity = face.get("identity_median")
        expected = face.get("identity_expected")
        identity_cell = (
            f"{identity}({expected})" if identity is not None else "—(构造恒等)"
        )
        lines.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                market,
                face["n"],
                identity_cell,
                face.get("identity_outside", "—"),
                face.get("implied_median", "—"),
                face.get("implied_out_of_band", "—"),
            )
        )
    lines += [
        "",
        "## 早锁价值（had 选样：同族早晚价对；beat=市场向选中侧前移）",
        "",
        "| 参考×era | n | beat 率 | clv 中位 | clv 均值 |",
        "|---|---|---|---|---|",
    ]
    for scope, face in payload["early_lock"].items():
        beat = face["beat_rate"]
        verdict = ""
        if face["n"] >= MIN_EARLY_SAMPLES and beat is not None:
            verdict = "（模型领先）" if beat > BEAT_THRESHOLD else "（跟随/噪声）"
        lines.append(
            "| {} | {} | {} {} | {} | {} |".format(
                scope,
                face["n"],
                beat,
                verdict,
                face["clv_median"],
                face["clv_mean"],
            )
        )
    lines += [
        "",
        "## 线漂移（被下注场次 vs 全预测场次，描述面）",
        "",
        "| 列 | 组 | n | mean | median | |drift| 中位 |",
        "|---|---|---|---|---|---|",
    ]
    for group in ("picked", "all"):
        for col, stats in payload["drift"][group].items():
            if stats["n"] == 0:
                continue
            lines.append(
                "| {} | {} | {} | {} | {} | {} |".format(
                    col,
                    group,
                    stats["n"],
                    stats["mean"],
                    stats["median"],
                    stats["abs_median"],
                )
            )
    lines += [
        "",
        "- 判读注意：" + payload["note"],
        "- 完整覆盖计数（玩法×era×参考）见 clv-decade.json。",
    ]
    return "\n".join(lines) + "\n"
