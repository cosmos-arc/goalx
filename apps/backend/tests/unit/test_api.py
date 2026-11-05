"""API 集成测试（隔离数据库，票 22/23/36 端点验收）。"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from goalx_backend import tasks
from goalx_backend.config import Settings
from goalx_backend.data.fixtures import CST, beijing_business_date
from goalx_backend.db import connect, migrate
from goalx_backend.main import create_app
from goalx_backend.modelling.forecast import content_hash, insert_forecast
from goalx_backend.modelling.score_matrix import matrix_from_lambdas

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
    # research 路径同指临时区（票 07②）：验证页回测读面不得在 CWD 落
    # data/goalx-research.db 跨 run 残留共享
    settings = Settings(
        db_path=db_path, research_db_path=db_path.parent / "research.db"
    )
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
    # 陈盘信号（票 48）：有竞彩快照 → 只报时长（无欧赔参考，drift 诚实缺省）
    assert entry["stale_line"] is not None
    assert entry["stale_line"]["minutes_since_move"] >= 0
    assert entry["stale_line"]["drift"] is None


def test_today_other_business_date_empty(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/fixtures/today", params={"date": "2020-01-01"})
    assert response.status_code == 200
    assert response.json() == []


def _payload_for(business_date: str, match_id: int, code: str) -> dict[str, object]:
    """指定业务日的单场竞彩载荷（票 wb-01 多日窗口测试）。"""
    payload = _payload()
    value = payload["value"]
    assert isinstance(value, dict)
    info_list = value["matchInfoList"]
    assert isinstance(info_list, list)
    info = info_list[0]
    assert isinstance(info, dict)
    info["businessDate"] = business_date
    sub = info["subMatchList"]
    assert isinstance(sub, list)
    match = sub[0]
    assert isinstance(match, dict)
    match["matchId"] = match_id
    match["matchNumStr"] = code
    return payload


def test_today_days_window_spans_business_dates(api_client: TestClient) -> None:
    # 次日再挂一场（开赛推后一天），days=2 应聚合两个业务日并按行标记归属
    db_path = api_client.app.state.settings.db_path  # type: ignore[attr-defined]
    conn = connect(db_path)
    from goalx_backend.data.ingest import sporttery

    next_date = (date.fromisoformat(BUSINESS_DATE) + timedelta(days=1)).isoformat()
    kickoff_bj = (NOW + timedelta(hours=50)).astimezone(CST)
    payload = _payload_for(next_date, 2, "周日001")
    value = payload["value"]
    assert isinstance(value, dict)
    info = value["matchInfoList"][0]
    sub_match = info["subMatchList"][0]
    assert isinstance(sub_match, dict)
    sub_match["matchDate"] = kickoff_bj.date().isoformat()
    sub_match["matchTime"] = kickoff_bj.time().strftime("%H:%M:%S")
    sporttery.store_matches(conn, sporttery.parse_matches(payload))  # type: ignore[arg-type]
    conn.close()

    window = api_client.get(
        "/api/v1/fixtures/today", params={"date": BUSINESS_DATE, "days": 2}
    )
    assert window.status_code == 200
    body = window.json()
    assert len(body) == 2
    assert {row["business_date"] for row in body} == {BUSINESS_DATE, next_date}
    # 单日（默认 days=1）不越界：仍只有当日一场
    single = api_client.get("/api/v1/fixtures/today", params={"date": BUSINESS_DATE})
    assert [row["business_date"] for row in single.json()] == [BUSINESS_DATE]


def test_today_days_param_validated(api_client: TestClient) -> None:
    assert (
        api_client.get("/api/v1/fixtures/today", params={"days": 0}).status_code == 422
    )
    assert (
        api_client.get("/api/v1/fixtures/today", params={"days": 8}).status_code == 422
    )


def test_today_had_quote_summary_from_demo(demo_client: TestClient) -> None:
    body = demo_client.get("/api/v1/fixtures/today").json()
    assert len(body) == 3
    by_code = {row["match_code"]: row for row in body}
    assert by_code["周六001"]["had_quote"]["status"] == "valid"
    assert by_code["周六001"]["had_quote"]["single_eligible"] is True
    assert by_code["周六002"]["had_quote"]["single_eligible"] is False
    assert by_code["周六003"]["had_quote"]["status"] == "rejected"
    assert "sale_stopped" in by_code["周六003"]["had_quote"]["reasons"]


# --- 票 39：共识分母护栏（low_confidence 标记，阈值 4 待人追认）---


def _add_eu_books(
    client: TestClient, fixture_id: int, books: int, *, first_index: int = 0
) -> None:
    """给场次补 N 本完整三向欧赔书（快照时间新鲜，共识分母 = N）。

    ``first_index`` 错开书名——同一测试内多次追加时不与既有书重名。
    """
    from goalx_backend.data import fixtures as fx_store
    from goalx_backend.models import SnapshotInput

    db_path = client.app.state.settings.db_path  # type: ignore[attr-defined]
    conn = connect(db_path)
    observed = (NOW - timedelta(seconds=60)).isoformat(timespec="seconds")
    for offset in range(books):
        index = first_index + offset
        for sel, odds in (("h", 6.1), ("d", 4.95), ("a", 1.29)):
            fx_store.insert_odds_snapshot(
                conn,
                SnapshotInput(
                    fixture_id=fixture_id,
                    market_code="had",
                    selection_code=sel,
                    source=f"odds_api:book{index}",
                    odds=odds + index * 0.01,
                    captured_at=observed,
                    observed_at=observed,
                    source_updated_at=observed,
                ),
            )
    conn.commit()
    conn.close()


def test_today_low_confidence_flag_below_threshold(api_client: TestClient) -> None:
    """分母 2/3：low_confidence 打标；2 同时命中 few_books（前端合并显示）。"""
    fixture_id = _fixture_id(api_client)
    for books in (2, 3):
        _add_eu_books(api_client, fixture_id, books)
        row = api_client.get(
            "/api/v1/fixtures/today", params={"date": BUSINESS_DATE}
        ).json()[0]
        assert row["books"] == books
        assert "low_confidence" in row["flags"]
        if books < 3:
            assert "few_books" in row["flags"]  # 两标并打（<3 区间被 <4 覆盖）
        else:
            assert "few_books" not in row["flags"]  # 3 = 仅低置信


def test_today_low_confidence_flag_at_and_above_threshold(
    api_client: TestClient,
) -> None:
    """分母 4（边界）与 5：不打标——阈值本身算足额。"""
    fixture_id = _fixture_id(api_client)
    for books in (4, 5):
        _add_eu_books(api_client, fixture_id, books)
        row = api_client.get(
            "/api/v1/fixtures/today", params={"date": BUSINESS_DATE}
        ).json()[0]
        assert row["books"] == books
        assert "low_confidence" not in row["flags"]
        assert "few_books" not in row["flags"]


def test_fixture_research_consensus_low_confidence(api_client: TestClient) -> None:
    """研究页共识视图：分母 <4 → low_confidence=True；≥4 → False（票 39 契约增量）。"""
    fixture_id = _fixture_id(api_client)
    _add_eu_books(api_client, fixture_id, 3)
    consensus = api_client.get(f"/api/v1/fixtures/{fixture_id}/research").json()[
        "consensus"
    ]
    assert consensus["books"] == 3
    assert consensus["low_confidence"] is True

    _add_eu_books(
        api_client, fixture_id, 1, first_index=3
    )  # 第 4 本 → 分母 4（边界不打标）
    consensus = api_client.get(f"/api/v1/fixtures/{fixture_id}/research").json()[
        "consensus"
    ]
    assert consensus["books"] == 4
    assert consensus["low_confidence"] is False


def test_fixture_odds_history_and_404(api_client: TestClient) -> None:
    fixture_id = _fixture_id(api_client)
    response = api_client.get(f"/api/v1/fixtures/{fixture_id}/odds")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3  # had: h/d/a
    assert {row["selection_code"] for row in body} == {"h", "d", "a"}
    assert api_client.get("/api/v1/fixtures/999/odds").status_code == 404


def _research_fixture_id(demo_client: TestClient) -> int:
    body = demo_client.get("/api/v1/fixtures/today").json()
    by_code = {row["match_code"]: row for row in body}
    return int(by_code["周六001"]["fixture_id"])


def test_fixture_research_books_consensus_and_eligibility(
    demo_client: TestClient,
) -> None:
    """研究页读模型（票 wb-02）：逐书 H/D/A + 捕获时间 + 去水共识 + 资格判定。"""
    fixture_id = _research_fixture_id(demo_client)
    response = demo_client.get(f"/api/v1/fixtures/{fixture_id}/research")
    assert response.status_code == 200
    body = response.json()
    assert body["fixture_id"] == fixture_id
    assert body["match_code"] == "周六001"
    assert body["home_team"] == "阿森纳"
    assert body["kickoff_utc"]
    assert body["business_date"] == beijing_business_date()
    # demo 种子：pin/avg/bet365 三本书（source 排序稳定）
    assert [book["book"] for book in body["books"]] == [
        "odds_api:avg",
        "odds_api:bet365",
        "odds_api:pin",
    ]
    pin = next(book for book in body["books"] if book["book"] == "odds_api:pin")
    assert pin["odds"] == {"h": 6.0, "d": 5.0, "a": 1.30}
    assert pin["captured_at"]  # 捕获时点随行
    # 共识 = 与列表页同口径（books=3，概率和≈1）；分母 3 <4 → 低置信（票 39）
    assert body["consensus"]["books"] == 3
    assert body["consensus"]["low_confidence"] is True
    probs = body["consensus"]["probability"]
    assert abs(sum(probs.values()) - 1.0) < 0.01
    # 无 Forecast 种子 → 模型区诚实为空（前端显示"暂无模型预测"）
    assert body["model"] is None
    # 资格判定内嵌（研究页不二次请求）
    assert body["had_quote"]["status"] == "valid"
    assert body["had_quote"]["single_eligible"] is True


def test_fixture_research_without_eu_books_is_honest(demo_client: TestClient) -> None:
    body = demo_client.get("/api/v1/fixtures/today").json()
    by_code = {row["match_code"]: row for row in body}
    fixture_id = int(by_code["周六003"]["fixture_id"])
    response = demo_client.get(f"/api/v1/fixtures/{fixture_id}/research")
    assert response.status_code == 200
    assert response.json()["books"] == []
    assert response.json()["consensus"] is None


def test_fixture_research_model_probability_and_ev(api_client: TestClient) -> None:
    """有 Forecast 时研究页带模型概率与模型 EV（模型概率 × 竞彩价 − 1）。"""
    fixture_id = _fixture_id(api_client)
    matrix = matrix_from_lambdas(1.8, 0.9)
    payload = {
        "matrix": [list(row) for row in matrix],
        "lambda_home": 1.8,
        "lambda_away": 0.9,
    }
    db_path = api_client.app.state.settings.db_path  # type: ignore[attr-defined]
    conn = connect(db_path)
    insert_forecast(
        conn,
        fixture_id=fixture_id,
        track="ml",
        model_version="dc-test",
        content_hash=content_hash(payload),
        payload=payload,
    )
    conn.commit()
    conn.close()

    body = api_client.get(f"/api/v1/fixtures/{fixture_id}/research").json()
    model = body["model"]
    assert model is not None
    assert model["model_version"] == "dc-test"
    assert model["issued_at"]
    had = model["probability"]
    assert abs(sum(had.values()) - 1.0) < 0.001  # 视图四舍五入后的概率和
    assert had["h"] > had["a"]  # λ_home=1.8 > λ_away=0.9 → 主胜概率更高
    # 模型 EV = 模型概率 × 竞彩价 − 1（种子竞彩 h=6.5 → 强正）
    assert model["ev"]["h"] == pytest.approx(had["h"] * 6.5 - 1, abs=1e-3)
    assert model["ev"]["d"] == pytest.approx(had["d"] * 5.0 - 1, abs=1e-3)
    assert model["ev"]["a"] == pytest.approx(had["a"] * 1.3 - 1, abs=1e-3)


def test_fixture_research_404(api_client: TestClient) -> None:
    assert api_client.get("/api/v1/fixtures/999/research").status_code == 404


# --- 票 wb-04：进球玩法页读模型（ttg/crs 报价 + 矩阵推导概率/EV）---


def _goals_by_code(demo_client: TestClient) -> dict[str, dict]:
    body = demo_client.get("/api/v1/markets/goals").json()
    assert isinstance(body, list)
    return {row["match_code"]: row for row in body}


def test_goals_market_rows_shape_and_odds(demo_client: TestClient) -> None:
    body = demo_client.get("/api/v1/markets/goals").json()
    assert len(body) == 3
    by_code = {row["match_code"]: row for row in body}
    # 场次身份与销售状态（比赛级 on_sale / stopped）
    assert by_code["周六001"]["home_team"] == "阿森纳"
    assert by_code["周六001"]["ttg"]["sale_state"] == "on_sale"
    assert by_code["周六001"]["ttg"]["single_eligible"] is True
    assert by_code["周六003"]["ttg"]["sale_state"] == "stopped"
    # 官方网格完整：ttg 八档、crs 31 档（含其他三档）
    first = by_code["周六001"]
    assert [sel["code"] for sel in first["ttg"]["selections"]] == [
        str(n) for n in range(8)
    ]
    assert len(first["crs"]["selections"]) == 31
    assert first["crs"]["selections"][-1]["code"] == "a_other"
    # 竞彩价随行：demo 002 ttg s2=4.50 / crs 1:1=6.0
    second = by_code["周六002"]
    ttg = {sel["code"]: sel for sel in second["ttg"]["selections"]}
    crs = {sel["code"]: sel for sel in second["crs"]["selections"]}
    assert ttg["2"]["odds"] == 4.5
    assert crs["1:1"]["odds"] == 6.0
    assert second["ttg"]["updated_at"]  # 调盘时点随行


def test_goals_market_model_probabilities_and_ev_caliber(
    demo_client: TestClient,
) -> None:
    """有 Forecast 的场次（demo 002）：矩阵推导概率 + EV=概率×竞彩价−1。"""
    row = _goals_by_code(demo_client)["周六002"]
    assert row["model_version"] == "dc-demo"
    assert row["issued_at"]
    ttg = {sel["code"]: sel for sel in row["ttg"]["selections"]}
    # 概率来自比分矩阵（λ 1.4/1.3）：Σ=1 且 s2 ≈ 0.245
    probs = [sel["probability"] for sel in row["ttg"]["selections"]]
    assert abs(sum(probs) - 1.0) < 0.01
    assert ttg["2"]["probability"] == pytest.approx(0.245, abs=1e-3)
    # EV 口径 = 模型概率 × 竞彩价 − 1（demo s2 价 4.50 → 正 EV）
    assert ttg["2"]["ev"] == pytest.approx(0.245 * 4.5 - 1, abs=1e-3)
    assert ttg["2"]["ev"] > 0
    assert ttg["3"]["ev"] == pytest.approx(0.2205 * 3.4 - 1, abs=1e-3)
    assert ttg["3"]["ev"] < 0
    # crs 与 ttg 出自同一矩阵：精确格 1:1 的 EV 同口径
    crs = {sel["code"]: sel for sel in row["crs"]["selections"]}
    assert crs["1:1"]["ev"] == pytest.approx(
        crs["1:1"]["probability"] * 6.0 - 1, abs=1e-4
    )


def test_goals_market_without_forecast_is_honest(demo_client: TestClient) -> None:
    """无 Forecast 的场次（demo 001/003）：概率/EV 与模型出处整体置空。"""
    row = _goals_by_code(demo_client)["周六001"]
    assert row["model_version"] is None
    assert row["issued_at"] is None
    ttg = row["ttg"]["selections"]
    assert all(sel["probability"] is None for sel in ttg)
    assert all(sel["ev"] is None for sel in ttg)
    assert all(sel["odds"] is not None for sel in ttg)  # 报价照常


def test_goals_market_days_param_validated(demo_client: TestClient) -> None:
    assert (
        demo_client.get("/api/v1/markets/goals", params={"days": 0}).status_code == 422
    )
    assert (
        demo_client.get("/api/v1/markets/goals", params={"days": 8}).status_code == 422
    )


def test_goals_market_other_business_date_empty(demo_client: TestClient) -> None:
    assert (
        demo_client.get("/api/v1/markets/goals", params={"date": "2020-01-01"}).json()
        == []
    )


def test_goals_market_fixture_without_goals_quotes(api_client: TestClient) -> None:
    """只采到 had 的场次也返回行：网格在、报价空、资格按比赛级回退诚实未知。"""
    body = api_client.get(
        "/api/v1/markets/goals", params={"date": BUSINESS_DATE}
    ).json()
    assert len(body) == 1
    row = body[0]
    assert len(row["ttg"]["selections"]) == 8
    assert all(sel["odds"] is None for sel in row["ttg"]["selections"])
    assert row["ttg"]["updated_at"] is None
    assert row["ttg"]["sale_state"] == "on_sale"  # 比赛级行回退
    assert row["ttg"]["single_eligible"] is None  # 无 poolList/市场块 → 未知


# ---- 建议仓位端点（票 wb-06）：纯计算、无库依赖，规则见 betting/staking ----


def test_stake_advice_paper_flat(demo_client: TestClient) -> None:
    """纸面一律 flat（红线）：注额=组合引擎同口径，不输出 Kelly。"""
    body = demo_client.post(
        "/api/v1/stake-advice",
        json={"mode": "paper", "bankroll": 10_000, "ev": 0.10, "odds": 2.0},
    ).json()
    assert body["tier"] == "flat"
    assert body["stake"] == pytest.approx(200.0)
    assert body["fraction"] == pytest.approx(0.02)
    assert body["full_kelly_fraction"] is None
    assert "红线" in body["reason"]


def test_stake_advice_live_quarter_kelly_and_cap(demo_client: TestClient) -> None:
    """真金 ¼Kelly + 单注上限截断；cap 参数分档（票 wb-07 三档复用）。"""
    body = demo_client.post(
        "/api/v1/stake-advice",
        json={"mode": "live", "bankroll": 10_000, "ev": 0.30, "odds": 1.5},
    ).json()
    assert body["tier"] == "quarter_kelly"
    assert body["full_kelly_fraction"] == pytest.approx(0.60)
    assert body["stake"] == pytest.approx(500.0)  # ¼×60%=15% → 截断 5%
    assert body["capped"] is True

    tight = demo_client.post(
        "/api/v1/stake-advice",
        json={
            "mode": "live",
            "bankroll": 10_000,
            "ev": 0.10,
            "odds": 2.0,
            "cap_fraction": 0.02,
        },
    ).json()
    assert tight["stake"] == pytest.approx(200.0)  # ¼×10%=2.5% → 截断 2%


def test_stake_advice_ev_non_positive_is_zero(demo_client: TestClient) -> None:
    """EV≤0 → ¥0 且理由显式（两模式一致）。"""
    body = demo_client.post(
        "/api/v1/stake-advice",
        json={"mode": "live", "bankroll": 10_000, "ev": -0.02, "odds": 2.0},
    ).json()
    assert body["stake"] == 0.0
    assert body["tier"] == "ev_non_positive"
    assert "¥0" in body["reason"]


def test_stake_advice_validates_inputs(demo_client: TestClient) -> None:
    """odds ≤ 1 / bankroll < 0 / cap 越界 → 422。"""
    assert (
        demo_client.post(
            "/api/v1/stake-advice",
            json={"mode": "paper", "bankroll": 100, "ev": 0.1, "odds": 1.0},
        ).status_code
        == 422
    )
    assert (
        demo_client.post(
            "/api/v1/stake-advice",
            json={"mode": "paper", "bankroll": -1, "ev": 0.1, "odds": 2.0},
        ).status_code
        == 422
    )
    assert (
        demo_client.post(
            "/api/v1/stake-advice",
            json={
                "mode": "live",
                "bankroll": 100,
                "ev": 0.1,
                "odds": 2.0,
                "cap_fraction": 0.10,
            },
        ).status_code
        == 422
    )


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


def test_bet_create_idempotent_client_request_id(demo_client: TestClient) -> None:
    """票 06/09：同 client_request_id 同意图重复提交回带既有注（201 同体），
    不落双行；意图指纹随行落库（腿序无关、价不计入）。"""
    single_ok, _parlay_ok, _stopped = _demo_ids(demo_client)
    body = _had_leg_payload(single_ok, "h") | {"client_request_id": "leg-abc-1"}
    first = demo_client.post("/api/v1/bets", json=body)
    assert first.status_code == 201
    assert first.json()["client_request_id"] == "leg-abc-1"
    replay = demo_client.post("/api/v1/bets", json=body)
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    # 同意图重提但腿价微变（价变确认后按新价重提同键）：价不在指纹内 → 仍回带
    price_drift = demo_client.post(
        "/api/v1/bets",
        json=body
        | {
            "legs": [
                {
                    "fixture_id": single_ok,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 6.4,
                }
            ]
        },
    )
    assert price_drift.status_code == 201
    assert price_drift.json()["id"] == first.json()["id"]
    matched = [
        bet
        for bet in demo_client.get("/api/v1/bets").json()
        if bet["client_request_id"] == "leg-abc-1"
    ]
    assert len(matched) == 1  # 同键不落双行
    # 不带键 = 旧行为：同内容照常第二注
    again = demo_client.post("/api/v1/bets", json=_had_leg_payload(single_ok, "h"))
    assert again.status_code == 201
    assert again.json()["id"] != first.json()["id"]


def test_bet_fingerprint_mixed_goal_line_no_crash(demo_client: TestClient) -> None:
    """票 09 correctness F1：同三元组双腿 goal_line 一 None 一 float 时
    指纹排序不得 TypeError 500——归输入校验域（同场串关 400）。"""
    single_ok, _parlay_ok, _stopped = _demo_ids(demo_client)
    response = demo_client.post(
        "/api/v1/bets",
        json={
            "mode": "paper",
            "stake": 2.0,
            "client_request_id": "leg-mixed-gl",
            "legs": [
                {
                    "fixture_id": single_ok,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 6.5,
                },
                {
                    "fixture_id": single_ok,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 6.5,
                    "goal_line": 2.5,
                },
            ],
        },
    )
    assert response.status_code == 400  # 同场串关校验先于指纹崩溃面
    assert "重复出现在串关中" in response.json()["detail"]


def test_bet_replay_intent_conflict_rejected(demo_client: TestClient) -> None:
    """票 09：同键异意图（改模式/改金额/换腿）→ 409 带原注摘要，不静默
    回旧、不落第二行——响应丢失后改单重提的反模式钉死。"""
    single_ok, _parlay_ok, stopped = _demo_ids(demo_client)
    body = _had_leg_payload(single_ok, "h") | {"client_request_id": "leg-conf-1"}
    first = demo_client.post("/api/v1/bets", json=body)
    assert first.status_code == 201
    # 改模式+改金额（报告二复现场景：paper/¥100 → live/¥200）
    conflict = demo_client.post(
        "/api/v1/bets",
        json=body | {"mode": "live", "stake": 200.0},
    )
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["existing_bet"] == {
        "id": first.json()["id"],
        "mode": "paper",
        "stake": 100.0,
    }
    # 换腿（不同业务意图）同样 409
    swapped = demo_client.post(
        "/api/v1/bets",
        json={
            "mode": "paper",
            "stake": 100.0,
            "client_request_id": "leg-conf-1",
            "legs": [
                {
                    "fixture_id": stopped,
                    "market_code": "had",
                    "selection_code": "h",
                    "locked_odds": 2.0,
                }
            ],
        },
    )
    assert swapped.status_code == 409
    assert swapped.json()["detail"]["existing_bet"]["id"] == first.json()["id"]
    # 换新键 = 新请求：按新意图正常另建（无双注、无吞改）
    separate = demo_client.post(
        "/api/v1/bets",
        json=body | {"mode": "live", "stake": 200.0, "client_request_id": "leg-conf-2"},
    )
    assert separate.status_code == 201
    assert separate.json()["id"] != first.json()["id"]
    rows = [
        bet
        for bet in demo_client.get("/api/v1/bets").json()
        if bet["client_request_id"] in {"leg-conf-1", "leg-conf-2"}
    ]
    assert len(rows) == 2  # 每键恰一行


def test_bet_replay_short_circuits_before_eligibility(demo_client: TestClient) -> None:
    """票 06 correctness F6b（票 09 收窄后）：同键**同意图**重放在资格校验
    之前短路——原决策已落库，腿事后已开赛不改变"该请求已建注"的事实。
    （换腿=异意图，见 test_bet_replay_intent_conflict_rejected 的 409。）"""
    single_ok, _parlay_ok, _stopped = _demo_ids(demo_client)
    body = _had_leg_payload(single_ok, "h") | {"client_request_id": "leg-replay-1"}
    created = demo_client.post("/api/v1/bets", json=body)
    assert created.status_code == 201
    # 事后开球：同载荷全新提交必 400（kickoff_passed），同意图重放 → 201 回带
    db_path = demo_client.app.state.settings.db_path  # type: ignore[attr-defined]
    conn = connect(db_path)
    with conn:
        conn.execute(
            "UPDATE fixtures SET kickoff_utc = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", single_ok),
        )
    conn.close()
    fresh = demo_client.post("/api/v1/bets", json=_had_leg_payload(single_ok, "h"))
    assert fresh.status_code == 400
    assert "kickoff_passed" in fresh.json()["detail"]
    replay = demo_client.post("/api/v1/bets", json=body)
    assert replay.status_code == 201
    assert replay.json()["id"] == created.json()["id"]


def test_no_candidate_attribution_endpoint(demo_client: TestClient) -> None:
    """票 07：六桶归因端点——demo 种子下停售场入脚注面、valid 无优势
    场入无优势桶、六桶恒在（空桶占位）+ 晨检同源覆盖数。"""
    response = demo_client.get("/api/v1/fixtures/today/attribution")
    assert response.status_code == 200
    payload = response.json()
    keys = [bucket["key"] for bucket in payload["buckets"]]
    assert keys == [
        "not_on_sale",
        "not_collected",
        "eu_cut",
        "evidence_unqualified",
        "no_edge",
        "missed_deadline",
    ]
    assert payload["window_closed"]["count"] == 1  # 周六003 停售
    assert payload["window_closed"]["reasons_seen"] == ["sale_stopped"]
    # demo 三场 EV 全负：001/002 valid → 无优势；候选面 0
    assert payload["buckets"][4]["count"] == 2
    assert payload["positive_candidates"]["count"] == 0
    assert payload["total"] == 3
    assert payload["board_coverage"] == [3, 0]  # 晨检第 8 项同源（demo 无 srct 链）
    assert "quote_evidence" in payload["caliber"]
    # 票 08 correctness F4：影子读数取请求日快照并带日期——未拍日 None
    assert payload["freshness_shadow"] is None
    assert payload["freshness_shadow_date"] is None


def test_attribution_marks_degraded_corpus_reader(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """票 07 correctness F2：corpus 读面缺席（锁占/库缺）→ corpus_reader=
    degraded 标记透传——eu_cut 桶可能混入读面缺席，对账先看本字段。"""
    from goalx_backend.data import corpus_duckdb

    def _unavailable(settings: object) -> None:
        raise OSError("corpus file missing")

    monkeypatch.setattr(corpus_duckdb, "connect", _unavailable)
    response = demo_client.get("/api/v1/fixtures/today/attribution")
    assert response.status_code == 200
    payload = response.json()
    assert payload["corpus_reader"] == "degraded"


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


def test_slip_sum_fields_rounded_to_cents(api_client: TestClient) -> None:
    """批A #19：SUM 聚合字段统一 round 到分——28.10+35.95+35.95 的浮点尾数
    （100.00000000000001）不外漏（stake/profit 此前只对 pool 字段 round）。"""
    fixture_id = _fixture_id(api_client)
    bet_ids = [
        api_client.post(
            "/api/v1/bets",
            json={
                "mode": "paper",
                "stake": stake,
                "legs": [
                    {
                        "fixture_id": fixture_id,
                        "market_code": "had",
                        "selection_code": selection,
                        "locked_odds": odds,
                    }
                ],
            },
        ).json()["id"]
        for selection, odds, stake in (
            ("h", 6.5, 28.10),
            ("d", 5.0, 35.95),
            ("a", 1.3, 35.95),
        )
    ]
    slip = api_client.post("/api/v1/bet-slips", json={"bet_ids": bet_ids})
    assert slip.status_code == 201
    assert slip.json()["stake_total"] == 100.0

    # 0:1 客胜：h/d 负、a 中（payout 35.95×1.3）——profit_total 非零面同走 round
    api_client.post(
        "/api/v1/draw-results",
        json={
            "results": [{"fixture_id": fixture_id, "home_goals": 0, "away_goals": 1}]
        },
    )
    api_client.post("/api/v1/settlements/run")
    listing = api_client.get("/api/v1/bet-slips").json()
    assert listing[0]["profit_total"] == round(
        -28.10 - 35.95 + round(35.95 * 1.3, 2) - 35.95, 2
    )


def test_slip_view_rounds_float_tails_on_all_sum_fields() -> None:
    """批A #19 单位面：SUM 浮点尾数（若出现）在视图层统一 round 到分——三字段同法。

    SQLite ≥3.43 SUM 用补偿求和，常规小 N 不产尾数（API 流程测试抓不到本守卫），
    故直接钉 _slip_view 变换本身。"""
    import sqlite3
    from typing import cast

    from goalx_backend.api.bets import _slip_view

    row = cast(
        "sqlite3.Row",
        {
            "id": 1,
            "mode": "paper",
            "placed_at": None,
            "note": None,
            "created_at": "2026-11-05T00:00:00+00:00",
            "bet_count": 3,
            "stake_total": 100.00000000000001,
            "profit_total": -100.00000000000001,
            "combination_count": 0,
            "pool_stake_total": 8.000000000000002,
            "quarantined_combinations": 0,
        },
    )
    view = _slip_view(row)
    assert view.stake_total == 100.0
    assert view.profit_total == -100.0
    assert view.pool_stake_total == 8.0


def test_paper_slip_with_actuals_rejected(api_client: TestClient) -> None:
    """票 01（报告一案例 1）：system_paper 6.5 锁票传 actual 99 → 400，
    票/绑定/actual 列零部分写入；99 无法以 system_paper 身份进价格链。"""
    fixture_id = _fixture_id(api_client)
    bet_id = api_client.post(
        "/api/v1/bets", json=_had_leg_payload(fixture_id, "h")
    ).json()["id"]
    rejected = api_client.post(
        "/api/v1/bet-slips",
        json={
            "bet_ids": [bet_id],
            "actuals": {
                str(bet_id): {
                    "stake": 2.0,
                    "leg_odds": [{"fixture_id": fixture_id, "odds": 99.0}],
                }
            },
        },
    )
    assert rejected.status_code == 400
    assert "实际条款" in rejected.json()["detail"]
    bet = api_client.get("/api/v1/bets").json()[0]
    assert bet["purchased"] is False
    assert bet["slip_id"] is None
    assert bet["actual_stake"] is None
    assert bet["legs"][0]["actual_odds"] is None


def test_actuals_non_integer_key_is_400_not_500(api_client: TestClient) -> None:
    """审计批B #6：actuals 非整数键 int() 的 ValueError 走 400 映射——
    此前推导式在 try 外，任何 DB 访问前裸 500。"""
    response = api_client.post(
        "/api/v1/bet-slips",
        json={
            "bet_ids": [1],
            "actuals": {"abc": {"stake": 10.0, "leg_odds": []}},
        },
    )
    assert response.status_code == 400
    assert "abc" in str(response.json()["detail"])


def test_leg_market_and_selection_closed_sets(api_client: TestClient) -> None:
    """审计批B #7：market/selection 按 markets 闭集拒收——闭集外市场结算
    hit=None → 注永远 open 每批重扫；闭集外选择在 had 下静默记输。"""
    fixture_id = _fixture_id(api_client)
    bad_market = api_client.post(
        "/api/v1/bets",
        json={
            "mode": "paper",
            "stake": 2.0,
            "legs": [
                {
                    "fixture_id": fixture_id,
                    "market_code": "xyz",
                    "selection_code": "h",
                    "locked_odds": 2.0,
                }
            ],
        },
    )
    assert bad_market.status_code == 422
    bad_selection = api_client.post(
        "/api/v1/bets",
        json={
            "mode": "paper",
            "stake": 2.0,
            "legs": [
                {
                    "fixture_id": fixture_id,
                    "market_code": "had",
                    "selection_code": "z",
                    "locked_odds": 2.0,
                }
            ],
        },
    )
    assert bad_selection.status_code == 422


def test_stake_money_fields_rounded_and_bounded(api_client: TestClient) -> None:
    """审计批B #5：四 Money 入口取整到分+上界 1e9——亚分级 stake 进台账
    后账面与流水（已 round）永久漂移；inf/巨值上界挡库外。"""
    fixture_id = _fixture_id(api_client)
    created = api_client.post(
        "/api/v1/bets",
        json={**_had_leg_payload(fixture_id, "h"), "stake": 10.006},
    )
    assert created.status_code == 201
    assert created.json()["stake"] == 10.01  # 入口取整，亚分不落库
    for bad_stake in (1e308, 0.001):  # 上界 + 取整后归零
        rejected = api_client.post(
            "/api/v1/bets",
            json={**_had_leg_payload(fixture_id, "h"), "stake": bad_stake},
        )
        assert rejected.status_code == 422
    deposit_rejected = api_client.post(
        "/api/v1/bankroll/deposits", json={"amount_cny": 1e308}
    )
    assert deposit_rejected.status_code == 422


def test_null_money_fields_are_422(api_client: TestClient) -> None:
    """批B correctness F1：null/非标量金额走 422 float_type——BeforeValidator
    里 float(None) 的裸 TypeError 不被 pydantic 转 422，曾回归成 500。"""
    fixture_id = _fixture_id(api_client)
    bet_null = api_client.post(
        "/api/v1/bets", json={**_had_leg_payload(fixture_id, "h"), "stake": None}
    )
    assert bet_null.status_code == 422
    slip_null = api_client.post(
        "/api/v1/bet-slips",
        json={
            "bet_ids": [1],
            "actuals": {"1": {"stake": None, "leg_odds": []}},
        },
    )
    assert slip_null.status_code == 422
    pool_null = api_client.post(
        "/api/v1/pool-slips/preview",
        json={
            "stake_per_combination": None,
            "picks": [{"match_seq": 1, "selection_code": "3"}],
        },
    )
    assert pool_null.status_code == 422
    deposit_null = api_client.post(
        "/api/v1/bankroll/deposits", json={"amount_cny": None}
    )
    assert deposit_null.status_code == 422


def test_timestamp_fields_reject_non_iso(api_client: TestClient) -> None:
    """审计批B #16：placed_at/occurred_at/published_at 收任意字符串 201 落
    TEXT——placed_at 坏值污染 decision_key 且在 clv 对账硬崩。"""
    fixture_id = _fixture_id(api_client)
    bet_id = api_client.post(
        "/api/v1/bets", json=_had_leg_payload(fixture_id, "h")
    ).json()["id"]
    bad_slip = api_client.post(
        "/api/v1/bet-slips",
        json={"bet_ids": [bet_id], "placed_at": "not-a-date"},
    )
    assert bad_slip.status_code == 422
    bad_deposit = api_client.post(
        "/api/v1/bankroll/deposits",
        json={"amount_cny": 100.0, "occurred_at": "garbage"},
    )
    assert bad_deposit.status_code == 422
    bad_draw = api_client.post(
        "/api/v1/draw-results",
        json={
            "results": [
                {
                    "fixture_id": fixture_id,
                    "home_goals": 1,
                    "away_goals": 0,
                    "published_at": "garbage",
                }
            ]
        },
    )
    assert bad_draw.status_code == 422


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
                {"match_seq": seq, "selection_code": "3", "fixture_id": fixture_id}
                for seq in range(1, 10)
            ]
            + [{"match_seq": 10, "selection_code": "1"}],  # 10 场单选 → 10 注
        },
    )
    assert response.status_code == 201
    slip = response.json()
    assert slip["mode"] == "paper"
    # 票 01 回读三方一致：组合数/池票金额直出（活口径）
    assert slip["combination_count"] == 10
    assert slip["pool_stake_total"] == 20.0
    assert slip["quarantined_combinations"] == 0
    combos = api_client.get("/api/v1/bet-slips")
    assert any(row["id"] == slip["id"] for row in combos.json())


def test_pool_slip_pick9_requires_nine_matches(api_client: TestClient) -> None:
    """票 01：任9 <9 场 = 400 整票拒绝（atomic 回滚）。"""
    response = api_client.post(
        "/api/v1/pool-slips",
        json={
            "mode": "paper",
            "picks": [
                {"match_seq": 1, "selection_code": "3"},
                {"match_seq": 2, "selection_code": "0"},
            ],
        },
    )
    assert response.status_code == 400
    assert "任9 需至少" in response.json()["detail"]
    assert api_client.get("/api/v1/bet-slips").json() == []


def test_pool_slip_unknown_fixture_rejected_not_500(api_client: TestClient) -> None:
    """票 08 预存注记收口：pick 带不存在 fixture_id → 400 明示（原裸 FK
    500）；预览/创建同校验。"""
    picks = [
        {"match_seq": seq, "selection_code": "3", "fixture_id": 999999}
        for seq in range(1, 10)
    ]
    for path in ("/api/v1/pool-slips/preview", "/api/v1/pool-slips"):
        body = {"mode": "paper", "picks": picks}
        if path.endswith("preview"):
            body = {"picks": picks}  # 预览载荷无 mode/期次
        response = api_client.post(path, json=body)
        assert response.status_code == 400, (path, response.text)
        assert "fixture" in response.json()["detail"]
    assert api_client.get("/api/v1/bet-slips").json() == []  # 无半写落库


@pytest.mark.parametrize(
    ("n", "count", "total"),
    [
        (9, 1, 2.0),  # 9 场单选 = 1 注 2 元
        (10, 10, 20.0),  # 10 场单选 = 10 注 20 元
        (14, 2002, 4004.0),  # 14 场单选 = 2002 注 4004 元
    ],
)
def test_pool_slip_preview_golden(
    api_client: TestClient, n: int, count: int, total: float
) -> None:
    """票 01 预览黄金样例：注数/总金额后端单源。"""
    response = api_client.post(
        "/api/v1/pool-slips/preview",
        json={
            "picks": [
                {"match_seq": seq, "selection_code": "3"} for seq in range(1, n + 1)
            ]
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["market_code"] == "pick9"
    assert body["combination_count"] == count
    assert body["stake_per_combination"] == 2.0
    assert body["total_stake"] == total
    assert len(body["combinations_sha256"]) == 64


def test_pool_slip_preview_multi_pick_and_under_nine(api_client: TestClient) -> None:
    """复式每场多选与 <9 场读数：10 场场 1 双选 = 19 注；8 场 = 0 注。"""
    multi = api_client.post(
        "/api/v1/pool-slips/preview",
        json={
            "picks": [{"match_seq": 1, "selection_code": sel} for sel in ("3", "1")]
            + [{"match_seq": seq, "selection_code": "3"} for seq in range(2, 11)]
        },
    )
    assert multi.status_code == 200
    assert multi.json()["combination_count"] == 19
    under = api_client.post(
        "/api/v1/pool-slips/preview",
        json={
            "picks": [{"match_seq": seq, "selection_code": "3"} for seq in range(1, 9)]
        },
    )
    assert under.status_code == 200
    assert under.json()["combination_count"] == 0
    assert under.json()["total_stake"] == 0.0


def test_pool_slip_preview_ttt14_incomplete_honest_zero(
    api_client: TestClient,
) -> None:
    """审计批C #9：ttt14 未满 14 场预览=诚实 0+空选择摘要——不再通用连乘
    算出"9 注真数"（创建侧 400"需 14 场"的落差）；与 pick9 <9 场同口径。"""
    from goalx_backend.betting import store as bt_store

    response = api_client.post(
        "/api/v1/pool-slips/preview",
        json={
            "market_code": "ttt14",
            "stake_per_combination": 2.0,
            "picks": [
                {"match_seq": seq, "selection_code": sel}
                for seq in (1, 2)
                for sel in ("3", "1", "0")
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["combination_count"] == 0
    assert body["total_stake"] == 0.0
    assert body["combinations_sha256"] == bt_store.EMPTY_SELECTIONS_SHA256
    # 同载荷创建侧依旧 400"需 14 场"（预览诚实 0 ≠ 可建）
    create = api_client.post(
        "/api/v1/pool-slips",
        json={
            "mode": "paper",
            "market_code": "ttt14",
            "picks": [{"match_seq": 1, "selection_code": "3"}],
        },
    )
    assert create.status_code == 400
    assert "14 场" in create.json()["detail"]


def test_pool_slip_preview_ttt14_cartesian(api_client: TestClient) -> None:
    """ttt14 玩法 = 整体连乘（14 场全单选 = 1 注；双选场放大 2 倍）。"""
    response = api_client.post(
        "/api/v1/pool-slips/preview",
        json={
            "market_code": "ttt14",
            "picks": [{"match_seq": seq, "selection_code": "3"} for seq in range(1, 15)]
            + [{"match_seq": 14, "selection_code": "1"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["market_code"] == "ttt14"
    assert response.json()["combination_count"] == 2
    assert response.json()["total_stake"] == 4.0


def test_pool_slip_preview_matches_readback_on_fractional_stake(
    api_client: TestClient,
) -> None:
    """票 01 三方一致（金额面）：预览 total_stake == 创建回读 pool_stake_total。"""
    picks = [{"match_seq": 1, "selection_code": sel} for sel in ("3", "1", "0")] + [
        {"match_seq": seq, "selection_code": "3"} for seq in range(2, 10)
    ]
    preview = api_client.post(
        "/api/v1/pool-slips/preview",
        json={"stake_per_combination": 0.1, "picks": picks},
    )
    assert preview.status_code == 200
    assert preview.json()["combination_count"] == 3
    created = api_client.post(
        "/api/v1/pool-slips",
        json={"mode": "paper", "stake_per_combination": 0.1, "picks": picks},
    )
    assert created.status_code == 201
    assert created.json()["pool_stake_total"] == preview.json()["total_stake"]


def test_pool_slip_preview_over_limit_rejected(api_client: TestClient) -> None:
    """票 01+07④：预览超限同创建口径 422（拒绝不截断，人裁决）。"""
    response = api_client.post(
        "/api/v1/pool-slips/preview",
        json={
            "picks": [
                {"match_seq": seq, "selection_code": sel}
                for seq in range(1, 15)  # C(14,9)×2^9 ≈ 102.5 万 > 10 万
                for sel in ("3", "1")
            ]
        },
    )
    assert response.status_code == 422
    assert "超上限" in str(response.json()["detail"])


def test_pool_slip_over_combination_limit_rejected(api_client: TestClient) -> None:
    """票 07④：超上限复式组合 422 拒绝，不落半截票。"""
    response = api_client.post(
        "/api/v1/pool-slips",
        json={
            "mode": "paper",
            "picks": [
                {"match_seq": seq, "selection_code": sel}
                for seq in range(1, 15)  # 14 场 × 3 向 = 478 万 > 10 万
                for sel in ("3", "1", "0")
            ],
        },
    )
    assert response.status_code == 422
    assert "超上限" in response.json()["detail"]
    # 回滚干净：票不落库
    assert api_client.get("/api/v1/bet-slips").json() == []


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


# ---- 赛果自动同步端点（票 42：触发 + 状态；HTTP 层 MockTransport，不打外网） ----

FIXTURE_PAGE_0916 = (
    Path(__file__).parent.parent / "fixtures" / "caiguo_2026-09-16.html.txt"
).read_text(encoding="utf-8")


def _sync_transport() -> httpx.MockTransport:
    """官方 uniform 同步源传输层：200 给 2026-09-16 赛果页。"""

    def handler(request: httpx.Request) -> httpx.Response:
        rows = [
            {
                "matchId": 99001,
                "matchNumStr": "周三001",
                "matchDate": "2026-09-16",
                "leagueName": "亚运会男足",
                "leagueId": 83,
                "matchResultStatus": "2",
                "poolStatus": "Payout",
                "sectionsNo999": "2:1",
                "sectionsNo1": "1:1",
                "winFlag": "H",
            },
            {
                "matchId": 99002,
                "matchNumStr": "周三014",
                "matchDate": "2026-09-16",
                "leagueName": "亚运会男足",
                "leagueId": 83,
                "matchResultStatus": "1",
                "poolStatus": "Close",
                "sectionsNo999": "",
                "sectionsNo1": "",
                "winFlag": "",
            },
        ]
        value = {"matchResult": rows, "pages": 1, "total": 2}
        return httpx.Response(
            200, json={"success": True, "errorCode": "0", "value": value}
        )

    return httpx.MockTransport(handler)


def _seed_pending_result_fixture(db_path: Path) -> None:
    """种一场已开赛、无开奖的竞彩场次（business_date 对齐 fixture 页 2026-09-16）。"""
    conn = connect(db_path)
    migrate(conn)
    from goalx_backend.data.fixtures import (
        upsert_competition,
        upsert_fixture,
        upsert_match_code,
        upsert_team,
    )
    from goalx_backend.models import MatchCodeInput

    competition = upsert_competition(conn, "亚运男足")
    home = upsert_team(conn, "中国亚运")
    away = upsert_team(conn, "朝鲜亚运")
    fixture_id = upsert_fixture(
        conn, competition, (NOW - timedelta(hours=2)).isoformat(), home, away
    )
    upsert_match_code(
        conn,
        MatchCodeInput(
            fixture_id=fixture_id,
            kind="jingcai",
            business_date="2026-09-16",
            code="周三001",
            source_match_id="99001",
        ),
    )
    conn.commit()
    conn.close()


def test_draw_sync_status_never_run(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/draw-sync/status")
    assert response.status_code == 200
    assert response.json() == {"last_run": None, "pending_results": 0}


def test_draw_sync_run_delegates_to_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST run 委派 tasks.draw_results_sync：触发即触发，router 不自带实现。"""
    db_path = tmp_path / "draw-delegate.db"
    _seed_pending_result_fixture(db_path)
    calls: list[None] = []
    monkeypatch.setattr(tasks, "draw_results_sync", lambda: calls.append(None) or {})
    with _make_client(db_path) as client:
        run = client.post("/api/v1/draw-sync/run")
        assert run.status_code == 200
        assert len(calls) == 1
        # 假任务不落行 → 视图读库仍是空同步史；待出数仍由库面计算
        assert run.json() == {"last_run": None, "pending_results": 1}


def test_draw_results_sync_chain_uniform_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tasks.draw_results_sync 全链（语料桥缺席 → 映射/源T 段降级，uniform 独走）。"""
    db_path = tmp_path / "draw-chain.db"
    _seed_pending_result_fixture(db_path)
    monkeypatch.setenv("GOALX_DB_PATH", str(db_path))
    monkeypatch.setenv("GOALX_CORPUS_ROOT", str(tmp_path / "no-corpus"))
    monkeypatch.setattr(
        tasks, "polite_client", lambda: httpx.Client(transport=_sync_transport())
    )
    stats = tasks.draw_results_sync()
    assert stats["links"] is None  # 语料桥缺席 → 映射段降级跳过
    assert stats["srct"]["degraded"] == "duck_con unavailable"
    assert stats["uniform"]["imported"] == 1
    with _make_client(db_path) as client:
        listing = client.get("/api/v1/draw-results").json()
        assert listing[0]["source"] == "sporttery.cn"
        assert (listing[0]["home_goals"], listing[0]["away_goals"]) == (2, 1)
        status = client.get("/api/v1/draw-sync/status").json()
        assert status["pending_results"] == 0
        assert status["last_run"]["source"] == "sporttery.cn"
        assert status["last_run"]["imported"] == 1
        assert status["last_run"]["business_dates"] == ["2026-09-16"]
        # 未完场行由官方同步静默跳过（缺果待下次拍；拒因才进清单）
        assert status["last_run"]["pending_manual"] == []


def test_draw_sync_run_maps_source_error_to_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sync-api-502.db"
    _seed_pending_result_fixture(db_path)

    def boom() -> dict[str, object]:
        raise httpx.ConnectError("source unreachable")

    monkeypatch.setattr(tasks, "draw_results_sync", boom)
    with _make_client(db_path) as client:
        run = client.post("/api/v1/draw-sync/run")
        assert run.status_code == 502
        assert "draw sync source error" in run.json()["detail"]
        # 失败不落元信息行
        assert client.get("/api/v1/draw-sync/status").json()["last_run"] is None


def test_pool_period_views_with_demo_seed(demo_client: TestClient) -> None:
    """彩池期次端点（票 43）：列表/详情/404；概率口径与份额/EV 链路。"""
    periods = demo_client.get("/api/v1/pool/periods")
    assert periods.status_code == 200
    body = periods.json()
    assert [row["period_no"] for row in body] == ["26999"]
    assert body[0]["match_count"] == 14
    assert body[0]["status"] == "on_sale"
    assert body[0]["shares_captured_at"] is not None
    assert body[0]["state"] is None  # 无代采销量 → None（AI 代采待命）

    detail = demo_client.get("/api/v1/pool/periods/26999")
    assert detail.status_code == 200
    payload = detail.json()
    assert len(payload["matches"]) == 14
    assert "返奖率" in payload["caliber"]
    # 场 2（利物浦 vs 曼城）映射到 demo 场次且有 Forecast → 模型口径
    mapped = [m for m in payload["matches"] if m["fixture_id"] is not None]
    assert {m["match_seq"] for m in mapped} == {1, 2, 3}
    model_match = next(m for m in payload["matches"] if m["match_seq"] == 2)
    assert all(s["prob_source"] == "model" for s in model_match["selections"])
    # 场 1 无 Forecast → 欧指去水兜底；份额 → 估计赔率 = 0.65/share
    fallback = next(m for m in payload["matches"] if m["match_seq"] == 1)
    assert all(s["prob_source"] == "euro_devig" for s in fallback["selections"])
    for selection in fallback["selections"]:
        assert selection["share"] is not None
        assert selection["implied_odds"] == pytest.approx(
            0.65 / selection["share"], abs=0.02
        )
        assert selection["ev"] == pytest.approx(
            selection["prob"] * selection["implied_odds"] - 1, abs=0.005
        )
    assert demo_client.get("/api/v1/pool/periods/88888").status_code == 404


def test_pool_sync_status_empty_when_never_synced(api_client: TestClient) -> None:
    """从未同步：last_run 为空、期次数 0（诚实空态）。"""
    response = api_client.get("/api/v1/pool-sync/status")
    assert response.status_code == 200
    assert response.json() == {"last_run": None, "period_count": 0}


def test_pool_state_agent_import_is_idempotent(demo_client: TestClient) -> None:
    """AI 代采入口（票 43 兜底层）：未知期次 404；导入幂等（同值重放单行）。"""
    missing = demo_client.post(
        "/api/v1/pool-states", json={"period_no": "88888", "sales_amount": 1.0}
    )
    assert missing.status_code == 404

    payload = {
        "period_no": "26999",
        "sales_amount": 12345678.5,
        "rollover_in": 654321.0,
        "published_at": "2026-09-20T12:00:00+00:00",
    }
    created = demo_client.post("/api/v1/pool-states", json=payload)
    assert created.status_code == 201
    assert created.json() == {"period_no": "26999", "imported": True}
    replay = demo_client.post("/api/v1/pool-states", json=payload)
    assert replay.status_code == 201
    periods = demo_client.get("/api/v1/pool/periods").json()
    state = periods[0]["state"]
    assert state is not None
    assert state["sales_amount"] == 12345678.5
    assert state["source"] == "agent"


def test_pool_target_plan_risk_tiers_and_units(demo_client: TestClient) -> None:
    """目标金额反推（票 pool-v2/03）：稳档全票/中搏档任9+冷替换、注数数学。"""
    missing = demo_client.post(
        "/api/v1/pool/target-plan",
        json={"period_no": "88888", "target_amount": 5000},
    )
    assert missing.status_code == 404

    steady = demo_client.post(
        "/api/v1/pool/target-plan",
        json={"period_no": "26999", "target_amount": 10000, "risk": "steady"},
    )
    assert steady.status_code == 200
    steady_body = steady.json()
    assert len(steady_body["ticket"]["picks"]) == 14  # 全场
    assert steady_body["ticket"]["swaps"] == []
    assert "稳档" in steady_body["note"]
    # 注数 = ceil(目标 ÷ 单注估计派彩)
    per_unit = steady_body["est_payout_per_unit"]
    assert steady_body["suggested_units"] == pytest.approx(math.ceil(10000 / per_unit))

    bold = demo_client.post(
        "/api/v1/pool/target-plan",
        json={"period_no": "26999", "target_amount": 50000, "risk": "bold"},
    )
    assert bold.status_code == 200
    bold_body = bold.json()
    # 任9：9 场；搏档冷替换命中 demo 唯一冷门样本（场 6 平局，含于 top9）
    assert len(bold_body["ticket"]["picks"]) == 9
    assert [
        (swap["match_seq"], swap["to_code"]) for swap in bold_body["ticket"]["swaps"]
    ] == [(6, "d")]
    assert bold_body["ticket"]["picks"]["6"] == "d"
    # 中档与搏档同选场逻辑（无第二冷门样本时注数同）
    balanced = demo_client.post(
        "/api/v1/pool/target-plan",
        json={"period_no": "26999", "target_amount": 50000, "risk": "balanced"},
    )
    assert balanced.status_code == 200
    assert len(balanced.json()["ticket"]["picks"]) == 9
    # 口径与不承诺声明
    assert "不承诺" in bold_body["caliber"]


def test_pool_cold_variants_greedy_and_valuation(demo_client: TestClient) -> None:
    """搏冷生成器（票 pool-v2/02）：缺省基础票、贪心替换、估值口径自洽。"""
    missing = demo_client.post(
        "/api/v1/pool/cold-variants", json={"period_no": "88888"}
    )
    assert missing.status_code == 404

    response = demo_client.post(
        "/api/v1/pool/cold-variants", json={"period_no": "26999", "coldness": 3}
    )
    assert response.status_code == 200
    payload = response.json()
    assert "贪心替换" in payload["caliber"]
    detail = demo_client.get("/api/v1/pool/periods/26999").json()
    base = payload["base"]
    # 缺省基础票 = 全部场有概率 → 14 场齐全、估值非空
    assert len(base["picks"]) == len(detail["matches"])
    assert base["hit_prob"] is not None
    share_product = 1.0
    for match in detail["matches"]:
        code = base["picks"][str(match["match_seq"])]
        sel = next(s for s in match["selections"] if s["code"] == code)
        share_product *= sel["share"]
    assert base["est_odds"] == pytest.approx(0.65 / share_product, rel=0.01)
    assert base["ev"] == pytest.approx(
        base["hit_prob"] * base["est_odds"] - 1, abs=0.01
    )
    # 变体 1..N：每变体恰比前一变体多一处替换，各场至多一处
    variants = payload["variants"]
    assert 1 <= len(variants) <= 3
    for index, variant in enumerate(variants, start=1):
        assert len(variant["swaps"]) == index
        seqs = [swap["match_seq"] for swap in variant["swaps"]]
        assert len(set(seqs)) == len(seqs)
        for swap in variant["swaps"]:
            assert swap["ev_gain"] > 0
            assert variant["picks"][str(swap["match_seq"])] == swap["to_code"]
            assert base["picks"][str(swap["match_seq"])] == swap["from_code"]
        # 冷替换降命中、抬赔率
        assert variant["hit_prob"] < base["hit_prob"]
        assert variant["est_odds"] > base["est_odds"]

    # 指定基础票 + 非法选项 → 422
    bad = demo_client.post(
        "/api/v1/pool/cold-variants",
        json={"period_no": "26999", "base_picks": {"1": "x"}},
    )
    assert bad.status_code == 422


def test_pool_paper_slip_with_real_period(demo_client: TestClient) -> None:
    """真实期次下的纸面池票提交（票 43：奖池型只纸面红线）。"""
    periods = demo_client.get("/api/v1/pool/periods").json()
    period_id = 1  # demo 库首个期次
    assert periods[0]["period_no"] == "26999"
    response = demo_client.post(
        "/api/v1/pool-slips",
        json={
            "mode": "paper",
            "pool_period_id": period_id,
            "stake_per_combination": 2.0,
            "picks": [
                {"match_seq": seq, "selection_code": "3"} for seq in range(1, 10)
            ],
        },
    )
    assert response.status_code == 201
    slip = response.json()
    assert slip["mode"] == "paper"
    # 池票组合在 combinations 表物化（单选 9 场 → 1 组合），bets 随购买/结算建
    slips = demo_client.get("/api/v1/bet-slips").json()
    assert any(row["id"] == slip["id"] for row in slips)


def test_pool_slip_payload_guards(demo_client: TestClient) -> None:
    """载荷守卫（报告审计票 08）：闭集/ttt14 14 场/期次归属——预览与创建同校验。"""
    # 报告二实测复现：ttt14 + 1 场 + 非法码此前 201 建 1 组合 ¥2 → 今 400
    reported = demo_client.post(
        "/api/v1/pool-slips",
        json={
            "mode": "paper",
            "pool_period_id": 1,
            "market_code": "ttt14",
            "picks": [{"match_seq": 1, "selection_code": "not-a-selection"}],
        },
    )
    assert reported.status_code == 400
    assert "闭集" in reported.json()["detail"]

    # 预览同校验（goals4 死码此前被完备性探针吞掉、pick9/ttt14 裸放）
    for market, code in (("ttt14", "x"), ("pick9", "h"), ("goals4", "h9")):
        preview_bad = demo_client.post(
            "/api/v1/pool-slips/preview",
            json={
                "market_code": market,
                "picks": [{"match_seq": 1, "selection_code": code}],
            },
        )
        assert preview_bad.status_code == 400, (market, preview_bad.text)

    # 合法码但不完备：预览=诚实 0（审计批C #9：不再通用连乘报"可建的
    # N 注"再吃创建 400），创建拒绝
    partial = [{"match_seq": seq, "selection_code": "3"} for seq in (1, 2)]
    partial_preview = demo_client.post(
        "/api/v1/pool-slips/preview",
        json={"market_code": "ttt14", "picks": partial},
    )
    assert partial_preview.status_code == 200
    assert partial_preview.json()["combination_count"] == 0
    partial_create = demo_client.post(
        "/api/v1/pool-slips",
        json={
            "mode": "paper",
            "pool_period_id": 1,
            "market_code": "ttt14",
            "picks": partial,
        },
    )
    assert partial_create.status_code == 400
    assert "ttt14 需 14 场" in partial_create.json()["detail"]

    # 期次归属（已指定期次）：跨期 match_seq 不落库
    foreign = demo_client.post(
        "/api/v1/pool-slips",
        json={
            "mode": "paper",
            "pool_period_id": 1,
            "picks": [{"match_seq": seq, "selection_code": "3"} for seq in range(1, 9)]
            + [{"match_seq": 99, "selection_code": "3"}],
        },
    )
    assert foreign.status_code == 400
    assert "不属于期次" in foreign.json()["detail"]

    # 完备合法票不受影响：ttt14 14 场单选 → 1 组合
    full = demo_client.post(
        "/api/v1/pool-slips",
        json={
            "mode": "paper",
            "pool_period_id": 1,
            "market_code": "ttt14",
            "picks": [
                {"match_seq": seq, "selection_code": "3"} for seq in range(1, 15)
            ],
        },
    )
    assert full.status_code == 201, full.text
    assert full.json()["combination_count"] == 1


def test_pool_sync_run_delegates_to_tasks(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST run 委派 tasks.pool_snapshot：触发即触发，router 不自带实现。"""
    calls: list[None] = []
    monkeypatch.setattr(tasks, "pool_snapshot", lambda: calls.append(None))
    response = api_client.post("/api/v1/pool-sync/run")
    assert response.status_code == 200
    assert calls == [None]
    # 假任务不落行 → 视图读库仍是空同步史
    assert response.json() == {"last_run": None, "period_count": 0}


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError, RuntimeError],
    ids=["unreachable", "page_anomaly"],
)
def test_pool_sync_run_maps_source_failures_to_502(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch, exc: type[Exception]
) -> None:
    """同步源不可达/官方页异常（success=false 等）→ 502（不落元信息行）。"""

    def boom() -> None:
        raise exc("source failed")

    monkeypatch.setattr(tasks, "pool_snapshot", boom)
    response = api_client.post("/api/v1/pool-sync/run")
    assert response.status_code == 502
    assert "pool sync source error" in response.json()["detail"]
    # 失败不落同步元信息
    status = api_client.get("/api/v1/pool-sync/status").json()
    assert status["last_run"] is None
