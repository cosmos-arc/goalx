"""
只读计税视图（measure-integrity 票 08：净到账口径）。

2024-09-01 起施行的彩票个税口径：同一人、同一期、同一游戏的中奖收入
**归集计税**——一次中奖收入不超过 1 万元暂免征收，超过则按全额 20%
（偶然所得）。

研究假设边界（report-audit-20261027 票 06，2026-10-28 核验落档）：
"business_date ≡ 官方期次"在当前冻结面**结构性成立**——would_bet 行是
逐场 flat 1 元/注（每行=单腿票），"票内最晚比赛编号日期"归集规则退化为
该场自身编号日期；主库 396 个期次编号（周六026 格式）星期与
business_date 零错配、159 行冻结账零跨期次组。两个已知边界：
(1) 若引入多腿过关票，分组键必须换成票内 max(编号日期)，届时本视图
分组口径需重核；(2) "同一人"假设在实盘多账户下需真实分组，纸面面以
系统自身为单一纳税人假设测算。

纯读面：不写表、不改结算（毛奖口径不动）；只做"若按此组归集"的
假设税测算，供日终报告并列税前/税后。完整引擎 park（裁决⑦：等真金
闭环）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

TAX_FREE_THRESHOLD = 10_000.0  # 单次中奖收入免征线（元）
INCIDENTAL_TAX_RATE = 0.20  # 超限全额计税（偶然所得）


def tax_on_prize(total_prize: float) -> tuple[float, float]:
    """计税组奖金合计 → (应扣税, 净到账)；≤1 万免、超限全额 20%。"""
    if total_prize <= TAX_FREE_THRESHOLD:
        return 0.0, total_prize
    tax = round(total_prize * INCIDENTAL_TAX_RATE, 2)
    return tax, round(total_prize - tax, 2)


def frozen_tax_face(conn: sqlite3.Connection, business_date: str) -> dict[str, Any]:
    """
    冻结 would_bet 面的计税组测算（同业务日=同期同游戏同人的假设组）。

    奖金口径：命中注的平注毛奖（flat stake=1 元/注 → 毛奖=odds 元，
    含本金——公告口径按中奖收入全额计）。未结算/未命中注不进奖金；
    净利=净到账−注数（本金）。标注为假设测算，非结算事实。
    """
    row = conn.execute(
        " ".join(
            (
                "SELECT COUNT(*) n, SUM(CASE WHEN settled_outcome = 'won'",
                "THEN odds ELSE 0 END) prize,",
                "SUM(CASE WHEN settled_outcome IN ('won', 'lost')",
                "THEN 1 ELSE 0 END) settled_n",
                "FROM frozen_decisions",
                "WHERE business_date = ? AND decision = 'would_bet'",
            )
        ),
        (business_date,),
    ).fetchone()
    n_would = int(row[0] or 0)
    settled_n = int(row[2] or 0)
    prize_gross = float(row[1] or 0.0)
    tax, net = tax_on_prize(prize_gross)
    return {
        "note": (
            "假设测算：同业务日 would_bet 命中注按计税组归集（≤1 万免、"
            "超限全额 20%，2024-09-01 口径）；毛奖含本金（flat 1 元/注）"
        ),
        "would_bet_cards": n_would,
        "settled_cards": settled_n,
        "prize_gross": round(prize_gross, 2),
        "tax": tax,
        "net_after_tax": net,
        "net_profit_flat": round(net - settled_n, 2),
    }
