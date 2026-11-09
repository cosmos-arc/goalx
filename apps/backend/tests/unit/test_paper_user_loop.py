"""纸面用户闭环服务层测试（票 36：资格判定、实际条款、影响预览）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from goalx_backend.betting.bets import (
    ActualLegOdds,
    ActualTerms,
    BetDraft,
    BetEligibilityError,
    create_bet_with_legs,
    record_purchase,
    validate_selections,
)
from goalx_backend.betting.settle import preview_draw_result_change, run_settlement
from goalx_backend.data.ingest.demo import DemoSeedRefused, seed_demo
from goalx_backend.data.ingest.results import import_draw_results
from goalx_backend.models import DrawResultInput, LegInput


def _had_leg(fixture_id: int, odds: float = 2.0) -> LegInput:
    return LegInput(
        fixture_id=fixture_id,
        market_code="had",
        selection_code="h",
        locked_odds=odds,
    )


@pytest.fixture
def demo(db: sqlite3.Connection) -> dict[str, object]:
    info = seed_demo(db)
    assert isinstance(info["fixture_ids"], list)
    return info


def ids(demo: dict[str, object]) -> tuple[int, int, int]:
    fid = demo["fixture_ids"]
    assert isinstance(fid, list)
    return int(fid[0]), int(fid[1]), int(fid[2])


def test_validate_selections_rules(db: sqlite3.Connection, demo) -> None:
    single_ok, parlay_only, stopped = ids(demo)

    validate_selections(db, [_had_leg(single_ok)])  # 单固在售 → 通过
    with pytest.raises(BetEligibilityError, match="单固"):
        validate_selections(db, [_had_leg(parlay_only)])
    # 串关共同可购买：两腿同一 as_of 判定均非拒绝
    validate_selections(db, [_had_leg(single_ok), _had_leg(parlay_only)])
    with pytest.raises(BetEligibilityError, match="sale_stopped"):
        validate_selections(db, [_had_leg(single_ok), _had_leg(stopped)])
    # 已开赛(未来 as_of)拒绝
    kickoff = str(
        db.execute(
            "SELECT kickoff_utc FROM fixtures WHERE id = ?", (single_ok,)
        ).fetchone()["kickoff_utc"]
    )
    after_kickoff = datetime.fromisoformat(kickoff).isoformat(timespec="seconds")
    with pytest.raises(BetEligibilityError, match="kickoff_passed"):
        validate_selections(db, [_had_leg(single_ok)], as_of=after_kickoff)
    # 空 evidence 场次：单关资格未知 → 拒绝(不倒填资格)
    blank = db.execute("SELECT MAX(id) + 100 AS id FROM fixtures").fetchone()["id"]
    with pytest.raises(BetEligibilityError, match="不可投"):
        validate_selections(db, [_had_leg(int(blank))])


def test_non_had_leg_gets_market_level_validation(db: sqlite3.Connection, demo) -> None:
    """票 14：非 had 市场走玩法级资格（在售 + 单关）——不再整体跳过。

    demo 种子的 hhad 无市场行 → 比赛级行不携带单固资格 → 单关未知即拒；
    串关腿（两腿）无单关要求，销售态按比赛级行在售 → 通过。
    """
    single_ok, parlay_only, _ = ids(demo)
    hhad_leg = LegInput(
        fixture_id=single_ok,
        market_code="hhad",
        selection_code="h",
        locked_odds=2.0,
        goal_line=-1.0,
    )
    # 单关：hhad 单固资格未知（demo 无 hhad 市场行）→ 拒
    with pytest.raises(BetEligibilityError, match=r"hhad.*single_unknown"):
        validate_selections(db, [hhad_leg])
    # 串关：不同场两腿，无单关要求 → 通过
    validate_selections(db, [hhad_leg, _had_leg(parlay_only)])


def test_record_purchase_actual_terms_flow(db: sqlite3.Connection, demo) -> None:
    """真实回录：实际条款结算、建议快照保留、决策身份取实际赔率。"""
    single_ok, _, _ = ids(demo)
    bet = create_bet_with_legs(
        db,
        BetDraft(mode="live", stake=10, legs=[_had_leg(single_ok, odds=2.0)]),  # type: ignore[arg-type]
    )
    record_purchase(
        db,
        [bet],
        actuals={
            bet: ActualTerms(
                stake=12, leg_odds=[ActualLegOdds(fixture_id=single_ok, odds=1.9)]
            )
        },
    )
    from goalx_backend.betting import store as bt_store

    assert bt_store.bankroll_balance(db) == -12.0  # 按实际金额扣款
    row = bt_store.get_bet(db, bet)
    assert row["stake"] == 10.0  # 建议不动
    assert row["actual_stake"] == 12.0
    legs = json.loads(row["legs"])
    assert legs[0]["locked_odds"] == 2.0
    assert legs[0]["actual_odds"] == 1.9

    import_draw_results(
        db, [DrawResultInput(fixture_id=single_ok, home_goals=2, away_goals=0)]
    )
    run_settlement(db)
    settled = bt_store.get_bet(db, bet)
    assert settled["payout"] == pytest.approx(22.8)
    assert settled["profit"] == pytest.approx(10.8)
    settled_view = {b.bet_id: b for b in bt_store.settled_purchased_bets(db)}[bet]
    assert settled_view.stake == 12.0  # 验证口径取实际条款
    assert settled_view.legs[0].locked_odds == 1.9
    # 票 03(a)：票级票金按有效执行金额聚合——三面一致（票金 12 = 流水
    # −12 = 结算 12×1.9）；修复前 stake_total 仍显建议金额 ¥10
    slip_row = bt_store.get_slip(db, row["slip_id"])
    assert slip_row["stake_total"] == pytest.approx(12.0)


def test_record_purchase_rejects_unknown_actual_leg(
    db: sqlite3.Connection, demo
) -> None:
    single_ok, parlay_ok, _ = ids(demo)
    bet = create_bet_with_legs(
        db,
        BetDraft(mode="live", stake=10, legs=[_had_leg(single_ok)]),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="不存在的腿"):
        record_purchase(
            db,
            [bet],
            actuals={
                bet: ActualTerms(
                    stake=12,
                    leg_odds=[ActualLegOdds(fixture_id=parlay_ok, odds=1.9)],
                )
            },
        )


def test_preview_draw_result_change(db: sqlite3.Connection, demo) -> None:
    single_ok, _, _ = ids(demo)
    bet = create_bet_with_legs(
        db,
        BetDraft(mode="paper", stake=100, legs=[_had_leg(single_ok, odds=6.5)]),  # type: ignore[arg-type]
    )
    record_purchase(db, [bet])
    import_draw_results(
        db, [DrawResultInput(fixture_id=single_ok, home_goals=3, away_goals=1)]
    )
    run_settlement(db)

    preview = preview_draw_result_change(
        db,
        [DrawResultInput(fixture_id=single_ok, home_goals=0, away_goals=1)],
    )
    change = preview.results[0]
    assert change.is_correction is True
    assert change.previous["home_goals"] == 3
    affected = preview.affected_bets[0]
    assert affected.bet_id == bet
    assert affected.status_current == "won"
    assert affected.payout_current == 650.0
    assert affected.status_projected == "lost"
    assert affected.delta_payout == -650.0
    # 只读：落库状态不被预览改变
    from goalx_backend.betting import store as bt_store

    assert bt_store.get_bet(db, bet)["payout"] == 650.0


def test_preview_correction_covers_all_parlay_legs(
    db: sqlite3.Connection, demo
) -> None:
    """票 A1：多腿更正预览的投影域=注的全部腿 fixture——payload 外的腿用
    现有事实，不得谎报 open 让 −¥60 冲正绕过操作者审批门。"""
    single_ok, parlay_ok, _ = ids(demo)
    bet = create_bet_with_legs(
        db,
        BetDraft(
            mode="paper",
            stake=10,
            legs=[_had_leg(single_ok, odds=2.0), _had_leg(parlay_ok, odds=3.0)],
        ),  # type: ignore[arg-type]
    )
    record_purchase(db, [bet])
    import_draw_results(
        db,
        [
            DrawResultInput(fixture_id=single_ok, home_goals=2, away_goals=0),
            DrawResultInput(fixture_id=parlay_ok, home_goals=3, away_goals=1),
        ],
    )
    run_settlement(db)
    from goalx_backend.betting import store as bt_store

    assert bt_store.get_bet(db, bet)["payout"] == pytest.approx(60.0)  # 10×2×3

    preview = preview_draw_result_change(
        db,
        [DrawResultInput(fixture_id=single_ok, home_goals=0, away_goals=1)],
    )
    affected = preview.affected_bets[0]
    assert affected.status_current == "won"
    assert affected.status_projected == "lost"  # 修复前=open（另一腿事实缺失）
    assert affected.delta_payout == -60.0


def test_seed_demo_refuses_non_demo_db(db: sqlite3.Connection, demo) -> None:
    """种子只写隔离库：已有非 demo 竞彩数据时拒绝（不伪造实采）。"""
    from goalx_backend.data.fixtures import CST, beijing_business_date
    from goalx_backend.data.ingest import sporttery

    sporttery.store_matches(
        db,
        sporttery.parse_matches(
            {
                "errorCode": "0",
                "value": {
                    "matchInfoList": [
                        {
                            "businessDate": beijing_business_date(),
                            "subMatchList": [
                                {
                                    "matchId": 99,
                                    "matchNumStr": "周日999",
                                    "leagueAbbName": "英超",
                                    "homeTeamAllName": "真实队A",
                                    "awayTeamAllName": "真实队B",
                                    "matchDate": datetime.now(UTC)
                                    .astimezone(CST)
                                    .date()
                                    .isoformat(),
                                    "matchTime": "23:00:00",
                                    "bettingSingle": 1,
                                    "had": {
                                        "h": "2.00",
                                        "d": "3.00",
                                        "a": "3.00",
                                        "updateDate": datetime.now(UTC)
                                        .astimezone(CST)
                                        .date()
                                        .isoformat(),
                                        "updateTime": "12:00:00",
                                    },
                                }
                            ],
                        }
                    ]
                },
            }
        ),
    )
    with pytest.raises(DemoSeedRefused):
        seed_demo(db)
