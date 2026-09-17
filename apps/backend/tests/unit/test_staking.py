"""建议仓位领域函数（票 wb-06）：flat 红线 / ¼Kelly / 截断 / EV≤0 / 整注口径。"""

from __future__ import annotations

import pytest

from goalx_backend.betting.staking import (
    FLAT_FRACTION,
    KELLY_FRACTION,
    MAX_BET_FRACTION,
    MIN_BET_FRACTION,
    MIN_STAKE_CNY,
    StakeTier,
    suggest_stake,
)
from goalx_backend.models import BetMode


def test_ev_non_positive_suggests_zero_in_both_modes() -> None:
    """EV≤0 → ¥0 且档位显式（防"看推荐再自己加码"）。"""
    for mode in (BetMode.PAPER, BetMode.LIVE):
        hit = suggest_stake(mode, bankroll=10_000, ev=0.0, odds=2.0)
        assert hit.stake == 0.0
        assert hit.tier is StakeTier.EV_NON_POSITIVE
        assert "¥0" in hit.reason
        negative = suggest_stake(mode, bankroll=10_000, ev=-0.05, odds=2.0)
        assert negative.stake == 0.0
        assert negative.tier is StakeTier.EV_NON_POSITIVE


def test_paper_mode_is_always_flat_red_line() -> None:
    """纸面一律 flat（红线）：无论 EV/赔率多大都不出 Kelly。"""
    hit = suggest_stake(BetMode.PAPER, bankroll=10_000, ev=0.20, odds=1.5)
    assert hit.tier is StakeTier.FLAT
    assert hit.stake == pytest.approx(10_000 * FLAT_FRACTION)  # 2% = ¥200
    assert hit.full_kelly_fraction is None  # 纸面期不计算/不输出 Kelly
    assert "flat" in hit.reason
    assert "红线" in hit.reason


def test_paper_flat_uses_stake_profile_caliber() -> None:
    """flat 档与组合引擎 StakeProfile 同口径：2% 截断 1-5%、下限 ¥2。"""
    # 常规 bankroll：2% 落在 1-5% 区间内
    hit = suggest_stake(BetMode.PAPER, bankroll=5_004.2, ev=0.03, odds=2.3)
    assert hit.stake == pytest.approx(100.08)
    assert hit.fraction == pytest.approx(hit.stake / 5_004.2)
    # 小 bankroll：最低注 ¥2 已越 5% 上限（30×5%=1.5）→ 按最低注建议并说明越限
    small = suggest_stake(BetMode.PAPER, bankroll=30, ev=0.03, odds=2.3)
    assert small.stake == MIN_STAKE_CNY
    assert small.fraction == pytest.approx(2.0 / 30)
    assert "超出 5% 上限" in small.reason
    # bankroll 80：2%=1.6 → 最低注 ¥2 仍在 5% 上限（¥4）内 → 只按 ¥2，不越限说明
    within = suggest_stake(BetMode.PAPER, bankroll=80, ev=0.03, odds=2.3)
    assert within.stake == MIN_STAKE_CNY
    assert "超出" not in within.reason
    # 未入金：按最低注建议并说明（不静默给 0 或伪造比例）
    unfunded = suggest_stake(BetMode.PAPER, bankroll=0, ev=0.03, odds=2.3)
    assert unfunded.stake == MIN_STAKE_CNY
    assert "未入金" in unfunded.reason


def test_live_quarter_kelly_base_case() -> None:
    """真金 ¼Kelly：f* = EV/(odds−1) 取 1/4，区间内不截断。"""
    # EV 10%，odds 2.0 → f* = 0.10/1.0 = 10%，¼ = 2.5%（1-5% 区间内）
    hit = suggest_stake(BetMode.LIVE, bankroll=10_000, ev=0.10, odds=2.0)
    assert hit.tier is StakeTier.QUARTER_KELLY
    assert hit.full_kelly_fraction == pytest.approx(0.10)
    assert hit.fraction == pytest.approx(0.025)
    assert hit.stake == pytest.approx(250.0)
    assert hit.capped is False
    assert "¼ fractional Kelly" in hit.reason or "1/4" in hit.reason


def test_live_caps_at_upper_bound_default_5pct() -> None:
    """大 edge 截断到单注 5% 上限（默认档）并显式说明。"""
    # EV 30%，odds 1.5 → f* = 60%，¼ = 15% > 5% → 截断
    hit = suggest_stake(BetMode.LIVE, bankroll=10_000, ev=0.30, odds=1.5)
    assert hit.fraction == pytest.approx(MAX_BET_FRACTION)
    assert hit.stake == pytest.approx(500.0)
    assert hit.capped is True
    assert "截断" in hit.reason


def test_live_cap_fraction_param_for_tiers() -> None:
    """cap 参数分档（票 wb-07 三档复用）：上限 2% 档比 5% 档更早截断。"""
    # ¼Kelly = 2.5%：cap 2% 截断到 2%，cap 5% 不截断
    tight = suggest_stake(
        BetMode.LIVE, bankroll=10_000, ev=0.10, odds=2.0, cap_fraction=0.02
    )
    loose = suggest_stake(
        BetMode.LIVE, bankroll=10_000, ev=0.10, odds=2.0, cap_fraction=0.05
    )
    assert tight.capped is True
    assert tight.stake == pytest.approx(200.0)
    assert loose.capped is False
    assert loose.stake == pytest.approx(250.0)


def test_live_small_edge_floors_at_1pct() -> None:
    """小 edge：¼Kelly 低于 1% 下限时按 1% 建议并说明（单注硬区间 1-5%）。"""
    # EV 2%，odds 3.0 → f* = 1%，¼ = 0.25% < 1% → 按下限 1%
    hit = suggest_stake(BetMode.LIVE, bankroll=10_000, ev=0.02, odds=3.0)
    assert hit.fraction == pytest.approx(MIN_BET_FRACTION)
    assert hit.stake == pytest.approx(100.0)
    assert "1%" in hit.reason


def test_live_unfunded_bankroll_is_honest_zero() -> None:
    """真金未入金：诚实 ¥0（不伪造比例建议）。"""
    hit = suggest_stake(BetMode.LIVE, bankroll=0, ev=0.10, odds=2.0)
    assert hit.stake == 0.0
    assert hit.tier is StakeTier.UNFUNDED
    assert "未入金" in hit.reason


def test_live_small_bankroll_min_stake_note() -> None:
    """小 bankroll：比例注额低于 ¥2 时按最低注建议并说明越限。"""
    hit = suggest_stake(BetMode.LIVE, bankroll=150, ev=0.10, odds=2.0)
    assert hit.fraction == pytest.approx(0.025)
    assert hit.stake == pytest.approx(3.75)  # 2.5% × 150 在区间且 ≥ ¥2
    tiny = suggest_stake(BetMode.LIVE, bankroll=30, ev=0.02, odds=3.0)
    # 1% × 30 = ¥0.3 < ¥2 → 最低注 ¥2 已越 5% 上限（¥1.5）→ 兜底并说明
    assert tiny.stake == MIN_STAKE_CNY
    assert "超出上限" in tiny.reason


def test_parlay_input_is_whole_bet_caliber() -> None:
    """串关=整注口径：调用方传联合赔率/联合 EV，本函数按单注一个 Kelly（不分腿）。"""
    # 两腿独立 EV +10%/+5% → 联合 EV = 1.10×1.05 − 1 = 15.5%，联合赔率 2.0×3.0 = 6.0
    joint_ev = 1.10 * 1.05 - 1
    hit = suggest_stake(BetMode.LIVE, bankroll=10_000, ev=joint_ev, odds=6.0)
    # f* = 0.155/5 = 3.1%，¼ ≈ 0.775% < 1% → 下限 1%
    assert hit.tier is StakeTier.QUARTER_KELLY
    assert hit.fraction == pytest.approx(MIN_BET_FRACTION)
    assert hit.stake == pytest.approx(100.0)


def test_invalid_inputs_raise() -> None:
    """非法输入：odds ≤ 1 与 cap 越界直接拒绝（API 层 422 前的领域防线）。"""
    with pytest.raises(ValueError, match="odds"):
        suggest_stake(BetMode.LIVE, bankroll=1_000, ev=0.1, odds=1.0)
    with pytest.raises(ValueError, match="cap_fraction"):
        suggest_stake(BetMode.LIVE, bankroll=1_000, ev=0.1, odds=2.0, cap_fraction=0.10)
    # bankroll 校验在 API 请求模型（ge=0）；领域层把 ≤0 一律按未入金诚实降级
    negative = suggest_stake(BetMode.PAPER, bankroll=-1, ev=0.1, odds=2.0)
    assert negative.tier is StakeTier.FLAT
    assert negative.stake == MIN_STAKE_CNY


def test_kelly_fraction_constant_is_quarter() -> None:
    """红线常量自检：fraction 系数 = 1/4（调研推荐起步，不引入 progression）。"""
    assert KELLY_FRACTION == 0.25
