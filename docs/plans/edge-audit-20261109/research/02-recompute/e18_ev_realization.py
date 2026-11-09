"""E-18 复算：EV 实现率（十年回测 t19-decade-full，run_id=5）。

载重数字（账面）：EV 实现率 −0.42（账面 +47.6 万 vs 实现 −19.9 万）。
口径（decade-closeout.md → decade-skill.json，evaluation/decade_skill.py
_ev_stats 口径转录）：实现率 = Σ利润 / Σ(ev×stake)，分母≤0 置空；逐注
backtest_bets 行直加。run23 交叉验证同式（落树 json −0.3663）。
独立实现：直接从 backtest_bets 行重算，不经 decade_skill 代码。
"""

from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common


def realize(run_id: int) -> None:
    con = common.business_ro()
    n, exp, real, staked = con.execute(
        "SELECT count(*), sum(ev*stake), sum(profit), sum(stake) FROM backtest_bets WHERE run_id=?",
        (run_id,),
    ).fetchone()
    label = con.execute("SELECT label, status, created_at FROM backtest_runs WHERE id=?", (run_id,)).fetchone()
    by_status = con.execute(
        "SELECT status, count(*), sum(ev*stake), sum(profit) FROM backtest_bets WHERE run_id=? GROUP BY status",
        (run_id,),
    ).fetchall()
    con.close()
    print(f"== run {run_id}（{label['label']} {label['status']} {label['created_at']}）")
    print(f"   n_bets={n} staked={staked:,.2f} Σ(ev×stake)={exp:,.2f} Σprofit={real:,.2f}")
    print(f"   ROI={real / staked:+.4%}  EV 实现率={real / exp:+.4f}")
    for s, cnt, e, p in by_status:
        print(f"   status={s}: n={cnt} Σ(ev×stake)={e:,.2f} Σprofit={p:,.2f}")


def main() -> None:
    realize(5)  # t19-decade-full（closeout 引用面）
    print()
    realize(23)  # 交叉验证：与落树 decade-skill.json（−0.3663）同 run


if __name__ == "__main__":
    main()
