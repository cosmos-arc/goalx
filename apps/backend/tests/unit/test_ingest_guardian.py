"""卫报新闻语料采集测试（票 79：分页状态机、预算熔断、断点续跑、双时间）。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import httpx

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import guardian

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)


def make_settings(tmp_path: Path, *, budget: int = 500) -> Settings:
    return Settings(
        corpus_root=tmp_path / "corpus",
        guardian_api_key="test-key",
        guardian_daily_request_budget=budget,
    )


def page_payload(
    *, pages: int, page: int, ids: list[str], total: int | None = None
) -> dict[str, object]:
    def article(article_id: str) -> dict[str, object]:
        return {
            "id": article_id,
            "webPublicationDate": "2026-09-26T18:00:00Z",
            "webTitle": f"标题 {article_id}",
            "webUrl": f"https://www.theguardian.com/football/{article_id}",
            "sectionName": "Football",
            "fields": {
                "bodyText": "正文文本",
                "lastModified": "2026-09-26T19:30:00Z",
            },
        }

    return {
        "response": {
            "status": "ok",
            "pages": pages,
            "page": page,
            "pageSize": guardian.PAGE_SIZE,
            "total": total if total is not None else pages * guardian.PAGE_SIZE,
            "results": [article(article_id) for article_id in ids],
        }
    }


class FakeClient:
    """按 (page) 派发预置响应的假客户端（params 断言 + 失败注入）。"""

    def __init__(self, pages: dict[int, dict[str, object] | Exception]) -> None:
        self.pages = pages
        self.calls: list[dict[str, str | int]] = []

    def get(
        self, url: str, *, params: dict[str, str | int], timeout: float
    ) -> httpx.Response:
        del url, timeout
        self.calls.append(dict(params))
        page = int(str(params["page"]))
        canned = self.pages[page]
        if isinstance(canned, Exception):
            raise canned
        return httpx.Response(
            200, text=json.dumps(canned), request=httpx.Request("GET", "https://x")
        )


def test_parse_articles_stores_both_timestamps() -> None:
    """发布/修改时间双存原样（定则 2/3）；缺 bodyText 行诚实置 None。"""
    payload = page_payload(pages=1, page=1, ids=["a1"])
    payload["response"]["results"] = cast(
        "list[object]",
        [
            payload["response"]["results"][0],
            {"id": "a2", "fields": {}},  # 缺字段行
        ],
    )
    rows = guardian.parse_articles(payload)
    assert rows[0]["webPublicationDate"] == "2026-09-26T18:00:00Z"
    assert rows[0]["lastModified"] == "2026-09-26T19:30:00Z"
    assert rows[0]["bodyText"] == "正文文本"
    assert rows[1]["lastModified"] == ""
    assert rows[1]["bodyText"] is None


def test_backfill_pages_then_transitions_to_daily(tmp_path: Path) -> None:
    """回填三页拉完转 daily；oldest-first + to-date 锚定参数齐；间距生效。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    client = FakeClient(
        {
            1: page_payload(pages=3, page=1, ids=["p1a", "p1b"]),
            2: page_payload(pages=3, page=2, ids=["p2a"]),
            3: page_payload(pages=3, page=3, ids=["p3a"]),
        }
    )
    sleeps: list[float] = []
    stats = guardian.sync_guardian(
        store, settings, client, now=NOW, sleeper=sleeps.append
    )
    store.close()
    assert stats.requests == 3
    assert stats.articles == 4
    assert stats.completed
    assert stats.phase == guardian.PHASE_DAILY
    assert sleeps == [1.0, 1.0]  # 页间距（尾页后不睡）
    first = client.calls[0]
    assert first["order-by"] == "oldest"
    assert first["from-date"] == "1999-01-01"
    assert first["to-date"] == "2026-09-27"  # 首跑锚定日冻结
    state = stats.state
    assert state["last_completed_date"] == "2026-09-27"
    assert stats.total_articles == 4


def test_budget_exhaustion_stops_and_resume_continues(tmp_path: Path) -> None:
    """预算熔断停在游标；次日（新账本日）自断点续跑到完。"""
    settings = make_settings(tmp_path, budget=3)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    # 预置当日已用 2/3 → 只允许再 1 页
    store.increment_guardian_requests(NOW.date().isoformat())
    store.increment_guardian_requests(NOW.date().isoformat())
    client = FakeClient(
        {
            1: page_payload(pages=3, page=1, ids=["p1a"]),
            2: page_payload(pages=3, page=2, ids=["p2a"]),
            3: page_payload(pages=3, page=3, ids=["p3a"]),
        }
    )
    stats = guardian.sync_guardian(store, settings, client, now=NOW)
    assert stats.budget_exhausted
    assert stats.requests == 1
    assert stats.state["page"] == 2
    # 次日续跑：账本日翻转 → 剩余两页拉完
    next_day = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
    stats2 = guardian.sync_guardian(store, settings, client, now=next_day)
    store.close()
    assert stats2.requests == 2
    assert stats2.completed
    assert stats2.total_articles == 3


def test_daily_zero_request_after_done(tmp_path: Path) -> None:
    """daily 态当日已完成 → 零请求跳过（零成本心跳）。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    store.upsert_guardian_state(
        {
            "phase": "daily",
            "from_date": "2026-09-27",
            "to_date": "2026-09-27",
            "page": 1,
            "last_completed_date": "2026-09-27",
            "total_articles": 10,
        }
    )
    client = FakeClient({})
    stats = guardian.sync_guardian(store, settings, client, now=NOW)
    store.close()
    assert stats.skipped_done_today
    assert stats.requests == 0
    assert client.calls == []


def test_daily_incremental_window_from_last_completed(tmp_path: Path) -> None:
    """日增量窗口=完成日+1→今日，新→旧直取（无 order-by）。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    store.upsert_guardian_state(
        {
            "phase": "daily",
            "from_date": "2026-09-26",
            "to_date": "2026-09-26",
            "page": 1,
            "last_completed_date": "2026-09-26",
            "total_articles": 100,
        }
    )
    client = FakeClient({1: page_payload(pages=1, page=1, ids=["d1"])})
    stats = guardian.sync_guardian(store, settings, client, now=NOW)
    store.close()
    assert stats.requests == 1
    assert stats.completed
    call = client.calls[0]
    assert call["from-date"] == "2026-09-27"
    assert call["to-date"] == "2026-09-27"
    assert "order-by" not in call
    assert stats.total_articles == 101


def test_request_cap_stops_midway(tmp_path: Path) -> None:
    """单次上限独立于日预算（回填多日分摊的可控旋钮）。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    client = FakeClient(
        {
            1: page_payload(pages=3, page=1, ids=["p1a"]),
            2: page_payload(pages=3, page=2, ids=["p2a"]),
        }
    )
    stats = guardian.sync_guardian(
        store, settings, client, now=NOW, request_cap=1, sleeper=lambda _: None
    )
    store.close()
    assert stats.request_capped
    assert stats.requests == 1
    assert stats.state["page"] == 2


def test_http_failure_keeps_cursor_no_state_advance(tmp_path: Path) -> None:
    """HTTP 失败：记账但不进游标（断点原地重试）。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    client = FakeClient(
        {
            1: httpx.ConnectError("boom"),
            2: page_payload(pages=2, page=2, ids=["p2a"]),
        }
    )
    stats = guardian.sync_guardian(store, settings, client, now=NOW)
    store.close()
    assert stats.requests == 0
    assert stats.pages == 0
    assert store.guardian_state()["page"] == 1  # 游标未进


def test_raw_bronze_envelope_and_sha_roundtrip(tmp_path: Path) -> None:
    """raw-first 对账：ingest sha == bronze raw_sha == checkpoint 登记；verify 真。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    client = FakeClient({1: page_payload(pages=1, page=1, ids=["a1", "a2"])})
    guardian.sync_guardian(store, settings, client, now=NOW)
    rows = store.read_bronze(guardian.PROVIDER, guardian.DATASET)
    assert len(rows) == 2
    assert rows[0]["parser_version"] == guardian.BRONZE_VERSION
    key = guardian._raw_key("1999-01-01", 1)
    assert store.verify_raw(guardian.PROVIDER, guardian.RAW_DATASET, key)
    for row in rows:
        assert row["raw_sha"] == store.raw_sha(
            guardian.PROVIDER, guardian.RAW_DATASET, key
        )
    store.close()


def test_missing_key_skips_cleanly(tmp_path: Path) -> None:
    """key 未配置：告警跳过零请求（防误打空 key 请求）。"""
    settings = Settings(corpus_root=tmp_path / "corpus", guardian_api_key="")
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    client = FakeClient({})
    stats = guardian.sync_guardian(store, settings, client, now=NOW)
    store.close()
    assert stats.requests == 0
    assert client.calls == []
    assert "state" in stats.__dict__


def test_fetch_failure_counts_budget_on_404(tmp_path: Path) -> None:
    """404/5xx 也记 1（免费层失败请求同样耗配额，propline 同型）。"""
    settings = make_settings(tmp_path, budget=2)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    client = FakeClient({1: httpx.HTTPStatusError("500", request=None, response=None)})
    stats = guardian.sync_guardian(store, settings, client, now=NOW)
    store.close()
    assert stats.requests == 0
    assert store.guardian_requests(NOW.date().isoformat()) >= 1


def test_idempotent_rerun_after_completion_zero_pages(tmp_path: Path) -> None:
    """回填完成后同日重跑：daily 已完成 → 零请求（幂等心跳）。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    client = FakeClient({1: page_payload(pages=1, page=1, ids=["only"])})
    guardian.sync_guardian(store, settings, client, now=NOW)
    again = guardian.sync_guardian(store, settings, client, now=NOW)
    store.close()
    assert again.skipped_done_today
    assert again.requests == 0
    assert len(store.read_bronze(guardian.PROVIDER, guardian.DATASET)) == 1


def test_date_helpers() -> None:
    """窗口小函数：缺口补齐 / 无完成日起于今日。"""
    today = date(2026, 9, 27)
    state = {"last_completed_date": "2026-09-25"}
    daily_window = guardian._daily_window
    assert daily_window(state, today) == ("2026-09-26", "2026-09-27")
    assert daily_window({}, today) == ("2026-09-27", "2026-09-27")
