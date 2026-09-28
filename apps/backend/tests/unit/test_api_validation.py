"""验证 API 测试（票 31）：run 列表/详情、三条件进度空态与真实态。"""

from __future__ import annotations

import json
import sqlite3
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
    # 基准分层（票 40）：直插行 close_basis=NULL → legacy 分列呈现
    assert body["clv"]["by_close_basis"]["legacy"]["bets"] == 2
    assert body["clv"]["close_basis_note"].startswith("pinnacle 主锚")
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


# ---- 票 review-20260928/03+09：三条件门两项裁决落地 ----


def _seed_base(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """一对队赛（competitions.name 唯一，只种一次），返回 (comp, home, away)。"""
    conn.execute(
        "INSERT OR IGNORE INTO competitions (name, tier, created_at)"
        " VALUES ('英超','tier1','x')"
    )
    comp = int(
        conn.execute("SELECT id FROM competitions WHERE name = '英超'").fetchone()["id"]
    )
    conn.execute(
        "INSERT OR IGNORE INTO teams (canonical_name, created_at) VALUES ('主','x')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO teams (canonical_name, created_at) VALUES ('客','x')"
    )
    home = int(
        conn.execute("SELECT id FROM teams WHERE canonical_name = '主'").fetchone()[
            "id"
        ]
    )
    away = int(
        conn.execute("SELECT id FROM teams WHERE canonical_name = '客'").fetchone()[
            "id"
        ]
    )
    return comp, home, away


def _seed_fixture(
    conn: sqlite3.Connection, base: tuple[int, int, int] | None = None, *, seq: int = 0
) -> int:
    comp, home, away = base or _seed_base(conn)
    # 同对阵四列唯一：用分钟偏移区分（同日多场不真实但测试只求行存在）
    kickoff = f"2026-09-13T02:{seq:02d}:00+00:00"
    return int(
        conn.execute(
            "INSERT INTO fixtures (competition_id, kickoff_utc, home_team_id,"
            " away_team_id) VALUES (?, ?, ?, ?)",
            (comp, kickoff, home, away),
        ).lastrowid
    )


def _review_client(tmp_path: Path, name: str, verdicts: list[str]) -> TestClient:
    """带复核记录的客户端：每条 verdict 独立场次（review_items 每场每路由唯一）。"""
    db_path = tmp_path / name
    conn = connect(db_path)
    migrate(conn)
    base = _seed_base(conn)
    for i, verdict in enumerate(verdicts):
        fixture = _seed_fixture(conn, base, seq=i)
        conn.execute(
            "INSERT INTO review_items (fixture_id, route, status, verdict,"
            " created_at, decided_at) VALUES (?, 'pre_match', 'done', ?,"
            " '2026-09-28T00:00:00+00:00', '2026-09-28T01:00:00+00:00')",
            (fixture, verdict),
        )
    conn.commit()
    conn.close()
    return TestClient(create_app(settings=Settings(db_path=db_path)))


@pytest.mark.parametrize(
    ("verdicts", "achieved", "expect_text"),
    [
        # ≤10% 且 ≥10 条：1/12 = 8.3% → 过
        (["misleading", *["key_contribution"] * 11], True, "misleading 1/12"),
        # 样本不足（9 < 10）：恒不过
        (["key_contribution"] * 9, False, "样本不足"),
        # 超阈（2/12 = 16.7%）→ 不过
        (
            ["misleading", "misleading", *["key_contribution"] * 10],
            False,
            "misleading 2/12",
        ),
        # 无记录：未评估（glossary 原口径保留）
        ([], False, "未评估"),
    ],
)
def test_review_errors_gate(
    tmp_path: Path, verdicts: list[str], achieved: bool, expect_text: str
) -> None:
    with _review_client(
        tmp_path, f"review-{len(verdicts)}-{verdicts[:1]}.db", verdicts
    ) as client:
        body = client.get("/api/v1/validation/progress").json()
    condition = {c["key"]: c for c in body["conditions"]}["review_errors"]
    assert condition["achieved"] is achieved
    assert expect_text in condition["current"]


def test_market_skill_deploys_latest_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """票 09 裁决：只认最新 issued_at 的部署版本——历史好版本救不了部署版负 skill。"""
    db_path = tmp_path / "deployed.db"
    conn = connect(db_path)
    migrate(conn)
    fixture = _seed_fixture(conn)
    for version, issued in (
        ("dc-v1", "2026-09-20T00:00:00+00:00"),
        ("dc-v2", "2026-09-27T00:00:00+00:00"),
    ):
        conn.execute(
            "INSERT INTO forecasts (fixture_id, track, model_version, issued_at,"
            " content_hash, payload) VALUES (?, 'ml', ?, ?, ?, '{}')",
            (fixture, version, issued, f"h-{version}"),
        )
    conn.commit()
    conn.close()
    groups = {
        "dc-v1": {"n": 50, "skill_rps": 0.02},
        "dc-v2": {"n": 50, "skill_rps": -0.01},
    }
    monkeypatch.setattr(
        "goalx_backend.evaluation.forward_validation.forward_skill_report",
        lambda conn: {"groups": groups},
    )
    with TestClient(create_app(settings=Settings(db_path=db_path))) as client:
        body = client.get("/api/v1/validation/progress").json()
    condition = {c["key"]: c for c in body["conditions"]}["market_skill"]
    # 部署版 dc-v2 为负 → 不过（旧 max 口径会拿 dc-v1 的 +0.02 通过）
    assert condition["achieved"] is False
    assert "dc-v2" in condition["current"]
    assert "部署" in condition["current"]

    groups["dc-v2"]["skill_rps"] = 0.01
    with TestClient(create_app(settings=Settings(db_path=db_path))) as client:
        body = client.get("/api/v1/validation/progress").json()
    condition = {c["key"]: c for c in body["conditions"]}["market_skill"]
    assert condition["achieved"] is True
    assert "dc-v2" in condition["current"]
