"""票 08 新鲜度双口径影子测量：记录器/读数/晨检项（300s 规则零改动）。"""

from __future__ import annotations

import sqlite3

from goalx_backend.data import quote_evidence as qe
from goalx_backend.data.today import HadQuoteStatus, TodayFixtureView


def _row(
    fixture_id: int, status: str, reasons: list[str], **clocks: float | None
) -> TodayFixtureView:
    return TodayFixtureView(
        fixture_id=fixture_id,
        match_code=f"周六{fixture_id:03d}",
        business_date="2026-11-04",
        competition="英超",
        tier="tier1",
        home_team="主队",
        away_team="客队",
        kickoff_utc="2026-11-04T19:00:00+00:00",
        is_single=True,
        joined=True,
        jc_odds={},
        had_quote=HadQuoteStatus(
            as_of="2026-11-04T12:00:00+00:00",
            status=status,
            reasons=reasons,
            **clocks,  # type: ignore[arg-type]
        ),
    )


def test_recorder_writes_dual_conclusions_and_idempotent(
    db: sqlite3.Connection,
) -> None:
    """票 08：影子行=五时钟+双口径结论；同场同日重拍 REPLACE 不双行；
    正式判定字段零改动（影子独立表）。"""
    rows = [
        # 规则旧/观测新（分歧方向）
        _row(
            1,
            "rejected",
            ["stale_source"],
            jc_age_seconds=3600.0,
            jc_observed_age_seconds=10.0,
            sale_age_seconds=30.0,
            eu_age_seconds=120.0,
            pair_gap_seconds=5.0,
        ),
        # 双口径都新鲜
        _row(
            2,
            "valid",
            [],
            jc_age_seconds=60.0,
            jc_observed_age_seconds=20.0,
            sale_age_seconds=40.0,
            eu_age_seconds=80.0,
            pair_gap_seconds=3.0,
        ),
    ]
    counts = qe.record_freshness_shadow(
        db, rows, "2026-11-04", as_of="2026-11-04T12:00:00+00:00"
    )
    assert counts == {
        "n": 2,
        "rule_stale": 1,
        "observed_stale": 0,
        "rule_stale_observed_fresh": 1,
        "rule_fresh": 1,
        "observed_fresh": 2,
        "n_comparable": 2,
    }
    stored = db.execute(
        "SELECT * FROM freshness_shadow WHERE fixture_id = 1"
    ).fetchone()
    assert stored["rule_stale_source"] == 1
    assert stored["observed_stale"] == 0
    assert stored["price_age_seconds"] == 3600.0
    assert stored["observed_age_seconds"] == 10.0
    assert stored["sale_age_seconds"] == 30.0
    assert stored["eu_age_seconds"] == 120.0
    assert stored["pair_gap_seconds"] == 5.0
    assert stored["clock_gap_seconds"] == 10.0 - 3600.0  # 差值入库：观测龄−价格龄
    # 同日重拍：REPLACE 不双行（日拍最新一拍生效）
    qe.record_freshness_shadow(
        db, rows, "2026-11-04", as_of="2026-11-04T13:00:00+00:00"
    )
    total = db.execute(
        "SELECT COUNT(*) AS n FROM freshness_shadow WHERE business_date = '2026-11-04'"
    ).fetchone()["n"]
    assert total == 2


def test_summary_reads_dual_coverage(db: sqlite3.Connection) -> None:
    """票 08：读数=双口径新鲜覆盖+分歧计数；未拍日 None。"""
    assert qe.freshness_shadow_summary(db, "2026-11-04") is None
    rows = [
        _row(
            1,
            "rejected",
            ["stale_source"],
            jc_age_seconds=900.0,
            jc_observed_age_seconds=900.0,
        ),
        _row(
            2,
            "rejected",
            ["stale_source"],
            jc_age_seconds=900.0,
            jc_observed_age_seconds=10.0,
        ),
        _row(3, "valid", [], jc_age_seconds=60.0, jc_observed_age_seconds=60.0),
    ]
    qe.record_freshness_shadow(
        db, rows, "2026-11-04", as_of="2026-11-04T12:00:00+00:00"
    )
    summary = qe.freshness_shadow_summary(db, "2026-11-04")
    assert summary is not None
    assert summary["n"] == 3
    assert summary["rule_fresh"] == 1  # 规则口径只放 3 号
    assert summary["observed_fresh"] == 2  # 观测口径放 2/3 号
    assert summary["divergent_rule_stale_observed_fresh"] == 1


def test_morning_check_item_reports_and_tolerates_empty(db: sqlite3.Connection) -> None:
    """票 08：晨检项只读消费——未拍不硬判；有拍报双口径覆盖+分歧。"""
    from goalx_backend.morning_check import check_freshness_shadow

    empty = check_freshness_shadow(db)
    assert empty.status == "ok"
    assert "未拍" in empty.detail
    rows = [_row(1, "valid", [], jc_age_seconds=60.0, jc_observed_age_seconds=60.0)]
    qe.record_freshness_shadow(
        db, rows, "2026-11-04", as_of="2026-11-04T12:00:00+00:00"
    )
    item = check_freshness_shadow(db)
    assert item.status == "ok"  # 观察项不判红
    assert "规则新鲜 1/1" in item.detail
    assert "观测新鲜 1/1" in item.detail
