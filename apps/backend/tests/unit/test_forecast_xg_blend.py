"""xG blend 接线测试（票 47）：混合数学精确性 + 生成链路五大/荷甲分支。"""

from __future__ import annotations

import json

import pytest

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs_store
from goalx_backend.modelling import dc_model as dcm
from goalx_backend.modelling import forecast as fc
from goalx_backend.models import MatchCodeInput, Tier


def _artifact(
    teams_params: dict[str, tuple[float, float]], *, rho: float
) -> dcm.DCArtifact:
    return dcm.DCArtifact(
        competition="TEST",
        train_window_start="2025-08-01",
        train_window_end="2026-05-01",
        as_of="2026-05-01",
        n_matches=100,
        half_life_days=365.0,
        teams={
            team: {"attack": attack, "defence": defence}
            for team, (attack, defence) in teams_params.items()
        },
        home_advantage=0.25,
        rho=rho,
        data_fingerprint="f" * 64,
    )


def test_build_payload_blend_is_exact_had_linear_pool() -> None:
    """矩阵算术混合：had 池精确 = 0.5·goal + 0.5·xG（实证口径逐位一致）。"""
    goal_artifact = _artifact({"A": (0.4, -0.2), "B": (-0.1, 0.15)}, rho=-0.08)
    xg_artifact = _artifact({"A": (0.1, -0.05), "B": (0.0, 0.0)}, rho=0.0)
    run = dcm.TrainingRun(base=goal_artifact)
    payload = fc.build_forecast_payload(
        run,
        fd_competition="E0",
        home_model_team="A",
        away_model_team="B",
        xg_side=fc.XGSide(artifact=xg_artifact, home_title="A", away_title="B"),
    )
    goal_matrix = goal_artifact.predict("A", "B")
    xg_matrix = xg_artifact.predict("A", "B")
    # 网格混合（抽样格；浮点求和序差用 approx）
    assert payload["matrix"][0][0] == pytest.approx(
        0.5 * goal_matrix.grid[0][0] + 0.5 * xg_matrix.grid[0][0]
    )
    assert payload["matrix"][2][1] == pytest.approx(
        0.5 * goal_matrix.grid[2][1] + 0.5 * xg_matrix.grid[2][1]
    )
    # had 池 = 线性池（票 45 实证口径）
    blended = fc.forecast_matrix_from_payload(payload).had()
    goal_had = goal_matrix.had()
    xg_had = xg_matrix.had()
    for sel in ("h", "d", "a"):
        assert blended[sel] == pytest.approx(0.5 * goal_had[sel] + 0.5 * xg_had[sel])
    # λ 语义 = 几何均值（对数池）；rho = 加权混合
    import math

    assert payload["lambda_home"] == pytest.approx(
        math.sqrt(goal_matrix.lam_home * xg_matrix.lam_home)
    )
    assert payload["rho"] == 0.5 * (-0.08)
    # 溯源字段
    meta = payload["xg_blend"]
    assert meta is not None
    assert meta["weight_goal_side"] == 0.5
    assert meta["xg_model_fingerprint"] == "f" * 64
    assert meta["ci_method"] == "goal_bootstrap_pooled_with_xg_point"
    assert meta["xg_home"] == "A"
    assert meta["xg_away"] == "B"


def test_build_payload_without_xg_side_unchanged() -> None:
    """无 xg 侧：payload 与票 27 形态一致（xg_blend=None）。"""
    run = dcm.TrainingRun(base=_artifact({"A": (0.3, 0.0), "B": (0.0, 0.0)}, rho=-0.05))
    payload = fc.build_forecast_payload(
        run, fd_competition="N1", home_model_team="A", away_model_team="B"
    )
    assert payload["xg_blend"] is None
    assert payload["rho"] == -0.05


def _seed_understat(db, league: str, teams: list[str], rounds: int = 8) -> None:
    """合成已完场 understat 行（npxG 齐备，title=训练键）。"""
    rows = []
    day = 1
    for _r in range(rounds):
        for i in range(len(teams) // 2):
            home, away = teams[i], teams[len(teams) - 1 - i]
            rows.append(
                {
                    "match_id": f"{league}-{_r}-{i}",
                    "league": league,
                    "season": "2025",
                    "datetime_utc": f"2025-10-{day:02d}T15:00:00",
                    "home_team_id": f"{league}h{i}",
                    "home_team": home,
                    "away_team_id": f"{league}a{i}",
                    "away_team": away,
                    "is_result": 1,
                    "goals_home": 1,
                    "goals_away": 1,
                    "xg_home": 1.2,
                    "xg_away": 1.1,
                    "npxg_home": 1.2,
                    "npxg_away": 1.1,
                    "forecast_w": 0.4,
                    "forecast_d": 0.3,
                    "forecast_l": 0.3,
                    "fixture_id": None,
                    "first_seen_at": "2026-09-20T00:00:00+00:00",
                    "observed_at": "2026-09-20T00:00:00+00:00",
                }
            )
        day = min(day + 2, 27)
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


def _seed_fixture(
    db,
    league: str,
    code: str,
    home_cn: str,
    away_cn: str,
    home_alias: str,
    away_alias: str,
    business_date: str,
) -> int:
    sport_key = {"英超": "soccer_epl", "荷甲": "soccer_eredivisie"}.get(league)
    competition = fx_store.upsert_competition(
        db, league, tier=Tier.TIER1, odds_api_sport_key=sport_key
    )
    home = fx_store.upsert_team(db, home_cn)
    away = fx_store.upsert_team(db, away_cn)
    fixture = fx_store.upsert_fixture(
        db, competition, f"{business_date}T19:00:00+00:00", home, away
    )
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date=business_date,
            code=code,
            source_match_id=code,
            is_single=True,
        ),
    )
    for team_id, alias in ((home, home_alias), (away, away_alias)):
        db.execute(
            "INSERT OR IGNORE INTO team_aliases (team_id, source, alias)"
            " VALUES (?, 'odds_api', ?)",
            (team_id, alias),
        )
    db.commit()
    return fixture


def _seed_fd_history(db, competition: str, teams: list[str]) -> None:
    import numpy as np

    rng = np.random.default_rng(11)
    rows = []
    day = 1
    for _r in range(12):
        for i in range(len(teams) // 2):
            rows.append(
                {
                    "competition": competition,
                    "season": "2526",
                    "match_date": f"2025-09-{day:02d}",
                    "home_team": teams[i],
                    "away_team": teams[len(teams) - 1 - i],
                    "fthg": int(rng.poisson(1.4)),
                    "ftag": int(rng.poisson(1.1)),
                    "ftr": "H",
                    "psc_home": 2.0,
                    "psc_draw": 3.4,
                    "psc_away": 3.8,
                    "avgc_home": 2.0,
                    "avgc_draw": 3.4,
                    "avgc_away": 3.8,
                }
            )
        day = min(day + 3, 27)
    rs_store.upsert_hist_matches(db, rows)


FD_TEAMS = [
    "Man United",
    "Liverpool",
    "Arsenal",
    "Chelsea",
    "Everton",
    "Fulham",
    "Burnley",
    "Brentford",
]
NL_TEAMS = [
    "Ajax",
    "PSV Eindhoven",
    "Feyenoord",
    "AZ Alkmaar",
    "Twente",
    "Utrecht",
    "Sparta",
    "Heerenveen",
]


def test_generate_forecasts_blends_big5_falls_back_tier2(db, tmp_path) -> None:
    """英超（understat 覆盖）blend 落库；荷甲（无覆盖）纯 goal-DC 兜底。"""
    _seed_fd_history(db, "E0", FD_TEAMS)
    _seed_fd_history(db, "N1", NL_TEAMS)
    dcm.train_competition(db, "E0", models_dir=tmp_path, n_boot=6, seed=3)
    dcm.train_competition(db, "N1", models_dir=tmp_path, n_boot=6, seed=3)
    _seed_understat(db, "epl", ["Manchester United", "Liverpool", "Arsenal", "Chelsea"])
    fid_epl = _seed_fixture(
        db,
        "英超",
        "周一001",
        "曼联",
        "利物浦",
        "Manchester United",
        "Liverpool",
        "2026-09-21",
    )
    fid_n1 = _seed_fixture(
        db, "荷甲", "周一002", "阿贾克斯", "PSV", "Ajax", "PSV Eindhoven", "2026-09-21"
    )
    stats = fc.generate_forecasts(
        db, business_date="2026-09-21", models_dir=str(tmp_path)
    )
    assert stats.generated == 2
    assert stats.xg_blended == 1

    epl_row = db.execute(
        "SELECT * FROM forecasts WHERE fixture_id = ?", (fid_epl,)
    ).fetchone()
    payload = json.loads(epl_row["payload"])
    assert epl_row["model_version"].startswith("dc-xgblend-")
    assert payload["xg_blend"] is not None
    assert payload["xg_blend"]["xg_home"] == "Manchester United"
    # had 池：goal 与 xG 侧都在（可复算）
    matrix = fc.forecast_matrix_from_payload(payload)
    assert sum(matrix.had().values()) > 0.99

    n1_row = db.execute(
        "SELECT * FROM forecasts WHERE fixture_id = ?", (fid_n1,)
    ).fetchone()
    n1_payload = json.loads(n1_row["payload"])
    assert n1_row["model_version"].startswith("dc-")
    assert not n1_row["model_version"].startswith("dc-xgblend-")
    assert n1_payload["xg_blend"] is None


def test_generate_forecasts_xg_title_unresolved_falls_back(db, tmp_path) -> None:
    """understat 有数据但 fixture 别名解析不到 → 纯 goal-DC + xg_unresolved。"""
    _seed_fd_history(db, "E0", FD_TEAMS)
    dcm.train_competition(db, "E0", models_dir=tmp_path, n_boot=4, seed=3)
    # understat 语料是低级别队（title 集不含 fixture 双方）→ 解析不到
    _seed_understat(db, "epl", ["Norwich City", "Watford", "Luton Town", "Millwall"])
    fid = _seed_fixture(
        db, "英超", "周一003", "切尔西", "埃弗顿", "Chelsea", "Everton", "2026-09-21"
    )
    stats = fc.generate_forecasts(
        db, business_date="2026-09-21", models_dir=str(tmp_path)
    )
    assert stats.generated == 1
    assert stats.xg_blended == 0
    assert stats.xg_unresolved == 1
    row = db.execute("SELECT * FROM forecasts WHERE fixture_id = ?", (fid,)).fetchone()
    payload = json.loads(row["payload"])
    assert payload["xg_blend"] is None
    assert row["model_version"].startswith("dc-")
