"""E-02 复算：H1 FLB 侵蚀差——JC 收盘 vs 欧收盘同场同向配对 ROI 差。

载重数字（账面）：长赔带 p<0.30 侵蚀差 −4.67%±0.12%（n=10,221）；
差中差 +1.45pp z=+11.9；全局侵蚀差 −6.12%（avgc 主锚）。
口径（源报告 07-h1-flb-erosion-gap）：JC=bronze had 末条收盘（2019+）；
欧锚=match_features avgc/psc/srct；fair=比例化去水；侵蚀差=
mean((o_jc−o_eu)×hit)（场聚类）；长赔带=fair<0.30 跨向合并。
独立实现，详见 common.py 头注。
"""

from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common

BUCKETS = [(0.0, 0.10, "<10%"), (0.10, 0.20, "10-20%"), (0.20, 0.30, "20-30%"),
           (0.30, 0.40, "30-40%"), (0.40, 0.50, "40-50%"), (0.50, 0.65, "50-65%"),
           (0.65, 1.01, "65%+")]


def main() -> None:
    jc_rows = common.load_jc_bronze("2019")
    eu_rows = common.load_euro("2019-01-01")
    pairs, stats = common.pair_jc_euro(jc_rows, eu_rows)
    print("配对漏斗:", stats)

    for anchor, label in (("avgc", "avgc（FD 欧收盘共识·主锚）"), ("psc", "psc（Pinnacle 收盘）"), ("srct", "srct（源T 收盘共识）")):
        sample = []
        for i, (jc, eu) in enumerate(pairs):
            euro = eu[anchor]
            fair = common.devig_ratio(euro)
            if fair is None:
                continue
            hit = (1.0 if eu["hg"] > eu["ag"] else 0.0, 1.0 if eu["hg"] == eu["ag"] else 0.0, 1.0 if eu["hg"] < eu["ag"] else 0.0)
            sample.append((i, jc, eu, euro, fair, hit))
        if not sample:
            print(f"\n== 锚 {label}：空样本（close1x2_cons 现为概率列 ≤1，odds 有效性门全拒——与源 raw-output 一致，该锚当时亦无输出）")
            continue
        seasons = sorted({eu["season"] for _, _, eu, _, _, _ in sample})
        print(f"\n== 锚 {label} n={len(sample)} 场  季 {seasons[0]}…{seasons[-1]}")

        rows_all = [(i, (jc["odds"][k] - euro[k]) * hit[k]) for i, jc, eu, euro, fair, hit in sample for k in range(3)]
        g, se_g, n_g = common.clustered_mean_se(rows_all)
        print(f"全局侵蚀差: {g:+.4%} ± {se_g:.4%}（场聚类 n={n_g}）")

        cells: dict[tuple[int, int], list[tuple[int, float]]] = defaultdict(list)
        for i, jc, eu, euro, fair, hit in sample:
            for k in range(3):
                for bi, (lo, hi, _) in enumerate(BUCKETS):
                    if lo <= fair[k] < hi:
                        cells[(bi, k)].append((i, (jc["odds"][k] - euro[k]) * hit[k]))
                        break
        print(f"{'桶':>8} {'向':>3} {'n':>6} {'侵蚀差':>9} {'差中差':>9} {'z':>6}")
        for k in range(3):
            for bi, (_, _, lab) in enumerate(BUCKETS):
                rows = cells.get((bi, k), [])
                if len(rows) < 30:
                    continue
                m, se, n = common.clustered_mean_se(rows)
                z = (m - g) / se if se > 0 else float("nan")
                print(f"{lab:>8} {'hda'[k]:>3} {n:>6} {m:>+9.3%} {m - g:>+9.3%} {z:>+6.2f}")

        long_rows = [(i, (jc["odds"][k] - euro[k]) * hit[k]) for i, jc, eu, euro, fair, hit in sample for k in range(3) if fair[k] < 0.30]
        m, se, n = common.clustered_mean_se(long_rows)
        z = (m - g) / se if se > 0 else float("nan")
        print(f"[长赔带 p<0.30] n={n} 场：侵蚀差 {m:+.3%} ± {se:.3%}，差中差 {m - g:+.3%}，z(对全局)={z:+.2f}")
        for k in range(3):
            rows = [(i, (jc["odds"][k] - euro[k]) * hit[k]) for i, jc, eu, euro, fair, hit in sample if fair[k] < 0.30]
            mk, sek, nk = common.clustered_mean_se(rows)
            print(f"  {'hda'[k]}: n={nk} 差 {mk:+.3%} ± {sek:.3%}")

        ev_rows = [(i, jc["odds"][k] * hit[k] - 1.0) for i, jc, eu, euro, fair, hit in sample for k in range(3) if jc["odds"][k] * fair[k] > 1.0]
        m2, se2, n2 = common.clustered_mean_se(ev_rows)
        print(f"[假想 EV 口径 jc×fair>1] n={n2} 场，实现 ROI {m2:+.3%} ± {se2:.3%}")

        by_season: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for i, jc, eu, euro, fair, hit in sample:
            for k in range(3):
                if fair[k] < 0.30:
                    by_season[eu["season"]].append((i, (jc["odds"][k] - euro[k]) * hit[k]))
        seas = []
        for s in sorted(by_season):
            ms, ses, ns = common.clustered_mean_se(by_season[s])
            seas.append(f"{s}:{ms:+.3%}(n={ns})")
        print("[逐季长赔带]", " ".join(seas))


if __name__ == "__main__":
    main()
