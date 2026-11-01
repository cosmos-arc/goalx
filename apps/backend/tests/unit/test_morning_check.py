"""09:05 晨检代码化测试（ingest-orchestration 票 01）：逐项红绿判定。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.models import MatchCodeInput, Tier
from goalx_backend.morning_check import (
    DEGRADED,
    FAIL,
    OK,
    check_bets,
    check_corpus,
    check_ledger,
    check_night,
    check_pool_drift,
    check_remat,
    check_serve_log,
    check_today_eu,
    check_wrap,
)

NOW = datetime(2026, 11, 1, 1, 5, tzinfo=UTC)  # 北京 09:05


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


@pytest.fixture
def store(tmp_path: Path) -> CorpusStore:
    return CorpusStore(tmp_path / "corpus")


def _night_row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "night_date": "2026-10-31",
        "started_at": _iso(9),
        "ended_at": _iso(2),
        "stop_reason": "completed",
        "dates_attempted": 1,
        "dates_done": 1,
        "dates_not_found": 0,
        "pending_before": 0,
        "pending_after": 0,
        "requests": 100,
        "raw_new": 50,
        "parsed_ok": 50,
        "bronze_repaired": 0,
        "xg_matches": 0,
        "parse_failed_count": 0,
        "failed_count": 0,
        "failed_json": "[]",
        "budget_cap": 8000,
    }
    base.update(overrides)
    return base


def test_check_night_states(store: CorpusStore) -> None:
    """completed=绿；budget/circuit=黄；过旧/无行=红。"""
    store.record_night_summary(_night_row())
    assert check_night(store, now=NOW).status == OK
    store.record_night_summary(_night_row(stop_reason="budget"))
    assert check_night(store, now=NOW).status == DEGRADED
    store.record_night_summary(_night_row(ended_at=_iso(40)))
    assert check_night(store, now=NOW).status == FAIL


def test_check_remat_and_corpus(store: CorpusStore) -> None:
    """状态行/最新工件新鲜判定（无行=红；26h/36h 界）。"""
    assert check_remat(store, now=NOW).status == FAIL
    store.upsert_remat_state(_iso(3), 100, "{}")
    assert check_remat(store, now=NOW).status == OK
    assert check_corpus(store, now=NOW).status == FAIL
    store.ingest_raw("srct", "day_page", "k1", b"x", ext=".htm")
    # ingest_raw 落 fetched_at=now（真实钟），对 NOW(2026-11-01) 而言必新鲜
    item = check_corpus(store, now=datetime.now(UTC))
    assert item.status == OK


def test_check_wrap_and_ledger(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO funnel_days (business_date, as_of, n_on_sale, n_quote_matched,"
        " n_fresh, n_valuation, n_eligible, created_at)"
        " VALUES ('2026-10-31', ?, 1, 1, 1, 1, 0, ?)",
        (_iso(2), _iso(2)),
    )
    db.commit()
    assert check_wrap(db, now=NOW).status == OK
    assert check_ledger(db, now=NOW).status == FAIL  # 空账本=红
    db.execute(
        "INSERT INTO price_deviation_ledger (fixture_id, market_code, selection_code,"
        " jc_odds, observed_at, snapshot_id, created_at)"
        " VALUES (1, 'had', 'h', 2.0, ?, 1, ?)",
        (_iso(3), _iso(3)),
    )
    db.commit()
    assert check_ledger(db, now=NOW).status == OK


def test_check_today_eu_buckets(db: sqlite3.Connection) -> None:
    """有链=绿；有场无链=红（红不许静默跳过）；空板=黄。"""
    business_date = fx_store.beijing_business_date(NOW)
    assert check_today_eu(db, business_date=business_date).status == DEGRADED
    comp = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, "阿森纳")
    away = fx_store.upsert_team(db, "切尔西")
    fixture = fx_store.upsert_fixture(db, comp, "2026-11-01T18:00:00+00:00", home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date=business_date,
            code="周日001",
        ),
    )
    db.commit()
    assert check_today_eu(db, business_date=business_date).status == FAIL
    db.execute(
        "INSERT INTO source_match_links (fixture_id, source, source_match_id, method,"
        " status, mapped_at) VALUES (?, 'srct', '123', 'primary_key', 'linked', ?)",
        (fixture, _iso(1)),
    )
    db.commit()
    assert check_today_eu(db, business_date=business_date).status == OK


def test_check_bets_observation(db: sqlite3.Connection) -> None:
    assert check_bets(db).status == OK
    assert "0 注" in check_bets(db).detail


def test_check_serve_log(tmp_path: Path) -> None:
    clean = tmp_path / "serve.log"
    clean.write_text("hello\n")
    assert check_serve_log(clean).status == OK
    dirty = tmp_path / "dirty.log"
    dirty.write_text("x\nCRASHED\nTraceback (most recent call last):\n")
    assert check_serve_log(dirty).status == DEGRADED
    assert check_serve_log(tmp_path / "absent.log").status == DEGRADED


def _pool_client(
    *, sfc: str = "26155", jqc: str | None = "26225", fail: bool = False
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(500, text="boom")
        param = str(request.url.params["param"])
        if param.startswith("94,"):
            face = (
                {"jqcMatch": {"lotteryDrawNum": jqc, "matchList": [{"matchNum": 1}]}}
                if jqc
                else {"jqcMatch": {}}
            )
            return httpx.Response(200, json={"success": True, "value": face})
        face = {
            "sfcMatch": {"lotteryDrawNum": sfc, "matchList": [{"matchNum": 1}]},
        }
        return httpx.Response(200, json={"success": True, "value": face})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_pool_drift(db: sqlite3.Connection) -> None:
    """当期在库=绿；库内落后=红（goals4 探测器）；面空+库旧=红；端点挂=黄。"""
    settings = Settings()
    pool_store.upsert_pool_period(db, "ttt14", "26155", _iso(-10))
    pool_store.upsert_pool_period(db, "goals4", "26225", _iso(-10))
    db.commit()
    assert check_pool_drift(db, settings, _pool_client()).status == OK
    degraded = check_pool_drift(db, settings, _pool_client(fail=True))
    assert degraded.status == DEGRADED


def test_check_pool_drift_detects_goals4_outage(db: sqlite3.Connection) -> None:
    """goals4 断供形态（票 02 事故）：库内止于 7 月 + 官方当期在售 → 红。"""
    settings = Settings()
    pool_store.upsert_pool_period(db, "ttt14", "26155", _iso(-10))
    pool_store.upsert_pool_period(db, "goals4", "26133", _iso(2400))  # 止于 7 月
    db.commit()
    item = check_pool_drift(db, settings, _pool_client())
    assert item.status == FAIL
    assert "goals4" in item.detail
    # 官方 jqc 面空（过渡态）且库旧 → 仍红（断供复发形态）
    empty_face = check_pool_drift(db, settings, _pool_client(jqc=None))
    assert empty_face.status == FAIL
