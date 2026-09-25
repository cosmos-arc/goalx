"""jc_sp_change_event silver 测试（票 72）：事件语义同构 + 幂等字节一致 + duckdb 视图。

样本行值照 2026-09-26 实测裁剪（官方域）。语义口径先例 test_ingest_srct_odds。
"""

from __future__ import annotations

import json
from pathlib import Path

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import jc, jc_silver


def _had(
    h: str, d: str, a: str, date: str = "2026-09-23", time: str = "09:17:34"
) -> dict:
    return {"h": h, "d": d, "a": a, "updateDate": date, "updateTime": time}


def _hhad(
    h: str,
    d: str,
    a: str,
    goal_line: str,
    date: str = "2026-09-23",
    time: str = "09:17:34",
) -> dict:
    return {**_had(h, d, a, date, time), "goalLine": goal_line}


def _ttg(sp: str, date: str = "2026-09-23", time: str = "09:17:34") -> dict:
    return {
        **{f"s{i}": "9.90" for i in range(8)},
        "s3": sp,
        "updateDate": date,
        "updateTime": time,
        "goalLine": "",
    }


def _bronze_row(
    match_id: str, had: list[dict], hhad: list[dict], ttg: list[dict]
) -> dict:
    value = {
        "oddsHistory": {
            "matchId": int(match_id),
            "leagueAbbName": "国际赛",
            "hadList": had,
            "hhadList": hhad,
            "ttgList": ttg,
        }
    }
    return {
        "provider": jc.JC_PROVIDER,
        "dataset": jc.SP_DATASET,
        "sid": match_id,
        "fetched_at": "2026-09-26T01:00:00+00:00",
        "parser_version": jc.BRONZE_VERSION,
        "raw_sha": "x",
        "payload": value,
    }


def _store_with(tmp_path: Path, rows: list[dict]) -> CorpusStore:
    settings = Settings(corpus_root=tmp_path / "corpus")
    store = CorpusStore(settings.corpus_root)
    store.ensure_tree()
    store.append_bronze(jc.JC_PROVIDER, jc.SP_DATASET, rows)
    return store


def _rows_of(report_rows: list[dict], **where: str) -> list[dict]:
    """事件行过滤助手（match_id/playtype/outcome 等值筛）。"""
    return [
        r for r in report_rows if all(str(r.get(k)) == str(v) for k, v in where.items())
    ]


def _build(store: CorpusStore) -> tuple[jc_silver.SilverJcSpReport, list[dict]]:
    report = jc_silver.build_sp_change_events(store)
    import pyarrow.parquet as pq

    table = pq.read_table(
        store.root
        / "silver"
        / jc.JC_PROVIDER
        / jc_silver.SP_EVENT_DATASET
        / "data.parquet"
    )
    return report, table.to_pylist()


def test_heartbeat_and_swing_semantics(tmp_path: Path) -> None:
    """心跳丢/A→B→A 保留/首条保留（同构核心）。"""
    store = _store_with(
        tmp_path,
        [
            _bronze_row(
                "100",
                # A→A(心跳)→B→A(回摆保留)：4 源行 → 3 事件
                had=[
                    _had("2.0", "3.0", "3.5", time="09:00:00"),
                    _had("2.0", "3.0", "3.5", time="10:00:00"),
                    _had("1.9", "3.1", "3.6", time="11:00:00"),
                    _had("2.0", "3.0", "3.5", time="12:00:00"),
                ],
                hhad=[],
                ttg=[],
            )
        ],
    )
    report, rows = _build(store)
    store.close()
    had_rows = _rows_of(rows, match_id="100", playtype="had")
    assert len(had_rows) == 3
    assert report.events_had == 3
    assert report.heartbeat_dropped == 1
    assert report.unexplained_gap == 0


def test_hhad_goal_line_in_value_group(tmp_path: Path) -> None:
    """盘口变=事件（story 19）：SP 全等但 goalLine 变 → 保留。"""
    store = _store_with(
        tmp_path,
        [
            _bronze_row(
                "100",
                had=[],
                hhad=[
                    _hhad("2.3", "4.8", "2.06", "-4", time="09:00:00"),
                    _hhad("2.3", "4.8", "2.06", "-3", time="10:00:00"),
                    _hhad("2.3", "4.8", "2.06", "-3", time="11:00:00"),
                ],
                ttg=[],
            )
        ],
    )
    report, rows = _build(store)
    store.close()
    hhad_rows = _rows_of(rows, match_id="100", playtype="hhad")
    assert len(hhad_rows) == 2  # 盘口变保留；同值心跳丢
    assert {r["goal_line"] for r in hhad_rows} == {-4.0, -3.0}
    assert report.unexplained_gap == 0


def test_ttg_long_format_outcomes(tmp_path: Path) -> None:
    """ttg 八档长格式（story 20）：s3 档独立轨迹，其余档全程心跳。"""
    store = _store_with(
        tmp_path,
        [
            _bronze_row(
                "100",
                had=[],
                hhad=[],
                ttg=[
                    _ttg("5.65", time="09:00:00"),
                    _ttg("5.65", time="10:00:00"),  # s3 心跳
                    _ttg("4.95", time="11:00:00"),  # 仅 s3 变
                ],
            )
        ],
    )
    report, rows = _build(store)
    store.close()
    ttg_rows = _rows_of(rows, match_id="100", playtype="ttg")
    # 八档各 1 事件（首条）+ s3 档再 1 事件（变化）= 9
    assert len(ttg_rows) == 9
    s3 = _rows_of(rows, match_id="100", playtype="ttg", outcome="3")
    assert len(s3) == 2
    assert {r["sp"] for r in s3} == {5.65, 4.95}
    # 记账：3 源行 × 8 档 = 24；事件 9 + 心跳 15
    assert report.source_rows == 24
    assert report.events_ttg == 9
    assert report.heartbeat_dropped == 15
    assert report.unexplained_gap == 0


def test_same_timestamp_tiebreak_by_source_order(tmp_path: Path) -> None:
    """同刻并列：source_order 定序（确定性，非声称真实先后）。"""
    store = _store_with(
        tmp_path,
        [
            _bronze_row(
                "100",
                had=[
                    _had("2.0", "3.0", "3.5", time="09:00:00"),
                    _had("1.5", "3.2", "4.0", time="09:00:00"),  # 同刻异值
                ],
                hhad=[],
                ttg=[],
            )
        ],
    )
    report, rows = _build(store)
    store.close()
    had_rows = _rows_of(rows, match_id="100", playtype="had")
    assert len(had_rows) == 2
    assert [r["source_order"] for r in had_rows] == [0, 1]
    assert report.same_minute_conflicts == 1


def test_bad_time_rows_accounted(tmp_path: Path) -> None:
    store = _store_with(
        tmp_path,
        [
            _bronze_row(
                "100",
                had=[
                    _had("2.0", "3.0", "3.5", date="bad", time="09:00:00"),
                    _had("2.1", "3.0", "3.4", time="09:30:00"),
                ],
                hhad=[],
                ttg=[],
            )
        ],
    )
    report, rows = _build(store)
    store.close()
    assert report.bad_time_rows == 1
    assert len(_rows_of(rows, match_id="100", playtype="had")) == 1
    assert report.unexplained_gap == 0


def test_latest_row_wins_and_idempotent_bytes(tmp_path: Path) -> None:
    """拍行/收口行同 sid 共存：后行（全量轨迹）胜出；重建字节级一致。"""
    store = _store_with(
        tmp_path,
        [
            _bronze_row("100", [_had("2.0", "3.0", "3.5")], [], []),  # 在售期拍行
            _bronze_row(  # 收口全量行（后行胜出）
                "100",
                [
                    _had("2.0", "3.0", "3.5", time="09:00:00"),
                    _had("1.8", "3.2", "3.8", time="20:00:00"),
                ],
                [],
                [],
            ),
        ],
    )
    report1, _first_rows = _build(store)
    path = (
        store.root
        / "silver"
        / jc.JC_PROVIDER
        / jc_silver.SP_EVENT_DATASET
        / "data.parquet"
    )
    first_bytes = path.read_bytes()
    report2, _rows2 = _build(store)  # 幂等重物化
    store.close()
    assert report1.events_had == 2  # 全量轨迹（拍行被收口行覆盖）
    assert report2.events_had == 2
    assert path.read_bytes() == first_bytes  # 字节级一致


def test_duckdb_view_queryable(tmp_path: Path) -> None:
    """一场的官方 HAD/HHAD/TTG 轨迹各一条 SQL 可取（验收）。"""
    import duckdb

    store = _store_with(
        tmp_path,
        [
            _bronze_row(
                "100",
                [_had("2.0", "3.0", "3.5"), _had("1.9", "3.1", "3.6", time="10:00:00")],
                [_hhad("2.3", "4.8", "2.06", "-4")],
                [_ttg("5.65")],
            )
        ],
    )
    jc_silver.build_sp_change_events(store)
    from goalx_backend.data import corpus_duckdb

    corpus_duckdb.build_corpus_duckdb(store)
    store.close()
    corpus_db = (
        Settings(corpus_root=tmp_path / "corpus").corpus_root / "duckdb/corpus.duckdb"
    )
    con = duckdb.connect(str(corpus_db), read_only=True)
    try:
        counts = con.execute(
            "SELECT playtype, COUNT(*) FROM jc_sp_change_event "
            "GROUP BY playtype ORDER BY playtype"
        ).fetchall()
        assert dict(counts) == {"had": 2, "hhad": 1, "ttg": 8}
        traj = con.execute(
            "SELECT published_at FROM jc_sp_change_event "
            "WHERE match_id='100' AND playtype='had' "
            "ORDER BY published_at"
        ).fetchall()
        assert len(traj) == 2
    finally:
        con.close()


def test_kickoff_from_ledger(tmp_path: Path) -> None:
    store = _store_with(
        tmp_path, [_bronze_row("100", [_had("2.0", "3.0", "3.5")], [], [])]
    )
    store.upsert_jc_shift_match(
        {
            "match_id": "100",
            "league": "国际赛",
            "home": "中国",
            "away": "新西兰",
            "kickoff_utc": "2026-09-26T19:00:00+00:00",
            "beats": 1,
            "finalized": 0,
        }
    )
    _report, rows = _build(store)
    store.close()
    assert rows[0]["kickoff"] is not None
    assert rows[0]["league"] == "国际赛"


def test_meta_written(tmp_path: Path) -> None:
    store = _store_with(
        tmp_path, [_bronze_row("100", [_had("2.0", "3.0", "3.5")], [], [])]
    )
    jc_silver.build_sp_change_events(store)
    meta_path = (
        store.root
        / "silver"
        / jc.JC_PROVIDER
        / jc_silver.SP_EVENT_DATASET
        / "_meta.json"
    )
    store.close()
    meta = json.loads(meta_path.read_text())
    assert meta["silver_version"] == jc_silver.SILVER_VERSION
    assert meta["unexplained_gap"] == 0
