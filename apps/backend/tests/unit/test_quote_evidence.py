"""as-of 报价证据判定测试（票 35 验收 1/4：时间语义与明确拒绝）。"""

from __future__ import annotations

import pytest

from goalx_backend import quote_evidence as qe
from goalx_backend.config import Settings
from goalx_backend.models import MatchCodeInput, SaleStatusInput, SnapshotInput, Tier
from goalx_backend.store import fixtures as fx_store

KICKOFF = "2026-09-13T18:00:00+00:00"
AS_OF = "2026-09-13T14:04:00+00:00"
# 源 14:00 调盘、本机 14:03 观测：对 14:04 的决策 age=240s（< 300s 新鲜窗）
SOURCE_TIME = "2026-09-13T14:00:00+00:00"
OBSERVED = "2026-09-13T14:03:00+00:00"


def seed_fixture(db, kickoff: str = KICKOFF) -> int:
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, "阿森纳")
    away = fx_store.upsert_team(db, "切尔西")
    fixture = fx_store.upsert_fixture(db, competition, kickoff, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date="2026-09-13",
            code="周六001",
        ),
    )
    db.commit()
    return fixture


def add_jc(
    db,
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


def add_book(
    db,
    fixture: int,
    book: str,
    prices: dict[str, float | None],
    *,
    captured: str = SOURCE_TIME,
    observed: str = OBSERVED,
    source_time: str | None = SOURCE_TIME,
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
                source=f"odds_api:{book}",
                odds=odds,
                captured_at=captured,
                observed_at=observed,
                source_updated_at=source_time,
            ),
        )
    db.commit()


def add_sale(
    db,
    fixture: int,
    *,
    state: str = "on_sale",
    single: bool | None = True,
    observed: str = OBSERVED,
) -> None:
    fx_store.append_sale_status(
        db,
        SaleStatusInput(
            fixture_id=fixture,
            market_code="had",
            sale_state=state,
            single_eligible=single,
            observed_at=observed,
        ),
    )
    db.commit()


def seed_valid(db) -> int:
    fixture = seed_fixture(db)
    add_jc(db, fixture, {"h": 2.10, "d": 3.40, "a": 3.20})
    add_book(db, fixture, "pinnacle", {"h": 2.20, "d": 3.50, "a": 3.30})
    add_book(db, fixture, "average", {"h": 2.18, "d": 3.48, "a": 3.28})
    add_sale(db, fixture)
    return fixture


def test_valid_verdict_carries_full_evidence(db) -> None:
    fixture = seed_valid(db)
    verdict = qe.adjudicate_had_quote(db, fixture, AS_OF)
    assert verdict.status == qe.VALID
    assert verdict.reasons == []
    assert verdict.jc_odds == {"h": 2.10, "d": 3.40, "a": 3.20}
    assert verdict.jc_age_seconds == 240.0
    assert verdict.sale_state == "on_sale"
    assert verdict.single_eligible is True
    assert verdict.eu_books == 2
    assert verdict.pair_gap_seconds == 0.0
    assert verdict.sources == ["sporttery", "odds_api:average", "odds_api:pinnacle"]
    assert verdict.eu_probabilities is not None
    assert pytest.approx(sum(verdict.eu_probabilities.values()), abs=1e-4) == 1.0


def test_late_observation_not_used_for_earlier_decision(db) -> None:
    """14:00 源更新、14:20 才首次观测 → 不能用于 14:05 决策（票 35 验收 1）。"""
    fixture = seed_fixture(db)
    add_jc(
        db,
        fixture,
        {"h": 2.10, "d": 3.40, "a": 3.20},
        observed="2026-09-13T14:20:00+00:00",
    )
    add_book(db, fixture, "pinnacle", {"h": 2.20, "d": 3.50, "a": 3.30})
    add_sale(db, fixture, observed="2026-09-13T14:20:00+00:00")
    verdict = qe.adjudicate_had_quote(db, fixture, "2026-09-13T14:05:00+00:00")
    assert verdict.status == qe.UNKNOWN
    assert "jc_observed_at_unknown" in verdict.reasons
    assert verdict.jc_odds == {}


def test_legacy_sporttery_rows_stay_unknown(db) -> None:
    """旧行无 observed_at：不可证明当时已知 → unknown，不倒填资格。"""
    fixture = seed_fixture(db)
    add_jc(db, fixture, {"h": 2.10, "d": 3.40, "a": 3.20}, observed=None)
    add_book(db, fixture, "pinnacle", {"h": 2.20, "d": 3.50, "a": 3.30})
    add_sale(db, fixture)
    verdict = qe.adjudicate_had_quote(db, fixture, AS_OF)
    assert verdict.status == qe.UNKNOWN
    assert "jc_observed_at_unknown" in verdict.reasons


def test_stale_source_rejected_with_age(db) -> None:
    fixture = seed_valid(db)
    verdict = qe.adjudicate_had_quote(
        db, fixture, "2026-09-13T14:55:00+00:00", freshness_seconds=300
    )
    # as_of 14:55, 源 14:00 → age=3300s > 300 → stale
    assert verdict.status == qe.REJECTED
    assert "stale_source" in verdict.reasons
    assert verdict.jc_age_seconds == 3300.0


def test_freshness_boundary_inclusive(db) -> None:
    fixture = seed_valid(db)
    # age 恰为 300s（as_of=14:05）不判 stale；301s 判 stale
    ok = qe.adjudicate_had_quote(db, fixture, "2026-09-13T14:05:00+00:00")
    assert "stale_source" not in ok.reasons
    late = qe.adjudicate_had_quote(db, fixture, "2026-09-13T14:05:01+00:00")
    assert "stale_source" in late.reasons


def test_kickoff_boundary_rejects(db) -> None:
    fixture = seed_valid(db)
    at_kickoff = qe.adjudicate_had_quote(db, fixture, KICKOFF)
    assert at_kickoff.status == qe.REJECTED
    assert at_kickoff.reasons == ["kickoff_passed"]
    before = qe.adjudicate_had_quote(db, fixture, "2026-09-13T17:59:59+00:00")
    assert "kickoff_passed" not in before.reasons


def test_sale_stopped_rejected(db) -> None:
    fixture = seed_fixture(db)
    add_jc(db, fixture, {"h": 2.10, "d": 3.40, "a": 3.20})
    add_sale(db, fixture, state="stopped")
    verdict = qe.adjudicate_had_quote(db, fixture, AS_OF)
    assert verdict.status == qe.REJECTED
    assert verdict.reasons == ["sale_stopped"]


def test_missing_three_way_rejected(db) -> None:
    fixture = seed_fixture(db)
    add_jc(db, fixture, {"h": 2.10, "a": 3.20})  # 缺平局价
    add_book(db, fixture, "pinnacle", {"h": 2.20, "d": 3.50, "a": 3.30})
    add_sale(db, fixture)
    verdict = qe.adjudicate_had_quote(db, fixture, AS_OF)
    assert verdict.status == qe.REJECTED
    assert "jc_three_way_incomplete" in verdict.reasons


def test_unknown_single_eligibility_flagged_not_valid_single(db) -> None:
    fixture = seed_fixture(db)
    add_jc(db, fixture, {"h": 2.10, "d": 3.40, "a": 3.20})
    add_book(db, fixture, "pinnacle", {"h": 2.20, "d": 3.50, "a": 3.30})
    add_sale(db, fixture, single=None)
    verdict = qe.adjudicate_had_quote(db, fixture, AS_OF)
    # 报价链有效但单固资格未知：明确标注，消费方必须拒绝单关正式候选
    assert verdict.single_eligible is None
    assert "single_eligibility_unknown" in verdict.reasons


def test_pair_gap_exceeded_drops_book(db) -> None:
    fixture = seed_fixture(db)
    add_jc(db, fixture, {"h": 2.10, "d": 3.40, "a": 3.20})
    add_book(db, fixture, "pinnacle", {"h": 2.20, "d": 3.50, "a": 3.30})
    # 另一家源时间与竞彩差 8 分钟 > 300s → 剔除并注明
    add_book(
        db,
        fixture,
        "slowbook",
        {"h": 2.25, "d": 3.55, "a": 3.35},
        source_time="2026-09-13T14:08:00+00:00",
    )
    add_sale(db, fixture)
    verdict = qe.adjudicate_had_quote(db, fixture, AS_OF)
    assert verdict.status == qe.VALID
    assert verdict.eu_books == 1
    assert "odds_api:slowbook:pair_gap_exceeded" in verdict.reasons


def test_eu_source_time_unknown_excluded(db) -> None:
    fixture = seed_fixture(db)
    add_jc(db, fixture, {"h": 2.10, "d": 3.40, "a": 3.20})
    add_book(db, fixture, "pinnacle", {"h": 2.20, "d": 3.50, "a": 3.30})
    add_book(db, fixture, "notime", {"h": 2.22, "d": 3.52, "a": 3.32}, source_time=None)
    add_sale(db, fixture)
    verdict = qe.adjudicate_had_quote(db, fixture, AS_OF)
    assert verdict.status == qe.VALID
    assert verdict.eu_books == 1
    assert "odds_api:notime:source_time_unknown" in verdict.reasons


def test_no_eu_consensus_unknown(db) -> None:
    fixture = seed_fixture(db)
    add_jc(db, fixture, {"h": 2.10, "d": 3.40, "a": 3.20})
    add_sale(db, fixture)
    verdict = qe.adjudicate_had_quote(db, fixture, AS_OF)
    assert verdict.status == qe.UNKNOWN
    assert "eu_no_quote" in verdict.reasons


def test_sale_status_only_from_before_as_of(db) -> None:
    """as_of 之后的停售观测不能证明 as_of 时已停售。"""
    fixture = seed_valid(db)
    fx_store.append_sale_status(
        db,
        SaleStatusInput(
            fixture_id=fixture,
            market_code="had",
            sale_state="stopped",
            observed_at="2026-09-13T14:10:00+00:00",
        ),
    )
    db.commit()
    verdict = qe.adjudicate_had_quote(db, fixture, AS_OF)
    assert verdict.sale_state == "on_sale"
    later = qe.adjudicate_had_quote(
        db, fixture, "2026-09-13T14:11:00+00:00", freshness_seconds=3600
    )
    assert later.status == qe.REJECTED
    assert later.reasons == ["sale_stopped"]


def test_cross_timezone_inputs(db) -> None:
    """+08:00 写入的 as_of 与 Z 结尾的 kickoff 正确比较（票 35 验收 4）。"""
    fixture = seed_valid(db)
    # 2026-09-13T18:00:00Z == 2026-09-14T02:00:00+08:00 → 恰开赛 → 拒绝
    verdict = qe.adjudicate_had_quote(db, fixture, "2026-09-14T02:00:00+08:00")
    assert verdict.reasons == ["kickoff_passed"]


# --- API 集成（票 35 交接契约的对外形态） ---


def test_had_quote_endpoint(tmp_path) -> None:

    from fastapi.testclient import TestClient

    from goalx_backend.db import connect, migrate
    from goalx_backend.main import create_app

    db_path = tmp_path / "api.db"
    conn = connect(db_path)
    migrate(conn)
    fixture = seed_fixture(conn)
    add_jc(conn, fixture, {"h": 2.10, "d": 3.40, "a": 3.20})
    add_book(conn, fixture, "pinnacle", {"h": 2.20, "d": 3.50, "a": 3.30})
    add_sale(conn, fixture)
    conn.close()

    settings = Settings(db_path=db_path)
    with TestClient(create_app(settings=settings)) as client:
        not_found = client.get("/api/v1/fixtures/999/had-quote")
        assert not_found.status_code == 404
        resp = client.get(
            f"/api/v1/fixtures/{fixture}/had-quote", params={"as_of": AS_OF}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "valid"
        assert body["jc_odds"]["h"] == pytest.approx(2.10)
        assert body["single_eligible"] is True
        assert body["pair_gap_seconds"] == 0.0
        # 自定义窗口也能表达
        strict = client.get(
            f"/api/v1/fixtures/{fixture}/had-quote",
            params={"as_of": AS_OF, "freshness_seconds": 60},
        )
        assert strict.json()["status"] == "rejected"
        assert "stale_source" in strict.json()["reasons"]
