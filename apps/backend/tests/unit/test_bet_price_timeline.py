"""五价时点记录管道（report-audit-20261103 票 09）。

纸面决策的每个价格事件自动落 bet_price_timeline 阶段行：发现（建注）、
决策+确认（锁定重绑/落库终态）、票面（live 实际条款）。UNIQUE(bet,
fixture, stage) 吸收重跑与幂等重放；409 未确认路径整票回滚不留时间线行
（未确认=未决策，预注册 §8）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from goalx_backend.betting import store as bt_store
from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.ingest.demo import seed_demo
from goalx_backend.db import connect, migrate
from goalx_backend.main import create_app
from goalx_backend.models import BetMode, MarketKind, SnapshotInput


def _setup(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection, int]:
    db_path = tmp_path / "price-timeline.db"
    conn = connect(db_path)
    migrate(conn)
    fixture_id = int(seed_demo(conn)["fixture_ids"][0])  # type: ignore[index]
    client = TestClient(create_app(settings=Settings(db_path=db_path)))
    client.__enter__()
    return client, conn, fixture_id


def _paper_leg(fixture_id: int, odds: float, **extra: Any) -> dict[str, Any]:
    return {
        "mode": "paper",
        "stake": 2,
        "legs": [
            {
                "fixture_id": fixture_id,
                "market_code": "had",
                "selection_code": "h",
                "locked_odds": odds,
            }
        ],
        **extra,
    }


def _stages(conn: sqlite3.Connection, bet_id: int) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM bet_price_timeline WHERE bet_id = ? ORDER BY id",
        (bet_id,),
    ).fetchall()
    return {str(r["stage"]): r for r in rows}


def _refresh_price(conn: sqlite3.Connection, fixture_id: int, odds: float) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for sel, price in (("h", odds), ("d", 5.0), ("a", 1.3)):
        fx_store.insert_odds_snapshot(
            conn,
            SnapshotInput(
                fixture_id=fixture_id,
                market_code="had",
                selection_code=sel,
                source="sporttery",
                odds=price,
                captured_at=now,
                observed_at=now,
                source_updated_at=now,
            ),
        )
    conn.commit()


def test_paper_create_records_discovery(tmp_path: Path) -> None:
    """建注即落发现价行：承载价 + 建注绑定身份（system/manual 同记）。"""
    client, conn, fixture_id = _setup(tmp_path)
    try:
        created = client.post("/api/v1/bets", json=_paper_leg(fixture_id, 6.5))
        assert created.status_code == 201, created.text
        bet = created.json()
        stages = _stages(conn, bet["id"])
        assert set(stages) == {"discovery"}
        row = stages["discovery"]
        assert row["odds"] == 6.5
        assert row["snapshot_id"] == bet["legs"][0]["snapshot_id"]
        assert row["recorded_at"] is not None
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_live_create_records_no_discovery(tmp_path: Path) -> None:
    """live 回录是事后记账：不落发现价（预注册 §3 live 只记票面价）。"""
    client, conn, fixture_id = _setup(tmp_path)
    try:
        created = client.post(
            "/api/v1/bets", json=_paper_leg(fixture_id, 6.5) | {"mode": "live"}
        )
        assert created.status_code == 201, created.text
        assert _stages(conn, created.json()["id"]) == {}
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_replay_same_request_id_no_duplicate_timeline(tmp_path: Path) -> None:
    """幂等重放回带既有注：时间线不落双行（UNIQUE + 短路双防线）。"""
    client, conn, fixture_id = _setup(tmp_path)
    try:
        payload = _paper_leg(fixture_id, 6.5) | {"client_request_id": "req-1"}
        first = client.post("/api/v1/bets", json=payload)
        replay = client.post("/api/v1/bets", json=payload)
        assert first.status_code == replay.status_code == 201
        assert replay.json()["id"] == first.json()["id"]
        n = conn.execute(
            "SELECT COUNT(*) FROM bet_price_timeline WHERE bet_id = ?",
            (first.json()["id"],),
        ).fetchone()[0]
        assert n == 1
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_lock_records_decision_and_confirm(tmp_path: Path) -> None:
    """锁定（价未变）：decision=重绑服务器价，confirm=终态价，两者恒等。"""
    client, conn, fixture_id = _setup(tmp_path)
    try:
        created = client.post("/api/v1/bets", json=_paper_leg(fixture_id, 6.5))
        bet_id = created.json()["id"]
        slip = client.post("/api/v1/bet-slips", json={"bet_ids": [bet_id]})
        assert slip.status_code == 201, slip.text
        stages = _stages(conn, bet_id)
        assert set(stages) == {"discovery", "decision", "confirm"}
        assert stages["decision"]["odds"] == 6.5
        assert stages["confirm"]["odds"] == 6.5
        assert stages["decision"]["snapshot_id"] == stages["confirm"]["snapshot_id"]
        assert stages["decision"]["recorded_at"] <= stages["confirm"]["recorded_at"]
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_lock_price_change_409_leaves_no_rows_then_confirm_records(
    tmp_path: Path,
) -> None:
    """价变未确认 409 整票回滚不留时间线；确认更新后按新价落行。"""
    client, conn, fixture_id = _setup(tmp_path)
    try:
        created = client.post("/api/v1/bets", json=_paper_leg(fixture_id, 6.5))
        bet_id = created.json()["id"]
        _refresh_price(conn, fixture_id, 5.8)
        rejected = client.post("/api/v1/bet-slips", json={"bet_ids": [bet_id]})
        assert rejected.status_code == 409, rejected.text
        assert set(_stages(conn, bet_id)) == {"discovery"}
        confirmed = client.post(
            "/api/v1/bet-slips", json={"bet_ids": [bet_id], "allow_price_update": True}
        )
        assert confirmed.status_code == 201, confirmed.text
        stages = _stages(conn, bet_id)
        assert set(stages) == {"discovery", "decision", "confirm"}
        assert stages["discovery"]["odds"] == 6.5  # 发现价不被覆写
        assert stages["decision"]["odds"] == 5.8
        assert stages["confirm"]["odds"] == 5.8
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_manual_bet_lock_confirm_only(tmp_path: Path) -> None:
    """manual_research 锁定：只落 confirm（客户端价），无 decision 行。"""
    client, conn, fixture_id = _setup(tmp_path)
    try:
        created = client.post(
            "/api/v1/bets",
            json=_paper_leg(fixture_id, 9.9, allow_manual_price=True),
        )
        assert created.status_code == 201
        bet_id = created.json()["id"]
        assert created.json()["price_source"] == "manual_research"
        slip = client.post("/api/v1/bet-slips", json={"bet_ids": [bet_id]})
        assert slip.status_code == 201, slip.text
        stages = _stages(conn, bet_id)
        assert set(stages) == {"discovery", "confirm"}
        assert stages["confirm"]["odds"] == 9.9
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_live_actuals_record_ticket_stage(tmp_path: Path) -> None:
    """live 实际条款回录落票面价行（人工录入无快照身份，NULL 诚实）。"""
    client, conn, fixture_id = _setup(tmp_path)
    try:
        created = client.post(
            "/api/v1/bets", json=_paper_leg(fixture_id, 6.5) | {"mode": "live"}
        )
        bet_id = created.json()["id"]
        slip = client.post(
            "/api/v1/bet-slips",
            json={
                "bet_ids": [bet_id],
                "actuals": {
                    str(bet_id): {
                        "stake": 2,
                        "leg_odds": [{"fixture_id": fixture_id, "odds": 6.4}],
                    }
                },
            },
        )
        assert slip.status_code == 201, slip.text
        stages = _stages(conn, bet_id)
        assert set(stages) == {"ticket"}
        assert stages["ticket"]["odds"] == 6.4
        assert stages["ticket"]["snapshot_id"] is None
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_actuals_duplicate_fixture_rejected(tmp_path: Path) -> None:
    """correctness F1：同一 fixture 两条实际赔率（后写覆盖 actual_odds、
    首写进时间线）会让两份账分裂——fail-loud 拒收整票。"""
    client, conn, fixture_id = _setup(tmp_path)
    try:
        created = client.post(
            "/api/v1/bets", json=_paper_leg(fixture_id, 6.5) | {"mode": "live"}
        )
        bet_id = created.json()["id"]
        slip = client.post(
            "/api/v1/bet-slips",
            json={
                "bet_ids": [bet_id],
                "actuals": {
                    str(bet_id): {
                        "stake": 2,
                        "leg_odds": [
                            {"fixture_id": fixture_id, "odds": 6.4},
                            {"fixture_id": fixture_id, "odds": 9.9},
                        ],
                    }
                },
            },
        )
        assert slip.status_code == 400, slip.text
        assert _stages(conn, bet_id) == {}  # 整票回滚，无任何阶段行
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_ticket_stage_uses_server_clock_not_placed_at(tmp_path: Path) -> None:
    """correctness F3：placed_at 客户端可回填——票面价 recorded_at 恒服务器时钟。"""
    client, conn, fixture_id = _setup(tmp_path)
    try:
        created = client.post(
            "/api/v1/bets", json=_paper_leg(fixture_id, 6.5) | {"mode": "live"}
        )
        bet_id = created.json()["id"]
        before = datetime.now(UTC).isoformat(timespec="seconds")
        slip = client.post(
            "/api/v1/bet-slips",
            json={
                "bet_ids": [bet_id],
                "placed_at": "2020-01-01T00:00:00+00:00",
                "actuals": {
                    str(bet_id): {
                        "stake": 2,
                        "leg_odds": [{"fixture_id": fixture_id, "odds": 6.4}],
                    }
                },
            },
        )
        assert slip.status_code == 201, slip.text
        ticket = _stages(conn, bet_id)["ticket"]
        assert ticket["recorded_at"] >= before  # 服务器时钟，非回填 placed_at
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_record_price_stage_unique_absorbs_rerun(tmp_path: Path) -> None:
    """仓储层：同 (bet, fixture, stage) 重跑 INSERT OR IGNORE 吸收，首行不覆写。"""
    db_path = tmp_path / "stage-unique.db"
    conn = connect(db_path)
    migrate(conn)
    try:
        fixture_id = int(seed_demo(conn)["fixture_ids"][0])  # type: ignore[index]
        bet = bt_store.create_bet(conn, BetMode.PAPER, MarketKind.FIXED, 2.0)
        for odds, at in (
            (6.5, "2026-11-04T08:00:00+00:00"),
            (7.0, "2026-11-04T09:00:00+00:00"),
        ):
            bt_store.record_price_stage(
                conn,
                bet,
                fixture_id,
                "h",
                "discovery",
                odds=odds,
                snapshot_id=None,
                recorded_at=at,
            )
        rows = conn.execute(
            "SELECT odds, recorded_at FROM bet_price_timeline WHERE bet_id = ?",
            (bet,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["odds"] == 6.5  # 首行生效，重跑不覆写
        assert rows[0]["recorded_at"] == "2026-11-04T08:00:00+00:00"
    finally:
        conn.close()
