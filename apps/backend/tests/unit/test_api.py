"""API 集成测试（隔离数据库，票 22/23/36 端点验收）。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend.config import Settings
from goalx_backend.data.fixtures import CST, beijing_business_date
from goalx_backend.db import connect, migrate
from goalx_backend.main import create_app

NOW = datetime.now(UTC)
KICKOFF = NOW + timedelta(hours=26)
BUSINESS_DATE = beijing_business_date(NOW)


def _payload() -> dict[str, object]:
    """动态时间竞彩载荷（开赛在未来、调盘新鲜，资格判定可复现）。"""
    kickoff_bj = KICKOFF.astimezone(CST)
    update_bj = (NOW - timedelta(seconds=60)).astimezone(CST)
    return {
        "errorCode": "0",
        "value": {
            "matchInfoList": [
                {
                    "businessDate": BUSINESS_DATE,
                    "subMatchList": [
                        {
                            "matchId": 1,
                            "matchNumStr": "周六001",
                            "leagueAbbName": "英超",
                            "homeTeamAllName": "阿森纳",
                            "awayTeamAllName": "切尔西",
                            "matchDate": kickoff_bj.date().isoformat(),
                            "matchTime": kickoff_bj.time().strftime("%H:%M:%S"),
                            "bettingSingle": 1,
                            "had": {
                                "a": "1.30",
                                "d": "5.00",
                                "h": "6.50",
                                "single": "1",
                                "updateDate": update_bj.date().isoformat(),
                                "updateTime": update_bj.time().strftime("%H:%M:%S"),
                            },
                        }
                    ],
                }
            ]
        },
    }


def _make_client(db_path: Path) -> TestClient:
    settings = Settings(db_path=db_path)
    return TestClient(create_app(settings=settings))


@pytest.fixture
def api_client(tmp_path: Path) -> Iterator[TestClient]:
    """绑定临时数据库的应用客户端（含预置竞彩场次，动态时间）。"""
    db_path = tmp_path / "api-test.db"
    conn = connect(db_path)
    migrate(conn)
    from goalx_backend.data.ingest import sporttery

    sporttery.store_matches(conn, sporttery.parse_matches(_payload()))  # type: ignore[arg-type]
    conn.close()
    with _make_client(db_path) as client:
        yield client


@pytest.fixture
def demo_client(tmp_path: Path) -> Iterator[TestClient]:
    """demo 种子客户端：三场（单固在售/非单固在售/停售）带完整欧赔证据。"""
    db_path = tmp_path / "demo-test.db"
    conn = connect(db_path)
    migrate(conn)
    from goalx_backend.data.ingest.demo import seed_demo

    seed_demo(conn)
    conn.close()
    with _make_client(db_path) as client:
        yield client


def _fixture_id(api_client: TestClient) -> int:
    today = api_client.get("/api/v1/fixtures/today", params={"date": BUSINESS_DATE})
    assert today.status_code == 200
    return int(today.json()[0]["fixture_id"])


def _demo_ids(demo_client: TestClient) -> list[int]:
    today = demo_client.get("/api/v1/fixtures/today")
    assert today.status_code == 200
    return [int(row["fixture_id"]) for row in today.json()]


def test_today_view_shape(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/fixtures/today", params={"date": BUSINESS_DATE})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["match_code"] == "周六001"
    assert entry["jc_odds"] == {"h": 6.5, "d": 5.0, "a": 1.3}
    assert entry["joined"] is False
    assert entry["eu_prob"] is None
    assert "not_joined" in entry["flags"]
    # 资格摘要（票 36）：欧赔缺失 → 未知但不拒绝；单固资格来自销售状态
    assert entry["had_quote"]["status"] == "unknown"
    assert "eu_no_quote" in entry["had_quote"]["reasons"]
    assert entry["had_quote"]["single_eligible"] is True
    assert entry["had_quote"]["sale_state"] == "on_sale"


def test_today_other_business_date_empty(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/fixtures/today", params={"date": "2020-01-01"})
    assert response.status_code == 200
    assert response.json() == []


def test_today_had_quote_summary_from_demo(demo_client: TestClient) -> None:
    body = demo_client.get("/api/v1/fixtures/today").json()
    assert len(body) == 3
    by_code = {row["match_code"]: row for row in body}
    assert by_code["周六001"]["had_quote"]["status"] == "valid"
    assert by_code["周六001"]["had_quote"]["single_eligible"] is True
    assert by_code["周六002"]["had_quote"]["single_eligible"] is False
    assert by_code["周六003"]["had_quote"]["status"] == "rejected"
    assert "sale_stopped" in by_code["周六003"]["had_quote"]["reasons"]


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
            "strategy_version": "manual-v1",
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
    assert bet["strategy_version"] == "manual-v1"
    assert bet["review"]["forward"] == "excluded_unlocked"

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


def test_bet_creation_eligibility_rules(demo_client: TestClient) -> None:
    """票 36 选择入口：单固校验、串关共同可购买、停售/开赛拒绝（服务端）。"""
    single_ok, parlay_ok, stopped = _demo_ids(demo_client)

    created = demo_client.post(
        "/api/v1/bets",
        json=_had_leg_payload(single_ok, "h"),
    )
    assert created.status_code == 201

    not_single = demo_client.post(
        "/api/v1/bets",
        json=_had_leg_payload(parlay_ok, "h"),
    )
    assert not_single.status_code == 400
    assert "单固" in not_single.json()["detail"]

    parlay = demo_client.post(
        "/api/v1/bets",
        json={
            "mode": "paper",
            "stake": 2.0,
            "legs": [
                {
                    "fixture_id": single_ok,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 6.5,
                },
                {
                    "fixture_id": parlay_ok,
                    "market_code": "had",
                    "selection_code": "a",
                    "locked_odds": 2.2,
                },
            ],
        },
    )
    assert parlay.status_code == 201  # 两腿同一 as_of 判定，共同可购买

    stopped_leg = demo_client.post(
        "/api/v1/bets",
        json={
            "mode": "paper",
            "stake": 2.0,
            "legs": [
                {
                    "fixture_id": single_ok,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 6.5,
                },
                {
                    "fixture_id": stopped,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 2.0,
                },
            ],
        },
    )
    assert stopped_leg.status_code == 400
    assert "sale_stopped" in stopped_leg.json()["detail"]


def _had_leg_payload(fixture_id: int, selection: str) -> dict[str, object]:
    return {
        "mode": "paper",
        "stake": 100.0,
        "legs": [
            {
                "fixture_id": fixture_id,
                "market_code": "had",
                "selection_code": selection,
                "locked_odds": 6.5,
            }
        ],
    }


def test_slip_record_purchase_flow(api_client: TestClient) -> None:
    fixture_id = _fixture_id(api_client)
    bet_id = api_client.post(
        "/api/v1/bets", json=_had_leg_payload(fixture_id, "h")
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
    assert bets[0]["locked_at"] is not None
    assert (
        bets[0]["review"]["forward"] == "missing_closing"
    )  # 无 closing 证据, 诚实缺失

    missing = api_client.post("/api/v1/bet-slips", json={"bet_ids": [999]})
    assert missing.status_code == 404


def test_lock_revalidates_sale_stop(tmp_path: Path) -> None:
    """票 36 验收 2：提交时服务端重新校验停售，不仅禁用按钮。"""
    from goalx_backend.data import fixtures as fx_store
    from goalx_backend.data.ingest.demo import seed_demo
    from goalx_backend.models import SaleStatusInput

    db_path = tmp_path / "stopped.db"
    conn = connect(db_path)
    migrate(conn)
    fixture_id = int(seed_demo(conn)["fixture_ids"][0])  # type: ignore[index]
    conn.close()
    with _make_client(db_path) as client:
        paper_id = client.post(
            "/api/v1/bets", json=_had_leg_payload(fixture_id, "h")
        ).json()["id"]
        live_id = client.post(
            "/api/v1/bets",
            json={**_had_leg_payload(fixture_id, "h"), "mode": "live"},
        ).json()["id"]
        # 销售状态追加停售后, paper 锁定被服务器拒绝
        conn = connect(db_path)
        fx_store.append_sale_status(
            conn,
            SaleStatusInput(
                fixture_id=fixture_id,
                market_code="had",
                sale_state="stopped",
                observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        conn.close()
        locked = client.post("/api/v1/bet-slips", json={"bet_ids": [paper_id]})
        assert locked.status_code == 400
        assert "sale_stopped" in locked.json()["detail"]
        assert client.get("/api/v1/bets").json()[0]["purchased"] is False
        # live 回录是事后记账: 停售/开赛后仍可入账, 由前瞻资格排除(交接契约)
        recorded = client.post("/api/v1/bet-slips", json={"bet_ids": [live_id]})
        assert recorded.status_code == 201
        live_bets = [
            bet for bet in client.get("/api/v1/bets").json() if bet["id"] == live_id
        ]
        assert live_bets[0]["purchased"] is True


def test_draw_results_import_and_settlement_loop(api_client: TestClient) -> None:
    fixture_id = _fixture_id(api_client)
    bet_id = api_client.post(
        "/api/v1/bets", json=_had_leg_payload(fixture_id, "h")
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


def test_draw_result_preview_is_readonly(api_client: TestClient) -> None:
    """票 36：更正影响预览（当前 vs 投影、差值），预览本身不改状态。"""
    fixture_id = _fixture_id(api_client)
    bet_id = api_client.post(
        "/api/v1/bets", json=_had_leg_payload(fixture_id, "h")
    ).json()["id"]
    api_client.post("/api/v1/bet-slips", json={"bet_ids": [bet_id]})
    api_client.post(
        "/api/v1/draw-results",
        json={
            "results": [{"fixture_id": fixture_id, "home_goals": 3, "away_goals": 1}]
        },
    )
    api_client.post("/api/v1/settlements/run")

    preview = api_client.post(
        "/api/v1/draw-results/preview",
        json={
            "results": [{"fixture_id": fixture_id, "home_goals": 0, "away_goals": 1}]
        },
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["results"][0]["is_correction"] is True
    assert body["results"][0]["previous"]["home_goals"] == 3
    affected = body["affected_bets"][0]
    assert affected["bet_id"] == bet_id
    assert affected["status_current"] == "won"
    assert affected["payout_current"] == 650.0
    assert affected["status_projected"] == "lost"
    assert affected["delta_payout"] == -650.0

    # 只读：预览后状态不变
    bets = api_client.get("/api/v1/bets").json()
    assert bets[0]["status"] == "won"
    assert bets[0]["payout"] == 650.0
    assert (
        api_client.post(
            "/api/v1/draw-results/preview",
            json={"results": [{"fixture_id": 999, "home_goals": 1, "away_goals": 0}]},
        ).status_code
        == 404
    )


def test_live_recording_with_actual_terms(demo_client: TestClient) -> None:
    """票 36 真实回录：实际条款结算、建议快照保留、账务一致。"""
    single_ok, _, _ = _demo_ids(demo_client)
    bet_id = demo_client.post(
        "/api/v1/bets",
        json={
            "mode": "live",
            "stake": 10,
            "legs": [
                {
                    "fixture_id": single_ok,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 2,
                }
            ],
        },
    ).json()["id"]
    slip = demo_client.post(
        "/api/v1/bet-slips",
        json={
            "bet_ids": [bet_id],
            "actuals": {
                str(bet_id): {
                    "stake": 12,
                    "leg_odds": [{"fixture_id": single_ok, "odds": 1.9}],
                }
            },
        },
    )
    assert slip.status_code == 201
    bank = demo_client.get("/api/v1/bankroll").json()
    assert bank["balance"] == -12  # 按实际金额扣款
    demo_client.post(
        "/api/v1/draw-results",
        json={"results": [{"fixture_id": single_ok, "home_goals": 2, "away_goals": 0}]},
    )
    demo_client.post("/api/v1/settlements/run")
    bet = demo_client.get("/api/v1/bets").json()[0]
    assert bet["status"] == "won"
    assert bet["payout"] == pytest.approx(22.8)  # 12 × 1.9（实际条款）
    assert bet["profit"] == pytest.approx(10.8)
    assert bet["actual_stake"] == 12
    assert bet["legs"][0]["locked_odds"] == 2.0  # 建议快照保留
    assert bet["legs"][0]["actual_odds"] == 1.9
    assert demo_client.get("/api/v1/bankroll").json()["balance"] == pytest.approx(10.8)
    assert bet["review"]["forward"] == "live_separate"  # live 单独分组


def test_cost_summary_endpoint(tmp_path: Path) -> None:
    db_path = tmp_path / "costs.db"
    conn = connect(db_path)
    migrate(conn)
    from goalx_backend.data import results as rs_store

    rs_store.record_cost(conn, "odds_api_credit", units=4, amount_cny=0, note="credits")
    rs_store.record_cost(conn, "odds_api_credit", units=1, amount_cny=0)
    rs_store.record_cost(conn, "llm_api", units=2, amount_cny=1.5)
    conn.commit()
    conn.close()
    with _make_client(db_path) as client:
        body = client.get("/api/v1/costs/summary").json()
        assert body["total_cny"] == 1.5
        assert body["credits_used"] == 5
        assert {item["category"] for item in body["items"]} == {
            "odds_api_credit",
            "llm_api",
        }
        # 空库诚实显示
        empty_path = tmp_path / "empty.db"
        conn = connect(empty_path)
        migrate(conn)
        conn.close()
        with _make_client(empty_path) as empty:
            empty_body = empty.get("/api/v1/costs/summary").json()
            assert empty_body["total_cny"] == 0
            assert empty_body["credits_used"] == 0
            assert empty_body["items"] == []


def test_bankroll_endpoint(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/bankroll")
    assert response.status_code == 200
    assert response.json() == {"balance": None, "events": []}


def test_create_deposit_endpoint(api_client: TestClient) -> None:
    """票 20 入金端点：落 deposit 流水、返回事件与新余额、GET bankroll 一致。"""
    response = api_client.post(
        "/api/v1/bankroll/deposits",
        json={"amount_cny": 5000.0, "note": "初始资金"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["event"]["kind"] == "deposit"
    assert body["event"]["amount_cny"] == 5000.0
    assert body["event"]["balance_after"] == 5000.0
    assert body["event"]["bet_id"] is None
    assert body["event"]["note"] == "初始资金"
    assert body["event"]["occurred_at"]  # 缺省取服务器当前时间
    assert body["balance"] == 5000.0

    second = api_client.post(
        "/api/v1/bankroll/deposits",
        json={
            "amount_cny": 250.5,
            "occurred_at": "2026-09-01T00:00:00+00:00",
        },
    )
    assert second.status_code == 201
    assert second.json()["balance"] == 5250.5
    assert second.json()["event"]["occurred_at"] == "2026-09-01T00:00:00+00:00"

    bank = api_client.get("/api/v1/bankroll").json()
    assert bank["balance"] == 5250.5
    assert [event["kind"] for event in bank["events"]] == ["deposit", "deposit"]


def test_create_deposit_rejects_non_positive_amount(api_client: TestClient) -> None:
    """金额非法(0/负数)在载荷校验被拒(422)，不落任何流水。"""
    for bad_amount in (0, -100.0):
        response = api_client.post(
            "/api/v1/bankroll/deposits", json={"amount_cny": bad_amount}
        )
        assert response.status_code == 422
    assert api_client.get("/api/v1/bankroll").json() == {
        "balance": None,
        "events": [],
    }


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
        "/api/v1/fixtures/today", params={"date": BUSINESS_DATE}
    ).json()
    assert today[0]["joined"] is True
    assert (
        api_client.post(
            "/api/v1/fixtures/999/join",
            json={"event_id": "x", "sport_key": "y"},
        ).status_code
        == 404
    )


def test_live_purchase_correction_and_counterfactual_exclusion(
    api_client: TestClient,
) -> None:
    fixture = _fixture_id(api_client)
    draft = {
        "mode": "live",
        "stake": 10,
        "legs": [
            {
                "fixture_id": fixture,
                "market_code": "had",
                "selection_code": "h",
                "locked_odds": 2,
            }
        ],
    }
    purchased = api_client.post("/api/v1/bets", json=draft).json()["id"]
    api_client.post("/api/v1/bets", json=draft)  # unpurchased counterfactual
    assert (
        api_client.post(
            "/api/v1/bet-slips", json={"bet_ids": [purchased, purchased]}
        ).status_code
        == 400
    )
    assert api_client.get("/api/v1/bankroll").json()["events"] == []
    assert (
        api_client.post("/api/v1/bet-slips", json={"bet_ids": [purchased]}).status_code
        == 201
    )
    assert (
        api_client.post("/api/v1/bet-slips", json={"bet_ids": [purchased]}).status_code
        == 400
    )
    assert api_client.get("/api/v1/bankroll").json()["balance"] == -10
    result = {"fixture_id": fixture, "home_goals": 2, "away_goals": 0}
    assert (
        api_client.post("/api/v1/draw-results", json={"results": [result]}).status_code
        == 201
    )
    assert api_client.post("/api/v1/settlements/run").status_code == 200
    assert api_client.get("/api/v1/bankroll").json()["balance"] == 10
    progress = api_client.get("/api/v1/validation/progress").json()
    assert progress["live"]["unique_bets"] == 1  # 真实购买单独分组(票 34)
    assert progress["paper"]["unique_bets"] == 0
    assert progress["yield_curve"] == []  # 默认曲线只含纸面,不混 live(票 34)
    live_curve = api_client.get(
        "/api/v1/validation/progress", params={"yield_mode": "live"}
    ).json()
    assert len(live_curve["yield_curve"]) == 1
    correction = {**result, "home_goals": 0, "away_goals": 2}
    assert (
        api_client.post(
            "/api/v1/draw-results", json={"results": [correction]}
        ).status_code
        == 400
    )
    assert api_client.get("/api/v1/bankroll").json()["balance"] == 10
    correction["correction_reason"] = "official corrected result"
    for _ in range(2):
        assert (
            api_client.post(
                "/api/v1/draw-results", json={"results": [correction]}
            ).status_code
            == 201
        )
    bank = api_client.get("/api/v1/bankroll").json()
    assert bank["balance"] == -10
    assert len(bank["events"]) == 3
    assert bank["events"][0]["note"].startswith("draw_result_revisions:")


def test_live_pool_rejected_and_invalid_bet_rolled_back(api_client: TestClient) -> None:
    response = api_client.post(
        "/api/v1/pool-slips",
        json={"mode": "live", "picks": [{"match_seq": 1, "selection_code": "3"}]},
    )
    assert response.status_code == 400
    assert api_client.get("/api/v1/bet-slips").json() == []
    response = api_client.post(
        "/api/v1/bets",
        json={
            "mode": "live",
            "stake": 10,
            "legs": [
                {
                    "fixture_id": 999,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 2,
                }
            ],
        },
    )
    assert response.status_code == 400
    assert api_client.get("/api/v1/bets").json() == []
