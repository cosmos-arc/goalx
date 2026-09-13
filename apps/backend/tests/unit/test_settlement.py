"""Settlement 引擎测试：官方规则口径（票 23 验收：无效场次单测覆盖）。"""

from __future__ import annotations

import pytest

from goalx_backend.settlement import (
    LegSpec,
    ResultFacts,
    detail_payload,
    judge_leg,
    settle_fixed_bet,
    settle_pool_combination,
)


def leg(
    fixture: int,
    market: str = "had",
    sel: str = "h",
    odds: float = 2.0,
    goal_line: float | None = None,
) -> LegSpec:
    return LegSpec(
        fixture_id=fixture,
        market_code=market,
        selection_code=sel,
        locked_odds=odds,
        goal_line=goal_line,
    )


def test_judge_leg_markets() -> None:
    result = ResultFacts(
        home_goals=2, away_goals=1, half_home_goals=1, half_away_goals=0
    )
    assert judge_leg(leg(1, "had", "h"), result).hit is True
    assert judge_leg(leg(1, "had", "a"), result).hit is False
    assert judge_leg(leg(1, "crs", "2:1"), result).hit is True
    assert (
        judge_leg(leg(1, "crs", "h_other"), ResultFacts(home_goals=7, away_goals=0)).hit
        is True
    )
    assert (
        judge_leg(leg(1, "crs", "5:2"), ResultFacts(home_goals=5, away_goals=2)).hit
        is True
    )
    assert judge_leg(leg(1, "ttg", "3"), result).hit is True
    assert (
        judge_leg(leg(1, "ttg", "7"), ResultFacts(home_goals=4, away_goals=5)).hit
        is True
    )
    assert judge_leg(leg(1, "hafu", "hh"), result).hit is True
    assert judge_leg(leg(1, "hafu", "dh"), result).hit is False


def test_judge_leg_hhad_goal_line() -> None:
    result = ResultFacts(home_goals=1, away_goals=1)
    # 主让 1 球（-1）：1:1 调整后 0:1 → 客胜
    assert judge_leg(leg(1, "hhad", "a", goal_line=-1), result).hit is True
    assert judge_leg(leg(1, "hhad", "d", goal_line=-1), result).hit is False
    # 半球线 -0.5：1:1 调整后 0.5:1 → 客胜（不整球线无平局态）
    assert judge_leg(leg(1, "hhad", "a", goal_line=-0.5), result).hit is True
    # 受让 +1：1:1 调整后 2:1 → 主胜
    assert judge_leg(leg(1, "hhad", "h", goal_line=1), result).hit is True


def test_judge_leg_requires_half_score_for_hafu() -> None:
    outcome = judge_leg(leg(1, "hafu", "hh"), ResultFacts(home_goals=2, away_goals=1))
    assert outcome.hit is None
    assert outcome.note == "missing_half_score"


def test_single_win_loss_open() -> None:
    results = {1: ResultFacts(home_goals=1, away_goals=0)}
    won = settle_fixed_bet(100.0, [leg(1, sel="h", odds=1.85)], results)
    assert (won.status, won.payout, won.profit) == ("won", 185.0, 85.0)
    lost = settle_fixed_bet(100.0, [leg(1, sel="d")], results)
    assert (lost.status, lost.payout, lost.profit) == ("lost", 0.0, -100.0)
    open_bet = settle_fixed_bet(100.0, [leg(2, sel="h")], results)
    assert open_bet.status == "open"
    assert open_bet.settled is False


def test_single_void_refunds() -> None:
    results = {
        1: ResultFacts(home_goals=0, away_goals=0, void=True, void_reason="腰斩")
    }
    out = settle_fixed_bet(100.0, [leg(1, sel="h", odds=1.85)], results)
    assert (out.status, out.payout, out.profit) == ("void", 100.0, 0.0)
    assert out.legs[0].void is True


def test_parlay_all_hit_multiplies_odds() -> None:
    results = {
        1: ResultFacts(home_goals=1, away_goals=0),
        2: ResultFacts(home_goals=0, away_goals=2),
    }
    out = settle_fixed_bet(
        2.0, [leg(1, sel="h", odds=1.85), leg(2, sel="a", odds=2.10)], results
    )
    assert out.status == "won"
    assert out.payout == pytest.approx(2.0 * 1.85 * 2.10)
    assert out.profit == pytest.approx(out.payout - 2.0)


def test_parlay_miss_loses() -> None:
    results = {
        1: ResultFacts(home_goals=1, away_goals=0),
        2: ResultFacts(home_goals=1, away_goals=1),
    }
    out = settle_fixed_bet(2.0, [leg(1, sel="h"), leg(2, sel="a")], results)
    assert (out.status, out.payout, out.profit) == ("lost", 0.0, -2.0)


def test_parlay_void_leg_counts_odds_one() -> None:
    results = {
        1: ResultFacts(home_goals=1, away_goals=0),
        2: ResultFacts(home_goals=0, away_goals=0, void=True),
        3: ResultFacts(home_goals=0, away_goals=1),
    }
    # 3 串 1 一腿无效：该腿赔率按 1，其余两腿正常连乘
    out = settle_fixed_bet(
        2.0,
        [
            leg(1, sel="h", odds=1.85),
            leg(2, sel="d", odds=3.2),
            leg(3, sel="a", odds=2.5),
        ],
        results,
    )
    assert out.status == "won"
    assert out.payout == 2.0 * 1.85 * 2.5
    assert "void_legs_odds_1:1" in out.notes


def test_two_leg_parlay_one_void_keeps_remaining_odds() -> None:
    results = {
        1: ResultFacts(home_goals=1, away_goals=0),
        2: ResultFacts(home_goals=0, away_goals=0, void=True),
    }
    # 2 串 1 无效腿按 1, 有效腿继续按原赔率计奖
    out = settle_fixed_bet(
        2.0, [leg(1, sel="h", odds=1.85), leg(2, sel="d", odds=3.2)], results
    )
    assert (out.status, out.payout, out.profit) == ("won", 3.7, 1.7)


def test_parlay_two_void_legs_full_refund() -> None:
    results = {
        1: ResultFacts(home_goals=0, away_goals=0, void=True),
        2: ResultFacts(home_goals=0, away_goals=0, void=True),
    }
    out = settle_fixed_bet(2.0, [leg(1, sel="h"), leg(2, sel="d")], results)
    assert (out.status, out.payout, out.profit) == ("void", 2.0, 0.0)
    assert "all_void_refund" in out.notes


def test_parlay_partial_void_miss_loses() -> None:
    results = {
        1: ResultFacts(home_goals=0, away_goals=1),
        2: ResultFacts(home_goals=0, away_goals=0, void=True),
        3: ResultFacts(home_goals=1, away_goals=0),
    }
    out = settle_fixed_bet(
        2.0, [leg(1, sel="h"), leg(2, sel="d"), leg(3, sel="h")], results
    )
    assert (out.status, out.payout) == ("lost", 0.0)


def test_parlay_awaiting_any_result_is_open() -> None:
    results = {1: ResultFacts(home_goals=1, away_goals=0)}
    out = settle_fixed_bet(2.0, [leg(1, sel="h"), leg(9, sel="a")], results)
    assert out.status == "open"


def test_pool_combination_judgement() -> None:
    results = {
        1: ResultFacts(home_goals=2, away_goals=0),
        2: ResultFacts(home_goals=0, away_goals=0, void=True),
    }
    hit, notes = settle_pool_combination({1: "3", 2: "0"}, results)
    assert hit is True  # 无效场次近似命中
    assert any("void_fixture_2" in note for note in notes)
    miss, _ = settle_pool_combination({1: "0", 2: "3"}, results)
    assert miss is False
    pending, _ = settle_pool_combination({1: "3", 7: "1"}, results)
    assert pending is None


def test_detail_payload_serializes() -> None:
    out = settle_fixed_bet(
        2.0, [leg(1, sel="h")], {1: ResultFacts(home_goals=1, away_goals=0)}
    )
    payload = detail_payload(out)
    assert payload["status"] == "won"
    assert payload["legs"][0]["fixture_id"] == 1
