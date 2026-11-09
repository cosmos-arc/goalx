"""E-22 复算：池面冷门十年——psc 窗正 EV=0 / max fair/份额。

载重数字（账面）：psc 窗 24,596 场 23,169 个冷门候选，彩池正 EV=0；
盈亏线 fair/份额>1.5385（=1/0.65），实测 max=1.4916、p50=0.9926；
实测命中 16.96% ≈ fair 17.52% ≈ 份额 17.64%。
口径（源报告 issues/51 + evaluation/pool_replay.py 口径转录）：
冷门候选=份额代理(AvgC Shin)<25%（逐场逐向）；fair=PSC Shin；彩池 EV=
fair×0.65/份额−1；psc 分期=该行 PSC+AvgC 双全；avgc 分期退化（fair=份额
同源，EV≡−35%）只作标注。Shin 独立实现（common.shin_probs）。
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common

COLD_MAX = 0.25
RETURN_RATE = 0.65
BREAKEVEN = 1.0 / RETURN_RATE
LEAGUES = ("B1", "D1", "E0", "E1", "F1", "I1", "N1", "P1", "SC0", "SP1", "T1")


def main() -> None:
    con = common.business_ro()
    rows = con.execute(
        f"""
        SELECT competition, season, fthg, ftag, ftr,
               psc_home, psc_draw, psc_away,
               avgc_home, avgc_draw, avgc_away
        FROM hist_matches
        WHERE competition IN ({','.join('?' * len(LEAGUES))})
        """,
        LEAGUES,
    ).fetchall()
    con.close()
    print(f"11 联赛行: {len(rows)}")

    stage = {"psc": 0, "avgc": 0, "none": 0}
    cand_rows = []  # (stage, fair_k, share_k, hit)
    ratios_psc: list[float] = []
    by_season_pos: dict[str, list[float]] = {}
    for r in rows:
        psc = (r["psc_home"], r["psc_draw"], r["psc_away"])
        avgc = (r["avgc_home"], r["avgc_draw"], r["avgc_away"])
        ftr = r["ftr"]
        if all(v is not None for v in psc) and all(v is not None for v in avgc):
            st = "psc"
        elif all(v is not None for v in avgc):
            st = "avgc"
        else:
            stage["none"] += 1
            continue
        stage[st] += 1
        share = common.shin_probs(avgc)
        if share is None:
            continue
        if st == "psc":
            fair = common.shin_probs(psc)
            if fair is None:
                continue
        else:
            fair = share  # 退化分期：同源
        hitv = (1.0 if ftr == "H" else 0.0, 1.0 if ftr == "D" else 0.0, 1.0 if ftr == "A" else 0.0)
        for k in range(3):
            if share[k] < COLD_MAX:
                cand_rows.append((st, fair[k], share[k], hitv[k], r["season"]))
                if st == "psc":
                    ratios_psc.append(fair[k] / share[k])
                    by_season_pos.setdefault(r["season"], []).append(fair[k] / share[k])

    psc_cands = [c for c in cand_rows if c[0] == "psc"]
    avgc_cands = [c for c in cand_rows if c[0] == "avgc"]
    print(f"分期: psc={stage['psc']} avgc={stage['avgc']} 无锚={stage['none']}")
    for name, cands in (("psc", psc_cands), ("avgc", avgc_cands)):
        if not cands:
            continue
        ratios = np.array([c[1] / c[2] for c in cands])
        evs = RETURN_RATE * ratios - 1.0
        pos = int((evs > 0).sum())
        print(
            f"[{name}] 候选 {len(cands)}；彩池 EV>0 = {pos}；"
            f"max fair/份额={ratios.max():.4f}（盈亏线 {BREAKEVEN:.4f}）"
            f" p50={np.percentile(ratios, 50):.4f} p99={np.percentile(ratios, 99):.4f}"
        )
        hits = sum(c[3] for c in cands)
        mean_fair = np.mean([c[1] for c in cands])
        mean_share = np.mean([c[2] for c in cands])
        print(
            f"   实测校准：命中 {hits / len(cands):.4%} ≈ fair 均值 {mean_fair:.4%}"
            f" ≈ 份额均值 {mean_share:.4%}（n 场向观测）"
        )
    seas = sorted(by_season_pos)
    print("分季 max fair/份额:", " ".join(f"{s}:{max(v):.3f}" for s, v in zip(seas, [by_season_pos[s] for s in seas])))


if __name__ == "__main__":
    main()
