"""Forecast 生成测试（票 27 验收：哈希可复验、CI 合理、跳过清单可查）。"""

from __future__ import annotations

import numpy as np

from goalx_backend import dc_model as dcm
from goalx_backend import forecast as fc
from goalx_backend.models import MatchCodeInput, Tier
from goalx_backend.store import fixtures as fx_store
from goalx_backend.store import results as rs_store


def seed_league_history(db, teams: list[str], rounds: int = 30, seed: int = 9):
    """合成 fd 历史并导入 hist_matches。"""
    rng = np.random.default_rng(seed)
    rows = []
    day = 1
    for _round_no in range(rounds):
        for i in range(len(teams) // 2):
            home = teams[i]
            away = teams[len(teams) - 1 - i]
            date_str = f"2025-09-{day:02d}"
            rows.append(
                {
                    "competition": "E0",
                    "season": "2526",
                    "match_date": date_str,
                    "home_team": home,
                    "away_team": away,
                    "fthg": int(rng.poisson(1.5)),
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
        day = min(day + 3, 28)
    rs_store.upsert_hist_matches(db, rows)
    return rows


def seed_jingcai_fixture(
    db,
    *,
    league: str = "英超",
    home_cn: str = "曼联",
    away_cn: str = "利物浦",
    aliases: tuple[tuple[str, str], ...] = (
        ("曼联", "Manchester United"),
        ("利物浦", "Liverpool"),
    ),
    kickoff: str = "2026-09-19T19:00:00+00:00",
    code: str = "周六001",
) -> int:
    sport_key = {"英超": "soccer_epl"}.get(league)
    competition = fx_store.upsert_competition(
        db, league, tier=Tier.TIER1, odds_api_sport_key=sport_key
    )
    home = fx_store.upsert_team(db, home_cn)
    away = fx_store.upsert_team(db, away_cn)
    fixture = fx_store.upsert_fixture(db, competition, kickoff, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date="2026-09-19",
            code=code,
            source_match_id="m1",
            is_single=True,
        ),
    )
    for canonical, alias in aliases:
        team_id = fx_store.upsert_team(db, canonical)
        db.execute(
            "INSERT OR IGNORE INTO team_aliases (team_id, source, alias)"
            " VALUES (?, 'odds_api', ?)",
            (team_id, alias),
        )
    db.commit()
    return fixture


def test_content_hash_stable_and_sensitive() -> None:
    payload = {"a": 1, "b": [1.5, 2.5]}
    same = {"b": [1.5, 2.5], "a": 1}
    assert fc.content_hash(payload) == fc.content_hash(same)  # 键序无关
    assert len(fc.content_hash(payload)) == 64
    changed = {"a": 1, "b": [1.5, 2.6]}
    assert fc.content_hash(payload) != fc.content_hash(changed)


def test_generate_forecasts_end_to_end(db, tmp_path) -> None:
    teams = [
        "Man United",
        "Liverpool",
        "Arsenal",
        "Chelsea",
        "Everton",
        "Fulham",
        "Burnley",
        "Brentford",
    ]
    seed_league_history(db, teams)
    dcm.train_competition(db, "E0", models_dir=tmp_path, n_boot=8, seed=3)
    fixture_id = seed_jingcai_fixture(db)

    stats = fc.generate_forecasts(
        db, business_date="2026-09-19", models_dir=str(tmp_path)
    )
    assert stats.generated == 1
    assert stats.skipped == []

    row = db.execute(
        "SELECT * FROM forecasts WHERE fixture_id = ?", (fixture_id,)
    ).fetchone()
    assert row is not None
    assert row["track"] == "ml"
    assert row["model_version"].startswith("dc-")

    import json

    payload = json.loads(row["payload"])
    assert payload["home_team"] == "Man United"
    assert payload["away_team"] == "Liverpool"
    matrix = fc.forecast_matrix_from_payload(payload)
    had = matrix.had()
    assert sum(had.values()) > 0.99
    # 哈希可复验：同 payload 重算哈希一致
    assert fc.content_hash(payload) == row["content_hash"]
    # bootstrap CI：单调（lo ≤ 点估计 ≤ hi，宽容 bootstrap 噪声）
    ci = payload["had_ci"]
    assert ci is not None
    for sel in ("h", "d", "a"):
        assert ci[sel][0] <= had[sel] + 0.02
        assert ci[sel][1] >= had[sel] - 0.02
        assert ci[sel][0] <= ci[sel][1]
    # bootstrap 样本数 ≥ 6（重采样偶发坏样本被跳过是设计行为，票 27）
    assert len(payload["bootstrap_lambda"]) >= 6


def test_generate_forecasts_idempotent_rerun(db, tmp_path) -> None:
    teams = [
        "Man United",
        "Liverpool",
        "Arsenal",
        "Chelsea",
        "Everton",
        "Fulham",
        "Burnley",
        "Brentford",
    ]
    seed_league_history(db, teams)
    dcm.train_competition(db, "E0", models_dir=tmp_path)
    seed_jingcai_fixture(db)
    first = fc.generate_forecasts(
        db, business_date="2026-09-19", models_dir=str(tmp_path)
    )
    second = fc.generate_forecasts(
        db, business_date="2026-09-19", models_dir=str(tmp_path)
    )
    assert first.generated == 1
    assert second.generated == 0
    assert second.duplicates == 1
    total = db.execute("SELECT COUNT(*) AS c FROM forecasts").fetchone()["c"]
    assert total == 1


def test_generate_forecasts_skip_reasons(db, tmp_path) -> None:
    # 无 fd 映射的联赛（欧冠）→ not_fd_league；英超无模型 → no_model
    seed_jingcai_fixture(db, league="欧冠")
    seed_jingcai_fixture(
        db,
        league="英超",
        home_cn="曼城",
        away_cn="热刺",
        aliases=(("曼城", "Manchester City"), ("热刺", "Tottenham Hotspur")),
        kickoff="2026-09-19T21:00:00+00:00",
        code="周六002",
    )
    before_train = fc.generate_forecasts(
        db, business_date="2026-09-19", models_dir=str(tmp_path)
    )
    assert {item["reason"] for item in before_train.skipped} == {
        "not_fd_league",
        "no_model",
    }

    # 训练后：队名对不上模型池 → no_mapping
    teams = ["Aa", "Bb", "Cc", "Dd", "Ee", "Ff", "Gg", "Hh"]
    seed_league_history(db, teams, rounds=14, seed=4)
    dcm.train_competition(db, "E0", models_dir=tmp_path)
    seed_jingcai_fixture(
        db,
        league="英超",
        home_cn="阿斯顿维拉",
        away_cn="纽卡斯尔",
        aliases=(
            ("阿斯顿维拉", "Aston Villa"),
            ("纽卡斯尔", "Newcastle United"),
        ),
        kickoff="2026-09-19T22:00:00+00:00",
        code="周六003",
    )
    stats = fc.generate_forecasts(
        db, business_date="2026-09-19", models_dir=str(tmp_path)
    )
    by_reason = {}
    for item in stats.skipped:
        by_reason.setdefault(item["reason"], []).append(item)
    assert set(by_reason) == {"not_fd_league", "no_mapping"}
    assert stats.generated == 0
    no_mapping_fixtures = {item["fixture"] for item in by_reason["no_mapping"]}
    assert no_mapping_fixtures == {"曼城 vs 热刺", "阿斯顿维拉 vs 纽卡斯尔"}


def test_forecast_matrix_views_from_payload(db, tmp_path) -> None:
    # 视图态不落库：从 payload 即时推导各玩法视图
    teams = [
        "Man United",
        "Liverpool",
        "Arsenal",
        "Chelsea",
        "Everton",
        "Fulham",
        "Burnley",
        "Brentford",
    ]
    seed_league_history(db, teams)
    dcm.train_competition(db, "E0", models_dir=tmp_path)
    fixture_id = seed_jingcai_fixture(db)
    fc.generate_forecasts(db, business_date="2026-09-19", models_dir=str(tmp_path))
    import json

    row = db.execute(
        "SELECT payload FROM forecasts WHERE fixture_id = ?", (fixture_id,)
    ).fetchone()
    payload = json.loads(row["payload"])
    matrix = fc.forecast_matrix_from_payload(payload)
    assert sum(matrix.ttg().values()) > 0.99
    assert sum(matrix.crs().values()) > 0.99
    assert sum(matrix.hafu().values()) > 0.99
