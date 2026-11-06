"""票 08 新鲜度双口径影子测量：记录器/读数/晨检项（300s 规则零改动）。"""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import quote_evidence as qe
from goalx_backend.data.ingest import sporttery
from goalx_backend.data.today import HadQuoteStatus, TodayFixtureView
from goalx_backend.models import ObservationInput


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
    """票 08/票 03(d)：晨检项只读消费——未拍=未开始积累不硬判；有拍报
    双口径覆盖+分歧;采集在拍而影子零样本=degraded;采样断流=degraded。"""
    from datetime import UTC, datetime

    from goalx_backend.db import utc_now_iso
    from goalx_backend.morning_check import check_freshness_shadow

    empty = check_freshness_shadow(db)
    assert empty.status == "ok"  # 采集侧亦无证据：观察不硬判
    assert "未开始积累" in empty.detail
    # 票 03(d)：采集侧 2 日内有证据而影子零样本——"未拍也绿"不再，判降级
    obs_id = fx_store.record_quote_observation(
        db,
        ObservationInput(
            source="sporttery",
            observed_at=utc_now_iso(),
            parse_version="test",
            raw_sha256="a" * 64,
        ),
    )
    assert obs_id is not None
    capturing = check_freshness_shadow(db)
    assert capturing.status == "degraded"
    assert "测量断线" in capturing.detail

    rows = [_row(1, "valid", [], jc_age_seconds=60.0, jc_observed_age_seconds=60.0)]
    qe.record_freshness_shadow(
        db, rows, "2026-11-04", as_of="2026-11-04T12:00:00+00:00"
    )
    item = check_freshness_shadow(db, now=datetime(2026, 11, 4, 12, tzinfo=UTC))
    assert item.status == "ok"  # 当日有拍：观察项不判红
    assert "规则新鲜 1/1" in item.detail
    assert "观测新鲜 1/1" in item.detail
    # 票 03(d)：最新拍落后超两个销售日=采样断流（详情带日期，防长期正常）
    stale = check_freshness_shadow(db, now=datetime(2026, 11, 10, 12, tzinfo=UTC))
    assert stale.status == "degraded"
    assert "采样断流" in stale.detail
    assert "2026-11-04" in stale.detail


def test_past_date_replay_refused(db: sqlite3.Connection) -> None:
    """票 08 correctness F2：过去日重放拒绝——无冻结 as_of 的回放会把
    当日读成 kickoff_passed 并 REPLACE 销毁原始快照。"""
    from goalx_backend import tasks
    from goalx_backend.data import fixtures as fx_store

    today = fx_store.beijing_business_date()
    rows = [_row(1, "valid", [], jc_age_seconds=60.0, jc_observed_age_seconds=60.0)]
    qe.record_freshness_shadow(db, rows, today, as_of="2026-11-04T12:00:00+00:00")
    import pytest

    with pytest.raises(ValueError, match="拒绝重放过去日"):
        tasks.freshness_shadow_day(business_date="2020-01-01")
    # 当日原始快照仍在（拒绝路径零写入）
    assert qe.freshness_shadow_summary(db, today) is not None


def test_morning_check_tolerates_missing_table(db: sqlite3.Connection) -> None:
    """票 08 correctness F3：v38 前旧库（表未建）晨检项降级'未开始积累'
    不抛——只读连接无法自 migrate，缺表部署窗不该打灭整张红绿面。"""
    from goalx_backend.morning_check import check_freshness_shadow

    db.execute("DROP TABLE IF EXISTS freshness_shadow")
    db.commit()
    item = check_freshness_shadow(db)
    assert item.status == "ok"
    assert "未开始积累" in item.detail


def _confirm_match() -> sporttery.ParsedMatch:
    """票 03 复现载体：一场在售场，had 三向，源调盘时刻固定（两次同价）。"""
    return sporttery.ParsedMatch(
        source_match_id="m-confirm-1",
        business_date="2026-11-05",
        code="周四001",
        league="英超",
        league_all_name="英格兰超级联赛",
        home_team="确认主队",
        away_team="确认客队",
        kickoff_utc="2026-11-05T19:00:00+00:00",
        is_single=True,
        markets=[
            sporttery.MarketQuote(
                market_code="had",
                captured_at="2026-11-04T04:30:00+00:00",
                prices={"h": 2.6, "d": 3.4, "a": 1.8},
            )
        ],
        sell_status_raw="1",  # 1=在售（票 09 语义考据）
    )


def test_same_price_recapture_confirmation_clock(db: sqlite3.Connection) -> None:
    """票 03（报告一案例 5）：同价重采去重保留（不落新快照行、不覆写
    observed_at），观测钟读最近一次成功确认——16:30:10 查询 age=10s
    （修复前 14410s=首见时刻）；查 15:00 仍只见 12:30 观测。"""
    day = "2026-11-04"
    match = _confirm_match()
    obs_ids = [
        fx_store.record_quote_observation(
            db,
            ObservationInput(
                source="sporttery",
                observed_at=f"{day}T{at}+00:00",
                parse_version="test",
                raw_sha256="a" * 64,
            ),
        )
        for at in ("12:30:00", "16:30:00")
    ]
    first = sporttery.store_matches(
        db, [match], observed_at=f"{day}T12:30:00+00:00", observation_id=obs_ids[0]
    )
    second = sporttery.store_matches(
        db, [match], observed_at=f"{day}T16:30:00+00:00", observation_id=obs_ids[1]
    )
    assert first.snapshots == 3
    assert second.snapshots == 0  # 同价重采不落新快照行（去重保留）
    assert second.duplicate_snapshots == 3
    fixture_id = int(
        db.execute(
            "SELECT id FROM fixtures WHERE kickoff_utc LIKE '2026-11-05%'"
        ).fetchone()[0]
    )
    fresh = qe.adjudicate_had_quote(db, fixture_id, f"{day}T16:30:10+00:00")
    assert fresh.jc_observed_at == f"{day}T16:30:00+00:00"
    assert fresh.jc_observed_age_seconds == 10.0  # 正确值 10s
    assert "stale_source" in fresh.reasons  # 规则钟不动：价 04:30 后未动
    as_1500 = qe.adjudicate_had_quote(db, fixture_id, f"{day}T15:00:00+00:00")
    assert as_1500.jc_observed_age_seconds == 9000.0  # 15:00 只见 12:30 观测
    # 验收 4：影子"规则旧、观测新"分歧浮出该场景（分歧数不再恒 0）
    view = _row(
        fixture_id,
        fresh.status,
        fresh.reasons,
        jc_age_seconds=fresh.jc_age_seconds,
        jc_observed_age_seconds=fresh.jc_observed_age_seconds,
    )
    counts = qe.record_freshness_shadow(db, [view], day, as_of=f"{day}T16:30:10+00:00")
    assert counts["rule_stale_observed_fresh"] == 1
    # 幂等：同证据重解析不落重复确认行
    sporttery.store_matches(
        db, [match], observed_at=f"{day}T16:30:00+00:00", observation_id=obs_ids[1]
    )
    confirmations = db.execute(
        "SELECT COUNT(*) AS n FROM had_quote_confirmations WHERE fixture_id = ?",
        (fixture_id,),
    ).fetchone()["n"]
    assert confirmations == 2
    # append-only 铁证（与 quote_observations/sale_statuses 同范式）
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(
            "DELETE FROM had_quote_confirmations WHERE fixture_id = ?", (fixture_id,)
        )
