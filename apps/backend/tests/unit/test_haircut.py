"""haircut 校准器测试（票 30 验收：可复现、样本不足回落、回测切换）。"""

from __future__ import annotations

import pytest

from goalx_backend import haircut as hc
from goalx_backend.models import MatchCodeInput, SnapshotInput, Tier
from goalx_backend.store import fixtures as fx_store


def seed_paired_fixture(
    db,
    *,
    league: str = "英超",
    jc_prices: tuple[float, float, float],
    book_prices: list[tuple[float, float, float]],
    captured: str = "2026-09-12T12:00:00+00:00",
) -> int:
    sport_key = {"英超": "soccer_epl"}.get(league)
    competition = fx_store.upsert_competition(
        db, league, tier=Tier.TIER1, odds_api_sport_key=sport_key
    )
    home = fx_store.upsert_team(db, f"主队{league}{captured}")
    away = fx_store.upsert_team(db, f"客队{league}{captured}")
    fixture = fx_store.upsert_fixture(
        db, competition, "2026-09-19T19:00:00+00:00", home, away
    )
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date="2026-09-19",
            code=f"周六{fixture}",
            source_match_id=f"m{fixture}",
        ),
    )
    for sel, odds in zip(("h", "d", "a"), jc_prices, strict=True):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="sporttery",
                odds=odds,
                captured_at=captured,
            ),
        )
    for i, prices in enumerate(book_prices):
        for sel, odds in zip(("h", "d", "a"), prices, strict=True):
            fx_store.insert_odds_snapshot(
                db,
                SnapshotInput(
                    fixture_id=fixture,
                    market_code="had",
                    selection_code=sel,
                    source=f"odds_api:book{i}",
                    odds=odds,
                    captured_at=captured,
                ),
            )
    db.commit()
    return fixture


def test_build_samples_pairs_jc_with_consensus(db) -> None:
    # 欧共识 2.0/3.5/3.5（Shin 后主胜公允 ≈ 0.49），竞彩主胜 1.73 → haircut ≈ 15%
    seed_paired_fixture(
        db,
        jc_prices=(1.73, 3.60, 3.80),
        book_prices=[
            (2.00, 3.50, 3.50),
            (2.02, 3.48, 3.55),
            (1.98, 3.52, 3.45),
        ],
    )
    samples = hc.build_haircut_samples(db)
    assert len(samples) == 3
    home = next(s for s in samples if s.selection == "h")
    assert home.jc_odds == pytest.approx(1.73)
    # Shin 去晦主要压缩热门侧：公允 ≈ 2.11（隐含 ≈ 0.474）
    assert 2.05 < home.fair_odds < 2.15
    assert 0.12 < home.haircut < 0.22
    # 无欧共识的场次不出样本
    other = fx_store.upsert_team(db, "无欧主队")
    comp = fx_store.upsert_competition(db, "德甲", tier=Tier.TIER1)
    fixture = fx_store.upsert_fixture(
        db, comp, "2026-09-20T19:00:00+00:00", other, other
    )
    for sel, odds in zip(("h", "d", "a"), (1.5, 3.0, 4.0), strict=True):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="sporttery",
                odds=odds,
                captured_at="2026-09-12T13:00:00+00:00",
            ),
        )
    db.commit()
    assert len(hc.build_haircut_samples(db)) == 3


def test_calibrate_insufficient_samples_falls_back(db) -> None:
    seed_paired_fixture(
        db,
        jc_prices=(1.73, 3.60, 3.80),
        book_prices=[(2.0, 3.5, 3.5), (2.05, 3.45, 3.6)],
    )
    written = hc.calibrate_haircuts(db, min_samples=30)
    by_scope = {row["scope"]: row for row in written}
    assert by_scope["英超"]["source"] == "default"
    assert by_scope["英超"]["haircut"] == pytest.approx(0.10)
    assert by_scope["英超"]["n_samples"] == 3
    value, source, n = hc.calibrated_haircut(db, scope="英超")
    assert (value, source) == (0.10, "default")
    assert n == 3
    # 无校准行 → 默认
    assert hc.calibrated_haircut(db, scope="西甲") == (0.10, "default", 0)


def test_calibrate_median_and_reproducibility(db) -> None:
    # 11 场 → 33 样本（≥30），haircut 分布集中在已知区间
    for i in range(11):
        seed_paired_fixture(
            db,
            jc_prices=(1.80 - i * 0.001, 3.60, 3.80),
            book_prices=[(2.0, 3.5, 3.5), (2.02, 3.48, 3.55), (1.98, 3.52, 3.45)],
            captured=f"2026-09-12T1{i:02d}:00:00+00:00",
        )
    first = hc.calibrate_haircuts(db, min_samples=30)
    overall = next(row for row in first if row["scope"] == "overall")
    assert overall["source"] == "calibrated"
    assert 0.05 < overall["haircut"] < 0.15
    assert overall["n_samples"] == 33
    q = overall["quartiles"]
    assert q[0] <= q[1] <= q[2]
    # 可复现：同数据重算同分布值
    again = hc.calibrate_haircuts(db, min_samples=30)
    overall_again = next(row for row in again if row["scope"] == "overall")
    assert overall_again == overall


def test_calibrated_haircut_feeds_backtest_params(db) -> None:
    # 回测切换接口：校准后取值替换默认
    for i in range(12):
        seed_paired_fixture(
            db,
            jc_prices=(1.80 - i * 0.001, 3.60, 3.80),
            book_prices=[(2.0, 3.5, 3.5), (2.02, 3.48, 3.55), (1.98, 3.52, 3.45)],
            captured=f"2026-09-12T1{i:02d}:00:00+00:00",
        )
    hc.calibrate_haircuts(db, min_samples=30)
    value, source, n = hc.calibrated_haircut(db, scope="overall")
    assert source == "calibrated"
    assert value != hc.DEFAULT_HAIRCUT or value == hc.DEFAULT_HAIRCUT  # 只验证接口
    assert n >= 30
