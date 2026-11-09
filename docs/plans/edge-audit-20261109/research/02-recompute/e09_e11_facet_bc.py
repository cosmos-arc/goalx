"""E-09/E-11 复算：EM-1 facet B1 陈盘 + facet C3 联赛（T=开球−2h，PIT Pinnacle 锚）。

载重数字（账面）：
- E-09 B1 6h×1pp：EV −51.2%（z=−3.12，n_sel=64，n_fx=10,461，搜索窗）。
- E-11 C3 荷甲：搜索窗 +123.5%（z=+2.15，n_sel=28）→ 验证窗 −100%
  （4 注 0 中，n_fx=156）。
口径（源报告 10-em1-facet-bc）：窗=搜索 y0≤2021/验证 2022-23/确认 2024-25
（season 前 4 字符）；T=kickoff(+08)−2h；JC 报价@T=轨迹 ≤T 末值；fair=Pin@T
比例化去水；B1 触发=|fair@T−fair@(T−6h)|≥1pp ∧ JC 该向 6h 内未变 ∧
odds_jc@T×fair@T>1；EV=场聚类 mean(odds×hit−1)，z=mean/SE；C3=联赛×价值侧
（odds×fair>1）。独立实现。
"""

from __future__ import annotations

import datetime as dt
import sys
from collections import defaultdict

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common

T_OFFSET = dt.timedelta(hours=2)
CST = dt.timezone(dt.timedelta(hours=8))


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


def main() -> None:
    jc_rows = common.load_jc_bronze("2016")
    eu_rows = common.load_euro("2016-01-01")
    pairs, stats = common.pair_jc_euro(jc_rows, eu_rows)
    print("配对漏斗:", stats)
    jc_had = common.load_jc_had_traj([jc["sid"] for jc, _ in pairs])
    pin = common.load_pin1x2_traj([eu["sid"] for _, eu in pairs])

    rows = []
    win_count: dict[str, int] = defaultdict(int)
    for fx, (jc, eu) in enumerate(pairs):
        w = window_of(eu["season"])
        if w is None or eu["kickoff"] is None:
            continue
        T = eu["kickoff"].replace(tzinfo=CST) - T_OFFSET
        jtr = jc_had.get(jc["sid"])
        ptr = pin.get(eu["sid"])
        if not jtr or not ptr:
            continue
        if T < jtr[0][0]:
            continue
        q_jc = common.quote_at(jtr, T)
        q_pin = common.quote_at(ptr, T)
        if q_jc is None or q_pin is None:
            continue
        fair = common.devig_ratio(q_pin[1])
        if fair is None:
            continue
        hit = (1.0 if eu["hg"] > eu["ag"] else 0.0, 1.0 if eu["hg"] == eu["ag"] else 0.0, 1.0 if eu["hg"] < eu["ag"] else 0.0)
        p6 = common.quote_at(ptr, T - dt.timedelta(hours=6))
        p24 = common.quote_at(ptr, T - dt.timedelta(hours=24))
        f6 = common.devig_ratio(p6[1]) if p6 else None
        f24 = common.devig_ratio(p24[1]) if p24 else None
        j6 = common.quote_at(jtr, T - dt.timedelta(hours=6))
        j24 = common.quote_at(jtr, T - dt.timedelta(hours=24))
        for k in range(3):
            rows.append(
                {
                    "fx": fx, "window": w, "league": eu["league"], "k": k,
                    "odds": q_jc[1][k], "fair": fair[k], "hit": hit[k],
                    "d6": (fair[k] - f6[k]) if f6 else None,
                    "d24": (fair[k] - f24[k]) if f24 else None,
                    "unf6": j6 is not None and j6[1][k] == q_jc[1][k],
                    "unf24": j24 is not None and j24[1][k] == q_jc[1][k],
                }
            )
            win_count[w] += 1
    print(f"T 时刻行 {len(rows)}（窗分布 {dict(sorted(win_count.items()))}）")

    by_win: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_win[r["window"]].append(r)

    def ev_stats(bet: list[dict]) -> tuple:
        m, se, n_fx_sel = common.clustered_mean_se([(r["fx"], r["odds"] * r["hit"] - 1.0) for r in bet])
        z = m / se if se and se > 0 and len(bet) >= 2 else float("nan")
        return m, se, z, len(bet), n_fx_sel

    # E-09：B1 陈盘 4 臂（搜索窗），账面主数=6h×1pp
    print("\n[E-09 B1 陈盘（搜索窗）]")
    for label, dkey, uf, thr in (("6h×1pp", "d6", "unf6", 0.01), ("6h×2pp", "d6", "unf6", 0.02),
                                 ("24h×1pp", "d24", "unf24", 0.01), ("24h×2pp", "d24", "unf24", 0.02)):
        denom = [r for r in by_win["search"] if r[dkey] is not None]
        bet = [r for r in denom if abs(r[dkey]) >= thr and r[uf] and r["odds"] * r["fair"] > 1.0]
        n_fx_denom = len({r["fx"] for r in denom})
        m, se, z, n_sel, _ = ev_stats(bet)
        hits = sum(1 for r in bet if r["hit"] > 0)
        print(f"  {label}: n_fx(分母)={n_fx_denom} n_sel={n_sel} 命中={hits} EV={m:+.3%} ± {se:.3%} z={z:+.2f}")

    # E-11：C3 联赛×价值侧
    print("\n[E-11 C3 联赛×价值侧（搜索窗 → 验证窗）]")
    for lg in sorted({r["league"] for r in by_win["search"]}):
        out = []
        for wname in ("search", "validation"):
            sub = [r for r in by_win[wname] if r["league"] == lg]
            bet = [r for r in sub if r["odds"] * r["fair"] > 1.0]
            m, se, z, n_sel, _ = ev_stats(bet)
            hits = sum(1 for r in bet if r["hit"] > 0)
            out.append(f"{wname}: n_fx={len({r['fx'] for r in sub})} n_sel={n_sel} 命中={hits} EV={m:+.3%} z={z:+.2f}")
        print(f"  {lg}: " + " → ".join(out))


if __name__ == "__main__":
    main()
