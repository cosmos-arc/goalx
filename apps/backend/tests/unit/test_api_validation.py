"""验证 API 测试（票 31）：run 列表/详情、三条件进度空态与真实态。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend.config import Settings
from goalx_backend.db import connect, migrate
from goalx_backend.main import create_app


@pytest.fixture
def seeded_client(tmp_path: Path) -> Iterator[TestClient]:
    """预置一个 done 回测 run（含指标）与注单的 API 客户端。"""
    db_path = tmp_path / "validation.db"
    conn = connect(db_path)
    migrate(conn)
    conn.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at, finished_at,"
        " summary) VALUES ('m2-smoke', '{}', 'done', '2026-09-13T10:00:00+00:00',"
        " '2026-09-13T10:05:00+00:00', ?)",
        (json.dumps({"predictions": 100, "bets": 12, "roi": -0.01}),),
    )
    run_id = int(conn.execute("SELECT id FROM backtest_runs").fetchone()["id"])
    conn.execute(
        "INSERT INTO backtest_metrics (run_id, scope, metrics)"
        " VALUES (?, 'overall', ?)",
        (
            run_id,
            json.dumps(
                {
                    "n": 100,
                    "rps_model": 0.2,
                    "rps_market": 0.199,
                    "skill_rps": 0.005,
                    "dm_p": 0.3,
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO backtest_metrics (run_id, scope, metrics) VALUES (?, 'E0', ?)",
        (run_id, json.dumps({"n": 60, "skill_rps": 0.01})),
    )
    # 两注已结算 paper 注（yield 曲线）
    comp = conn.execute(
        "INSERT INTO competitions (name, tier, created_at) VALUES ('英超','tier1','x')"
    ).lastrowid
    home = conn.execute(
        "INSERT INTO teams (canonical_name, created_at) VALUES ('主','x')"
    ).lastrowid
    away = conn.execute(
        "INSERT INTO teams (canonical_name, created_at) VALUES ('客','x')"
    ).lastrowid
    fixture = conn.execute(
        "INSERT INTO fixtures (competition_id, kickoff_utc, home_team_id,"
        " away_team_id) VALUES (?, '2026-09-13T02:00:00+00:00', ?, ?)",
        (comp, home, away),
    ).lastrowid
    # 两个不同决策的单关（不同选项；同决策重试会被去重,票 34）
    for status, stake, profit, selection in (
        ("won", 50.0, 40.0, "h"),
        ("lost", 50.0, -50.0, "a"),
    ):
        bet = conn.execute(
            "INSERT INTO bets (mode, market_kind, stake, created_at, status,"
            " profit, settled_at, purchased) VALUES ('paper','fixed',?,"
            " '2026-09-13T03:00:00"
            "+00:00', ?, ?, '2026-09-13T04:00:00+00:00', 1)",
            (stake, status, profit),
        ).lastrowid
        conn.execute(
            "INSERT INTO bet_legs (bet_id, fixture_id, market_code,"
            " selection_code, locked_odds) VALUES (?, ?, 'had', ?, 2.0)",
            (bet, fixture, selection),
        )
        conn.execute(
            "INSERT INTO clv_records (bet_id, fixture_id, market_code,"
            " selection_code, taken_odds, close_prob, clv_prob, close_source,"
            " minutes_to_kickoff, computed_at)"
            " VALUES (?, ?, 'had', ?, 2.0, 0.52, 0.02, 'odds_api_closing',"
            " 10.0, '2026-09-13T04:00:00+00:00')",
            (bet, fixture, selection),
        )
    conn.commit()
    conn.close()
    with TestClient(create_app(settings=Settings(db_path=db_path))) as client:
        yield client


def test_backtest_runs_list_and_detail(seeded_client: TestClient) -> None:
    runs = seeded_client.get("/api/v1/backtest/runs")
    assert runs.status_code == 200
    body = runs.json()
    assert len(body) == 1
    assert body[0]["label"] == "m2-smoke"
    assert body[0]["summary"]["predictions"] == 100
    assert body[0]["overall_metrics"]["skill_rps"] == pytest.approx(0.005)

    detail = seeded_client.get(f"/api/v1/backtest/runs/{body[0]['id']}")
    assert detail.status_code == 200
    metrics = detail.json()["metrics"]
    assert set(metrics) == {"overall", "E0"}

    assert seeded_client.get("/api/v1/backtest/runs/999").status_code == 404


def test_validation_progress_real_state(seeded_client: TestClient) -> None:
    response = seeded_client.get("/api/v1/validation/progress")
    assert response.status_code == 200
    body = response.json()
    by_key = {c["key"]: c for c in body["conditions"]}
    # 2 注 CLV（beat=100%）但不足 200 唯一注 → 未达成
    assert by_key["clv_beat"]["achieved"] is False
    assert "2 唯一注" in by_key["clv_beat"]["current"]
    # 前瞻评分集合为空 → 未评估不通过（票 34：不读最新回测 run）
    assert by_key["market_skill"]["achieved"] is False
    assert "无前瞻样本" in by_key["market_skill"]["current"]
    # 无复核 = 未评估；整赛季独立显示未完成
    assert by_key["review_errors"]["achieved"] is False
    assert "未评估" in by_key["review_errors"]["current"]
    assert by_key["full_season"]["achieved"] is False
    assert body["paper"]["unique_bets"] == 2
    assert body["paper"]["legs"] == 2
    assert body["live"]["unique_bets"] == 0
    assert len(body["yield_curve"]) == 2
    first = body["yield_curve"][0]
    assert first["cumulative_yield"] == pytest.approx(40.0 / 50.0)
    assert body["clv"]["denominator"]["unique_bets"] == 2
    assert body["latest_run"]["label"] == "m2-smoke"  # 仅展示,不供 skill


def test_validation_progress_empty_state(tmp_path: Path) -> None:
    # 空库：三条件空态正确（票 31 验收）
    db_path = tmp_path / "empty.db"
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    with TestClient(create_app(settings=Settings(db_path=db_path))) as client:
        response = client.get("/api/v1/validation/progress")
    assert response.status_code == 200
    body = response.json()
    # 空库：未知项一律不通过（票 34 验收 1）
    assert all(c["achieved"] is False for c in body["conditions"])
    assert body["yield_curve"] == []
    assert body["paper"]["unique_bets"] == 0
    assert body["live"]["unique_bets"] == 0
    assert body["unpurchased_open"] == 0
    assert body["latest_run"] is None
    assert body["clv"]["denominator"]["unique_bets"] == 0
    assert body["forward"]["coverage"]["scored"] == 0


def test_backtest_runs_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    with TestClient(create_app(settings=Settings(db_path=db_path))) as client:
        assert client.get("/api/v1/backtest/runs").json() == []
