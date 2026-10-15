"""
NewsShock 抽取的 LLM 胶水（caliber-loop 票 01 依赖倒置拆分）。

ADR-0008：LLM 调用住 llm 层（在 data.ingest 之上），语料管线住
``data/ingest/guardian_silver``，经 ``Extractor`` 缝注入——修复
model-iterate 票 04 遗留的 ingest→llm 上翻 import（main 连红 CI）。

供应商分账在本层消化：[1301] 内容过滤→filtered 条目（跳过不崩）；
网络瞬断/空响应各重试一次（真树实测温度 0 偶发空串）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from openai import BadRequestError
from openai.types.chat import ChatCompletionMessageParam

from goalx_backend.config import Settings
from goalx_backend.data.ingest.guardian_silver import (
    Candidate,
    Extractor,
    parse_extraction,
)
from goalx_backend.llm.client import (
    LlmBudgetExceeded,
    LlmKeyMissing,
    ModelTier,
    Purpose,
    glm_chat,
)

MAX_BODY_CHARS = 6000  # 抽取输入截断（成本护栏）

_SYSTEM_PROMPT = (
    "You extract structured football team-news events from a news article "
    "for a quant pipeline. Given the fixture (home team vs away team) and "
    "the article text, output STRICT JSON only:\n"
    '{"relevant": bool, "events": [{"type": '
    '"injury|suspension|manager_change|rotation_hint|other", '
    '"side": "home|away", "player": string-or-null, '
    '"subtype": "out|return-or-null", '
    '"novelty": "first|restate", '
    '"evidence_quote": string, '
    '"confidence": 0.0-1.0}]}\n'
    "Rules: only events affecting the two teams' lineup/management for THIS "
    "fixture; relevant=false if the article is not about team news for this "
    "fixture (post-match reports, transfers unrelated to availability, "
    "league-wide news). subtype='out' (player unavailable) or 'return' "
    "(player back) REQUIRED for injury/suspension, null otherwise. "
    "novelty: 'first' = first confirmation of this specific news, "
    "'restate' = restating previously reported news. evidence_quote: one "
    "verbatim sentence from the article supporting the event. "
    "No prose outside JSON."
)


class ContentFiltered(RuntimeError):
    """供应商内容过滤拦截（[1301]；真树实测个别博彩相关稿件触发）。"""


def make_extractor(conn: sqlite3.Connection, settings: Settings) -> Extractor:
    """生产抽取缝：单篇 LLM 抽取（scout 档）→ checkpoint 条目体。"""

    def extract(candidate: Candidate) -> dict[str, Any]:
        try:
            parsed, model, cost, empty = _extract_with_llm(conn, settings, candidate)
        except ContentFiltered as exc:
            return {"filtered": True, "error": str(exc)[:120]}
        return {
            "filtered": False,
            "parsed": parsed,
            "model": model,
            "cost": cost,
            "empty": empty,
        }

    return extract


def _extract_with_llm(
    conn: sqlite3.Connection, settings: Settings, candidate: Candidate
) -> tuple[dict[str, Any] | None, str, float, bool]:
    """单篇 LLM 抽取（scout 档）；返回 (parsed|None, model, 成本, 原文空)。"""
    link = candidate.link
    user_prompt = (
        f"Fixture: {link.en_home} (home) vs {link.en_away} (away), "
        f"date {link.kickoff_day}.\n"
        f"Article title: {candidate.title}\n"
        f"Article body:\n{candidate.body[:MAX_BODY_CHARS]}"
    )
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    try:
        parsed, model, cost, empty = _glm_once(conn, settings, messages)
    except (ContentFiltered, LlmBudgetExceeded, LlmKeyMissing):
        raise
    except Exception:  # 网络瞬断等——重试一次
        parsed, model, cost, empty = _glm_once(
            conn, settings, messages, temperature=0.2
        )
    if parsed is not None:
        return parsed, model, cost, empty
    # 空响应/违例重试一次（真树实测温度 0 偶发空串）
    parsed, model, cost2, empty = _glm_once(conn, settings, messages, temperature=0.2)
    return parsed, model, cost + cost2, empty


def _glm_once(
    conn: sqlite3.Connection,
    settings: Settings,
    messages: list[ChatCompletionMessageParam],
    *,
    temperature: float = 0.0,
) -> tuple[dict[str, Any] | None, str, float, bool]:
    try:
        result = glm_chat(
            conn,
            settings,
            ModelTier.SCOUT,
            messages,
            purpose=Purpose.SCOUT,
            max_tokens=512,
            temperature=temperature,
        )
    except BadRequestError as exc:  # [1301] contentFilter——个别稿触发，跳过不崩
        if "1301" in str(exc) or "contentFilter" in str(exc):
            raise ContentFiltered(str(exc)) from exc
        raise
    content = result.content.strip()
    return (
        parse_extraction(content),
        result.model,
        result.estimated_cny,
        content == "",
    )
