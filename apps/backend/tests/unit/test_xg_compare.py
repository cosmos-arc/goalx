"""xG 实证对比 runner 测试（票 45）：合成语料走通 walk-forward 全链路。"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from numpy import random as np_random

from goalx_backend.evaluation.xg_compare import (
    _bucket,
    _outcome_index,
    run_xg_comparison,
)

_TEAMS = ("t1", "t2", "t3", "t4")
_ATTACK = {"t1": 0.4, "t2": 0.1, "t3": -0.2, "t4": -0.3}
_DEFENCE = {"t1": -0.3, "t2": -0.1, "t3": 0.1, "t4": 0.3}
_GAMMA = 0.3
_MID = 2200  # understat 球队 id 区间内取值（字符串键，仅约定俗成）


def _synthetic_season(rng, season: str, weeks: int, start: date) -> list[dict]:
    """4 队双循环伪赛季：已知强度 Poisson 比分 + npxG + forecast。"""
    rows: list[dict] = []
    day = start
    # team id 跨季保持（俱乐部 id 稳定）
    ids = {team: str(_MID + i) for i, team in enumerate(_TEAMS)}
    for week in range(weeks):
        pairs = (
            (_TEAMS[week % 4], _TEAMS[(week + 1) % 4]),
            (_TEAMS[(week + 2) % 4], _TEAMS[(week + 3) % 4]),
        )
        for home, away in pairs:
            lam_h = math.exp(_GAMMA + _ATTACK[home] + _DEFENCE[away])
            lam_a = math.exp(_ATTACK[away] + _DEFENCE[home])
            gh, ga = int(rng.poisson(lam_h)), int(rng.poisson(lam_a))
            ph = lam_h / (lam_h + lam_a + 0.9)
            pa = lam_a / (lam_h + lam_a + 0.9)
            rows.append(
                {
                    "match_id": f"{season}-{week}-{home}{away}",
                    "league": "epl",
                    "season": season,
                    "datetime_utc": f"{day.isoformat()}T15:00:00",
                    "home_team_id": ids[home],
                    "home_team": home,
                    "away_team_id": ids[away],
                    "away_team": away,
                    "is_result": 1,
                    "goals_home": gh,
                    "goals_away": ga,
                    "xg_home": lam_h,
                    "xg_away": lam_a,
                    "npxg_home": lam_h,
                    "npxg_away": lam_a,
                    "forecast_w": round(ph, 4),
                    "forecast_d": round(max(1.0 - ph - pa, 0.02), 4),
                    "forecast_l": round(pa, 4),
                    "fixture_id": None,
                    "first_seen_at": "2026-09-20T00:00:00+00:00",
                    "observed_at": "2026-09-20T00:00:00+00:00",
                }
            )
        day += timedelta(days=7)
    return rows


def _insert(db, rows: list[dict]) -> None:
    for r in rows:
        db.execute(
            """
            INSERT INTO understat_matches
                (match_id, league, season, datetime_utc, home_team_id, home_team,
                 away_team_id, away_team, is_result, goals_home, goals_away,
                 xg_home, xg_away, npxg_home, npxg_away,
                 forecast_w, forecast_d, forecast_l, fixture_id,
                 first_seen_at, observed_at)
            VALUES (:match_id, :league, :season, :datetime_utc, :home_team_id,
                    :home_team, :away_team_id, :away_team, :is_result, :goals_home,
                    :goals_away, :xg_home, :xg_away, :npxg_home, :npxg_away,
                    :forecast_w, :forecast_d, :forecast_l, :fixture_id,
                    :first_seen_at, :observed_at)
            """,
            r,
        )
    db.commit()


def test_bucket_and_outcome_index() -> None:
    assert _bucket(1) == "wk1_4"
    assert _bucket(4) == "wk1_4"
    assert _bucket(5) == "wk5_8"
    assert _bucket(8) == "wk5_8"
    assert _bucket(9) == "wk9p"
    assert _bucket(38) == "wk9p"
    assert _outcome_index(2, 0) == 0
    assert _outcome_index(1, 1) == 1
    assert _outcome_index(0, 3) == 2


def test_run_xg_comparison_end_to_end(db) -> None:
    rng = np_random.default_rng(7)
    rows = [
        *_synthetic_season(rng, "2023", 10, date(2023, 8, 12)),  # 训练季
        *_synthetic_season(rng, "2024", 10, date(2024, 8, 10)),  # 训练季
        *_synthetic_season(rng, "2025", 8, date(2025, 8, 9)),  # 目标季
    ]
    _insert(db, rows)
    report = run_xg_comparison(
        db,
        leagues=("epl",),
        seasons=("2025",),
        train_seasons=2,
        shrink_k=(6.0,),
    )
    overall = report["overall"]
    assert {"goal_dc", "xg_dc", "shrink_k6", "understat_forecast"} <= set(overall)
    for variant in ("goal_dc", "xg_dc", "shrink_k6", "understat_forecast"):
        assert overall[variant]["n"] == 16  # 8 周 × 2 场
        assert 0.0 < overall[variant]["rps"] < 0.8
    # 配对差对基准以外变体存在（负=优）；xg 与 shrink 同为 16 场配对
    assert overall["xg_dc"]["paired_n"] == 16
    assert "rps_delta_vs_goal_dc" in overall["shrink_k6"]
    assert "rps_delta_vs_goal_dc" in overall["understat_forecast"]
    # 周桶分列（8 周 → wk1_4 与 wk5_8 两组有值）
    assert set(report["by_bucket"]) == {"wk1_4", "wk5_8"}
    assert report["by_bucket"]["wk1_4"]["goal_dc"]["n"] == 8
    # 目标季所有场次都有 forecast（合成语料全完场）
    assert overall["understat_forecast"]["n"] == 16
    assert report["params"]["seasons"] == ["2025"]


def test_run_xg_comparison_decay_sensitivity_section(db) -> None:
    """票 33 半衰期敏感性栏：报告附 goal-DC 衰减梯（2/2.5/3 季 + 无衰减
    基准），配对差对无衰减列 delta；衰减臂不漏进主对比表。"""
    rng = np_random.default_rng(9)
    rows = [
        *_synthetic_season(rng, "2023", 10, date(2023, 8, 12)),  # 训练季
        *_synthetic_season(rng, "2024", 10, date(2024, 8, 10)),  # 训练季
        *_synthetic_season(rng, "2025", 8, date(2025, 8, 9)),  # 目标季
    ]
    _insert(db, rows)
    report = run_xg_comparison(
        db, leagues=("epl",), seasons=("2025",), train_seasons=2, shrink_k=(6.0,)
    )
    section = report["decay_sensitivity"]
    assert section["baseline"] == "goal_dc_nodecay"
    assert section["variants"] == {
        "goal_dc": 365.0,
        "goal_dc_hl2s": 730.0,
        "goal_dc_hl2.5s": 912.5,
        "goal_dc_hl3s": 1095.0,
        "goal_dc_nodecay": None,
    }
    overall = section["overall"]
    for arm in section["variants"]:
        assert overall[arm]["n"] == 16  # 与主基准同覆盖（8 周 × 2 场）
    for arm in ("goal_dc", "goal_dc_hl2s", "goal_dc_hl2.5s", "goal_dc_hl3s"):
        # 配对差对无衰减基准（负 = 优于无衰减）；RPS 与 log loss 双指标
        assert overall[arm]["paired_n"] == 16
        assert "rps_delta_vs_goal_dc_nodecay" in overall[arm]
        assert "log_loss_delta_vs_goal_dc_nodecay" in overall[arm]
    assert "rps_delta_vs_goal_dc_nodecay" not in overall["goal_dc_nodecay"]
    # 臂级缺勤显式化（independent 复核 F2）：栏内自带 skipped 信号位，
    # 健康语料为空映射
    assert section["skipped"] == {}
    # 衰减臂只进敏感性栏，不污染主对比 summary（那里讲 xG 融合，不讲衰减）
    assert "goal_dc_hl2s" not in report["overall"]
    assert all("goal_dc_nodecay" not in b for b in report["by_bucket"].values())
    assert report["params"]["decay_half_life_seasons"] == [2.0, 2.5, 3.0]


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 11])
def test_decay_arms_recover_known_regime_shift(seed: int) -> None:
    """已知半衰期恢复（行为级）：t1/t2 攻强对换的合成语料（旧季 t1 弱 t2 强、
    新三季反向），2 季半衰期拟合的 t1−t2 强度对比显著高于无衰减，且更接近
    新 regime 真值 ±0.5——近因权重吃回已知换血，报告敏感性栏的统计前提
    成立。对换设计保持两 regime 总进球不变（ρ/水平不吸收方向信号）。
    种子族钉方向稳定性（independent 复核 F3：1/2/3/11 + 原 7）。"""
    from goalx_backend.modelling.dc_model import TrainingRow, fit_dc_model

    teams = ("t1", "t2", "t3", "t4")
    defence = {"t1": -0.1, "t2": 0.0, "t3": 0.1, "t4": 0.0}
    old_regime = {"t1": -0.5, "t2": 0.5, "t3": 0.0, "t4": 0.0}
    new_regime = {"t1": 0.5, "t2": -0.5, "t3": 0.0, "t4": 0.0}
    schedule: list[tuple[str, str, str, float, float]] = []
    for year, n_weeks, attack in (
        (2022, 15, old_regime),
        (2023, 12, new_regime),
        (2024, 12, new_regime),
        (2025, 10, new_regime),
    ):
        day = date(year, 8, 13)  # 每季重置到 8 月（旧 regime 距 as_of 2+ 年）
        for week in range(n_weeks):
            for home, away in (
                (teams[week % 4], teams[(week + 1) % 4]),
                (teams[(week + 2) % 4], teams[(week + 3) % 4]),
            ):
                schedule.append(
                    (
                        day.isoformat(),
                        home,
                        away,
                        math.exp(0.3 + attack[home] + defence[away]),
                        math.exp(attack[away] + defence[home]),
                    )
                )
            day += timedelta(days=7)
    rng = np_random.default_rng(seed)
    rows = [
        TrainingRow(d, h, a, int(rng.poisson(lh)), int(rng.poisson(la)))
        for d, h, a, lh, la in schedule
    ]
    decayed = fit_dc_model(rows, competition="syn", half_life_days=730.0)
    no_decay = fit_dc_model(rows, competition="syn", half_life_days=math.inf)

    def contrast(artifact: object) -> float:
        return artifact.teams["t1"]["attack"] - artifact.teams["t2"]["attack"]  # type: ignore[attr-defined]

    c_decayed, c_no_decay = contrast(decayed), contrast(no_decay)
    # 新 regime 真值对比 = 0.5−(−0.5) = 1.0；衰减拟合显著更靠近（近因权重）
    assert c_decayed - c_no_decay > 0.05
    assert abs(c_decayed - 1.0) < abs(c_no_decay - 1.0)
    assert decayed.half_life_days == 730.0
    assert no_decay.half_life_days == math.inf


def test_run_xg_comparison_skips_missing_league(db) -> None:
    report = run_xg_comparison(db, leagues=("epl",), seasons=("2025",))
    assert report["skipped"] == {"no_data_epl": 1}
    assert report["overall"] == {}


def test_walk_forward_trains_on_current_season_increment(db) -> None:
    """回归（2026-09-20 真 bug）：本季已完场必须进后续周训练集——

    目标季新队（升班马）首周 no_team 跳过、第二周起可预测；训练池若把
    目标季整体排除（旧行为），该队永远 no_team 且收缩拿空累计。
    """
    rng = np_random.default_rng(11)
    rows = [*_synthetic_season(rng, "2024", 8, date(2024, 8, 10))]
    # 目标季前两周：t5/t6 仅出现在目标季（首战 08-09，次战 08-16）
    promoted = [
        {
            "match_id": "2025-promoted-1",
            "league": "epl",
            "season": "2025",
            "datetime_utc": "2025-08-09T15:00:00",
            "home_team_id": "2299",
            "home_team": "t5",
            "away_team_id": str(_MID),
            "away_team": "t1",
            "is_result": 1,
            "goals_home": 1,
            "goals_away": 1,
            "xg_home": 1.0,
            "xg_away": 1.1,
            "npxg_home": 1.0,
            "npxg_away": 1.1,
            "forecast_w": 0.35,
            "forecast_d": 0.3,
            "forecast_l": 0.35,
            "fixture_id": None,
            "first_seen_at": "2026-09-20T00:00:00+00:00",
            "observed_at": "2026-09-20T00:00:00+00:00",
        },
        {
            "match_id": "2025-promoted-2",
            "league": "epl",
            "season": "2025",
            "datetime_utc": "2025-08-16T15:00:00",
            "home_team_id": str(_MID),
            "home_team": "t1",
            "away_team_id": "2299",
            "away_team": "t5",
            "is_result": 1,
            "goals_home": 0,
            "goals_away": 2,
            "xg_home": 0.7,
            "xg_away": 1.8,
            "npxg_home": 0.7,
            "npxg_away": 1.8,
            "forecast_w": 0.2,
            "forecast_d": 0.3,
            "forecast_l": 0.5,
            "fixture_id": None,
            "first_seen_at": "2026-09-20T00:00:00+00:00",
            "observed_at": "2026-09-20T00:00:00+00:00",
        },
    ]
    _insert(db, rows + promoted)
    report = run_xg_comparison(
        db, leagues=("epl",), seasons=("2025",), train_seasons=1, shrink_k=(6.0,)
    )
    # 首周 t5 无训练数据 → no_team；次周 t5 已有一场 → 可预测
    assert report["skipped"].get("no_team_goal_dc") == 1
    assert report["overall"]["goal_dc"]["n"] == 1  # 2025-promoted-2 评上
    # 收缩在本季有累计可依（t5 首战入账）→ 与基准产生差异
    assert report["overall"]["shrink_k6"]["rps"] != report["overall"]["goal_dc"]["rps"]
