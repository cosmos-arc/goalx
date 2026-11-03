"""票 11 gate/analyst 单测：JS 散度/路由阈值/复核追加语义（GLM 全 stub）。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

from goalx_backend.config import Settings
from goalx_backend.llm.gate import (
    _JS_ROUTE_THRESHOLD,
    gate_sweep,
    js_divergence,
)
from goalx_backend.modelling.forecast import insert_forecast

_KICKOFF = "2026-09-26T19:00:00+00:00"


class _StubCompletions:
    def __init__(self, content: str) -> None:
        self._content = content

    def create(self, **payload: Any) -> ChatCompletion:
        return ChatCompletion(
            id="stub",
            model=str(payload["model"]),
            object="chat.completion",
            created=0,
            choices=[
                Choice(
                    index=0,
                    finish_reason="stop",
                    message=ChatCompletionMessage(
                        role="assistant", content=self._content
                    ),
                )
            ],
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},  # type: ignore[arg-type]
        )


class _StubClient:
    def __init__(self, content: str) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = _StubCompletions(content)  # type: ignore[attr-defined]


_ANALYST_OUT = '{"h": 0.42, "d": 0.28, "a": 0.30, "rationale": "复核：伤停情报更可信"}'


def _seed_fixture(db: sqlite3.Connection, *, tier: str = "tier1") -> int:
    cur = db.execute(
        "INSERT INTO competitions (name, tier, odds_api_sport_key, created_at)"
        " VALUES (?, ?, 'soccer_epl', '2026-09-01T00:00:00+00:00')",
        ("英超", tier),
    )
    comp_id = cur.lastrowid
    ids = {}
    for name in ("伯恩茅斯", "利物浦"):
        cur = db.execute(
            "INSERT INTO teams (canonical_name, created_at)"
            " VALUES (?, '2026-09-01T00:00:00+00:00')",
            (name,),
        )
        ids[name] = cur.lastrowid
    cur = db.execute(
        "INSERT INTO fixtures (competition_id, kickoff_utc, home_team_id, away_team_id)"
        " VALUES (?, ?, ?, ?)",
        (comp_id, _KICKOFF, ids["伯恩茅斯"], ids["利物浦"]),
    )
    return int(cur.lastrowid)


def _ml_matrix_payload(scale: float = 1.0) -> dict[str, object]:
    """ml 轨真实格式：比分矩阵 payload（had 边际 ≈ 0.53/0.27/0.20）。"""
    # had 边际 ≈ (0.53, 0.27, 0.20)：h=i>j 格，d=对角
    base = [
        [0.12, 0.10, 0.06],
        [0.20, 0.08, 0.04],
        [0.20, 0.13, 0.07],
    ]
    return {
        "matrix": [[cell * scale for cell in row] for row in base],
        "lambda_home": 1.2,
        "lambda_away": 0.9,
    }


def _ml_triple(payload: dict[str, object]) -> tuple[float, float, float]:
    from goalx_backend.modelling.forecast import forecast_matrix_from_payload

    had = forecast_matrix_from_payload(payload).had()  # type: ignore[arg-type]
    return had["h"], had["d"], had["a"]


def _seed_forecasts(
    db: sqlite3.Connection,
    fixture_id: int,
    ml: tuple[float, float, float],
    llm: tuple[float, float, float],
) -> None:
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="ml",
        model_version="dc:1",
        content_hash=f"ml-{fixture_id}",
        payload=_ml_matrix_payload(),
        issued_at="2026-09-19T10:00:00+00:00",
    )
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="llm",
        model_version="glm:glm-5.3-flash",
        content_hash=f"llm-{fixture_id}",
        payload={
            "h": llm[0],
            "d": llm[1],
            "a": llm[2],
            "rationale": "x",
            "model": "glm-5.3-flash",
            "intel_count": 0,
            "intel_ids": [],
        },
        issued_at="2026-09-19T10:50:00+00:00",
    )


def test_js_divergence_properties() -> None:
    assert js_divergence((0.4, 0.3, 0.3), (0.4, 0.3, 0.3)) == 0.0
    # 对称
    p, q = (0.6, 0.25, 0.15), (0.2, 0.3, 0.5)
    assert js_divergence(p, q) == pytest.approx(js_divergence(q, p))
    assert js_divergence(p, q) > _JS_ROUTE_THRESHOLD  # 大分歧样例
    assert js_divergence((0.45, 0.30, 0.25), (0.44, 0.31, 0.25)) < 0.01  # 小分歧


def test_gate_routes_tier1_big_divergence_to_analyst(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_id = _seed_fixture(db)
    _seed_forecasts(
        db, fixture_id, _ml_triple(_ml_matrix_payload()), (0.25, 0.30, 0.45)
    )
    monkeypatch.setattr(
        "goalx_backend.llm.client._build_client",
        lambda s, base: _StubClient(_ANALYST_OUT),
    )
    stats = gate_sweep(db, Settings(glm_api_key="k"), now="2026-09-19T23:30:00+00:00")
    assert stats.pairs == 1
    assert stats.divergent == 1
    assert stats.routed == 1
    assert stats.analyst_inserted == 1
    # divergences 落行
    div = db.execute(
        "SELECT value FROM divergences WHERE fixture_id = ?", (fixture_id,)
    ).fetchall()
    assert len(div) == 1
    assert float(div[0]["value"]) > _JS_ROUTE_THRESHOLD
    # 复核队列入队（open）
    review = db.execute(
        "SELECT route, status FROM review_items WHERE fixture_id = ?", (fixture_id,)
    ).fetchone()
    assert review["route"] == "pre_match"
    assert review["status"] == "open"
    # analyst 追加新行，scout 原行仍在（追加语义）
    rows = db.execute(
        "SELECT id, payload FROM forecasts WHERE fixture_id = ? AND track='llm'"
        " ORDER BY id",
        (fixture_id,),
    ).fetchall()
    assert len(rows) == 2
    revision = json.loads(str(rows[1]["payload"]))
    assert revision["analyst"] is True
    assert revision["revision_of"] == str(rows[0]["id"])  # 指向 scout 原行 id
    assert rows[0]["payload"].find('"rationale": "x"') >= 0  # scout 原行未被改写

    # 重跑：最新 llm 已是 analyst 修订（与 ML 收敛）→ JS 降、不再路由——
    # 修订收敛语义；复核项仍只有一条（UNIQUE 幂等入队）
    stats2 = gate_sweep(db, Settings(glm_api_key="k"), now="2026-09-19T23:40:00+00:00")
    assert stats2.pairs == 1
    assert stats2.routed == 0
    n_review = db.execute(
        "SELECT COUNT(*) AS n FROM review_items WHERE fixture_id = ?", (fixture_id,)
    ).fetchone()["n"]
    assert n_review == 1
    n_llm = db.execute(
        "SELECT COUNT(*) AS n FROM forecasts WHERE fixture_id = ? AND track='llm'",
        (fixture_id,),
    ).fetchone()["n"]
    assert n_llm == 2  # 仍是 scout + analyst 两行，无重复追加


def test_analyst_replay_excludes_future_intel(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """票 04 回放黄金样例：analyst 复核在 backdated now 下读不到未来情报。"""
    from goalx_backend.llm.client import Purpose
    from goalx_backend.llm.gate import analyst_review
    from goalx_backend.llm.store import IntelDraft, insert_intel_observation

    fixture_id = _seed_fixture(db)
    past_id = insert_intel_observation(
        db,
        fixture_id,
        IntelDraft(
            kind="伤停",
            text="过去情报",
            source="s",
            collected_at="2026-09-19T22:00:00+00:00",
            collector="c",
            raw_payload={"i": 1},
        ),
    )
    insert_intel_observation(
        db,
        fixture_id,
        IntelDraft(
            kind="伤停",
            text="未来情报",
            source="s",
            collected_at="2026-09-19T23:45:00+00:00",
            collector="c",
            raw_payload={"i": 2},
        ),
    )
    monkeypatch.setattr(
        "goalx_backend.llm.client._build_client",
        lambda s, base: _StubClient(_ANALYST_OUT),
    )
    status, row_id = analyst_review(
        db,
        Settings(glm_api_key="k"),
        fixture_id,
        purpose=Purpose.SCOUT,
        scout_hash="x",
        now="2026-09-19T23:30:00+00:00",
    )
    assert status == "inserted"
    row = db.execute("SELECT payload FROM forecasts WHERE id = ?", (row_id,)).fetchone()
    revision = json.loads(str(row["payload"]))
    assert revision["intel_ids"] == [past_id]
    assert revision["intel_count"] == 1
    assert "未来情报" not in str(row["payload"])


def test_latest_scout_id_bounded_by_moment(db: sqlite3.Connection) -> None:
    """票 04 correctness I2：revision_of 目标按 moment 时点取——回放不
    指向决策时刻之后才出现的 analyst 修订行。"""
    from goalx_backend.llm.gate import _latest_scout_id

    fixture_id = _seed_fixture(db)
    _seed_forecasts(
        db, fixture_id, _ml_triple(_ml_matrix_payload()), (0.25, 0.30, 0.45)
    )
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="llm",
        model_version="glm:glm-5.3",
        content_hash=f"analyst-{fixture_id}",
        payload={"h": 0.4, "d": 0.3, "a": 0.3, "analyst": True},
        issued_at="2026-09-19T11:30:00+00:00",  # 决策时刻之后才出现的修订
    )
    llm_rows = db.execute(
        "SELECT id FROM forecasts WHERE fixture_id = ? AND track = 'llm' ORDER BY id",
        (fixture_id,),
    ).fetchall()
    scout_id, analyst_id = str(llm_rows[0]["id"]), str(llm_rows[1]["id"])
    # moment=11:00 → scout 原行（10:50），不是 11:30 的修订行
    assert _latest_scout_id(db, fixture_id, "2026-09-19T11:00:00+00:00") == scout_id
    # moment=12:00 → 修订行
    assert _latest_scout_id(db, fixture_id, "2026-09-19T12:00:00+00:00") == analyst_id


def test_gate_small_divergence_records_without_analyst(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_id = _seed_fixture(db)
    _seed_forecasts(
        db, fixture_id, _ml_triple(_ml_matrix_payload()), (0.52, 0.27, 0.21)
    )
    called = False

    def _fail(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("goalx_backend.llm.client._build_client", _fail)
    stats = gate_sweep(db, Settings(glm_api_key="k"), now="2026-09-19T23:30:00+00:00")
    assert stats.pairs == 1
    assert stats.divergent == 0  # 低于 0.02
    assert stats.routed == 0
    assert called is False
    assert db.execute("SELECT COUNT(*) AS n FROM review_items").fetchone()["n"] == 0


def test_gate_big_divergence_tier2_not_routed(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_id = _seed_fixture(db, tier="tier2")
    _seed_forecasts(
        db, fixture_id, _ml_triple(_ml_matrix_payload()), (0.25, 0.30, 0.45)
    )
    monkeypatch.setattr(
        "goalx_backend.llm.client._build_client",
        lambda s, base: _StubClient(_ANALYST_OUT),
    )
    stats = gate_sweep(db, Settings(glm_api_key="k"), now="2026-09-19T23:30:00+00:00")
    assert stats.divergent == 1
    assert stats.routed == 0  # Tier2 不复核（旧图纪律）
    assert db.execute("SELECT COUNT(*) AS n FROM review_items").fetchone()["n"] == 0
