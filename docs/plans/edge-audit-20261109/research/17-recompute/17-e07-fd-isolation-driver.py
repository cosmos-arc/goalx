#!/usr/bin/env python3
"""票 17：E-07 FD 断点隔离重算（2425 截止）驱动（edge-audit-20261109）。

四实际暴露面（票 15 research §三 A2 + 账本 E-07 审计更正行③）：
  1. F7 hhad 结构面（fd_avgc_ah 线+水位 = FD 列，整数线全窗落账）；
  2. F8 ttg 篮子（确认窗含 2526 段；锚=srct:ou 梯，票 12 NN 语义）；
  3. had 结构面（avgc = FD 列；联赛族 mean gap，含确认窗 2526 行）；
  4. Pin 松/紧切片（pin_dev = psc−avgc 双 FD 列；分类语义在 2526 行受害）。

臂：FULL=现树全窗（修后脚本语义，PR #286）vs CUT=2425 截止（剔 season
y0>=2025 整季；断点 2025-07-23 落秋春制休赛期=整季剔除无季内劈裂，票 15
§二）。FULL−CUT=2526 段贡献。

验证链（先证复现再谈差异）：FULL 复现冻结台账 09-em1-trial-ledger.csv 的
F1/F2/F6 搜索窗行与 F7 搜索/验证窗行；F7 确认窗与 F8 主行对票 12 NN 存档值
（树自然增长只解释行数增量）。全程冻结真树只读，不触试验账（零追加）。

用法：cd /Users/chevy/Desktop/code/goalx && uv run --no-sync python \
  .scratch/edge-audit-20261109/research/17-recompute/17-e07-fd-isolation-driver.py
"""

from __future__ import annotations

import csv
import datetime as dt
import sys
from collections import defaultdict

import numpy as np

REPORTS = "/Users/chevy/Desktop/code/goalx/docs/plans/autoresearch-20261107/reports"
sys.path.insert(0, REPORTS)

from _lib import (  # noqa: E402
    COARSE_LABELS,
    coarse_idx,
    clustered_mean,
    devig,
    load_jc,
    load_euro,
    load_ou_ladder,
    pair_fixtures,
    ttg_basket_ev,
)

LEDGER = f"{REPORTS}/09-em1-trial-ledger.csv"
BREAKPOINT = dt.datetime(2025, 7, 23)  # FD 官方警示（票 15 §一逐字）
CUT_YEAR = 2025  # 2425 截止：y0>=2025（2526/2627 季）整季剔除

# 票 12 存档 NN 全窗主行（research/12-f8-recompute-driver-output.txt 第 4 列，
# 2026-11-09 树；本驱动 FULL 的对照基准）
T12_NN = {
    ("search", "over"): {"gap": 0.0194, "ev": -1.0000, "z": float("nan"), "n_sel": 2},
    ("search", "under"): {"gap": -0.0194, "ev": -0.2410, "z": -1.394, "n_sel": 61},
    ("validation", "over"): {"gap": 0.0214, "ev": -1.0000, "z": float("nan"), "n_sel": 4},
    ("validation", "under"): {"gap": -0.0214, "ev": -0.3418, "z": -2.819, "n_sel": 53},
    ("confirmation", "over"): {"gap": 0.0270, "ev": -0.1000, "z": -0.169, "n_sel": 8},
    ("confirmation", "under"): {"gap": -0.0270, "ev": -0.2463, "z": -1.789, "n_sel": 86},
}


def window_of(season: str | None) -> str | None:
    if not season or len(season) < 4:
        return None
    y0 = int(season[:4])
    if y0 <= 2021:
        return "search"
    if y0 in (2022, 2023):
        return "validation"
    if y0 in (2024, 2025):
        return "confirmation"
    return None  # 2026-27 进行中季不入窗


def fmt(v: float, pct: bool = False, nd: int = 4) -> str:
    if v is None or not np.isfinite(v):
        return "nan"
    return f"{v:+.{nd}{'%' if pct else 'f'}}"


def close2(a: float, b: float, tol: float) -> bool:
    """nan 感知比较：双 nan 视为一致（nan±nan 的 abs 比较恒 False 是伪 mismatch）。"""
    if not np.isfinite(a) and not np.isfinite(b):
        return True
    if np.isfinite(a) and np.isfinite(b):
        return abs(a - b) < tol
    return False


def gap_metrics(rows: list[dict], key: str = "gap") -> dict:
    """clustered mean（按 fx 聚类）→ {n, mean, se, z}。"""
    m, se, n = clustered_mean([(r["fx"], r[key]) for r in rows])
    return {"n": n, "mean": m, "se": se, "z": (m / se if se and se > 0 and n >= 2 else float("nan"))}


def main() -> None:
    jc_rows = load_jc()
    eu_rows = load_euro()
    pairs, pstats = pair_fixtures(jc_rows, eu_rows)
    print(f"配对漏斗: {pstats}")

    in_win: list[tuple[int, dict, dict]] = []
    for fx, (jc, eu) in enumerate(pairs):
        w = window_of(eu["season"])
        if w is None:
            continue
        in_win.append((fx, jc, eu, int(eu["season"][:4])))

    # ---- 0. 断点边界审计（整季剔除的合法性 + 2526 段规模）
    print("\n== 0. 断点边界审计（in-window 配对行；断点=2025-07-23） ==")
    by_y0: dict[int, list[tuple[int, dict, dict]]] = defaultdict(list)
    for fx, jc, eu, y0 in in_win:
        by_y0[y0].append((fx, jc, eu))
    for y0 in sorted(by_y0):
        grp = by_y0[y0]
        kos = [eu["kickoff"] for _f, _j, eu in grp]
        n_after = sum(1 for k in kos if k > BREAKPOINT)
        print(
            f"  y0={y0} ({grp[0][2]['season']}): n={len(grp)} kickoff[{min(kos):%Y-%m-%d}..{max(kos):%Y-%m-%d}]"
            f" 断点后={n_after}"
        )
    n_conf_after = sum(
        1 for _f, _j, eu, _y in in_win if window_of(eu["season"]) == "confirmation" and eu["kickoff"] > BREAKPOINT
    )
    seg = [t for t in in_win if t[3] >= CUT_YEAR]
    print(
        f"  CUT 剔除 y0>=2025 共 {len(seg)}/{len(in_win)} 场；确认窗内断点后(>2025-07-23)总 {n_conf_after} 场"
        "（配对集内 2425 季 kickoff 全部 ≤2025-05-26 → 整季剔除=精确断点对齐，无季内劈裂）"
    )

    # ---- 1. had 行（avgc 主锚；含 pin_dev）——09 脚本语义逐行复刻
    had: list[dict] = []
    for fx, jc, eu, y0 in in_win:
        w = window_of(eu["season"])
        fairs = {"avgc": devig(eu["avgc"]), "psc": devig(eu["psc"])}
        if fairs["avgc"] is None:
            continue
        hg, ag = eu["hg"], eu["ag"]
        hit = (1.0 if hg > ag else 0.0, 1.0 if hg == ag else 0.0, 1.0 if hg < ag else 0.0)
        pin_dev = None
        if fairs["psc"] is not None:
            pin_dev = fairs["psc"][0] - fairs["avgc"][0]
        for k in range(3):
            had.append({
                "fx": fx, "window": w, "y0": y0, "league": eu["league"], "k": k,
                "hit": hit[k], "jc_odds": jc["jc"][k], "fair": fairs["avgc"][k],
                "gap": 1.0 / jc["jc"][k] - fairs["avgc"][k], "pin_dev": pin_dev,
            })
    n_had_frozen = 32889
    print(f"\n== 1. had 分析行（avgc）= {len(had)}（冻结台账 2026-11-08 为 {n_had_frozen}；树自然增长只入确认窗） ==")
    for w in ("search", "validation", "confirmation"):
        print(f"  {w}: {sum(1 for r in had if r['window'] == w)}")

    # pin_dev 三分位（搜索窗冻结，防泄漏；与 09 脚本同法）
    svals = sorted(r["pin_dev"] for r in had if r["window"] == "search" and r["pin_dev"] is not None)
    q33, q66 = float(np.percentile(svals, 33)), float(np.percentile(svals, 66))
    for r in had:
        if r["pin_dev"] is None:
            r["pin3"] = "缺psc"
        else:
            r["pin3"] = "Pin松" if r["pin_dev"] < q33 else ("Pin紧" if r["pin_dev"] > q66 else "≈")
    print(f"  pin_dev 三分位切点（搜索窗冻结，与 09 冻结值 −0.0022/+0.0039 同源）：{q33:+.4f} / {q66:+.4f}")

    # ---- 2. Phase A：FULL 复现冻结台账（F1/F2/F6 搜索窗 + F7 全窗）
    def slice_metrics(sub: list[dict]) -> dict:
        n_fx = len({r["fx"] for r in sub})
        gm, gs, _ = clustered_mean([(r["fx"], r["gap"]) for r in sub])
        ev_rows = [(r["fx"], r["jc_odds"] * r["hit"] - 1.0) for r in sub if r["jc_odds"] * r["fair"] > 1.0]
        em, es, _ = clustered_mean(ev_rows)
        return {
            "n_fx": n_fx, "n_sel": len(ev_rows), "gap": gm, "ev": em,
            "z": (em / es if es and es > 0 and len(ev_rows) >= 2 else float("nan")),
        }

    frozen: dict[tuple[str, str, str], dict] = {}
    with open(LEDGER) as f:
        for row in csv.DictReader(f):
            if row["ticket"] != "09":
                continue
            ev_gap = row["value"].split("/")
            ev = float(ev_gap[0]) if len(ev_gap) == 2 else float("nan")
            gap = float(ev_gap[1]) if len(ev_gap) == 2 else float(ev_gap[0])
            frozen[(row["family"], row["slice"], row["window"])] = {
                "n_fx": int(row["n_fx"]), "n_sel": int(row["n_sel"]), "ev": ev, "gap": gap,
                "z": float(row["z"]) if row["z"] else float("nan"),
            }

    search_rows = [r for r in had if r["window"] == "search"]
    f1 = [(lg, [r for r in search_rows if r["league"] == lg]) for lg in sorted({r["league"] for r in search_rows})]
    f2 = [
        (f"{bl}×{kcn}", [r for r in search_rows if coarse_idx(r["fair"]) == bi and r["k"] == k])
        for bi, bl in enumerate(COARSE_LABELS) for k, kcn in ((0, "主"), (1, "平"), (2, "客"))
    ]
    f6 = [
        (f"{v}×{bl}", [r for r in search_rows if r["pin3"] == v and coarse_idx(r["fair"]) == bi])
        for bi, bl in enumerate(COARSE_LABELS) for v in ("Pin松", "Pin紧", "≈", "缺psc")
    ]
    mism_a = 0
    checked = 0
    for fam, slices in (("F1", f1), ("F2", f2), ("F6", f6)):
        for sname, sub in slices:
            key = (fam, sname, "search")
            if key not in frozen:
                continue
            checked += 1
            m, fr = slice_metrics(sub), frozen[key]
            ok = (
                m["n_fx"] == fr["n_fx"] and m["n_sel"] == fr["n_sel"]
                and close2(m["gap"], fr["gap"], 6e-5) and close2(m["ev"], fr["ev"], 6e-5)
                and close2(m["z"], fr["z"], 5e-4)
            )
            if not ok:
                mism_a += 1
                print(f"  MISMATCH {key}: 冻结(n{fr['n_fx']}/sel{fr['n_sel']}/ev{fr['ev']:+.4f}/gap{fr['gap']:+.4f}/z{fr['z']:+.3f})"
                      f" vs 现树({m['n_fx']}/{m['n_sel']}/{m['ev']:+.4f}/{m['gap']:+.4f}/{m['z']:+.3f})")
    print(f"\n== 2. Phase A-1：F1/F2/F6 搜索窗 vs 冻结台账 mismatch={mism_a}/{checked}（应 0：搜索窗=断点前，无树增长） ==")

    # ---- 3. F7 hhad 结构面（09 脚本逐行复刻）+ FULL/CUT
    n_line_mismatch = 0
    hh: list[dict] = []
    for fx, jc, eu, y0 in in_win:
        w = window_of(eu["season"])
        if w is None or jc["hhad"] is None:
            continue
        jl, jh, jd, ja = jc["hhad"]
        if not (eu["ahh"] and eu["aha"] and eu["ah_line"] and eu["ahh"] > 1 and eu["aha"] > 1):
            continue
        if abs(jl - eu["ah_line"]) > 1e-9:
            n_line_mismatch += 1
            continue
        f3 = devig((jh, jd, ja))
        if f3 is None:
            continue
        ph_c = f3[0] / (f3[0] + f3[2])
        pa_c = f3[2] / (f3[0] + f3[2])
        e0, e1 = 1.0 / eu["ahh"], 1.0 / eu["aha"]
        e_ph = e0 / (e0 + e1)
        lg = "≤-2" if jl <= -2 else ("-1" if jl == -1 else ("+1" if jl == 1 else "≥+2"))
        hh.append({"fx": fx, "window": w, "y0": y0, "lg": lg, "side": "让方", "gap": ph_c - e_ph})
        hh.append({"fx": fx, "window": w, "y0": y0, "lg": lg, "side": "受让方", "gap": pa_c - (1 - e_ph)})
    print(f"\n== 3. F7 hhad 结构面：线不匹配/无AH收盘 {n_line_mismatch} 场不入；结构行 {len(hh)}（冻结 2,406） ==")

    f7_res: dict[str, dict[tuple[str, str], dict]] = {}
    for arm in ("FULL", "CUT"):
        rows_a = hh if arm == "FULL" else [r for r in hh if r["y0"] < CUT_YEAR]
        res: dict[tuple[str, str], dict] = {}
        for w in ("search", "validation", "confirmation"):
            for lg in ("≤-2", "-1", "+1", "≥+2"):
                for side in ("让方", "受让方"):
                    sub = [r for r in rows_a if r["window"] == w and r["lg"] == lg and r["side"] == side]
                    if sub:
                        res[(w, f"{lg}×{side}")] = gap_metrics(sub)
        f7_res[arm] = res
    # Phase A-2：F7 vs 冻结台账
    mism_f7 = 0
    for key, fr in sorted(frozen.items()):
        if key[0] != "F7":
            continue
        m = f7_res["FULL"].get((key[2], key[1]))
        if m is None:
            mism_f7 += 1
            continue
        ok = m["n"] == fr["n_fx"] and abs(m["mean"] - fr["gap"]) < 6e-5 and (
            np.isfinite(fr["z"]) and abs(m["z"] - fr["z"]) < 5e-4
            or not np.isfinite(fr["z"]) and not np.isfinite(m["z"])
        )
        if not ok:
            mism_f7 += 1
            print(f"  F7 MISMATCH {key}: 冻结(n{fr['n_fx']}/gap{fr['gap']:+.4f}/z{fr['z']:+.3f})"
                  f" vs FULL({m['n']}/{m['mean']:+.4f}/{m['z']:+.3f})")
    print(f"  Phase A-2：F7 全窗 vs 冻结台账 mismatch={mism_f7}/24（搜索/验证窗应 0；确认窗允许树增长型漂移）")
    print("\n  F7 读数（gap=JC 隐含−FD AH 锚；z=聚类 SE）：n/gap FULL → CUT（Δ=2526 段贡献）")
    for w in ("search", "validation", "confirmation"):
        print(f"  -- {w}")
        for key in sorted(f7_res["FULL"]):
            if key[0] != w:
                continue
            mf, mc = f7_res["FULL"][key], f7_res["CUT"].get(key, {"n": 0, "mean": float("nan"), "z": float("nan")})
            print(
                f"    {key[1]:>8}: n {mf['n']:>4}→{mc['n']:<4} gap {fmt(mf['mean'])}→{fmt(mc['mean'])}"
                f" (Δ{fmt(mc['mean'] - mf['mean'])}) z {fmt(mf['z'], nd=3)}→{fmt(mc['z'], nd=3)}"
            )
    # 2526 段自身读数
    print("  -- 2526 段自身（y0>=2025，即被剔段）")
    seg_hh = [r for r in hh if r["y0"] >= CUT_YEAR]
    for lg in ("≤-2", "-1", "+1", "≥+2"):
        for side in ("让方", "受让方"):
            sub = [r for r in seg_hh if r["lg"] == lg and r["side"] == side]
            if sub:
                m = gap_metrics(sub)
                print(f"    {lg}×{side}: n {m['n']:>3} gap {fmt(m['mean'])} z {fmt(m['z'], nd=3)}")

    # ---- 4. had 结构面：联赛族 mean gap（FULL vs CUT）+ 粗桶结构
    print("\n== 4. had 结构面（avgc=FD 列）：联赛族 mean gap（每场=三向 gap 均值；Δ=2526 段贡献） ==")
    fx_gap: dict[tuple[str, int, str], float] = {}
    cnt: dict[tuple[str, int, str], int] = defaultdict(int)
    for r in had:
        key = (r["window"], r["fx"], r["league"])
        fx_gap[key] = fx_gap.get(key, 0.0) + r["gap"]
        cnt[key] += 1
    had_fx = [
        {"window": w, "fx": fx, "league": lg, "y0": y0, "gap": fx_gap[(w, fx, lg)] / cnt[(w, fx, lg)]}
        for (w, fx, lg), y0 in {(r["window"], r["fx"], r["league"]): r["y0"] for r in had}.items()
    ]
    for w in ("search", "validation", "confirmation"):
        rows_w = [r for r in had_fx if r["window"] == w]
        rows_c = [r for r in rows_w if r["y0"] < CUT_YEAR]
        rows_s = [r for r in rows_w if r["y0"] >= CUT_YEAR]
        print(f"  -- {w}: 场次 {len(rows_w)}（CUT {len(rows_c)} / 2526 段 {len(rows_s)}）")
        for lg in sorted({r["league"] for r in rows_w}):
            sub_f = [r for r in rows_w if r["league"] == lg]
            sub_c = [r for r in rows_c if r["league"] == lg]
            mf = gap_metrics(sub_f)
            mc = gap_metrics(sub_c) if sub_c else {"n": 0, "mean": float("nan"), "z": float("nan")}
            print(
                f"    {lg}: n {mf['n']:>5}→{mc['n']:<5} gap {fmt(mf['mean'])}→{fmt(mc['mean'])}"
                f" (Δ{fmt(mc['mean'] - mf['mean'])}) z {fmt(mf['z'], nd=2)}→{fmt(mc['z'], nd=2)}"
            )
        mf, mc = gap_metrics(rows_w), gap_metrics(rows_c)
        print(f"    [全联赛]: n {mf['n']:>5}→{mc['n']:<5} gap {fmt(mf['mean'])}→{fmt(mc['mean'])} (Δ{fmt(mc['mean'] - mf['mean'])})")
    print("  -- 粗桶结构（确认窗；行=gap，对照报告引用的搜索窗 长赔带 +1.7~1.8pp / 60%+ +9.9pp）")
    conf = [r for r in had if r["window"] == "confirmation"]
    for bi, bl in enumerate(COARSE_LABELS):
        sub = [r for r in conf if coarse_idx(r["fair"]) == bi]
        sub_c = [r for r in sub if r["y0"] < CUT_YEAR]
        sub_s = [r for r in sub if r["y0"] >= CUT_YEAR]
        mf, mc = gap_metrics(sub), gap_metrics(sub_c)
        ms = gap_metrics(sub_s) if sub_s else {"n": 0, "mean": float("nan")}
        print(
            f"    {bl}: n {mf['n']:>6}→{mc['n']:<6} gap {fmt(mf['mean'])}→{fmt(mc['mean'])}"
            f" (Δ{fmt(mc['mean'] - mf['mean'])}) | 2526 段 n{ms['n']} gap {fmt(ms['mean'])}"
        )
    # 确认窗整体 margin 按季（2526 段自身 vs 2425）
    for y0 in (2024, 2025):
        sub = [r for r in had_fx if r["window"] == "confirmation" and r["y0"] == y0]
        m = gap_metrics(sub)
        print(f"    [margin by season] y0={y0}: n {m['n']} 每场三向均值 gap {fmt(m['mean'])}")

    # ---- 5. Pin 松/紧切片：确认窗 2526 分类语义 + FULL/CUT 结构
    print("\n== 5. Pin 松/紧切片（pin_dev=psc−avgc，双 FD 列；切点搜索窗冻结=断点前） ==")
    for w in ("search", "validation", "confirmation"):
        rows_w = [r for r in had if r["window"] == w and r["pin_dev"] is not None]
        by_y0w: dict[int, list[float]] = defaultdict(list)
        for r in rows_w:
            by_y0w[r["y0"]].append(r["pin_dev"])
        for y0 in sorted(by_y0w):
            arr = np.array(by_y0w[y0])
            print(f"  {w} y0={y0}: pin_dev n={len(arr)} mean={arr.mean():+.5f} p50={np.percentile(arr,50):+.5f} p05={np.percentile(arr,5):+.5f} p95={np.percentile(arr,95):+.5f}")
    conf_y = defaultdict(list)
    for r in had:
        if r["window"] == "confirmation":
            conf_y[r["y0"]].append(r)
    print("  确认窗分类份额（切点 q33/q66 冻结）与 psc 缺失：")
    for y0 in sorted(conf_y):
        rows_y = conf_y[y0]
        n = len(rows_y)
        shares = {v: sum(1 for r in rows_y if r["pin3"] == v) / n for v in ("Pin松", "≈", "Pin紧", "缺psc")}
        print(
            f"    y0={y0}: n={n} 松{shares['Pin松']:.1%} ≈{shares['≈']:.1%} 紧{shares['Pin紧']:.1%}"
            f" 缺psc{shares['缺psc']:.1%}"
        )
    print("  确认窗 pin3 结构 gap（行=每场三向均值；FULL → CUT）：")
    fx_pin: dict[tuple[int, str], tuple[float, int, int]] = {}
    for r in had:
        if r["window"] != "confirmation":
            continue
        key = (r["fx"], r["pin3"])
        g, c, y = fx_pin.get(key, (0.0, 0, r["y0"]))
        fx_pin[key] = (g + r["gap"], c + 1, y)
    pin_fx = [{"fx": fx, "pin3": p, "y0": y, "gap": g / c} for (fx, p), (g, c, y) in fx_pin.items()]
    for v in ("Pin松", "≈", "Pin紧", "缺psc"):
        sub = [r for r in pin_fx if r["pin3"] == v]
        sub_c = [r for r in sub if r["y0"] < CUT_YEAR]
        mf = gap_metrics(sub) if sub else {"n": 0, "mean": float("nan"), "z": float("nan")}
        mc = gap_metrics(sub_c) if sub_c else {"n": 0, "mean": float("nan"), "z": float("nan")}
        print(f"    {v}: n {mf['n']:>5}→{mc['n']:<5} gap {fmt(mf['mean'])}→{fmt(mc['mean'])} (Δ{fmt(mc['mean'] - mf['mean'])})")
    print("  确认窗 F6 形结构（pin3 × 粗桶 gap；FULL → CUT；Δ=2526 分类语义受害段贡献）：")
    for bi, bl in enumerate(COARSE_LABELS):
        for v in ("Pin松", "Pin紧", "≈"):
            sub_f = [r for r in had if r["window"] == "confirmation" and r["pin3"] == v and coarse_idx(r["fair"]) == bi]
            sub_c = [r for r in sub_f if r["y0"] < CUT_YEAR]
            mf, mc = gap_metrics(sub_f), gap_metrics(sub_c) if sub_c else {"n": 0, "mean": float("nan")}
            if mf["n"]:
                print(
                    f"    {v}×{bl}: n {mf['n']:>5}→{mc['n']:<5} gap {fmt(mf['mean'])}→{fmt(mc['mean'])}"
                    f" (Δ{fmt(mc['mean'] - mf['mean'])})"
                )

    # ---- 6. F8 ttg 篮子（票 12 NN 语义）FULL vs CUT
    print("\n== 6. F8 ttg 篮子（锚=srct:ou 每书末价均值+7+ 按 ≥7；27,844 行=冻结口径） ==")
    ladders = load_ou_ladder([eu["sid"] for _f, _j, eu, _y in in_win])
    tt: list[dict] = []
    n_no_ladder = 0
    for fx, jc, eu, y0 in in_win:
        w = window_of(eu["season"])
        if jc["ttg"] is None:
            continue
        lad = ladders.get(eu["sid"])
        rung25 = lad.get(2.5) if lad else None
        if rung25 is None:
            n_no_ladder += 1
            continue
        p_over25 = rung25.p_over
        t = eu["hg"] + eu["ag"]
        f8 = devig(jc["ttg"])
        if f8 is None:
            continue
        for basket, ks, fair_b in (("over", range(3, 8), p_over25), ("under", range(0, 3), 1.0 - p_over25)):
            ks_l = list(ks)
            ev_real = ttg_basket_ev(jc["ttg"], t, ks_l)
            jc_basket = sum(f8[k] for k in ks_l)
            ev_anchor = fair_b * sum(f8[k] * jc["ttg"][k] for k in ks_l) / (jc_basket * len(ks_l)) - 1.0
            tt.append({
                "fx": fx, "window": w, "y0": y0, "league": eu["league"], "basket": basket,
                "gap": jc_basket - fair_b, "ev_real": ev_real, "ev_anchor": ev_anchor,
            })
    print(f"  无 2.5 板 {n_no_ladder} 场不入；F8 分析行 {len(tt)}（冻结 27,844）")

    def f8_metrics(sub: list[dict]) -> dict:
        n_fx = len({r["fx"] for r in sub})
        sel = [r for r in sub if r["ev_anchor"] > 0]
        em, es, _ = clustered_mean([(r["fx"], r["ev_real"]) for r in sel])
        gm, gs, _ = clustered_mean([(r["fx"], r["gap"]) for r in sub])
        return {
            "n_fx": n_fx, "n_sel": len(sel), "gap": gm, "ev": em,
            "z": (em / es if es and es > 0 and len(sel) >= 2 else float("nan")),
        }

    f8_res: dict[str, dict[tuple[str, str], dict]] = {}
    for arm in ("FULL", "CUT"):
        rows_a = tt if arm == "FULL" else [r for r in tt if r["y0"] < CUT_YEAR]
        res = {}
        for w in ("search", "validation", "confirmation"):
            for b in ("over", "under"):
                sub = [r for r in rows_a if r["window"] == w and r["basket"] == b]
                if sub:
                    res[(w, b)] = f8_metrics(sub)
        f8_res[arm] = res
    print("  主行：n_sel/EV/z/gap FULL → CUT（Δ=2526 段贡献）；票 12 NN=2026-11-09 存档")
    mism12 = 0
    for w in ("search", "validation", "confirmation"):
        print(f"  -- {w}")
        for b in ("over", "under"):
            mf, mc = f8_res["FULL"].get((w, b)), f8_res["CUT"].get((w, b))
            ref = T12_NN[(w, b)]
            ok12 = (
                mf["n_sel"] == ref["n_sel"] and abs(mf["gap"] - ref["gap"]) < 6e-5
                and abs(mf["ev"] - ref["ev"]) < 6e-5
                and (np.isfinite(ref["z"]) and abs(mf["z"] - ref["z"]) < 5e-4
                     or not np.isfinite(ref["z"]) and not np.isfinite(mf["z"]))
            )
            mism12 += 0 if ok12 else 1
            print(
                f"    {b:>5}: n_sel {mf['n_sel']:>3}→{mc['n_sel']:<3} EV {fmt(mf['ev'])}→{fmt(mc['ev'])}"
                f" (Δ{fmt(mc['ev'] - mf['ev'])}) z {fmt(mf['z'], nd=3)}→{fmt(mc['z'], nd=3)}"
                f" | gap {fmt(mf['gap'])}→{fmt(mc['gap'])} (Δ{fmt(mc['gap'] - mf['gap'])})"
                f" | T12核对{'✔' if ok12 else '✘'}"
            )
    print(f"  FULL vs 票 12 NN 存档 mismatch={mism12}/6（应 0；F8 行集此前不受树增长影响）")
    # 搜索/验证窗 FULL=CUT 断言（2526 只在确认窗；nan 感知比较）
    for w in ("search", "validation"):
        for b in ("over", "under"):
            a, c = f8_res["FULL"][(w, b)], f8_res["CUT"][(w, b)]
            assert a["n_fx"] == c["n_fx"] and a["n_sel"] == c["n_sel"] and all(
                close2(a[k], c[k], 1e-12) for k in ("gap", "ev", "z")
            ), f"F8 {w}/{b} 搜索/验证窗不应受 CUT 影响"
    print("  断言：F8 搜索/验证窗 FULL=CUT ✔（2526 段只落确认窗→候选/晋升/幸存机制读数零变化）")
    print("  -- 确认窗联赛切片（实质变化行）")
    for lg in sorted({r["league"] for r in tt if r["window"] == "confirmation"}):
        for b in ("over", "under"):
            sub_f = [r for r in tt if r["window"] == "confirmation" and r["league"] == lg and r["basket"] == b]
            sub_c = [r for r in sub_f if r["y0"] < CUT_YEAR]
            mf, mc = f8_metrics(sub_f), f8_metrics(sub_c) if sub_c else {"n_sel": 0, "ev": float("nan"), "z": float("nan"), "gap": float("nan")}
            if mf["n_sel"] != mc["n_sel"] or (np.isfinite(mf["ev"]) and np.isfinite(mc["ev"]) and abs(mf["ev"] - mc["ev"]) > 6e-5):
                print(
                    f"    {lg}×{b}: n_sel {mf['n_sel']}→{mc['n_sel']} EV {fmt(mf['ev'])}→{fmt(mc['ev'])}"
                    f" z {fmt(mf['z'], nd=3)}→{fmt(mc['z'], nd=3)} gap {fmt(mf['gap'])}→{fmt(mc['gap'])}"
                )
    # 2526 段记录级：价值侧选择行
    print("  -- 2526 段自身读数 + 价值侧选择行（ev_anchor>0）逐行")
    seg_tt = [r for r in tt if r["y0"] >= CUT_YEAR]
    for b in ("over", "under"):
        sub = [r for r in seg_tt if r["basket"] == b]
        m = f8_metrics(sub) if sub else {"n_fx": 0, "n_sel": 0, "gap": float("nan"), "ev": float("nan"), "z": float("nan")}
        print(
            f"    [seg {b}]: n_fx {m['n_fx']} n_sel {m['n_sel']} gap {fmt(m['gap'])} EV {fmt(m['ev'])} z {fmt(m['z'], nd=3)}"
        )
    for r in seg_tt:
        if r["ev_anchor"] > 0:
            print(
                f"    选择行: fx={r['fx']} {r['league']} {r['basket']} gap={r['gap']:+.5f}"
                f" ev_real={r['ev_real']:+.4f} ev_anchor={r['ev_anchor']:+.5f}"
            )

    # ---- 7. gap 分解闭合校验（FULL=场次加权(CUT,SEG2526)；SEG 由被剔行直接重算）
    print("\n== 7. 分解闭合校验（确认窗 gap：FULL ≈ n 加权(CUT, SEG2526)） ==")
    checks = [
        ("F7 -1×让方", [r for r in hh if r["window"] == "confirmation" and r["lg"] == "-1" and r["side"] == "让方"]),
        ("F7 +1×让方", [r for r in hh if r["window"] == "confirmation" and r["lg"] == "+1" and r["side"] == "让方"]),
        ("F8 over", [r for r in tt if r["window"] == "confirmation" and r["basket"] == "over"]),
        ("F8 under", [r for r in tt if r["window"] == "confirmation" and r["basket"] == "under"]),
    ]
    for name, rows in checks:
        rows_f, rows_c, rows_s = rows, [r for r in rows if r["y0"] < CUT_YEAR], [r for r in rows if r["y0"] >= CUT_YEAR]
        if not rows_s:
            print(f"  {name}: 2526 段 0 行（FULL=CUT）")
            continue
        m_f, m_c, m_s = gap_metrics(rows_f), gap_metrics(rows_c), gap_metrics(rows_s)
        pred = (m_c["n"] * m_c["mean"] + m_s["n"] * m_s["mean"]) / m_f["n"]
        print(
            f"  {name}: FULL={m_f['mean']:+.6f} 加权预测={pred:+.6f} 残差={m_f['mean'] - pred:+.2e}"
            f"（n {m_f['n']}={m_c['n']}+{m_s['n']}；SEG2526 gap={m_s['mean']:+.4f}）"
        )


if __name__ == "__main__":
    main()
