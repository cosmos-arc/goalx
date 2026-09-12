"""API 集成测试（隔离数据库，票 22/23 端点验收）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend.config import Settings
from goalx_backend.db import connect, migrate
from goalx_backend.main import create_app

JINGCAI_PAYLOAD = {
    "errorCode": "0",
    "value": {
        "matchInfoList": [
            {
                "businessDate": "2026-09-12",
                "subMatchList": [
                    {
                        "matchId": 1,
                        "matchNumStr": "周六001",
                        "leagueAbbName": "英超",
                        "homeTeamAllName": "阿森纳",
                        "awayTeamAllName": "切尔西",
                        "matchDate": "2026-09-13",
                        "matchTime": "02:00:00",
                        "bettingSingle": 1,
                        "had": {
                            "a": "1.30",
                            "d": "5.00",
                            "h": "6.50",
                            "updateDate": "2026-09-12",
                            "updateTime": "20:00:00",
                        },
                    }
                ],
            }
        ]
    },
}


@pytest.fixture
def api_client(tmp_path: Path) -> Iterator[TestClient]:
    """绑定临时数据库的应用客户端（含预置竞彩场次）。"""
    db_path = tmp_path / "api-test.db"
    conn = connect(db_path)
    migrate(conn)
    from goalx_backend.ingest import sporttery

    sporttery.store_matches(conn, sporttery.parse_matches(JINGCAI_PAYLOAD))
    conn.close()
    settings = Settings(db_path=db_path)
    with TestClient(create_app(settings=settings)) as client:
        yield client


def _fixture_id(api_client: TestClient) -> int:
    today = api_client.get("/api/v1/fixtures/today", params={"date": "2026-09-12"})
    assert today.status_code == 200
    return int(today.json()[0]["fixture_id"])


def test_today_view_shape(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/fixtures/today", params={"date": "2026-09-12"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["match_code"] == "周六001"
    assert entry["jc_odds"] == {"h": 6.5, "d": 5.0, "a": 1.3}
    assert entry["jc_updated_at"] == "2026-09-12T12:00:00+00:00"
    assert entry["joined"] is False
    assert entry["eu_prob"] is None
    assert "not_joined" in entry["flags"]


def test_today_defaults_to_beijing_date(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/fixtures/today")
    assert response.status_code == 200
    assert response.json() == []  # 今天（北京日期）无场次


def test_fixture_odds_history_and_404(api_client: TestClient) -> None:
    fixture_id = _fixture_id(api_client)
    response = api_client.get(f"/api/v1/fixtures/{fixture_id}/odds")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3  # had: h/d/a
    assert {row["selection_code"] for row in body} == {"h", "d", "a"}
    assert api_client.get("/api/v1/fixtures/999/odds").status_code == 404


def test_bet_create_list_and_same_fixture_rejected(api_client: TestClient) -> None:
    fixture_id = _fixture_id(api_client)
    create = api_client.post(
        "/api/v1/bets",
        json={
            "mode": "paper",
            "stake": 100.0,
            "legs": [
                {
                    "fixture_id": fixture_id,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 6.5,
                }
            ],
        },
    )
    assert create.status_code == 201
    bet = create.json()
    assert bet["status"] == "open"
    assert bet["purchased"] is False
    assert bet["legs"][0]["selection_code"] == "h"

    same_fixture = api_client.post(
        "/api/v1/bets",
        json={
            "mode": "paper",
            "stake": 2.0,
            "legs": [
                {
                    "fixture_id": fixture_id,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 6.5,
                },
                {
                    "fixture_id": fixture_id,
                    "market_code": "hhad",
                    "selection_code": "a",
                    "locked_odds": 1.9,
                },
            ],
        },
    )
    assert same_fixture.status_code == 400

    listing = api_client.get("/api/v1/bets")
    assert listing.status_code == 200
    assert len(listing.json()) == 1


def test_slip_record_purchase_flow(api_client: TestClient) -> None:
    fixture_id = _fixture_id(api_client)
    bet_id = api_client.post(
        "/api/v1/bets",
        json={
            "mode": "paper",
            "stake": 100.0,
            "legs": [
                {
                    "fixture_id": fixture_id,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 6.5,
                }
            ],
        },
    ).json()["id"]
    slip = api_client.post(
        "/api/v1/bet-slips",
        json={"bet_ids": [bet_id], "placed_at": "2026-09-12T19:00:00+00:00"},
    )
    assert slip.status_code == 201
    assert slip.json()["bet_count"] == 1
    assert slip.json()["stake_total"] == 100.0
    bets = api_client.get("/api/v1/bets").json()
    assert bets[0]["purchased"] is True
    assert bets[0]["slip_id"] == slip.json()["id"]

    missing = api_client.post("/api/v1/bet-slips", json={"bet_ids": [999]})
    assert missing.status_code == 404


def test_draw_results_import_and_settlement_loop(api_client: TestClient) -> None:
    fixture_id = _fixture_id(api_client)
    bet_id = api_client.post(
        "/api/v1/bets",
        json={
            "mode": "paper",
            "stake": 100.0,
            "legs": [
                {
                    "fixture_id": fixture_id,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 6.5,
                }
            ],
        },
    ).json()["id"]
    assert bet_id > 0

    unknown = api_client.post(
        "/api/v1/draw-results",
        json={"results": [{"fixture_id": 999, "home_goals": 1, "away_goals": 0}]},
    )
    assert unknown.status_code == 404

    imported = api_client.post(
        "/api/v1/draw-results",
        json={
            "source": "manual",
            "results": [
                {
                    "fixture_id": fixture_id,
                    "home_goals": 3,
                    "away_goals": 1,
                    "half_home_goals": 2,
                    "half_away_goals": 0,
                }
            ],
        },
    )
    assert imported.status_code == 201
    assert imported.json() == {"imported": 1}

    listing = api_client.get("/api/v1/draw-results", params={"fixture_id": fixture_id})
    assert listing.json()[0]["home_goals"] == 3

    run = api_client.post("/api/v1/settlements/run")
    assert run.status_code == 200
    assert run.json() == {"settled": 1, "still_open": 0, "won": 1, "lost": 0, "void": 0}

    bets = api_client.get("/api/v1/bets").json()
    assert bets[0]["status"] == "won"
    assert bets[0]["payout"] == 650.0
    assert bets[0]["profit"] == 550.0


def test_bankroll_endpoint(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/bankroll")
    assert response.status_code == 200
    assert response.json() == {"balance": None, "events": []}


def test_pool_slip_creation_and_materialization(api_client: TestClient) -> None:
    fixture_id = _fixture_id(api_client)
    response = api_client.post(
        "/api/v1/pool-slips",
        json={
            "mode": "paper",
            "stake_per_combination": 2.0,
            "picks": [
                {"match_seq": 1, "selection_code": "3", "fixture_id": fixture_id},
                {"match_seq": 1, "selection_code": "1", "fixture_id": fixture_id},
                {"match_seq": 2, "selection_code": "0"},
            ],
        },
    )
    assert response.status_code == 201
    slip = response.json()
    assert slip["mode"] == "paper"
    combos = api_client.get("/api/v1/bet-slips")
    assert any(row["id"] == slip["id"] for row in combos.json())


def test_manual_join_endpoint(api_client: TestClient) -> None:
    fixture_id = _fixture_id(api_client)
    assert (
        api_client.post(
            f"/api/v1/fixtures/{fixture_id}/join",
            json={"event_id": "evt-manual", "sport_key": "soccer_epl"},
        ).status_code
        == 200
    )
    today = api_client.get(
        "/api/v1/fixtures/today", params={"date": "2026-09-12"}
    ).json()
    assert today[0]["joined"] is True
    assert (
        api_client.post(
            "/api/v1/fixtures/999/join",
            json={"event_id": "x", "sport_key": "y"},
        ).status_code
        == 404
    )
