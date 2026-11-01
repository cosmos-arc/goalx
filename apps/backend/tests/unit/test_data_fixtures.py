"""赛程/市场域仓储测试。"""

from __future__ import annotations

import sqlite3
from typing import Any

from goalx_backend.data import fixtures as fx
from goalx_backend.data import quote_evidence
from goalx_backend.models import MatchCodeInput, SaleStatusInput, SnapshotInput, Tier


def seed_fixture(
    db: sqlite3.Connection, kickoff: str = "2026-09-13T10:00:00+00:00"
) -> int:
    """插入一场比赛的全部关联实体，返回 fixture id。"""
    comp = fx.upsert_competition(
        db, "英超", tier=Tier.TIER1, odds_api_sport_key="soccer_epl"
    )
    home = fx.upsert_team(db, "阿森纳")
    away = fx.upsert_team(db, "切尔西")
    return fx.upsert_fixture(db, comp, kickoff, home, away)


def match_code(fixture: int, **overrides: object) -> MatchCodeInput:
    """构造竞彩销售编号输入。"""
    base: dict[str, Any] = {
        "fixture_id": fixture,
        "kind": "jingcai",
        "business_date": "2026-09-12",
        "code": "周六026",
        "source_match_id": "2041430",
        "is_single": False,
    }
    base.update(overrides)
    return MatchCodeInput(**base)


def snap(fixture: int, **overrides: object) -> SnapshotInput:
    """构造赔率快照输入。"""
    base: dict[str, Any] = {
        "fixture_id": fixture,
        "market_code": "had",
        "selection_code": "h",
        "source": "sporttery",
        "odds": 6.60,
        "captured_at": "2026-09-12T14:29:36+00:00",
    }
    base.update(overrides)
    return SnapshotInput(**base)


def test_upsert_fixture_idempotent(db: sqlite3.Connection) -> None:
    first = seed_fixture(db)
    second = seed_fixture(db)
    assert first == second


def test_match_code_and_source_lookup(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    fx.upsert_match_code(db, match_code(fixture))
    row = fx.find_fixture_by_source_match(db, "jingcai", "2041430")
    assert row is not None
    assert row["id"] == fixture


def test_snapshot_append_only_and_dedupe(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    first = fx.insert_odds_snapshot(db, snap(fixture))
    dup = fx.insert_odds_snapshot(db, snap(fixture))
    assert first is not None
    assert first > 0
    assert dup is None  # 同自然键重复采集被吸收（append-only）
    fx.insert_odds_snapshot(
        db, snap(fixture, odds=6.40, captured_at="2026-09-12T16:00:00+00:00")
    )
    latest = fx.latest_odds_by_selection(db, fixture, "had", "sporttery")
    assert latest["h"] == (6.40, "2026-09-12T16:00:00+00:00")
    assert len(fx.odds_history(db, fixture, "had")) == 2


def test_snapshot_persisted_meta(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    fx.insert_odds_snapshot(
        db,
        snap(
            fixture,
            market_code="hhad",
            odds=2.95,
            captured_at="2026-09-12T14:00:00+00:00",
            meta={"goal_line": "+1"},
        ),
    )
    row = fx.odds_history(db, fixture, "hhad")[0]
    assert row["meta"] == '{"goal_line": "+1"}'
    assert row["purpose"] == "live_capture"


def test_eu_book_odds_latest_per_book(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    for book, odds in (("odds_api:pinnacle", 1.85), ("odds_api:bet365", 1.90)):
        fx.insert_odds_snapshot(
            db,
            snap(
                fixture,
                selection_code="a",
                source=book,
                odds=odds,
                captured_at="2026-09-12T15:00:00+00:00",
            ),
        )
    # pinnacle 后续调价
    fx.insert_odds_snapshot(
        db,
        snap(
            fixture,
            selection_code="a",
            source="odds_api:pinnacle",
            odds=1.80,
            captured_at="2026-09-12T16:00:00+00:00",
        ),
    )
    books = quote_evidence.eu_book_odds(db, fixture)
    assert books["a"] == {"odds_api:pinnacle": 1.80, "odds_api:bet365": 1.90}


def test_fixtures_for_business_dates_window(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    fx.upsert_match_code(db, match_code(fixture))
    # 同一场次在次日的业务日再挂一个销售编号（改期场景：编号按业务日归属）
    fx.upsert_match_code(
        db,
        match_code(
            fixture,
            business_date="2026-09-13",
            code="周日026",
            source_match_id="2041431",
        ),
    )
    rows = fx.fixtures_for_business_dates(
        db, ["2026-09-12", "2026-09-13", "2026-09-14"]
    )
    assert [row["match_code"] for row in rows] == ["周六026", "周日026"]
    # 行内携带业务日归属（票 wb-01：前端按日分组依据）
    assert [row["business_date"] for row in rows] == ["2026-09-12", "2026-09-13"]
    assert fx.fixtures_for_business_dates(db, []) == []
    assert fx.fixtures_for_business_dates(db, ["2026-09-14"]) == []


def test_fixtures_for_business_date_single(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    fx.upsert_match_code(db, match_code(fixture))
    rows = fx.fixtures_for_business_date(db, "2026-09-12")
    assert len(rows) == 1
    assert rows[0]["match_code"] == "周六026"
    assert rows[0]["competition_name"] == "英超"
    assert rows[0]["home_team"] == "阿森纳"
    assert fx.fixtures_for_business_date(db, "2026-09-13") == []


def test_odds_api_join_persisted(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db)
    fx.set_odds_api_join(db, fixture, "evt123", "soccer_epl", "time_window")
    row = fx.get_fixture(db, fixture)
    assert row is not None
    assert row["odds_api_event_id"] == "evt123"
    assert row["join_method"] == "time_window"
    fx.unset_odds_api_join(db, fixture)
    row = fx.get_fixture(db, fixture)
    assert row is not None
    assert row["odds_api_event_id"] is None


def test_pending_jingcai_fixtures(db: sqlite3.Connection) -> None:
    fixture = seed_fixture(db, kickoff="2026-09-13T10:00:00+00:00")
    fx.upsert_match_code(db, match_code(fixture, source_match_id="1"))
    pending = fx.pending_jingcai_fixtures(db, "2026-09-13T12:00:00+00:00")
    assert [row["id"] for row in pending] == [fixture]
    assert fx.pending_jingcai_fixtures(db, "2026-09-13T09:00:00+00:00") == []


def test_sale_window_phase_default_boundaries() -> None:
    """缺省窗 [11:30, 22:00) 北京时：开端含、收端不含。"""
    assert fx.sale_window_phase("2026-11-01T03:29:59+00:00") == "pre_open"
    assert fx.sale_window_phase("2026-11-01T03:30:00+00:00") == "in_window"
    assert fx.sale_window_phase("2026-11-01T13:59:59+00:00") == "in_window"
    assert fx.sale_window_phase("2026-11-01T14:00:00+00:00") == "post_close"


def test_sale_window_phase_anchor_overrides_open() -> None:
    """当日锚（13:00 北京）覆写缺省开时刻：锚前窗外、锚点含开端。"""
    anchor = "2026-11-01T05:00:00+00:00"
    assert (
        fx.sale_window_phase("2026-11-01T03:31:00+00:00", anchor=anchor) == "pre_open"
    )
    assert (
        fx.sale_window_phase("2026-11-01T05:00:00+00:00", anchor=anchor) == "in_window"
    )


def test_sale_window_anchor_first_on_sale_of_beijing_day(
    db: sqlite3.Connection,
) -> None:
    """当日锚=该北京日首个 on_sale 观测；北京日界把 UTC 16:00 归次日。"""
    fixture = seed_fixture(db)
    for observed, state in (
        ("2026-10-21T01:00:00+00:00", "on_sale"),  # 北京 10-21 09:00
        ("2026-10-21T06:00:00+00:00", "on_sale"),  # 北京 10-21 14:00
        ("2026-10-20T16:00:00+00:00", "on_sale"),  # 北京 10-21 00:00（界上）
        ("2026-10-20T15:59:00+00:00", "on_sale"),  # 北京 10-20 23:59（界外）
    ):
        fx.append_sale_status(
            db,
            SaleStatusInput(
                fixture_id=fixture,
                market_code="had",
                sale_state=state,
                observed_at=observed,
            ),
        )
    db.commit()
    assert fx.sale_window_anchor(db, "2026-10-21") == "2026-10-20T16:00:00+00:00"
    assert fx.sale_window_anchor(db, "2026-11-01") is None


def test_sale_window_phase_cross_day_anchor_ignored() -> None:
    """跨北京日的锚弃用回缺省界（相位按当日窗解释）。"""
    stale_anchor = "2026-10-31T18:00:00+00:00"  # 10-31 日锚，非 11-01
    assert (
        fx.sale_window_phase("2026-11-01T03:31:00+00:00", anchor=stale_anchor)
        == "in_window"
    )


def test_sale_window_anchor_observation_cadence_blind_band() -> None:
    """钉住盲带语义（correctness W1；票 04 观测加密后语义更新）：锚=首观测
    非真开售时刻，[缺省开, 锚) 带内 not_on_sale 保守归窗外。观测密度
    =10:00/12:30/16:30/19:00 定拍 + probe 开售轮询（北京 [11:00,12:30)
    5 分钟一拍至翻转）→ 销售日盲带 ≈轮询拍距；改观测节奏或收紧语义时
    本测试是警示灯。"""
    anchor = "2026-10-22T11:00:00+00:00"  # 首拍 on_sale（19:00 北京定拍日形态）
    assert (
        fx.sale_window_phase("2026-10-22T04:00:00+00:00", anchor=anchor) == "pre_open"
    )
    assert (
        fx.not_on_sale_reject_reason(
            fx.sale_window_phase("2026-10-22T04:00:00+00:00", anchor=anchor)
        )
        == "sale_window_closed"
    )
    # 票 04 形态：probe 轮询翻转（11:35 北京）→ 锚=翻转拍点，带内仅剩
    # [真开售, 翻转拍) ≤5 分钟
    polled_anchor = "2026-10-22T03:35:00+00:00"
    assert (
        fx.sale_window_phase("2026-10-22T03:40:00+00:00", anchor=polled_anchor)
        == "in_window"
    )
