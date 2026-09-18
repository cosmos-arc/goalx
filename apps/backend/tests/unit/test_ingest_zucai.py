"""源B 彩池采集单测（票 43）：fixture 解析纯函数 + MockTransport 同步（零外网）。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data import pool as pool_store
from goalx_backend.data.ingest.zucai import (
    FetchedPage,
    fetch_popularity,
    parse_index_periods,
    parse_issue_page,
    parse_popularity,
    sync_pool_data,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _issue_page() -> FetchedPage:
    raw = (FIXTURES / "zucai_issue_26131.html.txt").read_bytes()
    return FetchedPage(url="x", text=raw.decode("gb18030", errors="replace"), raw=raw)


def _percent() -> dict[str, object]:
    return json.loads((FIXTURES / "zucai_pop_26131_percent.json").read_bytes())


def _number() -> dict[str, object]:
    return json.loads((FIXTURES / "zucai_pop_26131_number.json").read_bytes())


def test_parse_index_periods() -> None:
    """索引页 → 近期期号（升序去重，实测样本含在售与预售期）。"""
    raw = (FIXTURES / "zucai_index.html.txt").read_bytes()
    page = FetchedPage(url="x", text=raw.decode("gb18030", errors="replace"), raw=raw)
    assert parse_index_periods(page) == ["26129", "26130", "26131", "26132"]


def test_parse_issue_page_matches_and_deadline() -> None:
    """期次页 → 14 场对阵（场序/联赛/北京时间→UTC/主客/欧指）+ 截止时间。"""
    issue = parse_issue_page(_issue_page(), "26131")
    assert issue.sales_deadline_utc == "2026-09-20T12:30:00+00:00"
    assert [m.match_seq for m in issue.matches] == list(range(1, 15))
    first = issue.matches[0]
    assert first.source_match_id == "1331808"
    assert first.league == "英超"
    assert first.kickoff_utc == "2026-09-20T13:00:00+00:00"
    assert first.home_team == "伯恩茅"
    assert first.away_team == "利物浦"
    assert first.euro_odds == (3.05, 3.77, 2.14)
    # 14 场欧指全量可解析（概率兜底口径的输入完整性）
    assert all(
        h is not None and d is not None and a is not None
        for h, d, a in (m.euro_odds for m in issue.matches)
    )


def test_parse_issue_page_skips_broken_rows() -> None:
    """结构不完整的行直接跳过（宁缺勿错，不伪造对阵）。"""
    broken = FetchedPage(
        url="x",
        text='<tr id="tr1"><td>残缺行</td></tr>'
        + '<tr id="tr2"><td class="xh">2</td></tr>',
        raw=b"",
    )
    issue = parse_issue_page(broken, "26998")
    assert issue.matches == []
    assert issue.sales_deadline_utc is None


def test_parse_popularity_maps_keys_to_pool_codes() -> None:
    """源B 键 1/2/3 → 官方池码 3/1/0（主胜/平/客胜；实证键语义）。"""
    shares = parse_popularity(_percent(), _number())
    # 实测样本 14 场全量
    assert sorted(shares) == list(range(1, 15))
    seq1 = shares[1]
    # 实测：场 1 客胜（利物浦）人气 48.95% → 官方码 0
    assert seq1.shares["0"] == pytest.approx(0.4895)
    assert seq1.shares["3"] == pytest.approx(0.2251)
    assert seq1.shares["1"] == pytest.approx(0.2854)
    assert sum(seq1.shares.values()) == pytest.approx(1.0, abs=1e-6)
    # 票数同键映射（量级参考）
    assert seq1.votes is not None
    assert seq1.votes["0"] == 383670


def test_parse_popularity_drops_invalid_triples() -> None:
    """三向不全或和明显异常的场次整场丢弃。"""
    bad = {
        "statistic_popularity_response": {
            "1": {"3": "40", "1": "30"},  # 缺一向
            "2": {"3": "70", "1": "20", "2": "30"},  # 和=120% 越界
            "3": "not-a-dict",
            "4": {"3": "40", "1": "30", "2": "30"},  # 合法
        }
    }
    shares = parse_popularity(bad)
    assert sorted(shares) == [4]


def _mock_client() -> httpx.Client:
    """按 URL 分发的 MockTransport（期次页/索引/人气三种响应）。"""
    issue_raw = (FIXTURES / "zucai_issue_26131.html.txt").read_bytes()
    index_raw = (FIXTURES / "zucai_index.html.txt").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/ajax/" in url:
            # 人气接口的访问限制：Referer 必须指向对应期次页
            assert request.headers["Referer"].startswith("https://www.okooo.com/zucai/")
            assert request.url.params["method"] == "data.statistic.popularity"
            if request.url.params["Type"] == "percent":
                return httpx.Response(200, json=_percent())
            return httpx.Response(200, json=_number())
        if re.fullmatch(r"https://www\.okooo\.com/zucai/\d{5}/", url):
            return httpx.Response(200, content=issue_raw)
        assert url.endswith("/zucai/"), url
        return httpx.Response(200, content=index_raw)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sync_pool_data_persists_and_is_idempotent(db) -> None:
    """同步落库（期次/对阵当前态 + 份额快照）；重放刷新对阵、快照追加。"""
    client = _mock_client()
    fixed = datetime(2026, 9, 18, 6, 30, tzinfo=UTC)
    stats = sync_pool_data(db, Settings(), client, period_nos=["26131"], now=fixed)
    assert stats.period_nos == ["26131"]
    assert stats.matches == 14
    assert stats.share_rows == 42  # 14 场 × 3 向
    assert stats.missing_shares == 0
    rows = pool_store.list_pool_periods(db)
    assert [r["period_no"] for r in rows] == ["26131"]
    assert rows[0]["match_count"] == 14
    assert rows[0]["sales_deadline"] == "2026-09-20T12:30:00+00:00"
    shares = pool_store.latest_shares_for_period(db, 1)
    assert len(shares) == 14
    assert shares[1]["0"] == pytest.approx(0.4895)
    # 同步元信息（append-only，一次一行）
    run = pool_store.latest_pool_sync_run(db)
    assert run["source"] == "okooo.com"
    assert json.loads(run["period_nos"]) == ["26131"]
    # 重放：对阵当前态刷新（数不变），份额按 captured_at 追加一版
    later = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
    sync_pool_data(db, Settings(), client, period_nos=["26131"], now=later)
    assert pool_store.list_pool_periods(db)[0]["match_count"] == 14
    assert db.execute("SELECT COUNT(*) FROM public_shares").fetchone()[0] == 84


def test_fetch_popularity_requires_referer() -> None:
    """人气接口无 Referer → 源 405（访问限制实证；调用方必须带期次页 Referer）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "Referer" not in request.headers:
            return httpx.Response(405)
        return httpx.Response(200, json=_percent())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        out = fetch_popularity(client, Settings(), "26131")
        assert "statistic_popularity_response" in out


def test_parimutuel_ev_guards() -> None:
    """彩池口径 EV：抽水折算公式 + 无份额护栏（不伪造无穷赔率）。"""
    assert pool_store.parimutuel_odds(0.25) == pytest.approx(0.65 / 0.25)
    assert pool_store.parimutuel_ev(0.3, 0.25) == pytest.approx(0.3 * 2.6 - 1)
    assert pool_store.parimutuel_ev(0.3, 0.0) is None


def test_pool_state_upsert_idempotent(db) -> None:
    """AI 代采落库幂等：同值重放单行不追加；新值覆盖（状态表=最新一版）。"""
    period_id = pool_store.upsert_pool_period(db, "ttt14", "26999", None)
    pool_store.upsert_pool_state(
        db,
        period_id,
        sales_amount=100.0,
        rollover_in=None,
        prize_tiers=None,
        published_at=None,
        source="agent",
    )
    db.commit()
    pool_store.upsert_pool_state(
        db,
        period_id,
        sales_amount=100.0,
        rollover_in=None,
        prize_tiers=None,
        published_at=None,
        source="agent",
    )
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM pool_states").fetchone()[0] == 1
    assert pool_store.pool_state_for_period(db, period_id)["sales_amount"] == 100.0
    pool_store.upsert_pool_state(
        db,
        period_id,
        sales_amount=200.0,
        rollover_in=None,
        prize_tiers=None,
        published_at="2026-09-21T12:00:00+00:00",
        source="agent",
    )
    db.commit()
    row = pool_store.pool_state_for_period(db, period_id)
    assert db.execute("SELECT COUNT(*) FROM pool_states").fetchone()[0] == 1
    assert row["sales_amount"] == 200.0


def test_sync_discovers_periods_via_index(db) -> None:
    """期次发现缺省路径：索引页链接 → 最新 window 期（同步入口不传期号）。"""
    client = _mock_client()
    fixed = datetime(2026, 9, 18, 6, 30, tzinfo=UTC)
    stats = sync_pool_data(db, Settings(), client, window=1, now=fixed)
    # 索引发现 [26129..26132]，window=1 → 只同步最新一期
    assert stats.period_nos == ["26132"]
    assert len(stats.period_nos) == 1


def test_sync_records_missing_shares_for_popularity_less_period(db) -> None:
    """无人气数据的期次（如预售期）：missing_shares 如实计数、不发份额行。"""

    issue_raw = (FIXTURES / "zucai_issue_26131.html.txt").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/ajax/" in url:
            return httpx.Response(200, json={"statistic_popularity_response": {}})
        return httpx.Response(200, content=issue_raw)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        stats = sync_pool_data(
            db, Settings(), client, period_nos=["26199"], now=datetime.now(UTC)
        )
    assert stats.matches == 14
    assert stats.share_rows == 0
    assert stats.missing_shares == 14
    assert db.execute("SELECT COUNT(*) FROM public_shares").fetchone()[0] == 0
