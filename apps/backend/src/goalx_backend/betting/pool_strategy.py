"""
彩池搏冷策略（票 pool-v2/02、03；review-20260928 票 02 自 api/pool.py 下沉）。

归属（ADR-0008）：选注决策逻辑归 betting 域；纯函数、无表、无 SQL——
期次/份额取数与读模型装配（概率源策略）在 data/pool.py（含视图模型
PoolMatchView/PoolSelectionView，票 08 与装配同住所），api/pool.py 只留
路由与 HTTP 形状；本模块消费视图做纯决策。

口径（与期次详情页一致，单一正典在数据域）：
- 冷门阈值 COLD_SHARE_MAX、返奖率 POOL_RETURN_RATE 均自 data.pool 直引；
- 估计派彩赔率 = POOL_RETURN_RATE ÷ 各场份额连乘（单次抽水近似，
  未建模分彩与 price impact）；EV = 概率 × 赔率 − 1。
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel

from goalx_backend.data.pool import (
    COLD_SHARE_MAX,
    POOL_RETURN_RATE,
    PoolMatchView,
)

STAKE_PER_UNIT = 2.0  # 任9/14场 每注 ¥2
PICK9_COUNT = 9

COLD_CALIBER_TEXT = (
    "变体 = 基础票按「冷选项 EV − 基础向 EV」降序贪心替换 1..N 处"
    "（冷选项 = 份额 <25% 且 EV 为正）；"
    "命中概率 = 各场概率连乘；估计派彩赔率 = 返奖率 65% ÷ 各场份额连乘"
    "（单次抽水近似，未建模分彩与 price impact）；EV = 概率 × 赔率 − 1。"
)

TARGET_CALIBER_TEXT = (
    "任9 选场 = 各场最高概率的前 9（log 概率和最大化贪心）；"
    "派彩不足目标时按冷选项 EV 增益降序替换抬赔率（与生成器同口径）；"
    "估计派彩 = 返奖率 65% ÷ 份额连乘 × ¥2/注（单次抽水近似）——"
    '实际派彩随最终池变，输出是"若命中估计得 X"，不承诺目标达成。'
)


class ColdSwapView(BaseModel):
    """一处冷门替换。"""

    match_seq: int
    from_code: str
    to_code: str
    ev_gain: float  # to.ev − from.ev


class ColdTicketView(BaseModel):
    """一张票的估值：命中概率 / 估计派彩赔率 / EV。"""

    picks: dict[str, str]  # str(seq) → h/d/a
    swaps: list[ColdSwapView] = []
    hit_prob: float | None = None  # ∏p；任一场缺概率 → None
    est_odds: float | None = None  # POOL_RETURN_RATE ÷ ∏share（单次抽水口径）
    ev: float | None = None  # hit_prob × est_odds − 1


def ticket_view(
    picks: dict[int, str],
    views: list[PoolMatchView],
    swaps: list[ColdSwapView] | None = None,
) -> ColdTicketView:
    """票面 → 估值视图（任一场缺概率/份额时概率/赔率/EV 为 None，picks 照返）。"""
    by_seq = {v.match_seq: v for v in views}
    hit_prob: float | None = 1.0
    est_odds: float | None = None
    share_prod = 1.0
    for seq, code in sorted(picks.items()):
        sel = next((s for s in by_seq[seq].selections if s.code == code), None)
        if sel is None or sel.prob is None or sel.share is None:
            hit_prob, est_odds = None, None
            break
        hit_prob *= sel.prob
        share_prod *= sel.share
    if hit_prob is not None:
        est_odds = POOL_RETURN_RATE / share_prod
    ev = (
        hit_prob * est_odds - 1
        if (hit_prob is not None and est_odds is not None)
        else None
    )
    return ColdTicketView(
        picks={str(seq): code for seq, code in sorted(picks.items())},
        swaps=swaps or [],
        hit_prob=round(hit_prob, 6) if hit_prob is not None else None,
        est_odds=round(est_odds, 2) if est_odds is not None else None,
        ev=round(ev, 4) if ev is not None else None,
    )


def default_base_picks(views: list[PoolMatchView]) -> dict[int, str]:
    """基础票缺省：各场最高概率向（无概率的场跳过）。"""
    picks: dict[int, str] = {}
    for view in views:
        prob_sel = max(
            (s for s in view.selections if s.prob is not None),
            key=lambda s: s.prob or 0,
            default=None,
        )
        if prob_sel is not None:
            picks[view.match_seq] = prob_sel.code
    return picks


def cold_candidates(
    views: list[PoolMatchView], base_picks: dict[int, str]
) -> list[tuple[float, int, str, str]]:
    """冷替换候选（gain, seq, from, to），EV 增益降序；与判定口径一致。"""
    by_seq = {v.match_seq: v for v in views}
    candidates: list[tuple[float, int, str, str]] = []
    for seq, from_code in base_picks.items():
        from_ev = next(
            (
                s.ev
                for s in by_seq[seq].selections
                if s.code == from_code and s.ev is not None
            ),
            None,
        )
        if from_ev is None:
            continue
        for sel in by_seq[seq].selections:
            if (
                sel.code == from_code
                or sel.share is None
                or sel.share >= COLD_SHARE_MAX
            ):
                continue
            if sel.ev is None or sel.ev <= 0 or sel.ev <= from_ev:
                continue
            candidates.append((round(sel.ev - from_ev, 6), seq, from_code, sel.code))
    candidates.sort(reverse=True)
    return candidates


def cold_variants(
    views: list[PoolMatchView],
    base_picks: dict[int, str],
    coldness: int,
) -> tuple[ColdTicketView, list[ColdTicketView]]:
    """贪心：全局按 EV 增益降序候选，各场至多一处替换，输出冷度 1..N 变体。"""
    base = ticket_view(base_picks, views)
    candidates = cold_candidates(views, base_picks)
    variants: list[ColdTicketView] = []
    taken: dict[int, tuple[str, str, float]] = {}
    for ev_gain, seq, from_code, to_code in candidates:
        if len(taken) >= coldness:
            break
        if seq in taken:
            continue
        taken[seq] = (from_code, to_code, ev_gain)
        picks = dict(base_picks)
        swaps: list[ColdSwapView] = []
        for swap_seq, (swap_from, swap_to, swap_gain) in taken.items():
            picks[swap_seq] = swap_to
            swaps.append(
                ColdSwapView(
                    match_seq=swap_seq,
                    from_code=swap_from,
                    to_code=swap_to,
                    ev_gain=round(swap_gain, 4),
                )
            )
        variants.append(ticket_view(picks, views, swaps))
    return base, variants


def target_plan(
    views: list[PoolMatchView],
    *,
    target_amount: float,
    risk: Literal["steady", "balanced", "bold"],
) -> tuple[ColdTicketView, float | None, int, bool, str]:
    """
    目标金额反推（任9 贪心 + 冷替换抬派彩）：返回票面/每注派彩/注数/达标/说明。

    Raises:
            ValueError: 期次可用概率场次数不足任9。

    """
    base_picks = default_base_picks(views)
    if len(base_picks) < PICK9_COUNT:
        raise ValueError("期次可用概率场次数不足任9")

    if risk == "steady":
        picks, swaps = dict(base_picks), []
        note = "稳档：14 场全稳票，不做冷替换（派彩不足目标时如实告知）。"
    else:
        # 任9：按各场最高概率排序取前 9（log 概率和最大化的贪心近似）
        top9 = sorted(
            base_picks,
            key=lambda seq: (
                -max(
                    s.prob or 0
                    for v in views
                    if v.match_seq == seq
                    for s in v.selections
                )
            ),
        )[:PICK9_COUNT]
        picks = {seq: base_picks[seq] for seq in top9}
        max_swaps = 1 if risk == "balanced" else 3
        swaps: list[ColdSwapView] = []
        candidates = cold_candidates(views, base_picks)
        for ev_gain, seq, from_code, to_code in candidates:
            if len(swaps) >= max_swaps or seq not in picks:
                if len(swaps) >= max_swaps:
                    break
                continue
            picks[seq] = to_code
            swaps.append(
                ColdSwapView(
                    match_seq=seq,
                    from_code=from_code,
                    to_code=to_code,
                    ev_gain=round(ev_gain, 4),
                )
            )
        note = (
            f"{'中' if risk == 'balanced' else '搏'}档：任9 贪心选场"
            + (f" + {len(swaps)} 处冷替换抬派彩" if swaps else "（无正期望冷门可换）")
            + "。"
        )

    ticket = ticket_view(picks, views, swaps)
    per_unit = ticket.est_odds * STAKE_PER_UNIT if ticket.est_odds is not None else None
    units = math.ceil(target_amount / per_unit) if per_unit else 0
    reached = per_unit is not None and per_unit >= target_amount
    if per_unit is None:
        note += "所选场次有缺份额/概率数据，估计派彩不可得——注数建议为 0（诚实降级）。"
    elif not reached:
        note += (
            f"单注估计派彩 ¥{per_unit:,.0f} 未达目标——"
            f"注数补金额（{units} 注），赔率本身不够。"
        )
    return ticket, per_unit, units, reached, note
