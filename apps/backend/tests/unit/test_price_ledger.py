"""同步报价账本（measure-07，A 线仪器）的行为断言。"""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import price_ledger as pl
from goalx_backend.models import MatchCodeInput, SaleStatusInput, SnapshotInput, Tier

OBSERVED = "2026-10-16T14:03:00+00:00"
ANCHOR_TIME = "2026-10-16T14:00:00+00:00"


def _seed(db: sqlite3.Connection, n: int = 0) -> int:
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"主{n}")
    away = fx_store.upsert_team(db, f"客{n}")
    fixture = fx_store.upsert_fixture(
        db, competition, "2026-10-17T18:00:00+00:00", home, away
    )
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date="2026-10-16",
            code=f"周六{n:03d}",
        ),
    )
    db.commit()
    return fixture


def _add_anchor(
    db: sqlite3.Connection, fixture: int, odds: dict[str, float], at: str = ANCHOR_TIME
) -> None:
    for sel, o in odds.items():
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="odds_api:pinnacle",
                odds=o,
                captured_at=at,
                observed_at=at,
                source_updated_at=at,
            ),
        )
    db.commit()


def _jc_snapshot(
    db: sqlite3.Connection, fixture: int, odds: float, sel: str = "h"
) -> int:
    sid = fx_store.insert_odds_snapshot(
        db,
        SnapshotInput(
            fixture_id=fixture,
            market_code="had",
            selection_code=sel,
            source="sporttery",
            odds=odds,
            captured_at=OBSERVED,
            observed_at=OBSERVED,
            source_updated_at=OBSERVED,
        ),
    )
    db.commit()
    assert sid is not None
    return sid


def test_ledger_pairs_reference_and_sale(db: sqlite3.Connection) -> None:
    """JC 新观测同刻配对：参考锚 as-of + 销售态/单关资格落行。"""
    fixture = _seed(db)
    _add_anchor(db, fixture, {"h": 2.0, "d": 3.4, "a": 4.0})
    fx_store.append_sale_status(
        db,
        SaleStatusInput(
            fixture_id=fixture,
            market_code="had",
            sale_state="on_sale",
            single_eligible=True,
            observed_at=OBSERVED,
        ),
    )
    db.commit()
    sid = _jc_snapshot(db, fixture, 3.0)
    assert pl.record_jc_observation(
        db,
        SnapshotInput(
            fixture_id=fixture,
            market_code="had",
            selection_code="h",
            source="sporttery",
            odds=3.0,
            captured_at=OBSERVED,
            observed_at=OBSERVED,
            source_updated_at=OBSERVED,
        ),
        snapshot_id=sid,
    )
    row = db.execute(
        "SELECT jc_odds, p_sharp_h, anchor_ref, sale_state, single_eligible,"
        " reference_backfilled_at FROM price_deviation_ledger WHERE snapshot_id=?",
        (sid,),
    ).fetchone()
    assert row["jc_odds"] == 3.0
    assert row["p_sharp_h"] is not None
    assert row["anchor_ref"] is not None
    assert row["sale_state"] == "on_sale"
    assert row["single_eligible"] == 1
    assert row["reference_backfilled_at"] is None


def test_ledger_missing_reference_backfilled_by_sweep(db: sqlite3.Connection) -> None:
    """参考缺=诚实 NULL；锚晚落库后日终 sweep 按 observed_at 回填。"""
    fixture = _seed(db, 1)
    sid = _jc_snapshot(db, fixture, 2.5)
    assert pl.record_jc_observation(
        db,
        SnapshotInput(
            fixture_id=fixture,
            market_code="had",
            selection_code="h",
            source="sporttery",
            odds=2.5,
            captured_at=OBSERVED,
            observed_at=OBSERVED,
            source_updated_at=OBSERVED,
        ),
        snapshot_id=sid,
    )
    row = db.execute(
        "SELECT p_sharp_h FROM price_deviation_ledger WHERE snapshot_id=?", (sid,)
    ).fetchone()
    assert row["p_sharp_h"] is None  # 当时无锚
    # 锚观测早于 JC observed 但落库晚（拉取竞争）→ as-of 回填可用
    _add_anchor(db, fixture, {"h": 2.0, "d": 3.4, "a": 4.0}, at=ANCHOR_TIME)
    assert pl.backfill_missing_reference(db) == 1
    row2 = db.execute(
        "SELECT p_sharp_h, anchor_ref, reference_backfilled_at"
        " FROM price_deviation_ledger WHERE snapshot_id=?",
        (sid,),
    ).fetchone()
    assert row2["p_sharp_h"] is not None
    assert row2["reference_backfilled_at"] is not None
    assert pl.backfill_missing_reference(db) == 0  # 已填不重算
    assert pl.ledger_summary(db) == {
        "rows": 1,
        "missing_reference": 0,
        "reference_backfilled": 1,
    }


def test_ledger_append_only_and_unique(db: sqlite3.Connection) -> None:
    """append-only：行不可删、结构列不可改（参考回填列放行）；快照唯一。"""
    fixture = _seed(db, 2)
    sid = _jc_snapshot(db, fixture, 2.2)
    assert pl.record_jc_observation(
        db,
        SnapshotInput(
            fixture_id=fixture,
            market_code="had",
            selection_code="h",
            source="sporttery",
            odds=2.2,
            captured_at=OBSERVED,
            observed_at=OBSERVED,
            source_updated_at=OBSERVED,
        ),
        snapshot_id=sid,
    )
    # 同快照再记：UNIQUE 吸收
    assert not pl.record_jc_observation(
        db,
        SnapshotInput(
            fixture_id=fixture,
            market_code="had",
            selection_code="h",
            source="sporttery",
            odds=2.2,
            captured_at=OBSERVED,
            observed_at=OBSERVED,
            source_updated_at=OBSERVED,
        ),
        snapshot_id=sid,
    )
    assert db.execute("SELECT COUNT(*) FROM price_deviation_ledger").fetchone()[0] == 1
    row_id = db.execute("SELECT id FROM price_deviation_ledger").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM price_deviation_ledger WHERE id=?", (row_id,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE price_deviation_ledger SET jc_odds = 9.9 WHERE id=?", (row_id,)
        )
    # 参考回填列放行（sweep 路径：NULL→值）
    db.execute(
        "UPDATE price_deviation_ledger SET p_sharp_h = 0.5,"
        " reference_backfilled_at = '2026-10-17T00:00:00+00:00' WHERE id=?",
        (row_id,),
    )
    db.commit()
    # 已填参考写一次：覆写拦截（三轴 W2）
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE price_deviation_ledger SET p_sharp_h = 0.9 WHERE id=?",
            (row_id,),
        )


def test_ledger_anchor_only_no_consensus_fallback(db: sqlite3.Connection) -> None:
    """单锚不变量（三轴 W3）：无 pinnacle 时共识书不兜底，NULL 留 sweep。"""
    fixture = _seed(db, 3)
    for sel, o in {"h": 2.1, "d": 3.3, "a": 3.5}.items():
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="odds_api:avg_eu",
                odds=o,
                captured_at=ANCHOR_TIME,
                observed_at=ANCHOR_TIME,
                source_updated_at=ANCHOR_TIME,
            ),
        )
    db.commit()
    sid = _jc_snapshot(db, fixture, 2.5)
    assert pl.record_jc_observation(
        db,
        SnapshotInput(
            fixture_id=fixture,
            market_code="had",
            selection_code="h",
            source="sporttery",
            odds=2.5,
            captured_at=OBSERVED,
            observed_at=OBSERVED,
            source_updated_at=OBSERVED,
        ),
        snapshot_id=sid,
    )
    row = db.execute(
        "SELECT p_sharp_h, anchor_ref FROM price_deviation_ledger WHERE snapshot_id=?",
        (sid,),
    ).fetchone()
    assert row["p_sharp_h"] is None  # 欧共识在场也不兜底
    assert row["anchor_ref"] is None
    assert pl.backfill_missing_reference(db) == 0  # sweep 同样只认主锚


def test_backfill_via_srct_corpus_asof(db) -> None:
    """源T as-of 回填：语料桥在场走主链（sid 链→as-of 三元组→Shin）。"""
    import duckdb as ddb

    fixture = _seed(db, 5)
    sid = _jc_snapshot(db, fixture, 2.5)
    assert pl.record_jc_observation(
        db,
        SnapshotInput(
            fixture_id=fixture,
            market_code="had",
            selection_code="h",
            source="sporttery",
            odds=2.5,
            captured_at=OBSERVED,
            observed_at=OBSERVED,
            source_updated_at=OBSERVED,
        ),
        snapshot_id=sid,
    )
    # 物化链 sid（票 77）
    db.execute(
        "INSERT INTO source_match_links (fixture_id, source, source_match_id,"
        " method, status, mapped_at) VALUES (?, 'srct', 's-srct-1',"
        " 'manual', 'linked', '2026-10-16T00:00:00+00:00')",
        (fixture,),
    )
    db.commit()
    # 语料桥：odds_change_event 两条（JC 观测前后各一）→ as-of 取 ≤ 观测的最新
    duck = ddb.connect()
    duck.execute(
        "CREATE TABLE odds_change_event (sid VARCHAR, bookmaker_id VARCHAR,"
        " published_at TIMESTAMP, odds_home DOUBLE, odds_draw DOUBLE, odds_away DOUBLE)"
    )
    duck.execute(
        "INSERT INTO odds_change_event VALUES ('s-srct-1', 'srct:1x2:177',"
        " '2026-10-16 13:00:00', 2.20, 3.40, 3.60)"
    )
    duck.execute(
        "INSERT INTO odds_change_event VALUES ('s-srct-1', 'srct:1x2:177',"
        " '2026-10-16 14:02:00', 2.05, 3.45, 3.80)"
    )
    duck.execute(
        "INSERT INTO odds_change_event VALUES ('s-srct-1', 'srct:1x2:177',"
        " '2026-10-16 15:00:00', 1.90, 3.50, 4.00)"
    )
    assert pl.backfill_missing_reference(db, duck) == 1
    row = db.execute(
        "SELECT p_sharp_h, p_sharp_d, p_sharp_a, anchor_ref"
        " FROM price_deviation_ledger WHERE snapshot_id=?",
        (sid,),
    ).fetchone()
    assert row["anchor_ref"] == "srct_1x2_asof"
    # 14:02 那笔（≤14:03 最新），非 15:00 的盘后笔——as-of 无前视
    assert row["p_sharp_h"] > row["p_sharp_a"]  # 2.05 低赔侧概率高
    assert 0.4 < row["p_sharp_h"] < 0.6
    duck.close()
