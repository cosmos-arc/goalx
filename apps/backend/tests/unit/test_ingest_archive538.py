"""538 终版档案入库测试（票 55/56 切片 16）：静态资产 raw+silver+三源对照。

合成迷你 CSV（列结构对齐 research/21 留档终版）走真实入库/parquet/
duckdb 全链。真实 4,363 场口径与覆盖矩阵归 research/21，不进测试。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import archive538

# 三行 CorpusScope + 一行出范围（MLS）：xG 双非空 / 全空（英冠 2018 老行）
MINI_CSV = (
    "season,date,league_id,league,team1,team2,spi1,spi2,prob1,prob2,probtie,"
    "proj_score1,proj_score2,importance1,importance2,score1,score2,xg1,xg2,"
    "nsxg1,nsxg2,adj_score1,adj_score2\n"
    "2021,2021-09-18,2417,Barclays Premier League,Arsenal,Norwich,72.5,64.1,"
    "0.65,0.18,0.17,2.1,0.9,, ,3,0,2.8,0.4,1.9,0.5,2.7,0.4\n"
    "2018,2018-08-10,2411,English League Championship,Reading,Derby,48.2,47.9,"
    "0.42,0.29,0.29,1.4,1.2,, ,1,1,,,,,1.0,1.0\n"
    "2021,2021-09-18,2380,Major League Soccer,LA Galaxy,Austin,61.0,58.0,"
    "0.5,0.28,0.22,1.8,1.3,, ,2,1,1.9,1.1,1.4,0.9,1.8,1.0\n"
)


def _settings(tmp_path: Path, db_path: Path | None = None) -> Settings:
    return Settings(
        corpus_root=tmp_path / "corpus",
        db_path=db_path if db_path is not None else tmp_path / "goalx.db",
    )


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "mini538.csv"
    path.write_text(MINI_CSV, encoding="utf-8")
    return path


def _store(tmp_path: Path) -> CorpusStore:
    return CorpusStore(_settings(tmp_path).corpus_root)


def test_ingest_scopes_and_partitions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        report = archive538.ingest_archive_538(store, _csv(tmp_path))
        assert report.raw_ingested is True
        assert report.archive_rows == 3  # 原档总行数
        assert report.rows == 2  # MLS 出 CorpusScope
        assert report.has_xg_rows == 1  # 英冠 2018 无 xG 行如实记 False
        assert report.seasons == {"2021-22": 1, "2018-19": 1}  # 8 月界切
        part = (
            store.root
            / "silver"
            / "538"
            / "spi_matches"
            / "season=2021-22"
            / "competition=英超"
        )
        rows = pq.read_table(part / "data.parquet").to_pylist()
        first = rows[0]
        assert first["team1"] == "Arsenal"
        assert first["league_cn"] == "英超"
        assert first["xg1"] == pytest.approx(2.8)
        assert first["nsxg1"] == pytest.approx(1.9)
        assert first["has_xg"] is True
        assert first["archive_season"] == "2021"
        meta = json.loads(
            (store.root / "silver" / "538" / "spi_matches" / "_meta.json").read_text(
                encoding="utf-8"
            )
        )
        assert meta["license"] == "CC BY 4.0"
        assert store.verify_raw("538", "spi_matches", "terminal", ext=".csv")
    finally:
        store.close()


def test_rebuild_idempotent_from_tree_raw(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        archive538.ingest_archive_538(store, _csv(tmp_path))
        part = (
            store.root
            / "silver"
            / "538"
            / "spi_matches"
            / "season=2021-22"
            / "competition=英超"
            / "data.parquet"
        )
        first_bytes = part.read_bytes()
        report2 = archive538.ingest_archive_538(store, csv_path=None)  # 源档缺席
        assert report2.raw_ingested is False  # 树内 raw 自持，不依赖 .scratch
        assert part.read_bytes() == first_bytes  # 幂等：字节级一致
    finally:
        store.close()


def test_missing_source_raises_on_empty_tree(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(FileNotFoundError, match="538 原档"):
            archive538.ingest_archive_538(store, tmp_path / "nope.csv")
    finally:
        store.close()


def _seed_understat(face: Path) -> None:
    """运行面 understat 两行：EPL 2021-09-18 Arsenal（英文名与 538 对齐）。"""
    conn = sqlite3.connect(face)
    conn.execute(
        """
        CREATE TABLE understat_matches (
            id INTEGER PRIMARY KEY, match_id TEXT, league TEXT, season TEXT,
            datetime_utc TEXT, home_team_id TEXT, home_team TEXT,
            away_team_id TEXT, away_team TEXT, is_result INTEGER,
            goals_home INTEGER, goals_away INTEGER,
            xg_home REAL, xg_away REAL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO understat_matches (match_id, league, season, datetime_utc,
            home_team_id, home_team, away_team_id, away_team, is_result,
            goals_home, goals_away, xg_home, xg_away)
        VALUES ('u1', 'EPL', '2021', '2021-09-18 14:00:00',
            '89', 'Arsenal', '14', 'Norwich', 1, 3, 0, 2.74, 0.35)
        """
    )
    conn.commit()
    conn.close()


def test_duckdb_three_source_xg_query(tmp_path: Path) -> None:
    """单引擎单 SQL：源T xg_observation + 538 静态表 + 运行面 understat。"""
    from goalx_backend.data.ingest import srct, srct_silver

    store = _store(tmp_path)
    try:
        archive538.ingest_archive_538(store, _csv(tmp_path))
        # 源T 侧种一场（日页+统计 bronze → silver）
        store.append_bronze(
            srct.SRCT_PROVIDER,
            srct.DAY_DATASET,
            [
                {
                    "provider": "srct",
                    "dataset": "day_page",
                    "sid": "2021-09-18",
                    "fetched_at": "2021-09-19T01:00:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.DAY_DATASET],
                    "raw_sha": "3" * 64,
                    "payload": {
                        "matches": [
                            {
                                "sid": "71001",
                                "league": "英超",
                                "kickoff_label": "18日20:00",
                                "home": "阿森纳",
                                "away": "诺维奇",
                                "score": "3-0",
                            }
                        ]
                    },
                }
            ],
        )
        store.append_bronze(
            srct.SRCT_PROVIDER,
            srct.STATS_DATASET,
            [
                {
                    "provider": "srct",
                    "dataset": "match_stats",
                    "sid": "71001",
                    "fetched_at": "2021-09-19T01:01:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.STATS_DATASET],
                    "raw_sha": "4" * 64,
                    "payload": {
                        "stats": [
                            {
                                "kind": "EXPECTED_GOALS",
                                "name": "预期进球(xG)",
                                "home_value": 2.9,
                                "away_value": 0.4,
                            }
                        ],
                        "has_xg": True,
                    },
                }
            ],
        )
        srct_silver.build_fixture_universe(store)
        srct_silver.build_xg_observations(store)
        corpus_duckdb.build_corpus_duckdb(store)
    finally:
        store.close()
    face = tmp_path / "goalx.db"
    _seed_understat(face)
    con = corpus_duckdb.connect(_settings(tmp_path, db_path=face))
    try:
        # 单 SQL 三源齐查（同日同场；中文侧身份绑定后置，跨源行级 join 归切片 17）
        triple = con.execute(
            """
            SELECT
              (SELECT xg_home FROM xg_observation WHERE has_xg) AS srct_xg,
              (SELECT xg1 FROM archive_538 WHERE has_xg) AS x538_xg,
              (SELECT xg_home FROM goalx.understat_matches) AS understat_xg
            """
        ).fetchone()
        assert triple == (2.9, 2.8, 2.74)
        # 538 × understat：英文名+队名可直 join（当前就可行的对照通道）
        pair = con.execute(
            """
            SELECT a.team1, a.xg1, u.xg_home
            FROM archive_538 a
            JOIN goalx.understat_matches u
              ON a.team1 = u.home_team AND a.team2 = u.away_team
            WHERE a.has_xg
            """
        ).fetchone()
        assert pair[0] == "Arsenal"
        assert pair[1] == pytest.approx(2.8)
        assert pair[2] == pytest.approx(2.74)
    finally:
        con.close()


def test_cli_archive_538_seam(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from goalx_backend import cli

    csv_path = _csv(tmp_path)
    args = cli.build_parser().parse_args(["archive-538", "--csv", str(csv_path)])
    cli._cmd_archive_538(args, settings=_settings(tmp_path))
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == 2
    assert payload["has_xg_rows"] == 1
    assert payload["raw_ingested"] is True
    assert Path(payload["duckdb"]).is_file()


def test_season_boundary_calendar(tmp_path: Path) -> None:
    """7/8 月界切：7 月行归上季（夏季历标签偏差见模块注记）。"""
    from goalx_backend.data.ingest.srct_silver import season_of

    assert season_of(datetime(2021, 7, 15)) == "2020-21"
    assert season_of(datetime(2021, 8, 1)) == "2021-22"
    assert date(2021, 9, 18).isoformat() == "2021-09-18"
