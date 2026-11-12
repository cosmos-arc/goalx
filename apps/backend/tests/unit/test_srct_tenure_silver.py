"""源T 任期/队籍 silver 面测试（票 09，spec-srct-cutover）。

样本为 2026-11-12 首班语料（research/08-*）真实捕获 bronze 行的裁剪：
coachCount 竞赛分行结构（总分行=竞赛行之和）、档案页名字映射、
matchResult 当季球队枚举。端点/主机名不出现（代称红线，票 08 体例）。
"""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend import db
from goalx_backend.data import srct_tenure_silver
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_tenure

# —— 迁移 v61：两表 + 幂等 ———


def test_v61_creates_two_tables_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert db.migrate(conn) == 61
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"srct_coach_tenures", "srct_club_seasons"} <= tables
    assert db.migrate(conn) == 61  # 重跑幂等（已应用版本跳过）
    # UNIQUE 键形状：任期 (coach_id, team_id, date_from)；
    # 队籍 (league_id, season, team_id)
    tenure_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(srct_coach_tenures)")
    }
    assert {
        "coach_id",
        "coach_key",
        "coach_name_cn",
        "coach_name_en",
        "team_id",
        "team_name_cn",
        "team_name_en",
        "date_from",
        "date_to",
        "role",
        "games",
        "parse_version",
    } <= tenure_cols
    season_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(srct_club_seasons)")
    }
    assert {"league_id", "season", "team_id", "team_name_en"} <= season_cols
    conn.close()


# —— 真实捕获样本（bronze 载荷裁剪，2026-11-12 首班） ——

# cid 10138（林肯城现任）：coachCount 同一任期拆 1 总分行 + 3 竞赛行
SAMPLE_CAREER_10138 = {
    "sid": "10138",
    "payload": {
        "once_team": "诺丁汉森林U21、林肯城",
        "now_team": [
            ["林肯城", "   ", "", "主教练", "主教緟", "Coach", "Lincoln City"]
        ],
        "transfers": [],
        "tenures": [
            {
                "start": "2026-06-09",
                "end": None,
                "team_id": 1188,
                "name_cn": "林肯城",
                "name_tw": "林肯城",
                "name_en": "Lincoln City",
                "matches": 23,
                "wins": 7,
                "draws": 3,
                "losses": 13,
            },
            {
                "start": "2026-06-09",
                "end": None,
                "team_id": 1188,
                "name_cn": "林肯城",
                "name_tw": "林肯城",
                "name_en": "Lincoln City",
                "matches": 15,
                "wins": 4,
                "draws": 3,
                "losses": 8,
            },
            {
                "start": "2026-06-09",
                "end": None,
                "team_id": 1188,
                "name_cn": "林肯城",
                "name_tw": "林肯城",
                "name_en": "Lincoln City",
                "matches": 5,
                "wins": 1,
                "draws": 0,
                "losses": 4,
            },
            {
                "start": "2026-06-09",
                "end": None,
                "team_id": 1188,
                "name_cn": "林肯城",
                "name_tw": "林肯城",
                "name_en": "Lincoln City",
                "matches": 3,
                "wins": 2,
                "draws": 0,
                "losses": 1,
            },
        ],
        "last_update": "2026-11-12 09:21:11",
    },
}

SAMPLE_PAGE_10138 = {
    "sid": "10138@1188",
    "payload": {
        "name_cn": "克里斯·科亨",
        "name_tw": "基斯高漢",
        "name_en": "Chris Cohen",
        "career_cid": "10138",
        "career_version": "2026111209",
    },
}

# 残组（cid 124050 实测形态裁剪）：无总分行、最大行不支配 → 战绩置空
SAMPLE_CAREER_SPLIT_ONLY = {
    "sid": "124050",
    "payload": {
        "once_team": "",
        "now_team": [],
        "transfers": [],
        "tenures": [
            {
                "start": "2026-07-01",
                "end": None,
                "team_id": 344,
                "name_cn": "名B",
                "name_tw": "名B",
                "name_en": "Team B",
                "matches": 26,
                "wins": 14,
                "draws": 7,
                "losses": 5,
            },
            {
                "start": "2026-07-01",
                "end": None,
                "team_id": 344,
                "name_cn": "名B",
                "name_tw": "名B",
                "name_en": "Team B",
                "matches": 21,
                "wins": 10,
                "draws": 9,
                "losses": 2,
            },
            {
                "start": "2026-07-01",
                "end": None,
                "team_id": 344,
                "name_cn": "名B",
                "name_tw": "名B",
                "name_en": "Team B",
                "matches": 7,
                "wins": 3,
                "draws": 1,
                "losses": 3,
            },
            {
                "start": "2026-07-01",
                "end": None,
                "team_id": 344,
                "name_cn": "名B",
                "name_tw": "名B",
                "name_en": "Team B",
                "matches": 1,
                "wins": 0,
                "draws": 1,
                "losses": 0,
            },
        ],
        "last_update": "2026-11-12 09:21:11",
    },
}


def test_tenure_rows_dedupe_competition_splits() -> None:
    """coachCount 总分行（=竞赛行之和）当选；任期行数不随竞赛拆分膨胀。"""
    rows = srct_tenure_silver.tenure_rows_from_bronze(
        {"10138": SAMPLE_CAREER_10138, "124050": SAMPLE_CAREER_SPLIT_ONLY},
        {"10138@1188": SAMPLE_PAGE_10138},
        now="2026-11-12T00:00:00+00:00",
    )
    by_coach: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        by_coach.setdefault(int(row["coach_id"]), []).append(row)
    # 10138：4 行竞赛拆分 → 1 行任期（总分战绩 23/7/3/13）
    only = by_coach[10138]
    assert len(only) == 1
    assert only[0]["date_from"] == "2026-06-09"
    assert only[0]["date_to"] is None
    assert only[0]["team_id"] == 1188
    assert (only[0]["games"], only[0]["wins"], only[0]["draws"], only[0]["losses"]) == (
        23,
        7,
        3,
        13,
    )
    # 钉②键形态：英文名优先
    assert only[0]["coach_key"] == "Chris Cohen"
    assert only[0]["coach_name_cn"] == "克里斯·科亨"
    assert only[0]["coach_name_en"] == "Chris Cohen"
    # 现任角色（nowTeamInfo 同队 + 起止开区间）
    assert only[0]["role"] == "主教练"
    # 124050：残组（无总分行且无支配行）→ 任期行保留（日期/队是事实），战绩诚实置空
    split = by_coach[124050]
    assert len(split) == 1
    assert split[0]["date_from"] == "2026-07-01"
    assert split[0]["games"] is None
    assert split[0]["wins"] is None
    # 缺档案页 → 键回退 playerInfo id（钉②缺则 id）
    assert split[0]["coach_key"] == "124050"
    assert split[0]["coach_name_en"] is None


def test_tenure_rows_identical_duplicates_collapse() -> None:
    """源端整行重复（实测 79 组）：同组同值行折叠，不误报战绩。"""
    tenure = dict(SAMPLE_CAREER_10138["payload"]["tenures"][0])
    career = {
        "sid": "107874",
        "payload": {
            **SAMPLE_CAREER_10138["payload"],
            "tenures": [tenure, dict(tenure)],
        },
    }
    rows = srct_tenure_silver.tenure_rows_from_bronze(
        {"107874": career}, {}, now="2026-11-12T00:00:00+00:00"
    )
    assert len(rows) == 1
    assert rows[0]["games"] == 23


def test_club_season_rows_from_bronze() -> None:
    league_row = {
        "sid": "2015-2016-s36",
        "payload": {
            "league": {
                "league_id": 36,
                "name_cn": "英格兰超级联赛",
                "name_tw": "英格蘭超級聯賽",
                "name_en": "English Premier League",
                "season": "2015-2016",
            },
            "teams": [
                {
                    "team_id": 18,
                    "name_cn": "西布罗姆维奇",
                    "name_tw": "西布朗",
                    "name_en": "West Bromwich(WBA)",
                },
                {
                    "team_id": 19,
                    "name_cn": "阿森纳",
                    "name_tw": "阿仙奴",
                    "name_en": "Arsenal",
                },
            ],
        },
    }
    rows = srct_tenure_silver.club_season_rows_from_bronze(
        {"2015-2016-s36": league_row}, now="2026-11-12T00:00:00+00:00"
    )
    assert len(rows) == 2
    first = rows[0]
    assert first["league_id"] == 36
    assert first["season"] == "2015-2016"
    assert first["team_id"] == 18
    assert first["team_name_en"] == "West Bromwich(WBA)"
    assert first["source_key"] == "2015-2016-s36"
    assert first["parse_version"] == srct_tenure.TENURE_BRONZE_VERSIONS["league_season"]


# —— 全量重建（合成语料 → 合成库）———


def _seed_corpus(store: CorpusStore) -> None:
    store.append_bronze(
        srct.SRCT_PROVIDER,
        srct_tenure.COACH_CAREER_DATASET,
        [
            {
                "provider": srct.SRCT_PROVIDER,
                "dataset": srct_tenure.COACH_CAREER_DATASET,
                "sid": cid,
                "fetched_at": "2026-11-12T01:30:00+00:00",
                "parser_version": srct_tenure.TENURE_BRONZE_VERSIONS[
                    srct_tenure.COACH_CAREER_DATASET
                ],
                "raw_sha": "deadbeef",
                "payload": row["payload"],
            }
            for cid, row in (
                ("10138", SAMPLE_CAREER_10138),
                ("124050", SAMPLE_CAREER_SPLIT_ONLY),
            )
        ],
    )
    store.append_bronze(
        srct.SRCT_PROVIDER,
        srct_tenure.COACH_PAGE_DATASET,
        [
            {
                "provider": srct.SRCT_PROVIDER,
                "dataset": srct_tenure.COACH_PAGE_DATASET,
                "sid": SAMPLE_PAGE_10138["sid"],
                "fetched_at": "2026-11-12T01:30:00+00:00",
                "parser_version": srct_tenure.TENURE_BRONZE_VERSIONS[
                    srct_tenure.COACH_PAGE_DATASET
                ],
                "raw_sha": "deadbeef",
                "payload": SAMPLE_PAGE_10138["payload"],
            }
        ],
    )
    store.append_bronze(
        srct.SRCT_PROVIDER,
        srct_tenure.LEAGUE_SEASON_DATASET,
        [
            {
                "provider": srct.SRCT_PROVIDER,
                "dataset": srct_tenure.LEAGUE_SEASON_DATASET,
                "sid": "2015-2016-s36",
                "fetched_at": "2026-11-12T01:30:00+00:00",
                "parser_version": srct_tenure.TENURE_BRONZE_VERSIONS[
                    srct_tenure.LEAGUE_SEASON_DATASET
                ],
                "raw_sha": "deadbeef",
                "payload": {
                    "league": {
                        "league_id": 36,
                        "name_cn": "英格兰超级联赛",
                        "name_tw": "英格蘭超級聯賽",
                        "name_en": "English Premier League",
                        "season": "2015-2016",
                    },
                    "teams": [
                        {
                            "team_id": 19,
                            "name_cn": "阿森纳",
                            "name_tw": "阿仙奴",
                            "name_en": "Arsenal",
                        }
                    ],
                },
            }
        ],
    )


@pytest.fixture
def silver_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    yield conn
    conn.close()


def test_rebuild_round_trip_and_idempotent(
    tmp_path: object, silver_db: sqlite3.Connection
) -> None:
    import pathlib

    store = CorpusStore(pathlib.Path(str(tmp_path)) / "corpus")
    _seed_corpus(store)
    report = srct_tenure_silver.rebuild_srct_tenure_silver(
        silver_db, store, now="2026-11-12T02:00:00+00:00"
    )
    assert report["tenure_rows"] == 2
    assert report["club_season_rows"] == 1
    assert report["coaches"] == 2
    assert report["stats_via"]["stats_null"] == 1
    assert report["name_fallback"] == 1
    rows = silver_db.execute(
        "SELECT coach_key, team_id FROM srct_coach_tenures ORDER BY coach_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("Chris Cohen", 1188), ("124050", 344)]
    seasons = silver_db.execute(
        "SELECT season, team_id FROM srct_club_seasons"
    ).fetchall()
    assert [tuple(r) for r in seasons] == [("2015-2016", 19)]
    # 重跑幂等：DELETE+INSERT 全量重建，行数不变
    again = srct_tenure_silver.rebuild_srct_tenure_silver(
        silver_db, store, now="2026-11-12T02:30:00+00:00"
    )
    assert again["tenure_rows"] == 2
    assert (
        silver_db.execute("SELECT COUNT(*) FROM srct_coach_tenures").fetchone()[0] == 2
    )
    store.close()


def test_rebuild_empty_corpus_no_crash(silver_db: sqlite3.Connection) -> None:
    """空语料：零行重建不炸（派生面纪律，pedigree 先例）。"""
    import pathlib
    import tempfile

    store = CorpusStore(pathlib.Path(tempfile.mkdtemp()) / "corpus")
    report = srct_tenure_silver.rebuild_srct_tenure_silver(silver_db, store)
    assert report["tenure_rows"] == 0
    assert report["club_season_rows"] == 0
    assert (
        silver_db.execute("SELECT COUNT(*) FROM srct_coach_tenures").fetchone()[0] == 0
    )
    store.close()


def test_tenure_rows_group_sort_tolerates_open_ends() -> None:
    """组键排序跨开/闭区间混合（真实语料 2026-11-12 实缴：None<str TypeError）。"""
    career = {
        "sid": "777",
        "payload": {
            "once_team": "",
            "now_team": [],
            "transfers": [],
            "tenures": [
                {
                    "start": "2026-06-09",
                    "end": None,
                    "team_id": 1188,
                    "name_cn": "林肯城",
                    "name_tw": "林肯城",
                    "name_en": "Lincoln City",
                    "matches": 23,
                    "wins": 7,
                    "draws": 3,
                    "losses": 13,
                },
                {
                    "start": "2020-06-09",
                    "end": "2021-06-30",
                    "team_id": 1188,
                    "name_cn": "林肯城",
                    "name_tw": "林肯城",
                    "name_en": "Lincoln City",
                    "matches": 10,
                    "wins": 4,
                    "draws": 3,
                    "losses": 3,
                },
                {
                    "start": "2020-06-09",
                    "end": None,
                    "team_id": 1190,
                    "name_cn": "他队",
                    "name_tw": "他隊",
                    "name_en": "Other FC",
                    "matches": 8,
                    "wins": 3,
                    "draws": 2,
                    "losses": 3,
                },
            ],
            "last_update": "2026-11-12 09:21:11",
        },
    }
    rows = srct_tenure_silver.tenure_rows_from_bronze(
        {"777": career}, {}, now="2026-11-12T00:00:00+00:00"
    )
    assert len(rows) == 3
    # 同 start 时开区间组（None→空串占位）先行：确定序可测
    assert [r["date_to"] for r in rows] == [None, "2021-06-30", None]
