"""事件信号 PIT 回放（caliber-loop 票 01：场次级口径）的行为断言。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.fixtures import beijing_naive_to_utc
from goalx_backend.evaluation import news_signal as nsw


def _seed_run(face: sqlite3.Connection) -> int:
    cur = face.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at)"
        " VALUES ('r5', ?, 'done', '2026-10-14T00:00:00+00:00')",
        (json.dumps({"haircut": 0.1}),),
    )
    return int(cur.lastrowid or 0)


def _seed_pred(
    face: sqlite3.Connection,
    run_id: int,
    key: str,
    *,
    ftr: str = "H",
    fair: dict[str, float] | None = None,
    match_date: str = "2024-03-10",
    competition: str = "E0",
    season: str = "2023-24",
) -> None:
    probs = fair or {"h": 0.4, "d": 0.3, "a": 0.3}
    face.execute(
        """
        INSERT INTO backtest_predictions
        (run_id, match_key, competition, season, match_date, home_team, away_team,
         era, ftr, had_probs, fair_probs, fair_source, model_fingerprint,
         train_window_end)
        VALUES (?, ?, ?, ?, ?, 'A', 'B', NULL, ?, ?, ?,
                'psc', 'fp', '2024-03-01')
        """,
        (
            run_id,
            key,
            competition,
            season,
            match_date,
            ftr,
            json.dumps(probs),
            json.dumps(probs),
        ),
    )


def _write_events(store: CorpusStore, rows: list[dict[str, Any]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = store.silver_path("guardian", "news_event") / "all"
    root.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(
            rows,
            schema=pa.schema(
                [
                    pa.field("article_sid", pa.string()),
                    pa.field("sid", pa.string()),
                    pa.field("league", pa.string()),
                    pa.field("season", pa.string()),
                    pa.field("published_at", pa.string()),
                    pa.field("kickoff_day", pa.string()),
                    pa.field("kickoff_iso", pa.string()),
                    pa.field("event_type", pa.string()),
                    pa.field("side", pa.string()),
                    pa.field("player", pa.string()),
                    pa.field("confidence", pa.float64()),
                    pa.field("pit_group", pa.string()),
                    pa.field("model", pa.string()),
                    pa.field("version", pa.string()),
                ]
            ),
        ),
        root / "part-0.parquet",
    )


def _event(
    sid: str,
    *,
    etype: str = "injury",
    side: str = "home",
    article: str | None = None,
    published_at: str = "2024-03-09T10:00:00Z",
    kickoff_iso: str = "2024-03-10T20:00:00",
) -> dict[str, Any]:
    return {
        "article_sid": article or f"art-{sid}-{etype}-{side}",
        "sid": sid,
        "league": "E0",
        "season": "2023-24",
        "published_at": published_at,
        "kickoff_day": kickoff_iso[:10],
        "kickoff_iso": kickoff_iso,
        "event_type": etype,
        "side": side,
        "player": None,
        "confidence": 0.8,
        "pit_group": "strict_pit",
        "model": "test",
        "version": "2",
    }


def _gold_env_rows(count: int) -> list[dict[str, Any]]:
    import datetime as dt

    rows = []
    for i in range(count):
        rows.append(
            {
                "sid": f"s{i}",
                "league": "英超",
                "kickoff": dt.datetime(2024, 3, 10, 20),
                "season": "2023-24",
                "home": f"H{i}",
                "away": f"A{i}",
                "home_goals": 1,
                "away_goals": 0,
                "stage": "常规轮",
                "era": "psc_proxy",
                "admin_excluded": False,
                "close1x2_h": 2.0,
                "close1x2_d": 3.4,
                "close1x2_a": 3.8,
                "q1h1x2_h": 2.4,
                "q1h1x2_d": 3.4,
                "q1h1x2_a": 3.8,
                "version": "gold-test",
            }
        )
    return rows


def test_fixture_level_uniqueness_and_matched_contrast(gold_env, tmp_path):
    """主面=fixture×side 唯一：同场同侧两事件只计一个样本；对照=匹配池。"""
    with gold_env(_gold_env_rows(20)) as (face, duck_con, _store):
        run_id = _seed_run(face)
        for i in range(20):
            _seed_pred(face, run_id, f"s{i}", ftr="H" if i % 2 else "A")
        report_store = CorpusStore(tmp_path)
        # s0 同场同侧两条事件（剂量 2）；s1 一条 → h 侧样本=2、独立场次=2
        _write_events(
            report_store,
            [
                _event("s0", article="a1"),
                _event("s0", etype="suspension", article="a2"),
                _event("s1"),
            ],
        )
        payload = nsw.build_news_signal_report(
            face, duck_con, report_store, run_id=run_id
        )
        fixture = payload["fixture_face"]
        assert payload["event_face"]["n_events"] == 3
        assert fixture["n_fixtures"] == 2  # headline 独立场次数
        assert fixture["n_samples"] == 2  # 同场同侧两事件只计一次
        h_face = fixture["by_side"]["h"]
        assert h_face["event"]["n"] == 2
        assert h_face["dose"]["2+"]["n"] == 1  # s0 剂量=2+
        assert h_face["dose"]["1"]["n"] == 1
        # 对照=s2..s19 同联赛+同季+同周+同档（fair h=0.4 → 档 2）
        assert h_face["control"]["n"] == 18
        assert h_face["unmatched_event_samples"] == 0
        # 全部行同 ISO 周 → 周分块退化，区间诚实置 None（点估计仍报）
        assert h_face["week_block_bootstrap"] is None
        assert h_face["diff"] is not None
        # 事件加权次面保留条数口径并标注非独立
        assert payload["event_weighted_secondary"]["event_side_anchored"]["n"] == 3
        assert "非独立样本" in payload["event_weighted_secondary"]["label"]
        assert payload["match_params"]["prob_band_width"] == 0.2
        md = (tmp_path / "reports" / "news-signal.md").read_text(encoding="utf-8")
        assert "独立场次 2" in md  # headline 先报独立场次数
        report_store.close()


def test_matched_control_predicate_dimensions(gold_env, tmp_path):
    """匹配谓词四维：联赛/季/周窗/概率档——逐维排除，匹配不上剔除计数。"""
    with gold_env(_gold_env_rows(2)) as (face, duck_con, _store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s0", ftr="H")  # 事件场
        _seed_pred(face, run_id, "s1", ftr="H")  # 唯一合格对照（全维相同）
        report_store = CorpusStore(tmp_path)
        _write_events(report_store, [_event("s0")])
        # 四个反例夹具（无 gold 行也可有预测；price_in 缺行如实为 0）
        _seed_pred(face, run_id, "wrong_band", fair={"h": 0.9, "d": 0.05, "a": 0.05})
        _seed_pred(face, run_id, "wrong_season", season="2024-25")
        _seed_pred(face, run_id, "wrong_week", match_date="2024-04-28")
        _seed_pred(face, run_id, "wrong_league", competition="SP1")
        payload = nsw.build_news_signal_report(
            face, duck_con, report_store, run_id=run_id
        )
        h_face = payload["fixture_face"]["by_side"]["h"]
        assert h_face["control"]["n"] == 1  # 只有 s1 四维全中
        assert h_face["unmatched_event_samples"] == 0
        report_store.close()
        # 孤立事件场（无任何对照）→ 剔除计数、不产 diff
        face.execute("DELETE FROM backtest_predictions WHERE match_key = 's1'")
        report_store2 = CorpusStore(tmp_path / "x")
        _write_events(report_store2, [_event("s0")])
        payload2 = nsw.build_news_signal_report(
            face, duck_con, report_store2, run_id=run_id
        )
        h2 = payload2["fixture_face"]["by_side"]["h"]
        assert h2["control"]["n"] == 0
        assert h2["event"]["n"] == 0
        assert h2["unmatched_event_samples"] == 1
        assert h2["diff"] is None
        report_store2.close()


def test_kickoff_true_utc_conversion(gold_env, tmp_path):
    """时距=北京墙钟−8h 真 UTC（旧标签替换口径恒高估 8h）。"""
    with gold_env(_gold_env_rows(1)) as (face, duck_con, _store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s0")
        report_store = CorpusStore(tmp_path)
        # 北京 20:00 = UTC 12:00；发布 10:00Z → 真时距 2.0h（旧口径=10.0h）
        _write_events(
            report_store,
            [
                _event(
                    "s0",
                    kickoff_iso="2024-03-10T20:00:00",
                    published_at="2024-03-10T10:00:00Z",
                )
            ],
        )
        payload = nsw.build_news_signal_report(
            face, duck_con, report_store, run_id=run_id
        )
        assert payload["event_face"]["hours_to_kickoff_median"] == 2.0
        report_store.close()


def test_beijing_naive_to_utc_shared_source():
    """单一真相源：与抽取器同函数，−8h 真转换非标签替换。"""
    ko = beijing_naive_to_utc("2024-03-10T20:00:00")
    assert ko is not None
    assert ko.isoformat() == "2024-03-10T12:00:00+00:00"
    assert beijing_naive_to_utc("2024-03-10 20:00:00") == ko  # 空格分隔同义
    assert beijing_naive_to_utc("garbage") is None
    # aware 输入 fail-loud（None）：重贴时区标签=被修掉的 −8h 偏差来源
    assert beijing_naive_to_utc("2024-08-17T11:30:00+00:00") is None
    assert beijing_naive_to_utc("2024-08-17T11:30:00Z") is None


def test_bootstrap_deterministic_and_degenerate(gold_env, tmp_path):
    """周分块 bootstrap 固定种子确定性；单周簇退化=诚实置 None。"""
    with gold_env(_gold_env_rows(10)) as (face, duck_con, _store):
        run_id = _seed_run(face)
        for i in range(10):
            # 事件场与对照分两个 ISO 周（多周簇 → 区间可估）
            _seed_pred(
                face,
                run_id,
                f"s{i}",
                ftr="H" if i % 2 else "A",
                match_date="2024-03-10" if i % 2 else "2024-03-03",
            )
        report_store = CorpusStore(tmp_path)
        _write_events(report_store, [_event("s0"), _event("s1")])
        first = nsw.build_news_signal_report(
            face, duck_con, report_store, run_id=run_id
        )
        second = nsw.build_news_signal_report(
            face, duck_con, report_store, run_id=run_id
        )
        assert (
            first["fixture_face"]["by_side"]["h"]["week_block_bootstrap"]
            == second["fixture_face"]["by_side"]["h"]["week_block_bootstrap"]
        )
        report_store.close()
        # 单周簇退化：所有行同周 → 区间置 None（零宽区间=冒充不确定性）
        face.execute("UPDATE backtest_predictions SET match_date='2024-03-10'")
        degenerate_store = CorpusStore(tmp_path / "degenerate")
        _write_events(degenerate_store, [_event("s0")])
        payload_deg = nsw.build_news_signal_report(
            face, duck_con, degenerate_store, run_id=run_id
        )
        h_deg = payload_deg["fixture_face"]["by_side"]["h"]
        assert h_deg["week_block_bootstrap"] is None
        assert h_deg["diff"] is not None  # 点估计仍报，只是区间诚实置空
        degenerate_store.close()


def test_prediction_rows_bad_fair_skipped_not_fatal(gold_env, tmp_path):
    """fair 缺侧键/非 JSON 的行：跳过并计数，不杀整份报告。"""
    with gold_env(_gold_env_rows(3)) as (face, duck_con, _store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s0")
        _seed_pred(face, run_id, "s1", fair={"h": 0.4, "a": 0.6})  # 缺 d
        _seed_pred(face, run_id, "s2")
        face.execute(
            "UPDATE backtest_predictions SET fair_probs='garbage'"
            " WHERE match_key='s2'"  # 非 JSON（列 NOT NULL，坏串走守卫）
        )
        report_store = CorpusStore(tmp_path)
        _write_events(report_store, [_event("s0")])
        payload = nsw.build_news_signal_report(
            face, duck_con, report_store, run_id=run_id
        )
        assert payload["n_prediction_rows_skipped"] == 2
        assert payload["fixture_face"]["n_fixtures"] == 1  # 坏行不再爆炸
        report_store.close()


def test_p90_nearest_rank(gold_env, tmp_path):
    """p90=nearest-rank：n=10 已知时距 [2..11]h → 第 9 小=10.0 而非最大值 11.0。"""
    with gold_env(_gold_env_rows(1)) as (face, duck_con, _store):
        run_id = _seed_run(face)
        report_store = CorpusStore(tmp_path)
        rows = []
        for i in range(10):
            rows.append(
                _event(
                    "s0",
                    article=f"a{i}",
                    kickoff_iso="2024-03-10T20:00:00",  # 北京 20:00 = UTC 12:00
                    published_at=f"2024-03-10T{10 - i:02d}:00:00Z",  # 时距 2..11h
                )
            )
        _write_events(report_store, rows)
        payload = nsw.build_news_signal_report(
            face, duck_con, report_store, run_id=run_id
        )
        assert payload["event_face"]["n_events"] == 10
        assert payload["event_face"]["hours_to_kickoff_p90"] == 10.0
        report_store.close()


def test_news_signal_empty_events_honest(gold_env, tmp_path):
    """事件数据集缺席：各面 0 如实，不冒充结论。"""
    with gold_env(_gold_env_rows(5)) as (face, duck_con, _store):
        run_id = _seed_run(face)
        for i in range(5):
            _seed_pred(face, run_id, f"s{i}")
        report_store = CorpusStore(tmp_path)
        payload = nsw.build_news_signal_report(
            face, duck_con, report_store, run_id=run_id
        )
        assert payload["event_face"]["n_events"] == 0
        assert payload["fixture_face"]["n_fixtures"] == 0
        assert payload["fixture_face"]["n_samples"] == 0
        assert payload["price_in"]["n"] == 0
        report_store.close()


def test_devig_home_guards():
    assert nsw._devig_home(2.0, 3.4, 3.8) is not None
    assert nsw._devig_home(None, 3.4, 3.8) is None
    assert nsw._devig_home(0.9, 3.4, 3.8) is None
    assert nsw._devig_home(float("nan"), 3.4, 3.8) is None


def test_band_edges():
    assert nsw._band(0.0) == 0
    assert nsw._band(0.19) == 0
    assert nsw._band(0.2) == 1
    assert nsw._band(0.6) == 3  # 浮点边界（0.6/0.2=2.999…）epsilon 修正
    assert nsw._band(1.0) == 4  # 收口顶档


def test_pit_group_stratification(gold_env, tmp_path):
    """版本组分层：主结论面=strict_pit；混组保守归 unproven；legacy=unmarked。"""
    with gold_env(_gold_env_rows(6)) as (face, duck_con, _store):
        run_id = _seed_run(face)
        for i in range(6):
            _seed_pred(face, run_id, f"s{i}", ftr="H" if i % 2 else "A")
        report_store = CorpusStore(tmp_path)
        rows = [
            _event("s0"),  # strict（默认）
            _event("s1"),  # strict
            # s2 混组：strict+unproven 同场同侧 → 样本归 unproven
            _event("s2", article="m1"),
            dict(_event("s2", article="m2"), pit_group="unproven"),
        ]
        _write_events(report_store, rows)
        payload = nsw.build_news_signal_report(
            face, duck_con, report_store, run_id=run_id
        )
        h = payload["fixture_face"]["by_side"]["h"]
        assert h["main_pit_groups"] == ["strict_pit"]
        assert h["event"]["n"] == 2  # 主结论面只算 strict 样本
        groups = h["event_by_pit_group"]
        assert groups["strict_pit"]["n"] == 2
        assert groups["unproven"]["n"] == 1  # 混组保守归 unproven
        report_store.close()
        # legacy 无标记 → unmarked 单列，主面回退全体
        report_store2 = CorpusStore(tmp_path / "legacy")
        legacy_rows = [_event("s0"), _event("s1")]
        for r in legacy_rows:
            r.pop("pit_group")
        _write_events(report_store2, legacy_rows)
        payload2 = nsw.build_news_signal_report(
            face, duck_con, report_store2, run_id=run_id
        )
        h2 = payload2["fixture_face"]["by_side"]["h"]
        assert h2["main_pit_groups"] == ["unmarked"]
        assert h2["event"]["n"] == 2
        assert h2["event_by_pit_group"]["unmarked"]["n"] == 2
        report_store2.close()


def test_pit_group_all_unmatched_still_visible(gold_env, tmp_path):
    """strict 全被对照匹配剔除：组视图仍披露 strict，主面如实回退。"""
    with gold_env(_gold_env_rows(3)) as (face, duck_con, _store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s0")  # 事件场：概率档 2（fair h 0.4）
        # 唯一对照 s1 放不同档 → strict 样本全 unmatched
        _seed_pred(face, run_id, "s1", fair={"h": 0.9, "d": 0.05, "a": 0.05})
        report_store = CorpusStore(tmp_path)
        _write_events(report_store, [_event("s0")])
        payload = nsw.build_news_signal_report(
            face, duck_con, report_store, run_id=run_id
        )
        h = payload["fixture_face"]["by_side"]["h"]
        assert h["unmatched_event_samples"] == 1
        assert h["main_pit_groups"] == []  # matched 池空 → 主面无组
        # 组视图（匹配前全体）仍披露 strict 存在——不静默消失
        assert h["event_by_pit_group"]["strict_pit"]["n"] == 1
        report_store.close()
