"""E-06 复算：收盘有效性——PSC Shin 收盘隐含 vs 实际命中（h/d/a 三向）。

载重数字（账面）：35,754 场 PSH+PSC 双全（1920-2526）= 107,262 选向观测；
h 44.20/44.11、d 24.95/24.93、a 30.85/30.96（偏差 <0.2pp）。
口径（源报告 issues/54-drift-replay + evaluation/drift_replay.py 口径转录）：
行=hist_matches PSH+PSC 双全；p_close=Shin(PSC)；实测视角= ftr 命中频率。
独立实现（Shin 自实现见 common.shin_probs）。
"""

from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common

FTR_MAP = {"h": "H", "d": "D", "a": "A"}


def main() -> None:
    con = common.business_ro()
    rows = con.execute(
        """
        SELECT competition, season, ftr,
               psc_home, psc_draw, psc_away,
               psh_home, psh_draw, psh_away
        FROM hist_matches
        WHERE psc_home IS NOT NULL AND psc_draw IS NOT NULL AND psc_away IS NOT NULL
          AND psh_home IS NOT NULL AND psh_draw IS NOT NULL AND psh_away IS NOT NULL
          AND ftr IS NOT NULL AND ftr != ''
        """
    ).fetchall()
    con.close()
    n_fx = len(rows)
    seasons = sorted({r["season"] for r in rows})
    print(f"PSH+PSC 双全场: {n_fx}（季 {seasons[0]}…{seasons[-1]}）→ 选向观测 {n_fx * 3}")

    acc = {k: [0.0, 0] for k in ("h", "d", "a")}  # sum p_close, hits
    for r in rows:
        probs = common.shin_probs((r["psc_home"], r["psc_draw"], r["psc_away"]))
        if probs is None:
            continue
        for pos, k in enumerate(("h", "d", "a")):
            acc[k][0] += probs[pos]
            acc[k][1] += 1 if r["ftr"] == FTR_MAP[k] else 0
    n = n_fx
    print(f"{'向':>3} {'p_close 均值':>12} {'实际命中':>10} {'偏差(pp)':>9}")
    for k in ("h", "d", "a"):
        p_close = acc[k][0] / n
        actual = acc[k][1] / n
        print(f"{k:>3} {p_close:>12.4%} {actual:>10.4%} {(actual - p_close) * 100:>+9.2f}")
    total = sum(acc[k][1] for k in ("h", "d", "a"))
    print(f"命中合计 {total}/{n}（结果完备性检验）")


if __name__ == "__main__":
    main()
