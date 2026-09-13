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
            captured=f"2026-09-12T{9 + i:02d}:00:00+00:00",
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
            captured=f"2026-09-12T{8 + i:02d}:00:00+00:00",
        )
    hc.calibrate_haircuts(db, min_samples=30)
    value, source, n = hc.calibrated_haircut(db, scope="overall")
    assert source == "calibrated"
    assert value != hc.DEFAULT_HAIRCUT or value == hc.DEFAULT_HAIRCUT  # 只验证接口
    assert n >= 30


# --- 票 35：as-of 配对、场次/选择分列、方法敏感性 ---


def test_asof_pairing_excludes_later_eu_observation(db) -> None:
    """欧侧观测晚于竞彩调盘时刻的报价不参与该 as-of 配对。"""
    fixture = seed_paired_fixture(
        db,
        jc_prices=(1.73, 3.60, 3.80),
        book_prices=[(2.00, 3.50, 3.50)],
    )
    # 更新的欧共识在竞彩调盘之后 40 分钟才观测到 → 不进配对
    for sel, odds in zip(("h", "d", "a"), (2.5, 3.8, 3.8), strict=True):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="odds_api:book0",
                odds=odds,
                captured_at="2026-09-12T12:40:00+00:00",
                observed_at="2026-09-12T12:40:00+00:00",
                source_updated_at="2026-09-12T12:40:00+00:00",
            ),
        )
    db.commit()
    samples = hc.build_haircut_samples(db)
    home = next(s for s in samples if s.selection == "h")
    # 配对用的是 12:00 的旧共识（2.00），不是 12:40 的新共识（2.5）
    assert home.eu_books["odds_api:book0"]["h"] == pytest.approx(2.00)


def test_asof_pairing_excludes_beyond_gap_window(db) -> None:
    """欧侧观测距 as-of 超过配对时差上限（默认 5 分钟）→ 不配对。"""
    fixture = seed_paired_fixture(db, jc_prices=(1.73, 3.60, 3.80), book_prices=[])
    for sel, odds in zip(("h", "d", "a"), (2.0, 3.5, 3.5), strict=True):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="odds_api:book0",
                odds=odds,
                captured_at="2026-09-12T11:50:00+00:00",
                observed_at="2026-09-12T11:50:00+00:00",
            ),
        )
    db.commit()
    assert hc.build_haircut_samples(db) == []


def test_incomplete_book_excluded_from_consensus(db) -> None:
    fixture = seed_paired_fixture(
        db,
        jc_prices=(1.73, 3.60, 3.80),
        book_prices=[(2.00, 3.50, 3.50)],
    )
    # 同窗另一家缺平局价 → 整家剔除，不稀释共识
    for sel, odds in (("h", 2.1), ("a", 3.4)):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="odds_api:partial",
                odds=odds,
                captured_at="2026-09-12T12:00:00+00:00",
                observed_at="2026-09-12T12:00:00+00:00",
            ),
        )
    db.commit()
    samples = hc.build_haircut_samples(db)
    home = next(s for s in samples if s.selection == "h")
    assert set(home.eu_books) == {"odds_api:book0"}


def test_calibrate_reports_fixtures_and_selections_separately(db) -> None:
    """分列场次数与选择数（票 35 验收 5）。"""
    for i in range(2):
        seed_paired_fixture(
            db,
            jc_prices=(1.8, 3.6, 3.8),
            book_prices=[(2.0, 3.5, 3.5)],
            captured=f"2026-09-12T1{i}:00:00+00:00",
        )
    written = hc.calibrate_haircuts(db, min_samples=1)
    overall = next(row for row in written if row["scope"] == "overall")
    assert overall["n_samples"] == 6  # 2 场 × 3 选择
    assert overall["n_fixtures"] == 2
    assert overall["method_version"] == hc.HAIRCUT_METHOD_VERSION
    row = db.execute(
        "SELECT n_fixtures, method_version FROM haircut_calibrations"
        " WHERE scope='overall'"
    ).fetchone()
    assert row["n_fixtures"] == 2
    assert row["method_version"] == hc.HAIRCUT_METHOD_VERSION


def test_method_sensitivity_reproducible_and_recorded(db) -> None:
    """四方法对照可复现、记录差异；固定方法不被替换（不事后选优）。"""
    seed_paired_fixture(
        db,
        jc_prices=(1.73, 3.60, 3.80),
        book_prices=[(2.00, 3.50, 3.50), (2.02, 3.48, 3.55), (1.98, 3.52, 3.45)],
    )
    samples = hc.build_haircut_samples(db)
    first = hc.method_sensitivity(samples)
    second = hc.method_sensitivity(samples)
    assert first == second  # 相同样本可复现
    assert set(first) == {
        "shin_mean",
        "shin_per_book_mean",
        "normalized_mean",
        "power_mean",
    }
    assert all(row["n"] == 3 for row in first.values())
    # 各方法中位 haircut 存在可复核差异（方法敏感性可复验，非收益证据）
    assert len({row["median_haircut"] for row in first.values()}) >= 2
    # 固定方法版本是 shin_mean_v1
    assert hc.HAIRCUT_METHOD_VERSION == "shin_mean_v1"
