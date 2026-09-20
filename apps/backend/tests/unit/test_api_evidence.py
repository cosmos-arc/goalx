"""证据面 API 测试（票 14）：证据卡汇总/证据链/复核队列/盲评/三列报告。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend.config import Settings
from goalx_backend.db import connect, migrate
from goalx_backend.llm.gate import _enqueue_review, _record_divergence
from goalx_backend.llm.store import IntelDraft, insert_intel_observation
from goalx_backend.main import create_app
from goalx_backend.modelling.forecast import insert_forecast

_KICKOFF = "2026-09-26T19:00:00+00:00"
_PERIOD_NO = "26999"


def _seed_fixture(conn, fixture_id: int, home: str, away: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO competitions (name, tier, created_at)"
        " VALUES ('英超', 'tier1', '2026-09-01T00:00:00+00:00')"
    )
    comp_id = conn.execute(
        "SELECT id FROM competitions WHERE name = '英超'"
    ).fetchone()["id"]
    ids = {}
    for name in (home, away):
        cur = conn.execute(
            "INSERT INTO teams (canonical_name, created_at)"
            " VALUES (?, '2026-09-01T00:00:00+00:00')",
            (name,),
        )
        ids[name] = cur.lastrowid
    conn.execute(
        "INSERT INTO fixtures (id, competition_id, kickoff_utc, home_team_id,"
        " away_team_id) VALUES (?, ?, ?, ?, ?)",
        (fixture_id, comp_id, _KICKOFF, ids[home], ids[away]),
    )
    # 桥接候选集要求竞彩销售码（match_fixture_id 走 match_codes 业务日查询）
    conn.execute(
        "INSERT INTO match_codes (fixture_id, kind, code, business_date)"
        " VALUES (?, 'jingcai', ?, '2026-09-26')",
        (fixture_id, f"周六00{fixture_id}"),
    )


def _seed_tracks(
    conn,
    fixture_id: int,
    *,
    with_llm: bool = True,
    with_analyst: bool = False,
    with_fused: bool = False,
) -> None:
    insert_forecast(
        conn,
        fixture_id=fixture_id,
        track="ml",
        model_version="dc:1",
        content_hash=f"ml-{fixture_id}",
        payload={
            "matrix": [[0, 0, 0.2], [0, 0.25, 0], [0.5, 0, 0]],
            "lambda_home": 1.0,
            "lambda_away": 1.0,
        },
        issued_at="2026-09-26T10:00:00+00:00",
    )
    if with_llm:
        insert_forecast(
            conn,
            fixture_id=fixture_id,
            track="llm",
            model_version="glm:1",
            content_hash=f"llm-{fixture_id}",
            payload={"h": 0.5, "d": 0.25, "a": 0.25, "intel_count": 1},
            issued_at="2026-09-26T10:50:00+00:00",
        )
    if with_analyst:
        insert_forecast(
            conn,
            fixture_id=fixture_id,
            track="llm",
            model_version="glm:1",
            content_hash=f"analyst-{fixture_id}",
            payload={
                "h": 0.45,
                "d": 0.25,
                "a": 0.3,
                "analyst": True,
                "rationale": "伤停上调负概率",
            },
            issued_at="2026-09-26T11:50:00+00:00",
        )
    if with_fused:
        insert_forecast(
            conn,
            fixture_id=fixture_id,
            track="fused",
            model_version="leap:w=0.5",
            content_hash=f"fused-{fixture_id}",
            payload={"h": 0.48, "d": 0.25, "a": 0.27, "method": "log_pool"},
            issued_at="2026-09-26T12:00:00+00:00",
        )


def _seed_pool_period(conn, rows: list[tuple[int, str, str, str, int | None]]) -> None:
    """(seq, home, away, league, fixture_id) — fixture_id None = 未桥接。"""
    conn.execute(
        "INSERT INTO pool_periods (market_code, period_no, sales_deadline)"
        " VALUES ('ttt14', ?, '2026-09-26T12:00:00+00:00')",
        (_PERIOD_NO,),
    )
    period_id = conn.execute(
        "SELECT id FROM pool_periods WHERE period_no = ?", (_PERIOD_NO,)
    ).fetchone()["id"]
    for seq, home, away, league, _fid in rows:
        conn.execute(
            "INSERT INTO pool_matches (pool_period_id, match_seq, league,"
            " kickoff_utc, home_team, away_team) VALUES (?, ?, ?, ?, ?, ?)",
            (period_id, seq, league, _KICKOFF, home, away),
        )


@pytest.fixture
def evidence_client(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    """一期三场：场1 全链路（analyst+fused+情报+分歧+复核），场2 纯净，场3 未桥接。"""
    db_path = tmp_path / "evidence-test.db"
    conn = connect(db_path)
    migrate(conn)
    _seed_fixture(conn, 1, "阿森纳", "切尔西")
    _seed_fixture(conn, 2, "拜仁", "多特")
    _seed_pool_period(
        conn,
        [
            (1, "阿森纳", "切尔西", "英超", 1),
            (2, "拜仁", "多特", "德甲", 2),
            (3, "加的夫城", "诺维奇", "英冠", None),
        ],
    )
    _seed_tracks(conn, 1, with_llm=True, with_analyst=True, with_fused=True)
    _seed_tracks(conn, 2, with_llm=False)
    insert_intel_observation(
        conn,
        1,
        IntelDraft(
            kind="form",
            text="阿森纳 近6轮 WWDWLD（进9失6）",
            source="fdhist:E0",
            collected_at="2026-09-26T09:00:00+00:00",
            collector="internal-fdhist",
            raw_payload={"side": "home"},
        ),
    )
    _record_divergence(conn, 1, 0.072, "2026-09-26T11:00:00+00:00")
    _enqueue_review(conn, 1, 0.072, "2026-09-26T11:00:00+00:00")
    conn.commit()
    conn.close()
    with TestClient(create_app(settings=Settings(db_path=db_path))) as client:
        yield client, db_path


def test_evidence_summary_states(evidence_client: tuple[TestClient, Path]) -> None:
    client, _ = evidence_client
    resp = client.get(f"/api/v1/pool/periods/{_PERIOD_NO}/evidence-summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["period_no"] == _PERIOD_NO
    assert len(body["matches"]) == 3
    first, second, third = body["matches"]

    # 场 1：analyst 已复核 → 展示融合线，状态 analyst_done，情报/分歧/路由齐
    assert first["state"] == "analyst_done"
    assert first["forecast"]["track"] == "fused"
    assert first["forecast"]["h"] == 0.48
    assert first["intel_count"] == 1
    assert first["intels"][0]["source"] == "fdhist:E0"
    assert first["divergence"] == {"js": 0.072, "routed": True}

    # 场 2：有 ML 无情报无 LLM 产出 → 诚实降级（不装懂）
    assert second["state"] == "no_intel"
    assert second["forecast"] is None
    assert second["intels"] == []

    # 场 3：未桥接 fixture → 无证据可说
    assert third["fixture_id"] is None
    assert third["state"] == "no_intel"


def test_evidence_summary_scout_state_and_llm_fallback(
    evidence_client: tuple[TestClient, Path],
) -> None:
    client, db_path = evidence_client
    conn = connect(db_path)
    _seed_fixture(conn, 3, "加的夫城", "诺维奇")  # 与池对阵同名 → 桥接命中
    _seed_tracks(conn, 3, with_llm=True)  # scout only，无 analyst/fused
    conn.commit()
    conn.close()
    body = client.get(f"/api/v1/pool/periods/{_PERIOD_NO}/evidence-summary").json()
    third = body["matches"][2]
    assert third["state"] == "scout_done"
    assert third["forecast"]["track"] == "llm"
    assert third["divergence"]["js"] is None


def test_evidence_summary_404(evidence_client: tuple[TestClient, Path]) -> None:
    client, _ = evidence_client
    assert client.get("/api/v1/pool/periods/00000/evidence-summary").status_code == 404


def test_fixture_evidence_chain(evidence_client: tuple[TestClient, Path]) -> None:
    client, _ = evidence_client
    resp = client.get("/api/v1/fixtures/1/evidence")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["tracks"]) == {"ml", "llm", "fused"}
    assert body["tracks"]["ml"]["h"] == 0.5  # 矩阵推 had 边际
    assert body["tracks"]["fused"]["h"] == 0.48
    assert body["divergence"]["js"] == 0.072
    assert body["intels"][0]["kind"] == "form"
    assert body["reviews"][0]["route"] == "pre_match"
    assert body["reviews"][0]["status"] == "open"
    # 404
    assert client.get("/api/v1/fixtures/999/evidence").status_code == 404


def test_review_queue_and_verdict(evidence_client: tuple[TestClient, Path]) -> None:
    client, _ = evidence_client
    queue = client.get("/api/v1/review/queue").json()
    assert len(queue["items"]) == 1
    item = queue["items"][0]
    assert item["fixture_id"] == 1
    assert item["home_team"] == "阿森纳"
    assert item["route"] == "pre_match"
    assert item["js_value"] == 0.072

    ok = client.post(
        f"/api/v1/review/items/{item['id']}/verdict",
        json={"classification": "key_contribution", "note": "伤停情报关键"},
    )
    assert ok.status_code == 200
    assert ok.json() == {"recorded": True}
    # 已裁决 → 队列空；重复裁决 404
    assert client.get("/api/v1/review/queue").json()["items"] == []
    again = client.post(
        f"/api/v1/review/items/{item['id']}/verdict",
        json={"classification": "irrelevant"},
    )
    assert again.status_code == 404
    # 非法分类 422
    bad = client.post(
        f"/api/v1/review/items/{item['id']}/verdict",
        json={"classification": "wrong"},
    )
    assert bad.status_code == 422


def test_blind_review_idempotent(evidence_client: tuple[TestClient, Path]) -> None:
    client, _ = evidence_client
    payload = {"cycle": "2026-B19", "fixture_id": 1, "choice": "llm"}
    first = client.post("/api/v1/blind-reviews", json=payload)
    assert first.status_code == 200
    assert first.json() == {"recorded": True}
    dup = client.post("/api/v1/blind-reviews", json=payload)
    assert dup.json() == {"recorded": False}


def test_m3_protocol_report_endpoint(tmp_path: Path) -> None:
    db_path = tmp_path / "m3-report.db"
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    with TestClient(create_app(settings=Settings(db_path=db_path))) as client:
        resp = client.get("/api/v1/validation/m3-protocol")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["tracks"]) == {"ml", "llm", "fused"}
    assert body["tier_a"]["verdict"] == "未证明（样本不足）"
    assert body["note"].startswith("LLM/Fused 为参考列")
