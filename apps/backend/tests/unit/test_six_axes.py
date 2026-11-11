"""
六轴联结读缝 + S7 扫描器测试（mining-loop 票 06）。

联结读缝=hist_matches 锚的 as-of 特征装配（教练/底蕴/裁判/阵型族四面，
PIT：全部特征开球前可知）；三窗纪律与 embargo 由单测钉（窗界后行不改
变窗内读数）；S7=研究定拍扫描器（观测 REPLACE 幂等，读数进周批 read_faces）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from goalx_backend.data import formations
from goalx_backend.db import connect, migrate
from goalx_backend.evaluation import edge_scan, six_axes, weekly_agent


@pytest.fixture
def seam_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """搜索窗读缝测试库：E0 三场（1617）+ 窗界外对照 + 四面身份层。"""
    conn = connect(tmp_path / "goalx.db")
    migrate(conn)
    conn.executemany(
        """
        INSERT INTO hist_matches
            (competition, season, match_date, home_team, away_team, fthg, ftag,
             ftr, psc_home, psc_draw, psc_away, referee)
        VALUES ('E0', ?, ?, ?, ?, ?, ?, ?, ?, 3.5, ?, ?)
        """,
        [
            # 1617 窗内两场（同裁判 Ref A：先验逐场累计）+ 一场 2122 窗尾
            ("1617", "2016-08-11", "Jia FC", "Yi FC", 2, 1, "H", 2.0, 4.0, "Ref A"),
            ("1617", "2016-08-18", "Jia FC", "Zeta FC", 1, 0, "H", 2.5, 3.8, "Ref A"),
            ("2122", "2021-08-14", "Jia FC", "Yi FC", 0, 2, "A", 3.0, 2.2, "Ref B"),
        ],
    )
    # 窗界外对照（embargo 测试追加用，初始即插入验证不影响窗内读数）
    conn.execute(
        """
        INSERT INTO hist_matches
            (competition, season, match_date, home_team, away_team, fthg, ftag,
             ftr, psc_home, psc_draw, psc_away, referee)
        VALUES ('E0', '2223', '2022-08-13', 'Jia FC', 'Yi FC', 3, 3, 'D', 2.0, 2.0,
                2.0, 'Ref A')
        """
    )
    # TM 任期：club 900=Jia FC（2016-01-01 起 Old Coach）；club 901=Yi FC
    # （2017-02-01 才上任 → 1617 窗内空窗=unattributed 诚实缺）；Zeta 无任期
    conn.executemany(
        """
        INSERT INTO tm_coach_tenures
            (club_tm_id, club_name, role, coach_tm_id, coach_name, date_from,
             date_to, games, first_seen_at, observed_at, parse_version)
        VALUES (?, ?, 'manager', ?, ?, ?, NULL, 10, '2026-01-01', '2026-01-01',
                'tm_coach_v1')
        """,
        [
            (900, "Jia FC", 1, "Old Coach", "2016-01-01"),
            (901, "Yi FC", 3, "Yi Coach", "2017-02-01"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO tm_club_seasons
            (league_tm_code, season, club_tm_id, club_name, first_seen_at,
             observed_at)
        VALUES ('GB1', '1617', ?, ?, '2026-01-01', '2026-01-01')
        """,
        [(900, "Jia FC"), (901, "Yi FC")],
    )
    # 底蕴：1617 行齐；2122 缺（诚实缺面）；2223 行（窗界外，embargo 探针）
    conn.executemany(
        """
        INSERT INTO club_pedigree
            (team, season, top_flight_streak, points_pct_l5, l5_seasons,
             tier1_titles, major_honours, rebuilt_at)
        VALUES (?, ?, ?, ?, 5, ?, ?, '2026-01-01T00:00:00+00:00')
        """,
        [
            ("Jia FC", "1617", 5, 0.62, 1, 2),
            ("Yi FC", "1617", 2, 0.41, 0, 0),
            ("Jia FC", "2223", 9, 0.71, 2, 3),
            ("Yi FC", "2223", 4, 0.44, 0, 0),
        ],
    )
    # 阵型族面（票 05 读缝）：Old Coach 上一季 4-4-2 为主 + 当季 09 月格
    # （M1 的 08-11 视角必须看不到当季格）
    conn.executemany(
        """
        INSERT INTO formation_family_facet
            (season, league_fd, league_zh, team_id, club_tm_id, club_name,
             coach_name, coach_role, family, n_matches, n_known,
             first_match_date, last_match_date, rebuilt_at)
        VALUES (?, 'E0', '英超', NULL, ?, ?, ?, 'manager', ?, ?, ?,
                ?, ?, '2026-01-01T00:00:00+00:00')
        """,
        [
            (
                "2015-16",
                900,
                "Jia FC",
                "Old Coach",
                "4-4-2",
                8,
                8,
                "2015-08-01",
                "2016-05-01",
            ),
            (
                "2015-16",
                900,
                "Jia FC",
                "Old Coach",
                "4-3-3",
                2,
                10,
                "2015-08-01",
                "2016-05-01",
            ),
            (
                "2016-17",
                900,
                "Jia FC",
                "Old Coach",
                "3-5-2",
                3,
                3,
                "2016-09-01",
                "2016-09-30",
            ),
        ],
    )
    conn.commit()
    yield conn
    conn.close()


def test_seam_assembles_all_four_faces(seam_db: sqlite3.Connection) -> None:
    """四面联结：教练 as-of 指派/底蕴直键/裁判先验/阵型族 PIT 读缝。"""
    readings = six_axes.match_readings(seam_db)
    by_date = {r.match_date: r for r in readings}
    m1 = by_date["2016-08-11"]
    # 教练：Jia→club 900→Old Coach（上任 2016-01-01，08-11=223 天）
    assert m1.home.coach_name == "Old Coach"
    assert m1.home.tenure_days == 223
    assert m1.home.club_tm_id == 900
    # Yi FC 任期 2017-02 起 → 窗内空窗，诚实缺
    assert m1.away.coach_name is None
    # Zeta FC：不在 TM 宇宙 → club=None
    assert by_date["2016-08-18"].away.club_tm_id is None
    # 底蕴：1617 行直键
    assert m1.home.top_flight_streak == 5
    assert m1.home.points_pct_l5 == pytest.approx(0.62)
    assert m1.away.major_honours == 0
    # 裁判先验：M1 先验 0 场；M2 同裁判 Ref A 先验 1 场（M1 总进球 3）
    m2 = by_date["2016-08-18"]
    assert m1.referee_prior_n == 0
    assert m1.referee_prior_avg_goals is None
    assert m2.referee_prior_n == 1
    assert m2.referee_prior_avg_goals == pytest.approx(3.0)
    # 阵型族 PIT：M1 视角只见 2015-16 格（4-4-2 支配族；当季 3-5-2 格
    # last=2016-09-30 > on_day 不可见）
    assert m1.home.family == "4-4-2"
    assert m1.home.family_n_known == 10
    # 残差：M1 主胜 psc_home=2.0 → 1 − 0.5 = 0.5；客负 0 − 1/4 = −0.25
    assert m1.home.residual == pytest.approx(0.5)
    assert m1.away.residual == pytest.approx(-0.25)


def test_search_window_bounds_exclude_out_of_window(
    seam_db: sqlite3.Connection,
) -> None:
    """三窗纪律：读口只吃搜索窗 1617–2122；2223 不入。"""
    readings = six_axes.match_readings(seam_db)
    assert {r.season for r in readings} == {"1617", "2122"}
    assert all(r.competition == "E0" for r in readings)


def test_embargo_window_edge_invariance(seam_db: sqlite3.Connection) -> None:
    """embargo 断言：窗界后行（2223 面/裁判后续场）不改变窗内读数。"""
    before = six_axes.match_readings(seam_db)
    # 追加下一窗首季数据：hist 场 + 底蕴行 + 当季阵型格 + 窗外任期延长
    seam_db.execute(
        """
        INSERT INTO hist_matches
            (competition, season, match_date, home_team, away_team, fthg, ftag,
             ftr, psc_home, psc_draw, psc_away, referee)
        VALUES ('E0', '2223', '2022-12-26', 'Jia FC', 'Yi FC', 1, 1, 'D',
                2.4, 3.1, 3.0, 'Ref A')
        """
    )
    seam_db.execute(
        """
        INSERT INTO formation_family_facet
            (season, league_fd, league_zh, team_id, club_tm_id, club_name,
             coach_name, coach_role, family, n_matches, n_known,
             first_match_date, last_match_date, rebuilt_at)
        VALUES ('2016-17', 'E0', '英超', NULL, 900, 'Jia FC', 'Old Coach',
                'manager', '4-2-3-1', 5, 5, '2016-08-01', '2022-12-25',
                '2026-01-01T00:00:00+00:00')
        """
    )
    seam_db.commit()
    after = six_axes.match_readings(seam_db)
    assert before == after  # 窗内读数逐字段不变（含 family：upto_season 截断）


def test_linkage_report_rates(seam_db: sqlite3.Connection) -> None:
    """映射率报告（人裁门槛实测面）：窗内 3 场的逐面比率。"""
    report = six_axes.linkage_report(six_axes.match_readings(seam_db))
    assert report["matches"] == 3
    # 教练：Jia 三场主侧全通；Yi 客侧仅 2122 场（2017-02 上任覆盖 2021）
    # 通；1617 两场空窗 + Zeta 一场无身份
    assert report["coach_home"] == 1.0
    assert report["coach_away"] == pytest.approx(1 / 3, abs=1e-4)
    assert report["coach_both_sides"] == pytest.approx(1 / 3, abs=1e-4)
    # 底蕴 1617 行齐、2122 缺 → 主侧 2/3；裁判全覆盖；收盘锚全覆盖
    assert report["pedigree_home"] == pytest.approx(2 / 3, abs=1e-4)
    assert report["referee"] == pytest.approx(1.0)
    assert report["closing_anchor"] == pytest.approx(1.0)


def test_feature_importance_structure(seam_db: sqlite3.Connection) -> None:
    """特征重要性首产物：η² + 分层读数齐面（描述统计，无判定字段）。"""
    payload = six_axes.search_window_reading(seam_db)
    assert set(payload) >= {"run_params", "linkage", "features"}
    features = payload["features"]
    assert "coach_tenure_days" in features
    assert "coach_dominant_family" in features
    assert "referee_prior_avg_goals_vs_total_goals" in features
    for face in features.values():
        assert face["n"] >= 0
        assert "eta_sq" in face
        assert isinstance(face["strata"], list)
    # run_params 留痕（可复算口径）
    assert payload["run_params"]["window"] == ["1617", "2122"]


def test_run_s7_observation_idempotent(seam_db: sqlite3.Connection) -> None:
    """S7 e2e 一拍：观测落库（同窗 REPLACE 幂等）+ 周批 read_faces 可见。"""
    ctx = edge_scan.ScanContext(business_date="2026-11-11")
    first = edge_scan.run_s7(seam_db, ctx)
    assert first["scanner"] == "s7"
    rows = seam_db.execute(
        "SELECT window_key, payload FROM edge_observations WHERE scanner = 's7'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "search:1617-2122"
    payload = json.loads(str(rows[0][1]))
    assert payload["run_params"]["competitions"] == ["E0", "E1"]
    # 重跑同窗：REPLACE 不膨胀
    edge_scan.run_s7(seam_db, ctx)
    count = seam_db.execute(
        "SELECT COUNT(*) FROM edge_observations WHERE scanner = 's7'"
    ).fetchone()[0]
    assert count == 1
    # read_faces 注入
    faces = weekly_agent.read_faces(seam_db, lessons_dir=None)
    assert faces["s7_feature_reading"]["run_params"]["window"] == ["1617", "2122"]
    # SCANNER_LINKS s7 空挂（票 07 注册前零尝试不炸）
    assert edge_scan.hypothesis_registry.SCANNER_LINKS["s7"] == ()
    assert first["linked"] == {}


def test_s7_not_in_daily_active_scanners() -> None:
    """research 定拍：s7 注册在册但不进日扫编排（窗冻结，日跑空转）。"""
    assert "s7" in edge_scan.SCANNERS
    assert "s7" not in edge_scan.ACTIVE_SCANNERS
    assert edge_scan.SCANNERS["s7"].cadence == "research"


def test_write_search_report_fail_loud_without_observation(
    seam_db: sqlite3.Connection, tmp_path: Path
) -> None:
    """报告落档：观测缺席 fail-loud；在场时双视图写盘且 run_params 全量。"""
    with pytest.raises(RuntimeError, match="s7 观测缺席"):
        six_axes.write_search_report(seam_db, tmp_path)
    edge_scan.run_s7(seam_db, edge_scan.ScanContext(business_date="2026-11-11"))
    md_path = six_axes.write_search_report(seam_db, tmp_path)
    assert md_path == tmp_path / "reports" / f"{six_axes.REPORT_BASENAME}.md"
    assert (tmp_path / "reports" / f"{six_axes.REPORT_BASENAME}.json").exists()
    md = (tmp_path / "reports" / f"{six_axes.REPORT_BASENAME}.md").read_text()
    assert "六轴搜索窗特征重要性首报告" in md
    assert "run_params" in md
    assert six_axes.latest_observation(seam_db)["linkage"]["matches"] == 3


def test_facet_season_conversion() -> None:
    assert six_axes.facet_season("1617") == "2016-17"
    assert six_axes.facet_season("2627") == "2026-27"
    assert six_axes.SEARCH_WINDOW == ("1617", "2122")
    assert six_axes.VALIDATE_WINDOW == ("2223", "2324")
    assert six_axes.CONFIRM_WINDOW == ("2425", "2526")


# formations 读缝回归护栏：six_axes 消费 coach_family_prior 的口径未变
def test_coach_family_prior_still_importable(seam_db: sqlite3.Connection) -> None:
    prior = formations.coach_family_prior(
        seam_db, "Old Coach", upto_season="2016-17", on_day="2016-08-11"
    )
    assert prior["dominant_family"] == "4-4-2"
    assert prior["n_known"] == 10


def test_referee_prior_same_day_invisible(seam_db: sqlite3.Connection) -> None:
    """PIT 断言（Spec 轴修）：同日同裁判多场互不先验（严格早于当日）。"""
    seam_db.execute(
        """
        INSERT INTO hist_matches
            (competition, season, match_date, home_team, away_team, fthg, ftag,
             ftr, psc_home, psc_draw, psc_away, referee)
        VALUES ('E0', '1617', '2016-08-11', 'Yi FC', 'Zeta FC', 0, 0, 'D',
                2.2, 3.2, 3.2, 'Ref A')
        """
    )
    seam_db.commit()
    readings = {r.match_id: r for r in six_axes.match_readings(seam_db)}
    same_day = [
        r
        for r in readings.values()
        if r.match_date == "2016-08-11" and r.referee == "Ref A"
    ]
    # 同日两场 Ref A：先验都=0（当日互不可见）；次日 Ref A 场先验=2
    assert len(same_day) == 2
    assert all(r.referee_prior_n == 0 for r in same_day)
    next_day = next(r for r in readings.values() if r.match_date == "2016-08-18")
    assert next_day.referee_prior_n == 2
    assert next_day.referee_prior_avg_goals == pytest.approx(1.5)  # (3+0)/2


def test_empty_competitions_fail_loud(seam_db: sqlite3.Connection) -> None:
    """correctness F1：空联赛域 fail-loud（SQLite IN () 恒假不静默零行）。"""
    from goalx_backend.data import results as rs_store

    with pytest.raises(ValueError, match="空联赛域"):
        rs_store.hist_rows_in_season_window(
            seam_db, season_from="1617", season_to="2122", competitions=()
        )


def test_coach_name_empty_string_predicates_consistent(
    seam_db: sqlite3.Connection,
) -> None:
    """correctness F2：coach_name 空串下各映射率判定对称（恒 is not None）。"""
    readings = six_axes.match_readings(seam_db)
    for r in readings:
        if r.home.coach_name == "":
            r.home.coach_name = None  # 模拟空串入库形态
    report = six_axes.linkage_report(readings)
    any_side = sum(
        1
        for r in readings
        if r.home.coach_name is not None or r.away.coach_name is not None
    )
    assert report["coach_any_side"] == round(any_side / len(readings), 4)
