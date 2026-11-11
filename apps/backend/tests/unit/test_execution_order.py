"""执行单 schema（vision-build 票 12 交付 2）：字段定稿+结构不变量。

执行单=真金 γ 件（票 10）的人照单投注纸面产物；本票只定 schema（pydantic
模型即代码，票 10 建表/建面直接用）。钉住：字段完整性（场次/玩法/选项/
金额/截止/策略版本回指/h_id 回指）、类型形状（单关=1 腿、串关=多腿异场、
池面=期次回指+无锁价）、税感知不变量（固定赔率面单票奖金 ≤¥1 万免税桶，
与 sizing 规则同口径）、截止早于最早开球（固定赔率面）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from goalx_backend.betting.execution_order import ExecutionOrder, OrderKind
from goalx_backend.betting.tax import TAX_FREE_THRESHOLD

KICKOFF = "2026-11-08T15:00:00+00:00"
DEADLINE = "2026-11-08T14:50:00+00:00"  # 开球前 10min（kickoff 前置口径）


def _line(**overrides: object) -> dict[str, object]:
    """一条合法执行腿（ttg 单关，odds 5.74 → 帽内注额 ¥1,742）。"""
    base: dict[str, object] = {
        "fixture_id": 101,
        "match_code": "周六001",
        "market_code": "ttg",
        "selection_code": "2",
        "goal_line": None,
        "kickoff_utc": KICKOFF,
        "locked_odds": 5.74,
    }
    base.update(overrides)
    return base


def _order(**overrides: object) -> dict[str, object]:
    """一张合法执行单（单关，策略版本回指+h_id 回指齐备）。"""
    base: dict[str, object] = {
        "order_id": "auto-v1:2026-11-08:21:00:x:101",
        "kind": "single",
        "lines": [_line()],
        "stake": 1742.0,
        "deadline_utc": DEADLINE,
        "strategy_version_id": 3,
        "estimator_version": "artifact_schema=2;goalx-backend=1.0",
        "calibration_version": "beta+platt",
        "params_version": "9f2a" * 16,
        "h_id": "H7",
        "generated_at": "2026-11-08T13:01:00+00:00",
    }
    base.update(overrides)
    return base


def test_single_order_roundtrip() -> None:
    """单关执行单：合法构造+序列化往返（schema 即代码，票 10 直接用）。"""
    order = ExecutionOrder(**_order())
    assert order.kind is OrderKind.SINGLE
    assert order.lines[0].match_code == "周六001"
    assert order.h_id == "H7"
    assert order.pool_period_id is None
    rebuilt = ExecutionOrder.model_validate(order.model_dump())
    assert rebuilt == order


def test_parlay_joint_odds_tax_free_invariant() -> None:
    """串关=联合赔率整注口径：奖金越 ¥1 万拒、帽内过（边界两侧）。"""
    legs = [
        _line(fixture_id=1, locked_odds=2.0),
        _line(fixture_id=2, match_code="周六002", locked_odds=3.0),
    ]  # 联合 6.0：¥1,667 → 10,002 越线；¥1,666 → 9,996 帽内
    with pytest.raises(ValidationError, match="免税"):
        ExecutionOrder(**_order(kind="parlay", lines=legs, stake=1667.0))
    ok = ExecutionOrder(**_order(kind="parlay", lines=legs, stake=1666.0))
    assert ok.stake * 6.0 <= TAX_FREE_THRESHOLD


def test_single_boundary_prize_exactly_at_threshold_passes() -> None:
    """帽上奖金恰 ¥10,000（2.5 整除口径"不超过 1 万"免）——schema 放行。"""
    order = ExecutionOrder(**_order(stake=4000.0, lines=[_line(locked_odds=2.5)]))
    assert order.stake * 2.5 == TAX_FREE_THRESHOLD


def test_kind_shape_rules() -> None:
    """形状：单关恰 1 腿；串关 ≥2 腿且异场；池面须期次回指且无锁价。"""
    with pytest.raises(ValidationError, match="单关"):
        ExecutionOrder(**_order(lines=[_line(), _line(fixture_id=2)]))
    with pytest.raises(ValidationError, match="异场"):
        ExecutionOrder(
            **_order(
                kind="parlay",
                lines=[_line(locked_odds=2.0), _line(locked_odds=2.0)],
                stake=100.0,
            )
        )
    with pytest.raises(ValidationError, match="至少"):
        ExecutionOrder(**_order(kind="parlay", lines=[_line()], stake=100.0))
    pool_lines = [
        _line(market_code="pick9", selection_code="3", locked_odds=None),
        _line(
            fixture_id=2,
            match_code="周六002",
            market_code="pick9",
            selection_code="1",
            locked_odds=None,
        ),
    ]
    with pytest.raises(ValidationError, match="期次"):
        ExecutionOrder(**_order(kind="pool", lines=pool_lines, stake=4.0, h_id=None))
    with pytest.raises(ValidationError, match="锁价"):
        ExecutionOrder(
            **_order(
                kind="pool",
                lines=[_line(market_code="pick9", selection_code="3")],
                stake=4.0,
                pool_period_id=77,
            )
        )
    # 非池面不得带期次回指（形状单一，对账免歧义）
    with pytest.raises(ValidationError, match="期次"):
        ExecutionOrder(**_order(pool_period_id=77))


def test_pool_order_exempts_tax_cap_and_deadline_rule() -> None:
    """池面：大奖越线口径=税后（H7），schema 不压帽；截止=期次销售截止语义。"""
    pool = ExecutionOrder(
        **_order(
            kind="pool",
            lines=[
                _line(market_code="pick9", selection_code="3", locked_odds=None),
                _line(
                    fixture_id=2,
                    match_code="周六002",
                    market_code="pick9",
                    selection_code="1",
                    locked_odds=None,
                ),
            ],
            stake=5000.0,  # 无锁价面无 joint odds 可算——税帽不适用
            deadline_utc="2026-11-08T20:00:00+00:00",  # 晚于开球=期次截止语义
            pool_period_id=77,
            h_id=None,
        )
    )
    assert pool.kind is OrderKind.POOL
    assert pool.h_id is None  # 既有策略底注无假设归属


def test_deadline_must_precede_earliest_kickoff_fixed_odds() -> None:
    """固定赔率面：截止须早于最早开球（=开球前置口径的schema级下界）。"""
    with pytest.raises(ValidationError, match="截止"):
        ExecutionOrder(**_order(deadline_utc=KICKOFF))  # 恰等于开球也拒
    with pytest.raises(ValidationError, match="截止"):
        ExecutionOrder(**_order(deadline_utc="2026-11-08T15:01:00+00:00"))


def test_hhad_line_requires_goal_line() -> None:
    """hhad 腿必须带盘口线（prereg 整数线；结算判别的载荷）。"""
    with pytest.raises(ValidationError, match="goal_line"):
        ExecutionOrder(
            **_order(
                lines=[_line(market_code="hhad", selection_code="h", locked_odds=2.5)],
                stake=4000.0,
            )
        )
    ok = ExecutionOrder(
        **_order(
            lines=[
                _line(
                    market_code="hhad",
                    selection_code="h",
                    goal_line=-1.0,
                    locked_odds=2.5,
                )
            ],
            stake=4000.0,
        )
    )
    assert ok.lines[0].goal_line == -1.0


def test_version_backref_and_h_id_required_shape() -> None:
    """策略版本回指（票 01 状态机）齐备且非空；h_id 可空（无假设归属）。"""
    for field in ("estimator_version", "calibration_version", "params_version"):
        with pytest.raises(ValidationError):
            ExecutionOrder(**_order(**{field: ""}))
    with pytest.raises(ValidationError):
        ExecutionOrder(**_order(strategy_version_id=0))
    assert ExecutionOrder(**_order(h_id=None)).h_id is None


def test_identity_and_money_calibers() -> None:
    """order_id 非空；stake > 0（MoneyCny）；ISO 时间戳坏串拒收。"""
    with pytest.raises(ValidationError):
        ExecutionOrder(**_order(order_id=""))
    with pytest.raises(ValidationError):
        ExecutionOrder(**_order(stake=0.0))
    with pytest.raises(ValidationError):
        ExecutionOrder(**_order(generated_at="2026-13-45T99:00:00+00:00"))
    with pytest.raises(ValidationError):
        ExecutionOrder(**_order(lines=[_line(kickoff_utc="not-a-date")]))
    with pytest.raises(ValidationError):
        ExecutionOrder(**_order(lines=[_line(fixture_id=0)]))


def test_fixed_odds_line_requires_locked_odds() -> None:
    """固定赔率面腿缺锁价拒（税桶校验的锁价前置）；串关带期次回指同拒。"""
    with pytest.raises(ValidationError, match="锁价"):
        ExecutionOrder(**_order(lines=[_line(locked_odds=None)]))
    legs = [
        _line(fixture_id=1, locked_odds=2.0),
        _line(fixture_id=2, match_code="周六002", locked_odds=3.0),
    ]
    with pytest.raises(ValidationError, match="期次"):
        ExecutionOrder(
            **_order(kind="parlay", lines=legs, stake=100.0, pool_period_id=7)
        )


def test_joint_odds_derivation_and_schema_export() -> None:
    """只读派生面：joint_odds（单关/串关/池面）+ JSON Schema 导出（票 10 对照）。"""
    from goalx_backend.betting import execution_order as eo

    single = ExecutionOrder(**_order())
    assert single.joint_odds() == pytest.approx(5.74)
    parlay = ExecutionOrder(
        **_order(
            kind="parlay",
            lines=[
                _line(fixture_id=1, locked_odds=2.0),
                _line(fixture_id=2, match_code="周六002", locked_odds=3.0),
            ],
            stake=1666.0,
        )
    )
    assert parlay.joint_odds() == pytest.approx(6.0)
    pool = ExecutionOrder(
        **_order(
            kind="pool",
            lines=[_line(market_code="pick9", selection_code="3", locked_odds=None)],
            stake=4.0,
            pool_period_id=7,
        )
    )
    assert pool.joint_odds() is None
    schema = eo.model_json_schema()
    assert set(schema) == {"ExecutionLine", "ExecutionOrder"}


# ---- correctness 批修（票 12 review）：F2 时区 / F3 赔率 2dp / F4 闭集 / F5 快照 ----


def test_naive_timestamps_rejected_offsets_compare_by_instant() -> None:
    """correctness F2：naive 时刻拒收（防混比 TypeError 穿透 web 面）。

    aware 异 offset 按绝对时刻比较：+08:00 的 22:50 == 14:50Z < 开球 15:00Z
    → 合法；同瞬时相等（23:00+08:00 == 15:00Z 开球）→ 按截止哨拒。
    """
    with pytest.raises(ValidationError, match="时区"):
        ExecutionOrder(**_order(deadline_utc="2026-11-08T14:50:00"))
    with pytest.raises(ValidationError, match="时区"):
        ExecutionOrder(**_order(lines=[_line(kickoff_utc="2026-11-08T15:00:00")]))
    with pytest.raises(ValidationError, match="时区"):
        ExecutionOrder(**_order(generated_at="2026-11-08T13:01:00"))
    cross_offset = ExecutionOrder(
        **_order(deadline_utc="2026-11-08T22:50:00+08:00")  # == 14:50Z
    )
    assert cross_offset.deadline_utc.endswith("+08:00")
    with pytest.raises(ValidationError, match="截止"):
        ExecutionOrder(**_order(deadline_utc="2026-11-08T23:00:00+08:00"))


def test_odds_two_decimal_places_closes_tax_caliber_gap() -> None:
    """correctness F3：赔率两位小数（竞彩报价口径）+eps 1e-9——与税视图无缝。

    旧口径缝：odds 2.5000000001 × ¥4,000 → 奖金 10000.0000004，schema 的
    1e-6 eps 放行而 tax 全额征 20%。2dp 闭集后该输入在锁价校验即拒；
    真实分位粒度越线（4000.01×2.5=10000.025）照拒；恰 10000.0 放行
    （见 test_single_boundary_prize_exactly_at_threshold_passes）。
    """
    with pytest.raises(ValidationError, match="两位小数"):
        ExecutionOrder(**_order(lines=[_line(locked_odds=2.0000000001)], stake=100.0))
    with pytest.raises(ValidationError, match="两位小数"):
        ExecutionOrder(**_order(lines=[_line(locked_odds=2.5000000001)], stake=4000.0))
    with pytest.raises(ValidationError, match="免税"):
        ExecutionOrder(**_order(stake=4000.01, lines=[_line(locked_odds=2.5)]))


def test_market_and_selection_closed_sets_pinned_to_markets_source() -> None:
    """correctness F4：玩法/选项接 markets.LEG_MARKETS/LEG_SELECTIONS 单源闭集。"""
    from goalx_backend.markets import LEG_MARKETS, LEG_SELECTIONS

    # 大小写漂移/未知玩法拒（Literal 闭集）；闭集外选项拒（结算纪律）
    with pytest.raises(ValidationError):
        ExecutionOrder(
            **_order(
                lines=[_line(market_code="HHAD", selection_code="h", goal_line=-1.0)],
                stake=4000.0,
            )
        )
    with pytest.raises(ValidationError):
        ExecutionOrder(**_order(lines=[_line(market_code="xyz")], stake=100.0))
    with pytest.raises(ValidationError, match="闭集"):
        ExecutionOrder(
            **_order(
                lines=[_line(market_code="had", selection_code="x", locked_odds=2.5)],
                stake=4000.0,
            )
        )
    # 合法枚举过：ttg "2"（TTG_SELECTIONS）、pick9 "3"（POOL_HAD_CODES）
    ok = ExecutionOrder(**_order())
    assert ok.lines[0].selection_code in LEG_SELECTIONS["ttg"]
    assert set(LEG_MARKETS) >= {"had", "hhad", "crs", "ttg", "hafu", "htft6"}


def test_json_schema_snapshot_key_constraints() -> None:
    """correctness F5：schema 关键约束快照——stake 上下界/玩法枚举/锁价下界。"""
    from goalx_backend.betting import execution_order as eo
    from goalx_backend.markets import LEG_MARKETS

    schema = eo.model_json_schema()
    order_props = schema["ExecutionOrder"]["properties"]
    assert order_props["stake"]["exclusiveMinimum"] == 0
    assert order_props["stake"]["maximum"] == 1e9  # 文档口径：MoneyCny 上界
    line_props = schema["ExecutionLine"]["properties"]
    assert line_props["market_code"]["enum"] == sorted(LEG_MARKETS)
    odds_branches = line_props["locked_odds"]["anyOf"]
    number_branch = next(b for b in odds_branches if b.get("type") == "number")
    assert number_branch["exclusiveMinimum"] == 1
