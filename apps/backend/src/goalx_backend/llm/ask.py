"""
追问 analyst（票 15）：AG-UI 1.0 事件流——只引已存证情报。

一问 → 事件序列（RUN_STARTED → TEXT_MESSAGE_START/CONTENT×N/END →
RUN_FINISHED；失败 RUN_ERROR 收尾），经 openai-agents Runner 流式调
GLM（ADR-0004），prompt 只注入该场已存证情报。无情报场次不调模型，
直接走诚实降级话术（零成本）。每次调用按 usage 记 cost_ledger
（用途标签 ask）。追问不落任何预测/证据工件（advisory 层）。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import cast

from ag_ui.core.events import (
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui.encoder import EventEncoder
from agents import Agent, ModelSettings, Runner
from openai.types.responses import ResponseTextDeltaEvent

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.results import record_cost
from goalx_backend.llm.client import (
    COST_CATEGORY,
    Purpose,
    agent_model,
    estimate_cost_cny,
    resolve_ask_model,
)
from goalx_backend.llm.store import intel_for_fixture

# 追问是流式交互：小预算即可（5.3 系推理模型思考先耗 token，给足余量）
_ASK_MAX_TOKENS = 3000
_ASK_SETTINGS = ModelSettings(max_tokens=_ASK_MAX_TOKENS, temperature=0.3)
_NO_INTEL_MESSAGE = (
    "本场暂无已存证情报——我不能引用不存在的信息回答概率问题（不装懂）。"
    "可以先看研究页的赔率与共识数字；等 scout 采集到情报后再来追问。"
)


def _message_text(content: object) -> str:
    """AG-UI 消息 content → 纯文本（str 直返；分段数组取 text 段拼接）。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in cast(list[object], content):
            part = cast(dict[str, object], item) if isinstance(item, dict) else None
            if part is None:
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip()
    return ""


def extract_question(messages: Sequence[object]) -> str:
    """RunAgentInput.messages → 最后一条 user 消息文本（无则空串）。"""
    for message in reversed(list(messages)):
        role = getattr(message, "role", None)
        if role == "user":
            return _message_text(getattr(message, "content", None))
    return ""


def _intel_lines(intels: list[sqlite3.Row]) -> list[str]:
    return [
        "- [{}] {}（来源 {}，采集 {}）".format(
            r["kind"], r["text"], r["source"], r["collected_at"]
        )
        for r in intels
    ]


def _user_prompt(info: sqlite3.Row, intels: list[sqlite3.Row], question: str) -> str:
    lines = [
        "场次：{} {} vs {}，开球 {}".format(
            info["competition_name"],
            info["home_name"],
            info["away_name"],
            info["kickoff_utc"],
        ),
        "该场已存证情报（只能引用这些，回答时带来源与时点）：",
        *_intel_lines(intels),
        "",
        f"用户问题：{question}",
    ]
    return "\n".join(lines)


_ASK_SYSTEM_PROMPT = (
    "你是足球量化系统的高级分析师 analyst，回答用户对某场比赛的追问。"
    "证据边界：只引用给定情报清单里的内容，引用时带来源与采集时点；"
    "情报之外的任何事实不得编造。回答用中文：先给结论，再给依据，"
    "最后明确指出当前证据的弱点（覆盖面/时点新鲜度/可信度）。"
)


async def ask_analyst_events(
    conn: sqlite3.Connection,
    settings: Settings,
    fixture_id: int,
    question: str,
    *,
    thread_id: str,
    run_id: str | None = None,
    now: str | None = None,
) -> AsyncIterator[str]:
    """
    一问 → AG-UI 事件流（每项为 EventEncoder 编码好的 SSE 行）。

    流程：RUN_STARTED → [无情报：固定降级话术 | 有情报：GLM 流式逐字]
    → RUN_FINISHED；任何失败以 RUN_ERROR 收尾（SSE 已发出，无法改状态码）。
    """
    encoder = EventEncoder()
    run = run_id or uuid.uuid4().hex
    moment = now or datetime.now(UTC).isoformat(timespec="seconds")
    yield encoder.encode(RunStartedEvent(thread_id=thread_id, run_id=run))
    message_id = uuid.uuid4().hex
    try:
        intels = intel_for_fixture(conn, fixture_id)
        if not intels:
            yield encoder.encode(
                TextMessageStartEvent(message_id=message_id, role="assistant")
            )
            yield encoder.encode(
                TextMessageContentEvent(message_id=message_id, delta=_NO_INTEL_MESSAGE)
            )
            yield encoder.encode(TextMessageEndEvent(message_id=message_id))
            yield encoder.encode(
                RunFinishedEvent(
                    thread_id=thread_id, run_id=run, result={"degraded": "no_intel"}
                )
            )
            return

        model_name = resolve_ask_model(conn, settings)
        agent = Agent(
            name="analyst-ask",
            instructions=_ASK_SYSTEM_PROMPT,
            model=agent_model(settings, model_name),
            model_settings=_ASK_SETTINGS,
        )
        info = fx_store.fixture_team_info(conn, fixture_id)
        if info is None:
            raise ValueError(f"fixture {fixture_id} 无对阵信息")
        result = Runner.run_streamed(agent, _user_prompt(info, intels, question))
        yield encoder.encode(
            TextMessageStartEvent(message_id=message_id, role="assistant")
        )
        async for event in result.stream_events():
            if event.type == "raw_response_event" and isinstance(
                event.data, ResponseTextDeltaEvent
            ):
                delta = event.data.delta or ""
                if delta:
                    yield encoder.encode(
                        TextMessageContentEvent(message_id=message_id, delta=delta)
                    )
        yield encoder.encode(TextMessageEndEvent(message_id=message_id))
        usage = result.context_wrapper.usage
        record_cost(
            conn,
            COST_CATEGORY,
            units=float(usage.total_tokens),
            amount_cny=estimate_cost_cny(
                model_name, usage.input_tokens, usage.output_tokens
            ),
            note=f"{model_name}/coding/{Purpose.ASK.value}",
            meta={
                "model": model_name,
                "surface": "coding",
                "purpose": Purpose.ASK.value,
                "fixture_id": fixture_id,
                "prompt_tokens": usage.input_tokens,
                "completion_tokens": usage.output_tokens,
            },
        )
        conn.commit()
        yield encoder.encode(
            RunFinishedEvent(
                thread_id=thread_id,
                run_id=run,
                result={"recorded_at": moment},
            )
        )
    except Exception as exc:
        yield encoder.encode(RunErrorEvent(message=f"追问失败：{exc}"))
