"""源T 身份缝测试（票 10，spec-srct-cutover）。

身份主空间=源T（ADR-0014）：facet/读缝的俱乐部键=srct team_id、教练键=
coach_key（英文名优先）。本模块钉三件：同站中文名折（universe/match_detail
→ srct_club_seasons 枚举）、hist 英文名折（三级词法+受控别名，票 09 对账
桥机)、任期装载（manager-of-the-day 输入形态 + 幽灵教练守卫）。真实捕获
样本（西布朗/莱斯特城形态）取自 2026-11-12 首班语料实测（research/09）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from goalx_backend import db
from goalx_backend.data import srct_identity
from goalx_backend.data.coach_changes import assign_manager_day


@pytest.fixture
def identity_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    yield conn
    conn.close()


def _seed_club_seasons(conn: sqlite3.Connection) -> None:
    """srct_club_seasons 合成行（matchResult 枚举形态；2026-11-12 实测名）。"""
    conn.executemany(
        """
        INSERT INTO srct_club_seasons
            (league_id, season, team_id, team_name_cn, team_name_tw,
             team_name_en, league_name_cn, league_name_en, source_key,
             parse_version, materialized_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'k', 'v', '2026-11-12T00:00:00+00:00')
        """,
        [
            # 2015-2016 英超：西布罗姆维奇（en 名带站内缩写括注，票 09 实测）
            (
                36,
                "2015-2016",
                18,
                "西布罗姆维奇",
                "西布朗",
                "West Bromwich(WBA)",
                "英格兰超级联赛",
                "English Premier League",
            ),
            (
                36,
                "2015-2016",
                59,
                "莱斯特城",
                "李斯特城",
                "Leicester City",
                "英格兰超级联赛",
                "English Premier League",
            ),
            (
                36,
                "2024-2025",
                18,
                "西布罗姆维奇",
                "西布朗",
                "West Bromwich(WBA)",
                "英格兰超级联赛",
                "English Premier League",
            ),
            (
                37,
                "2024-2025",
                901,
                "甲队",
                "甲隊",
                "Jia FC",
                "英格兰冠军联赛",
                "England Championship",
            ),
        ],
    )
    conn.commit()


def _seed_coach_tenures(conn: sqlite3.Connection) -> None:
    conn.executemany(
        """
        INSERT INTO srct_coach_tenures
            (coach_id, coach_key, coach_name_cn, coach_name_en, team_id,
             team_name_cn, team_name_en, date_from, date_to, role, games,
             wins, draws, losses, parse_version, materialized_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 10, 5, 3, 2, 'v',
                '2026-11-12T00:00:00+00:00')
        """,
        [
            (
                166402,
                "Olivier Pantaloni",
                "奥利维耶·潘塔洛尼",
                "Olivier Pantaloni",
                18,
                "西布罗姆维奇",
                "West Bromwich(WBA)",
                "2023-07-01",
                None,
                "主教练",
            ),
            # 幽灵教练守卫样本：同一教练第二个 playerInfo id（tdl 伪 id 现象的
            # 银层显影）——同名同区间双行
            (
                866275,
                "Olivier Pantaloni",
                "奥利维耶·潘塔洛尼",
                "Olivier Pantaloni",
                18,
                "西布罗姆维奇",
                "West Bromwich(WBA)",
                "2023-07-01",
                None,
                "主教练",
            ),
            (
                77,
                "Yi Coach",
                "伊帅",
                "Yi Coach",
                901,
                "甲队",
                "Jia FC",
                "2024-01-01",
                "2024-06-30",
                None,
            ),
        ],
    )
    conn.commit()


# —— 季码折算 ——


def test_season_converters_roundtrip() -> None:
    assert srct_identity.srct_season_of("2024-25") == "2024-2025"
    assert srct_identity.srct_season_of("2015-16") == "2015-2016"
    assert srct_identity.facet_season_of("2015-2016") == "2015-16"
    assert srct_identity.srct_season_year("2015-2016") == 2015


# —— 同站中文名折（derive 消费） ——


def test_club_index_resolves_same_site_names(identity_db: sqlite3.Connection) -> None:
    _seed_club_seasons(identity_db)
    index = srct_identity.srct_club_index(identity_db)
    assert index.resolve("2015-2016", "西布罗姆维奇") == (18, "West Bromwich(WBA)")
    assert index.resolve("2024-2025", "甲队") == (901, "Jia FC")
    # 缺席（该季枚举无此名）= None（诚实缺，不猜联赛）
    assert index.resolve("2024-2025", "丙队") is None


def test_club_index_name_variants_resolve(identity_db: sqlite3.Connection) -> None:
    """同站产品名漂移（2026-11-12 实测）：(中) 后缀与莱切斯特城/莱斯特城。"""
    _seed_club_seasons(identity_db)
    index = srct_identity.srct_club_index(identity_db)
    # "(中)" 站内标记（桑德兰(中)/赫尔城(中) 实测形态）
    assert index.resolve("2015-2016", "西布罗姆维奇(中)") == (18, "West Bromwich(WBA)")
    # 受控别名：universe 莱切斯特城 ↔ 枚举莱斯特城
    assert index.resolve("2015-2016", "莱切斯特城") == (59, "Leicester City")


def test_club_index_conflicting_names_dropped(identity_db: sqlite3.Connection) -> None:
    """同季同名撞两队 → 弃键（不硬配）。"""
    _seed_club_seasons(identity_db)
    identity_db.executemany(
        """
        INSERT INTO srct_club_seasons
            (league_id, season, team_id, team_name_cn, team_name_tw,
             team_name_en, league_name_cn, league_name_en, source_key,
             parse_version, materialized_at)
        VALUES (36, '2024-2025', ?, '甲队', '甲隊', ?, '英格兰超级联赛',
                'English Premier League', 'k', 'v', '2026-11-12T00:00:00+00:00')
        """,
        [(99, "Doppel FC"), (98, "Doppel FC")],
    )
    identity_db.commit()
    index = srct_identity.srct_club_index(identity_db)
    # 同季同名撞多队 → 弃键（后续同键不复活；按弃键计数）
    assert index.resolve("2024-2025", "甲队") is None
    assert index.conflicts == 1
    # 他季同名的合法键不受牵连
    assert index.resolve("2015-2016", "西布罗姆维奇") == (18, "West Bromwich(WBA)")


# —— hist 英文名折（six_axes/pedigree 消费） ——


def test_hist_name_index_three_tier_plus_aliases(
    identity_db: sqlite3.Connection,
) -> None:
    """fd hist 名 → 源T team：三级词法原生 + 受控别名（实测 3/58 缺口项）。"""
    _seed_club_seasons(identity_db)
    identity_db.executemany(
        """
        INSERT INTO srct_club_seasons
            (league_id, season, team_id, team_name_cn, team_name_tw,
             team_name_en, league_name_cn, league_name_en, source_key,
             parse_version, materialized_at)
        VALUES (36, '2015-2016', ?, ?, ?, ?, '英格兰超级联赛',
                'English Premier League', 'k', 'v', '2026-11-12T00:00:00+00:00')
        """,
        [
            (27, "曼联", "曼聯", "Manchester United"),
            (52, "狼队", "狼隊", "Wolves"),
            (336, "谢周三", "謝週三", "Sheffield Wednesday"),
            (353, "彼得堡联", "彼得堡聯", "Peterborough United"),
        ],
    )
    identity_db.commit()
    index = srct_identity.srct_hist_name_index(identity_db)
    # fd 缩写形态（ABBREVIATIONS 词元展开）+ 词元子集原生命中
    assert index.resolve("Man United") == 27
    assert index.resolve("Wolves") == 52
    # 受控别名（词法不通的实测三例）
    assert index.resolve("West Brom") == 18
    assert index.resolve("Sheffield Weds") == 336
    assert index.resolve("Peterboro") == 353
    # 完全名直配
    assert index.resolve("Leicester City") == 59
    # 未知名 = None（不硬配）
    assert index.resolve("Zeta FC") is None


# —— 任期装载（manager-of-the-day 输入形态） ——


def test_srct_tenures_by_team_maps_role_and_order(
    identity_db: sqlite3.Connection,
) -> None:
    _seed_coach_tenures(identity_db)
    by_team = srct_identity.srct_tenures_by_team(identity_db)
    jia = by_team[901]
    assert len(jia) == 1
    # 源T role 域（主教练/NULL）折 manager——caretaker 分类随 TM 退役消失
    assert (jia[0].coach_name, jia[0].role) == ("Yi Coach", "manager")
    assert (jia[0].date_from, jia[0].date_to) == ("2024-01-01", "2024-06-30")
    # 指派语义复用 assign_manager_day（区间逻辑=缝契约）
    assert assign_manager_day(jia, "2024-03-01") == ("Yi Coach", "manager")
    assert assign_manager_day(jia, "2024-07-01") is None


def test_srct_tenures_ghost_coach_double_id_collapses(
    identity_db: sqlite3.Connection,
) -> None:
    """幽灵教练守卫：同教练两个 playerInfo id（tdl 伪 id 现象）→ 一条任期。"""
    _seed_coach_tenures(identity_db)
    by_team = srct_identity.srct_tenures_by_team(identity_db)
    wba = by_team[18]
    assert len(wba) == 1
    assert wba[0].coach_name == "Olivier Pantaloni"
    # 指派只出一个教练身份（换队保族/重逢旧主的键不裂）
    assert assign_manager_day(wba, "2024-08-01") == ("Olivier Pantaloni", "manager")


def test_srct_tenures_loader_orders_by_date_from(
    identity_db: sqlite3.Connection,
) -> None:
    """date_from 升序（assign_manager_day 反向首见=最晚上任语义的前提）。"""
    _seed_coach_tenures(identity_db)
    identity_db.execute(
        """
        INSERT INTO srct_coach_tenures
            (coach_id, coach_key, coach_name_cn, coach_name_en, team_id,
             team_name_cn, team_name_en, date_from, date_to, role, games,
             wins, draws, losses, parse_version, materialized_at)
        VALUES (166403, 'Old Coach', '老帅', 'Old Coach', 18, '西布罗姆维奇',
                'West Bromwich(WBA)', '2020-01-01', '2023-06-30', NULL, 10, 5,
                3, 2, 'v', '2026-11-12T00:00:00+00:00')
        """
    )
    identity_db.commit()
    wba = srct_identity.srct_tenures_by_team(identity_db)[18]
    assert [t.coach_name for t in wba] == ["Old Coach", "Olivier Pantaloni"]


# —— 迁移 v62：facet 换键重建 ——


def test_v62_rebuild_rekeys_facet_drops_tm_rows(tmp_path: object) -> None:
    """v61→v62：club_tm_id/coach_name → club_srct_id/coach_key；TM 键旧行
    整表弃（全量重派口径——迁移不 import data 域做键翻译，运维步先快照）。"""
    import pathlib

    from goalx_backend import db as db_mod

    conn = db.connect(pathlib.Path(str(tmp_path)) / "goalx.db")
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(db_mod, "MIGRATIONS", db_mod.MIGRATIONS[:61])
            assert db.migrate(conn) == 61
        conn.execute(
            """
            INSERT INTO formation_family_facet
                (season, league_fd, league_zh, team_id, club_tm_id, club_name,
                 coach_name, coach_role, family, n_matches, n_known,
                 first_match_date, last_match_date, rebuilt_at)
            VALUES ('2023-24', 'E0', '英超', NULL, 900, 'Jia FC', 'Old Manager',
                    'manager', '4-4-2', 8, 8, '2023-08-01', '2024-05-01', 't')
            """
        )
        conn.commit()
        assert db.migrate(conn) == 62
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(formation_family_facet)")
        }
        assert {"club_srct_id", "coach_key"} <= cols
        assert "club_tm_id" not in cols
        assert "coach_name" not in cols
        # 旧键空间行整表弃（迁移内不做键翻译；重派由 derive 全量补）
        assert (
            conn.execute("SELECT COUNT(*) FROM formation_family_facet").fetchone()[0]
            == 0
        )
        # 新 UNIQUE 形状：双行同格（换键后）撞约束
        insert_sql = """
            INSERT INTO formation_family_facet
                (season, league_fd, league_zh, team_id, club_srct_id, club_name,
                 coach_key, coach_role, family, n_matches, n_known,
                 first_match_date, last_match_date, rebuilt_at)
            VALUES ('2023-24', 'E0', '英超', NULL, 18, 'West Bromwich(WBA)',
                    'Old Coach', 'manager', '4-4-2', 1, 1, '2023-08-01',
                    '2023-08-01', 't')
        """
        conn.execute(insert_sql)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert_sql)
        # 教练索引随新键重建
        index_cols = {
            row["name"]
            for row in conn.execute(  # sqlite_master 系统目录读
                "SELECT name FROM pragma_index_info('idx_formation_facet_coach')"
            )
        }
        assert index_cols == {"coach_key", "season"}
    finally:
        conn.close()


def test_v62_migration_idempotent_via_runner(identity_db: sqlite3.Connection) -> None:
    """迁移幂等：runner 记账跳过（db.migrate 重跑同版本）。"""
    assert db.migrate(identity_db) >= 62
    assert db.migrate(identity_db) == db.current_version(identity_db)
    cols = {
        row["name"]
        for row in identity_db.execute("PRAGMA table_info(formation_family_facet)")
    }
    assert "club_srct_id" in cols
