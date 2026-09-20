"""AG-UI 追问端点测试（票 15）：事件序列/诚实降级/记账/错误收尾。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openai.types.responses import ResponseTextDeltaEvent

from goalx_backend.config import Settings
from goalx_backend.db import connect, migrate
from goalx_backend.llm.store import IntelDraft, insert_intel_observation
from goalx_backend.main import create_app

_KICKOFF = "2026-09-26T19:00:00+00:00"


def _seed_fixture(conn: sqlite3.Connection, fixture_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO competitions (name, tier, created_at)"
        " VALUES ('英超', 'tier1', '2026-09-01T00:00:00+00:00')"
    )
    comp_id = conn.execute(
        "SELECT id FROM competitions WHERE name = '英超'"
    ).fetchone()["id"]
    ids = {}
    for name in (f"主{fixture_id}", f"客{fixture_id}"):
        cur = conn.execute(
            "INSERT INTO teams (canonical_name, created_at)"
            " VALUES (?, '2026-09-01T00:00:00+00:00')",
            (name,),
        )
        ids[name] = cur.lastrowid
    conn.execute(
        "INSERT INTO fixtures (id, competition_id, kickoff_utc, home_team_id,"
        " away_team_id) VALUES (?, ?, ?, ?, ?)",
        (fixture_id, comp_id, _KICKOFF, ids[f"主{fixture_id}"], ids[f"客{fixture_id}"]),
    )


def _run_agent_input(question: str) -> dict[str, object]:
    return {
        "threadId": "fixture-1",
        "runId": "run-test",
        "messages": [
            {"id": "m1", "role": "user", "content": question},
        ],
    }


class _FakeUsage:
    input_tokens = 100
    output_tokens = 50
    total_tokens = 150


class _FakeResult:
    def __init__(self) -> None:
        self.context_wrapper = SimpleNamespace(usage=_FakeUsage())

    async def stream_events(self):  # type: ignore[no-untyped-def]
        yield SimpleNamespace(
            type="raw_response_event",
            data=ResponseTextDeltaEvent(
                type="response.output_text.delta",
                delta="依据伤停情报，",
                item_id="i1",
                output_index=0,
                content_index=0,
                sequence_number=1,
                logprobs=[],
            ),
        )
        yield SimpleNamespace(
            type="raw_response_event",
            data=ResponseTextDeltaEvent(
                type="response.output_text.delta",
                delta="主队胜率承压。证据弱点：仅一条情报。",
                item_id="i1",
                output_index=0,
                content_index=0,
                sequence_number=2,
                logprobs=[],
            ),
        )


@pytest.fixture
def ask_client(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    db_path = tmp_path / "ask-test.db"
    conn = connect(db_path)
    migrate(conn)
    _seed_fixture(conn, 1)
    _seed_fixture(conn, 2)  # 无情报场次（诚实降级路径）
    insert_intel_observation(
        conn,
        1,
        IntelDraft(
            kind="formation",
            text="主队主力中卫伤缺",
            source="okooo formation",
            collected_at="2026-09-26T09:00:00+00:00",
            collector="okooo",
            raw_payload={"side": "home"},
        ),
    )
    conn.commit()
    conn.close()
    settings = Settings(db_path=db_path, glm_api_key="test-key")
    with TestClient(create_app(settings=settings)) as client:
        yield client, db_path


def test_ask_streams_agui_events_and_records_cost(
    ask_client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db_path = ask_client
    import goalx_backend.llm.ask as ask_mod

    def _fake_run_streamed(*args: object, **kwargs: object) -> _FakeResult:
        return _FakeResult()  # Runner.run_streamed 是同步方法（流在 stream_events）

    monkeypatch.setattr(ask_mod.Runner, "run_streamed", _fake_run_streamed)

    with client.stream(
        "POST", "/api/v1/fixtures/1/ask", json=_run_agent_input("主队能赢吗？")
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        sse = "".join(chunk for chunk in resp.iter_text())
    assert "RUN_STARTED" in sse
    assert "TEXT_MESSAGE_START" in sse
    assert "依据伤停情报，" in sse
    assert "TEXT_MESSAGE_END" in sse
    assert "RUN_FINISHED" in sse
    assert "RUN_ERROR" not in sse

    # 记账：usage 折算金额落 cost_ledger（用途标签 ask）
    conn = connect(db_path)
    row = conn.execute(
        "SELECT units, amount_cny, note, meta FROM cost_ledger"
        " WHERE category = 'llm_call' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["units"] == 150.0
    assert row["note"].endswith("/ask")
    assert "fixture_id" in row["meta"]


def test_ask_no_intel_degrades_without_model_call(
    ask_client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db_path = ask_client

    def _fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("无情报场次不得调模型")

    monkeypatch.setattr("goalx_backend.llm.ask.Runner.run_streamed", _fail)

    with client.stream(
        "POST", "/api/v1/fixtures/2/ask", json=_run_agent_input("能赢吗？")
    ) as resp:
        assert resp.status_code == 200
        sse = "".join(chunk for chunk in resp.iter_text())
    assert "不装懂" in sse
    assert "RUN_FINISHED" in sse
    assert "degraded" in sse
    # 降级零成本
    conn = connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM cost_ledger WHERE category = 'llm_call'"
    ).fetchone()["n"]
    conn.close()
    assert n == 0


def test_ask_model_failure_ends_with_run_error(
    ask_client: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = ask_client
    import goalx_backend.llm.ask as ask_mod

    def _fail_streamed(*args: object, **kwargs: object) -> None:
        raise RuntimeError("GLM 不可达")

    monkeypatch.setattr(ask_mod.Runner, "run_streamed", _fail_streamed)

    with client.stream(
        "POST", "/api/v1/fixtures/1/ask", json=_run_agent_input("问题")
    ) as resp:
        assert resp.status_code == 200
        sse = "".join(chunk for chunk in resp.iter_text())
    assert "RUN_STARTED" in sse
    assert "RUN_ERROR" in sse
    assert "GLM 不可达" in sse


def test_ask_validations(ask_client: tuple[TestClient, Path]) -> None:
    client, _ = ask_client
    # 404 fixture
    assert (
        client.post("/api/v1/fixtures/999/ask", json=_run_agent_input("q")).status_code
        == 404
    )
    # 422 无用户问题
    assert (
        client.post(
            "/api/v1/fixtures/1/ask",
            json={
                "threadId": "t",
                "messages": [{"id": "m", "role": "assistant", "content": "hi"}],
            },
        ).status_code
        == 422
    )


def test_extract_question_handles_content_parts() -> None:
    from goalx_backend.llm.ask import extract_question

    messages = [
        SimpleNamespace(role="user", content="第一问"),
        SimpleNamespace(role="assistant", content="答"),
        SimpleNamespace(
            role="user",
            content=[
                {"type": "text", "text": "分段"},
                {"type": "text", "text": "提问"},
            ],
        ),
    ]
    assert extract_question(messages) == "分段\n提问"
