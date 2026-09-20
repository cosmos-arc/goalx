"""
scout 线（票 10）：GLM-5.3-Flash 读已存证情报 → 三项概率。

工件落既有 ``forecasts(track='llm')``（票 04 复用定案，绝不覆写
track='ml'）；payload 带 intel_observation ids 引用 + rationale +
intel_count（0=无情报降级，票 10 不变量）。目标集合：当期彩池桥接
场次（purpose=pool）+ 在售竞彩 Tier1 场次（purpose=scout）——约
30-50 场/日（票 03 定案）。

注意 5.3-Flash 是推理模型：思考走 reasoning_content 且先耗 token，
max_tokens 必须给足（票 08 实测定案），小预算会截出空 content。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from loguru import logger
from openai.types.chat import ChatCompletionMessageParam

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.llm.client import ModelTier, Purpose, glm_chat
from goalx_backend.llm.store import intel_for_fixture
from goalx_backend.modelling.forecast import insert_forecast

# openai 消息参数类型（glm_chat 签名要求；dict 字面量可直接赋值）
_Message = dict[str, str]

_MAX_TOKENS = 3500  # 推理模型预算余量（票 08：reasoning 先耗 token）
_TEMPERATURE = 0.3  # 结构化概率输出用低温
_SUM_TOLERANCE = 0.35  # 三项和的宽松校验（归一前）；超出视为坏输出

_JSON_RE = re.compile(r"\{[^{}]*\}")

# scout_fixture 结果状态
INSERTED = "inserted"
KNOWN = "known"  # 同内容已存在（幂等吸收）
PARSE_FAILED = "parse_failed"


@dataclass
class ScoutStats:
    """一次 scout 扫描的计数（幂等重跑 inserted=0 属正常）。"""

    targets: int = 0
    inserted: int = 0
    skipped_known: int = 0
    parse_failed: int = 0
    call_failed: int = 0


_SYSTEM_PROMPT = (
    "你是足球量化系统的情报分析师 scout，基于给定情报估计主胜/平/客负"
    "三项概率。情报里的来源与时点是证据边界：不得编造情报之外的事实。"
)

_OUTPUT_CONTRACT = (
    "综合判断输出严格 JSON："
    '{"h": 胜概率, "d": 平概率, "a": 负概率, "rationale": "不超过60字依据"}'
    "——h/d/a 为 0-1 的数且三者和为 1。只输出 JSON，不要输出其他内容。"
)


def _build_messages(
    info: sqlite3.Row, intels: list[sqlite3.Row]
) -> Sequence[ChatCompletionMessageParam]:
    """场次 + 已存证情报 → 对话（情报逐条带来源与时点）。"""
    lines = [
        "场次：{} {} vs {}，开球 {}".format(
            info["competition_name"],
            info["home_name"],
            info["away_name"],
            info["kickoff_utc"],
        )
    ]
    if intels:
        lines.append("情报（均已存证，带来源与采集时点）：")
        for row in intels:
            lines.append(
                "- [{}] {}（来源 {}，{}）".format(
                    row["kind"], row["text"], row["source"], row["collected_at"]
                )
            )
    else:
        lines.append("情报：本场暂无已存证情报——按基础实力先验估计并明说。")
    lines.append(_OUTPUT_CONTRACT)
    return [
        cast(ChatCompletionMessageParam, {"role": "system", "content": _SYSTEM_PROMPT}),
        cast(ChatCompletionMessageParam, {"role": "user", "content": "\n".join(lines)}),
    ]


def parse_probs(content: str) -> tuple[float, float, float, str] | None:
    """模型输出 → (h, d, a, rationale)；结构坏/越界返回 None。"""
    for candidate in [content, *_JSON_RE.findall(content)]:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        fields = cast(dict[str, object], obj)
        try:
            raw_h = cast("float | int | str", fields["h"])
            raw_d = cast("float | int | str", fields["d"])
            raw_a = cast("float | int | str", fields["a"])
            probs = (float(raw_h), float(raw_d), float(raw_a))
        except (KeyError, TypeError, ValueError):
            continue
        if any(p < 0 or p > 1 for p in probs):
            continue
        total = sum(probs)
        if total <= 1 - _SUM_TOLERANCE or total >= 1 + _SUM_TOLERANCE:
            continue
        h, d, a = (p / total for p in probs)  # 归一（宽容轻微不闭和）
        rationale = str(fields.get("rationale", ""))[:120]
        return h, d, a, rationale
    return None


def _payload_hash(payload: dict[str, object]) -> str:
    """工件哈希：payload 规范化 JSON 的 sha256（确定性，幂等键）。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def scout_fixture(
    conn: sqlite3.Connection,
    settings: Settings,
    fixture_id: int,
    *,
    purpose: Purpose,
    now: str | None = None,
) -> tuple[str, int | None]:
    """
    一场 → GLM 三项概率 → forecasts(track='llm')。

    返回 (状态, 新行 id)：inserted / known（同内容幂等吸收）/
    parse_failed（模型输出不可解析，零行——宁缺毋假）。
    """
    info = fx_store.fixture_team_info(conn, fixture_id)
    if info is None:
        return PARSE_FAILED, None
    moment = now or utc_now_iso()
    intels = intel_for_fixture(conn, fixture_id)
    payload: dict[str, object] | None = None
    with atomic(conn):
        result = glm_chat(
            conn,
            settings,
            ModelTier.SCOUT,
            _build_messages(info, intels),
            purpose=purpose,
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
        )
        parsed = parse_probs(result.content)
        if parsed is None:
            logger.warning(
                "scout 输出不可解析 fixture={}: {}", fixture_id, result.content[:80]
            )
            return PARSE_FAILED, None
        h, d, a, rationale = parsed
        payload = {
            "h": h,
            "d": d,
            "a": a,
            "rationale": rationale,
            "model": result.model,
            "intel_count": len(intels),
            "intel_ids": [int(r["id"]) for r in intels],
            "kickoff_utc": str(info["kickoff_utc"]),
        }
        row_id = insert_forecast(
            conn,
            fixture_id=fixture_id,
            track="llm",
            model_version=f"glm:{result.model}",
            content_hash=_payload_hash(payload),
            payload=payload,
            issued_at=moment,
        )
    if row_id is not None:
        return INSERTED, row_id
    return KNOWN, None


def scout_targets(
    conn: sqlite3.Connection, *, now: str | None = None
) -> dict[int, Purpose]:
    """目标集合：彩池桥接场次（pool 用途）+ 在售竞彩 Tier1（scout 用途）。"""
    moment = now or utc_now_iso()
    targets: dict[int, Purpose] = {}
    for market_code in ("ttt14", "pick9"):
        for period in pool_store.list_pool_periods(conn, market_code):
            deadline = period["sales_deadline"]
            if deadline is None or str(deadline) < moment:
                continue
            for row in pool_store.pool_matches_for_period(conn, int(period["id"])):
                fixture_id = pool_store.match_fixture_id(
                    conn,
                    str(row["kickoff_utc"]),
                    str(row["home_team"]),
                    str(row["away_team"]),
                )
                if fixture_id is not None:
                    targets[fixture_id] = Purpose.POOL
    ref = datetime.fromisoformat(moment) if now else datetime.now(UTC)
    dates = [
        fx_store.beijing_business_date(ref + timedelta(days=offset))
        for offset in (-1, 0, 1)
    ]
    for fixture in fx_store.fixtures_for_business_dates(conn, dates):
        fixture_id = int(fixture["id"])
        info = fx_store.fixture_team_info(conn, fixture_id)
        if info is not None and str(info["competition_tier"]) == "tier1":
            targets.setdefault(fixture_id, Purpose.SCOUT)
    return targets


def scout_sweep(
    conn: sqlite3.Connection, settings: Settings, *, now: str | None = None
) -> ScoutStats:
    """全目标扫描（逐场 atomic：单场失败计次不中断扫描）。"""
    stats = ScoutStats()
    for fixture_id, purpose in sorted(scout_targets(conn, now=now).items()):
        stats.targets += 1
        try:
            status, _row_id = scout_fixture(
                conn, settings, fixture_id, purpose=purpose, now=now
            )
        except Exception as exc:
            stats.call_failed += 1
            logger.warning("scout 失败 fixture={}: {}", fixture_id, exc)
            continue
        if status == INSERTED:
            stats.inserted += 1
        elif status == KNOWN:
            stats.skipped_known += 1
        else:
            stats.parse_failed += 1
    return stats


def scout_stats_dict(stats: ScoutStats) -> dict[str, object]:
    """统计转字典（flow 日志/CLI 输出用）。"""
    return {
        "targets": stats.targets,
        "inserted": stats.inserted,
        "skipped_known": stats.skipped_known,
        "parse_failed": stats.parse_failed,
        "call_failed": stats.call_failed,
    }
