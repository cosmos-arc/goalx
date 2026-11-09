"""E-30 复算：σ/功效推导——JC had 收盘全宇宙每注 σ 与 ROI/CLV 门注数。

载重数字（账面）：σ=1.525（e=2% 列；207,807 个赔率值、69,269 场）；
ROI 门 e=2% 需 23,270 注；CLV 门快 364–1517 倍（=(σ/σ_CLV)²，
σ_CLV∈{4%,8%}，σ 取逐 e 列 1.525/1.538/1.558）。
口径（源报告 vision-20261108/research/09 §1/§2.1 公式转录，独立实现）：
收盘=每场 jc_sp_change_event had 末次报价（arg_max published_at）；
σ²=mean_o[p·o²−2po+1−e²]，p=(1+e)/o；n_ROI=(2σ/e)²；倍率=(σ/σ_CLV)²。
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common


def sigma_mixture(odds: np.ndarray, edge: float) -> float:
    p = (1.0 + edge) / odds
    var = np.mean(p * odds * odds - 2 * p * odds + 1.0 - edge * edge)
    return float(np.sqrt(var))


def main() -> None:
    con = common.corpus_ro()
    rows = con.sql(
        """
        WITH last_quote AS (
          SELECT match_id,
                 arg_max(odds_h, published_at) AS oh,
                 arg_max(odds_d, published_at) AS od,
                 arg_max(odds_a, published_at) AS oa
          FROM jc_sp_change_event WHERE playtype='had'
          GROUP BY match_id
        )
        SELECT oh, od, oa FROM last_quote WHERE oh > 1 AND od > 1 AND oa > 1
        """
    ).fetchall()
    con.close()
    odds = np.array([o for r in rows for o in r], dtype=float)
    booksum = np.mean([sum(1.0 / o for o in r) for r in rows])
    print(f"JC had 收盘场数 {len(rows)}；赔率值 {len(odds)}；庄水(Σ1/o) 均值 {booksum:.3f}")
    print(f"赔率分布: p50 {np.percentile(odds, 50):.2f} / 均值 {odds.mean():.2f} / p95 {np.percentile(odds, 95):.2f} / max {odds.max():.1f}")

    print(f"\n{'e':>5} {'σ':>7} {'n_ROI=(2σ/e)²':>14} {'月@600注':>9} {'80%功效 n':>10}")
    sigmas = {}
    for e in (0.10, 0.05, 0.02):
        s = sigma_mixture(odds, e)
        sigmas[e] = s
        n_roi = (2 * s / e) ** 2
        n80 = ((1.96 + 0.8416) * s / e) ** 2
        print(f"{e:>5.0%} {s:>7.3f} {n_roi:>14,.0f} {n_roi / 600:>8.1f} {n80:>10,.0f}")

    print("\nCLV 门（n_CLV=(2σ_CLV/e)²）与 ROI/CLV 倍率 ((σ/σ_CLV)²):")
    for e, s in sigmas.items():
        for sclv in (0.04, 0.08):
            n_clv = (2 * sclv / e) ** 2
            print(f"  e={e:.0%} σ={s:.3f} σ_CLV={sclv:.0%}: n_CLV={n_clv:,.0f} 倍率={(s / sclv) ** 2:,.0f}×")


if __name__ == "__main__":
    main()
