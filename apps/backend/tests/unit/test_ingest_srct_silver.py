"""源T silver fixture_universe + DuckDB 只读桥测试（票 55/56 切片 14）。

合成日页（结构对齐实测裁剪样本）走 MockTransport 攒 bronze，再走真实
builder/parquet/duckdb 全链——重物化/幂等/分区/只读桥都在真文件上验。
真源网络永不进测试。端点模板一律 `.test` 占位域（代称红线）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pyarrow.parquet as pq
import pytest

from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_silver

DATES = ["2025-10-01", "2025-10-02", "2025-10-03"]
DATE_TO_SID = dict(zip(DATES, ["91001", "91002", "91003"], strict=True))


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """放宽滑窗硬顶（noop sleeper 下 20/min 会忙转到真实时间翻窗）。"""
    from limits import RateLimitItemPerMinute

    monkeypatch.setattr(srct, "_REQUEST_WINDOW", RateLimitItemPerMinute(10**6))


def _day_page(sid: str, league: str = "英超", label: str = "1日20:00") -> bytes:
    row = (
        "<tr height=18 align=center>"
        f"<td><span>{league}</span></td><td>{label}</td><td class=style1>完</td>"
        "<td align=right>主队甲</td>"
        "<td class=style1><font color=blue>1</font>-<font color=red>2</font></td>"
        "<td align=left>客队乙</td>"
        f"<td><a onclick='analysis({sid})'>析</a></td></tr>"
    )
    return f"<html><body><table>{row}</table></body></html>".encode("gb18030")


ODDS_JS = (
    'var matchname_cn="英超";'
    'game=Array("1129|1|X|3.2|3.4|2.1|27|26|47|88|2.9|3.1|2.2|30|27|43|88|'
    '0.85|0.85|0.93|t|");\ngameDetail=Array();\n'
).encode()
HANDICAP_BYTES = (
    "<html><head><title>亚赔变化表</title></head><body><table>"
    "<TR align=center><TD>0.80</TD><TD>平手</TD><TD>1.05</TD>"
    "<TD>10-01 19:29</TD><TD>即</TD></TR></table></body></html>"
).encode("gb18030")
STATS_HTML = (
    '<html><body><script>var jsonData = {"techStat":{"itemList":['
    '{"home":{"value":0.71},"away":{"value":1.68},"name":"预期进球",'
    '"kind":"EXPECTED_GOALS"}]},"info":{}};</script></body></html>'
).encode()


def _settings(tmp_path: Path, db_path: Path | None = None) -> Settings:
    return Settings(
        corpus_root=tmp_path / "corpus",
        db_path=db_path if db_path is not None else tmp_path / "goalx.db",
        srct_day_url="https://srct.test/over/{date}.htm",
        srct_odds_url="https://srct.test/odds/{sid}.js",
        srct_odds_referer="https://srct.test/oddslist/{sid}.htm",
        srct_handicap_url="https://srct.test/handicap/{sid}",
        srct_stats_url="https://srct.test/shijian/{sid}.htm",
    )


def _client(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/over/"):
            key = f"day:{path.removeprefix('/over/').removesuffix('.htm')}"
        elif path.startswith("/odds/"):
            key = f"odds:{path.removeprefix('/odds/').removesuffix('.js')}"
        elif path.startswith("/handicap/"):
            key = f"hdp:{path.removeprefix('/handicap/')}"
        else:
            key = f"stats:{path.removeprefix('/shijian/').removesuffix('.htm')}"
        if key not in routes:
            return httpx.Response(500, text="boom")
        return routes[key]

    return httpx.Client(transport=httpx.MockTransport(handler))


def _collect_bronze(tmp_path: Path) -> CorpusStore:
    """三合成日攒 bronze（日页行 + 每场三端点行），返回开着的 store。"""
    routes: dict[str, httpx.Response] = {}
    for day, sid in DATE_TO_SID.items():
        label = f"{int(day[-2:])}日20:00"  # 标签日 = 页日期（同日形态）
        routes[f"day:{day.replace('-', '')}"] = httpx.Response(
            200, content=_day_page(sid, label=label)
        )
        routes[f"odds:{sid}"] = httpx.Response(200, content=ODDS_JS)
        routes[f"hdp:{sid}"] = httpx.Response(200, content=HANDICAP_BYTES)
        routes[f"stats:{sid}"] = httpx.Response(200, content=STATS_HTML)
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    for day in DATES:
        srct.collect_day(
            store,
            settings,
            _client(routes),
            date=day,
            sleeper=lambda _s: None,
        )
    return store


def _running_face(tmp_path: Path) -> Path:
    """临时运行面 goalx.db：一张 hist_matches 两行（跨面冒烟参照）。"""
    face = tmp_path / "goalx.db"
    conn = sqlite3.connect(face)
    conn.execute("CREATE TABLE hist_matches (id INTEGER PRIMARY KEY, competition TEXT)")
    conn.executemany(
        "INSERT INTO hist_matches (competition) VALUES (?)", [("E0",), ("E0",)]
    )
    conn.commit()
    conn.close()
    return face


def test_resolve_kickoff_two_forms_and_anomaly() -> None:
    day = date(2025, 10, 18)
    assert srct_silver.resolve_kickoff(day, "18日19:30") == datetime(
        2025, 10, 18, 19, 30
    )
    assert srct_silver.resolve_kickoff(day, "19日02:45") == datetime(
        2025, 10, 19, 2, 45
    )  # 凌晨场：标签日 = 页日期+1
    assert srct_silver.resolve_kickoff(day, "17日20:00") is None  # 异常形态跳行
    assert srct_silver.resolve_kickoff(day, "garbage") is None
    # 跨月凌晨场：7/31 页上的 1日02:00 → 8/1
    assert srct_silver.resolve_kickoff(date(2025, 7, 31), "1日02:00") == datetime(
        2025, 8, 1, 2, 0
    )


def test_season_of_august_boundary() -> None:
    assert srct_silver.season_of(datetime(2025, 7, 31, 23, 59)) == "2024-25"
    assert srct_silver.season_of(datetime(2025, 8, 1, 0, 0)) == "2025-26"
    assert srct_silver.season_of(datetime(2026, 5, 20, 20, 0)) == "2025-26"


def test_stage_derivation() -> None:
    assert srct_silver._stage("英超", datetime(2025, 12, 1, 20, 0)) == "league"
    assert srct_silver._stage("欧冠杯", datetime(2025, 7, 20, 20, 0)) == "qualifier"
    assert srct_silver._stage("欧罗巴杯", datetime(2025, 9, 25, 20, 0)) == "main"


def test_build_partitions_sort_and_meta(tmp_path: Path) -> None:
    store = _collect_bronze(tmp_path)
    try:
        report = srct_silver.build_fixture_universe(store)
        assert report.rows == 3
        assert report.day_pages == 3
        assert report.partitions == 1
        assert report.seasons == {"2025-26": 3}
        part = (
            store.root
            / "silver"
            / "srct"
            / "fixture_universe"
            / "season=2025-26"
            / "competition=英超"
        )
        table = pq.read_table(part / "data.parquet")
        rows = table.to_pylist()
        # 排序：kickoff+sid；列含开球/比分/stage
        assert [r["kickoff"] for r in rows] == sorted(r["kickoff"] for r in rows)
        assert [r["sid"] for r in rows] == ["91001", "91002", "91003"]
        first = rows[0]
        assert (first["home_goals"], first["away_goals"]) == (1, 2)
        assert first["stage"] == "league"
        assert first["day"] == "2025-10-01"
        meta = json.loads(
            (
                store.root / "silver" / "srct" / "fixture_universe" / "_meta.json"
            ).read_text(encoding="utf-8")
        )
        assert meta["silver_version"] == srct_silver.SILVER_VERSION
        assert meta["bronze_version"] == srct.BRONZE_VERSIONS[srct.DAY_DATASET]
        assert meta["rows"] == 3
    finally:
        store.close()


def test_rebuild_idempotent_and_stale_removed(tmp_path: Path) -> None:
    store = _collect_bronze(tmp_path)
    try:
        part = (
            store.root
            / "silver"
            / "srct"
            / "fixture_universe"
            / "season=2025-26"
            / "competition=英超"
            / "data.parquet"
        )
        srct_silver.build_fixture_universe(store)
        first_bytes = part.read_bytes()
        report2 = srct_silver.build_fixture_universe(store)
        assert part.read_bytes() == first_bytes  # 幂等：同输入字节级一致
        assert report2.rows == 3
        assert report2.stale_partitions_removed == 0
        # 旧分区（联赛更名残留）重建后被清理
        stale = store.root / "silver" / "srct" / "fixture_universe" / "season=2024-25"
        (stale / "competition=西丁").mkdir(parents=True)
        (stale / "competition=西丁" / "data.parquet").write_bytes(b"stale")
        report3 = srct_silver.build_fixture_universe(store)
        assert report3.stale_partitions_removed == 1
        assert not stale.exists()
    finally:
        store.close()


def test_duckdb_bridge_cross_face_and_readonly(tmp_path: Path) -> None:
    store = _collect_bronze(tmp_path)
    try:
        srct_silver.build_fixture_universe(store)
        corpus_duckdb.build_corpus_duckdb(store)
    finally:
        store.close()
    face = _running_face(tmp_path)
    settings = _settings(tmp_path, db_path=face)
    con = corpus_duckdb.connect(settings)
    try:
        # silver 视图可查（hive 分区列随行）
        fixtures = con.execute(
            "SELECT sid, season, competition FROM fixture_universe ORDER BY sid"
        ).fetchall()
        assert [r[0] for r in fixtures] == ["91001", "91002", "91003"]
        assert fixtures[0][1:] == ("2025-26", "英超")
        # 跨面单引擎单 SQL（spec story 18）
        cross = con.execute(
            """
            SELECT (SELECT count(*) FROM fixture_universe) AS fixtures,
                   (SELECT count(*) FROM goalx.hist_matches) AS hist
            """
        ).fetchone()
        assert cross == (3, 2)
        # 消费侧只读：任何写路径被拒
        with pytest.raises(duckdb.Error):
            con.execute("INSERT INTO goalx.hist_matches (competition) VALUES ('x')")
        with pytest.raises(duckdb.Error):
            con.execute("CREATE TABLE goalx.evil (id INT)")
        with pytest.raises(duckdb.Error):
            con.execute("CREATE TABLE evil (id INT)")  # corpus.duckdb 只读打开
        with pytest.raises(duckdb.Error):
            con.execute(
                "INSERT INTO fixture_universe VALUES ('1','英超',now(),'a','b',1,0,'league','2025-10-01')"  # noqa: E501
            )
    finally:
        con.close()


def test_empty_corpus_build_degrades(tmp_path: Path) -> None:
    """空语料（首夜数据落地前）：silver 空建、duckdb 建库不炸、视图缺席。"""
    store = CorpusStore(_settings(tmp_path).corpus_root)
    try:
        report = srct_silver.build_fixture_universe(store)
        assert report.rows == 0
        assert report.partitions == 0
        path = corpus_duckdb.build_corpus_duckdb(store)
        assert path.is_file()
    finally:
        store.close()
    con = corpus_duckdb.connect(_settings(tmp_path))
    try:
        with pytest.raises(duckdb.Error):
            con.execute("SELECT * FROM fixture_universe")  # 视图未建（无 parquet）
    finally:
        con.close()


def test_sid_relisted_on_newer_day_wins(tmp_path: Path) -> None:
    """场次 sid 跨日页重挂：最新页的行胜出（防御性去重）。"""
    store = CorpusStore(_settings(tmp_path).corpus_root)
    try:
        payload_old = {
            "matches": [
                {
                    "sid": "91001",
                    "league": "英超",
                    "kickoff_label": "1日20:00",
                    "home": "旧主队",
                    "away": "旧客队",
                    "score": "0-0",
                }
            ]
        }
        payload_new = {
            "matches": [
                {
                    "sid": "91001",
                    "league": "英超",
                    "kickoff_label": "3日20:00",
                    "home": "主队甲",
                    "away": "客队乙",
                    "score": "1-2",
                }
            ]
        }
        for day, payload, fetched in (
            ("2025-10-01", payload_old, "2025-10-01T12:00:00+00:00"),
            ("2025-10-03", payload_new, "2025-10-03T12:00:00+00:00"),
        ):
            store.append_bronze(
                "srct",
                "day_page",
                [
                    {
                        "provider": "srct",
                        "dataset": "day_page",
                        "sid": day,
                        "fetched_at": fetched,
                        "parser_version": srct.BRONZE_VERSIONS[srct.DAY_DATASET],
                        "raw_sha": "0" * 64,
                        "payload": payload,
                    }
                ],
            )
        rows, day_pages, skipped = srct_silver.fixture_rows(store)
        assert day_pages == 2
        assert skipped == 0
        assert len(rows) == 1  # 同 sid 只留最新页行
        assert rows[0]["home"] == "主队甲"
        assert rows[0]["day"] == "2025-10-03"
    finally:
        store.close()


def test_malformed_score_skips_row(tmp_path: Path) -> None:
    """坏比分跳行计数（与开球异常同口径），不炸整树重建。"""
    store = CorpusStore(_settings(tmp_path).corpus_root)
    try:
        store.append_bronze(
            "srct",
            "day_page",
            [
                {
                    "provider": "srct",
                    "dataset": "day_page",
                    "sid": "2025-10-01",
                    "fetched_at": "2025-10-01T12:00:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.DAY_DATASET],
                    "raw_sha": "0" * 64,
                    "payload": {
                        "matches": [
                            {
                                "sid": "91001",
                                "league": "英超",
                                "kickoff_label": "1日20:00",
                                "home": "主队甲",
                                "away": "客队乙",
                                "score": "w-o",  # 坏比分
                            }
                        ]
                    },
                }
            ],
        )
        rows, _pages, skipped = srct_silver.fixture_rows(store)
        assert rows == []
        assert skipped == 1
    finally:
        store.close()


def test_build_xg_observations_flattens_and_keeps_keys(tmp_path: Path) -> None:
    store = _collect_bronze(tmp_path)
    try:
        report = srct_silver.build_xg_observations(store)
        assert report.rows == 3
        assert report.has_xg_rows == 3  # 合成统计页含 EXPECTED_GOALS
        assert report.partitions == 1
        part = (
            store.root
            / "silver"
            / "srct"
            / "xg_observation"
            / "season=2025-26"
            / "competition=英超"
        )
        rows = pq.read_table(part / "data.parquet").to_pylist()
        first = rows[0]
        assert first["xg_home"] == pytest.approx(0.71)  # 标题 xG 扁平列
        assert first["xg_away"] == pytest.approx(1.68)
        kinds = [item["kind"] for item in first["stats"]]
        assert kinds == ["EXPECTED_GOALS"]  # 合成页只带一键；真实页 47 键同通道
        assert first["stats"][0]["home_value"] == "0.71"  # 贴源串保留
    finally:
        store.close()


def test_build_xg_no_xg_match_keeps_stats(tmp_path: Path) -> None:
    """老页无 xG：has_xg=False、标题列 None，其余键照落（定则 4）。"""
    store = _collect_bronze(tmp_path)
    try:
        store.append_bronze(
            "srct",
            "match_stats",
            [
                {
                    "provider": "srct",
                    "dataset": "match_stats",
                    "sid": "91001",
                    "fetched_at": "2025-10-01T13:00:00+00:00",  # 晚于采集行=最新胜出
                    "parser_version": srct.BRONZE_VERSIONS[srct.STATS_DATASET],
                    "raw_sha": "1" * 64,
                    "payload": {
                        "stats": [
                            {
                                "kind": "CORNER",
                                "name": "角球",
                                "home_value": 3,
                                "away_value": 8,
                            },
                            {
                                "kind": "SHOOT",
                                "name": "射门",
                                "home_value": 9,
                                "away_value": 13,
                            },
                        ],
                        "has_xg": False,
                    },
                }
            ],
        )
        report = srct_silver.build_xg_observations(store)
        assert report.rows == 3
        assert report.has_xg_rows == 2
        part = (
            store.root
            / "silver"
            / "srct"
            / "xg_observation"
            / "season=2025-26"
            / "competition=英超"
        )
        rows = {r["sid"]: r for r in pq.read_table(part / "data.parquet").to_pylist()}
        old = rows["91001"]
        assert old["has_xg"] is False
        assert old["xg_home"] is None
        assert [i["kind"] for i in old["stats"]] == ["CORNER", "SHOOT"]  # 键不丢
        assert old["stats"][0]["home_value"] == "3"
    finally:
        store.close()


def test_build_xg_orphan_sid_unknown_partition(tmp_path: Path) -> None:
    """无 fixture 元数据的场：落 _unknown 分区不丢数据，计数留痕。"""
    store = CorpusStore(_settings(tmp_path).corpus_root)
    try:
        store.append_bronze(
            "srct",
            "match_stats",
            [
                {
                    "provider": "srct",
                    "dataset": "match_stats",
                    "sid": "99999",
                    "fetched_at": "2025-10-01T12:00:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.STATS_DATASET],
                    "raw_sha": "2" * 64,
                    "payload": {"stats": [], "has_xg": False},
                }
            ],
        )
        report = srct_silver.build_xg_observations(store)
        assert report.rows == 1
        assert report.orphan_sids == 1
        part = (
            store.root
            / "silver"
            / "srct"
            / "xg_observation"
            / "season=_unknown"
            / "competition=_unknown"
        )
        assert pq.read_table(part / "data.parquet").num_rows == 1
    finally:
        store.close()


def test_headline_missing_counted(tmp_path: Path) -> None:
    """has_xg（任一 XG 键）与标题键背离：计数留痕不炸。"""
    store = _collect_bronze(tmp_path)
    try:
        store.append_bronze(
            "srct",
            "match_stats",
            [
                {
                    "provider": "srct",
                    "dataset": "match_stats",
                    "sid": "91002",
                    "fetched_at": "2025-10-02T13:00:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.STATS_DATASET],
                    "raw_sha": "5" * 64,
                    "payload": {
                        "stats": [
                            {
                                "kind": "XGOT",
                                "name": "射正预期进球",
                                "home_value": 0.4,
                                "away_value": 0.9,
                            }
                        ],
                        "has_xg": True,  # XG 前缀键在场，但标题键缺席
                    },
                }
            ],
        )
        report = srct_silver.build_xg_observations(store)
        assert report.rows == 3
        assert report.has_xg_rows == 3
        assert report.headline_missing == 1  # 背离信号
        assert report.bad_xg_values == 0
    finally:
        store.close()


def test_cli_srct_silver_seam(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from goalx_backend import cli

    store = _collect_bronze(tmp_path)
    store.close()
    face = _running_face(tmp_path)
    args = cli.build_parser().parse_args(["srct-silver"])
    cli._cmd_srct_silver(args, settings=_settings(tmp_path, db_path=face))
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert payload["fixture"]["rows"] == 3
    assert payload["fixture"]["partitions"] == 1
    assert payload["fixture"]["silver_version"] == srct_silver.SILVER_VERSION
    assert payload["xg"]["rows"] == 3  # 每场一条 xg_observation
    assert payload["xg"]["has_xg_rows"] == 3
    assert Path(payload["duckdb"]).is_file()
    assert payload["cross_face_smoke"]["goalx_hist_matches"] == 2
