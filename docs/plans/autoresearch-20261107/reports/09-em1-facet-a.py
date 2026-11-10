#!/usr/bin/env python3
# 票 09：EM-1 facet A 定价差主面清扫（协议见 09-em1-protocol.md，先落再跑）
#
# 纯离线只读。gap = JC 收盘隐含 − 欧锚公允；三段切分（搜索/验证/确认各
# 碰一次）；候选/晋升/确认三门+BH 校正；全部切片（含全阴）入当前轮试验账
# （轮=台账文件边界，一轮一文件，_lib.em1_round_ledger_path 单源；
#   append-only，entry_id 幂等=同轮重跑）。
# BH=轮级单家族（E-32 修订 C，票 20）：本脚本 F1-F6/F8 与票 10 B/C 的
# 验证窗候选并成一个家族过一次 BH；族成员从试验账装载，轮不完整时
# fail-closed 不裁（无「先跑 09 还是 10」硬序，脚本幂等可重跑收口）。
#
# 用法：uv run python 09-em1-facet-a.py   （stdout=报告素材，重跑不重复记账）

from __future__ import annotations

import datetime as dt
from collections import defaultdict

import numpy as np

from _lib import (
    COARSE_LABELS,
    append_trial_ledger,
    coarse_idx,
    clustered_mean,
    devig,
    em1_pre_adjudication_rows,
    em1_format_z,
    em1_round_finalize,
    em1_round_ledger_path,
    ladder_coherent,
    ladder_to_totals,
    load_jc,
    load_euro,
    load_ou_ladder,
    one_sided_p,
    pair_fixtures,
    ttg_basket_ev,
)

LEDGER = em1_round_ledger_path()
N_FX_FLOOR = 200
N_SEL_FLOOR = 20
CONF_FX_FLOOR = 100
ANCHORS = ("avgc", "psc", "srct")  # 主锚 avgc；psc/srct=survivor 稳健性


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


def main() -> None:
    print("=" * 76)
    print("票 09：EM-1 facet A 定价差主面（协议=09-em1-protocol.md）")
    print("=" * 76)
    jc_rows = load_jc()
    eu_rows = load_euro()
    pairs, pstats = pair_fixtures(jc_rows, eu_rows)
    print(f"配对漏斗: {pstats}")

    # ---- had 行构造（各锚独立行集；srct 收盘共识列实测全 NULL→零行，覆盖账如实）
    rows_by_anchor: dict[str, list[dict]] = {a: [] for a in ANCHORS}
    for fx, (jc, eu) in enumerate(pairs):
        w = window_of(eu["season"])
        if w is None:
            continue
        fairs = {a: devig(eu[a]) for a in ANCHORS}
        hg, ag = eu["hg"], eu["ag"]
        hit = (1.0 if hg > ag else 0.0, 1.0 if hg == ag else 0.0, 1.0 if hg < ag else 0.0)
        ko = eu["kickoff"]
        hour = ko.hour
        hb = "0-9时" if hour < 9 else ("9-15时" if hour < 15 else ("15-21时" if hour < 21 else "21-24时"))
        wk = "周中" if ko.weekday() < 5 else "周末"
        pin_dev = None
        if fairs["psc"] is not None and fairs["avgc"] is not None:
            pin_dev = fairs["psc"][0] - fairs["avgc"][0]  # 同场三向近似同档（切片属性）
        for k in range(3):
            base = {
                "fx": fx, "window": w, "league": eu["league"], "k": k, "hit": hit[k],
                "jc_odds": jc["jc"][k], "hb": hb, "wk": wk, "pin_dev": pin_dev,
            }
            for a in ANCHORS:
                if fairs[a] is None:
                    continue
                r = dict(base)
                r["fair"] = fairs[a][k]
                rows_by_anchor[a].append(r)
    main_rows = rows_by_anchor["avgc"]
    by_win: dict[str, list[dict]] = defaultdict(list)
    for r in main_rows:
        by_win[r["window"]].append(r)
    print(f"had 分析行 {len(main_rows)}（窗分布 {[(w, len(v)) for w, v in sorted(by_win.items())]}）")

    # pin_dev 三分位（搜索窗冻结，防泄漏；三锚行集共用同一分类；缺 psc 行自成桶）
    svals = sorted(r["pin_dev"] for r in by_win["search"] if r["pin_dev"] is not None)
    q33, q66 = float(np.percentile(svals, 33)), float(np.percentile(svals, 66))
    for a in ANCHORS:
        for r in rows_by_anchor[a]:
            if r["pin_dev"] is None:
                r["pin3"] = "缺psc"
            else:
                r["pin3"] = "Pin松" if r["pin_dev"] < q33 else ("Pin紧" if r["pin_dev"] > q66 else "≈")
    print(f"pin_dev 三分位切点（搜索窗冻结）：{q33:+.4f} / {q66:+.4f}")

    def fam_slices(family: str, sample: list[dict]) -> list[tuple[str, list[dict]]]:
        if family == "F1":
            return [(lg, [r for r in sample if r["league"] == lg]) for lg in sorted({r["league"] for r in sample})]
        if family == "F2":
            out = []
            for bi, bl in enumerate(COARSE_LABELS):
                for k, kcn in ((0, "主"), (1, "平"), (2, "客")):
                    out.append((f"{bl}×{kcn}", [r for r in sample if coarse_idx(r["fair"]) == bi and r["k"] == k]))
            return out
        key = {"F3": "league", "F4": "hb", "F5": "wk", "F6": "pin3"}[family]
        out = []
        for bi, bl in enumerate(COARSE_LABELS):
            for v in sorted({r[key] for r in sample}):
                out.append((f"{v}×{bl}", [r for r in sample if r[key] == v and coarse_idx(r["fair"]) == bi]))
        return out

    def slice_metrics(sub: list[dict]) -> dict:
        n_fx = len({r["fx"] for r in sub})
        gm, gs, _ = clustered_mean([(r["fx"], 1.0 / r["jc_odds"] - r["fair"]) for r in sub])
        ev_rows = [(r["fx"], r["jc_odds"] * r["hit"] - 1.0) for r in sub if r["jc_odds"] * r["fair"] > 1.0]
        em, es, _ = clustered_mean(ev_rows)
        return {
            "n_fx": n_fx, "n_sel": len(ev_rows), "gap": gm, "gap_se": gs, "ev": em, "ev_se": es,
            "ev_z": (em / es if es and es > 0 and len(ev_rows) >= 2 else float("nan")),
        }

    def cand_gate(m: dict) -> bool:
        return m["n_fx"] >= N_FX_FLOOR and m["n_sel"] >= N_SEL_FLOOR and np.isfinite(m["ev_z"]) and m["ev"] > 0 and m["ev_z"] > 2

    def promo_gate(m: dict) -> bool:
        return m["n_fx"] >= N_FX_FLOOR and np.isfinite(m["ev_z"]) and m["ev"] >= 0.03 and m["ev_z"] > 2

    def conf_gate(m: dict) -> bool:
        return m["n_fx"] >= CONF_FX_FLOOR and np.isfinite(m["ev_z"]) and m["ev"] >= 0.03 and m["ev_z"] > 2

    def ledger_row(family: str, sname: str, window: str, m: dict, stage: str, note: str = "") -> dict:
        z = m["ev_z"]
        return {
            "entry_id": f"09-A-{family}-{sname}-{window}", "ts": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "ticket": "09", "facet": "A", "family": family, "slice": sname, "window": window,
            "n_fx": m["n_fx"], "n_sel": m["n_sel"], "metric": "ev/gap",
            "value": f"{m['ev']:.4f}/{m['gap']:.4f}", "se": f"{m['ev_se']:.4f}", "z": em1_format_z(z),
            "p": f"{one_sided_p(z):.5f}" if np.isfinite(z) else "",
            "stage": stage, "note": note or f"gap_se={m['gap_se']:.4f}",
        }

    ledger_rows: list[dict] = []
    candidates: list[tuple[str, str]] = []  # (family, slice)

    # ---- F1-F6：搜索→验证→确认（主锚 avgc）
    for family in ("F1", "F2", "F3", "F4", "F5", "F6"):
        for sname, sub in fam_slices(family, by_win["search"]):
            m = slice_metrics(sub)
            stage = "candidate" if cand_gate(m) else ("insufficient" if m["n_fx"] < N_FX_FLOOR else "all_negative")
            if stage == "candidate":
                candidates.append((family, sname))
            ledger_rows.append(ledger_row(family, sname, "search", m, stage))

    val_cache: dict[tuple[str, str], tuple[dict, bool]] = {}
    for family, sname in candidates:
        sub = dict(fam_slices(family, by_win["validation"])).get(sname, [])
        m = slice_metrics(sub)
        pass3 = promo_gate(m)
        val_cache[(family, sname)] = (m, pass3)
        ledger_rows.append(ledger_row(family, sname, "validation", m, "bh_pending" if pass3 else "dead"))
    # 轮级 BH 裁定延后：待 F7/F8 验证账落齐后与全轮 facet 并族一次校正
    # （E-32 修订 C，票 20）

    # ---- hhad F7（结构面 only：push 语义无锚拆分，EV 面未搜索——协议§1）
    n_line_mismatch = 0
    hh_rows = []
    for fx, (jc, eu) in enumerate(pairs):
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
        hh_rows.append({"fx": fx, "window": w, "lg": lg, "side": "让方", "gap": ph_c - e_ph})
        hh_rows.append({"fx": fx, "window": w, "lg": lg, "side": "受让方", "gap": pa_c - (1 - e_ph)})
    for w in ("search", "validation", "confirmation"):
        for lg in ("≤-2", "-1", "+1", "≥+2"):
            for side in ("让方", "受让方"):
                sub = [r for r in hh_rows if r["window"] == w and r["lg"] == lg and r["side"] == side]
                if not sub:
                    continue
                m_, se, n = clustered_mean([(r["fx"], r["gap"]) for r in sub])
                z = m_ / se if se and se > 0 else float("nan")
                ledger_rows.append({
                    "entry_id": f"09-A-F7-{lg}×{side}-{w}", "ts": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "ticket": "09", "facet": "A", "family": "F7", "slice": f"{lg}×{side}", "window": w,
                    "n_fx": n, "n_sel": 0, "metric": "gap", "value": f"{m_:.4f}", "se": f"{se:.4f}",
                    "z": em1_format_z(z), "p": "", "stage": "structure_only", "note": "EV面未搜索(push语义)",
                })

    # ---- ttg F8（协议修订 B：over2.5 单线篮子对照；装载=修订 A 赛前板 + 票 12 锚修）
    ladders = load_ou_ladder([eu["sid"] for _, eu in pairs if window_of(eu["season"])])
    tt_rows = []  # 每场×篮别一行
    n_no_ladder = 0
    rung_books: list[int] = []
    rung_ages: list[float] = []
    for fx, (jc, eu) in enumerate(pairs):
        w = window_of(eu["season"])
        if w is None or jc["ttg"] is None:
            continue
        lad = ladders.get(eu["sid"])
        rung25 = lad.get(2.5) if lad else None
        if rung25 is None:
            n_no_ladder += 1
            continue
        p_over25 = rung25.p_over
        rung_books.append(rung25.n_books)
        rung_ages.append(rung25.age_h)
        t = eu["hg"] + eu["ag"]
        f8 = devig(jc["ttg"])
        if f8 is None:
            continue
        for basket, ks, fair_b in (("over", range(3, 8), p_over25), ("under", range(0, 3), 1.0 - p_over25)):
            ks_l = list(ks)
            # 7+ 档按 t>=7 结算（edge-audit-20261109 票 12 修正；0-6 档维持恰好命中）
            ev_real = ttg_basket_ev(jc["ttg"], t, ks_l)
            # 锚篮 EV：欧篮概率按 JC 篮内形状分摊（篮级检验——只把篮间错价当信号）
            jc_basket = sum(f8[k] for k in ks_l)
            ev_anchor = fair_b * sum(f8[k] * jc["ttg"][k] for k in ks_l) / (jc_basket * len(ks_l)) - 1.0
            tt_rows.append({
                "fx": fx, "window": w, "league": eu["league"], "basket": basket,
                "gap": jc_basket - fair_b,
                "ev_real": ev_real, "ev_anchor": ev_anchor,
            })
    f8_candidates: list[tuple[str, str]] = []
    f8_metrics: dict[tuple[str, str, str], dict] = {}

    def f8_slices(sample: list[dict]) -> list[tuple[str, list[dict]]]:
        out = [("over", [r for r in sample if r["basket"] == "over"]), ("under", [r for r in sample if r["basket"] == "under"])]
        for lg in sorted({r["league"] for r in sample}):
            for b in ("over", "under"):
                out.append((f"{lg}×{b}", [r for r in sample if r["league"] == lg and r["basket"] == b]))
        return out

    def f8_metrics_of(sub: list[dict]) -> dict:
        n_fx = len({r["fx"] for r in sub})
        sel = [r for r in sub if r["ev_anchor"] > 0]
        em, es, _ = clustered_mean([(r["fx"], r["ev_real"]) for r in sel])
        gm, gs, _ = clustered_mean([(r["fx"], r["gap"]) for r in sub])
        return {
            "n_fx": n_fx, "n_sel": len(sel), "gap": gm, "gap_se": gs, "ev": em, "ev_se": es,
            "ev_z": (em / es if es and es > 0 and len(sel) >= 2 else float("nan")),
        }

    f8_val: dict[tuple[str, str], tuple[dict | None, bool]] = {}
    f8_cand: set[tuple[str, str]] = set()
    for w in ("search", "validation", "confirmation"):
        wsample = [r for r in tt_rows if r["window"] == w]
        for sname, sub in f8_slices(wsample):
            if not sub:
                continue
            m = f8_metrics_of(sub)
            f8_metrics[(sname, "", w)] = m
            if w == "confirmation":
                # F3（correctness 五修）：确认窗不落首批占位行——裁定后第二批
                # 按最终 stage 写幸存者确认行，首批 all_negative 占位会同
                # entry_id 被幂等吞掉；此处只为晋升者的确认门预留指标
                continue
            if w == "search":
                if m["n_fx"] < N_FX_FLOOR:
                    stage = "insufficient"
                elif cand_gate(m):
                    stage = "candidate"
                    f8_candidates.append((sname, ""))
                    f8_cand.add((sname, ""))
                else:
                    stage = "all_negative"
            else:
                # 验证窗：候选切片按晋升门写 bh_pending/dead（与 F1-F6 同构，
                # BH 拒绝终态行可复原）；非候选切片照旧全阴覆盖入账
                stage = "all_negative"
                if (sname, "") in f8_cand:
                    ok = promo_gate(m)
                    f8_val[(sname, "")] = (m, ok)
                    stage = "bh_pending" if ok else "dead"
            ledger_rows.append(ledger_row("F8", sname, w, m, stage))

    # ---- 轮级单家族 BH（E-32 修订 C，票 20）：先落搜索+验证账（首批经
    # em1_pre_adjudication_rows 过滤确认窗占位，F3），再从台账装载全轮族
    # （本脚本 F1-F6/F8 与票 10 B/C 的候选并集，含全阴与验证窗无行者
    # z=nan→p=1.0），一次校正统裁定+被拒终态行落账（_lib.em1_round_finalize
    # 共用接缝，两脚本同一 fail-closed 门，F5）。脚本独立运行无硬序：轮不
    # 完整（预注册 BH 贡献组缺 search 账）→ 不裁，候选留 bh_pending，待轮
    # 内其余脚本落账后重跑本脚本幂等收口。
    append_trial_ledger(em1_pre_adjudication_rows(ledger_rows))
    bh_pass, round_family, missing_groups = em1_round_finalize(LEDGER)
    if missing_groups:
        print(
            f"[轮级 BH 未裁] 轮族不完整：缺 {missing_groups} 的 search 账——缺报缩族与先筛后校"
            "同罪（修订 C）；候选留 bh_pending，轮内其余脚本落账后重跑本脚本收口"
        )

    promoted = [(f, s) for (f, s), (_m, ok) in val_cache.items() if ok and f"09-A-{f}-{s}-validation" in bh_pass]
    f8_promoted = [s for s, (_m, ok) in f8_val.items() if ok and f"09-A-F8-{s[0]}-validation" in bh_pass]

    conf_rows: list[dict] = []
    survivors = []
    for family, sname in promoted:
        sub = dict(fam_slices(family, by_win["confirmation"])).get(sname, [])
        m = slice_metrics(sub)
        ok = conf_gate(m)
        if ok:
            survivors.append((family, sname, m))
        conf_rows.append(ledger_row(family, sname, "confirmation", m, "survivor" if ok else "dead"))
    f8_survivors = []
    for sname, _ in f8_promoted:
        m = f8_metrics.get((sname, "", "confirmation"))
        if m is None:
            continue
        ok = conf_gate(m)
        if ok:
            f8_survivors.append((sname, m))
        conf_rows.append(ledger_row("F8", sname, "confirmation", m, "survivor" if ok else "dead"))
    append_trial_ledger(conf_rows)

    n_gate = sum(1 for _m, ok in val_cache.values() if ok)
    print(f"\nF1-F6：搜索切片候选 {len(candidates)}；验证过 promo 门 {n_gate}；轮级 BH 后晋升 {len(promoted)}；确认窗幸存 {len(survivors)}")
    print(
        f"F8(ttg 篮子=修订B)：搜索候选 {len(f8_candidates)}；验证过门 {sum(1 for _m, ok in f8_val.values() if ok)}；"
        f"轮级 BH 后晋升 {len(f8_promoted)}；确认幸存 {len(f8_survivors)}"
    )
    print(
        f"多重校正账（E-32 修订 C，轮级单家族）：BH 家族=本轮全 facet 候选并集 m={len(round_family)}"
        f"（含全阴/验证窗无行者，z 不可计算 p=1.0），α=0.05 预期假阳性 {len(round_family) * 0.05:.1f}"
    )
    print(f"试验账已落/更新：{LEDGER}（本票条目 {len(ledger_rows) + len(conf_rows)}）")

    # ---- 覆盖账 + 摘要
    print("\n[覆盖账]")
    print("  crs/hafu：未搜索（无欧锚对照面，协议§1 固定行）")
    print(f"  hhad：线不匹配/无AH收盘 {n_line_mismatch} 场不入 F7；F7 结构行 {len(hh_rows)}（结构面 only）")
    print(f"  ttg：无 over2.5 赛前板 {n_no_ladder} 场不入 F8（修订B 篮子）；F8 分析行 {len(tt_rows)}")
    if rung_books:
        ages = sorted(a for a in rung_ages if np.isfinite(a))
        age_note = (
            f"median={np.median(ages):.1f}h p90={np.percentile(ages, 90):.1f}h" if ages else "无 kickoff 不报告"
        )
        print(
            f"  OU 锚（票 12 修正=每书末价→跨书均值）：2.5 线书覆盖 median={np.median(rung_books):.1f} "
            f"p10={np.percentile(rung_books, 10):.1f}；报价年龄（最陈旧书）{age_note}"
        )

    print("\n[搜索窗 F1-F6 全切片 EV/gap 摘要（* = 候选；insufficient = n_fx<200）]")
    for family in ("F1", "F2", "F3", "F4", "F5", "F6"):
        for sname, sub in fam_slices(family, by_win["search"]):
            m = slice_metrics(sub)
            flag = ""
            if m["n_fx"] < N_FX_FLOOR:
                flag = " (insufficient)"
            elif cand_gate(m):
                flag = " *"
            print(f"  {family} {sname:>14}: n_fx={m['n_fx']:>5} n_sel={m['n_sel']:>5} EV={m['ev']:+.3%} z={m['ev_z']:+.2f} gap={m['gap']:+.3%}{flag}")
    print("\n[搜索窗 F8 篮子摘要（候选标 *）]")
    for (sname, _e, w), m in sorted(f8_metrics.items()):
        if w == "search":
            flag = " *" if (sname, "") in f8_candidates else (" (insufficient)" if m["n_fx"] < N_FX_FLOOR else "")
            print(f"  F8 {sname:>10}: n_fx={m['n_fx']:>5} n_sel={m['n_sel']:>5} EV={m['ev']:+.3%} z={m['ev_z']:+.2f} gap={m['gap']:+.3%}{flag}")

    print(f"\n[幸存者] had F1-F6: {[(f, s) for f, s, _ in survivors] or '无'}；ttg F8: {[s for s, _ in f8_survivors] or '无'}")
    for family, sname, m in survivors:
        notes = []
        for a in ("psc", "srct"):
            sub = dict(fam_slices(family, [r for r in rows_by_anchor[a] if r["window"] == "confirmation"])).get(sname, [])
            ma = slice_metrics(sub)
            notes.append(f"{a}: EV={ma['ev']:+.3%} z={ma['ev_z']:+.2f}")
        print(f"  survivor {family}/{sname}（确认窗 avgc EV={m['ev']:+.3%} z={m['ev_z']:+.2f}）稳健性 → {'; '.join(notes)}")
    for sname, m in f8_survivors:
        print(f"  survivor F8/{sname}（确认窗 EV={m['ev']:+.3%} z={m['ev_z']:+.2f}）")


if __name__ == "__main__":
    main()
