"""betting/haircut 源T 接线测试（odds-read-switch 票 05）。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import duckdb

from goalx_backend.betting import funnel as funnel_mod
from goalx_backend.betting.bets import validate_had_selections
from goalx_backend.betting.snapshot import build_ev_snapshot
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import quote_evidence
from goalx_backend.evaluation import haircut as hc
from goalx_backend.models import LegInput, SnapshotInput

BEIJING = timezone(timedelta(hours=8))
ANCHOR = quote_evidence.SRCT_PINNACLE_BOOK
AS_OF = "2026-10-31T01:00:00+00:00"
OBSERVED = datetime(2026, 10, 31, 8, 52, tzinfo=BEIJING)  # 00:52Z：haircut 配对窗内
KICKOFF_UTC = "2026-10-31T12:30:00+00:00"


def seed_corpus_duck() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE odds_change_event (
            sid VARCHAR, bookmaker_id VARCHAR, market VARCHAR,
            published_at TIMESTAMPTZ, observed_at TIMESTAMPTZ,
            odds_home DOUBLE, odds_draw DOUBLE, odds_away DOUBLE,
            source_order INTEGER
        )
        """
    )
    con.execute("CREATE TABLE bookmaker (bookmaker_id VARCHAR, match_count BIGINT)")
    events = [
        (ANCHOR, 2.0, 3.5, 3.2),
        ("srct:1x2:281", 2.05, 3.45, 3.25),
    ]
    for book, h, d, a in events:
        con.execute(
            "INSERT INTO odds_change_event VALUES ('3001', ?, '1x2', ?, ?, ?, ?, ?, 1)",
            [book, OBSERVED - timedelta(hours=2), OBSERVED, h, d, a],
        )
    con.execute("INSERT INTO bookmaker VALUES (?, 100)", [ANCHOR])
    con.execute("INSERT INTO bookmaker VALUES ('srct:1x2:281', 90)")
    return con


def seed_on_sale_fixture(
    db: sqlite3.Connection,
    *,
    business_date: str = "2026-10-31",
    linked: bool = True,
    kickoff: str = KICKOFF_UTC,
    jc_at: str = "2026-10-31T00:55:00+00:00",
) -> int:
    competition = fx_store.upsert_competition(db, "英超")
    home = fx_store.upsert_team(db, "主队英超")
    away = fx_store.upsert_team(db, "客队英超")
    fixture_id = fx_store.upsert_fixture(db, competition, kickoff, home, away)
    db.execute(
        "INSERT INTO match_codes (fixture_id, kind, business_date, code)"
        " VALUES (?, 'jingcai', ?, '周六001')",
        (fixture_id, business_date),
    )
    db.execute(
        "INSERT INTO sale_statuses (fixture_id, market_code, sale_state,"
        " single_eligible, observed_at, source_updated_at, created_at)"
        " VALUES (?, 'had', 'on_sale', 1, '2026-10-30T00:00:00+00:00',"
        " '2026-10-30T00:00:00+00:00', '2026-10-30T00:00:00+00:00')",
        (fixture_id,),
    )
    for sel in ("h", "d", "a"):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture_id,
                market_code="had",
                selection_code=sel,
                source="sporttery",
                odds=2.2 if sel == "h" else 3.3,
                captured_at=jc_at,
                observed_at=jc_at,
                source_updated_at=jc_at,
            ),
        )
    if linked:
        db.execute(
            "INSERT INTO source_match_links"
            " (fixture_id, source, source_match_id, method, status, meta, mapped_at)"
            " VALUES (?, 'srct', '3001', 'primary_key', 'linked', '{}',"
            " '2026-10-30T00:00:00+00:00')",
            (fixture_id,),
        )
    return fixture_id


def test_funnel_classify_matches_via_corpus(db: sqlite3.Connection) -> None:
    """漏斗分级：corpus 有链场 EU 锚/共识出数，L2 达 matched。"""
    fixture_id = seed_on_sale_fixture(db)
    duck_con = seed_corpus_duck()
    row = funnel_mod._classify_fixture(db, fixture_id, AS_OF, duck_con=duck_con)
    assert row.loss_reason is None or not str(row.loss_reason).startswith("l2_anchor")
    assert row.level_reached >= funnel_mod.L_MATCHED
    # 无 corpus（legacy 死源）→ 锚缺失归因（对照组，接线前后行为分水岭）
    legacy = funnel_mod._classify_fixture(db, fixture_id, AS_OF, duck_con=None)
    assert legacy.loss_reason == "l2_anchor_absent"


def test_ev_snapshot_consensus_via_corpus(db: sqlite3.Connection) -> None:
    """建注 EV 快照：欧共识腿走 corpus（corpus 段概率非 None）。"""
    fixture_id = seed_on_sale_fixture(db)
    duck_con = seed_corpus_duck()
    legs = [
        LegInput(
            fixture_id=fixture_id,
            market_code="had",
            selection_code="h",
            locked_odds=2.2,
        )
    ]
    snap = build_ev_snapshot(db, legs, as_of=AS_OF, duck_con=duck_con)
    assert snap.prob_consensus is not None
    # corpus 段（无 legacy 欧行）共识概率出数 = 接线分水岭
    legacy_snap = build_ev_snapshot(db, legs, as_of=AS_OF, duck_con=None)
    assert legacy_snap.prob_consensus is None


def test_validate_had_selections_corpus_path(db: sqlite3.Connection) -> None:
    """下注校验：corpus 行源下 EU 段原因不再是 eu_no_quote（JC 门独立）。"""
    fixture_id = seed_on_sale_fixture(db)
    duck_con = seed_corpus_duck()
    legs = [
        LegInput(
            fixture_id=fixture_id,
            market_code="had",
            selection_code="h",
            locked_odds=2.2,
        )
    ]
    # corpus 行源下校验通过（不抛）= EU 段可判定；JC 门（300s）独立于此
    validate_had_selections(db, legs, as_of=AS_OF, duck_con=duck_con)


def _haircut_rows(db: sqlite3.Connection, fixture_id: int) -> Any:
    return db.execute(
        "SELECT 1 FROM odds_snapshots WHERE fixture_id = ?", (fixture_id,)
    ).fetchone()


def test_haircut_corpus_era_and_legacy_pinned(db: sqlite3.Connection) -> None:
    """分代：corpus 段（bd≥9-25 且有链）配对走 corpus；历史段钉死 legacy。"""
    new_fid = seed_on_sale_fixture(db, business_date="2026-10-31")
    old_fid = seed_on_sale_fixture(
        db,
        business_date="2026-09-12",
        linked=False,
        kickoff="2026-09-12T19:00:00+00:00",
        jc_at="2026-09-12T15:02:00+00:00",
    )
    # 历史段 fixture 补 odds_api 欧赔行（legacy 口径原料）
    for book, h, d, a in (("odds_api:pinnacle", 2.0, 3.5, 3.2),):
        for sel, odds in (("h", h), ("d", d), ("a", a)):
            fx_store.insert_odds_snapshot(
                db,
                SnapshotInput(
                    fixture_id=old_fid,
                    market_code="had",
                    selection_code=sel,
                    source=book,
                    odds=odds,
                    captured_at="2026-09-12T15:00:00+00:00",
                    observed_at="2026-09-12T15:00:00+00:00",
                ),
            )
    duck_con = seed_corpus_duck()
    samples = hc.build_haircut_samples(db, duck_con=duck_con)
    by_fid = {s.fixture_id: s for s in samples}
    assert new_fid in by_fid  # corpus 段出样本
    assert set(by_fid[new_fid].eu_books) == {ANCHOR, "srct:1x2:281"}
    assert old_fid in by_fid  # legacy 段样本仍在
    assert set(by_fid[old_fid].eu_books) == {"odds_api:pinnacle"}  # 口径钉死
    # 无 duck_con：corpus 段场无欧行 → 不出样本（分代不混）
    samples_no_duck = hc.build_haircut_samples(db)
    assert new_fid not in {s.fixture_id for s in samples_no_duck}
