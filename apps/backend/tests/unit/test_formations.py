"""
六轴数据 ② 阵型族条件面测试（vision-build 票 09）。

族字典=受控常量（未匹配归 other 不炸）；条件面=族 × 赛季 × 教练任期联结
（match_detail 阵型 → manager-of-the-day 指派 → TM v49 任期空间）；PIT 读缝
只取查询时点前已完结的格子（不含当场面）。合成语料走真实
parquet/duckdb/迁移全链，真源网络与真实语料永不进测试。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb, formations, silver
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_market, srct_silver
from goalx_backend.db import connect, migrate

# ---- 族字典（受控常量） ----


def test_family_of_maps_controlled_formations() -> None:
    assert formations.family_of("4-4-2") == "4-4-2"
    assert formations.family_of("4-4-1-1") == "4-4-2"
    assert formations.family_of("4-2-3-1") == "4-2-3-1"
    assert formations.family_of("4-3-3") == "4-3-3"
    assert formations.family_of("4-5-1") == "4-5-1"
    assert formations.family_of("4-1-4-1") == "4-5-1"
    assert formations.family_of("3-5-2") == "3-5-2"
    assert formations.family_of("3-5-1-1") == "3-5-2"
    assert formations.family_of("3-4-3") == "3-4-3"
    assert formations.family_of("3-4-2-1") == "3-4-3"
    assert formations.family_of("5-3-2") == "back-five"
    assert formations.family_of("5-4-1") == "back-five"


def test_family_of_unmatched_and_missing_fall_back() -> None:
    # 未匹配阵型归 other 不炸；空值/空串=None（未知，不进族分母）
    assert formations.family_of("3-1-3-1-2") == "other"
    assert formations.family_of("") is None
    assert formations.family_of(None) is None


def test_families_constant_is_closed() -> None:
    # family_of 的一切映射值 ∈ FAMILIES；FAMILIES 含 other（且 other 不经映射产生）
    assert "other" in formations.FAMILIES
    mapped = set(formations.FAMILY_MAP.values())
    assert mapped <= set(formations.FAMILIES)
    assert "other" not in mapped


# ---- 合成环境（语料 corpus.duckdb + 运行面主库） ----


def _fixture_row(sid: str, league: str, kickoff: datetime, home: str, away: str):
    return {
        "sid": sid,
        "league": league,
        "kickoff": kickoff.replace(tzinfo=None),
        "home": home,
        "away": away,
        "home_goals": 1,
        "away_goals": 0,
        "stage": "league",
        "day": kickoff.date().isoformat(),
    }


def _detail_row(
    sid: str,
    kickoff: datetime,
    home: str,
    away: str,
    home_formation: str | None,
    away_formation: str | None,
    home_coach: str | None,
    away_coach: str | None,
):
    return {
        "sid": sid,
        "kickoff": kickoff,
        "observed_at": kickoff,
        "home": home,
        "away": away,
        "venue": "x",
        "weather": "x",
        "temperature": "x",
        "referee": "x",
        "home_formation": home_formation,
        "away_formation": away_formation,
        "home_coach": home_coach,
        "away_coach": away_coach,
        "has_xg": False,
        "xg_home": None,
        "xg_away": None,
        "tech_rows": 0,
        "events": 0,
        "home_starters": 11,
        "away_starters": 11,
        "home_bench": 5,
        "away_bench": 5,
    }


def _seed_face(conn: sqlite3.Connection, sids: list[str]) -> None:
    """两队（甲/乙）+ 逐 sid 一场已链 fixture + TM 任职层与 transfermarkt 别名。"""
    conn.execute(
        "INSERT INTO competitions (id, name, tier, created_at)"
        " VALUES (1, '英超', 'tier1', '2026-01-01')"
    )
    conn.executemany(
        "INSERT INTO teams (id, canonical_name, created_at)"
        " VALUES (?, ?, '2026-01-01')",
        [(1, "甲队"), (2, "乙队")],
    )
    for pos, sid in enumerate(sids):
        cur = conn.execute(
            """
            INSERT INTO fixtures
                (id, competition_id, kickoff_utc, home_team_id, away_team_id)
            VALUES (?, 1, ?, 1, 2)
            """,
            (pos + 1, f"2024-08-0{pos + 1}T20:00:00+00:00"),
        )
        assert cur.lastrowid is not None
        conn.execute(
            """
            INSERT INTO source_match_links
                (fixture_id, source, source_match_id, method, status, meta, mapped_at)
            VALUES (?, 'srct', ?, 'primary_key', 'linked', '{}', '2026-01-01')
            """,
            (int(cur.lastrowid), sid),
        )
    # TM 任职层：甲队 club 900 两任主帅（2024-12-01 换帅）；乙队 club 901 一任。
    conn.executemany(
        """
        INSERT INTO tm_coach_tenures
            (club_tm_id, club_name, role, coach_tm_id, coach_name, date_from,
             date_to, games, first_seen_at, observed_at, parse_version)
        VALUES (?, ?, 'manager', ?, ?, ?, ?, 10,
                '2026-01-01', '2026-01-01', 'tm_coach_v1')
        """,
        [
            (900, "Jia FC", 1, "Old Manager", "2024-03-01", "2024-11-30"),
            (900, "Jia FC", 2, "New Manager", "2024-12-01", None),
            (901, "Yi FC", 3, "Yi Coach", "2024-01-01", None),
        ],
    )
    conn.executemany(
        "INSERT INTO team_aliases (team_id, source, alias)"
        " VALUES (?, 'transfermarkt', ?)",
        [(1, "Jia FC"), (2, "Yi FC")],
    )
    conn.commit()


def _build_corpus(
    store: CorpusStore,
    fixture_rows: list[dict[str, object]],
    detail_rows: list[dict[str, object]],
) -> None:
    root = store.silver_path(srct.SRCT_PROVIDER, srct_silver.FIXTURE_DATASET)
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in fixture_rows:
        season = silver.season_of(row["kickoff"])  # type: ignore[arg-type]
        grouped.setdefault((season, str(row["league"])), []).append(row)
    for (season, league), rows in grouped.items():
        silver.write_partition(
            root / f"season={season}" / f"competition={league}",
            rows,
            srct_silver._FIXTURE_SCHEMA,
        )
    silver.write_dataset_file(
        store.silver_path(srct.SRCT_PROVIDER, srct_market.DETAIL_DATASET),
        detail_rows,
        srct_market._DETAIL_SCHEMA,
    )
    corpus_duckdb.build_corpus_duckdb(store)


@pytest.fixture
def facet_env(
    tmp_path: Path,
) -> Iterator[tuple[duckdb.DuckDBPyConnection, sqlite3.Connection]]:
    """合成环境：主库（链+任期）+ 语料（universe+detail）+ duckdb 桥。"""
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    sids = ["s1", "s2", "s3", "s4"]
    _seed_face(conn, sids)

    def kickoff(year: int, month: int, day: int) -> datetime:
        return datetime(year, month, day, 20, 0, tzinfo=UTC)

    fixture_rows = [
        _fixture_row("s1", "英超", kickoff(2024, 8, 1), "甲队", "乙队"),
        _fixture_row("s2", "英超", kickoff(2024, 9, 1), "甲队", "乙队"),
        _fixture_row("s3", "英超", kickoff(2024, 12, 15), "甲队", "乙队"),
        _fixture_row("s4", "英超", kickoff(2025, 2, 1), "甲队", "乙队"),
        # s5：语料有、链无 → 不入面（覆盖报告计数）
        _fixture_row("s5", "英超", kickoff(2025, 3, 1), "甲队", "乙队"),
    ]
    detail_rows = [
        _detail_row(
            "s1",
            kickoff(2024, 8, 1),
            "甲队",
            "乙队",
            "4-4-2",
            "4-2-3-1",
            "奥帅",
            "伊帅",
        ),
        _detail_row(
            "s2",
            kickoff(2024, 9, 1),
            "甲队",
            "乙队",
            "4-4-2",
            "4-2-3-1",
            "奥帅",
            "伊帅",
        ),
        _detail_row(
            "s3",
            kickoff(2024, 12, 15),
            "甲队",
            "乙队",
            "4-2-3-1",
            "4-2-3-1",
            "新帅",
            "伊帅",
        ),
        # s4：主队阵型未匹配 → other 族（分母计 1）
        _detail_row(
            "s4",
            kickoff(2025, 2, 1),
            "甲队",
            "乙队",
            "banana",
            "4-2-3-1",
            "新帅",
            "伊帅",
        ),
        _detail_row(
            "s5",
            kickoff(2025, 3, 1),
            "甲队",
            "乙队",
            "4-4-2",
            "4-2-3-1",
            "新帅",
            "伊帅",
        ),
    ]
    store = CorpusStore(tmp_path / "corpus")
    _build_corpus(store, fixture_rows, detail_rows)
    duck_con = corpus_duckdb.connect(
        Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    )
    yield duck_con, conn
    duck_con.close()
    conn.close()


def _facet_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT season, league_fd, league_zh, team_id, club_tm_id, club_name,
               coach_name, coach_role, family, n_matches, n_known,
               first_match_date, last_match_date
        FROM formation_family_facet
        ORDER BY club_name, coach_name, family
        """
    ).fetchall()


def test_derive_builds_family_by_season_by_tenure_cells(facet_env) -> None:
    duck_con, conn = facet_env
    report = formations.derive_formation_family_facet(
        conn, duck_con, now="2026-11-11T00:00:00+00:00"
    )
    rows = _facet_rows(conn)
    by_key = {(r["coach_name"], r["family"]): r for r in rows}
    # 换帅边界：s1/s2 归 Old Manager（4-4-2×2），s3/s4 归 New Manager
    old = by_key[("Old Manager", "4-4-2")]
    assert (old["season"], old["league_fd"], old["league_zh"]) == (
        "2024-25",
        "E0",
        "英超",
    )
    assert (old["club_tm_id"], old["club_name"], old["coach_role"]) == (
        900,
        "Jia FC",
        "manager",
    )
    assert old["n_matches"] == 2
    assert old["n_known"] == 2
    assert old["first_match_date"] == "2024-08-01"
    assert old["last_match_date"] == "2024-09-01"
    new231 = by_key[("New Manager", "4-2-3-1")]
    assert new231["n_matches"] == 1  # s3 一场
    assert new231["n_known"] == 2  # s3 + s4(other) 同分母
    other = by_key[("New Manager", "other")]
    assert other["n_matches"] == 1
    # 客队侧：Yi Coach 全季 4-2-3-1 ×4
    yi = by_key[("Yi Coach", "4-2-3-1")]
    assert (yi["club_tm_id"], yi["team_id"], yi["n_matches"], yi["n_known"]) == (
        901,
        2,
        4,
        4,
    )
    # s5 未链且在当季 → 不入面（当季单元格归链接链独占），按侧计数
    assert len(rows) == 4
    assert report["matches_seen"] == 5
    assert report["live_season_unlinked"] == 2
    assert report["rows"] == 4


def test_derive_is_idempotent_rebuild(facet_env) -> None:
    duck_con, conn = facet_env
    formations.derive_formation_family_facet(
        conn, duck_con, now="2026-11-11T00:00:00+00:00"
    )
    report = formations.derive_formation_family_facet(
        conn, duck_con, now="2026-11-12T00:00:00+00:00"
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM formation_family_facet").fetchone()[0] == 4
    )
    # 差分写幂等：重跑零效果——全量 preserved、零插入/改写/删除，且既有行
    # rebuilt_at 逐字节不动（票面验收：2026-27 既有行不得重写）
    assert report["preserved"] == 4
    assert report["inserted"] == 0
    assert report["updated"] == 0
    assert report["deleted"] == 0
    rebuilt = {
        row[0] for row in conn.execute("SELECT rebuilt_at FROM formation_family_facet")
    }
    assert rebuilt == {"2026-11-11T00:00:00+00:00"}


def test_derive_drop_paths_are_counted_not_guessed(tmp_path: Path) -> None:
    """诚实丢弃口径：无别名/俱乐部不唯一/任期空窗/无阵型逐档计数，不硬配。"""
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    conn.execute(
        "INSERT INTO competitions (id, name, tier, created_at)"
        " VALUES (1, '英超', 'tier1', '2026-01-01')"
    )
    conn.executemany(
        "INSERT INTO teams (id, canonical_name, created_at)"
        " VALUES (?, ?, '2026-01-01')",
        [(1, "甲队"), (2, "乙队"), (3, "无桥队"), (4, "幽灵队")],
    )
    for pos, (sid, home_id, away_id) in enumerate(
        [("s1", 3, 2), ("s2", 4, 2), ("s3", 1, 2)]
    ):
        cur = conn.execute(
            """
            INSERT INTO fixtures
                (id, competition_id, kickoff_utc, home_team_id, away_team_id)
            VALUES (?, 1, ?, ?, ?)
            """,
            (pos + 1, f"2024-08-0{pos + 1}T20:00:00+00:00", home_id, away_id),
        )
        assert cur.lastrowid is not None
        conn.execute(
            """
            INSERT INTO source_match_links
                (fixture_id, source, source_match_id, method, status, meta, mapped_at)
            VALUES (?, 'srct', ?, 'primary_key', 'linked', '{}', '2026-01-01')
            """,
            (int(cur.lastrowid), sid),
        )
    # 乙队别名+任期齐全；无桥队无别名；幽灵队有别名片但名字不指任何俱乐部；
    # 甲队别名+任期在（Old Manager 自 2024-03-01 起）
    conn.executemany(
        "INSERT INTO tm_coach_tenures"
        " (club_tm_id, club_name, role, coach_tm_id, coach_name, date_from,"
        "  date_to, games, first_seen_at, observed_at, parse_version)"
        " VALUES (?, ?, 'manager', ?, ?, ?, ?, 10, '2026-01-01', '2026-01-01',"
        " 'tm_coach_v1')",
        [
            (900, "Jia FC", 1, "Old Manager", "2024-03-01", None),
            (901, "Yi FC", 3, "Yi Coach", "2024-01-01", None),
        ],
    )
    conn.executemany(
        "INSERT INTO team_aliases (team_id, source, alias)"
        " VALUES (?, 'transfermarkt', ?)",
        [(1, "Jia FC"), (2, "Yi FC"), (4, "Ghost FC")],
    )
    conn.commit()

    def kickoff(month: int) -> datetime:
        return datetime(2024, month, 1, 20, 0, tzinfo=UTC)

    fixture_rows = [
        _fixture_row("s1", "英超", kickoff(8), "无桥队", "乙队"),
        _fixture_row("s2", "英超", kickoff(9), "幽灵队", "乙队"),
        # s3 在 2024-02-01：早于甲队首任 2024-03-01 → 空窗
        _fixture_row(
            "s3", "英超", datetime(2024, 2, 1, 20, 0, tzinfo=UTC), "甲队", "乙队"
        ),
    ]
    detail_rows = [
        _detail_row(
            "s1", kickoff(8), "无桥队", "乙队", "4-4-2", "4-2-3-1", "x", "伊帅"
        ),
        _detail_row(
            "s2", kickoff(9), "幽灵队", "乙队", "4-4-2", "4-2-3-1", "x", "伊帅"
        ),
        # s3：主队空窗指派失败；客队指派成功但阵型缺 → formation_unknown
        _detail_row(
            "s3",
            datetime(2024, 2, 1, 20, 0, tzinfo=UTC),
            "甲队",
            "乙队",
            "4-4-2",
            None,
            "x",
            "伊帅",
        ),
    ]
    store = CorpusStore(tmp_path / "corpus")
    _build_corpus(store, fixture_rows, detail_rows)
    duck_con = corpus_duckdb.connect(
        Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    )
    try:
        stats = formations.derive_formation_family_facet(
            conn, duck_con, now="2026-11-11T00:00:00+00:00"
        )
    finally:
        duck_con.close()
        conn.close()
    assert stats["no_alias"] == 1  # s1 主队无别名
    assert stats["no_club"] == 1  # s2 主队别名不指唯一俱乐部
    assert stats["unattributed"] == 1  # s3 主队任期空窗
    assert stats["formation_unknown"] == 1  # s3 客队指派成功但无阵型
    # 乙队三场 4-2-3-1 全归 Yi Coach → 唯一存活格
    assert stats["rows"] == 1


def test_coach_family_prior_is_pit_safe(facet_env) -> None:
    """PIT 断言：读缝只含查询时点前已完结格子，绝不含当场面。"""
    duck_con, conn = facet_env
    formations.derive_formation_family_facet(
        conn, duck_con, now="2026-11-11T00:00:00+00:00"
    )
    # 季首视角（不给 day）：只用此前赛季 → New Manager 无历史，惯用族未知
    at_season_start = formations.coach_family_prior(
        conn, "New Manager", upto_season="2024-25"
    )
    assert at_season_start["cells"] == {}
    assert at_season_start["n_known"] == 0
    # 季中 12-20 视角：12-15 已踢的 4-2-3-1 入缝，2025-02-01 的 other 未结不入
    mid_season = formations.coach_family_prior(
        conn, "New Manager", upto_season="2024-25", on_day="2024-12-20"
    )
    assert mid_season["cells"] == {"4-2-3-1": 1}
    assert mid_season["dominant_family"] == "4-2-3-1"
    # 次季视角（不给 day）：上季两格全入（last=2025-02-01 < 季首）
    next_season = formations.coach_family_prior(
        conn, "New Manager", upto_season="2025-26"
    )
    assert next_season["cells"] == {"4-2-3-1": 1, "other": 1}
    assert next_season["dominant_family"] == "4-2-3-1"  # 并列取字典序稳定者
    # 老帅跨季惯性：季首即可查
    old_habit = formations.coach_family_prior(
        conn, "Old Manager", upto_season="2025-26"
    )
    assert old_habit["cells"] == {"4-4-2": 2}
    assert old_habit["dominant_family"] == "4-4-2"


def test_coverage_report_slices_e0_e1(facet_env) -> None:
    duck_con, conn = facet_env
    formations.derive_formation_family_facet(
        conn, duck_con, now="2026-11-11T00:00:00+00:00"
    )
    report = formations.coverage_report(conn)
    leagues = {row["league_fd"]: row for row in report["leagues"]}
    assert leagues["E0"]["rows"] == 4
    assert leagues["E0"]["clubs"] == 2
    assert leagues["E0"]["coach_seasons"] == 3  # Old/New/Yi 各一
    # 逐季行数（票 05 验收③）：全量行落在 2024-25
    assert [item["season"] for item in report["seasons"]] == ["2024-25"]
    assert report["seasons"][0]["rows"] == 4
    # 切片=票面英超+英冠（全联赛落库，报告切片单独成节；此处合成语料仅英超）
    assert report["slice"] == ["E0", "E1"]
    assert [item["league_fd"] for item in report["priority_slice"]] == ["E0"]
    assert report["priority_slice"][0]["rows"] == 4
    assert report["totals"]["matches_seen"] == 5
    assert report["totals"]["live_season_unlinked"] == 2


def test_migration_v53_family_check_matches_controlled_constant(db) -> None:
    """迁移 v53 CHECK 族清单与受控常量同源（字面双写的防漂移钉）。"""
    row = db.execute(
        "SELECT sql FROM sqlite_master"
        " WHERE type = 'table' AND name = 'formation_family_facet'"
    ).fetchone()
    assert row is not None
    for family in formations.FAMILIES:
        assert f"'{family}'" in str(row["sql"])


def test_tm_rename_resolves_via_any_alias(tmp_path: Path) -> None:
    """F7：TM 改名窗口内多别名并存——任一名指到唯一俱乐部即接通，旧名不独裁。"""
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    conn.execute(
        "INSERT INTO competitions (id, name, tier, created_at)"
        " VALUES (1, '英超', 'tier1', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO teams (id, canonical_name, created_at)"
        " VALUES (1, '甲队', '2026-01-01')"
    )
    cur = conn.execute(
        """
        INSERT INTO fixtures
            (id, competition_id, kickoff_utc, home_team_id, away_team_id)
        VALUES (1, 1, '2024-08-01T20:00:00+00:00', 1, 1)
        """
    )
    assert cur.lastrowid is not None
    conn.execute(
        """
        INSERT INTO source_match_links
            (fixture_id, source, source_match_id, method, status, meta, mapped_at)
        VALUES (?, 'srct', 's1', 'primary_key', 'linked', '{}', '2026-01-01')
        """,
        (int(cur.lastrowid),),
    )
    # 任期面只挂新名（club 900）；别名桥旧名先落库（id 序在前）
    conn.execute(
        """
        INSERT INTO tm_coach_tenures
            (club_tm_id, club_name, role, coach_tm_id, coach_name, date_from,
             date_to, games, first_seen_at, observed_at, parse_version)
        VALUES (900, 'New Name FC', 'manager', 1, 'The Coach', '2024-01-01',
                NULL, 10, '2026-01-01', '2026-01-01', 'tm_coach_v1')
        """
    )
    conn.executemany(
        "INSERT INTO team_aliases (team_id, source, alias)"
        " VALUES (1, 'transfermarkt', ?)",
        [("Old Name FC",), ("New Name FC",)],
    )
    conn.commit()
    kickoff = datetime(2024, 8, 1, 20, 0, tzinfo=UTC)
    store = CorpusStore(tmp_path / "corpus")
    _build_corpus(
        store,
        [_fixture_row("s1", "英超", kickoff, "甲队", "乙队")],
        [_detail_row("s1", kickoff, "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y")],
    )
    duck_con = corpus_duckdb.connect(
        Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    )
    try:
        stats = formations.derive_formation_family_facet(
            conn, duck_con, now="2026-11-11T00:00:00+00:00"
        )
        rows = conn.execute(
            "SELECT club_tm_id, club_name, coach_name FROM formation_family_facet"
        ).fetchall()
    finally:
        duck_con.close()
        conn.close()
    # 旧名先落库但 tenure 挂新名：命中名=New Name FC，不再整队 no_club
    assert stats["no_club"] == 0
    assert stats["no_alias"] == 0
    assert stats["rows"] == 2  # 同队主客两侧各一格（4-4-2 / 4-2-3-1）
    assert {(r["club_tm_id"], r["club_name"], r["coach_name"]) for r in rows} == {
        (900, "New Name FC", "The Coach")
    }


def test_kickoff_null_buckets_separately(tmp_path: Path) -> None:
    """F8：开球缺失场独立分桶 kickoff_missing，不混入 universe_missing。"""
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    _seed_face(conn, ["s1", s_null := "s9"])
    kickoff = datetime(2024, 8, 1, 20, 0, tzinfo=UTC)
    store = CorpusStore(tmp_path / "corpus")
    _build_corpus(
        store,
        [
            _fixture_row("s1", "英超", kickoff, "甲队", "乙队"),
            # universe 有行、detail 开球缺失 → 无日期轴
            _fixture_row(
                s_null, "英超", datetime(2024, 9, 1, 20, 0, tzinfo=UTC), "甲队", "乙队"
            ),
        ],
        [
            _detail_row(
                "s1", kickoff, "甲队", "乙队", "4-4-2", "4-2-3-1", "奥帅", "伊帅"
            ),
            _detail_row(
                s_null, None, "甲队", "乙队", "4-4-2", "4-2-3-1", "奥帅", "伊帅"
            )
            | {"kickoff": None, "observed_at": kickoff},
        ],
    )
    duck_con = corpus_duckdb.connect(
        Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    )
    try:
        stats = formations.derive_formation_family_facet(
            conn, duck_con, now="2026-11-11T00:00:00+00:00"
        )
    finally:
        duck_con.close()
        conn.close()
    assert stats["matches_seen"] == 2
    assert stats["kickoff_missing"] == 1  # 独立桶
    assert stats["universe_missing"] == 0  # 不再被 NULL 开球污染
    assert stats["live_season_unlinked"] == 0


# ---- 历史链（mining-loop 票 05：zh→桥→hist 名→TM 俱乐部）----


def _seed_hist_bridge_face(conn: sqlite3.Connection) -> None:
    """历史链测试面：hist E0 三场（桥投票）+ TM 俱乐部/任期 + 成员面。"""
    conn.executemany(
        """
        INSERT INTO hist_matches
            (competition, season, match_date, home_team, away_team, fthg, ftag,
             ftr, psc_home, psc_draw, psc_away)
        VALUES ('E0', '2324', ?, ?, ?, ?, ?, 'H', 2.0, 3.5, 4.0)
        """,
        [
            ("2023-08-11", "Jia FC", "Yi FC", 2, 1),
            ("2023-08-18", "Jia FC", "Yi FC", 1, 0),
            ("2023-08-25", "Yi FC", "Jia FC", 0, 3),
        ],
    )
    conn.executemany(
        """
        INSERT INTO tm_coach_tenures
            (club_tm_id, club_name, role, coach_tm_id, coach_name, date_from,
             date_to, games, first_seen_at, observed_at, parse_version)
        VALUES (?, ?, 'manager', ?, ?, ?, ?, 10,
                '2026-01-01', '2026-01-01', 'tm_coach_v1')
        """,
        [
            # 甲队 2023-08-20 换帅：PIT 指派随 day 切换
            (900, "Jia FC", 1, "Old Manager", "2023-06-01", "2023-08-19"),
            (900, "Jia FC", 2, "New Manager", "2023-08-20", None),
            (901, "Yi FC", 3, "Yi Coach", "2023-01-01", None),
        ],
    )
    # 成员面（resolver 联赛域输入）
    conn.executemany(
        """
        INSERT INTO tm_club_seasons
            (league_tm_code, season, club_tm_id, club_name, first_seen_at,
             observed_at)
        VALUES ('GB1', '2324', ?, ?, '2026-01-01', '2026-01-01')
        """,
        [(900, "Jia FC"), (901, "Yi FC")],
    )
    conn.commit()


def test_history_chain_attributes_via_bridge(tmp_path: Path) -> None:
    """票 05 历史链：未链冻结季 sid 经 zh→桥→hist 名→TM 俱乐部入面，team 锚无。"""
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    _seed_hist_bridge_face(conn)
    store = CorpusStore(tmp_path / "corpus")

    def bj_kickoff(month: int, day: int) -> datetime:
        # 北京墙钟凌晨场（欧洲日前一天 20:00 开球）：桥语料侧日=UTC 日
        return datetime(2023, month, day, 3, 0)

    fixture_rows = [
        # 三场历史（universe 中文名 甲队/乙队，赛果与 hist 对齐），sid 不落
        # source_match_links → 走历史链
        _fixture_row("h1", "英超", bj_kickoff(8, 12), "甲队", "乙队")
        | {"home_goals": 2, "away_goals": 1, "day": "2023-08-11"},
        _fixture_row("h2", "英超", bj_kickoff(8, 19), "甲队", "乙队")
        | {"home_goals": 1, "away_goals": 0, "day": "2023-08-18"},
        _fixture_row("h3", "英超", bj_kickoff(8, 26), "乙队", "甲队")
        | {"home_goals": 0, "away_goals": 3, "day": "2023-08-25"},
        # 桥没通的第三方队（历史链身份折缺口分桶）
        _fixture_row("h4", "英超", bj_kickoff(9, 2), "丙队", "乙队")
        | {"home_goals": 1, "away_goals": 1, "day": "2023-09-01"},
        # 当季行：把当季界（=detail 覆盖最大季）推到 2024-25（未链 → 当季桶）
        _fixture_row("live", "英超", datetime(2024, 8, 1, 20, 0), "甲队", "乙队"),
    ]
    detail_rows = [
        _detail_row(
            "h1", bj_kickoff(8, 12), "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        )
        | {"kickoff": datetime(2023, 8, 11, 19, 0, tzinfo=UTC)},
        _detail_row(
            "h2", bj_kickoff(8, 19), "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        )
        | {"kickoff": datetime(2023, 8, 18, 19, 0, tzinfo=UTC)},
        _detail_row("h3", bj_kickoff(8, 26), "乙队", "甲队", "3-5-2", "4-4-2", "y", "x")
        | {"kickoff": datetime(2023, 8, 25, 19, 0, tzinfo=UTC)},
        _detail_row(
            "h4", bj_kickoff(9, 2), "丙队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        )
        | {"kickoff": datetime(2023, 9, 1, 19, 0, tzinfo=UTC)},
        # 当季 detail：season=2024-25（界由 detail 覆盖决定，universe 预挂不算）
        _detail_row(
            "live",
            datetime(2024, 8, 1, 20, 0),
            "甲队",
            "乙队",
            "4-4-2",
            "4-2-3-1",
            "x",
            "y",
        )
        | {"kickoff": datetime(2024, 8, 1, 19, 0, tzinfo=UTC)},
    ]
    _build_corpus(store, fixture_rows, detail_rows)
    duck_con = corpus_duckdb.connect(
        Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    )
    try:
        stats = formations.derive_formation_family_facet(
            conn, duck_con, now="2026-11-11T00:00:00+00:00"
        )
        rows = conn.execute(
            """
            SELECT season, league_fd, team_id, club_tm_id, club_name, coach_name,
                   family, n_matches, first_match_date, last_match_date
            FROM formation_family_facet ORDER BY coach_name, family
            """
        ).fetchall()
    finally:
        duck_con.close()
        conn.close()
    # 三场历史 × 两侧全部入面：h1/h2 在 2023-08-19 前 → Old Manager，
    # h3（08-25）→ New Manager（PIT：指派随当日任期态）
    by_key = {(r["coach_name"], r["family"]): r for r in rows}
    old = by_key[("Old Manager", "4-4-2")]
    assert (old["season"], old["league_fd"]) == ("2023-24", "E0")
    assert old["team_id"] is None  # 历史行无运行面 team 锚
    assert (old["club_tm_id"], old["club_name"]) == (900, "Jia FC")
    assert old["n_matches"] == 2  # h1/h2 主队
    new = by_key[("New Manager", "4-4-2")]
    assert new["n_matches"] == 1  # h3 客队（换帅后）
    assert new["first_match_date"] == "2023-08-25"
    yi = by_key[("Yi Coach", "4-2-3-1")]
    assert yi["club_tm_id"] == 901
    assert yi["n_matches"] == 3  # h1/h2/h4 客队
    # 丙队桥没通 → hist_no_bridge 按侧计数；桥报告上抛
    assert stats["hist_no_bridge"] == 1
    assert stats["bridge_zh_to_club"] >= 2
    assert stats["live_season"] == "2024-25"
    assert stats["live_season_unlinked"] == 2  # 当季 live 行未链（当季桶）


def test_backfill_preserves_linked_rows_byte_identical(tmp_path: Path) -> None:
    """票面验收：回填不重写已交付行——链接链行先落库，历史链后到逐字节不动。"""
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    _seed_face(conn, ["s1"])  # 2024-25 链接链行
    store = CorpusStore(tmp_path / "corpus")

    def bj_kickoff(month: int, day: int) -> datetime:
        return datetime(2023, month, day, 3, 0)

    kickoff = datetime(2024, 8, 1, 20, 0, tzinfo=UTC)
    fixture_rows = [
        _fixture_row("s1", "英超", kickoff, "甲队", "乙队"),
        # 历史 sid（2023-24，未链）从第一次 derive 就在语料里
        _fixture_row("h1", "英超", bj_kickoff(8, 12), "甲队", "乙队")
        | {"home_goals": 2, "away_goals": 1, "day": "2023-08-11"},
        _fixture_row("h2", "英超", bj_kickoff(8, 19), "甲队", "乙队")
        | {"home_goals": 1, "away_goals": 0, "day": "2023-08-18"},
        _fixture_row("h3", "英超", bj_kickoff(8, 26), "乙队", "甲队")
        | {"home_goals": 0, "away_goals": 3, "day": "2023-08-25"},
    ]
    detail_rows = [
        _detail_row("s1", kickoff, "甲队", "乙队", "4-4-2", "4-2-3-1", "奥帅", "伊帅"),
        _detail_row(
            "h1", bj_kickoff(8, 12), "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        )
        | {"kickoff": datetime(2023, 8, 11, 19, 0, tzinfo=UTC)},
        _detail_row(
            "h2", bj_kickoff(8, 19), "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        )
        | {"kickoff": datetime(2023, 8, 18, 19, 0, tzinfo=UTC)},
        _detail_row("h3", bj_kickoff(8, 26), "乙队", "甲队", "3-5-2", "4-4-2", "y", "x")
        | {"kickoff": datetime(2023, 8, 25, 19, 0, tzinfo=UTC)},
    ]
    _build_corpus(store, fixture_rows, detail_rows)
    duck_con = corpus_duckdb.connect(
        Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    )
    try:
        # 第一拍：hist 侧空 → 历史链全 drop，只有链接链两行落库
        first = formations.derive_formation_family_facet(
            conn, duck_con, now="2026-11-11T00:00:00+00:00"
        )
        assert first["hist_no_bridge"] == 6  # 三场 × 双侧
        before = conn.execute(
            "SELECT * FROM formation_family_facet WHERE team_id IS NOT NULL"
        ).fetchall()
        # 第二拍：hist 侧到位（等价于票 05 的历史链上线），历史行入面
        _seed_hist_bridge_face(conn)
        report = formations.derive_formation_family_facet(
            conn, duck_con, now="2026-12-01T00:00:00+00:00"
        )
        after = conn.execute(
            "SELECT * FROM formation_family_facet WHERE team_id IS NOT NULL"
        ).fetchall()
    finally:
        duck_con.close()
        conn.close()
    assert before == after  # 含 rebuilt_at 逐字节不动
    assert report["preserved"] == len(before)
    assert report["inserted"] == 4  # Old×1 + New×1 + Yi×2（4-2-3-1 与 3-5-2）


def test_coverage_report_marks_dead_zones_and_pre_hist(tmp_path: Path) -> None:
    """票 05 验收③：死区季零行也要出场并如实标注；hist_no_club 桶有测试。"""
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    _seed_hist_bridge_face(conn)
    # 丙队/丁队：桥通（三票）但 hist 名无 TM 俱乐部认领 → hist_no_club
    conn.executemany(
        """
        INSERT INTO hist_matches
            (competition, season, match_date, home_team, away_team, fthg, ftag,
             ftr, psc_home, psc_draw, psc_away)
        VALUES ('E0', '2324', ?, 'Zeta FC', 'Omega FC', ?, ?, 'H', 2.0, 3.5, 4.0)
        """,
        [
            # 欧洲日=universe 北京凌晨场 −8h（z_i 在 bj 9/16/23 03:00）
            ("2023-09-08", 1, 1),
            ("2023-09-15", 2, 0),
            ("2023-09-22", 0, 2),
        ],
    )
    conn.commit()
    store = CorpusStore(tmp_path / "corpus")

    def bj(day: datetime) -> datetime:
        # 北京墙钟凌晨场 → detail 开球 UTC 前一日 19:00（桥语料侧日=UTC 日）
        return day.replace(hour=3)

    def utc_day(day: datetime) -> datetime:
        return day.replace(hour=19, tzinfo=UTC)

    aug = datetime(2023, 8, 12)
    fixture_rows = [
        # 2015-16：早于 hist 窗 → rows=0 → pre_hist_window 注记
        _fixture_row("p1", "英超", bj(datetime(2015, 9, 12)), "甲队", "乙队")
        | {"home_goals": 2, "away_goals": 1, "day": "2015-09-11"},
        # 2023-24：一场阵型齐全（入面）+ 两场桥投票用（阵型缺）+ 四场全缺
        # （formation_unknown）→ known/sides=5/20 < 0.5 → formation_dead_zone
        _fixture_row("h1", "英超", bj(aug), "甲队", "乙队")
        | {"home_goals": 2, "away_goals": 1, "day": "2023-08-11"},
        _fixture_row("h2", "英超", bj(datetime(2023, 8, 19)), "甲队", "乙队")
        | {"home_goals": 1, "away_goals": 0, "day": "2023-08-18"},
        _fixture_row("h3", "英超", bj(datetime(2023, 8, 26)), "乙队", "甲队")
        | {"home_goals": 0, "away_goals": 3, "day": "2023-08-25"},
        *[
            _fixture_row(f"n{i}", "英超", bj(datetime(2023, 8, 20 + i)), "甲队", "乙队")
            | {"home_goals": 0, "away_goals": 0, "day": f"2023-08-2{i}"}
            for i in range(4)
        ],
        # 丙丁场：桥通但无 TM 俱乐部 → hist_no_club（双侧）
        *[
            _fixture_row(
                f"z{i}", "英超", bj(datetime(2023, 9, 9 + 7 * i)), "丙队", "丁队"
            )
            | {"home_goals": [1, 2, 0][i], "away_goals": [1, 0, 2][i]}
            for i in range(3)
        ],
        # 2024-25 一场（有 detail）→ 当季界推到 2024-25
        _fixture_row("L1", "英超", bj(datetime(2024, 8, 12)), "甲队", "乙队"),
    ]
    detail_rows = [
        _detail_row(
            "p1",
            bj(datetime(2015, 9, 12)),
            "甲队",
            "乙队",
            "4-4-2",
            "4-2-3-1",
            "x",
            "y",
        )
        | {"kickoff": utc_day(datetime(2015, 9, 11))},
        _detail_row("h1", bj(aug), "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y")
        | {"kickoff": utc_day(datetime(2023, 8, 11))},
        _detail_row(
            "h2", bj(datetime(2023, 8, 19)), "甲队", "乙队", None, None, "x", "y"
        )
        | {"kickoff": utc_day(datetime(2023, 8, 18))},
        _detail_row(
            "h3", bj(datetime(2023, 8, 26)), "乙队", "甲队", None, None, "y", "x"
        )
        | {"kickoff": utc_day(datetime(2023, 8, 25))},
        *[
            _detail_row(
                f"n{i}",
                bj(datetime(2023, 8, 20 + i)),
                "甲队",
                "乙队",
                None,
                None,
                "x",
                "y",
            )
            | {"kickoff": utc_day(datetime(2023, 8, 20 + i))}
            for i in range(4)
        ],
        *[
            # 丙丁场只主侧有阵型（known 计数压到 5/16，dead_zone 触发）
            _detail_row(
                f"z{i}",
                bj(datetime(2023, 9, 9 + 7 * i)),
                "丙队",
                "丁队",
                "4-4-2",
                None,
                "x",
                "y",
            )
            | {"kickoff": utc_day(datetime(2023, 9, 9 + 7 * i))}
            for i in range(3)
        ],
        _detail_row(
            "L1",
            bj(datetime(2024, 8, 12)),
            "甲队",
            "乙队",
            "4-4-2",
            "4-2-3-1",
            "x",
            "y",
        )
        | {"kickoff": utc_day(datetime(2024, 8, 11))},
    ]
    _build_corpus(store, fixture_rows, detail_rows)
    duck_con = corpus_duckdb.connect(
        Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    )
    try:
        stats = formations.derive_formation_family_facet(
            conn, duck_con, now="2026-11-11T00:00:00+00:00"
        )
        report = formations.coverage_report(conn)
    finally:
        duck_con.close()
        conn.close()
    # 死区季零行也出场：2015-16（pre_hist）+ 2023-24（dead_zone）+ 2024-25
    notes = {item["season"]: item.get("note") for item in report["seasons"]}
    assert [item["season"] for item in report["seasons"]] == [
        "2015-16",
        "2023-24",
        "2024-25",
    ]
    assert "pre_hist_window" in str(notes["2015-16"])
    assert "formation_dead_zone" in str(notes["2023-24"])
    assert notes["2024-25"] is None
    by_season = {item["season"]: item for item in report["seasons"]}
    assert by_season["2015-16"]["rows"] == 0
    assert by_season["2023-24"]["rows"] > 0
    # 丙丁三场 × 双侧：桥通但无 TM 俱乐部 → hist_no_club=6
    assert stats["hist_no_club"] == 6
    # 2023-24 known/sides=5/20（h1 双侧+z 三场主侧知；h2/h3/n0-n3 全缺）
    season_stat = {
        str(k): v for k, v in dict(stats["seasons"]).items() if k == "2023-24"
    }
    assert season_stat["2023-24"] == {"sides": 20, "known": 5}
