"""E-25 复算：H7 滚存期 EV 回放（14场胜负彩，随机票期望返奖率）。

载重数字（账面）：十年 1,623 期（滚存 355+非滚存 1,268）随机票主指标
差 +0.0942±0.0691（z=+1.36 未过 2SE）；滚存组 0.4956±0.061（95% 上界
≈0.62）、非滚存 0.4014；池面实现派彩率差 +0.2104±0.0167（z=+12.63）。
口径（源报告 06-h7-rollover-ev-replay 口径转录）：期 N 滚存 ⟺ 前驱期
pool_after>0（period_no 数值序）；随机票 EV = P14×(一等奖金/注数) +
P13×(二等奖金/注数)，P14=(1/3)^14、P13=14·(1/3)^13·(2/3)，除以 2 元注金；
0 中奖奖级按 0 计；派彩率=Σ一二等奖金/销量；Welch 期次级。
注：源口径无 20% 偶然所得税项（账本 E-25 行的「税后」前缀与源报告
措辞不符，见产出差异注记）。独立实现。
"""

from __future__ import annotations

import json
import math
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common

STAKE = 2.0
P14 = (1.0 / 3.0) ** 14
P13 = 14 * (1.0 / 3.0) ** 13 * (2.0 / 3.0)


def main() -> None:
    con = common.business_ro()
    rows = con.execute(
        """
        SELECT p.period_no, s.rollover_in, s.prize_tiers
        FROM pool_periods p JOIN pool_states s ON s.pool_period_id = p.id
        WHERE p.market_code='ttt14' AND s.prize_tiers IS NOT NULL
        ORDER BY CAST(p.period_no AS INTEGER)
        """
    ).fetchall()
    con.close()
    print(f"带 prize_tiers 的 ttt14 期次: {len(rows)}（{rows[0]['period_no']}…{rows[-1]['period_no']}）")

    periods = []
    bad = 0
    for r in rows:
        state = json.loads(r["prize_tiers"])
        sfc = state.get("sfc") or {}
        sale = sfc.get("total_sale")
        if not sale or sale <= 0:
            bad += 1
            continue
        ev1 = ev2 = paid = 0.0
        for t in sfc.get("tiers") or []:
            prize, count = t.get("prize_amount"), t.get("stake_count")
            level = str(t.get("level") or "")
            if prize is None or ("一" not in level and "二" not in level):
                continue
            paid += prize
            if count and count > 0:
                per_winner = prize / count
                if "一" in level:
                    ev1 = P14 * per_winner
                elif "二" in level:
                    ev2 = P13 * per_winner
        periods.append(
            {
                "no": r["period_no"],
                "pool_after": r["rollover_in"] or 0.0,
                "ev": (ev1 + ev2) / STAKE,
                "ev1": ev1 / STAKE,
                "ev2": ev2 / STAKE,
                "payout": paid / sale,
            }
        )
    print(f"可分析期次 {len(periods)}（缺销量/奖级排除 {bad}）")

    # 前驱 pool_after>0 → 滚存期
    sample = []
    for i in range(1, len(periods)):
        p = dict(periods[i])
        p["roll_in"] = periods[i - 1]["pool_after"]
        sample.append(p)
    rollover = [p for p in sample if p["roll_in"] > 0]
    normal = [p for p in sample if p["roll_in"] <= 0]
    print(f"样本 {len(sample)} = 滚存 {len(rollover)} + 非滚存 {len(normal)}（首期无前驱排除）")

    for label, key in (("①随机票期望返奖率（主指标）", "ev"), ("②池面实现派彩率（硬账）", "payout")):
        a = np.array([p[key] for p in rollover])
        b = np.array([p[key] for p in normal])
        d, se = common.welch_diff(a, b)
        print(f"\n{label}")
        print(f"  滚存期:   n={len(a)} 均值 {a.mean():.4f} 中位 {np.median(a):.4f}")
        print(f"  非滚存期: n={len(b)} 均值 {b.mean():.4f} 中位 {np.median(b):.4f}")
        print(f"  差(滚存−非滚存) = {d:+.4f} ± {se:.4f}（z={d / se:+.2f}，门=差>2SE）")
    a = np.array([p["ev"] for p in rollover])
    se_a = a.std(ddof=1) / math.sqrt(len(a))
    print(f"\n[判读线] 滚存组随机票返奖率 {a.mean():.4f} ± {se_a:.4f}（95% 上界 ≈{a.mean() + 1.96 * se_a:.2f}）")
    r1 = np.array([p["ev1"] for p in rollover]); n1 = np.array([p["ev1"] for p in normal])
    r2 = np.array([p["ev2"] for p in rollover]); n2 = np.array([p["ev2"] for p in normal])
    print(f"[奖级分解] 一等奖贡献: 滚存 {r1.mean():.4f} vs 非滚存 {n1.mean():.4f}；"
          f"二等奖: 滚存 {r2.mean():.4f} vs 非滚存 {n2.mean():.4f}")


if __name__ == "__main__":
    main()
