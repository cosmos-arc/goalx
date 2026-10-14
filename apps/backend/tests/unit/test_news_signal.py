"""事件信号 PIT 回放（model-iterate 票 05）的行为断言。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.evaluation import news_signal as nsw


def _seed_run(face: sqlite3.Connection) -> int:
    cur = face.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at)"
        " VALUES ('r5', ?, 'done', '2026-10-14T00:00:00+00:00')",
        (json.dumps({"haircut": 0.1}),),
    )
    return int(cur.lastrowid or 0)


def _seed_pred(face: sqlite3.Connection, run_id: int, key: str, ftr: str = "H") -> None:
    fair = {"h": 0.4, "d": 0.3, "a": 0.3}
    face.execute(
        """
        INSERT INTO backtest_predictions
        (run_id, match_key, competition, season, match_date, home_team, away_team,
         era, ftr, had_probs, fair_probs, fair_source, model_fingerprint,
         train_window_end)
        VALUES (?, ?, 'E0', '2023-24', '2024-03-10', 'A', 'B', NULL, ?, ?, ?,
                'psc', 'fp', '2024-03-01')
        """,
        (run_id, key, ftr, json.dumps(fair), json.dumps(fair)),
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
                    pa.field("model", pa.string()),
                    pa.field("version", pa.string()),
                ]
            ),
        ),
        root / "part-0.parquet",
    )


def _event(sid: str, etype: str = "injury") -> dict[str, Any]:
    return {
        "article_sid": f"art-{sid}-{etype}",
        "sid": sid,
        "league": "E0",
        "season": "2023-24",
        "published_at": "2024-03-09T10:00:00Z",
        "kickoff_day": "2024-03-10",
        "kickoff_iso": "2024-03-10T20:00:00",
        "event_type": etype,
        "side": "home",
        "player": None,
        "confidence": 0.8,
        "model": "test",
        "version": "1",
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


def test_news_signal_faces(gold_env, tmp_path):
    """事件面/残差对照/price-in 三面落位；报告双视图落盘。"""
    with gold_env(_gold_env_rows(20)) as (face, duck_con, _store):
        run_id = _seed_run(face)
        for i in range(20):
            _seed_pred(face, run_id, f"s{i}", "H" if i % 2 else "A")
        report_store = CorpusStore(tmp_path)
        # 事件覆盖 s0..s3（事件场 realized h=+0.6 全 H；对照场混合）
        _write_events(
            report_store,
            [_event("s0"), _event("s1", "suspension"), _event("s2"), _event("s3")],
        )
        payload = nsw.build_news_signal_report(
            face, duck_con, report_store, run_id=run_id
        )
        assert payload["event_face"]["n_events"] == 4
        assert payload["event_face"]["n_fixtures_with_events"] == 4
        assert payload["event_face"]["by_type"]["injury"] == 3
        # 残差对照：事件场均值=三向 realized 均值（H:0.6/D:-0.3/A:-0.3 → 0）
        event_stats = payload["residual_contrast"]["event"]
        assert event_stats["n"] == 12  # 4 场 × 3 向
        assert event_stats["mean"] == pytest.approx(0.0, abs=0.01)
        assert payload["residual_contrast"]["control"]["n"] > 0
        # price-in：q1h 2.4→close 2.0 主队概率上移 → 均值>0
        assert payload["price_in"]["n"] == 4
        assert payload["price_in"]["mean"] > 0
        md = (tmp_path / "reports" / "news-signal.md").read_text(encoding="utf-8")
        assert "残差对照" in md
        assert "price-in" in md
        report_store.close()


def test_news_signal_empty_events_honest(gold_env, tmp_path):
    """事件数据集缺席：事件面 0 如实，不冒充结论。"""
    with gold_env(_gold_env_rows(5)) as (face, duck_con, _store):
        run_id = _seed_run(face)
        for i in range(5):
            _seed_pred(face, run_id, f"s{i}")
        report_store = CorpusStore(tmp_path)
        payload = nsw.build_news_signal_report(
            face, duck_con, report_store, run_id=run_id
        )
        assert payload["event_face"]["n_events"] == 0
        assert payload["event_face"]["n_fixtures_with_events"] == 0
        assert payload["price_in"]["n"] == 0
        report_store.close()


def test_placebo_sids_deterministic_and_bounded():
    pool = [f"x{i}" for i in range(100)]
    first = nsw._placebo_sids(pool, 10)
    second = nsw._placebo_sids(pool, 10)
    assert first == second  # 确定性
    assert len(first) == 10
    assert nsw._placebo_sids([], 10) == []


def test_devig_home_guards():
    assert nsw._devig_home(2.0, 3.4, 3.8) is not None
    assert nsw._devig_home(None, 3.4, 3.8) is None
    assert nsw._devig_home(0.9, 3.4, 3.8) is None
    assert nsw._devig_home(float("nan"), 3.4, 3.8) is None
