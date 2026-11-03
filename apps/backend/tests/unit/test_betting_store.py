"""投注域仓储测试。"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from goalx_backend.betting import store as bt
from goalx_backend.data import fixtures as fx
from goalx_backend.models import (
    BetMode,
    BetStatus,
    LegInput,
    MarketKind,
    SettlementInput,
)


def seed_fixtures(db: sqlite3.Connection, count: int) -> list[int]:
    """插入 count 场比赛，返回 fixture id 列表。"""
    comp = fx.upsert_competition(db, "英超")
    home = fx.upsert_team(db, "阿森纳")
    away = fx.upsert_team(db, "切尔西")
    return [
        fx.upsert_fixture(db, comp, f"2026-09-1{day}T10:00:00+00:00", home, away)
        for day in range(count)
    ]


def test_create_bet_with_legs_and_list(db: sqlite3.Connection) -> None:
    first_fixture, second_fixture, _ = seed_fixtures(db, 3)
    bet = bt.create_bet(db, BetMode.PAPER, MarketKind.FIXED, 2.0)
    bt.add_leg(
        db,
        bet,
        LegInput(
            fixture_id=first_fixture,
            market_code="had",
            selection_code="h",
            locked_odds=2.10,
        ),
    )
    bt.add_leg(
        db,
        bet,
        LegInput(
            fixture_id=second_fixture,
            market_code="had",
            selection_code="a",
            locked_odds=3.40,
            goal_line=-0.5,
        ),
    )
    rows = bt.list_bets(db)
    assert len(rows) == 1
    legs = json.loads(rows[0]["legs"])
    assert [leg["fixture_id"] for leg in legs] == [first_fixture, second_fixture]
    assert legs[1]["goal_line"] == -0.5


def test_list_bets_filter_mode_and_open(db: sqlite3.Connection) -> None:
    bt.create_bet(db, BetMode.PAPER, MarketKind.FIXED, 2.0)
    live = bt.create_bet(db, BetMode.LIVE, MarketKind.FIXED, 2.0, purchased=True)
    assert len(bt.list_bets(db, mode=BetMode.LIVE)) == 1
    assert len(bt.list_bets(db, only_open=True)) == 2
    bt.save_settlement(
        db,
        SettlementInput(
            bet_id=live,
            status=BetStatus.WON,
            stake=2.0,
            payout=4.2,
            profit=2.2,
            detail={},
        ),
    )
    assert len(bt.list_bets(db, only_open=True)) == 1


def test_settlement_updates_bet_and_upserts(db: sqlite3.Connection) -> None:
    bet = bt.create_bet(db, BetMode.PAPER, MarketKind.FIXED, 2.0)
    bt.save_settlement(
        db,
        SettlementInput(
            bet_id=bet,
            status=BetStatus.WON,
            stake=2.0,
            payout=4.2,
            profit=2.2,
            detail={"k": "v"},
        ),
    )
    bt.save_settlement(
        db,
        SettlementInput(
            bet_id=bet,
            status=BetStatus.LOST,
            stake=2.0,
            payout=0.0,
            profit=-2.0,
            detail={"k": "v2"},
        ),
    )
    row = bt.get_bet(db, bet)
    assert row is not None
    assert row["status"] == "lost"
    count = db.execute("SELECT COUNT(*) AS n FROM settlements").fetchone()["n"]
    assert count == 1  # upsert 而非重复行


def test_slip_attach_sets_purchased(db: sqlite3.Connection) -> None:
    suggestion = bt.create_bet(db, BetMode.PAPER, MarketKind.FIXED, 2.0)
    other = bt.create_bet(db, BetMode.PAPER, MarketKind.FIXED, 4.0)
    slip = bt.create_slip(db, BetMode.PAPER, placed_at=None, note="周六")
    attached = bt.attach_bets_to_slip(
        db, slip, [suggestion], "2026-09-13T02:00:00+00:00"
    )
    assert attached == 1
    rows = {row["id"]: row for row in bt.list_bets(db)}
    assert rows[suggestion]["purchased"] == 1
    assert rows[suggestion]["slip_id"] == slip
    assert rows[other]["purchased"] == 0
    slips = bt.list_slips(db)
    assert slips[0]["bet_count"] == 1


def test_materialize_combinations_ttt14_cartesian(db: sqlite3.Connection) -> None:
    """ttt14（14 场整体连乘）口径 = 旧笛卡尔积行为不动。"""
    slip = bt.create_slip(db, BetMode.PAPER)
    for sel in ("3", "1"):
        bt.add_pool_pick(db, slip, 1, sel)
    bt.add_pool_pick(db, slip, 2, "0")
    n = bt.materialize_combinations(db, slip, market_code="ttt14")
    assert n == 2  # 2 × 1
    rows = db.execute(
        "SELECT selections FROM combinations WHERE slip_id = ? ORDER BY seq", (slip,)
    ).fetchall()
    first = json.loads(rows[0]["selections"])
    assert first == [
        {"match_seq": 1, "selection_code": "1"},
        {"match_seq": 2, "selection_code": "0"},
    ]
    # 重跑替换而非累加
    assert bt.materialize_combinations(db, slip, market_code="ttt14") == 2
    count = db.execute(
        "SELECT COUNT(*) AS n FROM combinations WHERE slip_id = ?", (slip,)
    ).fetchone()["n"]
    assert count == 2


def _pick9_matrix(n: int, double_first: bool = False) -> dict[int, list[str]]:
    """黄金样例矩阵：n 场，单选；double_first 时场 1 双选。"""
    matrix = {seq: ["3"] for seq in range(1, n + 1)}
    if double_first:
        matrix[1] = ["1", "3"]
    return matrix


@pytest.mark.parametrize(
    ("n", "double_first", "expected"),
    [
        (9, False, 1),  # 9 场单选 = 1 注 2 元
        (10, False, 10),  # 10 场单选 = C(10,9) = 10 注 20 元
        (14, False, 2002),  # 14 场单选 = C(14,9) = 2002 注 4004 元
        (10, True, 19),  # 10 场、场 1 双选 = 含场 1 的 9 子集×2 + 不含的 1 子集×1
    ],
)
def test_count_pool_combinations_pick9_golden(
    n: int, double_first: bool, expected: int
) -> None:
    """票 01 黄金样例：任9 = 全部 9 场子集 × 各子集内逐场 picks 之积。"""
    assert (
        bt.count_pool_combinations(_pick9_matrix(n, double_first), "pick9") == expected
    )


def test_count_pool_combinations_pick9_requires_nine_matches() -> None:
    with pytest.raises(ValueError, match="任9 需至少 9 场"):
        bt.count_pool_combinations(_pick9_matrix(8), "pick9")


def test_count_pool_combinations_rejects_empty_matrix() -> None:
    """空矩阵守卫：连乘口径空积=1 不得静默成一条零腿组合。"""
    with pytest.raises(ValueError, match="无已选场次"):
        bt.count_pool_combinations({}, "ttt14")
    with pytest.raises(ValueError, match="无已选场次"):
        bt.count_pool_combinations({}, "pick9")


def test_count_pool_combinations_rejects_unknown_play() -> None:
    with pytest.raises(ValueError, match="未知池票 Market"):
        bt.count_pool_combinations(_pick9_matrix(9), "goals4")


def test_materialize_combinations_pick9_golden(db: sqlite3.Connection) -> None:
    """任9 物化黄金样例 + 恒 9 腿 + 幂等 + 回读聚合（票 01 三方一致）。"""
    slip = bt.create_slip(db, BetMode.PAPER)
    for seq in range(1, 11):  # 10 场单选 → 10 注
        bt.add_pool_pick(db, slip, seq, "3")
    assert bt.materialize_combinations(db, slip) == 10
    rows = db.execute(
        "SELECT selections, stake FROM combinations WHERE slip_id = ? ORDER BY seq",
        (slip,),
    ).fetchall()
    assert len(rows) == 10
    assert {row["stake"] for row in rows} == {2.0}
    legs_seen = set()
    for row in rows:
        selections = json.loads(row["selections"])
        assert len(selections) == 9  # 任9 恒 9 腿
        legs_seen.add(tuple(sel["match_seq"] for sel in selections))
    # 10 场全选 → 子集 = 缺一场的 10 种
    assert legs_seen == {
        tuple(s for s in range(1, 11) if s != dropped) for dropped in range(1, 11)
    }
    # 幂等重跑
    assert bt.materialize_combinations(db, slip) == 10
    # SlipView 聚合回读（活口径）
    agg = bt.get_slip(db, slip)
    assert agg is not None
    assert agg["combination_count"] == 10
    assert agg["pool_stake_total"] == 20.0
    assert agg["quarantined_combinations"] == 0


def test_materialize_combinations_rejects_over_limit(
    db: sqlite3.Connection,
) -> None:
    """票 07④：组合数超上限先算后拒——拒绝发生在任何写入之前。"""
    slip = bt.create_slip(db, BetMode.PAPER)
    for seq in range(1, 15):  # 14 场 × 3 向任9 = C(14,9)×3^9 ≈ 3940 万 > 10 万
        for sel in ("3", "1", "0"):
            bt.add_pool_pick(db, slip, seq, sel)
    with pytest.raises(ValueError, match="超上限"):
        bt.materialize_combinations(db, slip)
    # 拒绝零写入：无组合行，也无半截删除/插入
    n = db.execute(
        "SELECT COUNT(*) AS n FROM combinations WHERE slip_id = ?", (slip,)
    ).fetchone()["n"]
    assert n == 0


def test_materialize_pick9_over_limit_subset_sum(
    db: sqlite3.Connection,
) -> None:
    """票 01：任9 超限按子集积口径判——14 场×2 选 = C(14,9)×2^9 ≈ 102.5 万。"""
    slip = bt.create_slip(db, BetMode.PAPER)
    for seq in range(1, 15):
        for sel in ("1", "3"):
            bt.add_pool_pick(db, slip, seq, sel)
    with pytest.raises(bt.CombinationLimitExceeded, match="累加至"):
        bt.materialize_combinations(db, slip)
    n = db.execute(
        "SELECT COUNT(*) AS n FROM combinations WHERE slip_id = ?", (slip,)
    ).fetchone()["n"]
    assert n == 0


def test_materialize_combinations_preserves_prior_rows_on_reject(
    db: sqlite3.Connection,
) -> None:
    """超限拒绝不动已物化的上一轮组合（幂等面不被毒化）。"""
    slip = bt.create_slip(db, BetMode.PAPER)
    for sel in ("3", "1"):
        bt.add_pool_pick(db, slip, 1, sel)
    bt.add_pool_pick(db, slip, 2, "0")
    assert bt.materialize_combinations(db, slip, market_code="ttt14") == 2
    # 追加复式面致超限（2×3^13 连乘远超 10 万）
    for seq in range(2, 15):
        for sel in ("3", "1", "0"):
            bt.add_pool_pick(db, slip, seq, sel)
    with pytest.raises(ValueError, match="超上限"):
        bt.materialize_combinations(db, slip, market_code="ttt14")
    count = db.execute(
        "SELECT COUNT(*) AS n FROM combinations WHERE slip_id = ?", (slip,)
    ).fetchone()["n"]
    assert count == 2


def test_pool_selections_sha256_matches_materialized_rows(
    db: sqlite3.Connection,
) -> None:
    """票 01 可校验摘要：预览摘要 == 物化后按 DB 行重算的摘要（同生成序）。"""
    picks = [(1, "3"), (1, "1")] + [(seq, "3") for seq in range(2, 10)]
    matrix = bt.pool_pick_matrix(picks)  # 排序单源：与落库回读同展开序
    slip = bt.create_slip(db, BetMode.PAPER)
    for seq, sel in picks:
        bt.add_pool_pick(db, slip, seq, sel)
    expected = bt.pool_selections_sha256(matrix, "pick9")
    assert bt.materialize_combinations(db, slip) == 2
    rows = db.execute(
        "SELECT selections FROM combinations WHERE slip_id = ? ORDER BY seq", (slip,)
    ).fetchall()
    recomputed = hashlib.sha256()
    for row in rows:
        recomputed.update(
            json.dumps(
                json.loads(row["selections"]),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )
        recomputed.update(b"\n")
    assert recomputed.hexdigest() == expected


def test_pool_pick_matrix_dedupes_and_sorts() -> None:
    """预览/创建同矩阵：载荷重复行与次序无关性（去重+排序单源）。"""
    assert bt.pool_pick_matrix([(2, "0"), (1, "3"), (1, "1"), (1, "3")]) == {
        1: ["1", "3"],
        2: ["0"],
    }


def _legacy_combination_row(
    db: sqlite3.Connection, slip_id: int, seq: int, legs: int
) -> None:
    """直接落一行旧口径组合（legs 条腿）——绕开物化器模拟历史数据。"""
    db.execute(
        "INSERT INTO combinations (slip_id, seq, stake, selections) VALUES (?,?,?,?)",
        (
            slip_id,
            seq,
            2.0,
            json.dumps(
                [{"match_seq": i, "selection_code": "3"} for i in range(1, legs + 1)]
            ),
        ),
    )


def test_materialize_pick9_full_cover_14_matches(db: sqlite3.Connection) -> None:
    """任9 全覆盖黄金样例：14 场单选 = 2002 行/4004 元（物化与回读两当面）。"""
    slip = bt.create_slip(db, BetMode.PAPER)
    for seq in range(1, 15):
        bt.add_pool_pick(db, slip, seq, "3")
    assert bt.materialize_combinations(db, slip) == 2002
    count = db.execute(
        "SELECT COUNT(*) AS n FROM combinations WHERE slip_id = ?", (slip,)
    ).fetchone()["n"]
    assert count == 2002  # 返回计数与实插行数同源同值
    agg = bt.get_slip(db, slip)
    assert agg is not None
    assert agg["combination_count"] == 2002
    assert agg["pool_stake_total"] == 4004.0


def test_v36_quarantines_legacy_cartesian_rows(db: sqlite3.Connection) -> None:
    """票 01 人裁决：任9 旧笛卡尔积史行标记隔离——原行保留、只标不改写。"""
    from goalx_backend.data.pool import upsert_pool_period
    from goalx_backend.migrations import _apply_v36

    legacy = bt.create_slip(db, BetMode.PAPER)  # 无期次 → 任9 归因
    _legacy_combination_row(db, legacy, 0, 8)  # n<9 笛卡尔积
    _legacy_combination_row(db, legacy, 1, 10)  # n>9 笛卡尔积（10 腿整票）
    _legacy_combination_row(db, legacy, 2, 9)  # 9 腿 = 两口径等价 → 不标
    # 挂期次的史行同规则（任9 页是唯一池票来源，不按期次 market 分叉）：
    # 12 腿与 14 腿均 ≠9 → 标记隔离（14 腿的"真整体票"歧义见 v36 docstring）
    goals4_period = upsert_pool_period(
        db, "goals4", "9999", "2026-11-04T12:30:00+00:00"
    )
    goals4_slip = bt.create_slip(db, BetMode.PAPER, pool_period_id=goals4_period)
    _legacy_combination_row(db, goals4_slip, 0, 12)
    ttt14_period = upsert_pool_period(db, "ttt14", "9998", "2026-11-04T12:30:00+00:00")
    ttt14_slip = bt.create_slip(db, BetMode.PAPER, pool_period_id=ttt14_period)
    _legacy_combination_row(db, ttt14_slip, 0, 14)

    _apply_v36(db)
    marks = {
        (row["slip_id"], row["seq"]): row["quarantine_reason"]
        for row in db.execute(
            "SELECT slip_id, seq, quarantine_reason FROM combinations"
        )
    }
    assert marks[(legacy, 0)] == "legacy_cartesian_pick9_20261103"
    assert marks[(legacy, 1)] == "legacy_cartesian_pick9_20261103"
    assert marks[(legacy, 2)] is None
    assert marks[(goals4_slip, 0)] == "legacy_cartesian_pick9_20261103"
    assert marks[(ttt14_slip, 0)] == "legacy_cartesian_pick9_20261103"

    # SlipView 活口径排除隔离行 + 隔离计数单列（对账注记）
    agg = bt.get_slip(db, legacy)
    assert agg is not None
    assert agg["combination_count"] == 1
    assert agg["pool_stake_total"] == 2.0
    assert agg["quarantined_combinations"] == 2

    # 重入幂等：只标未标行
    _apply_v36(db)
    marks2 = {
        (row["slip_id"], row["seq"]): row["quarantine_reason"]
        for row in db.execute(
            "SELECT slip_id, seq, quarantine_reason FROM combinations"
        )
    }
    assert marks2 == marks


def test_bankroll_events(db: sqlite3.Connection) -> None:
    bet = bt.create_bet(db, BetMode.LIVE, MarketKind.FIXED, 2.0)
    assert bt.bankroll_balance(db) is None
    bt.record_bankroll_event(db, "deposit", 5000.0, note="初始资金")
    bt.record_bankroll_event(db, "bet_payout", 4.2, bet_id=bet)
    assert bt.bankroll_balance(db) == 5004.2
    events = bt.list_bankroll_events(db)
    assert events[0]["kind"] == "bet_payout"
    assert events[1]["balance_after"] == 5000.0


def test_record_deposit(db: sqlite3.Connection) -> None:
    """票 20 入金领域函数：kind=deposit 落账、余额累计、occurred_at 缺省取当前。"""
    first = bt.record_deposit(db, 5000.0, note="初始资金")
    assert first["kind"] == "deposit"
    assert first["amount_cny"] == 5000.0
    assert first["balance_after"] == 5000.0
    assert first["bet_id"] is None
    assert first["note"] == "初始资金"
    assert bt.bankroll_balance(db) == 5000.0

    second = bt.record_deposit(db, 250.5, occurred_at="2026-09-01T00:00:00+00:00")
    assert second["balance_after"] == 5250.5
    assert second["occurred_at"] == "2026-09-01T00:00:00+00:00"
    assert second["note"] is None
    assert bt.bankroll_balance(db) == 5250.5

    for bad in (0.0, -100.0):
        with pytest.raises(ValueError, match="入金金额必须大于 0"):
            bt.record_deposit(db, bad)
    assert bt.bankroll_balance(db) == 5250.5  # 非法金额不落账
