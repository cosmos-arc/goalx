"""
六轴数据 ④ 底蕴硬层四因子面测试（vision-build 票 09）。

四因子独立列不强行合成；PIT 断言=只用该赛季开始前可知的输入（近 5 季=
该赛季前 5 季、连续年限=截至上一赛季、升降级=纯后视窗 t+1≤S-1；荣誉=
全史静态任一时点可知）。合成 hist/TM 行走真实迁移库，真实语料不进测试。
"""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend.data import pedigree
from goalx_backend.db import connect, migrate


def _hist(
    season: str,
    day: str,
    home: str,
    away: str,
    hg: int,
    ag: int,
    *,
    comp: str = "E0",
) -> tuple[str, str, str, str, str, int, int, str]:
    """hist_matches 一行（ftr 由比分派生）。"""
    ftr = "H" if hg > ag else ("D" if hg == ag else "A")
    return (comp, season, day, home, away, hg, ag, ftr)


def _seed_hist(
    conn: sqlite3.Connection, rows: list[tuple[str, str, str, str, str, int, int, str]]
) -> None:
    conn.executemany(
        """
        INSERT INTO hist_matches
            (competition, season, match_date, home_team, away_team, fthg, ftag, ftr)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def _seed_tm_seasons(
    conn: sqlite3.Connection, rows: list[tuple[str, str, int, str]]
) -> None:
    """tm_club_seasons 行 (league_tm_code, season, club_tm_id, club_name)。"""
    conn.executemany(
        """
        INSERT INTO tm_club_seasons
            (league_tm_code, season, club_tm_id, club_name, first_seen_at, observed_at)
        VALUES (?, ?, ?, ?, '2026-01-01', '2026-01-01')
        """,
        rows,
    )
    conn.commit()


@pytest.fixture
def hist_db() -> sqlite3.Connection:
    conn = connect(":memory:")
    migrate(conn)
    yield conn
    conn.close()


def _row(conn: sqlite3.Connection, team: str, season: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM club_pedigree WHERE team = ? AND season = ?", (team, season)
    ).fetchone()
    assert row is not None, f"{team} {season} 行缺失"
    return row


def test_derive_four_factors_with_pit_windows(hist_db: sqlite3.Connection) -> None:
    # Man United：2324 全胜（pct=1.0）→ 2425 全败（pct=0.0）→ 2526 触发行
    # （自季赛果绝不可入本季因子）；Rovers=2024 降英冠 2025 回英超（频谱后
    # 视窗：升回信号落在 2626 行）；Wanderers=2024 缺席（窗内离开=降级类）
    _seed_hist(
        hist_db,
        [
            _hist("2324", "2024-01-13", "Man United", "Rovers", 2, 0),
            _hist("2324", "2024-02-13", "Rovers", "Man United", 0, 3),
            _hist("2425", "2025-01-11", "Man United", "Rovers", 0, 2),
            _hist("2425", "2025-02-11", "Rovers", "Man United", 1, 0),
            _hist("2526", "2026-01-10", "Man United", "Rovers", 5, 0),
            _hist("2526", "2026-01-10", "Wanderers", "Veteran", 1, 1),
            _hist("2626", "2026-09-12", "Rovers", "Filler", 1, 1),
        ],
    )
    _seed_tm_seasons(
        hist_db,
        [
            ("GB1", "2023", 100, "Man United"),
            ("GB1", "2024", 100, "Man United"),
            ("GB1", "2023", 200, "Rovers"),
            ("GB2", "2024", 200, "Rovers"),
            ("GB1", "2025", 200, "Rovers"),
            ("GB1", "2023", 300, "Wanderers"),
            ("GB1", "2025", 300, "Wanderers"),
            ("GB1", "2025", 100, "Man United"),
        ],
    )
    stats = pedigree.derive_club_pedigree(hist_db, now="2026-11-11T00:00:00+00:00")
    # ---- 近 5 季积分百分位（PIT：只用前季） ----
    mu_2425 = _row(hist_db, "Man United", "2425")
    assert mu_2425["points_pct_l5"] == pytest.approx(1.0)  # 只有 2324（1.0）
    assert mu_2425["l5_seasons"] == 1
    mu_2526 = _row(hist_db, "Man United", "2526")
    # 前季=2324(1.0)+2425(0.0)；2526 自季 5:0 大胜绝不入
    assert mu_2526["points_pct_l5"] == pytest.approx(0.5)
    assert mu_2526["l5_seasons"] == 2
    # ---- 顶级联赛连续年限（PIT：截至上一赛季） ----
    assert mu_2425["top_flight_streak"] == 1  # 截至 2023：GB1 在
    assert mu_2526["top_flight_streak"] == 2  # 截至 2024：2023+2024 连续
    rovers = _row(hist_db, "Rovers", "2526")
    assert rovers["top_flight_streak"] == 0  # 截至 2024：GB2 → 断（2025 不入）
    # ---- 升降级频谱（PIT：纯后视窗 t+1 ≤ S-1） ----
    # 2526 行只看 ≤2024：2023 GB1→2024 GB2 = 降级 1；2024→2025 升级未到窗
    assert (rovers["promo_to_tier1"], rovers["releg_from_tier1"]) == (0, 1)
    rovers_next = _row(hist_db, "Rovers", "2626")
    # 2626 行看 ≤2025：降级+升级各 1；年限截至 2025=1
    assert (rovers_next["promo_to_tier1"], rovers_next["releg_from_tier1"]) == (1, 1)
    assert rovers_next["top_flight_streak"] == 1
    wanderers = _row(hist_db, "Wanderers", "2526")
    # 2023 GB1→2024 缺席（窗内=离开受采面）= 降级类；2025 升回未到窗
    assert (wanderers["promo_to_tier1"], wanderers["releg_from_tier1"]) == (0, 1)
    # 稳定在英超：无升降级
    assert (mu_2526["promo_to_tier1"], mu_2526["releg_from_tier1"]) == (0, 0)
    # ---- 荣誉计数（全史静态：任一时点可知 → 两季同值） ----
    assert (mu_2425["tier1_titles"], mu_2425["major_honours"]) == (20, 28)
    assert (mu_2526["tier1_titles"], mu_2526["major_honours"]) == (20, 28)
    # 种子外球队（Rovers/Wanderers/Veteran）= 0/0
    assert (rovers["tier1_titles"], rovers["major_honours"]) == (0, 0)
    # TM 解析注记
    assert mu_2526["club_tm_id"] == 100
    # hist 出现即行：Man United×3 + Rovers×4 + Wanderers/Veteran/Filler 各一 = 10
    assert stats["rows"] == 10


def test_l5_window_caps_at_five_prior_seasons(hist_db: sqlite3.Connection) -> None:
    # 六个前季在场（1920 pct=1.0、其余五季 0.0）→ l5 只取最近五季，1920 出窗
    rows = [
        _hist("1920", "2020-01-11", "Veteran", "Filler", 9, 0),
        _hist("2526", "2026-01-10", "Veteran", "Filler", 0, 1),
    ]
    for pos, season in enumerate(("2021", "2122", "2223", "2324", "2425")):
        rows.append(_hist(season, f"2024-03-1{pos}", "Veteran", "Filler", 0, 2))
    _seed_hist(hist_db, rows)
    pedigree.derive_club_pedigree(hist_db, now="2026-11-11T00:00:00+00:00")
    veteran = _row(hist_db, "Veteran", "2526")
    assert veteran["l5_seasons"] == 5
    assert veteran["points_pct_l5"] == pytest.approx(0.0)  # 1.0 的 1920 被截出窗


def test_tm_unresolved_team_degrades_to_null_not_zero(
    hist_db: sqlite3.Connection,
) -> None:
    _seed_hist(hist_db, [_hist("2425", "2025-04-01", "Strangers", "Filler", 1, 1)])
    pedigree.derive_club_pedigree(hist_db, now="2026-11-11T00:00:00+00:00")
    row = _row(hist_db, "Strangers", "2425")
    # TM 未解析：年限/频谱= NULL（诚实缺，不当 0 用）；积分/荣誉照常
    assert row["club_tm_id"] is None
    assert row["top_flight_streak"] is None
    assert row["promo_to_tier1"] is None
    assert row["releg_from_tier1"] is None
    assert row["l5_seasons"] == 0
    assert row["points_pct_l5"] is None  # 无前季不造率
    assert (row["tier1_titles"], row["major_honours"]) == (0, 0)


def test_first_observed_season_row_has_no_prior(hist_db: sqlite3.Connection) -> None:
    _seed_hist(hist_db, [_hist("2425", "2025-04-01", "Newcomers", "Filler", 2, 2)])
    _seed_tm_seasons(hist_db, [("GB1", "2024", 400, "Newcomers")])
    pedigree.derive_club_pedigree(hist_db, now="2026-11-11T00:00:00+00:00")
    row = _row(hist_db, "Newcomers", "2425")
    assert row["points_pct_l5"] is None
    assert row["l5_seasons"] == 0
    assert row["top_flight_streak"] == 0  # 已解析：S-1=2023 无 GB1 → 真 0
    assert row["club_tm_id"] == 400


def test_honours_seed_shape_and_spot_values() -> None:
    # 形状：非负整数二元组、fd 联赛码键、名字键非空；种子成员资格=
    # 至少一项重要荣誉（无冠的欧战冠军队 titles 可为 0，如 West Ham）
    for comp, clubs in pedigree.HONOURS.items():
        assert comp
        assert len(comp) <= 4
        for name, (titles, majors) in clubs.items():
            assert name
            assert titles >= 0
            assert majors >= 1
            assert majors >= titles
    # 抽查（2026-11 冻结值，出处见常量注记）
    assert pedigree.HONOURS["E0"]["Manchester United"] == (20, 28)
    assert pedigree.HONOURS["E0"]["Liverpool"] == (20, 34)
    assert pedigree.HONOURS["E0"]["Aston Villa"] == (7, 10)
    assert pedigree.HONOURS["SP1"]["Real Madrid"] == (36, 69)
    assert pedigree.HONOURS["D1"]["Bayern Munich"] == (35, 48)
    assert pedigree.HONOURS["I1"]["Inter Milan"] == (21, 31)
    assert pedigree.HONOURS["F1"]["Paris Saint-Germain"] == (14, 19)
    assert pedigree.HONOURS["SC0"]["Celtic"] == (56, 57)
    assert pedigree.HONOURS["N1"]["PSV Eindhoven"] == (27, 29)
    assert pedigree.HONOURS["T1"]["Galatasaray"] == (26, 28)
    assert pedigree.HONOURS["B1"]["Anderlecht"] == (34, 39)
    assert pedigree.HONOURS["P1"]["Porto"] == (31, 38)


def test_honours_seed_resolves_to_hist_names(hist_db: sqlite3.Connection) -> None:
    # 种子键经 NameIndex 归一解析到 fd 缩写名（Man United/Ath Madrid 形态）
    _seed_hist(
        hist_db,
        [
            _hist("2425", "2025-04-01", "Man United", "Rivals", 1, 1, comp="E0"),
            _hist("2425", "2025-04-02", "Ath Madrid", "Real Madrid", 1, 1, comp="SP1"),
        ],
    )
    pedigree.derive_club_pedigree(hist_db, now="2026-11-11T00:00:00+00:00")
    mu = _row(hist_db, "Man United", "2425")
    assert (mu["tier1_titles"], mu["major_honours"]) == (20, 28)
    am = _row(hist_db, "Ath Madrid", "2425")
    assert (am["tier1_titles"], am["major_honours"]) == (11, 19)
    rm = _row(hist_db, "Real Madrid", "2425")
    assert (rm["tier1_titles"], rm["major_honours"]) == (36, 69)


def test_pedigree_read_seam_facet_scanner_demo(hist_db: sqlite3.Connection) -> None:
    """facet 消费演示：扫描器按 (队, 季) 直读四因子面。"""
    _seed_hist(
        hist_db,
        [
            _hist("2324", "2024-01-13", "Man United", "Rovers", 2, 0),
            _hist("2425", "2025-01-11", "Man United", "Rovers", 0, 2),
            _hist("2526", "2026-01-10", "Man United", "Rovers", 1, 1),
        ],
    )
    pedigree.derive_club_pedigree(hist_db, now="2026-11-11T00:00:00+00:00")
    face = pedigree.pedigree_for(hist_db, "Man United", "2526")
    assert face["team"] == "Man United"
    assert face["season"] == "2526"
    assert face["top_flight_streak"] is None  # 无 TM 面 → 未解析 NULL
    assert face["club_tm_id"] is None
    assert face["l5_seasons"] == 2
    assert face["points_pct_l5"] == pytest.approx(0.5)
    assert (face["tier1_titles"], face["major_honours"]) == (20, 28)
    missing = pedigree.pedigree_for(hist_db, "Nobody", "2526")
    assert missing is None


def test_derive_is_idempotent(hist_db: sqlite3.Connection) -> None:
    _seed_hist(hist_db, [_hist("2425", "2025-04-01", "Strangers", "Filler", 1, 1)])
    pedigree.derive_club_pedigree(hist_db, now="2026-11-11T00:00:00+00:00")
    pedigree.derive_club_pedigree(hist_db, now="2026-11-12T00:00:00+00:00")
    count = hist_db.execute("SELECT COUNT(*) FROM club_pedigree").fetchone()
    assert int(count[0]) == 2  # Strangers + Filler 各一行，重建不翻倍


def test_six_axes_task_wiring_degrades_without_corpus() -> None:
    """tasks 缝：④ 空库照跑（零行不炸），② 无语料桥 degraded 零动作。"""
    from goalx_backend import tasks

    derive = tasks.six_axes_derive()
    assert derive["pedigree"]["rows"] == 0
    assert "degraded" in derive["formations"]
    report = tasks.six_axes_report()
    assert report["formations"]["derived_pending"] is True or (
        report["formations"]["totals"]["rows"] == 0
    )
    assert report["pedigree"] is not None
    assert report["pedigree"]["rows"] == 0
