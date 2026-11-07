"""票 07 F1：PIT 报价读取 seam——bind_quote_reads 四法 reader 等价性 +
per_book_window 配对窗语义（haircut 私有副本收编的逐字节钉死）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import duckdb
import pytest

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import mapping, quote_evidence
from goalx_backend.evaluation import haircut as hc
from goalx_backend.models import SnapshotInput

BEIJING = timezone(timedelta(hours=8))
ANCHOR = quote_evidence.SRCT_PINNACLE_BOOK
AS_OF = "2026-10-31T01:00:00+00:00"
OBSERVED = datetime(2026, 10, 31, 8, 52, tzinfo=BEIJING)  # 00:52Z
KICKOFF_UTC = "2026-10-31T12:30:00+00:00"


def _seed_corpus_duck() -> duckdb.DuckDBPyConnection:
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
    for book, h, d, a in ((ANCHOR, 2.0, 3.5, 3.2), ("srct:1x2:281", 2.05, 3.45, 3.25)):
        con.execute(
            "INSERT INTO odds_change_event VALUES ('3001', ?, '1x2', ?, ?, ?, ?, ?, 1)",
            [book, OBSERVED - timedelta(hours=2), OBSERVED, h, d, a],
        )
    con.execute("INSERT INTO bookmaker VALUES (?, 100, NULL)", [ANCHOR])
    con.execute("INSERT INTO bookmaker VALUES ('srct:1x2:281', 90, NULL)")
    return con


def _seed_fixture(db: sqlite3.Connection) -> int:
    competition = fx_store.upsert_competition(db, "英超")
    home = fx_store.upsert_team(db, "主队")
    away = fx_store.upsert_team(db, "客队")
    fixture_id = fx_store.upsert_fixture(db, competition, KICKOFF_UTC, home, away)
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
                captured_at="2026-10-31T00:55:00+00:00",
                observed_at="2026-10-31T00:55:00+00:00",
                source_updated_at="2026-10-31T00:55:00+00:00",
            ),
        )
    db.execute(
        "INSERT INTO source_match_links"
        " (fixture_id, source, source_match_id, method, status, meta, mapped_at)"
        " VALUES (?, 'srct', '3001', 'primary_key', 'linked', '{}',"
        " '2026-10-30T00:00:00+00:00')",
        (fixture_id,),
    )
    db.commit()
    return fixture_id


def test_reader_equivalence_with_five_param_protocol(
    db: sqlite3.Connection,
) -> None:
    """reader 四法 ≡ 显式 (conn, fid, as_of, duck_con=, sid=) 五参协议
    （corpus 与 legacy 两域）——收口是纯移动，分派语义零变化。"""
    fixture_id = _seed_fixture(db)
    duck_con = _seed_corpus_duck()
    sid = mapping.srct_sid_for_fixture(db, fixture_id)
    assert sid == "3001"
    reader = quote_evidence.bind_quote_reads(
        db, duck_con, lambda fid: mapping.srct_sid_for_fixture(db, fid)
    )
    verdict = reader.adjudicate_had_quote(fixture_id, AS_OF)
    direct = quote_evidence.adjudicate_had_quote(
        db, fixture_id, AS_OF, duck_con=duck_con, sid=sid
    )
    assert verdict == direct

    assert reader.sharp_probs_asof(
        fixture_id, AS_OF
    ) == quote_evidence.sharp_probs_asof(
        db, fixture_id, AS_OF, duck_con=duck_con, sid=sid
    )
    assert reader.eu_consensus_asof(fixture_id, AS_OF) == (
        quote_evidence.eu_consensus_asof(
            db, fixture_id, AS_OF, duck_con=duck_con, sid=sid
        )
    )
    assert reader.books_complete_asof(fixture_id, AS_OF) == (
        quote_evidence.books_complete_asof(
            db, fixture_id, AS_OF, duck_con=duck_con, sid=sid
        )
    )

    # legacy 域（无 duck）：resolve_sid 缺省 None=恒 legacy
    legacy = quote_evidence.bind_quote_reads(db, None, None)
    assert legacy.sharp_probs_asof(fixture_id, AS_OF) == (
        quote_evidence.sharp_probs_asof(db, fixture_id, AS_OF)
    )


def test_per_book_window_matches_pair_semantics(db: sqlite3.Connection) -> None:
    """per_book_window=True ≡ haircut 旧私有 _books_asof 的配对窗语义：
    先选各 book 各选项最新行、再按入选行**最大**观测时刻验窗——出窗
    老行入选不连坐（per_row 逐行截断会回退窗内旧值/拆三向，分水岭
    用双钟分离行钉死两代差）。"""
    fixture_id = fx_store.upsert_fixture(
        db,
        fx_store.upsert_competition(db, "英超"),
        KICKOFF_UTC,
        fx_store.upsert_team(db, "主队"),
        fx_store.upsert_team(db, "客队"),
    )
    rows: list[dict[str, Any]] = []
    # bookA 分水岭：a 的最新行 captured 最新但 observed 出窗（双钟分离），
    # 窗内另有更早 a 行——per_row 逐行截断回退窗内旧值 3.3；per_book 按入选
    # 行最大观测（h/d 新鲜）放行整书、a 用最新行 3.9（配对口径=haircut）
    for sel, odds, minutes in (("h", 2.1, 5), ("d", 3.4, 5), ("a", 3.3, 5)):
        rows.append(_row("bookA", sel, odds, AS_OF, minutes_before=minutes))
    rows.append(
        _row("bookA", "a", 3.9, AS_OF, minutes_before=600, captured_minutes_before=1)
    )
    # bookB 不对称剔除场：d/a 最新行出窗且无窗内替补、h 新鲜——
    # per_row 截断后三向不全整书剔除；per_book 最大观测（h）放行整书
    rows.append(_row("bookB", "h", 2.2, AS_OF, minutes_before=5))
    for sel, odds in (("d", 3.3), ("a", 3.2)):
        rows.append(
            _row(
                "bookB", sel, odds, AS_OF, minutes_before=600, captured_minutes_before=2
            )
        )
    # bookC 全新鲜：两语义同存活同值（对照组）
    for sel, odds in (("h", 2.2), ("d", 3.3), ("a", 3.2)):
        rows.append(_row("bookC", sel, odds, AS_OF, minutes_before=5))

    per_book = quote_evidence.books_complete_asof(
        db, fixture_id, AS_OF, max_age_seconds=300, rows=rows, per_book_window=True
    )
    per_row = quote_evidence.books_complete_asof(
        db, fixture_id, AS_OF, max_age_seconds=300, rows=rows
    )
    # 配对语义：A/B/C 全存活——a 取最新行 3.9、B 整书靠新鲜 h 放行
    assert per_book == {
        "bookA": {"h": 2.1, "d": 3.4, "a": 3.9},
        "bookB": {"h": 2.2, "d": 3.3, "a": 3.2},
        "bookC": {"h": 2.2, "d": 3.3, "a": 3.2},
    }
    # 逐行语义：A 回退窗内旧值、B 三向不全剔除
    assert per_row == {
        "bookA": {"h": 2.1, "d": 3.4, "a": 3.3},
        "bookC": {"h": 2.2, "d": 3.3, "a": 3.2},
    }


_ROW_ID = iter(range(1, 10_000))


def _row(
    book: str,
    sel: str,
    odds: float,
    as_of: str,
    *,
    minutes_before: int,
    captured_minutes_before: int | None = None,
) -> dict[str, Any]:
    """合成行：observed 窗判钟与 captured 选序钟可分离（分水岭场需要）。"""
    observed = datetime.fromisoformat(as_of) - timedelta(minutes=minutes_before)
    captured = datetime.fromisoformat(as_as := as_of) - timedelta(
        minutes=minutes_before
        if captured_minutes_before is None
        else captured_minutes_before
    )
    del as_as
    return {
        "id": next(_ROW_ID),
        "source": book,
        "selection_code": sel,
        "odds": odds,
        "captured_at": captured.isoformat(),
        "observed_at": observed.isoformat(),
        "source_updated_at": observed.isoformat(),
    }


def test_haircut_fork_collapse_output_pinned(db: sqlite3.Connection) -> None:
    """收编后 haircut 样本 eu_books 与直接 per_book 窗查一致（历史校准
    口径不变量：样本三向/书集逐字节）。"""
    fixture_id = _seed_fixture(db)
    duck_con = _seed_corpus_duck()
    samples = hc.build_haircut_samples(db, duck_con=duck_con)
    by_fid = {s.fixture_id: s for s in samples}
    assert fixture_id in by_fid
    books = by_fid[fixture_id].eu_books
    assert set(books) == {ANCHOR, "srct:1x2:281"}
    # 与 per_book 窗查直接调用同书集同值（配对时点=JC 最新 captured_at）
    direct = quote_evidence.books_complete_asof(
        db,
        fixture_id,
        by_fid[fixture_id].jc_captured_at,
        max_age_seconds=hc.MAX_PAIR_GAP_SECONDS,
        duck_con=duck_con,
        sid="3001",
        per_book_window=True,
    )
    assert books == direct


@pytest.mark.parametrize("method", ["adjudicate", "sharp", "consensus", "books"])
def test_reader_resolve_sid_injection(db: sqlite3.Connection, method: str) -> None:
    """sid 解析注入缝：resolver 返回 None → 恒 legacy；返回链 → corpus。
    （分层契约：quote_evidence 本体不知道 mapping。）"""
    fixture_id = _seed_fixture(db)
    duck_con = _seed_corpus_duck()
    corpus_reader = quote_evidence.bind_quote_reads(db, duck_con, lambda _fid: "3001")
    legacy_reader = quote_evidence.bind_quote_reads(db, duck_con, lambda _fid: None)
    if method == "adjudicate":
        got_corpus = corpus_reader.adjudicate_had_quote(fixture_id, AS_OF)
        got_legacy = legacy_reader.adjudicate_had_quote(fixture_id, AS_OF)
        # 两域各自完整判定；EU 书数是分派指纹（corpus 双书 vs legacy 零书）
        assert got_corpus.status == "valid"
        assert got_corpus.eu_books == 2
        assert got_legacy.eu_books == 0
    elif method == "sharp":
        assert corpus_reader.sharp_probs_asof(fixture_id, AS_OF) is not None
        assert legacy_reader.sharp_probs_asof(fixture_id, AS_OF) is None
    elif method == "consensus":
        assert corpus_reader.eu_consensus_asof(fixture_id, AS_OF) is not None
        assert legacy_reader.eu_consensus_asof(fixture_id, AS_OF) is None
    else:
        assert set(corpus_reader.books_complete_asof(fixture_id, AS_OF)) == {
            ANCHOR,
            "srct:1x2:281",
        }
        assert legacy_reader.books_complete_asof(fixture_id, AS_OF) == {}


def test_reader_per_book_window_corpus_path(db: sqlite3.Connection) -> None:
    """correctness info①：reader.books_complete_asof(per_book_window=True)
    走 corpus 行源（无 rows 注入）——SQL 级逐行年龄过滤须被抑制（否则
    截断发生在选行前，破坏配对语义），窗判定后置到书级。"""
    fixture_id = _seed_fixture(db)
    duck_con = _seed_corpus_duck()
    reader = quote_evidence.bind_quote_reads(db, duck_con, lambda _fid: "3001")
    # 双钟分离：锚书行 captured 最新、observed 出窗；窗内另有旧行——
    # per_book 语义应整书剔除（配对完整性），per_row 会回退窗内旧行
    stale_obs = OBSERVED - timedelta(minutes=600)
    duck_con.execute(
        "INSERT INTO odds_change_event VALUES"
        " ('3001', ?, '1x2', ?, ?, 2.2, 3.4, 3.3, 1)",
        [ANCHOR, OBSERVED, stale_obs],
    )
    # 窗=3600s（种子行龄 480s 在窗内；分水岭行 600min=10h 出窗）
    per_book = reader.books_complete_asof(
        fixture_id, AS_OF, max_age_seconds=3600, per_book_window=True
    )
    per_row = reader.books_complete_asof(fixture_id, AS_OF, max_age_seconds=3600)
    # 分水岭双钟行成了锚书三选的入选最新（published 新）→ 其 observed 出窗
    # 连坐整书剔除（配对完整性）；新鲜书 281 不受连坐
    assert ANCHOR not in per_book
    assert set(per_book) == {"srct:1x2:281"}
    # 逐行语义：SQL 级年龄过滤截掉分水岭行 → 回退窗内原行，锚书存活
    assert set(per_row) == {ANCHOR, "srct:1x2:281"}
