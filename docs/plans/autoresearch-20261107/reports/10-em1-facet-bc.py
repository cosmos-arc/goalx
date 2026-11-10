#!/usr/bin/env python3
# 票 10：EM-1 facet B 时序面 + facet C 基本面（协议=09-em1-protocol.md 共用）
#
# 决策时刻 T=开球−2h（固定规则，无前视）：JC 报价@T × PIT 欧锚（Pinnacle
# @T）公允 >1 → 价值侧下注，实现 EV=odds@T×hit−1；特征只用 T 之前信息。
# 三窗+门+BH 全按票 09 协议；切片入同一轮试验账（facet=B/C；轮=台账文件
# 边界，一轮一文件，_lib.em1_round_ledger_path 单源）。
# BH=轮级单家族（E-32 修订 C，票 20）：本脚本 B/C 与票 09 A(F1-F8) 的
# 验证窗候选并成一个家族过一次 BH；族成员从试验账装载，轮不完整时
# fail-closed 不裁（无「先跑 09 还是 10」硬序，脚本幂等可重跑收口）。
#
# facet B 切片族（预注册）：
#   B1 陈盘：T−{6h,24h} 窗内 Pinnacle 隐含动 ≥{1,2}pp 且 JC 该向未跟
#        （JC 报价窗内未变）→ 顺欧盘方向下注。子片=窗口×幅度（4）。
#   B2 steam：Pinnacle T−6h→T 漂移符号×顺漂移方向（2）。
#   B3 JC 价格年龄：JC 该向距上次变价 {<6h,6-12h,>12h}（3）。
#   B4 联赛滞后带（票 08 输出直接接：慢=英冠/苏超，快=其余）×B1 触发（2×2）。
# facet C 切片族：
#   C1 Elo 残差：elo 隐含−市场公允（T 时刻 Pinnacle）三分位×价值侧（3）。
#   C2 休息日：min(主,客) 距上场 {≤3,4-6,≥7} 天×价值侧（3）。
#   C3 联赛×价值侧（9）。
# 覆盖账：xG=后视（无赛前预测面）未搜索；阵容公布窗=lineup-edge 板 03
# 已冻结（公布窗无价格断点），本票引用不重跑。

from __future__ import annotations

import datetime as dt
from collections import defaultdict

import numpy as np

from _lib import (
    append_trial_ledger,
    clustered_mean,
    devig,
    em1_pre_adjudication_rows,
    em1_format_z,
    em1_round_finalize,
    em1_round_ledger_path,
    load_jc,
    load_jc_traj,
    load_euro,
    load_pin_1x2,
    one_sided_p,
    pair_fixtures,
)

LEDGER = em1_round_ledger_path()
N_FX_FLOOR = 200
N_SEL_FLOOR = 20
CONF_FX_FLOOR = 100
SLOW_LEAGUES = ("英冠", "苏超")  # 票 08 联赛滞后切片直接接（跟随率最低两联赛）
T_OFFSET = dt.timedelta(hours=2)


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
    return None


def quote_at(traj, t):
    """traj=[(t,q)...] 升序；返回 ≤t 的最后报价 (t,q) 或 None。"""
    lo, hi = 0, len(traj) - 1
    res = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if traj[mid][0] <= t:
            res = traj[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return res


def main() -> None:
    print("=" * 76)
    print("票 10：EM-1 facet B 时序 + facet C 基本面（T=开球−2h，PIT Pinnacle 锚）")
    print("=" * 76)
    jc_rows = load_jc()
    eu_rows = load_euro()
    pairs, pstats = pair_fixtures(jc_rows, eu_rows)
    print(f"配对漏斗: {pstats}")
    jc_had = load_jc_traj([jc["sid"] for jc, _ in pairs], "had")
    pin = load_pin_1x2([eu["sid"] for _, eu in pairs])

    # 休息日（同联赛同季同队名上一场）
    sched: dict[tuple[str, str, str], list] = defaultdict(list)
    for e in eu_rows:
        sched[(e["league"], e["season"], e["home"])].append(e["kickoff"])
        sched[(e["league"], e["season"], e["away"])].append(e["kickoff"])
    for k in sched:
        sched[k].sort()

    def rest_days(league: str, season: str, team: str, ko) -> float | None:
        lst = sched.get((league, season, team))
        if not lst:
            return None
        i = np.searchsorted(lst, ko)
        if i <= 0:
            return None
        return (ko - lst[i - 1]).total_seconds() / 86400.0

    # ---- T 时刻行构造
    rows = []
    acct = {"no_jc_quote_at_T": 0, "no_pin_at_T": 0, "jc_window_not_covering_T": 0}
    for fx, (jc, eu) in enumerate(pairs):
        w = window_of(eu["season"])
        if w is None or eu["kickoff"] is None:
            continue
        cst = dt.timezone(dt.timedelta(hours=8))
        T = eu["kickoff"].replace(tzinfo=cst) - T_OFFSET
        jtr = jc_had.get(jc["sid"])
        ptr = pin.get(eu["sid"])
        if not jtr or not ptr:
            continue
        if T < jtr[0][0]:
            acct["jc_window_not_covering_T"] += 1
            continue
        q_jc = quote_at(jtr, T)
        q_pin = quote_at(ptr, T)
        if q_jc is None:
            acct["no_jc_quote_at_T"] += 1
            continue
        if q_pin is None:
            acct["no_pin_at_T"] += 1
            continue
        fair = devig(q_pin[1])
        if fair is None:
            continue
        hg, ag = eu["hg"], eu["ag"]
        hit = (1.0 if hg > ag else 0.0, 1.0 if hg == ag else 0.0, 1.0 if hg < ag else 0.0)
        # 特征（全部只用 ≤T 信息）
        feats = {}
        for label, back in (("6h", dt.timedelta(hours=6)), ("24h", dt.timedelta(hours=24))):
            p_prev = quote_at(ptr, T - back)
            if p_prev is None:
                feats[f"euro_d_{label}"] = None
            else:
                f_prev = devig(p_prev[1])
                feats[f"euro_d_{label}"] = tuple(fair[k] - f_prev[k] for k in range(3)) if f_prev else None
        j_prev6 = quote_at(jtr, T - dt.timedelta(hours=6))
        j_prev24 = quote_at(jtr, T - dt.timedelta(hours=24))
        jc_age = (T - q_jc[0]).total_seconds() / 3600.0
        # elo 残差（elo 隐含主胜，400 制）
        elo_h, elo_a = eu["elo_h"], eu["elo_a"]
        elo_dev = None
        if elo_h is not None and elo_a is not None:
            p_elo = 1.0 / (1.0 + 10 ** (-(elo_h - elo_a) / 400.0))
            elo_dev = p_elo - fair[0]
        rh = rest_days(eu["league"], eu["season"], eu["home"], eu["kickoff"])
        ra = rest_days(eu["league"], eu["season"], eu["away"], eu["kickoff"])
        rests = [r for r in (rh, ra) if r is not None]
        for k in range(3):
            rows.append({
                "fx": fx, "window": w, "league": eu["league"], "k": k,
                "odds": q_jc[1][k], "fair": fair[k], "hit": hit[k],
                "jc_age": jc_age, "elo_dev": elo_dev,
                "rest_min": min(rests) if len(rests) == 2 else None,
                "lag_band": "慢" if eu["league"] in SLOW_LEAGUES else "快",
                "euro_d6": feats["euro_d_6h"][k] if feats["euro_d_6h"] else None,
                "euro_d24": feats["euro_d_24h"][k] if feats["euro_d_24h"] else None,
                "jc_unfollowed6": j_prev6 is not None and j_prev6[1][k] == q_jc[1][k],
                "jc_unfollowed24": j_prev24 is not None and j_prev24[1][k] == q_jc[1][k],
            })
    by_win: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_win[r["window"]].append(r)
    print(f"T 时刻分析行 {len(rows)}（窗分布 {[(w, len(v)) for w, v in sorted(by_win.items())]}）；剔除账 {acct}")

    # elo_dev / rest 三分位（搜索窗冻结）
    ev = sorted(r["elo_dev"] for r in by_win["search"] if r["elo_dev"] is not None)
    eq = (float(np.percentile(ev, 33)), float(np.percentile(ev, 66))) if ev else (0.0, 0.0)
    for r in rows:
        if r["elo_dev"] is None:
            r["elo3"] = "缺"
        else:
            r["elo3"] = "Elo低" if r["elo_dev"] < eq[0] else ("Elo高" if r["elo_dev"] > eq[1] else "≈")
        if r["rest_min"] is None:
            r["rest3"] = "缺"
        else:
            r["rest3"] = "≤3天" if r["rest_min"] <= 3 else ("4-6天" if r["rest_min"] <= 6 else "≥7天")
        r["age3"] = "<6h" if r["jc_age"] < 6 else ("6-12h" if r["jc_age"] <= 12 else ">12h")
    print(f"elo_dev 三分位（搜索窗冻结）：{eq[0]:+.4f} / {eq[1]:+.4f}")

    def metrics(sub: list[dict], stake_rows=None) -> dict:
        """stake_rows=None → 价值侧（odds×fair>1）；否则用传入下注行。"""
        n_fx = len({r["fx"] for r in sub})
        bet = stake_rows if stake_rows is not None else [r for r in sub if r["odds"] * r["fair"] > 1.0]
        em, es, _ = clustered_mean([(r["fx"], r["odds"] * r["hit"] - 1.0) for r in bet])
        return {
            "n_fx": n_fx, "n_sel": len(bet), "ev": em, "ev_se": es,
            "ev_z": (em / es if es and es > 0 and len(bet) >= 2 else float("nan")),
        }

    def fam_slices(facet: str, family: str, sample: list[dict]):
        """返回 [(slice名, 分母行, 下注行)]——facet B 的下注行由特征触发。"""
        out = []
        if family == "B1":
            for label, dkey, uf in (("6h×1pp", "euro_d6", "jc_unfollowed6"), ("6h×2pp", "euro_d6", "jc_unfollowed6"),
                                     ("24h×1pp", "euro_d24", "jc_unfollowed24"), ("24h×2pp", "euro_d24", "jc_unfollowed24")):
                thr = 0.01 if "1pp" in label else 0.02
                denom = [r for r in sample if r[dkey] is not None]
                # 欧动后价值条件 odds×fair>1 已隐含「押欧盘新信息方向、吃 JC 陈价」
                bet = [r for r in denom if abs(r[dkey]) >= thr and r[uf] and r["odds"] * r["fair"] > 1.0]
                out.append((label, denom, bet))
        elif family == "B2":
            denom = [r for r in sample if r["euro_d6"] is not None]
            bet = [r for r in denom if abs(r["euro_d6"]) >= 0.01 and r["odds"] * r["fair"] > 1.0]
            up = [r for r in bet if r["euro_d6"] > 0]
            dn = [r for r in bet if r["euro_d6"] < 0]
            out += [("顺升", [r for r in denom if r["euro_d6"] > 0], up), ("顺降", [r for r in denom if r["euro_d6"] < 0], dn)]
        elif family == "B3":
            for g in ("<6h", "6-12h", ">12h"):
                denom = [r for r in sample if r["age3"] == g]
                out.append((g, denom, [r for r in denom if r["odds"] * r["fair"] > 1.0]))
        elif family == "B4":
            for band in ("慢", "快"):
                denom = [r for r in sample if r["lag_band"] == band and r["euro_d24"] is not None]
                bet = [r for r in denom if abs(r["euro_d24"]) >= 0.01 and r["jc_unfollowed24"] and r["odds"] * r["fair"] > 1.0]
                out.append((f"{band}联赛带", denom, bet))
        elif family == "C1":
            for g in ("Elo低", "≈", "Elo高"):
                denom = [r for r in sample if r["elo3"] == g]
                out.append((g, denom, [r for r in denom if r["odds"] * r["fair"] > 1.0]))
        elif family == "C2":
            for g in ("≤3天", "4-6天", "≥7天"):
                denom = [r for r in sample if r["rest3"] == g]
                out.append((g, denom, [r for r in denom if r["odds"] * r["fair"] > 1.0]))
        elif family == "C3":
            for lg in sorted({r["league"] for r in sample}):
                denom = [r for r in sample if r["league"] == lg]
                out.append((lg, denom, [r for r in denom if r["odds"] * r["fair"] > 1.0]))
        return out

    def cand_gate(m):
        return m["n_fx"] >= N_FX_FLOOR and m["n_sel"] >= N_SEL_FLOOR and np.isfinite(m["ev_z"]) and m["ev"] > 0 and m["ev_z"] > 2

    def promo_gate(m):
        return m["n_fx"] >= N_FX_FLOOR and np.isfinite(m["ev_z"]) and m["ev"] >= 0.03 and m["ev_z"] > 2

    def conf_gate(m):
        return m["n_fx"] >= CONF_FX_FLOOR and np.isfinite(m["ev_z"]) and m["ev"] >= 0.03 and m["ev_z"] > 2

    ledger_rows: list[dict] = []
    candidates = []
    for facet, families in (("B", ("B1", "B2", "B3", "B4")), ("C", ("C1", "C2", "C3"))):
        for family in families:
            for sname, denom, bet in fam_slices(facet, family, by_win["search"]):
                m = metrics(denom, bet)
                stage = "candidate" if cand_gate(m) else ("insufficient" if m["n_fx"] < N_FX_FLOOR else "all_negative")
                if stage == "candidate":
                    candidates.append((facet, family, sname))
                ledger_rows.append({
                    "entry_id": f"10-{facet}-{family}-{sname}-search", "ts": dt.datetime.now().isoformat(timespec="seconds"),
                    "ticket": "10", "facet": facet, "family": family, "slice": sname, "window": "search",
                    "n_fx": m["n_fx"], "n_sel": m["n_sel"], "metric": "ev", "value": f"{m['ev']:.4f}",
                    "se": f"{m['ev_se']:.4f}", "z": em1_format_z(m["ev_z"]),
                    "p": f"{one_sided_p(m['ev_z']):.5f}" if np.isfinite(m["ev_z"]) else "",
                    "stage": stage,
                    "note": "T=ko-2h PIT Pinnacle" ,
                })

    val_cache = {}
    for facet, family, sname in candidates:
        denom, bet = None, None
        for s2, d2, b2 in fam_slices(facet, family, by_win["validation"]):
            if s2 == sname:
                denom, bet = d2, b2
                break
        if denom is None:
            # 验证窗无行：不入账（既有行为），但仍入轮级 BH 家族（台账装载无
            # 验证条目 → z=nan → p=1.0，修订 C）
            val_cache[(facet, family, sname)] = (None, False)
            continue
        m = metrics(denom, bet)
        ok = promo_gate(m)
        val_cache[(facet, family, sname)] = (m, ok)
        ledger_rows.append({
            "entry_id": f"10-{facet}-{family}-{sname}-validation", "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "ticket": "10", "facet": facet, "family": family, "slice": sname, "window": "validation",
            "n_fx": m["n_fx"], "n_sel": m["n_sel"], "metric": "ev", "value": f"{m['ev']:.4f}",
            "se": f"{m['ev_se']:.4f}", "z": em1_format_z(m["ev_z"]),
            "p": f"{one_sided_p(m['ev_z']):.5f}" if np.isfinite(m["ev_z"]) else "",
            "stage": "bh_pending" if ok else "dead", "note": "",
        })
    # ---- 轮级单家族 BH（E-32 修订 C，票 20）：先落搜索+验证账（首批经
    # em1_pre_adjudication_rows 过滤确认窗占位，F3——本脚本首批本就无
    # 确认行，统一接缝防御），再从台账装载全轮族（本脚本 B/C 与票 09 A
    # 的候选并集，含全阴与验证窗无行者 z=nan→p=1.0），一次校正统裁定+
    # 被拒终态行落账（_lib.em1_round_finalize 共用接缝，两脚本同一
    # fail-closed 门，F5）。脚本独立运行无硬序：轮不完整（预注册 BH
    # 贡献组缺 search 账）→ 不裁，候选留 bh_pending，待轮内其余脚本
    # 落账后重跑本脚本幂等收口。
    append_trial_ledger(em1_pre_adjudication_rows(ledger_rows))
    bh_pass, round_family, missing_groups = em1_round_finalize(LEDGER)
    if missing_groups:
        print(
            f"[轮级 BH 未裁] 轮族不完整：缺 {missing_groups} 的 search 账——缺报缩族与先筛后校"
            "同罪（修订 C）；候选留 bh_pending，轮内其余脚本落账后重跑本脚本收口"
        )
    promoted = [k for k, (_m, ok) in val_cache.items() if ok and f"10-{k[0]}-{k[1]}-{k[2]}-validation" in bh_pass]

    conf_rows: list[dict] = []
    survivors = []
    for facet, family, sname in promoted:
        denom, bet = None, None
        for s2, d2, b2 in fam_slices(facet, family, by_win["confirmation"]):
            if s2 == sname:
                denom, bet = d2, b2
                break
        if denom is None:
            continue
        m = metrics(denom, bet)
        ok = conf_gate(m)
        if ok:
            survivors.append((facet, family, sname, m))
        conf_rows.append({
            "entry_id": f"10-{facet}-{family}-{sname}-confirmation", "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "ticket": "10", "facet": facet, "family": family, "slice": sname, "window": "confirmation",
            "n_fx": m["n_fx"], "n_sel": m["n_sel"], "metric": "ev", "value": f"{m['ev']:.4f}",
            "se": f"{m['ev_se']:.4f}", "z": em1_format_z(m["ev_z"]),
            "p": f"{one_sided_p(m['ev_z']):.5f}" if np.isfinite(m["ev_z"]) else "",
            "stage": "survivor" if ok else "dead", "note": "",
        })
    append_trial_ledger(conf_rows)

    n_gate = sum(1 for _m, ok in val_cache.values() if ok)
    print(f"\nB/C：搜索切片候选 {len(candidates)}；验证过 promo 门 {n_gate}；轮级 BH 后晋升 {len(promoted)}；确认幸存 {len(survivors)}")
    print(
        f"多重校正账（E-32 修订 C，轮级单家族）：BH 家族=本轮全 facet 候选并集 m={len(round_family)}"
        f"（含全阴/验证窗无行者，z 不可计算 p=1.0），α=0.05 预期假阳性 {len(round_family) * 0.05:.1f}"
    )

    print("\n[覆盖账]")
    print("  xG 面：未搜索（match_features.xg=赛后实际值，无赛前预测面=后视）")
    print("  阵容公布窗：lineup-edge 板 03 已冻结（公布窗无价格断点 p=0.878），引用不重跑")
    print(f"  T 时刻剔除账：{acct}")
    print("  B1/B4 陈盘触发=「欧动≥阈值 且 JC 未跟 且 JC 价值侧」三条件同时；分母=有特征行的切片")

    print("\n[搜索窗全切片 EV 摘要（* = 候选；insufficient = n_fx<200）]")
    for facet, families in (("B", ("B1", "B2", "B3", "B4")), ("C", ("C1", "C2", "C3"))):
        for family in families:
            for sname, denom, bet in fam_slices(facet, family, by_win["search"]):
                m = metrics(denom, bet)
                flag = ""
                if m["n_fx"] < N_FX_FLOOR:
                    flag = " (insufficient)"
                elif cand_gate(m):
                    flag = " *"
                print(f"  {facet}/{family} {sname:>10}: n_fx={m['n_fx']:>5} n_sel={m['n_sel']:>5} EV={m['ev']:+.3%} z={m['ev_z']:+.2f}{flag}")

    print(f"\n[幸存者] {[(f, fa, s) for f, fa, s, _ in survivors] or '无'}")
    for f, fa, s, m in survivors:
        print(f"  survivor {f}/{fa}/{s}: 确认窗 EV={m['ev']:+.3%} z={m['ev_z']:+.2f} n_sel={m['n_sel']}")


if __name__ == "__main__":
    main()
