"""
六轴数据 ② 阵型族条件面测试（vision-build 票 09；票 10 身份键全切源T）。

族字典=受控常量（未匹配归 other 不炸）；条件面=族 × 赛季 × 教练任期联结
（match_detail 阵型 → 同站中文名折 → srct_coach_tenures manager-of-the-day
指派）；PIT 读缝只取查询时点前已完结的格子（不含当场面）。合成语料走真实
parquet/duckdb/迁移全链，真源网络与真实语料永不进测试。票 10 断言继承纪律：
PIT/差分写/覆盖切片等外部行为断言原样，仅身份输入从 TM 换源T。
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


def _seed_srct_club_seasons(
    conn: sqlite3.Connection, rows: list[tuple[int, str, int, str, str]]
) -> None:
    """srct_club_seasons 行 (league_id, season, team_id, name_cn, name_en)。"""
    conn.executemany(
        """
        INSERT INTO srct_club_seasons
            (league_id, season, team_id, team_name_cn, team_name_tw,
             team_name_en, league_name_cn, league_name_en, source_key,
             parse_version, materialized_at)
        VALUES (?, ?, ?, ?, '繁', ?, '英格兰超级联赛', 'English Premier League',
                'k', 'v', '2026-01-01T00:00:00+00:00')
        """,
        rows,
    )


def _seed_srct_tenures(
    conn: sqlite3.Connection,
    rows: list[tuple[int, str, int, str, str, str | None]],
) -> None:
    """srct_coach_tenures 行 (coach_id, coach_key, team_id, from, to)。"""
    conn.executemany(
        """
        INSERT INTO srct_coach_tenures
            (coach_id, coach_key, coach_name_cn, coach_name_en, team_id,
             team_name_cn, team_name_en, date_from, date_to, role, games,
             wins, draws, losses, parse_version, materialized_at)
        VALUES (?, ?, ?, ?, ?, '名', '名', ?, ?, NULL, 10, 5, 3, 2, 'v',
                '2026-01-01T00:00:00+00:00')
        """,
        [(cid, key, key, key, team, frm, to) for cid, key, team, frm, to in rows],
    )


def _seed_face(conn: sqlite3.Connection, sids: list[str]) -> None:
    """两队（甲/乙）+ 逐 sid 一场已链 fixture + 源T 队籍/任期层。"""
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
    # 源T 队籍：甲队 team 900 / 乙队 team 901（2024-25 枚举）
    _seed_srct_club_seasons(
        conn,
        [
            (36, "2024-2025", 900, "甲队", "Jia FC"),
            (36, "2024-2025", 901, "乙队", "Yi FC"),
        ],
    )
    # 源T 任期：甲队 900 两任主帅（2024-12-01 换帅）；乙队 901 一任
    _seed_srct_tenures(
        conn,
        [
            (1, "Old Manager", 900, "2024-03-01", "2024-11-30"),
            (2, "New Manager", 900, "2024-12-01", None),
            (3, "Yi Coach", 901, "2024-01-01", None),
        ],
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
    """合成环境：主库（链+源T 身份层）+ 语料（universe+detail）+ duckdb 桥。"""
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
        SELECT season, league_fd, league_zh, team_id, club_srct_id, club_name,
               coach_key, coach_role, family, n_matches, n_known,
               first_match_date, last_match_date
        FROM formation_family_facet
        ORDER BY club_name, coach_key, family
        """
    ).fetchall()


def test_derive_builds_family_by_season_by_tenure_cells(facet_env) -> None:
    duck_con, conn = facet_env
    report = formations.derive_formation_family_facet(
        conn, duck_con, now="2026-11-11T00:00:00+00:00"
    )
    rows = _facet_rows(conn)
    by_key = {(r["coach_key"], r["family"]): r for r in rows}
    # 换帅边界：s1/s2 归 Old Manager（4-4-2×2），s3/s4 归 New Manager
    old = by_key[("Old Manager", "4-4-2")]
    assert (old["season"], old["league_fd"], old["league_zh"]) == (
        "2024-25",
        "E0",
        "英超",
    )
    assert (old["club_srct_id"], old["club_name"], old["coach_role"]) == (
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
    assert (yi["club_srct_id"], yi["team_id"], yi["n_matches"], yi["n_known"]) == (
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
    # 换键后首拍=全量重派口径（票 10：preserved=0/inserted=N 是口径事实）
    assert report["preserved"] == 0
    assert report["inserted"] == 4


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
    # rebuilt_at 逐字节不动（票面验收：既有交付行不得重写）
    assert report["preserved"] == 4
    assert report["inserted"] == 0
    assert report["updated"] == 0
    assert report["deleted"] == 0
    rebuilt = {
        row[0] for row in conn.execute("SELECT rebuilt_at FROM formation_family_facet")
    }
    assert rebuilt == {"2026-11-11T00:00:00+00:00"}


def test_derive_drop_paths_are_counted_not_guessed(tmp_path: Path) -> None:
    """诚实丢弃口径：无源T身份/任期空窗/无阵型逐档计数，不硬配。"""
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
    # 队籍枚举：乙队 901 在；甲队 900 在但任期 2024-09-01 才开始；
    # 无桥队/幽灵队不在枚举（= 语料范围外/真缺，同桶不猜）
    _seed_srct_club_seasons(
        conn,
        [
            (36, "2024-2025", 900, "甲队", "Jia FC"),
            (36, "2024-2025", 901, "乙队", "Yi FC"),
        ],
    )
    _seed_srct_tenures(
        conn,
        [
            (1, "Jia Coach", 900, "2024-09-01", None),
            (3, "Yi Coach", 901, "2024-01-01", None),
        ],
    )
    conn.commit()

    def kickoff(month: int, day: int) -> datetime:
        return datetime(2024, month, day, 20, 0, tzinfo=UTC)

    fixture_rows = [
        _fixture_row("s1", "英超", kickoff(8, 1), "无桥队", "乙队"),
        _fixture_row("s2", "英超", kickoff(8, 2), "幽灵队", "乙队"),
        # s3 在 2024-08-03：早于甲队首任 2024-09-01 → 空窗
        _fixture_row("s3", "英超", kickoff(8, 3), "甲队", "乙队"),
    ]
    detail_rows = [
        _detail_row(
            "s1", kickoff(8, 1), "无桥队", "乙队", "4-4-2", "4-2-3-1", "x", "伊帅"
        ),
        _detail_row(
            "s2", kickoff(8, 2), "幽灵队", "乙队", "4-4-2", "4-2-3-1", "x", "伊帅"
        ),
        # s3：主队身份通但空窗指派失败；客队指派成功但阵型缺 → formation_unknown
        _detail_row("s3", kickoff(8, 3), "甲队", "乙队", "4-4-2", None, "x", "伊帅"),
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
    assert stats["no_srct_identity"] == 2  # s1/s2 主队不在枚举
    assert stats["unattributed"] == 1  # s3 主队任期空窗
    assert stats["formation_unknown"] == 1  # s3 客队指派成功但无阵型
    # 乙队三场 4-2-3-1 两场知（s3 缺）→ 归 Yi Coach 唯一存活格
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
    assert report["totals"]["no_srct_identity"] == 0


def test_migration_v53_family_check_matches_controlled_constant(db) -> None:
    """迁移 CHECK 族清单与受控常量同源（字面双写的防漂移钉；v62 沿用）。"""
    row = db.execute(
        "SELECT sql FROM sqlite_master"
        " WHERE type = 'table' AND name = 'formation_family_facet'"
    ).fetchone()
    assert row is not None
    for family in formations.FAMILIES:
        assert f"'{family}'" in str(row["sql"])


def test_name_variant_resolves_via_alias_and_marker(tmp_path: Path) -> None:
    """同站产品名漂移（票 10 实测形态）：受控别名与 (中) 标记剥除可解析。"""
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
    # 枚举侧名：莱斯特城（59）；universe 侧名：莱切斯特城（受控别名）、
    # 桑德兰(中)（站内标记）——2026-11-12 全量实测仅有的两类漂移
    _seed_srct_club_seasons(
        conn,
        [
            (36, "2024-2025", 59, "莱斯特城", "Leicester City"),
            (36, "2024-2025", 65, "桑德兰", "Sunderland"),
        ],
    )
    _seed_srct_tenures(
        conn,
        [
            (7, "Lei Boss", 59, "2024-01-01", None),
            (8, "Sun Boss", 65, "2024-01-01", None),
        ],
    )
    conn.commit()
    kickoff = datetime(2024, 8, 1, 20, 0, tzinfo=UTC)
    store = CorpusStore(tmp_path / "corpus")
    _build_corpus(
        store,
        [_fixture_row("s1", "英超", kickoff, "莱切斯特城", "桑德兰(中)")],
        [
            _detail_row(
                "s1", kickoff, "莱切斯特城", "桑德兰(中)", "4-4-2", "4-2-3-1", "x", "y"
            )
        ],
    )
    duck_con = corpus_duckdb.connect(
        Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    )
    try:
        stats = formations.derive_formation_family_facet(
            conn, duck_con, now="2026-11-11T00:00:00+00:00"
        )
        rows = conn.execute(
            "SELECT club_srct_id, club_name, coach_key FROM formation_family_facet"
        ).fetchall()
    finally:
        duck_con.close()
        conn.close()
    # 变体名不丢队：别名/标记解析接通，不入 no_srct_identity
    assert stats["no_srct_identity"] == 0
    assert stats["rows"] == 2  # 同队主客两侧各一格（4-4-2 / 4-2-3-1）
    assert {(r["club_srct_id"], r["club_name"], r["coach_key"]) for r in rows} == {
        (59, "Leicester City", "Lei Boss"),
        (65, "Sunderland", "Sun Boss"),
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


# ---- 冻结季未链侧（票 05 历史回填 → 票 10 同站折原生解析）----


def _seed_frozen_season(conn: sqlite3.Connection) -> None:
    """冻结季（2023-24）源T 身份层：甲/乙队籍 + 任期（2023-08-20 换帅）。"""
    _seed_srct_club_seasons(
        conn,
        [
            (36, "2023-2024", 900, "甲队", "Jia FC"),
            (36, "2023-2024", 901, "乙队", "Yi FC"),
        ],
    )
    _seed_srct_tenures(
        conn,
        [
            # 甲队 2023-08-20 换帅：PIT 指派随 day 切换
            (1, "Old Manager", 900, "2023-06-01", "2023-08-19"),
            (2, "New Manager", 900, "2023-08-20", None),
            (3, "Yi Coach", 901, "2023-01-01", None),
        ],
    )
    conn.commit()


def test_frozen_season_unlinked_resolves_natively(tmp_path: Path) -> None:
    """票 10：未链冻结季 sid 经同站折原生入面（无 TM 链/hist 桥），team 锚无。"""
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    _seed_face(conn, [])  # 2024-25 链接链身份层
    _seed_frozen_season(conn)
    store = CorpusStore(tmp_path / "corpus")

    def utc_kickoff(month: int, day: int) -> datetime:
        return datetime(2023, month, day, 19, 0, tzinfo=UTC)

    fixture_rows = [
        # 三场历史（universe 中文名 甲队/乙队），sid 不落 source_match_links
        # → 走同站折（不再需要 hist_matches 对侧）
        _fixture_row("h1", "英超", utc_kickoff(8, 11), "甲队", "乙队"),
        _fixture_row("h2", "英超", utc_kickoff(8, 18), "甲队", "乙队"),
        _fixture_row("h3", "英超", utc_kickoff(8, 25), "乙队", "甲队"),
        # 枚举折不出的第三方队（身份缺口分桶）
        _fixture_row("h4", "英超", utc_kickoff(9, 1), "丙队", "乙队"),
        # 当季行：把当季界（=detail 覆盖最大季）推到 2024-25（未链 → 当季桶）
        _fixture_row(
            "live", "英超", datetime(2024, 8, 1, 19, 0, tzinfo=UTC), "甲队", "乙队"
        ),
    ]
    detail_rows = [
        _detail_row(
            "h1", utc_kickoff(8, 11), "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        ),
        _detail_row(
            "h2", utc_kickoff(8, 18), "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        ),
        _detail_row(
            "h3", utc_kickoff(8, 25), "乙队", "甲队", "3-5-2", "4-4-2", "y", "x"
        ),
        _detail_row(
            "h4", utc_kickoff(9, 1), "丙队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        ),
        _detail_row(
            "live",
            datetime(2024, 8, 1, 19, 0, tzinfo=UTC),
            "甲队",
            "乙队",
            "4-4-2",
            "4-2-3-1",
            "x",
            "y",
        ),
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
            SELECT season, league_fd, team_id, club_srct_id, club_name, coach_key,
                   family, n_matches, first_match_date, last_match_date
            FROM formation_family_facet ORDER BY coach_key, family
            """
        ).fetchall()
    finally:
        duck_con.close()
        conn.close()
    # 三场历史 × 两侧全部入面：h1/h2 在 2023-08-19 前 → Old Manager，
    # h3（08-25）→ New Manager（PIT：指派随当日任期态）
    by_key = {(r["coach_key"], r["family"]): r for r in rows}
    old = by_key[("Old Manager", "4-4-2")]
    assert (old["season"], old["league_fd"]) == ("2023-24", "E0")
    assert old["team_id"] is None  # 冻结季未链行无运行面 team 锚
    assert (old["club_srct_id"], old["club_name"]) == (900, "Jia FC")
    assert old["n_matches"] == 2  # h1/h2 主队
    new = by_key[("New Manager", "4-4-2")]
    assert new["n_matches"] == 1  # h3 客队（换帅后）
    assert new["first_match_date"] == "2023-08-25"
    yi = by_key[("Yi Coach", "4-2-3-1")]
    assert yi["club_srct_id"] == 901
    assert yi["n_matches"] == 3  # h1/h2/h4 客队
    # 丙队枚举折不出 → no_srct_identity 按侧计数
    assert stats["no_srct_identity"] == 1
    assert stats["live_season"] == "2024-25"
    assert stats["live_season_unlinked"] == 2  # 当季 live 行未链（当季桶）


def test_backfill_preserves_linked_rows_byte_identical(tmp_path: Path) -> None:
    """票面验收：回填不重写已交付行——链接链行先落库，冻结季身份后到逐字节不动。"""
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    _seed_face(conn, ["s1"])  # 2024-25 链接链身份层（不含 2023-24）
    store = CorpusStore(tmp_path / "corpus")

    def utc_kickoff(month: int, day: int) -> datetime:
        return datetime(2023, month, day, 19, 0, tzinfo=UTC)

    kickoff = datetime(2024, 8, 1, 20, 0, tzinfo=UTC)
    fixture_rows = [
        _fixture_row("s1", "英超", kickoff, "甲队", "乙队"),
        # 冻结季 sid（2023-24，未链）从第一次 derive 就在语料里
        _fixture_row("h1", "英超", utc_kickoff(8, 11), "甲队", "乙队"),
        _fixture_row("h2", "英超", utc_kickoff(8, 18), "甲队", "乙队"),
        _fixture_row("h3", "英超", utc_kickoff(8, 25), "乙队", "甲队"),
    ]
    detail_rows = [
        _detail_row("s1", kickoff, "甲队", "乙队", "4-4-2", "4-2-3-1", "奥帅", "伊帅"),
        _detail_row(
            "h1", utc_kickoff(8, 11), "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        ),
        _detail_row(
            "h2", utc_kickoff(8, 18), "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        ),
        _detail_row(
            "h3", utc_kickoff(8, 25), "乙队", "甲队", "3-5-2", "4-4-2", "y", "x"
        ),
    ]
    _build_corpus(store, fixture_rows, detail_rows)
    duck_con = corpus_duckdb.connect(
        Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    )
    try:
        # 第一拍：2023-24 队籍缺席 → 冻结季侧全 drop，只有链接链两行落库
        first = formations.derive_formation_family_facet(
            conn, duck_con, now="2026-11-11T00:00:00+00:00"
        )
        assert first["no_srct_identity"] == 6  # 三场 × 双侧
        before = conn.execute(
            "SELECT * FROM formation_family_facet WHERE team_id IS NOT NULL"
        ).fetchall()
        # 第二拍：冻结季身份层到位（等价于票 05 的历史链上线），历史行入面
        _seed_frozen_season(conn)
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


def test_coverage_report_marks_dead_zones_and_pre_window(tmp_path: Path) -> None:
    """票 05 验收③：死区季零行也要出场并如实标注；no_srct_identity 桶有测试。"""
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    _seed_face(conn, [])
    _seed_frozen_season(conn)
    store = CorpusStore(tmp_path / "corpus")

    def utc_kickoff(day: datetime) -> datetime:
        return day.replace(hour=19, tzinfo=UTC)

    aug = datetime(2023, 8, 12)
    fixture_rows = [
        # 2015-16：早于源T 队籍窗（枚举 2023-24 起）→ rows=0 → pre_srct_window
        _fixture_row("p1", "英超", utc_kickoff(datetime(2015, 9, 11)), "甲队", "乙队"),
        # 2023-24：一场阵型齐全（入面）+ 两场阵型缺 + 四场全缺
        # （formation_unknown）→ known/sides=5/20 < 0.5 → formation_dead_zone
        _fixture_row("h1", "英超", utc_kickoff(datetime(2023, 8, 11)), "甲队", "乙队"),
        _fixture_row("h2", "英超", utc_kickoff(datetime(2023, 8, 18)), "甲队", "乙队"),
        _fixture_row("h3", "英超", utc_kickoff(datetime(2023, 8, 25)), "乙队", "甲队"),
        *[
            _fixture_row(
                f"n{i}", "英超", utc_kickoff(datetime(2023, 8, 20 + i)), "甲队", "乙队"
            )
            for i in range(4)
        ],
        # 丙丁场：枚举折不出 → no_srct_identity（双侧）
        *[
            _fixture_row(
                f"z{i}",
                "英超",
                utc_kickoff(datetime(2023, 9, 9 + 7 * i)),
                "丙队",
                "丁队",
            )
            for i in range(3)
        ],
        # 2024-25 一场（有 detail）→ 当季界推到 2024-25
        _fixture_row("L1", "英超", utc_kickoff(datetime(2024, 8, 11)), "甲队", "乙队"),
    ]
    detail_rows = [
        _detail_row(
            "p1",
            utc_kickoff(datetime(2015, 9, 11)),
            "甲队",
            "乙队",
            "4-4-2",
            "4-2-3-1",
            "x",
            "y",
        ),
        _detail_row(
            "h1", utc_kickoff(aug), "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        ),
        _detail_row(
            "h2",
            utc_kickoff(datetime(2023, 8, 18)),
            "甲队",
            "乙队",
            None,
            None,
            "x",
            "y",
        ),
        _detail_row(
            "h3",
            utc_kickoff(datetime(2023, 8, 25)),
            "乙队",
            "甲队",
            None,
            None,
            "y",
            "x",
        ),
        *[
            _detail_row(
                f"n{i}",
                utc_kickoff(datetime(2023, 8, 20 + i)),
                "甲队",
                "乙队",
                None,
                None,
                "x",
                "y",
            )
            for i in range(4)
        ],
        *[
            # 丙丁场只主侧有阵型（known 计数压到 5/20，dead_zone 触发）
            _detail_row(
                f"z{i}",
                utc_kickoff(datetime(2023, 9, 9 + 7 * i)),
                "丙队",
                "丁队",
                "4-4-2",
                None,
                "x",
                "y",
            )
            for i in range(3)
        ],
        _detail_row(
            "L1",
            utc_kickoff(datetime(2024, 8, 11)),
            "甲队",
            "乙队",
            "4-4-2",
            "4-2-3-1",
            "x",
            "y",
        ),
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
    # 死区季零行也出场：2015-16（pre_srct）+ 2023-24（dead_zone）+ 2024-25
    notes = {item["season"]: item.get("note") for item in report["seasons"]}
    assert [item["season"] for item in report["seasons"]] == [
        "2015-16",
        "2023-24",
        "2024-25",
    ]
    assert "pre_srct_window" in str(notes["2015-16"])
    assert "formation_dead_zone" in str(notes["2023-24"])
    assert notes["2024-25"] is None
    by_season = {item["season"]: item for item in report["seasons"]}
    assert by_season["2015-16"]["rows"] == 0
    assert by_season["2023-24"]["rows"] > 0
    # 丙丁三场 × 双侧：枚举折不出 → no_srct_identity=6（p1 双侧同桶 → 8）
    assert stats["no_srct_identity"] == 8
    # 2023-24 known/sides=5/20（h1 双侧+z 三场主侧知；h2/h3/n0-n3 全缺）
    season_stat = {
        str(k): v for k, v in dict(stats["seasons"]).items() if k == "2023-24"
    }
    assert season_stat["2023-24"] == {"sides": 20, "known": 5}
    assert report["totals"]["no_srct_identity"] == 8


def test_diff_write_updates_and_deletes_vanished_cells(tmp_path: Path) -> None:
    """差分写另两路（correctness-review 补）：输入漂移→updated，输入消失→deleted。"""
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    # 只需冻结季身份层（2024-25 的 L1 未链 → 当季桶，不消费 2024-25 身份）
    _seed_frozen_season(conn)
    store = CorpusStore(tmp_path / "corpus")

    def utc_kickoff(month: int, day: int) -> datetime:
        return datetime(2023, month, day, 19, 0, tzinfo=UTC)

    fixture_rows = [
        _fixture_row("h1", "英超", utc_kickoff(8, 11), "甲队", "乙队"),
        _fixture_row("h2", "英超", utc_kickoff(8, 18), "甲队", "乙队"),
        _fixture_row("h3", "英超", utc_kickoff(8, 25), "乙队", "甲队"),
        _fixture_row(
            "L1", "英超", datetime(2024, 8, 1, 19, 0, tzinfo=UTC), "甲队", "乙队"
        ),
    ]
    detail_rows = [
        _detail_row(
            "h1", utc_kickoff(8, 11), "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        ),
        _detail_row(
            "h2", utc_kickoff(8, 18), "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
        ),
        _detail_row(
            "h3", utc_kickoff(8, 25), "乙队", "甲队", "3-5-2", "4-4-2", "y", "x"
        ),
        _detail_row(
            "L1",
            datetime(2024, 8, 1, 19, 0, tzinfo=UTC),
            "甲队",
            "乙队",
            "4-4-2",
            "4-2-3-1",
            "x",
            "y",
        ),
    ]
    _build_corpus(store, fixture_rows, detail_rows)
    duck_con = corpus_duckdb.connect(
        Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    )
    try:
        formations.derive_formation_family_facet(
            conn, duck_con, now="2026-11-11T00:00:00+00:00"
        )
        old_before = conn.execute(
            """
            SELECT n_matches, rebuilt_at FROM formation_family_facet
            WHERE coach_key = 'Old Manager'
            """
        ).fetchone()
        # 漂移：换帅日提前到 08-16 → h2（08-18）改归 New Manager
        conn.execute(
            "UPDATE srct_coach_tenures SET date_to = '2023-08-15'"
            " WHERE coach_key = 'Old Manager'"
        )
        conn.execute(
            "UPDATE srct_coach_tenures SET date_from = '2023-08-16'"
            " WHERE coach_key = 'New Manager'"
        )
        conn.commit()
        drifted = formations.derive_formation_family_facet(
            conn, duck_con, now="2026-12-01T00:00:00+00:00"
        )
        old_after = conn.execute(
            """
            SELECT n_matches, rebuilt_at FROM formation_family_facet
            WHERE coach_key = 'Old Manager'
            """
        ).fetchone()
        # 消失：队籍枚举全清 → 同站折全空 → 历史格全灭
        conn.execute("DELETE FROM srct_club_seasons")
        conn.commit()
        emptied = formations.derive_formation_family_facet(
            conn, duck_con, now="2027-01-01T00:00:00+00:00"
        )
        counts_after_empty = conn.execute(
            "SELECT COUNT(*) FROM formation_family_facet"
        ).fetchone()[0]
    finally:
        duck_con.close()
        conn.close()
    # 漂移拍：Old n 2→1（updated+rebuilt_at 换新），New n 1→2（updated）
    assert old_before[0] == 2
    assert old_after[0] == 1
    assert old_after[1] == "2026-12-01T00:00:00+00:00"
    assert drifted["updated"] == 2
    assert drifted["preserved"] == 2  # Yi 两格不动
    # 消失拍：4 格全删；再跑幂等
    assert emptied["deleted"] == 4
    assert counts_after_empty == 0


def test_ghost_coach_double_id_single_facet_identity(tmp_path: Path) -> None:
    """幽灵教练守卫（票面验收）：同教练两个 playerInfo id → facet 单一教练身份。"""
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
    _seed_srct_club_seasons(conn, [(36, "2024-2025", 900, "甲队", "Jia FC")])
    # tdl coach 变量伪 id 现象的银层显影：同一教练（coach_key 同）两个
    # playerInfo id、同区间双行（票 08 钉①：canonical=166402，866275 是伪值）
    _seed_srct_tenures(
        conn,
        [
            (166402, "Olivier Pantaloni", 900, "2024-01-01", None),
            (866275, "Olivier Pantaloni", 900, "2024-01-01", None),
        ],
    )
    conn.commit()
    kickoff = datetime(2024, 8, 1, 20, 0, tzinfo=UTC)
    store = CorpusStore(tmp_path / "corpus")
    _build_corpus(
        store,
        [_fixture_row("s1", "英超", kickoff, "甲队", "甲队")],
        [_detail_row("s1", kickoff, "甲队", "甲队", "4-4-2", "4-2-3-1", "x", "y")],
    )
    duck_con = corpus_duckdb.connect(
        Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    )
    try:
        formations.derive_formation_family_facet(
            conn, duck_con, now="2026-11-11T00:00:00+00:00"
        )
        rows = conn.execute(
            "SELECT coach_key, COUNT(*) AS n FROM formation_family_facet"
            " GROUP BY coach_key"
        ).fetchall()
        prior = formations.coach_family_prior(
            conn, "Olivier Pantaloni", upto_season="2025-26"
        )
    finally:
        duck_con.close()
        conn.close()
    # 双 id 不裂身份：两格（主客）同一 coach_key，读缝键空间单一
    assert [tuple(r) for r in rows] == [("Olivier Pantaloni", 2)]
    assert prior["cells"] == {"4-4-2": 1, "4-2-3-1": 1}
    assert prior["dominant_family"] == "4-2-3-1"  # 并列字典序稳定者


# ---- e2e 一拍（票 10 spec Testing Decisions：采集→silver→derive→S7）----


def test_e2e_bronze_silver_derive_s7_one_beat(tmp_path: Path) -> None:
    """合成环境全链一拍：bronze 三族数据集 → silver 物化 → facet derive →
    S7 观测（映射率计算走通、教练面读源T；spec e2e 验收）。"""
    import json

    from goalx_backend.data import srct_tenure_silver
    from goalx_backend.data.ingest import srct_tenure
    from goalx_backend.evaluation import edge_scan

    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    store = CorpusStore(tmp_path / "corpus")
    careers = [
        {
            "provider": srct.SRCT_PROVIDER,
            "dataset": srct_tenure.COACH_CAREER_DATASET,
            "sid": "41",
            "fetched_at": "2026-11-12T01:30:00+00:00",
            "parser_version": srct_tenure.TENURE_BRONZE_VERSIONS[
                srct_tenure.COACH_CAREER_DATASET
            ],
            "raw_sha": "deadbeef",
            "payload": {
                "once_team": "",
                "now_team": [],
                "transfers": [],
                "tenures": [
                    {
                        "start": "2016-01-01",
                        "end": None,
                        "team_id": 900,
                        "name_cn": "甲队",
                        "name_tw": "甲隊",
                        "name_en": "Jia FC",
                        "matches": 30,
                        "wins": 15,
                        "draws": 8,
                        "losses": 7,
                    }
                ],
                "last_update": "2026-11-12 09:21:11",
            },
        },
        {
            "provider": srct.SRCT_PROVIDER,
            "dataset": srct_tenure.COACH_CAREER_DATASET,
            "sid": "42",
            "fetched_at": "2026-11-12T01:30:00+00:00",
            "parser_version": srct_tenure.TENURE_BRONZE_VERSIONS[
                srct_tenure.COACH_CAREER_DATASET
            ],
            "raw_sha": "deadbeef",
            "payload": {
                "once_team": "",
                "now_team": [["乙队", " ", "", "主教练", "主教緟", "Coach", "Yi FC"]],
                "transfers": [],
                "tenures": [
                    {
                        "start": "2016-01-01",
                        "end": None,
                        "team_id": 901,
                        "name_cn": "乙队",
                        "name_tw": "乙隊",
                        "name_en": "Yi FC",
                        "matches": 30,
                        "wins": 10,
                        "draws": 10,
                        "losses": 10,
                    }
                ],
                "last_update": "2026-11-12 09:21:11",
            },
        },
    ]
    pages = [
        {
            "provider": srct.SRCT_PROVIDER,
            "dataset": srct_tenure.COACH_PAGE_DATASET,
            "sid": f"{cid}@{team}",
            "fetched_at": "2026-11-12T01:30:00+00:00",
            "parser_version": srct_tenure.TENURE_BRONZE_VERSIONS[
                srct_tenure.COACH_PAGE_DATASET
            ],
            "raw_sha": "deadbeef",
            "payload": {
                "name_cn": cn,
                "name_tw": cn,
                "name_en": en,
                "career_cid": cid,
                "career_version": "2026111209",
            },
        }
        for cid, team, cn, en in (
            ("41", 900, "甲帅", "Jia Boss"),
            ("42", 901, "乙帅", "Yi Boss"),
        )
    ]
    leagues = [
        {
            "provider": srct.SRCT_PROVIDER,
            "dataset": srct_tenure.LEAGUE_SEASON_DATASET,
            "sid": "2023-2024-s36",
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
                    "season": "2023-2024",
                },
                "teams": [
                    {
                        "team_id": 900,
                        "name_cn": "甲队",
                        "name_tw": "甲隊",
                        "name_en": "Jia FC",
                    },
                    {
                        "team_id": 901,
                        "name_cn": "乙队",
                        "name_tw": "乙隊",
                        "name_en": "Yi FC",
                    },
                ],
            },
        }
    ]
    store.append_bronze(srct.SRCT_PROVIDER, srct_tenure.COACH_CAREER_DATASET, careers)
    store.append_bronze(srct.SRCT_PROVIDER, srct_tenure.COACH_PAGE_DATASET, pages)
    store.append_bronze(srct.SRCT_PROVIDER, srct_tenure.LEAGUE_SEASON_DATASET, leagues)
    # 语料赛场面（fixture_universe + match_detail parquet，同 _build_corpus）；
    # live 行（2024-25，未链）推高当季界，使 h1（2023-24）成冻结季可入面
    kickoff = datetime(2023, 8, 11, 19, 0, tzinfo=UTC)
    live_kickoff = datetime(2024, 8, 10, 19, 0, tzinfo=UTC)
    _build_corpus(
        store,
        [
            _fixture_row("h1", "英超", kickoff, "甲队", "乙队"),
            _fixture_row("live", "英超", live_kickoff, "甲队", "乙队"),
        ],
        [
            _detail_row("h1", kickoff, "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"),
            _detail_row(
                "live", live_kickoff, "甲队", "乙队", "4-4-2", "4-2-3-1", "x", "y"
            ),
        ],
    )
    # S7 锚：hist E0 一场（窗内，英文名与源T 枚举一致 → 教练面可解析）
    conn.execute(
        """
        INSERT INTO hist_matches
            (competition, season, match_date, home_team, away_team, fthg, ftag,
             ftr, psc_home, psc_draw, psc_away, referee)
        VALUES ('E0', '1617', '2016-08-11', 'Jia FC', 'Yi FC', 2, 1, 'H',
                2.0, 3.5, 4.0, 'Ref A')
        """
    )
    conn.commit()
    duck_con = corpus_duckdb.connect(
        Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    )
    try:
        # ① silver 物化（采集班的库内容投影）
        silver_report = srct_tenure_silver.rebuild_srct_tenure_silver(
            conn, store, now="2026-11-12T02:00:00+00:00"
        )
        assert silver_report["tenure_rows"] == 2
        assert silver_report["club_season_rows"] == 2
        # ② derive（facet 换键后全量重派）
        derive = formations.derive_formation_family_facet(
            conn, duck_con, now="2026-11-12T03:00:00+00:00"
        )
        facet = {
            (r["coach_key"], r["family"]): r
            for r in conn.execute(
                "SELECT coach_key, family, club_srct_id, club_name, n_matches"
                " FROM formation_family_facet"
            ).fetchall()
        }
        assert set(facet) == {("Jia Boss", "4-4-2"), ("Yi Boss", "4-2-3-1")}
        assert facet[("Jia Boss", "4-4-2")]["club_srct_id"] == 900
        assert derive["preserved"] == 0
        assert derive["inserted"] == 2
        # ③ S7 观测（映射率 + REPLACE 幂等；教练面读源T）
        ctx = edge_scan.ScanContext(business_date="2026-11-12")
        first = edge_scan.run_s7(conn, ctx)
        assert first["scanner"] == "s7"
        assert first["linkage"]["coach_both_sides"] == 1.0
        assert first["linkage"]["coach_home"] == 1.0
        edge_scan.run_s7(conn, ctx)
        rows = conn.execute(
            "SELECT payload FROM edge_observations WHERE scanner = 's7'"
        ).fetchall()
        assert len(rows) == 1  # 同窗 REPLACE 不膨胀
        payload = json.loads(str(rows[0][0]))
        # 教练名=源T coach_key（英文名优先键）
        features = payload["features"]
        assert features["coach_dominant_family"]["n"] >= 0
    finally:
        duck_con.close()
        conn.close()
        store.close()
