"""票 07 首页无候选六桶归因：映射表黄金样例 + 聚合不变量。"""

from __future__ import annotations

import sqlite3

from goalx_backend.data.today import (
    HadQuoteStatus,
    SelectionTriple,
    TodayFixtureView,
    no_candidate_attribution,
)


def _row(
    fixture_id: int,
    status: str = "valid",
    reasons: list[str] | None = None,
    ev: tuple[float | None, float | None, float | None] | None = None,
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
        jc_odds=SelectionTriple(h=2.0, d=3.0, a=3.5),
        had_quote=HadQuoteStatus(
            as_of="2026-11-04T10:00:00+00:00",
            status=status,
            reasons=reasons or [],
        ),
        ev=SelectionTriple(h=ev[0], d=ev[1], a=ev[2]) if ev else None,
    )


def _attribution(db: sqlite3.Connection, rows: list[TodayFixtureView]):
    return no_candidate_attribution(
        db, rows, "2026-11-04", as_of="2026-11-04T10:00:00+00:00"
    )


def _bucket(db: sqlite3.Connection, rows: list[TodayFixtureView], key: str):
    result = _attribution(db, rows)
    if key in ("window_closed", "positive_candidates"):
        return getattr(result, key)
    return next(bucket for bucket in result.buckets if bucket.key == key)


def test_bucket_mapping_matches_verdict_reasons(db: sqlite3.Connection) -> None:
    """票 07 映射表黄金样例：每种裁决原因各进其桶（一一对应）。"""
    cases: list[tuple[str, str, list[str]]] = [
        ("not_on_sale", "rejected", ["sale_not_on_sale"]),
        ("not_on_sale", "rejected", ["sale_window_closed"]),
        ("not_collected", "unknown", ["jc_no_quote"]),
        ("not_collected", "unknown", ["sale_status_unknown"]),
        ("eu_cut", "unknown", ["eu_no_quote"]),
        ("eu_cut", "unknown", ["eu_no_valid_books"]),
        ("eu_cut", "unknown", ["eu_observed_at_unknown"]),
        (
            "evidence_unqualified",
            "rejected",
            ["stale_source"],
        ),
        ("evidence_unqualified", "unknown", ["jc_three_way_incomplete"]),
    ]
    rows = [
        _row(index, status=status, reasons=reasons)
        for index, (_key, status, reasons) in enumerate(cases, start=1)
    ]
    for (key, _status, reasons), row in zip(cases, rows, strict=True):
        bucket = _bucket(db, [row], key)
        assert bucket.count == 1, f"{reasons} 应归 {key}"
        assert bucket.reasons_seen == reasons


def test_valid_rows_split_by_ev(db: sqlite3.Connection) -> None:
    """valid 场按 EV 分流：有正 EV → 候选面；全无正 EV → 无优势桶。"""
    no_edge = _row(1, ev=(-0.05, -0.02, 0.0))
    no_ev_data = _row(2)  # valid 但无共识 EV（ev=None）——信息齐无优势
    positive = _row(3, ev=(0.08, -0.05, -0.05))
    assert _bucket(db, [no_edge], "no_edge").count == 1
    assert _bucket(db, [no_ev_data], "no_edge").count == 1
    assert _bucket(db, [positive], "positive_candidates").count == 1


def test_window_closed_footnote_not_in_six_buckets(db: sqlite3.Connection) -> None:
    """已停售/已开赛入脚注面不入六桶；来不及执行桶首期恒空（人裁决）。"""
    result = _attribution(
        db,
        [
            _row(1, "rejected", ["sale_stopped"]),
            _row(2, "rejected", ["kickoff_passed"]),
        ],
    )
    assert result.window_closed.count == 2
    six_keys = {bucket.key for bucket in result.buckets}
    assert six_keys == {
        "not_on_sale",
        "not_collected",
        "eu_cut",
        "evidence_unqualified",
        "no_edge",
        "missed_deadline",
    }
    missed = next(b for b in result.buckets if b.key == "missed_deadline")
    assert missed.count == 0  # 空桶占位
    assert missed.samples == []


def test_priority_first_match_one_fixture_one_bucket(db: sqlite3.Connection) -> None:
    """首匹配优先级：一场只进一桶（停售+断供 → 脚注面，不双计）。"""
    row = _row(1, "rejected", ["sale_stopped", "eu_no_quote"])
    result = _attribution(db, [row])
    assert result.window_closed.count == 1
    assert sum(bucket.count for bucket in result.buckets) == 0
    assert result.window_closed.count + result.positive_candidates.count == 1


def test_unmapped_reason_falls_back_and_surfaces(db: sqlite3.Connection) -> None:
    """未映射新裁决原因兜底进"有价但证据不合格"并经 reasons_seen 浮出。"""
    bucket = _bucket(
        db, [_row(1, "rejected", ["future_new_reason"])], "evidence_unqualified"
    )
    assert bucket.count == 1
    assert bucket.reasons_seen == ["future_new_reason"]


def test_total_and_samples_capped(db: sqlite3.Connection) -> None:
    """total=场数；样本上限封顶但计数全量。"""
    rows = [_row(i, "rejected", ["stale_source"]) for i in range(1, 61)]
    result = _attribution(db, rows)
    assert result.total == 60
    bucket = next(b for b in result.buckets if b.key == "evidence_unqualified")
    assert bucket.count == 60
    assert len(bucket.samples) == 50  # MAX_BUCKET_SAMPLES
