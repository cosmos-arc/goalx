"""auto-paper 票 04：纸面账户 P&L 面。

钉住五件事：曲线按结算日累计含初始、最大回撤、待结算浮仓单列（open
注+彩池未开奖组合绝不进已实现）、auto-v1 与人工分列不混数、税后
（tax_on_prize 逐注奖金口径）与样本不足旗标。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend.betting import store as bt_store
from goalx_backend.betting.bets import (
    BetDraft,
    create_bet_with_legs,
    intent_fingerprint,
)
from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.db import connect, migrate
from goalx_backend.main import create_app
from goalx_backend.models import (
    BetMode,
    BetStatus,
    LegInput,
    MatchCodeInput,
    SaleStatusInput,
    SettlementInput,
    SnapshotInput,
    Tier,
)

KICKOFF = "2026-11-08T15:00:00+00:00"


def _fixture(db: sqlite3.Connection, tag: str, fid: int) -> int:
    """一场带当日竞彩码的场次；返回 fixture id（自增，不假设具体值）。"""
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"主队{tag}")
    away = fx_store.upsert_team(db, f"客队{tag}")
    fixture = fx_store.upsert_fixture(db, competition, KICKOFF, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date="2026-11-08",
            code=f"周六00{fid}",
        ),
    )
    fx_store.append_sale_status(
        db,
        SaleStatusInput(
            fixture_id=fixture,
            market_code=None,
            sale_state="on_sale",
            single_eligible=True,
            observed_at="2026-11-08T12:50:00+00:00",
        ),
    )
    fx_store.insert_odds_snapshot(
        db,
        SnapshotInput(
            fixture_id=fixture,
            market_code="ttg",
            selection_code="2",
            source="sporttery",
            odds=2.0,
            captured_at="2026-11-08T12:50:00+00:00",
            observed_at="2026-11-08T12:50:00+00:00",
        ),
    )
    db.commit()
    return fixture


def _bet(
    db: sqlite3.Connection,
    *,
    stake: float,
    strategy: str | None,
    fid: int,
) -> int:
    legs = [
        LegInput(fixture_id=fid, market_code="ttg", selection_code="2", locked_odds=2.0)
    ]
    draft = BetDraft(
        mode=BetMode.PAPER,
        stake=stake,
        legs=legs,
        strategy_version=strategy,
        client_request_id=f"acct:{fid}:{strategy}:{stake}",
        intent_fingerprint=intent_fingerprint(BetMode.PAPER, stake, legs),
    )
    return create_bet_with_legs(db, draft)


def _settle(
    db: sqlite3.Connection,
    bet_id: int,
    *,
    status: BetStatus,
    profit: float,
    payout: float,
    settled_at: str,
) -> None:
    stake = float(
        db.execute("SELECT stake FROM bets WHERE id=?", (bet_id,)).fetchone()["stake"]
    )
    bt_store.save_settlement(
        db,
        SettlementInput(
            bet_id=bet_id,
            status=status,
            stake=stake,
            payout=payout,
            profit=profit,
            detail={"test": True},
        ),
    )
    # 结算日钉死（save_settlement 用内部时刻，此处测试口径覆写）
    db.execute("UPDATE bets SET settled_at=? WHERE id=?", (settled_at, bet_id))
    db.commit()


@pytest.fixture
def seeded_client(tmp_path: Path) -> Iterator[TestClient]:
    """合成账栈：auto-v1 两注（won/lost）+人工两注（won/open）+彩池待开奖。"""
    db_path = tmp_path / "account.db"
    db = connect(db_path)
    migrate(db)
    f1, f2, f3, f4 = (
        _fixture(db, tag, fid)
        for fid, tag in ((1, "甲"), (2, "乙"), (3, "丙"), (4, "丁"))
    )
    b1 = _bet(db, stake=2.0, strategy="auto-v1", fid=f1)
    b2 = _bet(db, stake=2.0, strategy="auto-v1", fid=f2)
    b3 = _bet(db, stake=10.0, strategy=None, fid=f3)
    _bet(db, stake=5.0, strategy=None, fid=f4)
    # auto-v1：day1 won +¥2（payout 6 免税），day2 lost −¥2
    _settle(
        db,
        b1,
        status=BetStatus.WON,
        profit=2.0,
        payout=6.0,
        settled_at="2026-11-09T10:00:00+00:00",
    )
    _settle(
        db,
        b2,
        status=BetStatus.LOST,
        profit=-2.0,
        payout=0.0,
        settled_at="2026-11-10T10:00:00+00:00",
    )
    # 人工：day1 won 大奖（payout 15000 → 超限全额 20% 税 3000）；
    # 一注 open=浮仓 ¥5
    _settle(
        db,
        b3,
        status=BetStatus.WON,
        profit=14990.0,
        payout=15000.0,
        settled_at="2026-11-09T11:00:00+00:00",
    )
    # 彩池待开奖组合（浮仓池面 ¥4）
    slip = bt_store.create_slip(db, BetMode.PAPER, source="auto-v1")
    bt_store.add_pool_pick(db, slip, 1, "3", fixture_id=1)
    db.commit()
    bt_store.materialize_combinations(
        db, slip, stake_per_combination=4.0, market_code="ttt14"
    )
    db.commit()
    db.close()
    with TestClient(
        create_app(
            settings=Settings(db_path=db_path, research_db_path=tmp_path / "r.db")
        )
    ) as test_client:
        yield test_client


def test_account_face_curve_drawdown_floating_split_tax(
    seeded_client: TestClient,
) -> None:
    resp = seeded_client.get("/api/v1/paper-account")
    assert resp.status_code == 200
    face = resp.json()

    # 两策略分列不混数
    by_strategy = {row["strategy"]: row for row in face["strategies"]}
    assert set(by_strategy) == {"auto-v1", "manual"}
    auto = by_strategy["auto-v1"]
    manual = by_strategy["manual"]
    assert auto["bets_n"] == 2
    assert auto["settled_n"] == 2
    assert manual["bets_n"] == 2
    assert manual["settled_n"] == 1
    assert manual["open_n"] == 1

    # 曲线按结算日累计含初始：auto day1=1002、day2=1000；回撤=峰谷差
    assert auto["curve"] == [
        {"date": "2026-11-09", "value": 1002.0},
        {"date": "2026-11-10", "value": 1000.0},
    ]
    assert auto["max_drawdown"] == pytest.approx(2.0)
    # 全账
    assert face["total_profit_settled"] == pytest.approx(14990.0)  # 2−2+14990
    assert face["bankroll_now"] == pytest.approx(15990.0)

    # 浮仓单列：open 注 ¥5+彩池未开奖 ¥4，绝不进已实现
    assert face["floating_stake_fixed"] == pytest.approx(5.0)
    assert face["floating_stake_pool"] == pytest.approx(4.0)

    # 税后：奖金 6 免税+15000 税 3000；净利 =(6−2)+(12000−10)+(−2)=11992
    assert face["tax_total"] == pytest.approx(3000.0)
    assert face["profit_after_tax"] == pytest.approx(11992.0)

    # 样本不足旗标（4 注 < 200）
    assert face["sample_sufficient"] is False

    # 分玩法：ttg 三注已结算（auto 两注+人工一注）
    markets = {row["market"]: row for row in face["per_market"]}
    assert markets["ttg"]["bets_n"] == 3


def test_manual_null_strategy_grouping(seeded_client: TestClient) -> None:
    """人工纸面=strategy_version IS NULL 归 manual（EV 快照缺失列 null 不冒充）。"""
    face = seeded_client.get("/api/v1/paper-account").json()
    manual = next(r for r in face["strategies"] if r["strategy"] == "manual")
    assert manual["mean_decision_ev"] is None
    assert manual["ev_realization_gap"] is None


def test_open_strategy_set_and_void_denominator(tmp_path: Path) -> None:
    """correctness F1/F2：具名策略显式成面不丢账；void 退款不进 ROI 分母。"""
    db_path = tmp_path / "open.db"
    db = connect(db_path)
    migrate(db)
    b_exp = _bet(db, stake=100.0, strategy="exp-a", fid=_fixture(db, "亥", 6))
    b_void = _bet(db, stake=100.0, strategy=None, fid=_fixture(db, "子", 7))
    _settle(
        db,
        b_exp,
        status=BetStatus.LOST,
        profit=-100.0,
        payout=0.0,
        settled_at="2026-11-09T10:00:00+00:00",
    )
    _settle(
        db,
        b_void,
        status=BetStatus.VOID,
        profit=0.0,
        payout=100.0,
        settled_at="2026-11-09T10:00:00+00:00",
    )
    db.close()
    with TestClient(
        create_app(
            settings=Settings(db_path=db_path, research_db_path=tmp_path / "r.db")
        )
    ) as client:
        face = client.get("/api/v1/paper-account").json()
        # F1：exp-a 显式成面，账户不漏项（1000−100=900）
        names = {row["strategy"] for row in face["strategies"]}
        assert names == {"auto-v1", "manual", "exp-a"}
        assert face["bankroll_now"] == pytest.approx(900.0)
        assert face["total_bets_n"] == 2
        # F2：void（退款）不计 ROI 分母——manual 已结算 1 注但分母 0
        manual = next(r for r in face["strategies"] if r["strategy"] == "manual")
        assert manual["stake_settled"] == pytest.approx(0.0)
        assert manual["realized_roi"] is None
