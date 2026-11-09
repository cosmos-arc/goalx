"""玩法资格链单源判定（edge-audit-20261109 票 14）。

market_eligibility_problem 是玩法级「在售 + 单关资格」的单源谓词：预筛
（auto_strategy.anchor_candidates）与下游建注资格链
（bets.validate_had_selections 非 had 腿）两处引用同一判定——外部审计
反例转正：had 在售但 ttg 停售且不允许单关 → 拒绝（此前预筛硬编码 had、
下游非 had 腿零验证，两门都不拦）。

历史注记（仅前向生效）：资格未齐的历史记录不追溯改判——本谓词只用于
新建注/锁定时的在售判定，既有已建/已结注不回扫重判。
"""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend.betting.bets import (
    BetEligibilityError,
    market_eligibility_problem,
    validate_had_selections,
)
from goalx_backend.data import fixtures as fx_store
from goalx_backend.models import LegInput, SaleStatusInput, Tier

SEEN_AT = "2026-11-08T12:50:00+00:00"
AS_OF = "2026-11-08T12:55:00+00:00"


def _seed_fixture(db: sqlite3.Connection, tag: str = "甲") -> int:
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"主队{tag}")
    away = fx_store.upsert_team(db, f"客队{tag}")
    return fx_store.upsert_fixture(
        db, competition, "2026-11-08T15:00:00+00:00", home, away
    )


def _seed_sale(
    db: sqlite3.Connection,
    fixture_id: int,
    *,
    market_code: str | None,
    sale_state: str,
    single_eligible: bool | None,
    at: str = SEEN_AT,
) -> None:
    fx_store.append_sale_status(
        db,
        SaleStatusInput(
            fixture_id=fixture_id,
            market_code=market_code,
            sale_state=sale_state,
            single_eligible=single_eligible,
            observed_at=at,
        ),
    )


# ---- 谓词：单源玩法级资格判定 ----


def test_market_eligible_on_sale_single_ok(db: sqlite3.Connection) -> None:
    fixture = _seed_fixture(db)
    _seed_sale(
        db, fixture, market_code="ttg", sale_state="on_sale", single_eligible=True
    )
    assert (
        market_eligibility_problem(db, fixture, "ttg", AS_OF, requires_single=True)
        is None
    )
    assert (
        market_eligibility_problem(db, fixture, "ttg", AS_OF, requires_single=False)
        is None
    )


def test_counterexample_had_on_sale_ttg_stopped_and_not_single(
    db: sqlite3.Connection,
) -> None:
    """外部审计反例：had 在售，但 ttg 停售且不允许单关 → 拒绝。"""
    fixture = _seed_fixture(db)
    _seed_sale(
        db, fixture, market_code=None, sale_state="on_sale", single_eligible=True
    )
    _seed_sale(
        db, fixture, market_code="had", sale_state="on_sale", single_eligible=True
    )
    _seed_sale(
        db, fixture, market_code="ttg", sale_state="stopped", single_eligible=False
    )
    # had 照常可投；ttg 按其市场行拒绝（玩法行优先于比赛行）
    assert (
        market_eligibility_problem(db, fixture, "had", AS_OF, requires_single=True)
        is None
    )
    assert market_eligibility_problem(
        db, fixture, "ttg", AS_OF, requires_single=True
    ) == ("sale_stopped")
    # 串关腿无需单关资格，但停售仍拒
    assert market_eligibility_problem(
        db, fixture, "ttg", AS_OF, requires_single=False
    ) == ("sale_stopped")


def test_market_row_preferred_over_fixture_row(db: sqlite3.Connection) -> None:
    """市场级行优先：比赛行停售、ttg 行在售 → ttg 判在售（共享读语义）。"""
    fixture = _seed_fixture(db)
    _seed_sale(
        db, fixture, market_code=None, sale_state="stopped", single_eligible=None
    )
    _seed_sale(
        db, fixture, market_code="ttg", sale_state="on_sale", single_eligible=True
    )
    assert (
        market_eligibility_problem(db, fixture, "ttg", AS_OF, requires_single=True)
        is None
    )


def test_single_ineligible_and_unknown_fail_closed(db: sqlite3.Connection) -> None:
    fixture = _seed_fixture(db)
    _seed_sale(
        db, fixture, market_code="ttg", sale_state="on_sale", single_eligible=False
    )
    assert market_eligibility_problem(
        db, fixture, "ttg", AS_OF, requires_single=True
    ) == ("single_ineligible")
    # 串关腿不看单关资格
    assert (
        market_eligibility_problem(db, fixture, "ttg", AS_OF, requires_single=False)
        is None
    )

    fixture2 = _seed_fixture(db, "乙")
    _seed_sale(
        db, fixture2, market_code=None, sale_state="on_sale", single_eligible=None
    )
    # 单关资格未知（如 hhad/hafu 历史无市场行）→ 保守拒绝；串关照常
    assert market_eligibility_problem(
        db, fixture2, "hhad", AS_OF, requires_single=True
    ) == ("single_unknown")
    assert (
        market_eligibility_problem(db, fixture2, "hhad", AS_OF, requires_single=False)
        is None
    )


def test_no_rows_and_not_on_sale_reasons(db: sqlite3.Connection) -> None:
    fixture = _seed_fixture(db)
    # 零销售状态行 → 不可判（不倒填资格）
    assert market_eligibility_problem(
        db, fixture, "ttg", AS_OF, requires_single=True
    ) == ("sale_status_unknown")
    fixture2 = _seed_fixture(db, "乙")
    _seed_sale(
        db, fixture2, market_code=None, sale_state="not_on_sale", single_eligible=None
    )
    # 未开售：窗内=场次级归因（锚时刻 12:55Z=20:55 北京，在 11:30-22:00 窗内）
    assert market_eligibility_problem(
        db, fixture2, "ttg", AS_OF, requires_single=True
    ) == ("sale_not_on_sale")


# ---- 下游建注资格链：非 had 腿玩法级验证 ----


def _ttg_leg(fixture_id: int, odds: float = 4.0) -> LegInput:
    return LegInput(
        fixture_id=fixture_id,
        market_code="ttg",
        selection_code="2",
        locked_odds=odds,
    )


def test_downstream_rejects_ttg_single_when_had_only_on_sale(
    db: sqlite3.Connection,
) -> None:
    """反例在建注闸同样成立：ttg 单关腿（had 在售但 ttg 停售/非单固）→ 拒。"""
    fixture = _seed_fixture(db)
    _seed_sale(
        db, fixture, market_code=None, sale_state="on_sale", single_eligible=True
    )
    _seed_sale(
        db, fixture, market_code="had", sale_state="on_sale", single_eligible=True
    )
    _seed_sale(
        db, fixture, market_code="ttg", sale_state="stopped", single_eligible=False
    )
    with pytest.raises(BetEligibilityError, match=r"ttg.*sale_stopped"):
        validate_had_selections(db, [_ttg_leg(fixture)], as_of=AS_OF)


def test_downstream_single_unknown_rejected_parlay_passes(
    db: sqlite3.Connection,
) -> None:
    fixture = _seed_fixture(db)
    # 比赛级行不携带单关资格（生产 ingest 口径）→ ttg 单关资格未知
    _seed_sale(
        db, fixture, market_code=None, sale_state="on_sale", single_eligible=None
    )
    # 单关资格未知 → 拒（fail-closed）
    with pytest.raises(BetEligibilityError, match="single_unknown"):
        validate_had_selections(db, [_ttg_leg(fixture)], as_of=AS_OF)
    # 两腿串关（不同场）无单关要求 → 通过（销售态按比赛行在售）
    fixture2 = _seed_fixture(db, "乙")
    _seed_sale(
        db, fixture2, market_code=None, sale_state="on_sale", single_eligible=None
    )
    validate_had_selections(
        db,
        [_ttg_leg(fixture), _ttg_leg(fixture2, odds=4.2)],
        as_of=AS_OF,
    )
