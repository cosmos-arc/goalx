"""NewsShock P0 抽取器（model-iterate 票 04）：链接/预筛/解析的行为断言。"""

from __future__ import annotations

import json
from typing import Any

import duckdb
import pytest

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import guardian_silver as gs


def _bronze_row(
    sid: str,
    title: str,
    published: str,
    body: str = "",
    last_modified: str | None = None,
) -> dict[str, Any]:
    payload = {
        "id": sid,
        "webTitle": title,
        "webPublicationDate": published,
        "bodyText": body,
    }
    if last_modified is not None:
        payload["lastModified"] = last_modified
    return {"sid": sid, "payload": payload}


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
        ' "side": "home", "player": "Saka", "subtype": "out",'
        ' "novelty": "first", "evidence_quote": "Saka is out.",'
        ' "confidence": 0.8}]}\n```'
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
            return {
                "filtered": False,
                "parsed": {
                    "relevant": True,
                    "events": [
                        {
                            "type": "injury",
                            "side": "home",
                            "player": "Saka",
                            "subtype": "out",
                            "novelty": "first",
                            "evidence_quote": "Saka is out injured.",
                            "confidence": 0.9,
                        }
                    ],
                },
                "model": "fake-model",
                "cost": 0.0,
                "empty": False,
            }
        return {
            "filtered": False,
            "parsed": {"relevant": False, "events": []},
            "model": "fake-model",
            "cost": 0.0,
            "empty": False,
        }

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
                    "subtype": "out",
                    "novelty": "first",
                    "evidence_quote": "He is out.",
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
    """断点续跑：复合键已有条目不再抽取；成功后 checkpoint 清除。"""
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
    links_map, _ = gs.english_links(
        con, gs.fixture_index(con, "E0", ("2023-24",)), "E0"
    )
    candidates, _, _ = gs.candidate_articles(store, links_map)
    checkpoint = gs._checkpoint_path(store)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        json.dumps(
            {
                "key": gs.checkpoint_key(candidates[0]),
                "article_sid": "a1",
                "sid": "s1",
                "body_hash": "deadbeef",
                "extractor_version": gs.EVENT_VERSION,
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
        return {
            "filtered": False,
            "parsed": None,
            "model": "should-not-run",
            "cost": 0.0,
            "empty": True,
        }

    report = gs.build_news_events(
        ctx, store, seasons=("2023-24",), extractor=counting_extractor
    )
    assert calls == []  # 全部命中 checkpoint，零新调用
    assert report.model == "cached"
    assert not checkpoint.exists()  # 成功后清除
    store.close()
    con.close()


# ---- 票 02：复合键与版本组 ----


def _adjacent_duck() -> duckdb.DuckDBPyConnection:
    """合成 duck：两场相邻日（同一篇 3 天窗内稿可命中两场）。"""
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
        " ('s2', '英超', TIMESTAMP '2024-03-11 20:00:00', '2023-24',"
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
        " ('E0', '2023-24', '2024-03-11', 'Liverpool', 'Man City', 1, 1)"
    )
    return con


def test_checkpoint_composite_key_one_article_two_fixtures(tmp_path):
    """一稿两场+中途熔断重放：两场各自落对、无交叉污染（复合键）。"""
    from goalx_backend.config import Settings

    con = _adjacent_duck()
    store = CorpusStore(tmp_path)
    # 标题同时命中 s1 主队 Arsenal 与 s2 客队 Man City → 一稿两场
    store.append_bronze(
        gs.PROVIDER,
        gs.BRONZE_DATASET,
        [
            _bronze_row(
                "dual",
                "Arsenal prepare as Man City rotation looms",
                "2024-03-09T10:00:00Z",
                "team news body",
            )
        ],
    )
    ctx = gs.NewsContext(
        duck_con=con,
        conn=None,
        settings=Settings(),  # type: ignore[arg-type]
    )

    calls_first: list[str] = []

    def interrupting_extractor(candidate):
        calls_first.append(f"{candidate.article_sid}:{candidate.link.sid}")
        if candidate.link.sid == "s2":
            raise RuntimeError("熔断：中途断点")  # s1 已入 checkpoint 后断
        return {
            "filtered": False,
            "parsed": {
                "relevant": True,
                "events": [
                    {
                        "type": "injury",
                        "side": "home",
                        "player": "Saka",
                        "subtype": "out",
                        "novelty": "first",
                        "evidence_quote": "Saka is out injured.",
                        "confidence": 0.9,
                    }
                ],
            },
            "model": "run1",
            "cost": 0.0,
            "empty": False,
        }

    with pytest.raises(RuntimeError, match="熔断"):
        gs.build_news_events(
            ctx, store, seasons=("2023-24",), extractor=interrupting_extractor
        )
    checkpoint = gs._checkpoint_path(store)
    assert checkpoint.exists()
    saved = json.loads(checkpoint.read_text(encoding="utf-8").splitlines()[0])
    assert saved["sid"] == "s1"
    assert saved["article_sid"] == "dual"
    assert saved["key"] == gs.checkpoint_key(
        gs.Candidate(
            article_sid="dual",
            published_at="2024-03-09T10:00:00Z",
            title="Arsenal prepare as Man City rotation looms",
            body="team news body",
            link=gs._Link(
                sid="s1",
                kickoff_day="2024-03-10",
                kickoff_iso="2024-03-10T20:00:00",
                en_home="Arsenal",
                en_away="Chelsea",
            ),
            side="home",
            pit_group=gs.PIT_UNPROVEN,
        )
    )

    calls_replay: list[str] = []

    def replay_extractor(candidate):
        calls_replay.append(f"{candidate.article_sid}:{candidate.link.sid}")
        return {
            "filtered": False,
            "parsed": {
                "relevant": True,
                "events": [
                    {
                        "type": "suspension",
                        "side": "away",
                        "player": "Other",
                        "subtype": "return",
                        "novelty": "restate",
                        "evidence_quote": "He is back from suspension.",
                        "confidence": 0.7,
                    }
                ],
            },
            "model": "run2",
            "cost": 0.0,
            "empty": False,
        }

    report = gs.build_news_events(
        ctx, store, seasons=("2023-24",), extractor=replay_extractor
    )
    # s1 复用断点（run1 结果），只重抽 s2——无交叉污染
    assert calls_replay == ["dual:s2"]
    assert report.events == 2  # s1 一条（run1）+ s2 一条（run2）
    import pyarrow.parquet as pq

    rows = pq.read_table(
        store.silver_path(gs.PROVIDER, gs.EVENT_DATASET) / "all"
    ).to_pylist()
    by_sid = {r["sid"]: r for r in rows}
    assert by_sid["s1"]["event_type"] == "injury"  # run1 的结果落在 s1
    assert by_sid["s1"]["model"] == "run1"
    assert by_sid["s2"]["event_type"] == "suspension"  # run2 的结果落在 s2
    assert by_sid["s2"]["model"] == "run2"
    assert not gs._checkpoint_path(store).exists()  # 全量成功后清除
    store.close()
    con.close()


def test_checkpoint_legacy_lines_ignored_reextract(tmp_path):
    """旧格式行（无 key）不迁移：跳过=按新键自然重抽。"""
    from goalx_backend.config import Settings

    con = _pilot_duck()
    store = CorpusStore(tmp_path)
    store.append_bronze(
        gs.PROVIDER,
        gs.BRONZE_DATASET,
        [_bronze_row("a1", "Arsenal news", "2024-03-09T10:00:00Z", "body")],
    )
    checkpoint = gs._checkpoint_path(store)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        json.dumps(
            {
                "article_sid": "a1",
                "filtered": False,
                "parsed": {"relevant": True, "events": []},
                "model": "legacy",
                "cost": 0.0,
                "empty": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    def counting(candidate):
        calls.append(candidate.article_sid)
        return {
            "filtered": False,
            "parsed": {"relevant": False, "events": []},
            "model": "fresh",
            "cost": 0.0,
            "empty": False,
        }

    report = gs.build_news_events(
        gs.NewsContext(duck_con=con, conn=None, settings=Settings()),  # type: ignore[arg-type]
        store,
        seasons=("2023-24",),
        extractor=counting,
    )
    assert calls == ["a1"]  # 旧行被忽略，重抽
    assert report.model == "fresh"
    store.close()
    con.close()


def test_pit_group_from_last_modified(tmp_path):
    """版本组：lastModified<开球=strict_pit；≥开球或缺失=unproven。"""
    store = CorpusStore(tmp_path)
    links = {
        "s1": gs._Link(
            sid="s1",
            kickoff_day="2024-03-10",
            kickoff_iso="2024-03-10T20:00:00",  # 北京 20:00 = UTC 12:00
            en_home="Arsenal",
            en_away="Chelsea",
        )
    }
    rows = [
        _bronze_row(
            "strict",
            "Arsenal news",
            "2024-03-08T10:00:00Z",
            "b",
            last_modified="2024-03-09T10:00:00Z",  # 早于开球 → strict
        ),
        _bronze_row(
            "late",
            "Arsenal news late edit",
            "2024-03-08T10:00:00Z",
            "b",
            last_modified="2024-03-10T13:00:00Z",  # 晚于开球 → unproven
        ),
        _bronze_row("absent", "Arsenal news no lm", "2024-03-08T10:00:00Z", "b"),
    ]
    store.append_bronze(gs.PROVIDER, gs.BRONZE_DATASET, rows)
    candidates, _, _ = gs.candidate_articles(store, links)
    groups = {c.article_sid: c.pit_group for c in candidates}
    assert groups == {
        "strict": gs.PIT_STRICT,
        "late": gs.PIT_UNPROVEN,
        "absent": gs.PIT_UNPROVEN,
    }
    store.close()


def test_naive_timestamps_conservative_not_fatal(tmp_path):
    """tz-strict：naive lastModified/webPublicationDate 保守降级不崩预筛。"""
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
        # naive lastModified → 不崩，组=unproven（毒丸行不再卡死重放）
        _bronze_row(
            "naive-lm",
            "Arsenal news",
            "2024-03-08T10:00:00Z",
            "b",
            last_modified="2024-03-09 11:00:00",
        ),
        # naive webPublicationDate → 不产候选（不崩）
        _bronze_row(
            "naive-pub",
            "Arsenal news pub",
            "2024-03-08 10:00:00",
            "b",
        ),
        # lastModified 恰等开球（UTC 12:00）→ 严格 < 不满足 → unproven
        _bronze_row(
            "boundary",
            "Arsenal news boundary",
            "2024-03-08T10:00:00Z",
            "b",
            last_modified="2024-03-10T12:00:00Z",
        ),
    ]
    store.append_bronze(gs.PROVIDER, gs.BRONZE_DATASET, rows)
    candidates, _, _ = gs.candidate_articles(store, links)  # 不 raise 即过关
    by_sid = {c.article_sid: c for c in candidates}
    assert set(by_sid) == {"naive-lm", "boundary"}  # naive-pub 不产候选
    assert by_sid["naive-lm"].pit_group == gs.PIT_UNPROVEN
    assert by_sid["boundary"].pit_group == gs.PIT_UNPROVEN
    store.close()


# ---- 票 07：schema v2 与有界扩样 ----


def test_parse_extraction_v2_fail_loud() -> None:
    """v2 新字段违例 fail-loud：缺 subtype/novelty/evidence_quote 均 None。"""
    base = '{"relevant": true, "events": [%s]}'
    ok = (
        '{"type": "injury", "side": "home", "subtype": "out",'
        ' "novelty": "first", "evidence_quote": "He is out.",'
        ' "confidence": 0.8}'
    )
    assert gs.parse_extraction(base % ok) is not None
    missing_subtype = (
        '{"type": "injury", "side": "home", "novelty": "first",'
        ' "evidence_quote": "q", "confidence": 0.8}'
    )
    assert gs.parse_extraction(base % missing_subtype) is None
    bad_novelty = (
        '{"type": "injury", "side": "home", "subtype": "out",'
        ' "novelty": "maybe", "evidence_quote": "q", "confidence": 0.8}'
    )
    assert gs.parse_extraction(base % bad_novelty) is None
    missing_evidence = (
        '{"type": "injury", "side": "home", "subtype": "out",'
        ' "novelty": "first", "confidence": 0.8}'
    )
    assert gs.parse_extraction(base % missing_evidence) is None
    subtype_on_other = (
        '{"type": "manager_change", "side": "home", "subtype": "out",'
        ' "novelty": "first", "evidence_quote": "q", "confidence": 0.8}'
    )
    assert gs.parse_extraction(base % subtype_on_other) is None


def test_kickoff_utc_write_side_and_dual_read() -> None:
    """写侧 UTC：v2 行 aware ISO；读侧双模 v1 墙钟/v2 UTC 一致。"""
    from goalx_backend.data.fixtures import kickoff_to_utc, kickoff_utc_iso

    utc_iso = kickoff_utc_iso("2024-03-10T20:00:00")
    assert utc_iso == "2024-03-10T12:00:00+00:00"
    # 双模读：v2 aware 与 v1 naive 同时刻
    assert kickoff_to_utc(utc_iso) == kickoff_to_utc("2024-03-10T20:00:00")
    # 非法写侧 raise（fail-loud）
    import pytest

    with pytest.raises(ValueError, match="非法"):
        kickoff_utc_iso("garbage")


def test_fixture_budget_bounded_by_independent_fixtures(tmp_path) -> None:
    """fixture_budget=按独立场次截断候选（预算按场次不按事件条数）。"""
    from goalx_backend.config import Settings

    con = _adjacent_duck()
    store = CorpusStore(tmp_path)
    store.append_bronze(
        gs.PROVIDER,
        gs.BRONZE_DATASET,
        [
            _bronze_row("a1", "Arsenal news", "2024-03-09T10:00:00Z", "b1"),
            _bronze_row("a2", "Man City news", "2024-03-10T10:00:00Z", "b2"),
            _bronze_row("a3", "Arsenal late", "2024-03-10T11:00:00Z", "b3"),
        ],
    )
    calls: list[str] = []

    def extractor(candidate):
        calls.append(candidate.article_sid)
        return {
            "filtered": False,
            "parsed": {"relevant": False, "events": []},
            "model": "fake",
            "cost": 0.0,
            "empty": False,
        }

    report = gs.build_news_events(
        gs.NewsContext(duck_con=con, conn=None, settings=Settings()),  # type: ignore[arg-type]
        store,
        seasons=("2023-24",),
        fixture_budget=1,  # 只跑 s1 一场：a1+a3 两稿都在 s1
        extractor=extractor,
    )
    assert sorted(calls) == ["a1", "a3"]  # s1 的两稿；s2 的 a2 截断
    assert report.llm_calls == 2
    store.close()
    con.close()


def test_convergence_table_levels(tmp_path) -> None:
    """收敛表：候选→独立场次→有预测→可检验 逐级（表先行）。"""
    from goalx_backend.config import Settings
    from goalx_backend.db import connect, migrate
    from goalx_backend.evaluation import metrics as ev_latest

    con = _pilot_duck()
    face = connect(":memory:")
    migrate(face)
    face.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at)"
        " VALUES ('r', '{}', 'done', '2026-10-16T00:00:00+00:00')"
    )
    # s1（Arsenal 场）有预测+赛果；s2 无
    for key, ftr in (("s1", "H"), ("s2", None)):
        face.execute(
            "INSERT INTO backtest_predictions"
            " (run_id, match_key, competition, season, match_date, home_team,"
            "  away_team, era, ftr, had_probs, fair_probs, fair_source,"
            "  model_fingerprint, train_window_end)"
            " VALUES (1, ?, 'E0', '2023-24', '2024-03-10', 'A', 'B', NULL, ?,"
            "  '{}', '{}', 'psc', 'fp', '2024-03-01')",
            (key, ftr),
        )
    store = CorpusStore(tmp_path)
    store.append_bronze(
        gs.PROVIDER,
        gs.BRONZE_DATASET,
        [_bronze_row("a1", "Arsenal injury news", "2024-03-09T10:00:00Z", "body")],
    )
    predicted, tested = ev_latest.latest_done_run_prediction_sids(face)
    table = gs.convergence_table(
        gs.NewsContext(duck_con=con, conn=face, settings=Settings()),  # type: ignore[arg-type]
        store,
        predicted,
        tested,
        competition="E0",
        seasons=("2023-24",),
    )
    season = table["per_season"]["2023-24"]
    assert season["n_candidates"] == 1
    assert season["n_fixtures_with_news"] == 1
    assert season["n_fixtures_with_predictions"] == 1
    assert season["n_fixtures_testable"] == 1
    assert table["total"]["fixtures_testable"] == 1
    store.close()
    face.close()
    con.close()
