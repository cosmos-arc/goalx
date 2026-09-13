"""CLV 跟踪测试（票 32 验收：CLV_proxy 计算、beat rate 报表、回归可复现）。"""

from __future__ import annotations

import pytest

from goalx_backend import clv
from goalx_backend.models import BetMode, LegInput, SnapshotInput, Tier
from goalx_backend.services import BetDraft, create_bet_with_legs
from goalx_backend.store import fixtures as fx_store

KICKOFF = "2026-09-12T19:00:00+00:00"


def seed_fixture(db, *, league: str = "英超") -> int:
    sport_key = {"英超": "soccer_epl"}.get(league)
    competition = fx_store.upsert_competition(
        db, league, tier=Tier.TIER1, odds_api_sport_key=sport_key
    )
    home = fx_store.upsert_team(db, f"主队{league}")
    away = fx_store.upsert_team(db, f"客队{league}")
    return fx_store.upsert_fixture(db, competition, KICKOFF, home, away)


def seed_closing_snapshots(
    db,
    fixture_id: int,
    prices: tuple[float, float, float],
    *,
    captured: str = "2026-09-12T18:45:00+00:00",
) -> None:
    from goalx_backend.models import SnapshotPurpose

    for book in ("pin", "avg"):
        for sel, odds in zip(("h", "d", "a"), prices, strict=True):
            fx_store.insert_odds_snapshot(
                db,
                SnapshotInput(
                    fixture_id=fixture_id,
                    market_code="had",
                    selection_code=sel,
                    source=f"odds_api:{book}",
                    odds=odds,
                    captured_at=captured,
                    purpose=SnapshotPurpose.CLOSING,
                ),
            )
    db.commit()


def settle_bet(db, bet_id: int, *, status: str = "won") -> None:
    db.execute(
        "UPDATE bets SET purchased = 1, status = ?,"
        " settled_at = '2026-09-12T21:00:00+00:00',"
        " profit = 10.0 WHERE id = ?",
        (status, bet_id),
    )
    db.commit()


def test_closing_prob_requires_closing_purpose(db) -> None:
    fixture = seed_fixture(db)
    # live_capture 快照不算收盘
    for sel, odds in zip(("h", "d", "a"), (2.0, 3.5, 3.5), strict=True):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="odds_api:pin",
                odds=odds,
                captured_at="2026-09-12T18:00:00+00:00",
            ),
        )
    db.commit()
    assert clv._closing_prob(db, fixture, "h") is None
    seed_closing_snapshots(db, fixture, (2.0, 3.5, 3.5))
    close = clv._closing_prob(db, fixture, "h")
    assert close is not None
    prob, source = close
    assert source == "odds_api_closing"
    assert 0.45 < prob < 0.55


def test_closing_snapshot_window_guard(db) -> None:
    fixture = seed_fixture(db)
    # 远早于开球的 closing 快照（昨日）不算
    seed_closing_snapshots(
        db, fixture, (2.0, 3.5, 3.5), captured="2026-09-11T18:45:00+00:00"
    )
    assert clv._closing_prob(db, fixture, "h") is None


def test_reconcile_clv_and_report(db) -> None:
    fixture = seed_fixture(db)
    seed_closing_snapshots(db, fixture, (2.0, 3.5, 3.5))  # 主胜公允 ≈ 0.474
    bet_id = create_bet_with_legs(
        db,
        BetDraft(
            mode=BetMode.PAPER,
            stake=50.0,
            legs=[
                LegInput(
                    fixture_id=fixture,
                    market_code="had",
                    selection_code="h",
                    locked_odds=2.20,  # 买入隐含 0.4545 < 收盘 0.474 → beat
                )
            ],
        ),
    )
    db.execute(
        "UPDATE bets SET placed_at = '2026-09-12T18:50:00+00:00' WHERE id = ?",
        (bet_id,),
    )
    settle_bet(db, bet_id, status="won")
    stats = clv.reconcile_clv(db)
    assert stats.recorded == 1
    row = db.execute("SELECT * FROM clv_records WHERE bet_id = ?", (bet_id,)).fetchone()
    assert row is not None
    assert row["clv_prob"] == pytest.approx(0.474 - 1 / 2.20, abs=1e-3)
    assert row["minutes_to_kickoff"] == pytest.approx(10.0)
    # 幂等
    assert clv.reconcile_clv(db).recorded == 0

    report = clv.clv_report(db)
    assert report["n_records"] == 1
    assert report["beat_rate_overall"] == 1.0
    assert report["by_minutes_bucket"]["[10,30)min"]["n"] == 1
    assert report["by_market_league"]["had:英超"]["beat_rate"] == 1.0
    # 回归：单样本 → None
    assert report["regression"]["slope"] is None


def test_reconcile_skips_open_bets_and_unmapped(db) -> None:
    # 未结算注不对账
    fixture_with_close = seed_fixture(db)
    seed_closing_snapshots(db, fixture_with_close, (2.0, 3.5, 3.5))
    open_bet = create_bet_with_legs(
        db,
        BetDraft(
            mode=BetMode.PAPER,
            stake=50.0,
            legs=[
                LegInput(
                    fixture_id=fixture_with_close,
                    market_code="had",
                    selection_code="h",
                    locked_odds=2.2,
                )
            ],
        ),
    )
    assert clv.reconcile_clv(db).recorded == 0
    # 已结算但该场无 closing 快照（append-only 不可删，另建一场）→ skipped
    fixture_no_close = seed_fixture(db, league="西甲")
    lost_bet = create_bet_with_legs(
        db,
        BetDraft(
            mode=BetMode.PAPER,
            stake=50.0,
            legs=[
                LegInput(
                    fixture_id=fixture_no_close,
                    market_code="had",
                    selection_code="h",
                    locked_odds=2.2,
                )
            ],
        ),
    )
    settle_bet(db, open_bet, status="won")
    settle_bet(db, lost_bet, status="lost")
    stats = clv.reconcile_clv(db)
    assert stats.recorded == 1  # 只有有收盘的一场
    assert any("no_close" in item for item in stats.skipped)


def test_regression_slope_positive_when_clv_predicts_profit(db) -> None:
    xs = [0.05 * i - 0.1 for i in range(20)]
    ys = [3.0 * x + (i % 3 - 1) * 0.1 for i, x in enumerate(xs)]
    slope, r2 = clv._ols_slope(xs, ys)
    assert slope == pytest.approx(3.0, rel=5e-2)  # 噪声与 i 相关，非正交设计
    assert r2 is not None
    assert r2 > 0.95
    assert clv._ols_slope([1.0], [1.0]) == (None, None)


def test_unpurchased_counterfactual_is_excluded_even_with_legacy_clv(db) -> None:
    fixture = seed_fixture(db)
    seed_closing_snapshots(db, fixture, (2.0, 3.5, 3.5))
    bet = create_bet_with_legs(
        db,
        BetDraft(
            mode=BetMode.PAPER,
            stake=10,
            legs=[
                LegInput(
                    fixture_id=fixture,
                    market_code="had",
                    selection_code="h",
                    locked_odds=2,
                )
            ],
        ),
    )
    settle_bet(db, bet)
    assert clv.reconcile_clv(db).recorded == 1
    db.execute("UPDATE bets SET purchased=0 WHERE id=?", (bet,))
    assert clv.reconcile_clv(db).recorded == 0
    assert clv.clv_report(db)["n_records"] == 0
