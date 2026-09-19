"""
GLM 调用基座（票 08）：档位路由 + 三级熔断 + cost_ledger 记账 + 端面降级。

端面定案（2026-09-19 实测定案，票 08 Comments）：默认 Coding Plan 端面
（订阅额度内零边际成本）；权限/配额类错误（403/429）自动降级按量端面
重试一次。预算口径保守：无论端面，均按牌价折算金额记账并参与熔断。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from agents import OpenAIChatCompletionsModel, set_tracing_disabled
from openai import AsyncOpenAI, OpenAI, PermissionDeniedError, RateLimitError
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from goalx_backend.config import Settings
from goalx_backend.data.results import category_spend_cny, record_cost

COST_CATEGORY = "llm_call"

# 牌价（元 / 百万 tokens；票 02 调研 2026-09，glm-4.7-flash 免费档为 0）
MODEL_PRICING_CNY: dict[str, tuple[float, float]] = {
    "glm-5.3": (8.0, 28.0),
    "glm-5.3-flash": (0.8, 2.8),
    "glm-4.7-flash": (0.0, 0.0),
}
_UNKNOWN_MODEL_PRICING = (0.8, 2.8)

# 熔断阈值（票 08：60%/80%/95% 三级）
_THRESHOLD_ANALYST_DOWN = 0.60
_THRESHOLD_POOL_ONLY = 0.80
_THRESHOLD_FREE_ONLY = 0.95
_LEVEL_ANALYST_DOWN = 1
_LEVEL_POOL_ONLY = 2
_LEVEL_FREE_ONLY = 3


class LlmBudgetExceeded(RuntimeError):
    """月预算熔断拒绝调用（票 08 不变量：超限抛错不静默）。"""


class LlmKeyMissing(RuntimeError):
    """GLM_API_KEY 未配置——调用前显式失败，不落入空 key 请求。"""


class ModelTier(StrEnum):
    """调用档位（票 02：scout 轻量 / analyst 旗舰 / fallback 免费）。"""

    SCOUT = "scout"
    ANALYST = "analyst"
    FALLBACK = "fallback"


class Purpose(StrEnum):
    """调用用途：熔断 80% 档仅保留彩池用途。"""

    SCOUT = "scout"
    POOL = "pool"
    ASK = "ask"


@dataclass(frozen=True)
class ChatResult:
    """一次 GLM 调用的结果与记账要素。"""

    content: str
    model: str
    surface: str  # coding | payg
    prompt_tokens: int
    completion_tokens: int
    estimated_cny: float


def month_spend_cny(conn: sqlite3.Connection, *, now: str | None = None) -> float:
    """本月 LLM 折算支出（cost_ledger category=llm_call，经 data 包读取）。"""
    ref = datetime.fromisoformat(now) if now else datetime.now(UTC)
    month_start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return category_spend_cny(conn, COST_CATEGORY, month_start.isoformat())


def breaker_level(spend_cny: float, budget_cny: float) -> int:
    """熔断档位：0 正常；1 ≥60%；2 ≥80%；3 ≥95%（预算为 0 视为无护栏）。"""
    if budget_cny <= 0:
        return 0
    ratio = spend_cny / budget_cny
    if ratio >= _THRESHOLD_FREE_ONLY:
        return _LEVEL_FREE_ONLY
    if ratio >= _THRESHOLD_POOL_ONLY:
        return _LEVEL_POOL_ONLY
    if ratio >= _THRESHOLD_ANALYST_DOWN:
        return _LEVEL_ANALYST_DOWN
    return 0


def resolve_model(settings: Settings, tier: ModelTier, level: int) -> str:
    """档位→型号，叠加熔断降级（1 档 analyst 降 scout；3 档全降免费）。"""
    if level >= _LEVEL_FREE_ONLY:
        return settings.glm_fallback_model
    if tier is ModelTier.ANALYST and level >= _LEVEL_ANALYST_DOWN:
        return settings.glm_scout_model
    if tier is ModelTier.ANALYST:
        return settings.glm_analyst_model
    if tier is ModelTier.SCOUT:
        return settings.glm_scout_model
    return settings.glm_fallback_model


def estimate_cost_cny(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按牌价折算一次调用的金额（未知型号按 flash 价保守折算）。"""
    price_in, price_out = MODEL_PRICING_CNY.get(model, _UNKNOWN_MODEL_PRICING)
    return round(
        prompt_tokens / 1_000_000 * price_in
        + completion_tokens / 1_000_000 * price_out,
        6,
    )


def _enforce_purpose(purpose: Purpose, level: int) -> None:
    """80% 档仅放行彩池用途（票 08：熔断第二级'仅保彩池 14 场'）。"""
    if level >= _LEVEL_POOL_ONLY and purpose is not Purpose.POOL:
        raise LlmBudgetExceeded(
            f"月预算熔断 level={level}：仅保留彩池用途调用（purpose={purpose}）"
        )


def _build_client(settings: Settings, base_url: str) -> OpenAI:
    """构造同步 OpenAI 兼容客户端（SDK 内建 429/5xx 重试）。"""
    return OpenAI(
        api_key=settings.glm_api_key,
        base_url=base_url,
        timeout=settings.glm_request_timeout,
        max_retries=3,
    )


def _create_completion(
    settings: Settings,
    base_url: str,
    *,
    model: str,
    messages: Sequence[ChatCompletionMessageParam],
    max_tokens: int,
    temperature: float,
) -> ChatCompletion:
    """在指定端面发起一次 chat 调用（显式 kwargs 保住 SDK 重载类型解析）。"""
    return _build_client(settings, base_url).chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def glm_chat(
    conn: sqlite3.Connection,
    settings: Settings,
    tier: ModelTier,
    messages: Sequence[ChatCompletionMessageParam],
    *,
    purpose: Purpose,
    max_tokens: int = 2048,
    temperature: float = 1.0,
) -> ChatResult:
    """单次 GLM 调用：熔断检查 → coding 端面 → 权限类错误降级按量端面 → 记账。"""
    if not settings.glm_api_key:
        raise LlmKeyMissing("GLM_API_KEY 未配置（.env）")
    spend = month_spend_cny(conn)
    level = breaker_level(spend, settings.glm_monthly_budget_cny)
    _enforce_purpose(purpose, level)
    model = resolve_model(settings, tier, level)

    try:
        completion = _create_completion(
            settings,
            settings.glm_base_url,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        surface = "coding"
    except (PermissionDeniedError, RateLimitError):
        # Coding 端面配额耗尽（403/429）→ 按量端面重试一次（票 08 端面定案）
        completion = _create_completion(
            settings,
            settings.glm_payg_base_url,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        surface = "payg"

    usage = completion.usage
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0
    estimated = estimate_cost_cny(model, prompt_tokens, completion_tokens)
    content = completion.choices[0].message.content or ""
    record_cost(
        conn,
        COST_CATEGORY,
        units=float(prompt_tokens + completion_tokens),
        amount_cny=estimated,
        note=f"{model}/{surface}/{purpose.value}",
        meta={
            "model": model,
            "surface": surface,
            "purpose": purpose.value,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    )
    return ChatResult(
        content=content,
        model=model,
        surface=surface,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cny=estimated,
    )


def agent_model(settings: Settings, model: str) -> OpenAIChatCompletionsModel:
    """
    openai-agents 框架的 GLM 模型工厂（ADR 0004 / 票 03 定案）。

    GLM 无 /responses API——必须走 OpenAIChatCompletionsModel（异步，
    只吃 AsyncOpenAI）并关闭 agents 的 tracing（票 02）。agents 路径的
    记账由调用方在 Run 结束后从 usage 汇总补记（本工厂不拦截）。
    """
    set_tracing_disabled(True)
    return OpenAIChatCompletionsModel(
        model=model,
        openai_client=AsyncOpenAI(
            api_key=settings.glm_api_key,
            base_url=settings.glm_base_url,
            timeout=settings.glm_request_timeout,
            max_retries=3,
        ),
    )


__all__ = [
    "COST_CATEGORY",
    "ChatResult",
    "LlmBudgetExceeded",
    "LlmKeyMissing",
    "ModelTier",
    "Purpose",
    "agent_model",
    "breaker_level",
    "estimate_cost_cny",
    "glm_chat",
    "month_spend_cny",
    "resolve_model",
]
