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
    # ---- 荣誉计数（F4：titles=as-of，全史 20 − 窗内 S'≥S 冠军数；
    # 本场景窗内 MU 冠 2324 与 2526：2425 行扣 2526 冠军=19，2526 行扣
    # 自季冠军=19；major_honours=静态先验（窗内前视增量，消费方签字可用）
    # → 两季同值 28 ----
    assert (mu_2425["tier1_titles"], mu_2425["major_honours"]) == (19, 28)
    assert (mu_2526["tier1_titles"], mu_2526["major_honours"]) == (19, 28)
    # 种子外球队（Rovers/Wanderers/Veteran）= NULL/NULL（F2：未命中不写假 0）
    assert (rovers["tier1_titles"], rovers["major_honours"]) == (None, None)
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
    # 种子未命中（F2）：荣誉两列 NULL，不写假 0
    assert (row["tier1_titles"], row["major_honours"]) == (None, None)


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
    assert pedigree.HONOURS["E0"]["Nottingham Forest"] == (1, 4)  # F5：洲际 0
    assert pedigree.HONOURS["E0"]["Aston Villa"] == (7, 10)
    assert pedigree.HONOURS["SP1"]["Real Madrid"] == (36, 67)  # F5：世俱 5 非 7
    assert pedigree.HONOURS["D1"]["Bayern Munich"] == (35, 49)  # F5：补 1967 CWC
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
    # titles as-of（F4）：本场景唯一 SP1 场两队同分同净胜同进球——决胜
    # 字典序（max）判 Real Madrid 为 2425 合成冠军 → 36−1=35
    rm = _row(hist_db, "Real Madrid", "2425")
    assert (rm["tier1_titles"], rm["major_honours"]) == (35, 67)


def test_pedigree_read_seam_facet_scanner_demo(hist_db: sqlite3.Connection) -> None:
    """facet 消费演示：扫描器按 (队, 季) 直读四因子面。"""
    _seed_hist(
        hist_db,
        [
            _hist("2324", "2024-01-13", "Man United", "Rovers", 2, 0),
            _hist("2425", "2025-01-11", "Man United", "Rovers", 0, 2),
            _hist("2526", "2026-01-10", "Man United", "Rovers", 2, 1),
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
    # titles as-of（F4）：2526 自季 MU 2-1 夺冠（季末才定，扣）→ 20−1=19；
    # majors 静态先验 28
    assert (face["tier1_titles"], face["major_honours"]) == (19, 28)
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


def test_top_flight_is_per_country_not_big_five(hist_db: sqlite3.Connection) -> None:
    """F1：顶级口径=逐国顶级——Ajax 在 NL1 连续 11 季照常计年限（旧实现恒 0）。"""
    _seed_hist(
        hist_db,
        [
            _hist("2727", "2027-08-14", "Ajax", "Twente", 1, 0, comp="N1"),
            _hist("2627", "2026-09-12", "Celtic", "Rangers", 1, 0, comp="SC0"),
        ],
    )
    _seed_tm_seasons(
        hist_db,
        [("NL1", str(year), 500, "Ajax") for year in range(2016, 2027)]
        + [("SC1", str(year), 501, "Celtic") for year in range(2024, 2027)],
    )
    pedigree.derive_club_pedigree(hist_db, now="2026-11-11T00:00:00+00:00")
    ajax = _row(hist_db, "Ajax", "2727")
    assert ajax["club_tm_id"] == 500
    assert ajax["top_flight_streak"] == 11  # 2016-2026 连续荷甲顶级
    assert (ajax["promo_to_tier1"], ajax["releg_from_tier1"]) == (0, 0)
    celtic = _row(hist_db, "Celtic", "2627")
    assert celtic["top_flight_streak"] == 2  # 苏超同口径（SC1 亦顶级）


def test_honours_fd_name_forms_resolve_not_null(hist_db: sqlite3.Connection) -> None:
    """F2：fd 真名形态（Wolves/Sp Lisbon/St Gilloise/Buyuksehyr）命中种子。"""
    _seed_hist(
        hist_db,
        [
            _hist("2425", "2025-04-01", "Wolves", "Filler", 1, 1, comp="E0"),
            _hist("2425", "2025-04-02", "Sp Lisbon", "Filler", 0, 2, comp="P1"),
            _hist("2425", "2025-04-03", "St Gilloise", "Filler", 0, 2, comp="B1"),
            _hist("2425", "2025-04-04", "Buyuksehyr", "Filler", 0, 2, comp="T1"),
            _hist("2425", "2025-04-05", "No Honour FC", "Filler", 0, 2, comp="E0"),
        ],
    )
    pedigree.derive_club_pedigree(hist_db, now="2026-11-11T00:00:00+00:00")
    assert (_row(hist_db, "Wolves", "2425")["tier1_titles"]) == 3
    assert (_row(hist_db, "Sp Lisbon", "2425")["tier1_titles"]) == 21
    assert (_row(hist_db, "St Gilloise", "2425")["tier1_titles"]) == 12
    assert (_row(hist_db, "Buyuksehyr", "2425")["tier1_titles"]) == 1
    # 种子未命中（含真无冠队与形态缺口）：NULL 而非 0，绝不写假 0
    unseeded = _row(hist_db, "No Honour FC", "2425")
    assert (unseeded["tier1_titles"], unseeded["major_honours"]) == (None, None)
    # 各联赛均只一场合成赛 → 无冠军扣减，titles=静态全史数


def test_honours_match_globally_across_comps(hist_db: sqlite3.Connection) -> None:
    """F3：荣誉按俱乐部全局匹配——Blackburn 只在 E1 出现也拿 3 冠。"""
    _seed_hist(
        hist_db,
        [
            _hist("2425", "2025-04-01", "Blackburn", "Filler", 1, 1, comp="E1"),
            _hist("2425", "2025-04-02", "Sheffield United", "Filler", 1, 1, comp="E1"),
        ],
    )
    pedigree.derive_club_pedigree(hist_db, now="2026-11-11T00:00:00+00:00")
    blackburn = _row(hist_db, "Blackburn", "2425")
    assert (blackburn["tier1_titles"], blackburn["major_honours"]) == (3, 3)
    sheffield = _row(hist_db, "Sheffield United", "2425")
    assert (sheffield["tier1_titles"], sheffield["major_honours"]) == (1, 1)


def test_titles_as_of_season_subtracts_window_champions(
    hist_db: sqlite3.Connection,
) -> None:
    """F4：titles=全史总数 − 窗内 S'≥S 冠军数（City/Ajax 两例手算钉死）。

    场景：City 静态 (10, 14)，窗内 1819/2425 夺冠、1617/2627 输——
    1617 行=10−2=8、1819 行=10−2=8（自季冠军季末才定也扣）、2425 行=
    10−1=9（kickoff 时点 9 冠，与史实一致）、2627 行=10−0=10；
    majors 静态先验全行 14（窗内前视增量，消费方签字可用）。
    Ajax 静态 (36, 46)，窗内 2122 夺冠、1617/2526 不夺冠——1617=35、2526=36。
    """
    _seed_hist(
        hist_db,
        [
            _hist("1617", "2017-04-01", "Man City", "Others", 0, 2, comp="E0"),
            _hist("1819", "2019-04-01", "Man City", "Others", 2, 0, comp="E0"),
            _hist("1819", "2019-04-02", "Others", "Man City", 0, 3, comp="E0"),
            _hist("2425", "2025-04-01", "Man City", "Others", 2, 0, comp="E0"),
            _hist("2425", "2025-04-02", "Others", "Man City", 0, 3, comp="E0"),
            _hist("2627", "2026-09-01", "Man City", "Others", 0, 2, comp="E0"),
            _hist("1617", "2017-04-03", "Ajax", "Rival", 0, 2, comp="N1"),
            _hist("2122", "2022-04-03", "Ajax", "Rival", 2, 0, comp="N1"),
            _hist("2122", "2022-04-04", "Rival", "Ajax", 0, 3, comp="N1"),
            _hist("2526", "2026-04-03", "Ajax", "Rival", 0, 2, comp="N1"),
        ],
    )
    pedigree.derive_club_pedigree(hist_db, now="2026-11-11T00:00:00+00:00")
    seasons = ("1617", "1819", "2425", "2627")
    city = {season: _row(hist_db, "Man City", season) for season in seasons}
    assert city["1617"]["tier1_titles"] == 8  # 10 −（1819+2425 两冠在后）
    assert city["1819"]["tier1_titles"] == 8  # 自季冠军季末才定，照扣
    assert city["2425"]["tier1_titles"] == 9  # kickoff 时点 9 冠（史实）
    assert city["2627"]["tier1_titles"] == 10  # 窗内再无后置冠军
    assert all(row["major_honours"] == 14 for row in city.values())  # 静态先验
    ajax = {season: _row(hist_db, "Ajax", season) for season in ("1617", "2526")}
    assert ajax["1617"]["tier1_titles"] == 35  # 36 − 2122 一冠在后
    assert ajax["2526"]["tier1_titles"] == 36  # 无后置冠军 → 全史数
    # PIT 不变量：titles 随赛季单调不减（越晚的行扣得越少、窗前部分
    # 对一切窗内行都是过去）——四个赛季值手算钉死如上
    values = [city[s]["tier1_titles"] for s in ("1617", "1819", "2425", "2627")]
    assert values == sorted(values)


def test_honours_collision_drops_to_null_and_reports(
    hist_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6：同名不同值互撞 → 整名 NULL + conflicts 可观测（不保先到值）。"""
    _seed_hist(hist_db, [_hist("2425", "2025-04-01", "Man United", "Filler", 1, 1)])
    monkeypatch.setattr(
        pedigree,
        "HONOURS",
        {
            "E0": {
                "Manchester United": (20, 28),
                "Man United": (99, 99),  # 归一后同名、不同值 → 互撞
            }
        },
    )
    stats = pedigree.derive_club_pedigree(hist_db, now="2026-11-11T00:00:00+00:00")
    row = _row(hist_db, "Man United", "2425")
    assert (row["tier1_titles"], row["major_honours"]) == (None, None)
    assert stats["honours_conflicts"] == ["Man United"]
