"""NewsShock P0 抽取器（model-iterate 票 04）：链接/预筛/解析的行为断言。"""

from __future__ import annotations

import json
from typing import Any

import duckdb
import pytest

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import guardian_silver as gs


def _bronze_row(sid: str, title: str, published: str, body: str = "") -> dict[str, Any]:
    return {
        "sid": sid,
        "payload": {
            "id": sid,
            "webTitle": title,
            "webPublicationDate": published,
            "bodyText": body,
        },
    }


# ---- 词边界与预筛 ----


def test_word_boundary_no_substring_false_positive():
    """Man City 不命中 Manchester City 的子串误报（词边界）。"""
    assert gs._word_boundary("Arsenal", "Arsenal team news")
    assert not gs._word_boundary("Man City", "Manchester City team news")
    assert not gs._word_boundary("Arsenal", "Arsenalist bulletin")


def test_candidate_articles_window_and_pit(tmp_path):
    """窗内候选保留；赛后稿计数剔除；窗外/无命中丢弃。"""
    store = CorpusStore(tmp_path)
    links = {
        "s1": gs._Link(
            sid="s1",
            kickoff_day="2024-03-10",
            kickoff_iso="2024-03-10T20:00:00",
            en_home="Arsenal",
            en_away="Chelsea",
        )
    }
    rows = [
        _bronze_row("a1", "Arsenal injury news", "2024-03-08T10:00:00Z"),  # 窗内
        _bronze_row("a2", "Arsenal match report", "2024-03-10T23:59:59Z"),  # 开球后
        _bronze_row("a3", "Arsenal preview", "2024-02-01T10:00:00Z"),  # 窗外
        _bronze_row("a4", "Nothing relevant", "2024-03-08T10:00:00Z"),  # 无命中
    ]
    store.append_bronze(gs.PROVIDER, gs.BRONZE_DATASET, rows)
    candidates, scanned, post = gs.candidate_articles(store, links)
    assert scanned == 4
    assert post == 1
    assert len(candidates) == 1
    assert candidates[0].article_sid == "a1"
    assert candidates[0].side == "home"
    store.close()


def test_candidate_articles_dedup_article_sid(tmp_path):
    """同 sid 重复行只扫一次（bronze append 语义防重）。"""
    store = CorpusStore(tmp_path)
    links = {
        "s1": gs._Link(
            sid="s1",
            kickoff_day="2024-03-10",
            kickoff_iso="2024-03-10T20:00:00",
            en_home="Arsenal",
            en_away="Chelsea",
        )
    }
    row = _bronze_row("a1", "Arsenal news", "2024-03-09T10:00:00Z")
    store.append_bronze(gs.PROVIDER, gs.BRONZE_DATASET, [row, dict(row)])
    candidates, scanned, _ = gs.candidate_articles(store, links)
    assert scanned == 1
    assert len(candidates) == 1
    store.close()


# ---- LLM 响应解析 ----


def test_parse_extraction_valid_with_fence():
    content = (
        '```json\n{"relevant": true, "events": [{"type": "injury",'
        ' "side": "home", "player": "Saka", "confidence": 0.8}]}\n```'
    )
    parsed = gs.parse_extraction(content)
    assert parsed is not None
    assert parsed["relevant"] is True
    assert parsed["events"][0]["type"] == "injury"


def test_parse_extraction_violations_return_none():
    """非 JSON/顶层缺字段/类型越界/置信度越界/事件非对象 → None。"""
    assert gs.parse_extraction("not json") is None
    assert gs.parse_extraction('{"relevant": "yes", "events": []}') is None
    assert gs.parse_extraction('{"relevant": true, "events": "x"}') is None
    bad_type = (
        '{"relevant": true, "events": [{"type": "bogus",'
        ' "side": "home", "confidence": 0.5}]}'
    )
    assert gs.parse_extraction(bad_type) is None
    bad_conf = (
        '{"relevant": true, "events": [{"type": "injury",'
        ' "side": "home", "confidence": 1.5}]}'
    )
    assert gs.parse_extraction(bad_conf) is None
    bad_side = (
        '{"relevant": true, "events": [{"type": "injury",'
        ' "side": "mid", "confidence": 0.5}]}'
    )
    assert gs.parse_extraction(bad_side) is None
    non_dict_event = '{"relevant": true, "events": ["oops"]}'
    assert gs.parse_extraction(non_dict_event) is None


# ---- 链接与构建（合成 duck） ----


def _pilot_duck() -> duckdb.DuckDBPyConnection:
    """合成 duck：fixture_universe + goalx.hist_matches（E0 双场）。"""
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE fixture_universe (
            sid VARCHAR, league VARCHAR, kickoff TIMESTAMP, season VARCHAR,
            home VARCHAR, away VARCHAR, home_goals INT, away_goals INT,
            stage VARCHAR
        )
        """
    )
    con.execute(
        "INSERT INTO fixture_universe VALUES"
        " ('s1', '英超', TIMESTAMP '2024-03-10 20:00:00', '2023-24',"
        "  '阿森纳', '切尔西', 2, 1, '常规轮'),"
        " ('s2', '英超', TIMESTAMP '2024-04-01 20:00:00', '2023-24',"
        "  '利物浦', '曼城', 1, 1, '常规轮')"
    )
    con.execute("CREATE SCHEMA goalx")
    con.execute(
        """
        CREATE TABLE goalx.hist_matches (
            competition VARCHAR, season VARCHAR, match_date VARCHAR,
            home_team VARCHAR, away_team VARCHAR, fthg INT, ftag INT
        )
        """
    )
    con.execute(
        "INSERT INTO goalx.hist_matches VALUES"
        " ('E0', '2023-24', '2024-03-10', 'Arsenal', 'Chelsea', 2, 1),"
        " ('E0', '2023-24', '2024-04-01', 'Liverpool', 'Man City', 1, 1)"
    )
    return con


def test_fixture_index_and_english_links():
    """试点索引过滤生效；英文名经 match_fixtures 配到 sid。"""
    con = _pilot_duck()
    fixtures = gs.fixture_index(con, "E0", ("2023-24",))
    assert [f.sid for f in fixtures] == ["s1", "s2"]
    links, unlinked = gs.english_links(con, fixtures, "E0")
    assert unlinked == 0
    assert links["s1"].en_home == "Arsenal"
    assert links["s2"].en_away == "Man City"
    con.close()


def test_fixture_index_unknown_competition_fails_loud():
    con = _pilot_duck()
    with pytest.raises(ValueError, match="未知联赛码"):
        gs.fixture_index(con, "XX", ("2023-24",))
    con.close()


def test_build_news_events_with_fake_extractor(tmp_path):
    """全链（注入 extractor）：候选→抽取→事件行→silver 落盘+meta。"""
    from goalx_backend.config import Settings

    con = _pilot_duck()
    store = CorpusStore(tmp_path)
    store.append_bronze(
        gs.PROVIDER,
        gs.BRONZE_DATASET,
        [
            _bronze_row(
                "a1",
                "Arsenal injury crisis ahead of Chelsea clash",
                "2024-03-09T10:00:00Z",
                "Saka is out injured. rotation likely.",
            ),
            _bronze_row(
                "a2",
                "Liverpool preview",
                "2024-03-31T10:00:00Z",
                "regular preview",
            ),
        ],
    )

    def fake_extractor(candidate):
        if "Arsenal" in candidate.title:
            return (
                {
                    "relevant": True,
                    "events": [
                        {
                            "type": "injury",
                            "side": "home",
                            "player": "Saka",
                            "confidence": 0.9,
                        }
                    ],
                },
                "fake-model",
                0.0,
                False,
            )
        return {"relevant": False, "events": []}, "fake-model", 0.0, False

    ctx = gs.NewsContext(
        duck_con=con,
        conn=None,
        settings=Settings(),  # type: ignore[arg-type]
    )
    report = gs.build_news_events(
        ctx, store, seasons=("2023-24",), extractor=fake_extractor
    )
    assert report.linked_sids == 2
    assert report.candidates == 2
    assert report.events == 1
    assert report.relevant_articles == 1
    meta = json.loads(
        (store.silver_path(gs.PROVIDER, gs.EVENT_DATASET) / "_meta.json").read_text()
    )
    assert meta["rows"] == 1
    assert meta["pilot"] == {"competition": "E0", "seasons": ["2023-24"]}
    import pyarrow.parquet as pq

    table = pq.read_table(store.silver_path(gs.PROVIDER, gs.EVENT_DATASET) / "all")
    rows = table.to_pylist()
    assert rows[0]["sid"] == "s1"
    assert rows[0]["event_type"] == "injury"
    assert rows[0]["model"] == "fake-model"
    store.close()
    con.close()


def test_pit_violation_fails_loud(tmp_path):
    """PIT 终检：published_at 晚于开球日的事件行落盘即炸。"""
    store = CorpusStore(tmp_path)
    # 直接构造违例行喂 _write_silver
    with pytest.raises(ValueError, match="PIT"):
        gs._write_silver(
            store,
            [
                {
                    "article_sid": "late",
                    "sid": "s1",
                    "league": "E0",
                    "season": "2023-24",
                    "published_at": "2024-03-10T23:00:00Z",
                    "kickoff_day": "2024-03-10",
                    "kickoff_iso": "2024-03-10T20:00:00",
                    "event_type": "injury",
                    "side": "home",
                    "player": None,
                    "confidence": 0.5,
                    "model": "test",
                    "version": gs.EVENT_VERSION,
                }
            ],
            "E0",
            ("2023-24",),
            gs.GuardianEventReport(),
        )
    store.close()


def test_checkpoint_resume_skips_done_articles(tmp_path):
    """断点续跑：checkpoint 已有文章不再抽取；成功后 checkpoint 清除。"""
    from goalx_backend.config import Settings

    con = _pilot_duck()
    store = CorpusStore(tmp_path)
    store.append_bronze(
        gs.PROVIDER,
        gs.BRONZE_DATASET,
        [_bronze_row("a1", "Arsenal news", "2024-03-09T10:00:00Z", "body")],
    )
    ctx = gs.NewsContext(
        duck_con=con,
        conn=None,
        settings=Settings(),  # type: ignore[arg-type]
    )
    checkpoint = gs._checkpoint_path(store)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        json.dumps(
            {
                "article_sid": "a1",
                "filtered": False,
                "parsed": {"relevant": True, "events": []},
                "model": "cached",
                "cost": 0.0,
                "empty": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls = []

    def counting_extractor(candidate):
        calls.append(candidate.article_sid)
        return None, "should-not-run", 0.0, True

    report = gs.build_news_events(
        ctx, store, seasons=("2023-24",), extractor=counting_extractor
    )
    assert calls == []  # 全部命中 checkpoint，零新调用
    assert report.model == "cached"
    assert not checkpoint.exists()  # 成功后清除
    store.close()
    con.close()
