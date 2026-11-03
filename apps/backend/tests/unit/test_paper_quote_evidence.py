"""纸面报价证据闭环（boundary-hardening 票 04）。

locked_odds 不再是客户端单方断言：paper 建注/锁定时服务器按共享证据面
重取当前有效 had 报价比对——价变 409 返回新价要求确认（人裁决①）；
三分类标签（system_paper/live_backfill/manual_research）落库并作为
正式纸面成绩面唯一入口；腿级证据身份落 bet_legs.snapshot_id。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.ingest.demo import seed_demo
from goalx_backend.db import connect, migrate
from goalx_backend.main import create_app
from goalx_backend.models import SnapshotInput


def _client(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection]:
    db_path = tmp_path / "paper-quote.db"
    conn = connect(db_path)
    migrate(conn)
    fixture_id = int(seed_demo(conn)["fixture_ids"][0])  # type: ignore[index]
    client = TestClient(create_app(settings=Settings(db_path=db_path)))
    client.__enter__()
    return client, conn, fixture_id


def _had_leg(fixture_id: int, odds: float, **extra: Any) -> dict[str, Any]:
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


def _refresh_price(conn: sqlite3.Connection, fixture_id: int, odds: float) -> int:
    """追加一轮新价三向快照（模拟页面停留期间报价变动）；返回 h 行 id。"""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    h_sid = None
    for sel, price in (("h", odds), ("d", 5.0), ("a", 1.3)):
        cur = fx_store.insert_odds_snapshot(
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
        assert cur is not None
        if sel == "h":
            h_sid = cur
    conn.commit()
    assert h_sid is not None
    return h_sid


def test_paper_price_match_binds_system_paper(tmp_path: Path) -> None:
    """价一致 → system_paper 标签 + 腿级快照身份落库（验收①）。"""
    client, conn, fixture_id = _client(tmp_path)
    try:
        created = client.post("/api/v1/bets", json=_had_leg(fixture_id, 6.5))
        assert created.status_code == 201, created.text
        bet = created.json()
        assert bet["price_source"] == "system_paper"
        assert bet["legs"][0]["snapshot_id"] is not None
        # 身份键真实指向 sporttery 快照行
        row = conn.execute(
            "SELECT source FROM odds_snapshots WHERE id = ?",
            (bet["legs"][0]["snapshot_id"],),
        ).fetchone()
        assert row is not None
        assert row["source"] == "sporttery"
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_paper_price_mismatch_409_then_confirm_new_price(tmp_path: Path) -> None:
    """验收②：提交时报价已变 → 409 返回新价；按新价重提 → system_paper。"""
    client, conn, fixture_id = _client(tmp_path)
    try:
        stale = client.post("/api/v1/bets", json=_had_leg(fixture_id, 6.5))
        assert stale.status_code == 201
        new_sid = _refresh_price(conn, fixture_id, 5.8)

        changed = client.post("/api/v1/bets", json=_had_leg(fixture_id, 6.5))
        assert changed.status_code == 409
        detail = changed.json()["detail"]
        quotes = detail["current_quotes"]
        assert quotes[0]["server_odds"] == 5.8
        assert quotes[0]["snapshot_id"] == new_sid

        confirmed = client.post("/api/v1/bets", json=_had_leg(fixture_id, 5.8))
        assert confirmed.status_code == 201
        assert confirmed.json()["price_source"] == "system_paper"
        assert confirmed.json()["legs"][0]["snapshot_id"] == new_sid
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_manual_price_channel_labels_research(tmp_path: Path) -> None:
    """显式手工价通道（裁决① 的研究出口）：客户端价保留,manual_research。"""
    client, conn, fixture_id = _client(tmp_path)
    try:
        manual = client.post(
            "/api/v1/bets",
            json=_had_leg(fixture_id, 9.9, allow_manual_price=True),
        )
        assert manual.status_code == 201
        bet = manual.json()
        assert bet["price_source"] == "manual_research"
        assert bet["legs"][0]["locked_odds"] == 9.9  # 手工价不被改写
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_unquotable_and_non_had_label_manual_research(tmp_path: Path) -> None:
    """报价不可得（非 had 市场）→ manual_research,不拒建不虚标。"""
    client, _conn, fixture_id = _client(tmp_path)
    try:
        ttg = client.post(
            "/api/v1/bets",
            json={
                "mode": "paper",
                "stake": 2,
                "legs": [
                    {
                        "fixture_id": fixture_id,
                        "market_code": "ttg",
                        "selection_code": "2",
                        "locked_odds": 4.5,
                    }
                ],
            },
        )
        assert ttg.status_code == 201
        assert ttg.json()["price_source"] == "manual_research"
        assert ttg.json()["legs"][0]["snapshot_id"] is None
    finally:
        client.__exit__(None, None, None)
        _conn.close()


def test_live_label_and_no_tightening(tmp_path: Path) -> None:
    """live 一律 live_backfill：客户端价不做系统约束（不变量：live 不收紧）。"""
    client, _conn, fixture_id = _client(tmp_path)
    try:
        live = client.post(
            "/api/v1/bets", json={**_had_leg(fixture_id, 99.0), "mode": "live"}
        )
        assert live.status_code == 201
        assert live.json()["price_source"] == "live_backfill"
    finally:
        client.__exit__(None, None, None)
        _conn.close()


def test_lock_drift_409_then_confirm_updates_leg(tmp_path: Path) -> None:
    """锁定时价重验：价变 409；确认后 locked_odds/快照身份刷新为决策价。"""
    client, conn, fixture_id = _client(tmp_path)
    try:
        bet_id = client.post("/api/v1/bets", json=_had_leg(fixture_id, 6.5)).json()[
            "id"
        ]
        new_sid = _refresh_price(conn, fixture_id, 5.2)

        rejected = client.post("/api/v1/bet-slips", json={"bet_ids": [bet_id]})
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["current_quotes"][0]["server_odds"] == 5.2
        # 整票回滚：未购、无票
        assert client.get("/api/v1/bets").json()[0]["purchased"] is False

        confirmed = client.post(
            "/api/v1/bet-slips",
            json={"bet_ids": [bet_id], "allow_price_update": True},
        )
        assert confirmed.status_code == 201
        leg = conn.execute(
            "SELECT locked_odds, snapshot_id FROM bet_legs WHERE bet_id = ?",
            (bet_id,),
        ).fetchone()
        assert leg["locked_odds"] == 5.2
        assert leg["snapshot_id"] == new_sid
        row = conn.execute(
            "SELECT price_source FROM bets WHERE id = ?", (bet_id,)
        ).fetchone()
        assert row["price_source"] == "system_paper"
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_lock_manual_research_not_reverified(tmp_path: Path) -> None:
    """manual_research 锁定不重验不改写（类③自定价；改写反而破坏假设记录）。"""
    client, conn, fixture_id = _client(tmp_path)
    try:
        bet_id = client.post(
            "/api/v1/bets",
            json=_had_leg(fixture_id, 7.7, allow_manual_price=True),
        ).json()["id"]
        _refresh_price(conn, fixture_id, 4.0)
        locked = client.post("/api/v1/bet-slips", json={"bet_ids": [bet_id]})
        assert locked.status_code == 201  # 无 409
        leg = conn.execute(
            "SELECT locked_odds FROM bet_legs WHERE bet_id = ?", (bet_id,)
        ).fetchone()
        assert leg["locked_odds"] == 7.7  # 手工价原样
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_review_forward_gates_on_price_source(tmp_path: Path) -> None:
    """验收④：仅 system_paper 进正式面——manual_research/历史 NULL 均
    excluded_price_source（裁决③：历史不回溯打标）。"""
    client, conn, fixture_id = _client(tmp_path)
    try:
        manual_id = client.post(
            "/api/v1/bets",
            json=_had_leg(fixture_id, 7.7, allow_manual_price=True),
        ).json()["id"]
        system_id = client.post("/api/v1/bets", json=_had_leg(fixture_id, 6.5)).json()[
            "id"
        ]
        # 历史形态：抹掉标签模拟 v35 前存量注
        legacy_id = client.post("/api/v1/bets", json=_had_leg(fixture_id, 6.5)).json()[
            "id"
        ]
        conn.execute("UPDATE bets SET price_source = NULL WHERE id = ?", (legacy_id,))
        conn.commit()
        for bet_id in (manual_id, system_id, legacy_id):
            locked = client.post("/api/v1/bet-slips", json={"bet_ids": [bet_id]})
            assert locked.status_code == 201
        forwards = {
            bet["id"]: bet["review"]["forward"]
            for bet in client.get("/api/v1/bets").json()
        }
        assert forwards[system_id] == "missing_closing"  # 正常流入成绩面口径
        assert forwards[manual_id] == "excluded_price_source"
        # 裁决③：历史 NULL 不回溯排除——沿旧判定链（缺 closing 而非价源）
        assert forwards[legacy_id] == "missing_closing"
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_pool_period_detail_exposes_stable_id(tmp_path: Path) -> None:
    """验收③：期次详情暴露稳定 id;池票落库携带真实期次身份。"""
    client, conn, fixture_id = _client(tmp_path)
    try:
        period_no = conn.execute(
            "SELECT period_no FROM pool_periods ORDER BY id LIMIT 1"
        ).fetchone()["period_no"]
        expected = conn.execute(
            "SELECT id FROM pool_periods WHERE period_no = ?", (period_no,)
        ).fetchone()["id"]
        detail = client.get(f"/api/v1/pool/periods/{period_no}")
        assert detail.status_code == 200
        assert detail.json()["pool_period_id"] == expected

        slip = client.post(
            "/api/v1/pool-slips",
            json={
                "mode": "paper",
                "pool_period_id": expected,
                "stake_per_combination": 2,
                "picks": [
                    # 任9 口径（票 01）：9 场单选 → 1 组合
                    {"match_seq": seq, "selection_code": "3"}
                    for seq in range(1, 10)
                ],
            },
        )
        assert slip.status_code == 201, slip.text
        row = conn.execute(
            "SELECT pool_period_id FROM bet_slips WHERE id = ?", (slip.json()["id"],)
        ).fetchone()
        assert row["pool_period_id"] == expected
        del fixture_id
    finally:
        client.__exit__(None, None, None)
        conn.close()


def test_settled_denominator_gates_manual_research(tmp_path: Path) -> None:
    """范围④成绩面闸门：manual_research 纸面注排除出验证/CLV 共用分母
    （settled_purchased_bets）；system_paper 与历史 NULL 维持、live 不受影响。"""
    from goalx_backend.betting.store import settled_purchased_bets
    from goalx_backend.db import utc_now_iso

    conn = connect(tmp_path / "denom.db")
    try:
        migrate(conn)
        now = utc_now_iso()
        # bet_legs FK 需要 fixture 父行（selections 目录由 v1 迁移播种）
        conn.execute(
            "INSERT INTO competitions (id, name, created_at) VALUES (1, '英超', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO teams (id, canonical_name, created_at)"
            " VALUES (1, '主', ?), (2, '客', ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO fixtures (id, competition_id, kickoff_utc,"
            " home_team_id, away_team_id) VALUES (1, 1, ?, 1, 2)",
            (now,),
        )
        rows = [
            (1, "paper", "system_paper"),
            (2, "paper", "manual_research"),
            (3, "paper", None),  # 历史未分类（裁决③不回溯）
            (4, "live", "manual_research"),  # live 语义不受纸面闸门影响
        ]
        for bet_id, mode, source in rows:
            conn.execute(
                "INSERT INTO bets (id, mode, market_kind, purchased, stake,"
                " created_at, status, profit, settled_at, price_source)"
                " VALUES (?, ?, 'fixed', 1, 2, ?, 'won', 1, ?, ?)",
                (bet_id, mode, now, now, source),
            )
            conn.execute(
                "INSERT INTO bet_legs (bet_id, fixture_id, market_code,"
                " selection_code, locked_odds) VALUES (?, 1, 'had', 'h', 2.0)",
                (bet_id,),
            )
        conn.commit()
        kept = {bet.bet_id for bet in settled_purchased_bets(conn)}
        assert kept == {1, 3, 4}  # 2=manual_research 纸面注被闸门排除
    finally:
        conn.close()
