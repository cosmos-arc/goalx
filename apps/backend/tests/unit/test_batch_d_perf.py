"""票 04 批 D 性能三小：列表 LIMIT / goals 页常数查询 / bankroll 舍入。

D11 列表端点有界（store 层 limit 参数，内部调用方缺省全量语义不变）；
D12 goals 页 O(场×5) 收敛常数条（trace 计数断言）+ 批量 ≡ 逐场等价；
D13 balance_after 全 2 位小数（0.1+0.2 浮点和渗出钉死）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from goalx_backend.api import markets as markets_api
from goalx_backend.betting import store as bt
from goalx_backend.data import fixtures as fx
from goalx_backend.data import results as rs
from goalx_backend.db import connect, migrate

MOMENT = "2026-09-01T00:00:00+00:00"


def _seed_fixtures(db: sqlite3.Connection, n: int) -> list[int]:
    competition = fx.upsert_competition(db, "英超")
    home = fx.upsert_team(db, "主队")
    away = fx.upsert_team(db, "客队")
    ids = [
        fx.upsert_fixture(
            db,
            competition,
            f"2026-09-01T{12 + index % 12:02d}:{index:02d}:00+00:00",
            home,
            away,
        )
        for index in range(n)
    ]
    db.commit()
    return ids


# --- D11：列表端点有界 ---


def test_list_bets_limit_bounds_api_face(db: sqlite3.Connection) -> None:
    """120 注 → limit=100 恰百行（新→旧）；缺省全量（结算/更正内部面）。"""
    from goalx_backend.betting.bets import BetDraft, create_bet_with_legs
    from goalx_backend.models import BetMode, LegInput

    fixture = _seed_fixtures(db, 1)[0]
    for _ in range(120):
        create_bet_with_legs(
            db,
            BetDraft(
                mode=BetMode.PAPER,
                stake=2,
                legs=[
                    LegInput(
                        fixture_id=fixture,
                        market_code="had",
                        selection_code="h",
                        locked_odds=2.0,
                    )
                ],
            ),
        )
    assert len(bt.list_bets(db, limit=100)) == 100
    assert len(bt.list_bets(db)) == 120  # 内部调用方（settle/更正）语义不变


def test_list_slips_limit_bounds_api_face(db: sqlite3.Connection) -> None:
    """120 票 → limit=100 恰百行；缺省全量。"""
    for _ in range(120):
        db.execute(
            "INSERT INTO bet_slips (mode, created_at) VALUES ('paper', ?)",
            (MOMENT,),
        )
    db.commit()
    assert len(bt.list_slips(db, limit=100)) == 100
    assert len(bt.list_slips(db)) == 120


def test_list_draw_results_limit_bounds_api_face(db: sqlite3.Connection) -> None:
    """110 条开奖 → limit=100 恰百行（created_at 新→旧）；缺省全量。"""
    for fixture in _seed_fixtures(db, 110):
        db.execute(
            "INSERT INTO draw_results (fixture_id, home_goals, away_goals, source,"
            " created_at) VALUES (?, 1, 0, 'test', ?)",
            (fixture, MOMENT),
        )
    db.commit()
    limited = rs.list_draw_results(db, limit=100)
    assert len(limited) == 100
    assert limited[0]["created_at"] == MOMENT
    assert len(rs.list_draw_results(db)) == 110  # llm/review 内部面不变


# --- D12：goals 页常数条查询 + 批量 ≡ 逐场 ---


def test_goals_market_query_count_is_constant(tmp_path: Path) -> None:
    """3 场页全链 SELECT 计数 ≤6（逐场×5 的 O(N) 回归即 16+）。"""
    db_path = tmp_path / "goals-count.db"
    conn = connect(db_path)
    migrate(conn)
    from goalx_backend.data.ingest.demo import seed_demo

    seed_demo(conn)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        rows = markets_api.get_goals_market(conn)
    finally:
        conn.set_trace_callback(None)
    conn.close()
    assert len(rows) == 3  # demo 三场
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # 常数预算钉死 5：场次清单 1 + odds 窗查 1 + forecast 窗查 1 + 销售×2
    # （旧 O(场×5) 实现下 3 场=16——预算漂移即回归）
    assert len(selects) == 5, f"goals 页查询数 {len(selects)}：批量预取回归 O(场×N)"


def test_bulk_latest_equivalence_with_single_fixture_queries(
    db: sqlite3.Connection,
) -> None:
    """批量窗查 ≡ 逐场 latest_odds/sale/forecast（同决胜序）；细节钉死。"""
    from goalx_backend.models import SnapshotInput

    ids = _seed_fixtures(db, 3)
    for index, fixture in enumerate(ids):
        # 每场每玩法两条同选项快照：后 captured_at 胜出（价 2.5+index*0.01）
        for offset, odds in ((0, 2.0), (1, 2.5)):
            for market in ("ttg", "crs"):
                fx.insert_odds_snapshot(
                    db,
                    SnapshotInput(
                        fixture_id=fixture,
                        market_code=market,
                        selection_code="0" if market == "ttg" else "1:1",
                        source="sporttery",
                        odds=odds + index * 0.01,
                        captured_at=f"2026-09-0{1 + offset}T00:00:00+00:00",
                        observed_at=f"2026-09-0{1 + offset}T00:00:00+00:00",
                        source_updated_at=MOMENT,
                    ),
                )
        # 销售状态：比赛级 stopped 先到、市场级 on_sale 后到 → 市场级胜
        for market_code, state, observed in (
            (None, "stopped", "2026-09-01T00:00:00+00:00"),
            ("ttg", "on_sale", "2026-09-01T06:00:00+00:00"),
        ):
            db.execute(
                "INSERT INTO sale_statuses (fixture_id, market_code, sale_state,"
                " single_eligible, observed_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    fixture,
                    market_code,
                    state,
                    1 if market_code else 0,
                    observed,
                    MOMENT,
                ),
            )
        db.execute(
            "INSERT INTO forecasts (fixture_id, track, model_version, issued_at,"
            " content_hash, payload) VALUES (?, 'ml', ?, ?, ?, ?)",
            (
                fixture,
                f"v{index}",
                f"2026-09-01T0{index}:00:00+00:00",
                f"hash-{index}",
                json.dumps({"m": index}),
            ),
        )
    db.commit()

    from goalx_backend.modelling.forecast import latest_forecast, latest_forecasts_bulk

    odds_bulk = fx.latest_odds_by_selection_bulk(db, ids, ["ttg", "crs"], "sporttery")
    sale_bulk = fx.latest_sale_status_asof_bulk(
        db, ids, "ttg", "2026-09-02T00:00:00+00:00"
    )
    forecasts = latest_forecasts_bulk(db, ids)
    for fixture in ids:
        for market in ("ttg", "crs"):
            assert odds_bulk[(fixture, market)] == fx.latest_odds_by_selection(
                db, fixture, market, "sporttery"
            )
        sale = sale_bulk[fixture]
        single = fx.latest_sale_status_asof(
            db, fixture, "ttg", "2026-09-02T00:00:00+00:00"
        )
        assert (sale["sale_state"], sale["single_eligible"]) == (
            single["sale_state"],
            single["single_eligible"],
        )
        assert (
            forecasts[fixture]["model_version"]
            == latest_forecast(db, fixture)["model_version"]
        )
    # 决胜细节：后 captured_at 胜出（首场价 2.5）+ 市场级 on_sale 胜比赛级
    assert odds_bulk[(ids[0], "ttg")]["0"][0] == pytest.approx(2.5)
    assert sale_bulk[ids[0]]["sale_state"] == "on_sale"


# --- D13：bankroll balance_after 2 位小数 ---


def test_bankroll_balance_after_two_decimals(db: sqlite3.Connection) -> None:
    """0.1+0.2 流水和必须 2 位舍入（裸浮点和 0.30000000000000004 渗出钉死）。"""
    bt.record_bankroll_event(db, "deposit", 0.1)
    bt.record_bankroll_event(db, "bet_payout", 0.2)
    rows = bt.list_bankroll_events(db)
    assert [row["balance_after"] for row in rows] == [0.3, 0.1]
    assert bt.bankroll_balance(db) == 0.3  # api/results.py 资金页读同一存储值
