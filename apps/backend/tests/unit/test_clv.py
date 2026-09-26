"""CLV 跟踪测试（票 32 验收：CLV_proxy 计算、beat rate 报表、回归可复现；
票 40：基准分层降级矩阵与 close_basis 标注）。"""

from __future__ import annotations

import pytest

from goalx_backend import odds_math as om
from goalx_backend.betting.bets import BetDraft, create_bet_with_legs
from goalx_backend.data import corpus_duckdb
from goalx_backend.data import fixtures as fx_store
from goalx_backend.evaluation import clv
from goalx_backend.models import BetMode, LegInput, SnapshotInput, Tier

KICKOFF = "2026-09-12T19:00:00+00:00"


def seed_fixture(db, *, league: str = "英超") -> int:
    sport_key = {"英超": "soccer_epl"}.get(league)
    competition = fx_store.upsert_competition(
        db, league, tier=Tier.TIER1, odds_api_sport_key=sport_key
    )
    home = fx_store.upsert_team(db, f"主队{league}")
    away = fx_store.upsert_team(db, f"客队{league}")
    return fx_store.upsert_fixture(db, competition, KICKOFF, home, away)


def seed_closing_books(
    db,
    fixture_id: int,
    books: dict[str, tuple[float, float, float]],
    *,
    captured: str = "2026-09-12T18:45:00+00:00",
) -> None:
    """closing 快照按 book 显式播种（票 40 降级矩阵用）。"""
    from goalx_backend.models import SnapshotPurpose

    for book, prices in books.items():
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


def seed_closing_snapshots(
    db,
    fixture_id: int,
    prices: tuple[float, float, float],
    *,
    captured: str = "2026-09-12T18:45:00+00:00",
) -> None:
    seed_closing_books(
        db, fixture_id, {"pin": prices, "avg": prices}, captured=captured
    )


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
    prob, source, basis = close
    assert source == "odds_api_closing"
    assert basis == "consensus"  # pin/avg 非锚书 → 共识口径（票 40）
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
    assert report["denominator"]["reconciled_bets"] == 1
    assert report["denominator"]["unique_bets"] == 1
    assert report["denominator"]["legs"] == 1  # 腿数不冒充分母（票 34）
    assert report["singles"]["paper"]["beat_rate"] == 1.0
    assert report["singles"]["live"]["n_bets"] == 0
    assert report["by_minutes_bucket_single"]["[10,30)min"]["n"] == 1
    # 回归：单样本 → None（只用单关）
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
    assert any("no_pre_kickoff_close" in item for item in stats.skipped)


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
    assert clv.clv_report(db)["denominator"]["unique_bets"] == 0


# --- 票 34：票级口径 / 分组分母 / 迟到 closing ---


def seed_parlay(
    db,
    *,
    mode: BetMode = BetMode.PAPER,
    stake: float = 50.0,
    placed_at: str | None = "2026-09-12T18:50:00+00:00",
) -> int:
    """两腿 2串1：英超主胜 @2.2 + 西甲客胜 @3.0。"""
    f1 = seed_fixture(db, league="英超")
    f2 = seed_fixture(db, league="西甲")
    return (
        create_bet_with_legs(
            db,
            BetDraft(
                mode=mode,
                stake=stake,
                legs=[
                    LegInput(
                        fixture_id=f1,
                        market_code="had",
                        selection_code="h",
                        locked_odds=2.2,
                    ),
                    LegInput(
                        fixture_id=f2,
                        market_code="had",
                        selection_code="a",
                        locked_odds=3.0,
                    ),
                ],
            ),
        ),
        f1,
        f2,
    )


def test_parlay_ticket_level_clv_with_independence_flag(db) -> None:
    bet, f1, f2 = seed_parlay(db)
    # 两场 closing 共识:主胜≈0.474、客胜≈0.30
    seed_closing_snapshots(db, f1, (2.0, 3.5, 3.5))
    seed_closing_snapshots(db, f2, (4.5, 3.5, 3.0))
    db.execute(
        "UPDATE bets SET placed_at = ? WHERE id = ?", ("2026-09-12T18:50:00+00:00", bet)
    )
    settle_bet(db, bet, status="won")
    assert clv.reconcile_clv(db).recorded == 2  # 逐腿记录
    report = clv.clv_report(db)
    assert report["singles"]["paper"]["n_bets"] == 0
    group = report["parlay2"]["paper"]
    assert group["n_bets"] == 1  # 2 腿 → 1 票,不冒充 2 注
    # 票级联合概率口径 = ∏close_prob − 1/∏locked_odds
    close_probs = [
        float(r["close_prob"])
        for r in db.execute(
            "SELECT close_prob FROM clv_records WHERE bet_id = ?", (bet,)
        ).fetchall()
    ]
    expected = close_probs[0] * close_probs[1] - 1.0 / (2.2 * 3.0)
    assert group["avg_clv"] == pytest.approx(expected, abs=1e-6)
    assert report["independence_assumed"] is True
    assert report["regression"]["n"] == 0  # 串关不进回归


def test_parlay_missing_one_leg_close_excluded(db) -> None:
    bet, f1, _f2 = seed_parlay(db)
    seed_closing_snapshots(db, f1, (2.0, 3.5, 3.5))  # 只有一场有 closing
    db.execute(
        "UPDATE bets SET placed_at = ? WHERE id = ?", ("2026-09-12T18:50:00+00:00", bet)
    )
    settle_bet(db, bet, status="lost")
    clv.reconcile_clv(db)
    report = clv.clv_report(db)
    assert report["parlay2"]["paper"]["n_bets"] == 0
    assert report["denominator"]["no_close_bets"] == 1  # 缺一腿整票排除


def test_mixed_market_leg_bet_excluded(db) -> None:
    f1 = seed_fixture(db, league="英超")
    f2 = seed_fixture(db, league="西甲")
    seed_closing_snapshots(db, f1, (2.0, 3.5, 3.5))
    bet = create_bet_with_legs(
        db,
        BetDraft(
            mode=BetMode.PAPER,
            stake=50.0,
            legs=[
                LegInput(
                    fixture_id=f1,
                    market_code="had",
                    selection_code="h",
                    locked_odds=2.2,
                ),
                LegInput(
                    fixture_id=f2,
                    market_code="ttg",
                    selection_code="3",
                    locked_odds=4.0,
                ),
            ],
        ),
    )
    settle_bet(db, bet, status="won")
    clv.reconcile_clv(db)
    report = clv.clv_report(db)
    assert report["singles"]["paper"]["n_bets"] == 0  # 混合玩法腿不能当单关
    assert report["denominator"]["unsupported_bets"] == 1


def test_decision_identity_dedup_for_denominator(db) -> None:
    """同决策重试/拆分金额只计一次验证分母（票 34 验收 1）。"""
    fixture = seed_fixture(db)
    seed_closing_snapshots(db, fixture, (2.0, 3.5, 3.5))
    ids = []
    for stake in (30.0, 20.0):  # 拆分金额 → 同一决策
        bet = create_bet_with_legs(
            db,
            BetDraft(
                mode=BetMode.PAPER,
                stake=stake,
                legs=[
                    LegInput(
                        fixture_id=fixture,
                        market_code="had",
                        selection_code="h",
                        locked_odds=2.2,
                    )
                ],
            ),
        )
        db.execute(
            "UPDATE bets SET placed_at = ? WHERE id = ?",
            ("2026-09-12T18:50:00+00:00", bet),
        )
        settle_bet(db, bet, status="won")
        ids.append(bet)
    clv.reconcile_clv(db)
    report = clv.clv_report(db)
    assert report["denominator"]["reconciled_bets"] == 2  # 账务如实保留
    assert report["denominator"]["unique_bets"] == 1  # 验证分母去重
    assert report["denominator"]["deduped_duplicates"] == 1
    assert report["singles"]["paper"]["n_bets"] == 1


def test_late_closing_observation_excluded(db) -> None:
    """closing 快照观测时间晚于 kickoff → 迟到,不算前瞻收盘（票 34）。"""
    fixture = seed_fixture(db)
    from goalx_backend.models import SnapshotPurpose

    for book in ("pin",):
        for sel, odds in zip(("h", "d", "a"), (2.0, 3.5, 3.5), strict=True):
            fx_store.insert_odds_snapshot(
                db,
                SnapshotInput(
                    fixture_id=fixture,
                    market_code="had",
                    selection_code=sel,
                    source=f"odds_api:{book}",
                    odds=odds,
                    captured_at="2026-09-12T18:50:00+00:00",  # 开赛前(旧口径可过)
                    observed_at="2026-09-12T19:20:00+00:00",  # 实际观测在开赛后
                    purpose=SnapshotPurpose.CLOSING,
                ),
            )
    db.commit()
    assert clv._closing_prob(db, fixture, "h") is None


def test_paper_live_reported_separately(db) -> None:
    fixture = seed_fixture(db)
    seed_closing_snapshots(db, fixture, (2.0, 3.5, 3.5))
    for mode in (BetMode.PAPER, BetMode.LIVE):
        bet = create_bet_with_legs(
            db,
            BetDraft(
                mode=mode,
                stake=50.0,
                legs=[
                    LegInput(
                        fixture_id=fixture,
                        market_code="had",
                        selection_code="h",
                        locked_odds=2.2,
                    )
                ],
            ),
        )
        db.execute(
            "UPDATE bets SET placed_at = ? WHERE id = ?",
            ("2026-09-12T18:50:00+00:00", bet),
        )
        settle_bet(db, bet, status="won")
    clv.reconcile_clv(db)
    report = clv.clv_report(db)
    assert report["singles"]["paper"]["n_bets"] == 1
    assert report["singles"]["live"]["n_bets"] == 1  # 分开报告,不混


# --- 票 40：基准分层（Pinnacle 主锚 → Betfair 辅 → 共识 fallback） ---


def test_anchor_pinnacle_primary_beats_exchange_and_consensus(db) -> None:
    """主锚在场即用 Pinnacle 单书 Shin，不与辅锚/其他书混均价。"""
    fixture = seed_fixture(db)
    seed_closing_books(
        db,
        fixture,
        {
            "pinnacle": (2.10, 3.40, 3.60),  # sharp 主锚
            "betfair_ex_eu": (2.20, 3.50, 3.50),
            "bet365": (1.90, 3.60, 4.20),
        },
    )
    close = clv._closing_prob(db, fixture, "h")
    assert close is not None
    prob, _, basis = close
    assert basis == "pinnacle"
    expected = om.shin_implied((2.10, 3.40, 3.60))
    assert prob == pytest.approx(expected[0], abs=1e-9)


def test_anchor_betfair_exchange_commission_adjusted(db) -> None:
    """无 Pinnacle 有交易所 → 辅锚：back 价按佣金调有效赔率后归一化。"""
    fixture = seed_fixture(db)
    back = (2.20, 3.50, 3.50)
    seed_closing_books(db, fixture, {"betfair_ex_eu": back})
    close = clv._closing_prob(db, fixture, "h")
    assert close is not None
    prob, _, basis = close
    assert basis == "betfair_ex"
    expected = om.normalized_implied(
        tuple(1.0 + (o - 1.0) * (1.0 - clv.BETFAIR_COMMISSION) for o in back)
    )
    assert prob == pytest.approx(expected[0], abs=1e-9)
    # 佣金率参数化（调研区间 2–5%）：5% 覆写改变概率
    close5 = clv._closing_prob(db, fixture, "h", betfair_commission=0.05)
    assert close5 is not None
    prob5, _, _ = close5
    expected5 = om.normalized_implied(tuple(1.0 + (o - 1.0) * 0.95 for o in back))
    assert prob5 == pytest.approx(expected5[0], abs=1e-9)
    assert prob5 != pytest.approx(prob)
    # UK 区域交易所键同认（实采是 EU 区变体）
    fixture_uk = seed_fixture(db, league="西甲")
    seed_closing_books(db, fixture_uk, {"betfair_ex_uk": back})
    close_uk = clv._closing_prob(db, fixture_uk, "h")
    assert close_uk is not None
    assert close_uk[2] == "betfair_ex"


def test_anchor_consensus_fallback_without_sharp_books(db) -> None:
    """无主辅锚 → 共识 fallback；高 margin 书（onexbet）只进共识不作锚。"""
    fixture = seed_fixture(db)
    books = {"onexbet": (1.85, 3.80, 4.40), "bet365": (1.95, 3.60, 4.00)}
    seed_closing_books(db, fixture, books)
    close = clv._closing_prob(db, fixture, "a")
    assert close is not None
    prob, _, basis = close
    assert basis == "consensus"
    consensus = tuple(
        sum(prices[i] for prices in books.values()) / len(books) for i in range(3)
    )
    expected = om.shin_implied(consensus)
    assert prob == pytest.approx(expected[2], abs=1e-9)


def test_anchor_tier_order_betfair_before_consensus(db) -> None:
    """辅锚优先于共识：Betfair 在场（即使还有别的书）仍取 Betfair。"""
    fixture = seed_fixture(db)
    seed_closing_books(
        db,
        fixture,
        {"betfair_ex_eu": (2.20, 3.50, 3.50), "onexbet": (1.85, 3.80, 4.40)},
    )
    close = clv._closing_prob(db, fixture, "h")
    assert close is not None
    assert close[2] == "betfair_ex"


def test_anchor_none_when_no_closing_books(db) -> None:
    """两级都缺且无任何 closing 书 → 无基准（保留排除路径）。"""
    fixture = seed_fixture(db)
    assert clv._closing_prob(db, fixture, "h") is None


def test_reconcile_writes_close_basis_per_tier(db) -> None:
    """对账落库带 close_basis：Pinnacle 场记 pinnacle，共识场记 consensus。"""
    f_pin = seed_fixture(db, league="英超")
    f_cons = seed_fixture(db, league="西甲")
    seed_closing_books(db, f_pin, {"pinnacle": (2.0, 3.5, 3.5)})
    seed_closing_snapshots(db, f_cons, (4.5, 3.5, 3.0))  # pin/avg → 共识
    for fixture in (f_pin, f_cons):
        bet = create_bet_with_legs(
            db,
            BetDraft(
                mode=BetMode.PAPER,
                stake=50.0,
                legs=[
                    LegInput(
                        fixture_id=fixture,
                        market_code="had",
                        selection_code="h",
                        locked_odds=2.2,
                    )
                ],
            ),
        )
        db.execute(
            "UPDATE bets SET placed_at = ? WHERE id = ?",
            ("2026-09-12T18:50:00+00:00", bet),
        )
        settle_bet(db, bet, status="won")
    clv.reconcile_clv(db)
    bases = {
        int(r["fixture_id"]): r["close_basis"]
        for r in db.execute(
            "SELECT fixture_id, close_basis FROM clv_records"
        ).fetchall()
    }
    assert bases == {f_pin: "pinnacle", f_cons: "consensus"}


def test_report_by_close_basis_parallel_presentation(db) -> None:
    """报表 by_close_basis：分层行按级分列，历史 NULL 行记 legacy 不重算。"""
    f_new = seed_fixture(db, league="英超")
    f_old = seed_fixture(db, league="西甲")
    seed_closing_books(db, f_new, {"pinnacle": (2.0, 3.5, 3.5)})
    seed_closing_snapshots(db, f_old, (2.0, 3.5, 3.5))
    for fixture in (f_new, f_old):
        bet = create_bet_with_legs(
            db,
            BetDraft(
                mode=BetMode.PAPER,
                stake=50.0,
                legs=[
                    LegInput(
                        fixture_id=fixture,
                        market_code="had",
                        selection_code="h",
                        locked_odds=2.2,  # 隐含 0.4545 < 收盘 → beat
                    )
                ],
            ),
        )
        db.execute(
            "UPDATE bets SET placed_at = ? WHERE id = ?",
            ("2026-09-12T18:50:00+00:00", bet),
        )
        settle_bet(db, bet, status="won")
    clv.reconcile_clv(db)
    # 模拟分层前历史行：置 NULL（历史行不重算的呈现路径）
    db.execute(
        "UPDATE clv_records SET close_basis = NULL WHERE fixture_id = ?", (f_old,)
    )
    db.commit()

    report = clv.clv_report(db)
    assert report["close_basis_note"].startswith("pinnacle 主锚")
    by_basis = report["by_close_basis"]
    assert set(by_basis) == {"pinnacle", "legacy"}
    assert by_basis["pinnacle"]["legs"] == 1
    assert by_basis["pinnacle"]["bets"] == 1
    assert by_basis["pinnacle"]["groups"]["single"]["paper"]["beat_rate"] == 1.0
    assert by_basis["legacy"]["legs"] == 1
    assert by_basis["legacy"]["bets"] == 1
    # 主口径不分基准：两组各 1 注（窗口期新旧并行呈现）
    assert report["singles"]["paper"]["n_bets"] == 2
    assert report["singles"]["paper"]["beat_rate"] == 1.0


def test_report_mixed_basis_parlay(db) -> None:
    """串关两腿基准不同 → 票级 mixed，只计注不进单级分组。"""
    f_pin = seed_fixture(db, league="英超")
    f_cons = seed_fixture(db, league="西甲")
    seed_closing_books(db, f_pin, {"pinnacle": (2.0, 3.5, 3.5)})
    seed_closing_snapshots(db, f_cons, (4.5, 3.5, 3.0))
    bet = create_bet_with_legs(
        db,
        BetDraft(
            mode=BetMode.PAPER,
            stake=50.0,
            legs=[
                LegInput(
                    fixture_id=f_pin,
                    market_code="had",
                    selection_code="h",
                    locked_odds=2.2,
                ),
                LegInput(
                    fixture_id=f_cons,
                    market_code="had",
                    selection_code="a",
                    locked_odds=3.0,
                ),
            ],
        ),
    )
    db.execute(
        "UPDATE bets SET placed_at = ? WHERE id = ?",
        ("2026-09-12T18:50:00+00:00", bet),
    )
    settle_bet(db, bet, status="won")
    clv.reconcile_clv(db)
    report = clv.clv_report(db)
    by_basis = report["by_close_basis"]
    assert by_basis["mixed"]["bets"] == 1
    # 腿数归各级：pinnacle 1 腿、consensus 1 腿，但单级注数不收 mixed 票
    assert by_basis["pinnacle"]["legs"] == 1
    assert by_basis["pinnacle"]["bets"] == 0
    assert by_basis["consensus"]["legs"] == 1
    assert by_basis["consensus"]["bets"] == 0
    # 主口径串关组照常计票
    assert report["parlay2"]["paper"]["n_bets"] == 1


# --- 票 75：srct 收盘锚接管（odds_api 判死后的前向主通路） ---


def seed_duck(
    *,
    home: str = "主队英超",
    away: str = "客队英超",
    sid: str = "2790001",
    kickoff_bj: str = "2026-09-12 21:00:00",
    events: tuple[tuple[str, float, float, float], ...] = (
        ("2026-09-12 05:00:00+08:00", 2.0, 3.5, 3.5),
    ),
):
    """内存 duckdb 假 corpus：fixture_universe + odds_change_event 最小面。"""
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE fixture_universe"
        " (sid VARCHAR, league VARCHAR, kickoff TIMESTAMP, home VARCHAR,"
        " away VARCHAR)"
    )
    con.execute(
        "INSERT INTO fixture_universe VALUES (?, ?, ?::TIMESTAMP, ?, ?)",
        [sid, "英超", kickoff_bj, home, away],
    )
    con.execute(
        "CREATE TABLE odds_change_event"
        " (sid VARCHAR, bookmaker_id VARCHAR, market VARCHAR,"
        " published_at TIMESTAMPTZ, odds_home DOUBLE, odds_draw DOUBLE,"
        " odds_away DOUBLE)"
    )
    for published, h, d, a in events:
        con.execute(
            "INSERT INTO odds_change_event VALUES"
            " (?, ?, '1x2', ?::TIMESTAMPTZ, ?, ?, ?)",
            [sid, corpus_duckdb.SRCT_PINNACLE_BOOK, published, h, d, a],
        )
    return con


def test_srct_anchor_primary_key_match(db) -> None:
    """中文队名+北京日期主键命中 → srct_pinnacle 基准（Shin 概率）。"""
    duck_con = seed_duck()
    fixture = seed_fixture(db)  # kickoff 2026-09-12T19:00Z = 北京 13 日 03:00?
    # KICKOFF=19:00Z → 北京 09-13 03:00；假 corpus 按同口径重灌
    duck_con.execute("DELETE FROM fixture_universe")
    duck_con.execute(
        "INSERT INTO fixture_universe VALUES"
        " ('2790001', '英超', TIMESTAMP '2026-09-13 03:00:00', '主队英超', '客队英超')"
    )
    close = clv._closing_prob(db, fixture, "h", duck_con=duck_con)
    assert close is not None
    prob, source, basis = close
    assert source == "srct_1x2_closing"
    assert basis == "srct_pinnacle"
    assert 0.45 < prob < 0.55  # (2.0, 3.5, 3.5) 均衡三向


def test_srct_anchor_kickoff_fallback_for_name_variant(db) -> None:
    """客队命名变体（赫拉克勒斯/赫拉克莱斯型）→ kickoff 精确+主队兜底命中。"""
    duck_con = seed_duck(away="客队英超变体")
    fixture = seed_fixture(db)
    # 主键不中（away 不同）；兜底 kickoff 精确（北京 09-13 03:00）+ home 命中
    duck_con.execute(
        "UPDATE fixture_universe SET kickoff = TIMESTAMP '2026-09-13 03:00:00'"
    )
    close = clv._closing_prob(db, fixture, "h", duck_con=duck_con)
    assert close is not None
    assert close[2] == "srct_pinnacle"


def test_srct_anchor_no_match_returns_none(db) -> None:
    """两步键全不中（CorpusScope 外场）→ 诚实无锚。"""
    duck_con = seed_duck(home="无关队", away="无关队")
    fixture = seed_fixture(db)
    assert clv._closing_prob(db, fixture, "h", duck_con=duck_con) is None


def test_srct_anchor_excludes_post_kickoff_event(db) -> None:
    """published_at > kickoff 的迟到事件不取（票 34 防前视，源语义适配）。"""
    duck_con = seed_duck(
        events=(
            ("2026-09-12 05:00:00+08:00", 2.0, 3.5, 3.5),
            ("2026-09-13 04:00:00+08:00", 1.5, 4.0, 6.0),  # 开球后的场内价
        )
    )
    duck_con.execute(
        "UPDATE fixture_universe SET kickoff = TIMESTAMP '2026-09-13 03:00:00'"
    )
    fixture = seed_fixture(db)
    close = clv._closing_prob(db, fixture, "h", duck_con=duck_con)
    assert close is not None
    assert 0.45 < close[0] < 0.55  # 取盘前 2.0 一笔，非场内 1.5


def test_srct_anchor_latest_pre_kickoff_wins(db) -> None:
    """盘前多笔取最新（published_at 降序第一）。"""
    duck_con = seed_duck(
        events=(
            ("2026-09-12 01:00:00+08:00", 2.2, 3.4, 3.2),
            ("2026-09-12 20:30:00+08:00", 1.9, 3.6, 4.2),
        )
    )
    duck_con.execute(
        "UPDATE fixture_universe SET kickoff = TIMESTAMP '2026-09-13 03:00:00'"
    )
    fixture = seed_fixture(db)
    close = clv._closing_prob(db, fixture, "a", duck_con=duck_con)
    assert close is not None
    assert close[0] < 0.35  # 4.2 客胜 → 客胜概率显著低


def test_reconcile_uses_srct_anchor_when_odds_api_dead(db) -> None:
    """odds_api 三级空手 + duck_con 传入 → srct 锚写 clv_records（端到端）。"""
    duck_con = seed_duck()
    duck_con.execute(
        "UPDATE fixture_universe SET kickoff = TIMESTAMP '2026-09-13 03:00:00'"
    )
    fixture = seed_fixture(db)
    bet = create_bet_with_legs(
        db,
        BetDraft(
            mode=BetMode.PAPER,
            stake=2.0,
            legs=[
                LegInput(
                    fixture_id=fixture,
                    market_code="had",
                    selection_code="h",
                    locked_odds=2.5,
                )
            ],
        ),
    )
    settle_bet(db, bet)
    stats = clv.reconcile_clv(db, duck_con=duck_con)
    assert stats.recorded == 1
    row = db.execute("SELECT * FROM clv_records").fetchone()
    assert row["close_basis"] == "srct_pinnacle"
    assert row["close_source"] == "srct_1x2_closing"
    report = clv.clv_report(db)
    assert report["by_close_basis"]["srct_pinnacle"]["bets"] == 1


def test_srct_anchor_degrades_when_views_missing(db) -> None:
    """corpus 库在而银层视图缺席 → 降级 None 不打挂对账（票 75 验收 5）。"""
    import duckdb

    empty_con = duckdb.connect(":memory:")  # 无 fixture_universe/odds_change_event
    fixture = seed_fixture(db)
    assert clv._closing_prob(db, fixture, "h", duck_con=empty_con) is None


def test_corpus_anchor_contextmanager_closes(monkeypatch) -> None:
    """corpus_anchor 上下文管理器：正常路径关闭连接；缺席路径降级 None。"""
    closed: list[bool] = []

    class FakeCon:
        def close(self) -> None:
            closed.append(True)

    import goalx_backend.evaluation.clv as clv_mod

    fake = FakeCon()
    monkeypatch.setattr(clv_mod.corpus_duckdb, "connect", lambda _s: fake)
    with clv_mod.corpus_anchor() as anchor:
        assert anchor is fake
    assert closed == [True]

    def boom(_s: object) -> object:
        raise OSError("no corpus")

    monkeypatch.setattr(clv_mod.corpus_duckdb, "connect", boom)
    with clv_mod.corpus_anchor() as anchor:
        assert anchor is None
    assert closed == [True]  # 缺席路径无连接可关
