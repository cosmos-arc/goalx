"""只读计税视图（measure-08：净到账口径）的行为断言。"""

from __future__ import annotations

from goalx_backend.betting import tax


def test_tax_free_below_threshold() -> None:
    """≤1 万免征：净到账=全额。"""
    assert tax.tax_on_prize(0.0) == (0.0, 0.0)
    assert tax.tax_on_prize(9_999.0) == (0.0, 9_999.0)
    assert tax.tax_on_prize(10_000.0) == (0.0, 10_000.0)  # 不超过=免


def test_tax_above_threshold_full_amount() -> None:
    """超限全额 20%（非超额部分计税）：1.2 万 → 税 2400、净 9600。"""
    assert tax.tax_on_prize(10_001.0) == (2000.2, 8000.8)
    assert tax.tax_on_prize(12_000.0) == (2400.0, 9600.0)


def test_frozen_tax_face_groups_by_business_date(db) -> None:
    """冻结面计税组：同业务日 would_bet 命中注归集（毛奖含本金）。"""
    from goalx_backend.betting import frozen as fz
    from goalx_backend.betting.funnel import record_funnel_day
    from goalx_backend.data import fixtures as fx_store
    from goalx_backend.models import (
        MatchCodeInput,
        SaleStatusInput,
        SnapshotInput,
        Tier,
    )

    def seed(n: int) -> int:
        competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
        home = fx_store.upsert_team(db, f"t{n}")
        away = fx_store.upsert_team(db, f"u{n}")
        fid = fx_store.upsert_fixture(
            db, competition, "2026-10-17T18:00:00+00:00", home, away
        )
        fx_store.upsert_match_code(
            db,
            MatchCodeInput(
                fixture_id=fid,
                kind="jingcai",
                business_date="2026-10-16",
                code=f"周六{n:03d}",
            ),
        )
        for sel, o in {"h": 3.0, "d": 3.3, "a": 3.5}.items():
            fx_store.insert_odds_snapshot(
                db,
                SnapshotInput(
                    fixture_id=fid,
                    market_code="had",
                    selection_code=sel,
                    source="sporttery",
                    odds=o,
                    captured_at="2026-10-16T14:00:00+00:00",
                    observed_at="2026-10-16T14:03:00+00:00",
                    source_updated_at="2026-10-16T14:00:00+00:00",
                ),
            )
        for sel, o in {"h": 2.0, "d": 3.4, "a": 4.0}.items():
            fx_store.insert_odds_snapshot(
                db,
                SnapshotInput(
                    fixture_id=fid,
                    market_code="had",
                    selection_code=sel,
                    source="odds_api:pinnacle",
                    odds=o,
                    captured_at="2026-10-16T14:00:00+00:00",
                    observed_at="2026-10-16T14:00:00+00:00",
                    source_updated_at="2026-10-16T14:00:00+00:00",
                ),
            )
        fx_store.append_sale_status(
            db,
            SaleStatusInput(
                fixture_id=fid,
                market_code="had",
                sale_state="on_sale",
                single_eligible=True,
                observed_at="2026-10-16T14:03:00+00:00",
            ),
        )
        db.commit()
        return fid

    for n in range(3):
        seed(n)
    record_funnel_day(db, as_of="2026-10-16T14:04:00+00:00", business_date="2026-10-16")
    fz.record_frozen_decisions(db, business_date="2026-10-16")
    # 三张 would_bet 卡（各 odds 3.0）全命中：毛奖 9 元（免征线内）
    face = tax.frozen_tax_face(db, "2026-10-16")
    assert face["would_bet_cards"] == 3
    assert face["settled_cards"] == 0  # 未结算注不进奖金面
    # 人工结算三张全 won：毛奖=3×3.0=9 → 免征、净利=9−3=6
    db.execute(
        "UPDATE frozen_decisions SET settled_outcome = 'won', settled_profit = 2.0"
        " WHERE decision = 'would_bet'"
    )
    db.commit()
    face2 = tax.frozen_tax_face(db, "2026-10-16")
    assert face2["prize_gross"] == 9.0
    assert face2["tax"] == 0.0
    assert face2["net_after_tax"] == 9.0
    assert face2["net_profit_flat"] == 6.0
    assert "假设测算" in face2["note"]
    # 汇总面带 tax_face（日终报告并列税前/税后）
    summary = fz.frozen_summary(db, "2026-10-16")
    assert summary["tax_face"]["prize_gross"] == 9.0
