"""E-08 复算：H4 调盘滞后——Pinnacle 1x2 动 → JC had 同向首动。

载重数字（账面）：滞后>15min 案例率 63.6%；干净口径 15min 未跟概率 92.5%
（=1−14,057/186,164）；16,828 场 / 186,164 合格欧盘动；跟随率 71.2%；
滞后中位 278min（p90=1878min）。
口径（源报告 08-h4-h6-historical）：配对=2016+ 三轮；欧盘动=Pin 1x2 相邻
快照任一向 |Δ(1/o)|≥0.5pp，时刻=后快照，须 ∈[JC首价+5min, JC末价−60min]；
JC 窗<2h 场剔除；JC 跟随=事件后首个同向变价（任意幅度）；滞后=t_jc−t_eu。
独立实现。
"""

from __future__ import annotations

import datetime as dt
import sys
from collections import Counter, defaultdict

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common

EURO_MOVE_MIN = 0.005
HEAD = dt.timedelta(minutes=5)
TAIL = dt.timedelta(minutes=60)
LAG_GATE = dt.timedelta(minutes=15)


def main() -> None:
    jc_rows = common.load_jc_bronze("2016")
    eu_rows = common.load_euro("2016-01-01")
    pairs, stats = common.pair_jc_euro(jc_rows, eu_rows)
    print("配对漏斗:", stats)

    jc_had = common.load_jc_had_traj([jc["sid"] for jc, _ in pairs])
    pin = common.load_pin1x2_traj([eu["sid"] for _, eu in pairs])
    print(f"轨迹装载：Pinnacle 1x2 {len(pin)} 场；JC had {len(jc_had)} 场")

    cases: list[dict] = []
    n_fx_used = excl_short = excl_tail = 0
    for fx, (jc, eu) in enumerate(pairs):
        ptr = pin.get(eu["sid"])
        jtr = jc_had.get(jc["sid"])
        if not ptr or not jtr or len(ptr) < 2 or len(jtr) < 2:
            continue
        t0, t1 = jtr[0][0], jtr[-1][0]
        if t1 - t0 < dt.timedelta(hours=2):
            excl_short += 1
            continue
        n_fx_used += 1
        for i in range(1, len(ptr)):
            t_prev, q_prev = ptr[i - 1]
            t, q = ptr[i]
            if t < t0 + HEAD or t > t1 - TAIL:
                if t0 + HEAD <= t <= t1:
                    excl_tail += 1
                continue
            for k in range(3):
                d = 1.0 / q[k] - 1.0 / q_prev[k]
                if abs(d) < EURO_MOVE_MIN:
                    continue
                # JC 事件后首个同向变价
                cls, lag = "no_follow", float("nan")
                prev_q = None
                for t_j, q_j in jtr:
                    if t_j <= t:
                        prev_q = q_j
                        continue
                    dj = 1.0 / q_j[k] - 1.0 / prev_q[k]
                    if dj != 0:
                        if (dj > 0) == (d > 0):
                            lag = (t_j - t).total_seconds() / 60.0
                            cls = "le15" if lag <= 15 else ("15_60" if lag <= 60 else "gt60")
                            break
                    prev_q = q_j
                cases.append({"fx": fx, "season": eu["season"], "cls": cls, "lag": lag})
    ct = Counter(c["cls"] for c in cases)
    n = len(cases)
    follow = n - ct["no_follow"]
    lag15 = ct["15_60"] + ct["gt60"]
    print(f"\n样本账：用场 {n_fx_used}；JC窗<2h 剔除 {excl_short}；窗尾剔除欧盘动 {excl_tail}")
    print(f"合格欧盘动 n={n}")
    print(f"跟随率 {follow / n:.1%}（{follow}）")
    print(f"滞后>15min 案例率 {lag15 / n:.1%}（{lag15}）")
    print(f"15min 内未跟概率（干净口径） {1 - ct['le15'] / n:.1%}（le15={ct['le15']} → 1−{ct['le15']}/{n}）")
    print(f"分类：≤15min {ct['le15']} / 15-60 {ct['15_60']} / >60 {ct['gt60']} / 无跟随 {ct['no_follow']}")
    lags = [c["lag"] for c in cases if c["cls"] != "no_follow"]
    if lags:
        print(f"滞后分布（跟随者）：中位 {np.median(lags):.0f}min / p90 {np.percentile(lags, 90):.0f}min / max {max(lags):.0f}min")
    by_season: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        by_season[c["season"]].append(c)
    rows = []
    for s, cs in sorted(by_season.items()):
        if len(cs) < 50:
            continue
        g15 = sum(1 for c in cs if c["cls"] in ("15_60", "gt60"))
        rows.append(f"{s}: n={len(cs):>5} 滞后>15m率 {g15/len(cs):>5.1%}")
    print("逐季（n≥50）：" + " | ".join(rows))


if __name__ == "__main__":
    main()
