"""票 08 GLM 基座单测：熔断/路由/记账/端面降级（全部离线，不发网络请求）。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx
import pytest
from openai import PermissionDeniedError
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

from goalx_backend.config import Settings
from goalx_backend.data.results import record_cost
from goalx_backend.llm.client import (
    COST_CATEGORY,
    LlmBudgetExceeded,
    LlmKeyMissing,
    ModelTier,
    Purpose,
    breaker_level,
    estimate_cost_cny,
    glm_chat,
    month_spend_cny,
    resolve_model,
)


def _settings(**overrides: Any) -> Settings:
    """带 key 的 GLM 配置（预算可覆盖以便触发熔断档）。"""
    return Settings(
        glm_api_key="test-key",
        glm_monthly_budget_cny=overrides.pop("budget", 100.0),
        **overrides,
    )


class _StubCompletions:
    """按脚本回放的假 completions：元素为异常或 usage 元组。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)

    def create(self, **payload: Any) -> ChatCompletion:
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        prompt_tokens, completion_tokens = step
        return ChatCompletion(
            id="stub",
            model=str(payload["model"]),
            object="chat.completion",
            created=0,
            choices=[
                Choice(
                    index=0,
                    finish_reason="stop",
                    message=ChatCompletionMessage(role="assistant", content="可用"),
                )
            ],
            usage={  # type: ignore[arg-type]
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )


class _StubClient:
    def __init__(self, script: list[Any]) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = _StubCompletions(script)  # type: ignore[attr-defined]


def _permission_error() -> PermissionDeniedError:
    request = httpx.Request("POST", "https://stub/chat/completions")
    response = httpx.Response(403, request=request)
    return PermissionDeniedError("model_access_denied", response=response, body=None)


def _seed_spend(
    db: sqlite3.Connection, amount: float, *, month: str = "2026-09-01T00:00:00+00:00"
) -> None:
    record_cost(db, COST_CATEGORY, units=1.0, amount_cny=amount, occurred_at=month)


# --- 熔断档位 ---


@pytest.mark.parametrize(
    ("spend", "budget", "level"),
    [
        (0.0, 100.0, 0),
        (59.9, 100.0, 0),
        (60.0, 100.0, 1),
        (79.9, 100.0, 1),
        (80.0, 100.0, 2),
        (94.9, 100.0, 2),
        (95.0, 100.0, 3),
        (150.0, 100.0, 3),
        (100.0, 0.0, 0),  # 预算为 0 = 无护栏
    ],
)
def test_breaker_level(spend: float, budget: float, level: int) -> None:
    assert breaker_level(spend, budget) == level


def test_resolve_model_downgrades() -> None:
    settings = _settings()
    assert resolve_model(settings, ModelTier.ANALYST, 0) == "glm-5.3"
    assert resolve_model(settings, ModelTier.ANALYST, 1) == "glm-5.3-flash"
    assert resolve_model(settings, ModelTier.ANALYST, 3) == "glm-4.7-flash"
    assert resolve_model(settings, ModelTier.SCOUT, 0) == "glm-5.3-flash"
    assert resolve_model(settings, ModelTier.SCOUT, 3) == "glm-4.7-flash"
    assert resolve_model(settings, ModelTier.FALLBACK, 0) == "glm-4.7-flash"


# --- 记账与月支出 ---


def test_month_spend_only_counts_current_month(db: sqlite3.Connection) -> None:
    _seed_spend(db, 30.0, month="2026-08-15T00:00:00+00:00")
    _seed_spend(db, 12.5, month="2026-09-05T00:00:00+00:00")
    _seed_spend(db, 7.5, month="2026-09-19T00:00:00+00:00")
    record_cost(
        db,
        "odds_api_credit",
        units=3.0,
        amount_cny=99.0,
        occurred_at="2026-09-19T00:00:00+00:00",
    )
    assert month_spend_cny(db, now="2026-09-19T12:00:00+00:00") == pytest.approx(20.0)


def test_estimate_cost_known_and_unknown() -> None:
    # 1M tokens 按 5.3 牌价：8 + 28 = 36 元
    assert estimate_cost_cny("glm-5.3", 1_000_000, 1_000_000) == pytest.approx(36.0)
    # 4.7-flash 免费档折算 0
    assert estimate_cost_cny("glm-4.7-flash", 1000, 1000) == 0.0
    # 未知型号按 flash 价保守折算
    assert estimate_cost_cny("glm-9.9-pro", 1_000_000, 0) == pytest.approx(0.8)


# --- glm_chat 行为 ---


def test_glm_chat_missing_key_raises(db: sqlite3.Connection) -> None:
    settings = Settings(glm_api_key="")
    with pytest.raises(LlmKeyMissing):
        glm_chat(
            db,
            settings,
            ModelTier.SCOUT,
            [{"role": "user", "content": "hi"}],
            purpose=Purpose.SCOUT,
        )


def test_glm_chat_records_cost(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(
        "goalx_backend.llm.client._build_client",
        lambda s, base: _StubClient([(17, 73)]),
    )
    result = glm_chat(
        db,
        settings,
        ModelTier.SCOUT,
        [{"role": "user", "content": "hi"}],
        purpose=Purpose.SCOUT,
    )
    assert result.content == "可用"
    assert result.surface == "coding"
    assert result.model == "glm-5.3-flash"
    assert result.estimated_cny == pytest.approx(
        estimate_cost_cny("glm-5.3-flash", 17, 73)
    )
    row = db.execute(
        "SELECT units, amount_cny, note, meta FROM cost_ledger WHERE category = ?",
        (COST_CATEGORY,),
    ).fetchone()
    assert row["units"] == 90.0
    assert row["note"].startswith("glm-5.3-flash/coding/scout")
    meta = json.loads(row["meta"])
    assert meta["surface"] == "coding"
    assert meta["completion_tokens"] == 73


def test_glm_chat_payg_fallback_on_permission_denied(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    clients: list[_StubClient] = [
        _StubClient([_permission_error()]),
        _StubClient([(10, 20)]),
    ]
    monkeypatch.setattr(
        "goalx_backend.llm.client._build_client", lambda s, base: clients.pop(0)
    )
    result = glm_chat(
        db,
        settings,
        ModelTier.ANALYST,
        [{"role": "user", "content": "hi"}],
        purpose=Purpose.POOL,
    )
    assert result.surface == "payg"
    assert result.model == "glm-5.3"


def test_glm_chat_level2_blocks_non_pool(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _seed_spend(db, 81.0)
    called = False

    def _fail(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("goalx_backend.llm.client._build_client", _fail)
    with pytest.raises(LlmBudgetExceeded):
        glm_chat(
            db,
            settings,
            ModelTier.SCOUT,
            [{"role": "user", "content": "hi"}],
            purpose=Purpose.ASK,
        )
    assert called is False


def test_glm_chat_level1_downgrades_analyst_model(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _seed_spend(db, 65.0)
    seen: dict[str, Any] = {}

    class _Capture(_StubClient):
        def __init__(self) -> None:
            super().__init__([(5, 5)])

        def __getattr__(self, name: str) -> Any:
            return super().__getattr__(name)

    def _capture(settings_arg: Settings, base: str) -> _StubClient:
        stub = _Capture()

        original_create = stub.chat.completions.create

        def create(**payload: Any) -> Any:
            seen["model"] = payload["model"]
            return original_create(**payload)

        stub.chat.completions.create = create  # type: ignore[method-assign]
        return stub

    monkeypatch.setattr("goalx_backend.llm.client._build_client", _capture)
    glm_chat(
        db,
        settings,
        ModelTier.ANALYST,
        [{"role": "user", "content": "hi"}],
        purpose=Purpose.POOL,
    )
    assert seen["model"] == "glm-5.3-flash"


def test_glm_chat_level3_uses_free_model(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _seed_spend(db, 96.0)
    monkeypatch.setattr(
        "goalx_backend.llm.client._build_client",
        lambda s, base: _StubClient([(3, 4)]),
    )
    # 三级=全切免费档，但用途门控仍按 80% 档纪律（仅彩池）——预算保护优先
    result = glm_chat(
        db,
        settings,
        ModelTier.ANALYST,
        [{"role": "user", "content": "hi"}],
        purpose=Purpose.POOL,
    )
    assert result.model == "glm-4.7-flash"
    assert result.estimated_cny == 0.0
    with pytest.raises(LlmBudgetExceeded):
        glm_chat(
            db,
            settings,
            ModelTier.SCOUT,
            [{"role": "user", "content": "hi"}],
            purpose=Purpose.ASK,
        )
