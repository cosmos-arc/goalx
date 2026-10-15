"""机会漏斗观测（caliber-loop 票 04）：五级分母/流失原因/轨一估值的行为断言。"""

from __future__ import annotations

import json
import sqlite3

from goalx_backend.betting import funnel as fn
from goalx_backend.data import fixtures as fx_store
from goalx_backend.models import MatchCodeInput, SaleStatusInput, SnapshotInput, Tier

KICKOFF = "2026-10-17T18:00:00+00:00"
AS_OF = "2026-10-16T14:04:00+00:00"  # 冻结时点（kickoff 前一日）
BUSINESS_DATE = "2026-10-16"
SOURCE_TIME = "2026-10-16T14:00:00+00:00"
OBSERVED = "2026-10-16T14:03:00+00:00"
STALE_TIME = "2026-10-15T02:00:00+00:00"  # 超 300s 窗的陈报价


def _seed_fixture(db: sqlite3.Connection, *, kickoff: str = KICKOFF, n: int = 0) -> int:
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"主队{n}")
    away = fx_store.upsert_team(db, f"客队{n}")
    fixture = fx_store.upsert_fixture(db, competition, kickoff, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date=BUSINESS_DATE,
            code=f"周六{n:03d}",
        ),
    )
    db.commit()
    return fixture


def _add_jc(
    db: sqlite3.Connection,
    fixture: int,
    prices: dict[str, float | None],
    *,
    captured: str = SOURCE_TIME,
    observed: str | None = OBSERVED,
) -> None:
    for sel, odds in prices.items():
        if odds is None:
            continue
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="sporttery",
                odds=odds,
                captured_at=captured,
                observed_at=observed,
                source_updated_at=captured,
            ),
        )
    db.commit()


def _add_anchor(
    db: sqlite3.Connection,
    fixture: int,
    prices: dict[str, float | None],
    *,
    captured: str = SOURCE_TIME,
    observed: str = OBSERVED,
) -> None:
    for sel, odds in prices.items():
        if odds is None:
            continue
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="odds_api:pinnacle",
                odds=odds,
                captured_at=captured,
                observed_at=observed,
                source_updated_at=captured,
            ),
        )
    db.commit()


def _add_sale(
    db: sqlite3.Connection,
    fixture: int,
    *,
    state: str = "on_sale",
    single: bool | None = True,
) -> None:
    fx_store.append_sale_status(
        db,
        SaleStatusInput(
            fixture_id=fixture,
            market_code="had",
            sale_state=state,
            single_eligible=single,
            observed_at=OBSERVED,
        ),
    )
    db.commit()


def _seed_full_pass(db: sqlite3.Connection, n: int = 0) -> int:
    fixture = _seed_fixture(db, n=n)
    _add_jc(db, fixture, {"h": 2.10, "d": 3.40, "a": 3.20})
    _add_anchor(db, fixture, {"h": 2.20, "d": 3.50, "a": 3.30})
    _add_sale(db, fixture)
    return fixture


def test_funnel_all_pass(db: sqlite3.Connection) -> None:
    """全过：五级计数全等；轨一价值代理三向落库；幂等重跑不重复。"""
    fixture = _seed_full_pass(db)
    report = fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    assert report.n_on_sale == 1
    assert report.n_quote_matched == 1
    assert report.n_fresh == 1
    assert report.n_valuation == 1
    assert report.n_eligible == 1
    assert report.loss_reasons == {}
    row = db.execute(
        "SELECT * FROM funnel_fixture_days WHERE fixture_id = ?", (fixture,)
    ).fetchone()
    assert row["level_reached"] == fn.L_ELIGIBLE
    assert row["loss_reason"] is None
    assert row["anchor_ref"] == "pinnacle"
    # 价值代理 = O_JC × p_sharp − 1（h 侧：2.10×p_sharp_h−1）
    assert row["jc_odds_h"] == 2.10
    assert row["value_proxy_h"] is not None
    # 不写 bets 表（票 04 纪律）
    assert db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 0
    # 幂等：同业务日重跑不重复行
    fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    assert db.execute("SELECT COUNT(*) FROM funnel_days").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM funnel_fixture_days").fetchone()[0] == 1


def test_funnel_partial_losses_level_chain(db: sqlite3.Connection) -> None:
    """部分流失：逐级计数与流失原因各归其级（验收：分母链构造断言）。"""
    full = _seed_full_pass(db, n=0)
    # L2 流失：无锚书报价（jc 齐）
    no_anchor = _seed_fixture(db, n=1)
    _add_jc(db, no_anchor, {"h": 2.0, "d": 3.3, "a": 3.5})
    _add_sale(db, no_anchor)
    # L3 流失：锚书报价超窗陈旧
    stale_anchor = _seed_fixture(db, n=2)
    _add_jc(db, stale_anchor, {"h": 2.0, "d": 3.3, "a": 3.5})
    _add_anchor(
        db,
        stale_anchor,
        {"h": 2.2, "d": 3.4, "a": 3.6},
        captured=STALE_TIME,
        observed=STALE_TIME,  # 采集没跑：本机很久未见新报价
    )
    _add_sale(db, stale_anchor)
    # L5 流失：单固不合规
    not_single = _seed_fixture(db, n=3)
    _add_jc(db, not_single, {"h": 2.0, "d": 3.3, "a": 3.5})
    _add_anchor(db, not_single, {"h": 2.2, "d": 3.4, "a": 3.6})
    _add_sale(db, not_single, single=False)
    # L1 外：当日有销售编号但已开赛（在售分母排除）
    kicked = _seed_fixture(db, kickoff="2026-10-16T10:00:00+00:00", n=4)
    _add_jc(db, kicked, {"h": 2.0, "d": 3.3, "a": 3.5})
    report = fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    assert report.n_on_sale == 4  # kicked 不进分母
    assert report.n_quote_matched == 3  # no_anchor 在 L2 流失
    assert report.n_fresh == 2  # stale_anchor 在 L3 流失
    assert report.n_valuation == 2  # devig 恒可出（fresh 锚在场）
    assert report.n_eligible == 1  # not_single 在 L5 流失
    assert report.loss_reasons == {
        "l2_anchor_absent": 1,
        "l3_anchor_stale": 1,
        "l5_single_not_eligible": 1,
    }
    assert full  # 全过夹具已由 test_funnel_all_pass 覆盖语义
    # 轨二列可空（票 05 填充）+ 收盘对账列可空（票 06 回填）
    row = db.execute(
        "SELECT track2_json, p_sharp_close_h FROM funnel_fixture_days"
        " WHERE fixture_id = ?",
        (full,),
    ).fetchone()
    assert row["track2_json"] is None
    assert row["p_sharp_close_h"] is None


def test_funnel_all_rejected(db: sqlite3.Connection) -> None:
    """全拒：在售有分母、以下全零、流失原因如实（零注可审计区分）。"""
    _seed_fixture(db, n=0)  # 无任何报价/状态
    report = fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    assert report.n_on_sale == 1
    assert report.n_eligible == 0
    assert report.value_proxy_stats == {
        "h": {"n": 0, "mean": None, "max": None},
        "d": {"n": 0, "mean": None, "max": None},
        "a": {"n": 0, "mean": None, "max": None},
    }
    assert "l2_jc_jc_no_quote" in report.loss_reasons


def test_funnel_liveness_and_day_row(db: sqlite3.Connection) -> None:
    """判活=漏斗表最新业务日；汇总行 JSON 可读回。"""
    assert fn.funnel_liveness(db) is None
    _seed_full_pass(db)
    fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    assert fn.funnel_liveness(db) == BUSINESS_DATE
    row = fn.funnel_day_row(db, BUSINESS_DATE)
    assert row is not None
    assert json.loads(row["loss_reasons"]) == {}
    stats = json.loads(row["value_proxy_stats"])
    assert stats["h"]["n"] == 1


def test_value_proxy_matches_spec_example(db: sqlite3.Connection) -> None:
    """价值代理口径：O_JC × p_sharp − 1（spec story 11 示例同构）。"""
    fixture = _seed_fixture(db)
    _add_jc(db, fixture, {"h": 2.10, "d": 3.40, "a": 3.20})
    # 均价 2.2/3.5/3.3 devig 后 p_sharp_h≈0.43 量级——断言口径非精确值
    _add_anchor(db, fixture, {"h": 2.20, "d": 3.50, "a": 3.30})
    _add_sale(db, fixture)
    fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    row = db.execute(
        "SELECT value_proxy_h, p_sharp_h, jc_odds_h FROM funnel_fixture_days"
        " WHERE fixture_id = ?",
        (fixture,),
    ).fetchone()
    assert row["value_proxy_h"] == round(row["jc_odds_h"] * row["p_sharp_h"] - 1.0, 4)
