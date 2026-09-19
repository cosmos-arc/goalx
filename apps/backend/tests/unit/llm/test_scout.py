"""票 10 scout 线单测：解析/落库/幂等/目标集合（GLM 全 stub，零外网）。"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

from goalx_backend.config import Settings
from goalx_backend.data import pool as pool_store
from goalx_backend.llm.client import Purpose
from goalx_backend.llm.scout import (
    INSERTED,
    KNOWN,
    PARSE_FAILED,
    parse_probs,
    scout_fixture,
    scout_targets,
)
from goalx_backend.llm.store import IntelDraft, insert_intel_observation

_KICKOFF = "2026-09-26T19:00:00+00:00"


def _settings() -> Settings:
    return Settings(glm_api_key="test-key")


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


_GOOD = '{"h": 0.45, "d": 0.27, "a": 0.28, "rationale": "主队伤停较多"}'


def _seed_fixture(db: sqlite3.Connection) -> int:
    cur = db.execute(
        "INSERT INTO competitions (name, tier, odds_api_sport_key, created_at)"
        " VALUES ('英超', 'tier1', 'soccer_epl', '2026-09-01T00:00:00+00:00')"
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
    fixture_id = int(cur.lastrowid)
    db.execute(
        "INSERT INTO match_codes (fixture_id, kind, business_date, code, is_single)"
        " VALUES (?, 'jingcai', ?, '周六001', 1)",
        (fixture_id, "2026-09-26"),
    )
    return fixture_id


def _seed_intel(db: sqlite3.Connection, fixture_id: int) -> None:
    insert_intel_observation(
        db,
        fixture_id,
        IntelDraft(
            kind="伤停",
            text="主队伤停（影响600万）：伤【卫】法比安",
            source="okooo.com/formation",
            collected_at="2026-09-19T22:00:00+00:00",
            collector="okooo-formation",
            raw_payload={"team": "home"},
        ),
    )


def test_parse_probs_variants() -> None:
    assert parse_probs(_GOOD) == pytest.approx((0.45, 0.27, 0.28, "主队伤停较多"))
    # 包裹在说明文字里的 JSON 也能抽出来
    wrapped = f"分析如下：{_GOOD} 以上。"
    assert parse_probs(wrapped) is not None
    # 轻微不闭和 → 归一
    r = parse_probs('{"h": 0.5, "d": 0.3, "a": 0.3, "rationale": "x"}')
    assert r is not None
    assert sum(r[:3]) == pytest.approx(1.0)
    # 坏输出 → None
    assert parse_probs("我认为主队会赢") is None
    assert parse_probs('{"h": 2.0, "d": -1, "a": 0, "rationale": ""}') is None
    assert (
        parse_probs('{"h": 0.25, "d": 0.25, "a": 0.25}') is not None
    )  # 和=0.75 容忍归一
    assert parse_probs('{"h": 0.1, "d": 0.1, "a": 0.1}') is None  # 和=0.3 超容忍


def test_scout_fixture_inserts_forecast(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_id = _seed_fixture(db)
    _seed_intel(db, fixture_id)
    monkeypatch.setattr(
        "goalx_backend.llm.client._build_client", lambda s, base: _StubClient(_GOOD)
    )
    status, row_id = scout_fixture(
        db,
        _settings(),
        fixture_id,
        purpose=Purpose.SCOUT,
        now="2026-09-19T23:00:00+00:00",
    )
    assert status == INSERTED
    assert row_id is not None
    row = db.execute(
        "SELECT track, model_version, payload FROM forecasts WHERE id = ?", (row_id,)
    ).fetchone()
    assert row["track"] == "llm"
    assert row["model_version"] == "glm:glm-5.3-flash"
    import json as _json

    payload = _json.loads(str(row["payload"]))
    assert payload["intel_count"] == 1
    assert payload["intel_ids"]
    assert sum(payload[k] for k in ("h", "d", "a")) == pytest.approx(1.0)
    # 成本行落账（GLM 调用记账）
    assert (
        db.execute(
            "SELECT COUNT(*) AS n FROM cost_ledger WHERE category = 'llm_call'"
        ).fetchone()["n"]
        == 1
    )

    # 同内容重跑 → 幂等吸收
    status2, row_id2 = scout_fixture(
        db,
        _settings(),
        fixture_id,
        purpose=Purpose.SCOUT,
        now="2026-09-19T23:10:00+00:00",
    )
    assert status2 == KNOWN
    assert row_id2 is None
    assert (
        db.execute(
            "SELECT COUNT(*) AS n FROM forecasts WHERE track = 'llm'"
        ).fetchone()["n"]
        == 1
    )


def test_scout_fixture_parse_failed_zero_rows(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_id = _seed_fixture(db)
    monkeypatch.setattr(
        "goalx_backend.llm.client._build_client",
        lambda s, base: _StubClient("主场氛围不错，应该能赢"),
    )
    status, row_id = scout_fixture(
        db,
        _settings(),
        fixture_id,
        purpose=Purpose.SCOUT,
        now="2026-09-19T23:00:00+00:00",
    )
    assert status == PARSE_FAILED
    assert row_id is None
    assert (
        db.execute(
            "SELECT COUNT(*) AS n FROM forecasts WHERE track = 'llm'"
        ).fetchone()["n"]
        == 0
    )
    # 解析失败但 token 已花——成本行仍记账（atomic 正常提交）
    assert (
        db.execute(
            "SELECT COUNT(*) AS n FROM cost_ledger WHERE category = 'llm_call'"
        ).fetchone()["n"]
        == 1
    )


def test_scout_targets_pool_purpose(db: sqlite3.Connection) -> None:
    fixture_id = _seed_fixture(db)
    period_id = pool_store.upsert_pool_period(
        db, "ttt14", "26999", "2026-09-27T21:00:00+00:00"
    )
    pool_store.replace_pool_matches(
        db,
        period_id,
        [
            pool_store.PoolMatchInput(
                match_seq=1,
                source_match_id="1328101",
                league="英超",
                kickoff_utc=_KICKOFF,
                home_team="伯恩茅斯",
                away_team="利物浦",
                euro_odds=(2.5, 3.2, 2.6),
            )
        ],
    )
    targets = scout_targets(db, now="2026-09-26T10:00:00+00:00")
    assert targets.get(fixture_id) is Purpose.POOL  # 彩池优先级盖过 tier1


def test_scout_targets_tier1_without_pool(db: sqlite3.Connection) -> None:
    fixture_id = _seed_fixture(db)  # tier1 + 带销售编号
    targets = scout_targets(db, now="2026-09-26T10:00:00+00:00")
    assert targets.get(fixture_id) is Purpose.SCOUT
