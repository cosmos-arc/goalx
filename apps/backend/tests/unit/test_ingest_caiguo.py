"""
源D 结果页采集测试（票 42）：固定 fixture HTML 解析 + 注入 client 的同步链路。

fixture 为 2026-09-18 实测页面裁剪（scripts/styles 剥离，比赛行保留原样）：
- caiguo_2026-09-16.html.txt：16 完场（含让球/无让球/大比分）+ 1 推迟（status=0）；
- caiguo_2026-09-17.html.txt：11 完场。
测试不打真实外网——HTTP 层用 httpx.MockTransport 仿 301 → /?e= 与 GB18030 响应。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data import results as rs_store
from goalx_backend.data.fixtures import (
    upsert_competition,
    upsert_fixture,
    upsert_match_code,
    upsert_team,
)
from goalx_backend.data.ingest import caiguo
from goalx_backend.models import DrawResultInput, MatchCodeInput

FIXTURES = Path(__file__).parent.parent / "fixtures"
PAGE_0916 = (FIXTURES / "caiguo_2026-09-16.html.txt").read_text(encoding="utf-8")
PAGE_0917 = (FIXTURES / "caiguo_2026-09-17.html.txt").read_text(encoding="utf-8")
NOW = datetime(2026, 9, 18, 5, 30, tzinfo=UTC)  # 实测采集当日清晨


def _settings() -> Settings:
    return Settings(caiguo_base_url="https://live.500.test/jczq.php")


def _transport(pages: dict[str, str]) -> httpx.MockTransport:
    """仿真实行为：jczq.php 301 → /?e=，最终页 GB18030 编码返回。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        date = str(request.url.params.get("e", ""))
        if "jczq.php" in path:
            return httpx.Response(301, headers={"Location": "/?e=" + date})
        html = pages.get(date)
        if html is None:
            return httpx.Response(404, text="no fixture")
        return httpx.Response(200, content=html.encode("gb18030"))

    return httpx.MockTransport(handler)


def _client(pages: dict[str, str]) -> httpx.Client:
    return httpx.Client(transport=_transport(pages))


# --- 解析（纯函数） ---


def test_parse_finished_with_half_and_full_scores() -> None:
    page = caiguo.FetchedPage(business_date="2026-09-16", html=PAGE_0916, raw=b"")
    parsed = caiguo.parse_page(page)
    # 16 完场 + 周三014 推迟（status=0 → not_finished）
    assert len(parsed.finished) == 16
    assert len(parsed.skipped) == 1
    assert parsed.skipped[0].code == "周三014"
    assert parsed.skipped[0].reason == "not_finished"
    first = parsed.finished[0]
    assert first.code == "周三001"
    assert (first.home_goals, first.away_goals) == (2, 1)
    assert (first.half_home_goals, first.half_away_goals) == (1, 1)


def test_parse_all_rows_on_second_fixture_page() -> None:
    parsed = caiguo.parse_page(
        caiguo.FetchedPage(business_date="2026-09-17", html=PAGE_0917, raw=b"")
    )
    assert len(parsed.finished) == 11
    assert parsed.skipped == []
    # 半场比分分量不越界（口径自洽性）
    for row in parsed.finished:
        assert row.half_home_goals is not None
        assert row.half_away_goals is not None
        assert row.half_home_goals <= row.home_goals
        assert row.half_away_goals <= row.away_goals


def test_parse_skips_letter_mismatch_and_missing_score() -> None:
    # 构造：完场但胜平负字母与比分矛盾 → had_letter_mismatch
    bad_letter = PAGE_0916.replace(">胜 </td>", ">负 </td>", 1)
    parsed = caiguo.parse_page(
        caiguo.FetchedPage(business_date="2026-09-16", html=bad_letter, raw=b"")
    )
    reasons = {item.reason for item in parsed.skipped}
    assert "had_letter_mismatch" in reasons

    # 构造：完场但比分列空 → score_missing
    no_score = PAGE_0916.replace('class="clt1" >2</a>', 'class="clt1" ></a>', 1)
    parsed = caiguo.parse_page(
        caiguo.FetchedPage(business_date="2026-09-16", html=no_score, raw=b"")
    )
    reasons = {item.reason for item in parsed.skipped}
    assert "score_missing" in reasons


def test_parse_half_score_out_of_range_goes_manual() -> None:
    # 半场 > 全场（页面异常）→ half_score_invalid，不落库
    html = PAGE_0917.replace(
        '<td align="center" class="red">3 - 0</td>',
        '<td align="center" class="red">9 - 0</td>',
        1,
    )
    parsed = caiguo.parse_page(
        caiguo.FetchedPage(business_date="2026-09-17", html=html, raw=b"")
    )
    assert "half_score_invalid" in {item.reason for item in parsed.skipped}


# --- HTTP 拉取（薄网络层，注入 transport） ---


def test_fetch_follows_redirect_and_decodes_gb18030() -> None:
    with _client({"2026-09-16": PAGE_0916}) as client:
        page = caiguo.fetch_jczq_page(client, _settings(), "2026-09-16")
    assert page.business_date == "2026-09-16"
    assert "周三001" in page.html


def test_fetch_raises_on_http_error() -> None:
    with _client({}) as client:
        with pytest.raises(httpx.HTTPStatusError):
            caiguo.fetch_jczq_page(client, _settings(), "2026-09-16")


# --- 同步链路（匹配 + 落库 + 元信息） ---


def _seed_fixture(
    db, code: str, business_date: str, kickoff: str, fixture_id: int | None = None
) -> int:
    competition = upsert_competition(db, "亚运男足")
    home = upsert_team(db, f"主队{code}")
    away = upsert_team(db, f"客队{code}")
    fid = fixture_id or upsert_fixture(db, competition, kickoff, home, away)
    upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fid, kind="jingcai", business_date=business_date, code=code
        ),
    )
    return fid


def test_sync_imports_finished_matches_and_records_run(db) -> None:
    fid1 = _seed_fixture(db, "周三001", "2026-09-16", "2026-09-16T10:00:00+00:00")
    fid2 = _seed_fixture(db, "周三014", "2026-09-16", "2026-09-16T19:30:00+00:00")
    with _client({"2026-09-16": PAGE_0916}) as client:
        stats = caiguo.sync_draw_results(
            db, _settings(), client, business_dates=["2026-09-16"], now=NOW
        )
    assert stats.fetched == 16
    # 周三001 完场落库；周三014 推迟 → 待人工；其余 15 行完场但库内无场次 → unmatched
    assert stats.imported == 1
    assert stats.unmatched == 15
    assert stats.pending_manual == [
        {"business_date": "2026-09-16", "code": "周三014", "reason": "not_finished"}
    ]
    row = rs_store.get_draw_result(db, fid1)
    assert row is not None
    assert (int(row["home_goals"]), int(row["away_goals"])) == (2, 1)
    assert (int(row["half_home_goals"]), int(row["half_away_goals"])) == (1, 1)
    assert row["source"] == "500.com"
    assert row["published_at"] is None
    assert rs_store.get_draw_result(db, fid2) is None
    # 元信息行可查：来源/时点/场次数
    run = caiguo.latest_sync_run(db)
    assert run is not None
    assert run["source"] == "500.com"
    assert run["observed_at"] == NOW.isoformat(timespec="seconds")
    assert int(run["imported"]) == 1
    assert json.loads(run["business_dates"]) == ["2026-09-16"]


def test_sync_idempotent_rerun_counts_unchanged(db) -> None:
    _seed_fixture(db, "周四001", "2026-09-17", "2026-09-17T05:00:00+00:00")
    with _client({"2026-09-17": PAGE_0917}) as client:
        first = caiguo.sync_draw_results(
            db, _settings(), client, business_dates=["2026-09-17"], now=NOW
        )
        second = caiguo.sync_draw_results(
            db, _settings(), client, business_dates=["2026-09-17"], now=NOW
        )
    assert first.imported == 1
    assert second.imported == 0
    assert second.unchanged == 1
    # 两轮只留一行 draw_results
    assert len(rs_store.list_draw_results(db)) == 1


def test_sync_does_not_overwrite_stored_differs(db) -> None:
    fid = _seed_fixture(db, "周四001", "2026-09-17", "2026-09-17T05:00:00+00:00")
    from goalx_backend.data.ingest.results import import_draw_results

    import_draw_results(
        db,
        [DrawResultInput(fixture_id=fid, home_goals=0, away_goals=0, source="manual")],
    )
    with _client({"2026-09-17": PAGE_0917}) as client:
        stats = caiguo.sync_draw_results(
            db, _settings(), client, business_dates=["2026-09-17"], now=NOW
        )
    # 与人工先录不一致 → 不自动冲正，进待人工清单
    assert stats.imported == 0
    assert stats.pending_manual == [
        {"business_date": "2026-09-17", "code": "周四001", "reason": "stored_differs"}
    ]
    row = rs_store.get_draw_result(db, fid)
    assert row is not None
    assert int(row["home_goals"]) == 0


def test_candidate_business_dates_and_zero_cost_skip(db) -> None:
    # 空库：无待出赛果 → 候选为空，同步零请求零落库但仍记一行元信息
    assert caiguo.candidate_business_dates(db, now=NOW) == []
    with _client({}) as client:
        stats = caiguo.sync_draw_results(db, _settings(), client, now=NOW)
    assert stats.pages == 0
    assert stats.imported == 0
    assert caiguo.latest_sync_run(db) is not None

    # 已开赛无开奖 → 该业务日入选；已有开奖/未开赛/超 7 天窗口 → 不选
    _seed_fixture(db, "周三001", "2026-09-16", "2026-09-16T10:00:00+00:00")
    fid_done = _seed_fixture(db, "周四001", "2026-09-17", "2026-09-17T05:00:00+00:00")
    _seed_fixture(db, "周四002", "2026-09-17", "2026-09-20T12:00:00+00:00")
    _seed_fixture(db, "周五001", "2026-09-05", "2026-09-05T10:00:00+00:00")
    from goalx_backend.data.ingest.results import import_draw_results

    import_draw_results(
        db, [DrawResultInput(fixture_id=fid_done, home_goals=1, away_goals=1)]
    )
    assert caiguo.candidate_business_dates(db, now=NOW) == ["2026-09-16"]


def test_pending_result_count(db) -> None:
    fid = _seed_fixture(db, "周三001", "2026-09-16", "2026-09-16T10:00:00+00:00")
    _seed_fixture(db, "周四001", "2026-09-17", "2026-09-20T12:00:00+00:00")
    assert caiguo.pending_result_count(db, now=NOW) == 1
    from goalx_backend.data.ingest.results import import_draw_results

    import_draw_results(
        db, [DrawResultInput(fixture_id=fid, home_goals=2, away_goals=1)]
    )
    assert caiguo.pending_result_count(db, now=NOW) == 0
