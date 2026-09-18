"""注级 EV 概率快照测试（票 41：双口径双存、口径外/存量注空、API 形状与 CLV 联结）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend import odds_math as om
from goalx_backend.betting import store as bt_store
from goalx_backend.betting.bets import BetDraft, create_bet_with_legs
from goalx_backend.betting.snapshot import build_ev_snapshot
from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.db import connect, migrate
from goalx_backend.evaluation import clv
from goalx_backend.main import create_app
from goalx_backend.modelling.forecast import (
    content_hash,
    insert_forecast,
    latest_forecast_asof,
)
from goalx_backend.modelling.score_matrix import ScoreMatrix, matrix_from_lambdas
from goalx_backend.models import (
    BetMode,
    LegInput,
    MarketKind,
    SnapshotInput,
    SnapshotPurpose,
    Tier,
)


def _model_had(lambdas: tuple[float, float]) -> dict[str, float]:
    """测试侧模型三向概率（与 Forecast payload 相同的矩阵构造）。"""
    lam_home, lam_away = lambdas
    matrix = ScoreMatrix(
        matrix_from_lambdas(lam_home, lam_away),
        lam_home=lam_home,
        lam_away=lam_away,
    )
    return matrix.had()


AS_OF = "2026-09-12T19:00:00+00:00"
OBSERVED = "2026-09-12T18:59:00+00:00"
KICKOFF = "2026-09-12T22:00:00+00:00"
CLOSING_AT = "2026-09-12T21:45:00+00:00"


def _seed_fixture(
    db, tag: str, book_prices: dict[str, tuple[float, float, float]] | None
) -> int:
    """一场带可选欧赔三向快照的场次（观测时间在 AS_OF 前 1 分钟）。"""
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"快照主队{tag}")
    away = fx_store.upsert_team(db, f"快照客队{tag}")
    fixture = fx_store.upsert_fixture(db, competition, KICKOFF, home, away)
    for book, prices in (book_prices or {}).items():
        for sel, odds in zip(("h", "d", "a"), prices, strict=True):
            fx_store.insert_odds_snapshot(
                db,
                SnapshotInput(
                    fixture_id=fixture,
                    market_code="had",
                    selection_code=sel,
                    source=f"odds_api:{book}",
                    odds=odds,
                    captured_at=OBSERVED,
                    observed_at=OBSERVED,
                ),
            )
    db.commit()
    return fixture


def _seed_forecast(
    db, fixture_id: int, lambdas: tuple[float, float], *, issued_at: str
) -> None:
    """一条固定 λ 的矩阵 Forecast（老 payload 形状，无 had_probs 键）。"""
    matrix = matrix_from_lambdas(*lambdas)
    payload: dict[str, object] = {
        "matrix": [list(row) for row in matrix],
        "lambda_home": lambdas[0],
        "lambda_away": lambdas[1],
        "rho": 0.0,
        "model_as_of": issued_at,
    }
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="ml",
        model_version="dc-test",
        content_hash=content_hash(payload),
        payload=payload,
        issued_at=issued_at,
    )
    db.commit()


def _leg(fixture_id: int, selection: str, odds: float) -> LegInput:
    return LegInput(
        fixture_id=fixture_id,
        market_code="had",
        selection_code=selection,
        locked_odds=odds,
    )


def test_snapshot_dual_caliber_single_leg(db) -> None:
    """双存裁决落地：同一锁定腿同时落共识口径与模型口径（各自可缺）。"""
    fixture = _seed_fixture(
        db, "s1", {"pin": (6.0, 5.0, 1.30), "avg": (6.2, 4.9, 1.32)}
    )
    _seed_forecast(db, fixture, (1.4, 1.3), issued_at=OBSERVED)
    snap = build_ev_snapshot(db, [_leg(fixture, "h", 6.5)], as_of=AS_OF)
    consensus = om.shin_implied((6.1, 4.95, 1.31))
    model_had = _model_had((1.4, 1.3))
    assert snap.prob_consensus == pytest.approx(consensus[0], abs=1e-6)
    assert snap.ev_consensus == pytest.approx(consensus[0] * 6.5 - 1.0, abs=1e-6)
    assert snap.prob_model == pytest.approx(model_had["h"], abs=1e-6)
    assert snap.ev_model == pytest.approx(model_had["h"] * 6.5 - 1.0, abs=1e-6)
    assert snap.present is True


def test_snapshot_parlay_joint_probability(db) -> None:
    """串关快照：联合概率连乘（独立假设）× 联合锁定赔率 − 1。"""
    f1 = _seed_fixture(db, "p1", {"pin": (2.0, 3.5, 3.5)})
    f2 = _seed_fixture(db, "p2", {"pin": (2.4, 3.3, 2.9)})
    _seed_forecast(db, f2, (1.2, 1.1), issued_at=OBSERVED)
    snap = build_ev_snapshot(db, [_leg(f1, "h", 2.0), _leg(f2, "a", 2.9)], as_of=AS_OF)
    p1 = om.shin_implied((2.0, 3.5, 3.5))[0]
    p2 = om.shin_implied((2.4, 3.3, 2.9))[2]
    assert snap.prob_consensus == pytest.approx(p1 * p2, abs=1e-6)
    assert snap.ev_consensus == pytest.approx(p1 * p2 * 2.0 * 2.9 - 1.0, abs=1e-6)
    # f1 无 Forecast → 模型口径整注缺失（不部分落值；f2 的模型概率不单独上浮）
    assert snap.prob_model is None
    assert snap.ev_model is None


def test_snapshot_missing_sources_stay_null(db) -> None:
    """无欧赔无 Forecast → 空快照；仅有 Forecast → 共识口径缺失、模型口径可算。"""
    bare = _seed_fixture(db, "n1", None)
    empty = build_ev_snapshot(db, [_leg(bare, "h", 2.0)], as_of=AS_OF)
    assert empty.present is False
    assert (empty.prob_consensus, empty.ev_consensus) == (None, None)
    assert (empty.prob_model, empty.ev_model) == (None, None)

    forecast_only = _seed_fixture(db, "n2", None)
    _seed_forecast(db, forecast_only, (1.0, 1.0), issued_at=OBSERVED)
    snap = build_ev_snapshot(db, [_leg(forecast_only, "h", 2.0)], as_of=AS_OF)
    assert snap.prob_consensus is None
    assert snap.ev_consensus is None
    had = _model_had((1.0, 1.0))
    assert snap.prob_model == pytest.approx(had["h"], abs=1e-6)


def test_snapshot_forecast_issued_after_as_of_excluded(db) -> None:
    """防时间泄漏：as_of 之后才发出的 Forecast 不进该时点快照。"""
    fixture = _seed_fixture(db, "t1", None)
    _seed_forecast(db, fixture, (1.0, 1.0), issued_at="2026-09-12T20:00:00+00:00")
    assert latest_forecast_asof(db, fixture, AS_OF) is None
    snap = build_ev_snapshot(db, [_leg(fixture, "h", 2.0)], as_of=AS_OF)
    assert snap.prob_model is None
    assert latest_forecast_asof(db, fixture, "2026-09-12T20:00:00+00:00") is not None


def test_snapshot_non_had_leg_out_of_scope(db) -> None:
    """非 had 腿（v1 可映射口径之外，与 CLV 同界）→ 整注无快照。"""
    fixture = _seed_fixture(db, "x1", {"pin": (2.0, 3.5, 3.5)})
    _seed_forecast(db, fixture, (1.0, 1.0), issued_at=OBSERVED)
    hhad = LegInput(
        fixture_id=fixture,
        market_code="hhad",
        selection_code="a",
        locked_odds=1.9,
        goal_line=-1.0,
    )
    snap = build_ev_snapshot(db, [hhad], as_of=AS_OF)
    assert snap.present is False
    mixed = build_ev_snapshot(db, [_leg(fixture, "h", 2.0), hhad], as_of=AS_OF)
    assert mixed.present is False


def test_create_bet_with_legs_persists_snapshot_and_legacy_default(db) -> None:
    """建注链路落快照：create_bet_with_legs 写四列；仓储不传 → 全 NULL（存量形状）。"""
    fixture = _seed_fixture(db, "c1", {"pin": (2.0, 3.5, 3.5)})
    expected = build_ev_snapshot(db, [_leg(fixture, "h", 2.0)], as_of=AS_OF)
    bet_id = create_bet_with_legs(
        db, BetDraft(mode=BetMode.PAPER, stake=10.0, legs=[_leg(fixture, "h", 2.0)])
    )
    row = db.execute(
        "SELECT snap_prob_consensus, snap_prob_model, snap_ev_consensus,"
        " snap_ev_model FROM bets WHERE id = ?",
        (bet_id,),
    ).fetchone()
    assert row["snap_prob_consensus"] == pytest.approx(expected.prob_consensus)
    assert row["snap_ev_consensus"] == pytest.approx(expected.ev_consensus)
    assert row["snap_prob_model"] is None
    assert row["snap_ev_model"] is None

    legacy = bt_store.create_bet(db, BetMode.PAPER, MarketKind.FIXED, 10.0)
    legacy_row = db.execute(
        "SELECT snap_prob_consensus, snap_ev_consensus FROM bets WHERE id = ?",
        (legacy,),
    ).fetchone()
    assert legacy_row["snap_prob_consensus"] is None
    assert legacy_row["snap_ev_consensus"] is None


def test_reconcile_clv_joins_snapshot_bets(db) -> None:
    """票 41 CLV 联结：结算后 reconcile 写 clv_records，bet_id 直连注级快照列。"""
    fixture = _seed_fixture(db, "v1", {"pin": (2.0, 3.5, 3.5)})
    bet_id = create_bet_with_legs(
        db, BetDraft(mode=BetMode.PAPER, stake=10.0, legs=[_leg(fixture, "h", 2.0)])
    )
    db.execute(
        "UPDATE bets SET purchased = 1, status = 'won',"
        " settled_at = '2026-09-12T23:00:00+00:00', profit = 10.0 WHERE id = ?",
        (bet_id,),
    )
    for book in ("pin", "avg"):
        for sel, odds in zip(("h", "d", "a"), (2.0, 3.5, 3.5), strict=True):
            fx_store.insert_odds_snapshot(
                db,
                SnapshotInput(
                    fixture_id=fixture,
                    market_code="had",
                    selection_code=sel,
                    source=f"odds_api:{book}",
                    odds=odds,
                    captured_at=CLOSING_AT,
                    purpose=SnapshotPurpose.CLOSING,
                ),
            )
    db.commit()
    stats = clv.reconcile_clv(db)
    assert stats.recorded == 1
    joined = db.execute(
        """
        SELECT c.close_prob, b.snap_prob_consensus, b.snap_ev_consensus
        FROM clv_records c JOIN bets b ON b.id = c.bet_id WHERE c.bet_id = ?
        """,
        (bet_id,),
    ).fetchone()
    assert joined is not None
    assert joined["close_prob"] is not None
    assert joined["snap_prob_consensus"] is not None
    assert joined["snap_ev_consensus"] is not None


@pytest.fixture
def demo_client(tmp_path: Path) -> Iterator[TestClient]:
    """demo 种子客户端：001 单固带三家欧赔无 Forecast，002 非单固带欧赔+Forecast。"""
    db_path = tmp_path / "bet-snapshot-api.db"
    conn = connect(db_path)
    migrate(conn)
    from goalx_backend.data.ingest.demo import seed_demo

    seed_demo(conn)
    conn.close()
    with TestClient(create_app(settings=Settings(db_path=db_path))) as client:
        yield client


def _demo_ids(demo_client: TestClient) -> list[int]:
    today = demo_client.get("/api/v1/fixtures/today")
    assert today.status_code == 200
    return [int(row["fixture_id"]) for row in today.json()]


def test_api_bet_view_ev_snapshot_dual_caliber(demo_client: TestClient) -> None:
    """BetView 快照字段：单关（001）共识口径可算、模型缺失 None。"""
    single_ok, _parlay_ok, _stopped = _demo_ids(demo_client)
    created = demo_client.post(
        "/api/v1/bets",
        json={
            "mode": "paper",
            "stake": 100.0,
            "legs": [
                {
                    "fixture_id": single_ok,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 6.5,
                }
            ],
        },
    )
    assert created.status_code == 201
    snapshot = created.json()["ev_snapshot"]
    # demo 001：pin/avg/bet365 主胜 6.0/6.2/6.4 → 共识 6.2；无 Forecast
    consensus = om.shin_implied((6.2, 4.9, 1.3))
    assert snapshot["prob_consensus"] == pytest.approx(consensus[0], abs=1e-6)
    assert snapshot["ev_consensus"] == pytest.approx(consensus[0] * 6.5 - 1.0, abs=1e-6)
    assert snapshot["prob_model"] is None
    assert snapshot["ev_model"] is None
    # 列表口径一致
    listing = demo_client.get("/api/v1/bets").json()
    assert listing[0]["ev_snapshot"] == snapshot


def test_api_bet_view_parlay_both_calibers(tmp_path: Path) -> None:
    """串关（001 h × 002 h）双口径齐备：001 补种 Forecast 后联合概率/联合赔率。"""
    db_path = tmp_path / "bet-snapshot-parlay.db"
    conn = connect(db_path)
    migrate(conn)
    from goalx_backend.data.ingest.demo import seed_demo

    seed_demo(conn)
    single_ok, parlay_ok, _stopped = [
        int(row["id"])
        for row in conn.execute("SELECT id FROM fixtures ORDER BY id").fetchall()
    ]
    # demo 001 无 Forecast（paper-loop 诚实占位依赖此状）；本用例单独补种，
    # 验证两腿齐备时模型口径按联合概率连乘。
    _seed_forecast(conn, single_ok, (1.1, 0.9), issued_at=OBSERVED)
    conn.close()
    with TestClient(create_app(settings=Settings(db_path=db_path))) as client:
        created = client.post(
            "/api/v1/bets",
            json={
                "mode": "paper",
                "stake": 2.0,
                "legs": [
                    {
                        "fixture_id": single_ok,
                        "market_code": "had",
                        "selection_code": "a",
                        "locked_odds": 1.3,
                    },
                    {
                        "fixture_id": parlay_ok,
                        "market_code": "had",
                        "selection_code": "h",
                        "locked_odds": 3.0,
                    },
                ],
            },
        )
    assert created.status_code == 201
    snapshot = created.json()["ev_snapshot"]
    p1 = om.shin_implied((6.2, 4.9, 1.3))[2]
    p2 = om.shin_implied((3.0, 3.4, 2.2))[0]
    assert snapshot["prob_consensus"] == pytest.approx(p1 * p2, abs=1e-6)
    assert snapshot["ev_consensus"] == pytest.approx(
        p1 * p2 * 1.3 * 3.0 - 1.0, abs=1e-6
    )
    model1 = _model_had((1.1, 0.9))
    model2 = _model_had((1.4, 1.3))
    assert snapshot["prob_model"] == pytest.approx(model1["a"] * model2["h"], abs=1e-6)
    assert snapshot["ev_model"] == pytest.approx(
        model1["a"] * model2["h"] * 3.9 - 1.0, abs=1e-6
    )


def test_api_bet_view_legacy_and_out_of_scope_null(demo_client: TestClient) -> None:
    """存量形状：SQL 直插旧注（无快照）与 hhad 腿注 → ev_snapshot 为 null。"""
    single_ok, _parlay_ok, _stopped = _demo_ids(demo_client)
    legacy = demo_client.post(
        "/api/v1/bets",
        json={
            "mode": "paper",
            "stake": 10.0,
            "legs": [
                {
                    "fixture_id": single_ok,
                    "market_code": "hhad",
                    "selection_code": "a",
                    "locked_odds": 1.9,
                    "goal_line": -1.0,
                }
            ],
        },
    )
    assert legacy.status_code == 201
    assert legacy.json()["ev_snapshot"] is None
    listing = demo_client.get("/api/v1/bets").json()
    assert all(bet["ev_snapshot"] is None for bet in listing)
