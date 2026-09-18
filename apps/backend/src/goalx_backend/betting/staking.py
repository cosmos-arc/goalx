"""
建议仓位（票 wb-06）：只读注额建议——纸面一律 flat，真金 ¼ fractional Kelly。

归属（ADR-0008）：注额建议服务于建注决策（选注篮/组合头部），归 betting 域；
纯函数、无表、无 SQL（建议留痕随注单落库是后续项，见调研 §5.3）。

规则（调研定稿 ``research/staking-plans.md`` §5；倍投/斐波那契等 progression
系已否决——不改变 EV 只重排破产路径，且污染 CLV/skill 统计）：

- EV≤0 → 建议 ¥0 并显式说明（防"系统推荐了我再自己加码"）；
- paper 一律 flat（红线）：验证期不引入任何变注额机制，flat 档 = bankroll×2%
  截断到 1–5% 区间、下限竞彩最低注 ¥2——与组合引擎 ``StakeProfile``
  （票 wb-03/05 两玩法页共用）同口径；
- live = ¼ fractional Kelly：f* = EV/(odds−1) 取 1/4，再截断到单注
  1%–cap%（默认 5%；票 wb-07 三档额度复用 cap 参数分档）；
- 串关由调用方传**联合赔率/联合 EV**（腿间独立性假设在前），本函数按
  "整注"口径计算——注额=单关口径（单注一个 Kelly，不分腿）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from goalx_backend.models import BetMode

FLAT_FRACTION = 0.02  # flat 档目标比例（组合引擎 StakeProfile.flatFraction 同值）
MIN_BET_FRACTION = 0.01  # 单注占 bankroll 下限（map 定稿 1–5% 硬区间）
MAX_BET_FRACTION = 0.05  # 单注占 bankroll 上限（默认档；live 可经 cap_fraction 收紧）
KELLY_FRACTION = 0.25  # fractional Kelly 系数（调研推荐 1/4 起步）
MIN_STAKE_CNY = 2.0  # 竞彩最小注（比例区间无法表达的最小面额）


class StakeTier(StrEnum):
    """建议档位（前端按档位渲染理由）。"""

    FLAT = "flat"
    QUARTER_KELLY = "quarter_kelly"
    EV_NON_POSITIVE = "ev_non_positive"
    UNFUNDED = "unfunded"


class StakeSuggestion(BaseModel):
    """建议输出：注额 + 档位 + 理由（只读，不自动改单）。"""

    stake: float
    tier: StakeTier
    fraction: float | None = None
    full_kelly_fraction: float | None = None
    capped: bool = False
    reason: str


def _round_cny(value: float) -> float:
    """按分取整（与组合引擎 flat 档同精度，两处数字可对账）。"""
    return round(value * 100) / 100


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def suggest_stake(
    mode: BetMode,
    bankroll: float,
    ev: float,
    odds: float,
    cap_fraction: float = MAX_BET_FRACTION,
) -> StakeSuggestion:
    """
    建议注额（纯函数）。

    Args:
        mode: 纸面/真金——纸面一律 flat（红线），真金 ¼ fractional Kelly。
        bankroll: 资金池余额（真金账本；≤0 视为未入金，诚实降级不伪造比例）。
        ev: 单位 EV（如 0.05 = +5%；串关传联合 EV = Π(1+EV_i) − 1）。
        odds: decimal 赔率（串关传联合赔率；须 >1）。
        cap_fraction: live 单注上限占 bankroll 比例（≤5%，默认 5%）。

    Returns:
        StakeSuggestion：注额/档位/占比/满 Kelly 对照/是否截断/理由文本。

    """
    if odds <= 1:
        raise ValueError(f"odds 须 > 1（decimal 口径），得到 {odds}")
    if not 0 < cap_fraction <= MAX_BET_FRACTION:
        raise ValueError(
            f"cap_fraction 须在 (0, {MAX_BET_FRACTION}] 内，得到 {cap_fraction}"
        )
    if ev <= 0:
        return StakeSuggestion(
            stake=0.0,
            tier=StakeTier.EV_NON_POSITIVE,
            reason=(
                f"EV {_pct(ev)} ≤ 0——建议不投（¥0）。EV 是诊断量非机会信号："
                "无正期望时不给注额，防止“看系统有推荐再自己加码”。"
            ),
        )
    if mode is BetMode.PAPER:
        return _flat_stake(bankroll)
    return _quarter_kelly_stake(bankroll, ev, odds, cap_fraction)


def _flat_stake(bankroll: float) -> StakeSuggestion:
    """纸面 flat（红线）：bankroll×2% 截断 1–5%，下限 ¥2；未入金按最低注诚实说明。"""
    if bankroll <= 0:
        return StakeSuggestion(
            stake=MIN_STAKE_CNY,
            tier=StakeTier.FLAT,
            reason=(
                "纸面期一律 flat（红线）：资金池未入金——暂按竞彩最低注"
                f" ¥{_round_cny(MIN_STAKE_CNY):.0f} 建议，入金后按 bankroll 1–5% 校准"
            ),
        )
    upper = bankroll * MAX_BET_FRACTION
    target = min(max(bankroll * FLAT_FRACTION, bankroll * MIN_BET_FRACTION), upper)
    stake = _round_cny(max(target, MIN_STAKE_CNY))
    fraction = stake / bankroll
    reason = (
        f"纸面期一律 flat（红线）：bankroll × {_pct(FLAT_FRACTION)} 截断到 1–5% 区间"
        f" → ¥{stake:.2f}（占比 {_pct(fraction)}）"
        "——比例策略（Kelly）在前瞻 skill 过线前不启用，保证验证指标无偏"
    )
    if stake > upper:
        reason += f"；bankroll ¥{bankroll:.2f} 较小"
        reason += (
            f"：最低注 ¥{MIN_STAKE_CNY:.0f} 已超出 5% 上限（¥{_round_cny(upper):.2f}）"
        )
        reason += "——建议先入金再按比例投注"
    return StakeSuggestion(
        stake=stake, tier=StakeTier.FLAT, fraction=fraction, reason=reason
    )


def _quarter_kelly_stake(
    bankroll: float, ev: float, odds: float, cap_fraction: float
) -> StakeSuggestion:
    """真金 ¼ fractional Kelly：f* = EV/(odds−1) 取 1/4，截断单注 1%–cap%。"""
    if bankroll <= 0:
        return StakeSuggestion(
            stake=0.0,
            tier=StakeTier.UNFUNDED,
            reason=(
                "真金建议 = ¼ fractional Kelly（f* = EV/(odds−1) 取 1/4，"
                "单注 1–5% 截断），但资金池未入金——暂无注额建议（入金后按比例校准）"
            ),
        )
    full = ev / (odds - 1)
    quarter = full * KELLY_FRACTION
    fraction = min(max(quarter, MIN_BET_FRACTION), cap_fraction)
    capped = quarter > cap_fraction
    floored = quarter < MIN_BET_FRACTION
    stake = _round_cny(max(bankroll * fraction, MIN_STAKE_CNY))
    reason = (
        f"真金 ¼ fractional Kelly：f* = EV/(odds−1) = {_pct(full)}"
        f"，取 1/4 = {_pct(quarter)}"
    )
    if capped:
        reason += f"，已按单注上限 {cap_fraction * 100:.0f}% 截断 → {_pct(fraction)}"
    elif floored:
        reason += f"，低于单注下限 1%——按 1% 建议（{_pct(quarter)} → {_pct(fraction)}）"
    else:
        reason += f" → 占比 {_pct(fraction)}"
    reason += (
        f" → ¥{stake:.2f}；单注硬区间 1%–{cap_fraction * 100:.0f}%"
        f"（竞彩最低 ¥{MIN_STAKE_CNY:.0f}）"
    )
    if stake > bankroll * cap_fraction:
        reason += "；bankroll 较小：最低注已超出上限——建议先入金再按比例投注"
    return StakeSuggestion(
        stake=stake,
        tier=StakeTier.QUARTER_KELLY,
        fraction=stake / bankroll,
        full_kelly_fraction=full,
        capped=capped,
        reason=reason,
    )
