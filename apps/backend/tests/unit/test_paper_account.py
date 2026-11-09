"""auto-paper 票 04：纸面账户 P&L 面。

钉住五件事：曲线按结算日累计含初始、最大回撤、待结算浮仓单列（open
注+彩池未开奖组合绝不进已实现）、auto-v1 与人工分列不混数、税后与
样本不足旗标。edge-audit 票 13 反例钉口径：税后=同人+同期+同游戏
归集计税；sample_sufficient 分母=独立暴露（已结算、非 void、按场
去重）；ROI 双栏=等注口径与按场口径并列。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend.betting import store as bt_store
from goalx_backend.betting.account import paper_account_face
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


def _fixture(
    db: sqlite3.Connection, tag: str, fid: int, *, business_date: str = "2026-11-08"
) -> int:
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
            business_date=business_date,
            code=f"{business_date}周六00{fid}",
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
    req_tag: str = "",
) -> int:
    legs = [
        LegInput(fixture_id=fid, market_code="ttg", selection_code="2", locked_odds=2.0)
    ]
    draft = BetDraft(
        mode=BetMode.PAPER,
        stake=stake,
        legs=legs,
        strategy_version=strategy,
        client_request_id=f"acct:{fid}:{strategy}:{stake}:{req_tag}",
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

    # 税后（票 13 归集口径）：¥6 与 ¥15000 同期（2026-11-08）同游戏归集
    # 15006 → 超限全额 20% 税 3001.2（旧逐注口径为 3000，重算差异如实呈现）；
    # 净利 = 15006 − 3001.2 − 总注 14 = 11990.8
    assert face["tax_total"] == pytest.approx(3001.2)
    assert face["profit_after_tax"] == pytest.approx(11990.8)

    # 样本不足旗标：独立暴露 3 场（f1/f2/f3 已结算；open 注 f4 不算）< 200
    assert face["independent_exposures_n"] == 3
    assert face["sample_sufficient"] is False

    # ROI 双栏出 API 面（等注口径既有；按场口径新列）
    auto_face = by_strategy["auto-v1"]
    assert auto_face["realized_roi"] == pytest.approx(0.0)
    assert auto_face["realized_roi_per_match"] == pytest.approx(0.0)  # (+100%−100%)/2
    assert manual["realized_roi_per_match"] == pytest.approx(1499.0)  # 单场单注=等注

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


# ---- 票 13（edge-audit-20261109）：账户面三修反例 ----


def test_after_tax_aggregates_same_period_same_game(
    db: sqlite3.Connection,
) -> None:
    """票 13 反例①（2024 年第 12 号公告）：同人+同期+同游戏归集计税。

    同期（比赛编号业务日同为 2026-11-08）同游戏两笔 ¥6000 奖金：逐注
    口径各 ≤1 万免税（税 0，旧实现即如此漏税），归集后 12000 → 超限
    全额 20% = 税 2400；净到账 9600。
    """
    b1 = _bet(db, stake=2.0, strategy=None, fid=_fixture(db, "丑", 11))
    b2 = _bet(db, stake=2.0, strategy=None, fid=_fixture(db, "寅", 12))
    for bet in (b1, b2):
        _settle(
            db,
            bet,
            status=BetStatus.WON,
            profit=5998.0,
            payout=6000.0,
            settled_at="2026-11-09T10:00:00+00:00",
        )
    face = paper_account_face(db)
    assert face.tax_total == pytest.approx(2400.0)
    # 税后已实现 = 净到账 9600 − 总注 4
    assert face.profit_after_tax == pytest.approx(9596.0)


def test_after_tax_keeps_periods_isolated(db: sqlite3.Connection) -> None:
    """跨期不合并（反例①对照组）：不同比赛编号业务日各 ¥6000 → 各自免税。"""
    b1 = _bet(
        db,
        stake=2.0,
        strategy=None,
        fid=_fixture(db, "卯", 13, business_date="2026-11-07"),
    )
    b2 = _bet(
        db,
        stake=2.0,
        strategy=None,
        fid=_fixture(db, "辰", 14, business_date="2026-11-08"),
    )
    for bet in (b1, b2):
        _settle(
            db,
            bet,
            status=BetStatus.WON,
            profit=5998.0,
            payout=6000.0,
            settled_at="2026-11-09T10:00:00+00:00",
        )
    face = paper_account_face(db)
    assert face.tax_total == pytest.approx(0.0)
    assert face.profit_after_tax == pytest.approx(11996.0)


def _bulk_single(
    db: sqlite3.Connection, fid: int, *, status: str = "open", settled: bool = False
) -> None:
    """直插一条单腿 paper 注（读面测试的批量种子，绕过建注幂等键）。"""
    db.execute(
        "INSERT INTO bets (mode, market_kind, stake, created_at, status,"
        " payout, profit, settled_at)"
        " VALUES ('paper', 'fixed', 2.0, '2026-11-08T10:00:00+00:00', ?,"
        " ?, ?, ?)",
        (
            status,
            4.0 if settled else None,
            2.0 if settled else None,
            "2026-11-09T10:00:00+00:00" if settled else None,
        ),
    )
    bet_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    db.execute(
        "INSERT INTO bet_legs (bet_id, fixture_id, market_code, selection_code,"
        " locked_odds) VALUES (?, ?, 'ttg', '2', 2.0)",
        (bet_id, fid),
    )
    db.commit()


def test_sample_sufficient_excludes_partial_correction_rows(
    db: sqlite3.Connection,
) -> None:
    """correctness F1 回归：partial（赛果更正回待开）非最终结算，不入暴露分母。

    2 腿 partial 串关覆盖 2 场 + 1 条 won 单关 1 场 → 分母=1（partial 两场剔除）。
    """
    fid_a = _fixture(db, "辰", 20)
    fid_b = _fixture(db, "辰", 21)
    fid_c = _fixture(db, "辰", 22)
    _bulk_single(db, fid_c, status="won", settled=True)
    db.execute(
        "INSERT INTO bets (mode, market_kind, stake, created_at, status)"
        " VALUES ('paper', 'fixed', 2.0, '2026-11-08T10:00:00+00:00', 'partial')"
    )
    partial_id = int(db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    db.executemany(
        "INSERT INTO bet_legs (bet_id, fixture_id, market_code, selection_code,"
        " locked_odds) VALUES (?, ?, 'ttg', '2', 2.0)",
        [(partial_id, fid_a), (partial_id, fid_b)],
    )
    db.commit()
    face = paper_account_face(db)
    assert face.independent_exposures_n == 1
    assert face.sample_sufficient is False


def test_sample_sufficient_200_unsettled_same_fixture(
    db: sqlite3.Connection,
) -> None:
    """票 13 反例②：200 条未结/全部同场记录 → sample_sufficient=false。

    分母=已结算、合格（非 void 退款）、按场独立的暴露数——未结注与
    同场重复锚不虚增样本充足性（旧行为 total_bets_n>=200 即 true）。
    """
    fid = _fixture(db, "巳", 15)
    for _ in range(200):
        _bulk_single(db, fid)
    face = paper_account_face(db)
    assert face.total_bets_n == 200
    assert face.independent_exposures_n == 0
    assert face.sample_sufficient is False


def test_sample_sufficient_counts_distinct_settled_fixtures(
    db: sqlite3.Connection,
) -> None:
    """分母按场独立：同场 200 已结算 → 1 场仍 false；跨 200 场各 1 注 → true。"""
    fid = _fixture(db, "午", 16)
    for _ in range(200):
        _bulk_single(db, fid, status="won", settled=True)
    same = paper_account_face(db)
    assert same.independent_exposures_n == 1
    assert same.sample_sufficient is False

    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    for i in range(200):
        home = fx_store.upsert_team(db, f"主队散{i}")
        away = fx_store.upsert_team(db, f"客队散{i}")
        spread_fid = fx_store.upsert_fixture(
            db, competition, f"2026-11-08T1{i % 24:02d}:00:00+00:00", home, away
        )
        _bulk_single(db, spread_fid, status="won", settled=True)
    spread = paper_account_face(db)
    assert spread.independent_exposures_n == 201  # 1 同场 + 200 跨场
    assert spread.sample_sufficient is True


def test_realized_roi_dual_columns(db: sqlite3.Connection) -> None:
    """票 13：ROI 双栏——等注口径与按场口径并列（资金规则一致性）。

    外部审计数值例：场 1 两注 +1/−1、场 2 一注 +1（各 stake ¥1）——
    等注（stake 加权）ROI=1/3≈0.3333；按场口径先算各场 ROI 再跨场
    平均=(0+1)/2=0.5。
    """
    f1 = _fixture(db, "申", 21)
    f2 = _fixture(db, "酉", 22)
    bets = [
        _bet(db, stake=1.0, strategy=None, fid=f1, req_tag="a"),
        _bet(db, stake=1.0, strategy=None, fid=f1, req_tag="b"),
        _bet(db, stake=1.0, strategy=None, fid=f2),
    ]
    outcomes = (
        (BetStatus.WON, 1.0, 2.0),
        (BetStatus.LOST, -1.0, 0.0),
        (BetStatus.WON, 1.0, 2.0),
    )
    for bet, (status, profit, payout) in zip(bets, outcomes, strict=True):
        _settle(
            db,
            bet,
            status=status,
            profit=profit,
            payout=payout,
            settled_at="2026-11-09T10:00:00+00:00",
        )
    face = paper_account_face(db)
    manual = next(f for f in face.strategies if f.strategy == "manual")
    assert manual.realized_roi == pytest.approx(0.3333)  # round 4dp（读面惯例）
    assert manual.realized_roi_per_match == pytest.approx(0.5)
