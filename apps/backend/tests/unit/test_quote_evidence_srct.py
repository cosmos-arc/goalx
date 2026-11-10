"""源T 欧赔读侧桥测试（odds-read-switch 票 02：corpus 行源/锚切换/降级）。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import duckdb
import pytest

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
    book_names: dict[str, str] | None = None,
) -> duckdb.DuckDBPyConnection:
    """内存 corpus：odds_change_event + bookmaker 维度最小投影。"""
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
    con.execute(
        "CREATE TABLE bookmaker ("
        "bookmaker_id VARCHAR, match_count BIGINT, name_en VARCHAR)"
    )
    for sid, book, published, observed, h, d, a in events:
        con.execute(
            "INSERT INTO odds_change_event VALUES (?, ?, '1x2', ?, ?, ?, ?, ?, 1)",
            [sid, book, published, observed, h, d, a],
        )
    names = book_names or {}
    for book, count in (book_counts or {}).items():
        con.execute(
            "INSERT INTO bookmaker VALUES (?, ?, ?)",
            [book, count, names.get(book) or None],
        )
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
            db,
            _snap(
                fixture_id,
                sel,
                quote_evidence.SHARP_ANCHOR_SOURCE,
                2.0 if sel == "h" else 3.5,
            ),
        )
    duck_con = seed_corpus([])  # corpus 在但无链 → 调用方解析得 sid=None
    books = quote_evidence.books_complete_asof(
        db, fixture_id, AS_OF, duck_con=duck_con, sid=None
    )
    assert set(books) == {quote_evidence.SHARP_ANCHOR_SOURCE}


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


def test_adjudicate_corpus_path_age_gate_and_pair_gap_skip(
    db: sqlite3.Connection,
) -> None:
    """corpus 路径 EU 门：60min 观察龄（超龄→unknown）；pair-gap 不适用
    （published 跨日不动 + JC 五分钟前调盘 → 仍 valid，票02 裁决默认）。"""

    fixture_id = seed_linked_fixture(db)
    db.execute(
        "INSERT INTO sale_statuses (fixture_id, market_code, sale_state,"
        " single_eligible, observed_at, source_updated_at, created_at)"
        " VALUES (?, 'had', 'on_sale', 1, ?, ?, ?)",
        (
            fixture_id,
            "2026-10-31T00:00:00+00:00",
            "2026-10-31T00:00:00+00:00",
            "2026-10-31T00:00:00+00:00",
        ),
    )
    for sel in ("h", "d", "a"):  # JC 侧新鲜（as_of 01:00 前 5min）
        db.execute(
            "INSERT INTO odds_snapshots (fixture_id, market_code, selection_code,"
            " source, odds, captured_at, observed_at, source_updated_at, created_at)"
            " VALUES (?, 'had', ?, 'sporttery', 2.0, '2026-10-31T00:55:00+00:00',"
            " '2026-10-31T00:55:00+00:00', '2026-10-31T00:55:00+00:00',"
            " '2026-10-31T00:55:00+00:00')",
            (fixture_id, sel),
        )
    db.commit()
    # corpus 书：published 十日前（价格自那未动）、observed 新鲜 30min
    fresh = seed_corpus(
        [_event(ANCHOR, datetime(2026, 10, 19, 3, 0, tzinfo=BEIJING), 2.0, 3.5, 3.2)]
    )
    verdict = quote_evidence.adjudicate_had_quote(
        db, fixture_id, AS_OF, duck_con=fresh, sid="3001"
    )
    assert verdict.status == "valid", verdict.reasons
    assert verdict.eu_books == 1
    assert ANCHOR in verdict.sources  # sporttery（JC 源）同列属既有行为
    # 同书观测变 2h 前（>60min 门）→ EU 段无有效书 → unknown
    stale_obs = OBSERVED - timedelta(hours=2)
    stale = seed_corpus(
        [
            _event(
                ANCHOR,
                datetime(2026, 10, 19, 3, 0, tzinfo=BEIJING),
                2.0,
                3.5,
                3.2,
                observed=stale_obs,
            )
        ]
    )
    verdict2 = quote_evidence.adjudicate_had_quote(
        db, fixture_id, AS_OF, duck_con=stale, sid="3001"
    )
    assert verdict2.status == "unknown"


def test_today_view_survives_reader_absent(db: sqlite3.Connection) -> None:
    """degrade 验收（票02）：corpus 缺席（duck_con=None）今日页照常组装。"""
    fixture_id = seed_linked_fixture(db)
    db.execute(
        "INSERT INTO match_codes (fixture_id, kind, business_date, code)"
        " VALUES (?, 'jingcai', '2026-10-31', '周六001')",
        (fixture_id,),
    )
    db.commit()
    from goalx_backend.data.today import build_today_view

    view = build_today_view(db, "2026-10-31", duck_con=None)
    assert len(view) == 1
    assert view[0].joined is True
    assert view[0].books == 0  # EU 段空值不炸


def test_same_minute_tiebreak_by_source_order() -> None:
    """F2 回归：published/observed 并列时 source_order 大者胜（重放确定性）。"""
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE odds_change_event (sid VARCHAR, bookmaker_id VARCHAR,"
        " market VARCHAR, published_at TIMESTAMPTZ, observed_at TIMESTAMPTZ,"
        " odds_home DOUBLE, odds_draw DOUBLE, odds_away DOUBLE, source_order INTEGER)"
    )
    pub = datetime(2026, 10, 30, 20, 0, tzinfo=BEIJING)
    for order, home in ((1, 2.10), (2, 2.05)):  # 同分钟两笔，order 2 是后价
        con.execute(
            "INSERT INTO odds_change_event VALUES"
            " ('3001', 'srct:1x2:281', '1x2', ?, ?, ?, 3.5, 3.2, ?)",
            [pub, OBSERVED, home, order],
        )
    rows = quote_evidence.srct_snapshot_rows_asof(con, "3001", AS_OF)
    by_sel = {r["selection_code"]: r["odds"] for r in rows}
    assert by_sel["h"] == 2.05  # source_order 决胜，取后价


def test_book_cap_degrades_when_dim_lags(db: sqlite3.Connection) -> None:
    """F3 回归：维度未覆盖候选 → 降级保全量不塌缩；tiebreak 定确书集。"""
    fixture_id = seed_linked_fixture(db)
    events = [
        _event(ANCHOR, datetime(2026, 10, 30, 20, 0, tzinfo=BEIJING), 2.0, 3.5, 3.2)
    ]
    counts = {ANCHOR: 100}  # 维度全覆盖 → 截断生效
    for i in range(20):
        book = f"srct:1x2:{100 + i}"
        events.append(
            _event(book, datetime(2026, 10, 30, 20, 0, tzinfo=BEIJING), 2.0, 3.5, 3.2)
        )
        counts[book] = 500  # 全并列 match_count → tiebreak bookmaker_id 定序
    duck_con = seed_corpus(events, counts)
    books = quote_evidence.books_complete_asof(
        db, fixture_id, AS_OF, duck_con=duck_con, sid="3001"
    )
    assert len(books) == quote_evidence.SRCT_EU_BOOK_CAP
    # 并列 500 时 tiebreak=bookmaker_id 升序：100..110 号书 + 主锚（111 出局）
    assert "srct:1x2:100" in books
    assert "srct:1x2:110" in books
    assert "srct:1x2:111" not in books
    # 维度空表（覆盖 0/21）→ 降级保全量（先清 TTL 缓存模拟另一进程冷启）
    quote_evidence._dim_cache.counts = {}
    quote_evidence._dim_cache.at = 0.0
    empty_dim = seed_corpus(events, {})
    kept = quote_evidence.books_complete_asof(
        db, fixture_id, AS_OF, duck_con=empty_dim, sid="3001"
    )
    assert len(kept) == 21


@pytest.fixture(autouse=True)
def _reset_dim_cache() -> None:
    """维度 TTL 缓存进程级——逐测清空防跨库污染（票 05）。"""
    from goalx_backend.data.quote_evidence import _dim_cache

    _dim_cache.counts = {}
    _dim_cache.names = {}
    _dim_cache.at = 0.0


# ---- 票 19：锚书分季入库节奏观察面（晨检/周报数源） ----


def _seed_season_corpus() -> duckdb.DuckDBPyConnection:
    """内存 corpus（含 hive 分区列 season）：锚书轨迹 × 两 sid + 干扰行。"""
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE odds_change_event (
            sid VARCHAR, bookmaker_id VARCHAR, market VARCHAR, season VARCHAR,
            published_at TIMESTAMPTZ, observed_at TIMESTAMPTZ,
            odds_home DOUBLE, odds_draw DOUBLE, odds_away DOUBLE,
            source_order INTEGER
        )
        """
    )
    rows = [
        # 锚书 2627 季两场（最新观测 10:00Z）
        ("3001", ANCHOR, "1x2", "2026-27", OBSERVED, OBSERVED, 2.0, 3.5, 3.2),
        ("3002", ANCHOR, "1x2", "2026-27", OBSERVED, OBSERVED, 2.2, 3.4, 3.1),
        (
            "3002",
            ANCHOR,
            "1x2",
            "2026-27",
            OBSERVED,
            datetime(2026, 10, 31, 18, 0, tzinfo=BEIJING),  # 10:00Z 最新
            2.1,
            3.45,
            3.15,
        ),
        # 干扰：他书/他季不进锚书面
        ("3003", "srct:1x2:281", "1x2", "2026-27", OBSERVED, OBSERVED, 2.0, 3.5, 3.2),
        ("3004", ANCHOR, "1x2", "2025-26", OBSERVED, OBSERVED, 2.0, 3.5, 3.2),
    ]
    for sid, book, market, season, published, observed, h, d, a in rows:
        con.execute(
            "INSERT INTO odds_change_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            [sid, book, market, season, published, observed, h, d, a],
        )
    return con


def test_srct_anchor_season_face_counts_distinct_sids() -> None:
    """票 19 观察面：锚书分季 DISTINCT sid 计数 + 最新观测；他书/他季不进。"""
    con = _seed_season_corpus()
    face = quote_evidence.srct_anchor_season_face(con, season="2026-27")
    assert face == {
        "season": "2026-27",
        "book": ANCHOR,
        "n_sids": 2,
        "latest_observed": "2026-10-31T10:00:00+00:00",
    }


def test_srct_anchor_season_face_empty_season_is_zero_truth() -> None:
    """无行季=空真（n_sids=0、latest=None 不冒充），缺席视图=None 降级。"""
    con = _seed_season_corpus()
    face = quote_evidence.srct_anchor_season_face(con, season="2020-21")
    assert face is not None
    assert face["n_sids"] == 0
    assert face["latest_observed"] is None
    # 视图缺席（库无该表）→ None（晨检黄面），不炸不冒充
    bare = duckdb.connect(":memory:")
    assert quote_evidence.srct_anchor_season_face(bare, season="2026-27") is None


def test_book_display_names_join_and_fallbacks() -> None:
    """研究页书商直名（票 13）：字典有名→返回；空名/legacy 源不返回（前端回退 cid）。"""
    duck_con = seed_corpus(
        [],
        {ANCHOR: 100, "srct:1x2:281": 90},
        book_names={ANCHOR: "Pinnacle", "srct:1x2:281": ""},
    )
    names = quote_evidence.book_display_names(
        duck_con, [ANCHOR, "srct:1x2:281", "odds_api:pin"]
    )
    assert names == {ANCHOR: "Pinnacle"}


def test_book_display_names_degrades_without_corpus() -> None:
    """corpus 缺席（reader 派发 None）→ 空映射，前端回退 cid 展示。"""
    assert quote_evidence.book_display_names(None, [ANCHOR]) == {}
