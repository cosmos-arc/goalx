"""iterate 票 10：/bets/summary 全量聚合 + 列表有界不截断聚合面。

验收（板内原文）：>100 注 fixture 下聚合=全量——列表缺省面恰 100 行
（修复前 /bets 无界返回全量、聚合职责在客户端，两端断言均必红），
summary 的 settled_n/total_pnl/日桶反映全部注。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend.betting.summary import bets_summary
from goalx_backend.config import Settings
from goalx_backend.db import connect, migrate
from goalx_backend.main import create_app
from goalx_backend.models import BetMode


def _insert_bet(
    db: sqlite3.Connection,
    *,
    mode: str = "paper",
    purchased: int = 1,
    status: str = "won",
    stake: float = 2.0,
    actual_stake: float | None = None,
    profit: float | None = 1.0,
    settled_at: str | None = "2026-11-01T12:00:00+00:00",
    ev_consensus: float | None = None,
    ev_model: float | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO bets (mode, market_kind, purchased, stake, actual_stake,
                          created_at, status, payout, profit, settled_at,
                          snap_ev_consensus, snap_ev_model)
        VALUES (?, 'fixed', ?, ?, ?, '2026-10-31T10:00:00+00:00', ?, NULL, ?, ?, ?, ?)
        """,
        (
            mode,
            purchased,
            stake,
            actual_stake,
            status,
            profit,
            settled_at,
            ev_consensus,
            ev_model,
        ),
    )
    db.commit()


@pytest.fixture
def api_env(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    """绑定临时库的应用客户端 + 库路径（测试可直写行再走 HTTP 读）。"""
    db_path = tmp_path / "bets-summary.db"
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    settings = Settings(db_path=db_path, research_db_path=tmp_path / "research.db")
    with TestClient(create_app(settings=settings)) as client:
        yield client, db_path


def test_api_list_bounded_but_summary_full_set(
    api_env: tuple[TestClient, Path],
) -> None:
    """验收主钉：120 已结算注 → 列表恰 100（渲染事实），聚合全量（经济账）。"""
    client, db_path = api_env
    conn = connect(db_path)
    for index in range(120):
        won = index % 2 == 0
        _insert_bet(conn, status="won" if won else "lost", profit=1.0 if won else -0.5)
    conn.close()
    listing = client.get("/api/v1/bets")
    assert listing.status_code == 200
    assert len(listing.json()) == 100  # 修复前：无界返回 120 → 红
    summary = client.get("/api/v1/bets/summary")
    assert summary.status_code == 200
    paper = summary.json()["paper"]
    assert paper["settled_n"] == 120  # 修复前：端点不存在 → 红
    assert paper["total_pnl"] == 30.0
    assert paper["won_n"] == 60


def test_api_only_open_face_stays_unbounded(api_env: tuple[TestClient, Path]) -> None:
    """only_open 面=待办/结算判定集合（天然小）不有界：截它=静默漏待办。"""
    client, db_path = api_env
    conn = connect(db_path)
    for index in range(120):
        if index < 110:
            _insert_bet(conn, profit=1.0)  # 旧注已结算
        else:
            _insert_bet(conn, status="open", profit=None, settled_at=None)
    conn.close()
    assert len(client.get("/api/v1/bets", params={"only_open": True}).json()) == 10


def test_summary_domain_full_set_beyond_bound(db: sqlite3.Connection) -> None:
    """域层：120 已结算注聚合=全量；列表 limit=100 截不到聚合面。"""
    for index in range(120):
        _insert_bet(db, profit=1.0 if index % 2 == 0 else -0.5)
    faces = {face.mode: face for face in bets_summary(db)}
    paper = faces[BetMode.PAPER.value]
    assert paper.bets_n == 120
    assert paper.settled_n == 120
    assert paper.total_pnl == 30.0
    assert paper.decided_n == 120
    # 列表有界只影响展示面，聚合源职责已分离（settle 内部缺省全量不变）
    from goalx_backend.betting.store import list_bets

    assert len(list_bets(db, limit=100)) == 100
    assert len(list_bets(db)) == 120


def test_summary_settled_caliber_and_mode_split(db: sqlite3.Connection) -> None:
    """结算口径=已锁定且已结算：open 注与未锁定死建议只进 bets_n/open_n。"""
    _insert_bet(db, purchased=0, status="open", profit=None, settled_at=None)  # 活建议
    _insert_bet(
        db, purchased=1, status="open", profit=None, settled_at=None
    )  # 已锁未结
    _insert_bet(db, purchased=0, status="lost", profit=-2.0)  # 未锁定死建议：结算批照跑
    _insert_bet(db, purchased=1, status="won", profit=3.0)
    _insert_bet(db, mode="live", purchased=1, status="void", profit=0.0, stake=10.0)
    faces = {face.mode: face for face in bets_summary(db)}
    paper = faces[BetMode.PAPER.value]
    assert paper.bets_n == 4
    assert paper.open_n == 2
    assert paper.settled_n == 1
    assert paper.total_pnl == 3.0
    live = faces[BetMode.LIVE.value]
    assert live.settled_n == 1
    assert live.won_n == 0
    assert live.decided_n == 0  # void 不入分母（命中率口径）
    assert live.stake_settled == 10.0


def test_summary_day_buckets_beijing_business_date(db: sqlite3.Connection) -> None:
    """日桶=北京业务日：UTC 17:30 结算落次日桶；多日升序；EV 双口径分账。"""
    _insert_bet(
        db,
        profit=1.0,
        settled_at="2026-11-01T03:00:00+00:00",
        ev_consensus=0.02,
        ev_model=0.05,
    )  # 北京 11-01 11:00
    _insert_bet(
        db,
        status="lost",
        profit=-1.0,
        settled_at="2026-11-01T17:30:00+00:00",
        ev_model=0.04,
    )  # 北京 11-02 01:30 → 次日桶
    _insert_bet(db, status="void", profit=0.0, settled_at="2026-11-03T01:00:00+00:00")
    faces = {face.mode: face for face in bets_summary(db)}
    days = faces[BetMode.PAPER.value].days
    assert [bucket.date for bucket in days] == [
        "2026-11-01",
        "2026-11-02",
        "2026-11-03",
    ]
    first = days[0]
    assert first.settled_n == 1
    assert first.pnl == 1.0
    assert first.sum_ev_consensus == 0.02
    assert first.n_ev_consensus == 1
    assert first.sum_ev_model == 0.05
    assert first.n_ev_model == 1
    second = days[1]
    assert second.sum_ev_consensus == 0.0
    assert second.n_ev_consensus == 0
    assert second.sum_ev_model == 0.04
    assert second.n_ev_model == 1
    third = days[2]
    assert third.settled_n == 1
    assert third.decided_n == 0  # void 退款不计分母
    # 模式级 EV 总账与日桶一致（共识/模型不混算的原料）
    paper = faces[BetMode.PAPER.value]
    assert paper.n_ev_consensus == 1
    assert paper.sum_ev_consensus == 0.02
    assert paper.n_ev_model == 2
    assert paper.sum_ev_model == 0.09


def test_summary_actual_stake_overrides(db: sqlite3.Connection) -> None:
    """stake_settled 用实际条款金额（真金回录口径），无实际额回退建议额。"""
    _insert_bet(db, mode="live", stake=2.0, actual_stake=5.0, profit=1.0)
    _insert_bet(db, mode="live", stake=2.0, profit=-2.0)
    faces = {face.mode: face for face in bets_summary(db)}
    assert faces[BetMode.LIVE.value].stake_settled == 7.0


def test_summary_no_negative_zero_pnl(db: sqlite3.Connection) -> None:
    """correctness：0.3-0.1-0.2 型浮点残差不得 round 出 -0.0（API/晨检不渗 -0.00）。"""
    for profit in (0.3, -0.1, -0.2):
        _insert_bet(db, status="won" if profit > 0 else "lost", profit=profit)
    face = {f.mode: f for f in bets_summary(db)}[BetMode.PAPER.value]
    assert face.total_pnl == 0.0
    assert str(face.total_pnl) == "0.0"  # -0.0 的 str 是 "-0.0"
    assert str(face.days[0].pnl) == "0.0"
