"""票 02 批 B：交付层输入卫生——畸形参数 422 非 500、空串拒收、脏时间戳拒收。

同垃圾输入不再有两种行为（date=banana&days=1 曾静默 200 空列表）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend.config import Settings
from goalx_backend.db import connect, migrate
from goalx_backend.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "hygiene.db"
    conn = connect(db_path)
    migrate(conn)
    # ask 端点先查 fixture 存在性（404）再解析 body——预置一场
    from goalx_backend.data import fixtures as fx

    competition = fx.upsert_competition(conn, "test")
    home = fx.upsert_team(conn, "home")
    away = fx.upsert_team(conn, "away")
    fx.upsert_fixture(conn, competition, "2026-09-01T12:00:00+00:00", home, away)
    conn.commit()
    conn.close()
    settings = Settings(db_path=db_path, research_db_path=tmp_path / "research.db")
    with TestClient(create_app(settings=settings)) as c:
        yield c


V1 = "/api/v1"


def test_malformed_as_of_is_422(client: TestClient) -> None:
    for path in ("/fixtures/1/had-quote", "/fixtures/1/research"):
        resp = client.get(f"{V1}{path}", params={"as_of": "banana"})
        assert resp.status_code == 422, (path, resp.status_code)


def test_malformed_date_is_422_even_single_day(client: TestClient) -> None:
    """days=1 曾静默 200 空列表（business_dates_from 只在 days>1 才 parse）。"""
    for endpoint in (
        "/fixtures/today",
        "/fixtures/today/attribution",
        "/markets/goals",
    ):
        resp = client.get(f"{V1}{endpoint}", params={"date": "banana", "days": 1})
        assert resp.status_code == 422, (endpoint, resp.status_code)


def test_ask_malformed_json_is_422_not_500(client: TestClient) -> None:
    resp = client.post(
        f"{V1}/fixtures/1/ask",
        content="not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_manual_join_rejects_empty_strings(client: TestClient) -> None:
    resp = client.post(f"{V1}/fixtures/1/join", json={"event_id": "", "sport_key": ""})
    assert resp.status_code == 422


def test_create_bet_rejects_empty_request_id(client: TestClient) -> None:
    resp = client.post(
        f"{V1}/bets",
        json={
            "mode": "paper",
            "stake": 2,
            "legs": [
                {
                    "fixture_id": 1,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 2.0,
                }
            ],
            "client_request_id": "",
        },
    )
    assert resp.status_code == 422


def test_pool_state_published_at_must_be_iso() -> None:
    """模型层钉死（端点有期次存在性 404 前置，banana 在校验期即 422）。"""
    from pydantic import ValidationError

    from goalx_backend.api.pool import PoolStateImportPayload

    with pytest.raises(ValidationError, match="published_at"):
        PoolStateImportPayload(period_no="26001", published_at="banana")


def test_empty_request_id_skips_idempotency_lookup(db: sqlite3.Connection) -> None:
    """域层：None/"" 不入幂等查询——历史 "" 键行不被新注误撞（审计 B14）。"""
    from goalx_backend.betting.bets import replay_bet_id

    assert replay_bet_id(db, None, None) is None
    assert replay_bet_id(db, "", None) is None
