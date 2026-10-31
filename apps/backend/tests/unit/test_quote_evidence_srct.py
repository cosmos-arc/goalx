"""源T 欧赔读侧桥测试（odds-read-switch 票 02：corpus 行源/锚切换/降级）。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import duckdb

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import quote_evidence

BEIJING = timezone(timedelta(hours=8))
# as_of = 北京 10-31 09:00；观测 08:30 北京（=00:30Z）在窗内
AS_OF = "2026-10-31T01:00:00+00:00"
OBSERVED = datetime(2026, 10, 31, 8, 30, tzinfo=BEIJING)
KICKOFF_UTC = "2026-10-31T12:30:00+00:00"
ANCHOR = quote_evidence.SRCT_PINNACLE_BOOK


def seed_corpus(
    events: list[tuple[str, str, datetime, datetime, float, float, float]],
    book_counts: dict[str, int] | None = None,
) -> duckdb.DuckDBPyConnection:
    """内存 corpus：odds_change_event + bookmaker 维度最小投影。"""
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE odds_change_event (
            sid VARCHAR, bookmaker_id VARCHAR, market VARCHAR,
            published_at TIMESTAMPTZ, observed_at TIMESTAMPTZ,
            odds_home DOUBLE, odds_draw DOUBLE, odds_away DOUBLE
        )
        """
    )
    con.execute("CREATE TABLE bookmaker (bookmaker_id VARCHAR, match_count BIGINT)")
    for sid, book, published, observed, h, d, a in events:
        con.execute(
            "INSERT INTO odds_change_event VALUES (?, ?, '1x2', ?, ?, ?, ?, ?)",
            [sid, book, published, observed, h, d, a],
        )
    for book, count in (book_counts or {}).items():
        con.execute("INSERT INTO bookmaker VALUES (?, ?)", [book, count])
    return con


def seed_linked_fixture(db: sqlite3.Connection, *, sid: str = "3001") -> int:
    """竞彩场 + srct 已链（票 01 链路面）。"""
    competition = fx_store.upsert_competition(db, "英超")
    home = fx_store.upsert_team(db, "主队英超")
    away = fx_store.upsert_team(db, "客队英超")
    fixture_id = fx_store.upsert_fixture(db, competition, KICKOFF_UTC, home, away)
    db.execute(
        "INSERT INTO source_match_links"
        " (fixture_id, source, source_match_id, method, status, meta, mapped_at)"
        " VALUES (?, 'srct', ?, 'primary_key', 'linked', '{}',"
        " '2026-10-31T00:00:00+00:00')",
        (fixture_id, sid),
    )
    return fixture_id


def _event(
    book: str,
    published: datetime,
    h: float,
    d: float,
    a: float,
    *,
    sid: str = "3001",
    observed: datetime = OBSERVED,
) -> tuple[str, str, datetime, datetime, float, float, float]:
    return (sid, book, published, observed, h, d, a)


def test_books_complete_asof_via_corpus_bridge(db: sqlite3.Connection) -> None:
    """corpus 桥：有链走源T，逐书 published 最新、UTC iso 时间、伪行三向完整。"""
    fixture_id = seed_linked_fixture(db)
    duck_con = seed_corpus(
        [
            _event(
                "srct:1x2:281",
                datetime(2026, 10, 30, 20, 0, tzinfo=BEIJING),
                2.0,
                3.5,
                3.2,
            ),
            # 同书更晚调价 → 该笔胜出
            _event(
                "srct:1x2:281",
                datetime(2026, 10, 30, 22, 0, tzinfo=BEIJING),
                2.1,
                3.4,
                3.3,
            ),
            _event(
                ANCHOR, datetime(2026, 10, 30, 21, 0, tzinfo=BEIJING), 2.05, 3.45, 3.25
            ),
        ]
    )
    books = quote_evidence.books_complete_asof(
        db, fixture_id, AS_OF, duck_con=duck_con, sid="3001"
    )
    assert set(books) == {"srct:1x2:281", ANCHOR}
    assert books["srct:1x2:281"] == {"h": 2.1, "d": 3.4, "a": 3.3}
    # 时间统一 UTC iso（北京 08:30 → 00:30Z），字符串比较与 as_of 兼容
    rows = quote_evidence.srct_snapshot_rows_asof(duck_con, "3001", AS_OF)
    assert all(r["observed_at"] == "2026-10-31T00:30:00+00:00" for r in rows)
    assert all(r["source_updated_at"].endswith("+00:00") for r in rows)


def test_corpus_bridge_replay_and_age_gates(db: sqlite3.Connection) -> None:
    """重放语义：observed_at 晚于 as_of 不进决策；age 下界同口径。"""
    fixture_id = seed_linked_fixture(db)
    future = OBSERVED + timedelta(hours=2)  # 北京 10:30 > as_of 09:00
    duck_con = seed_corpus(
        [
            _event(
                "srct:1x2:281",
                datetime(2026, 10, 30, 20, 0, tzinfo=BEIJING),
                2.0,
                3.5,
                3.2,
            ),
            _event(
                "srct:1x2:500",
                datetime(2026, 10, 30, 20, 0, tzinfo=BEIJING),
                2.2,
                3.3,
                3.1,
                observed=future,
            ),
        ]
    )
    books = quote_evidence.books_complete_asof(
        db, fixture_id, AS_OF, duck_con=duck_con, sid="3001"
    )
    assert set(books) == {"srct:1x2:281"}  # 未来观测行被拒
    aged = quote_evidence.books_complete_asof(
        db, fixture_id, AS_OF, max_age_seconds=600.0, duck_con=duck_con
    )
    assert aged == {}  # 唯一在窗书 00:30Z 距 as_of 01:00Z 1800s > 600s


def test_corpus_bridge_null_price_dropped_and_book_cap(db: sqlite3.Connection) -> None:
    """三向缺价书剔除；书集超上限按维度覆盖 top-N 截断、主锚豁免。"""
    fixture_id = seed_linked_fixture(db)
    events = [
        _event(ANCHOR, datetime(2026, 10, 30, 20, 0, tzinfo=BEIJING), 2.0, 3.5, 3.2),
        # 缺客胜价 → 整书剔除
        _event(
            "srct:1x2:999",
            datetime(2026, 10, 30, 20, 0, tzinfo=BEIJING),
            2.0,
            3.5,
            None,
        ),
    ]
    counts = {ANCHOR: 100}
    for i in range(20):  # 20 本普通书 + 主锚 = 21 本 > cap 12
        book = f"srct:1x2:{100 + i}"
        events.append(
            _event(book, datetime(2026, 10, 30, 20, 0, tzinfo=BEIJING), 2.0, 3.5, 3.2)
        )
        counts[book] = 1000 - i  # 覆盖数递减 → top-11 = 100..110
    duck_con = seed_corpus(events, counts)
    books = quote_evidence.books_complete_asof(
        db, fixture_id, AS_OF, duck_con=duck_con, sid="3001"
    )
    assert len(books) == quote_evidence.SRCT_EU_BOOK_CAP
    assert "srct:1x2:999" not in books  # 缺价书出局
    assert ANCHOR in books  # 主锚覆盖最低仍豁免截断
    assert "srct:1x2:100" in books
    assert "srct:1x2:110" in books
    assert "srct:1x2:111" not in books  # 第 12 名普通书之外截断


def test_sharp_anchor_prefers_srct_cid177(db: sqlite3.Connection) -> None:
    """陈盘/账本锚：corpus 路径主锚 = cid177，身份串用书 id（双端同法可比）。"""
    fixture_id = seed_linked_fixture(db)
    duck_con = seed_corpus(
        [
            _event(
                ANCHOR, datetime(2026, 10, 30, 21, 0, tzinfo=BEIJING), 2.0, 3.5, 3.2
            ),
            _event(
                "srct:1x2:281",
                datetime(2026, 10, 30, 20, 0, tzinfo=BEIJING),
                2.4,
                3.3,
                2.9,
            ),
        ]
    )
    sharp = quote_evidence.sharp_probs_asof(
        db, fixture_id, AS_OF, duck_con=duck_con, sid="3001"
    )
    assert sharp is not None
    probs, identity = sharp
    assert identity == ANCHOR
    assert abs(sum(probs.values()) - 1.0) < 1e-9  # Shin 去水归一
    anchor_only = quote_evidence.sharp_probs_asof(
        db, fixture_id, AS_OF, anchor_only=True, duck_con=duck_con, sid="3001"
    )
    assert anchor_only is not None
    assert anchor_only[1] == ANCHOR


def test_unlinked_fixture_falls_back_to_legacy(db: sqlite3.Connection) -> None:
    """无链场走 legacy odds_api（单测/演示/历史口径）；有链即 corpus 唯一真值。"""
    competition = fx_store.upsert_competition(db, "英超")
    home = fx_store.upsert_team(db, "主队英超")
    away = fx_store.upsert_team(db, "客队英超")
    fixture_id = fx_store.upsert_fixture(db, competition, KICKOFF_UTC, home, away)
    for sel in ("h", "d", "a"):
        fx_store.insert_odds_snapshot(
            db, _snap(fixture_id, sel, "odds_api:pinnacle", 2.0 if sel == "h" else 3.5)
        )
    duck_con = seed_corpus([])  # corpus 在但无链 → 调用方解析得 sid=None
    books = quote_evidence.books_complete_asof(
        db, fixture_id, AS_OF, duck_con=duck_con, sid=None
    )
    assert set(books) == {"odds_api:pinnacle"}


def _snap(fixture_id: int, sel: str, source: str, odds: float) -> Any:
    from goalx_backend.models import SnapshotInput

    return SnapshotInput(
        fixture_id=fixture_id,
        market_code="had",
        selection_code=sel,
        source=source,
        odds=odds,
        captured_at="2026-10-30T20:00:00+00:00",
        observed_at="2026-10-30T20:00:00+00:00",
    )
