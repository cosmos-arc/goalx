"""新闻/伤停 PIT 前向夜拍测试（autoresearch 票 17：拍窗、PIT 准入、断点、预算）。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import guardian, news_pit

# 拍点时刻：北京 23:30 = UTC 15:30（daily-wrap deployment 同一夜身份）
NIGHT1 = datetime(2026, 11, 10, 15, 30, tzinfo=UTC)
NIGHT2 = datetime(2026, 11, 11, 15, 30, tzinfo=UTC)


def make_settings(tmp_path: Path, *, budget: int = 500) -> Settings:
    return Settings(
        corpus_root=tmp_path / "corpus",
        guardian_api_key="test-key",
        guardian_daily_request_budget=budget,
    )


def page_payload(
    *, pages: int, page: int, ids: list[str], published: str
) -> dict[str, object]:
    def article(article_id: str) -> dict[str, object]:
        return {
            "id": article_id,
            "webPublicationDate": published,
            "webTitle": f"标题 {article_id}",
            "webUrl": f"https://www.theguardian.com/football/{article_id}",
            "sectionName": "Football",
            "fields": {
                "bodyText": "正文文本",
                "lastModified": published,
            },
        }

    return {
        "response": {
            "status": "ok",
            "pages": pages,
            "page": page,
            "pageSize": guardian.PAGE_SIZE,
            "total": pages * len(ids),
            "results": [article(article_id) for article_id in ids],
        }
    }


class FakeClient:
    """按 (from_date, page) 派发预置响应的假客户端（参数断言 + 失败注入）。"""

    def __init__(
        self,
        pages: dict[tuple[str, int], dict[str, object] | Exception],
    ) -> None:
        self.pages = pages
        self.calls: list[dict[str, str | int]] = []

    def get(
        self, url: str, *, params: dict[str, str | int], timeout: float
    ) -> httpx.Response:
        del url, timeout
        self.calls.append(dict(params))
        key = (str(params["from-date"]), int(str(params["page"])))
        canned = self.pages[key]
        if isinstance(canned, Exception):
            raise canned
        return httpx.Response(
            200, text=json.dumps(canned), request=httpx.Request("GET", "https://x")
        )


def seed_corpus_daily(store: CorpusStore, *, last_completed: str) -> None:
    """语料轨（票 79）游标预置：daily 态完成日（拍缝 seam 推导面）。"""
    store.upsert_guardian_state(
        {
            "phase": guardian.PHASE_DAILY,
            "from_date": last_completed,
            "to_date": last_completed,
            "page": 1,
            "last_completed_date": last_completed,
            "total_articles": 100,
        }
    )


def test_first_beat_window_from_corpus_seam(tmp_path: Path) -> None:
    """首拍窗口=语料轨完成日−1→今（保守回看），oldest-first 防漂移。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    seed_corpus_daily(store, last_completed="2026-11-09")
    client = FakeClient(
        {
            ("2026-11-08", 1): page_payload(
                pages=1, page=1, ids=["a1", "a2"], published="2026-11-09T08:00:00Z"
            ),
        }
    )
    stats = news_pit.run_news_pit_beat(store, settings, client, now=NIGHT1)
    store.close()
    assert stats.requests == 1
    assert stats.articles == 2
    assert stats.completed
    call = client.calls[0]
    assert call["from-date"] == "2026-11-08"  # 语料完成日 −1（缝 + 保守回看）
    assert call["to-date"] == "2026-11-10"  # 拍日（进行中日入窗，尾由明晚补）
    assert call["order-by"] == "oldest"
    # 拍行落专表（match_detail_prematch 分表先例），observed_at=拍纪元
    rows = store.read_bronze(news_pit.PROVIDER, news_pit.PIT_DATASET)
    assert len(rows) == 2
    assert rows[0]["parser_version"] == news_pit.PIT_BRONZE_VERSION
    assert rows[0]["fetched_at"] == stats.observed_at


def test_first_beat_virgin_corpus_default_lookback(tmp_path: Path) -> None:
    """无语料轨（virgin corpus）首拍：窗起=今−1（缺省回看）。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    client = FakeClient(
        {
            ("2026-11-09", 1): page_payload(
                pages=1, page=1, ids=["v1"], published="2026-11-10T02:00:00Z"
            ),
        }
    )
    stats = news_pit.run_news_pit_beat(store, settings, client, now=NIGHT1)
    store.close()
    assert stats.completed
    assert client.calls[0]["from-date"] == "2026-11-09"
    # 保守完成：拍日进行中，只记到拍日−1；下一窗重含拍日（防洞）
    state = store.news_pit_state()
    assert state["last_full_day"] == "2026-11-09"
    assert state["window_from"] == "2026-11-10"


def test_next_beat_reincludes_partial_day_tail(tmp_path: Path) -> None:
    """防洞追踪：拍日晚间发布的文章由明晚窗重含拍日兜住（联合覆盖无洞）。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    # 夜 1（11-10 15:30）：窗 [11-09, 11-10]，只看得见拍日午前的 early
    client1 = FakeClient(
        {
            ("2026-11-09", 1): page_payload(
                pages=1, page=1, ids=["early"], published="2026-11-10T08:00:00Z"
            ),
        }
    )
    stats1 = news_pit.run_news_pit_beat(store, settings, client1, now=NIGHT1)
    assert stats1.completed
    # 夜 2（11-11 15:30）：窗 [11-10, 11-11]——重含 11-10，late（拍日
    # 18:00 发布>夜1拍点）此刻首次入观测；early 二次观测=新纪元行
    client2 = FakeClient(
        {
            ("2026-11-10", 1): page_payload(
                pages=1,
                page=1,
                ids=["late", "early"],
                published="2026-11-10T18:00:00Z",
            ),
        }
    )
    stats2 = news_pit.run_news_pit_beat(store, settings, client2, now=NIGHT2)
    store.close()
    assert stats2.completed
    assert client2.calls[0]["from-date"] == "2026-11-10"
    assert client2.calls[0]["to-date"] == "2026-11-11"
    rows = store.read_bronze(news_pit.PROVIDER, news_pit.PIT_DATASET)
    by_sid: dict[str, list[str]] = {}
    for row in rows:
        by_sid.setdefault(str(row["sid"]), []).append(str(row["fetched_at"]))
    # late：只在夜 2 出现——其最早（=首次）观测纪元=夜 2
    assert by_sid["late"] == [stats2.observed_at]
    # early：两夜各一行（append-only 多纪元；读侧取最早为首次准入）
    assert sorted(by_sid["early"]) == sorted([stats1.observed_at, stats2.observed_at])
    assert store.news_pit_state()["last_full_day"] == "2026-11-10"


def test_same_night_rerun_zero_request_heartbeat(tmp_path: Path) -> None:
    """同夜已 ok 重跑：零请求心跳（deployment 重触发/手工冒烟防双花）。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    client = FakeClient(
        {
            ("2026-11-09", 1): page_payload(
                pages=1, page=1, ids=["a1"], published="2026-11-10T08:00:00Z"
            ),
        }
    )
    news_pit.run_news_pit_beat(store, settings, client, now=NIGHT1)
    again = news_pit.run_news_pit_beat(store, settings, client, now=NIGHT1)
    beats = store.news_pit_beats()
    store.close()
    assert again.skipped_done_today
    assert again.requests == 0
    assert len(client.calls) == 1  # 重跑零请求
    assert again.observed_at  # 心跳仍留 observed_at 供晨检归夜
    # 心跳不落新拍行：ok 行保持（覆写成 done_today 会让晨检读到假降级）
    assert len(beats) == 1
    assert beats[0]["status"] == news_pit.STATUS_OK


def test_page_cap_stop_and_resume_no_refetch(tmp_path: Path) -> None:
    """缝夜多页撞单拍上限：页游标持久，次夜续页不重翻前页。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    # 4 页窗口（缝夜形态）
    pages = {
        ("2026-11-09", n): page_payload(
            pages=4, page=n, ids=[f"s{n}"], published="2026-11-09T08:00:00Z"
        )
        for n in range(1, 5)
    }
    client = FakeClient(pages)
    stats = news_pit.run_news_pit_beat(
        store, settings, client, now=NIGHT1, request_cap=2, sleeper=lambda _: None
    )
    assert stats.stopped == "cap"
    assert stats.requests == 2
    assert store.news_pit_state()["page"] == 3  # 游标=下一未拉页
    # 次夜续跑：窗起不变（缝未收口）、page 3 起——不重翻 1/2
    stats2 = news_pit.run_news_pit_beat(store, settings, client, now=NIGHT2)
    store.close()
    assert stats2.completed
    fetched_pages = [int(str(c["page"])) for c in client.calls]
    assert fetched_pages == [1, 2, 3, 4]
    assert stats2.requests == 2
    # 拍行：两夜各一行（cap 夜如实 + 完成夜 ok）
    beats = store.news_pit_beats()
    assert [b["status"] for b in beats] == [news_pit.STATUS_OK, "cap"]  # 新→旧
    assert beats[1]["requests"] == 2
    assert beats[0]["requests"] == 2
    # 累计计数按页增量（correctness：stats.articles 是本拍累计——跨页不双计）
    assert store.news_pit_state()["total_articles"] == 4  # 4 页 × 1 篇


def test_budget_exhausted_stops_before_any_request(tmp_path: Path) -> None:
    """预算触顶：开拉前即停（500/日账本与语料轨共享，防双花）。"""
    settings = make_settings(tmp_path, budget=2)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    store.increment_guardian_requests("2026-11-10")
    store.increment_guardian_requests("2026-11-10")
    client = FakeClient(
        {
            ("2026-11-09", 1): page_payload(
                pages=1, page=1, ids=["a1"], published="2026-11-10T08:00:00Z"
            ),
        }
    )
    stats = news_pit.run_news_pit_beat(store, settings, client, now=NIGHT1)
    store.close()
    assert stats.stopped == "budget"
    assert stats.requests == 0
    assert client.calls == []
    assert store.news_pit_beats()[0]["status"] == "budget"


def test_http_failure_keeps_cursor_same_night_resume(tmp_path: Path) -> None:
    """HTTP 失败：游标不进；同夜重试自断点续完（拍行计数累加）。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    fail = httpx.ConnectError("boom")
    client = FakeClient(
        {
            ("2026-11-09", 2): fail,
        }
    )
    # 预置：page 1 已成功过（游标=2）——重试直接从 2 续
    store.upsert_news_pit_state(
        {
            "window_from": "2026-11-09",
            "page": 2,
            "last_full_day": None,
            "total_articles": 1,
        }
    )
    stats = news_pit.run_news_pit_beat(store, settings, client, now=NIGHT1)
    assert stats.stopped == "http_failure"
    assert store.news_pit_state()["page"] == 2  # 游标未进
    assert store.news_pit_beats()[0]["status"] == "failed"
    # 同夜重试（换好客户端）：从 page 2 续完
    good = FakeClient(
        {
            ("2026-11-09", 2): page_payload(
                pages=2, page=2, ids=["p2"], published="2026-11-10T08:00:00Z"
            ),
        }
    )
    stats2 = news_pit.run_news_pit_beat(store, settings, good, now=NIGHT1)
    store.close()
    assert stats2.completed
    assert int(str(good.calls[0]["page"])) == 2
    beats = store.news_pit_beats()
    assert len(beats) == 1  # 同夜一行：状态覆盖为 ok
    assert beats[0]["status"] == news_pit.STATUS_OK
    assert beats[0]["requests"] == 2  # 失败 1 + 成功 1 累加（诚实账）


def test_missing_key_skips_with_honest_beat_row(tmp_path: Path) -> None:
    """key 未配置：零请求跳过，拍行如实记 no_key（晨检可分辨）。"""
    settings = Settings(
        corpus_root=tmp_path / "corpus",
        guardian_api_key="",
    )
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    client = FakeClient({})
    stats = news_pit.run_news_pit_beat(store, settings, client, now=NIGHT1)
    store.close()
    assert stats.skipped_no_key
    assert stats.requests == 0
    assert client.calls == []
    assert store.news_pit_beats()[0]["status"] == news_pit.STATUS_NO_KEY


def test_pit_admission_predicate_tz_strict() -> None:
    """准入判据（账本红线代码化）：observed_at≤决策时点；naive/非法不准入。"""
    asof = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    assert news_pit.pit_admitted("2026-11-10T11:59:59Z", asof)
    assert news_pit.pit_admitted("2026-11-10T12:00:00Z", asof)  # 边界含等
    assert not news_pit.pit_admitted("2026-11-10T12:00:01Z", asof)
    # 带偏移时刻显式归一（北京 20:00 = UTC 12:00 恰准入）
    assert news_pit.pit_admitted("2026-11-10T20:00:00+08:00", asof)
    # naive/非法：不可证 → 不准入（晚到不回灌的防御面）
    assert not news_pit.pit_admitted("2026-11-10 12:00:00", asof)
    assert not news_pit.pit_admitted("garbage", asof)
    assert not news_pit.pit_admitted("", asof)


def test_e12_pairing_notes_reserved() -> None:
    """E-12 扩样面预留：配对口径钉住（读面 stub——采集是长跑）。"""
    notes = news_pit.E12_PAIRING_NOTES
    assert notes["power_gate_n"] == 785  # 55% 功效门所需独立场次（票面）
    assert "jc_shift" in notes["pairing"]
    assert "observed_at" in notes["admission"]  # 准入判据单一真相源挂靠
    assert notes["status"] == "reserved"


def test_dual_track_face_backfill_and_forward(tmp_path: Path) -> None:
    """双轨报告：存量清单（语料轨游标+预算）+ 增量通道（夜拍连续面）。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    # 存量轨：回填进行中（1999 起，锚定日冻结）
    store.upsert_guardian_state(
        {
            "phase": guardian.PHASE_BACKFILL,
            "from_date": "2001-06-15",
            "to_date": "2026-11-10",
            "page": 3,
            "last_completed_date": None,
            "total_articles": 5_000,
            "boundary": None,
        }
    )
    store.increment_guardian_requests("2026-11-10")
    store.upsert_news_pit_state(
        {
            "window_from": "2026-11-10",
            "page": 1,
            "last_full_day": "2026-11-09",
            "total_articles": 60,
        }
    )
    # 增量通道：三夜两 ok 一 cap
    for beat_date, status in (
        ("2026-11-08", news_pit.STATUS_OK),
        ("2026-11-09", "cap"),
        ("2026-11-10", news_pit.STATUS_OK),
    ):
        store.record_news_pit_beat(
            {
                "beat_date": beat_date,
                "observed_at": f"{beat_date}T15:30:00+00:00",
                "window_from": "2026-11-07",
                "window_to": beat_date,
                "pages": 1,
                "requests": 1,
                "articles": 20,
                "status": status,
            }
        )
    face = news_pit.dual_track_face(store, settings, today=date(2026, 11, 10))
    store.close()
    backfill = face["backfill"]
    assert backfill["phase"] == guardian.PHASE_BACKFILL
    assert backfill["cursor"] == {
        "from_date": "2001-06-15",
        "to_date": "2026-11-10",
        "page": 3,
    }
    assert backfill["requests_today"] == 1
    assert backfill["budget"] == 500
    assert backfill["estimated_progress"] is not None  # 5_000/203_080 估计面
    forward = face["forward_pit"]
    assert forward["last_beat"]["beat_date"] == "2026-11-10"
    assert forward["beats_recent_7d"] == 3
    assert forward["ok_beats_recent_7d"] == 2
    assert forward["gap_nights_recent_7d"] == []  # 三夜连续无洞
    assert forward["total_observations"] == 60
    assert "observed_at" in str(face["pit_rule"]["admission"])


def test_dual_track_face_gap_nights_honest(tmp_path: Path) -> None:
    """缺口夜如实列出（每晚不开=永久缺口——可见性优先）。"""
    settings = make_settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    store.record_news_pit_beat(
        {
            "beat_date": "2026-11-07",
            "observed_at": "2026-11-07T15:30:00+00:00",
            "window_from": "2026-11-06",
            "window_to": "2026-11-07",
            "pages": 1,
            "requests": 1,
            "articles": 10,
            "status": news_pit.STATUS_OK,
        }
    )
    face = news_pit.dual_track_face(store, settings, today=date(2026, 11, 10))
    store.close()
    forward = face["forward_pit"]
    # 首拍 11-07 起算：11-08/11-09 两夜缺拍（today=11-10 属进行中夜不计缺；
    # 首拍前的夜不算缺——未部署期的诚实口径）
    assert forward["gap_nights_recent_7d"] == [
        "2026-11-08",
        "2026-11-09",
    ]
    assert forward["ok_beats_recent_7d"] == 1


def test_cli_wires_news_pit_commands(monkeypatch: Any) -> None:
    """CLI 面（票 17）：news-pit-beat 手动触发 + guardian-status 双轨只读报告。"""
    from goalx_backend import cli

    parser = cli.build_parser()
    args = parser.parse_args(["news-pit-beat", "--request-cap", "3"])
    assert args.request_cap == 3
    assert parser.parse_args(["guardian-status"]).command == "guardian-status"
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli.tasks,
        "news_pit_beat",
        lambda **kwargs: captured.update(kwargs) or {"beat": "ok"},
    )
    monkeypatch.setattr(cli.tasks, "guardian_status", lambda: {"face": True})
    assert cli.main(["news-pit-beat"]) == 0
    assert cli.main(["news-pit-beat", "--request-cap", "5"]) == 0
    assert captured == {"request_cap": 5}  # 参数透传 task 适配器
    assert cli.main(["guardian-status"]) == 0
