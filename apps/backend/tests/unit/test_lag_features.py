"""滞后特征面（model-v2 票 01）：滚动/严格 lag/缺数据的行为断言。"""

from __future__ import annotations

from goalx_backend.modelling import lag_features as lf
from goalx_backend.modelling.lag_features import LagRow, build_lag_features


def _row(
    sid: str,
    kickoff: str,
    home: str = "H",
    away: str = "A",
    hg: int = 2,
    ag: int = 1,
    xg_h: float | None = None,
    xg_a: float | None = None,
    shots_h: int | None = None,
    shots_a: int | None = None,
    sot_h: int | None = None,
    sot_a: int | None = None,
    elo_h: float | None = None,
    elo_a: float | None = None,
    league: str = "英超",
) -> LagRow:
    return LagRow(
        sid=sid,
        league=league,
        kickoff=kickoff,
        home=home,
        away=away,
        home_goals=hg,
        away_goals=ag,
        xg_home=xg_h,
        xg_away=xg_a,
        shots_home=shots_h,
        shots_away=shots_a,
        sot_home=sot_h,
        sot_away=sot_a,
        elo_home_pre=elo_h,
        elo_away_pre=elo_a,
    )


def test_strict_lag_same_day_invisible():
    """严格 lag：同日两场互不可见（快照先于入窗）。"""
    rows = [
        _row("m1", "2024-01-01T10:00:00", hg=3, ag=0),
        _row("m2", "2024-01-01T20:00:00"),  # 同日更晚——m1 不可见
    ]
    feats = build_lag_features(rows)
    # m2 的 home 滚动仍只有自身入窗前的历史=空
    assert feats["m2"]["goal_diff_diff"] is None
    # m3（次日）能看到 m1+m2
    rows.append(_row("m3", "2024-01-02T10:00:00"))
    feats = build_lag_features(rows)
    assert feats["m3"]["goal_diff_diff"] is not None


def test_first_match_all_empty_except_elo():
    """首场：滚动全空（None），Elo 差照带（本就赛前值）。"""
    rows = [_row("m1", "2024-01-01", elo_h=1600.0, elo_a=1500.0)]
    feats = build_lag_features(rows)
    assert feats["m1"]["elo_diff"] == 100.0
    assert feats["m1"]["xg_for_diff"] is None
    assert feats["m1"]["points_diff"] is None


def test_rolling_mean_hand_computed():
    """滚动均值手算对齐（胜 3 平 1 积分 + 净胜球 + 滚窗截断）。"""
    rows = [
        _row("m1", "2024-01-01", hg=2, ag=0),  # H: +2 净胜, 3 分
        _row("m2", "2024-01-02", hg=1, ag=1),  # H: 0, 1 分
        _row("m3", "2024-01-03"),  # 目标场
    ]
    feats = build_lag_features(rows)
    # m3 的 home（H 两场）场均净胜 1.0、积分 2.0；away（A 两场）−1.0/0.5
    assert feats["m3"]["goal_diff_diff"] == 1.0 - (-1.0)
    assert feats["m3"]["points_diff"] == 2.0 - 0.5


def test_window_truncation():
    """滚窗截断到 LAG_WINDOW（老场次滑出）。"""
    rows = [_row(f"m{i}", f"2024-01-{i + 1:02d}", hg=10, ag=0) for i in range(12)]
    rows.append(_row("target", "2024-02-01", hg=0, ag=0))
    feats = build_lag_features(rows)
    # home 窗只含最近 10 场（m2..m11），全 10-0 → 净胜均值 10.0
    assert feats["target"]["goal_diff_diff"] == 10.0 - (-10.0)


def test_xg_partial_coverage_no_imputation():
    """xG 缺失场不进滚动窗（不冒充 0）——均值只对有 xG 的场。"""
    rows = [
        _row("m1", "2024-01-01", xg_h=2.0, xg_a=1.0),  # 有 xG
        _row("m2", "2024-01-02"),  # 无 xG——不进窗
        _row("m3", "2024-01-03", xg_h=1.0, xg_a=0.5),  # 有 xG
        _row("m4", "2024-01-04", xg_h=1.0, xg_a=0.5),  # 有 xG
        _row("m5", "2024-01-05"),  # 目标：H 窗=[2.0,1.0,1.0]（≥3 过门）
    ]
    feats = build_lag_features(rows)
    # 缺 xG 的 m2 不进窗——均值=(2.0+1.0+1.0)/3 而非 /4 冒充
    assert feats["m5"]["xg_for_diff"] == (4.0 / 3) - (2.0 / 3)


def test_league_scoped_team_keys():
    """(league, team) 键控：跨联赛同名队不串窗。"""
    rows = [
        _row("m1", "2024-01-01", hg=5, ag=0, league="英超"),
        _row("m2", "2024-01-02", league="西甲"),  # 同名 H/A 但另一联赛
    ]
    feats = build_lag_features(rows)
    assert feats["m2"]["goal_diff_diff"] is None  # 西甲窗空


def test_determinism_same_input_same_output():
    rows = [_row(f"m{i}", f"2024-01-{i + 1:02d}") for i in range(5)]
    assert build_lag_features(rows) == build_lag_features(rows)


def test_projection_from_duck_rows_sorted():
    """取数投影：kickoff 排序在投影内保证（乱序输入→定序输出）。"""
    duck_rows = [
        (
            "b",
            "英超",
            "2024-01-02T20:00:00",
            "H",
            "A",
            1,
            1,
            None,
            None,
            10,
            9,
            3,
            2,
            1500.0,
            1500.0,
        ),
        (
            "a",
            "英超",
            "2024-01-01T20:00:00",
            "H",
            "A",
            2,
            0,
            2.5,
            0.5,
            None,
            None,
            None,
            None,
            1600.0,
            1400.0,
        ),
    ]
    lag_rows = lf.lag_features_from_gold(duck_rows)
    assert [r.sid for r in lag_rows] == ["a", "b"]
    assert lag_rows[0].xg_home == 2.5
    assert lag_rows[1].shots_home == 10


def test_xg_min_samples_gate():
    """xG 滚动样本 <MIN_XG_SAMPLES 留空（票面"场数不足诚实留空"）。"""
    rows = [
        _row("m1", "2024-01-01", xg_h=2.0, xg_a=1.0),
        _row("m2", "2024-01-02"),  # 目标：窗只有 1 个 xG 样本 < 3
    ]
    feats = build_lag_features(rows)
    assert feats["m2"]["xg_for_diff"] is None


def test_xg_pair_not_split():
    """xG 配对观测：一侧缺则整场不进两侧 xG 窗。"""
    rows = [
        _row("m1", "2024-01-01", xg_h=2.0, xg_a=None),  # 只有 home 侧 xG
        _row("m2", "2024-01-02"),
    ]
    feats = build_lag_features(rows)
    # m1 整场没进窗——m2 的 home xg_for 窗为空
    assert feats["m2"]["xg_for_diff"] is None


def test_window_parameter_effective():
    """window 参数真生效（=3 截 10 场老数据）。"""
    rows2 = [
        _row("m1", "2024-01-01", hg=6, ag=0),
        _row("m2", "2024-01-02", hg=6, ag=0),
        _row("m3", "2024-01-03", hg=6, ag=0),
        _row("m4", "2024-01-04", hg=0, ag=0),
        _row("m5", "2024-01-05", hg=0, ag=0),
        _row("m6", "2024-01-06", hg=0, ag=0),
        _row("target", "2024-02-01", hg=0, ag=0),
    ]
    full = build_lag_features(rows2, window=6)  # H 窗含 6-0×3 + 0-0×3 → 3.0
    recent = build_lag_features(rows2, window=3)  # 只 0-0×3 → 0.0
    assert full["target"]["goal_diff_diff"] == 3.0 - (-3.0)
    assert recent["target"]["goal_diff_diff"] == 0.0


def test_unsorted_input_fails_loud():
    """乱序输入 fail-loud（严格 lag 前提断言常开）。"""
    rows = [
        _row("late", "2024-01-02"),
        _row("early", "2024-01-01"),
    ]
    import pytest

    with pytest.raises(ValueError, match="升序"):
        build_lag_features(rows)
