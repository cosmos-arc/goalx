"""
gate 路由 + analyst 复核（票 11）。

gate：最新 LLM 三项 vs 最新 ML 三项的 JS 散度（base-2 对称），记既有
``divergences`` 表（票 03 复用定案，SQL 归 llm 域）。阈值沿用旧图：
>0.02 记为分歧；>0.06 且 Tier1 → 入复核队列（``review_items``，
route=pre_match）并触发 analyst。

analyst = GLM-5.3：复核产出**追加**新 forecasts(track='llm') 行
（payload 带 revision_of 指向 scout 工件哈希），不改写 scout 行——
追加语义（票 11 验收）。复核结论只进评测集，不改任何预测工件。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from loguru import logger
from openai.types.chat import ChatCompletionMessageParam

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.llm.client import ModelTier, Purpose, glm_chat
from goalx_backend.llm.scout import INSERTED, KNOWN, PARSE_FAILED, parse_probs
from goalx_backend.llm.store import intel_for_fixture
from goalx_backend.modelling.forecast import (
    forecast_matrix_from_payload,
    forecasts_for_track,
    insert_forecast,
    latest_forecast_asof,
)

_JS_RECORD_THRESHOLD = 0.02  # 分歧标记线（旧图票 09 定案）
_JS_ROUTE_THRESHOLD = 0.06  # analyst 路由线（Tier1）
_METRIC = "js_had_ml_llm"


@dataclass
class GateStats:
    """一次 gate 扫描的计数。"""

    pairs: int = 0  # 双轨齐备的场次
    divergent: int = 0  # JS > 0.02
    routed: int = 0  # JS > 0.06 且 Tier1 → analyst
    analyst_inserted: int = 0
    analyst_known: int = 0
    analyst_parse_failed: int = 0
    analyst_call_failed: int = 0


def js_divergence(p: Sequence[float], q: Sequence[float]) -> float:
    """Jensen-Shannon 散度（base 2，对称半量）——两项同长分布。"""
    total = 0.0
    for pi, qi in zip(p, q, strict=True):
        m = (pi + qi) / 2
        if pi > 0:
            total += pi / 2 * _log2(pi / m)
        if qi > 0:
            total += qi / 2 * _log2(qi / m)
    return total


def _log2(x: float) -> float:
    return math.log2(x)


def _latest_triple(
    conn: sqlite3.Connection, fixture_id: int, track: str
) -> tuple[float, float, float] | None:
    """
    最新一项轨道预测的三项。

    ml 轨 payload 是比分矩阵（ADR 0006 canonical）——经 had() 推边际；
    llm 轨 payload 直接携带 h/d/a（票 10 三项定案）。无预测/坏 payload
    返回 None。
    """
    row = latest_forecast_asof(conn, fixture_id, utc_now_iso(), track=track)
    if row is None:
        return None
    payload = json.loads(str(row["payload"]))
    if track == "ml":
        try:
            had = forecast_matrix_from_payload(payload).had()
            return (had["h"], had["d"], had["a"])
        except (KeyError, TypeError, ValueError):
            return None
    try:
        return (float(payload["h"]), float(payload["d"]), float(payload["a"]))
    except (KeyError, TypeError, ValueError):
        return None


def _record_divergence(
    conn: sqlite3.Connection, fixture_id: int, value: float, moment: str
) -> None:
    """Divergences 追加一行（append 日志，重复计算自然累积）。"""
    conn.execute(
        """
        INSERT INTO divergences
            (fixture_id, metric, reference, value, computed_at)
        VALUES (?, ?, 'ml', ?, ?)
        """,
        (fixture_id, _METRIC, value, moment),
    )


def _enqueue_review(
    conn: sqlite3.Connection, fixture_id: int, js_value: float, moment: str
) -> None:
    """赛前复核入队（幂等：同场次同路由只一条 open 项）。"""
    conn.execute(
        """
        INSERT OR IGNORE INTO review_items
            (fixture_id, route, js_value, status, created_at)
        VALUES (?, 'pre_match', ?, 'open', ?)
        """,
        (fixture_id, js_value, moment),
    )


def _analyst_messages(
    info: sqlite3.Row,
    intels: list[sqlite3.Row],
    ml: tuple[float, float, float],
    scout: tuple[float, float, float],
) -> Sequence[ChatCompletionMessageParam]:
    """复核对话：给出双轨概率与全部存证情报，要求裁决与修订。"""
    fmt = "胜 {:.3f} / 平 {:.3f} / 负 {:.3f}".format
    ml_line = "量化模型（ML，时间衰减 Dixon-Coles）三项概率：" + fmt(*ml)
    scout_line = "情报分析师（scout）三项概率：" + fmt(*scout)
    lines = [
        "场次：{} {} vs {}，开球 {}".format(
            info["competition_name"],
            info["home_name"],
            info["away_name"],
            info["kickoff_utc"],
        ),
        ml_line,
        scout_line,
        "两轨分歧显著，需要你复核。",
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
        lines.append("情报：本场无已存证情报。")
    contract = (
        "裁决哪一轨更可信（或给出修订），输出严格 JSON："
        '{"h": 修订胜概率, "d": 修订平概率, "a": 修订负概率,'
        ' "rationale": "不超过80字：倾向哪轨、依据哪条情报"}'
        "——h/d/a 为 0-1 且和为 1。只输出 JSON。"
    )
    lines.append(contract)
    return [
        cast(
            ChatCompletionMessageParam,
            {
                "role": "system",
                "content": (
                    "你是足球量化系统的高级分析师 analyst，负责裁决 ML 量化线与"
                    " LLM 情报线的显著分歧。证据边界：只引用给定情报，不得编造。"
                ),
            },
        ),
        cast(ChatCompletionMessageParam, {"role": "user", "content": "\n".join(lines)}),
    ]


def analyst_review(
    conn: sqlite3.Connection,
    settings: Settings,
    fixture_id: int,
    *,
    purpose: Purpose,
    scout_hash: str,
    now: str | None = None,
) -> tuple[str, int | None]:
    """Analyst 复核 → 追加新 forecasts(track='llm')（revision_of 引用 scout）。"""
    info = fx_store.fixture_team_info(conn, fixture_id)
    if info is None:
        return PARSE_FAILED, None
    moment = now or utc_now_iso()
    intels = intel_for_fixture(conn, fixture_id)
    ml = _latest_triple(conn, fixture_id, "ml") or (0.0, 0.0, 0.0)
    scout = _latest_triple(conn, fixture_id, "llm") or (0.0, 0.0, 0.0)
    with atomic(conn):
        result = glm_chat(
            conn,
            settings,
            ModelTier.ANALYST,
            _analyst_messages(info, intels, ml, scout),
            purpose=purpose,
            max_tokens=4000,
            temperature=0.2,
        )
        parsed = parse_probs(result.content)
        if parsed is None:
            logger.warning(
                "analyst 输出不可解析 fixture={}: {}", fixture_id, result.content[:80]
            )
            return PARSE_FAILED, None
        h, d, a, rationale = parsed
        payload: dict[str, object] = {
            "h": h,
            "d": d,
            "a": a,
            "rationale": rationale,
            "model": result.model,
            "analyst": True,
            "revision_of": scout_hash,
            "intel_count": len(intels),
            "intel_ids": [int(r["id"]) for r in intels],
        }
        row_id = insert_forecast(
            conn,
            fixture_id=fixture_id,
            track="llm",
            model_version=f"glm:{result.model}",
            content_hash=_hash_payload(payload),
            payload=payload,
            issued_at=moment,
        )
    if row_id is not None:
        return INSERTED, row_id
    return KNOWN, None


def _hash_payload(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def gate_sweep(
    conn: sqlite3.Connection, settings: Settings, *, now: str | None = None
) -> GateStats:
    """双轨 JS 散度全量扫描 + 路由复核（divergences/review_items 幂等追加）。"""
    stats = GateStats()
    moment = now or utc_now_iso()
    ml_fixtures = {int(r["fixture_id"]) for r in forecasts_for_track(conn, "ml")}
    fixture_ids = sorted(
        {
            int(r["fixture_id"])
            for r in forecasts_for_track(conn, "llm")
            if int(r["fixture_id"]) in ml_fixtures
        }
    )
    for fixture_id in fixture_ids:
        ml = _latest_triple(conn, fixture_id, "ml")
        scout = _latest_triple(conn, fixture_id, "llm")
        if ml is None or scout is None:
            continue
        stats.pairs += 1
        js = round(js_divergence(ml, scout), 6)
        info = fx_store.fixture_team_info(conn, fixture_id)
        tier1 = info is not None and str(info["competition_tier"]) == "tier1"
        with atomic(conn):
            _record_divergence(conn, fixture_id, js, moment)
        if js > _JS_RECORD_THRESHOLD:
            stats.divergent += 1
        if js > _JS_ROUTE_THRESHOLD and tier1:
            stats.routed += 1
            with atomic(conn):
                _enqueue_review(conn, fixture_id, js, moment)
            purpose = (
                Purpose.POOL
                if _is_pool_fixture(conn, fixture_id, moment)
                else Purpose.SCOUT
            )
            scout_id = _latest_scout_id(conn, fixture_id)
            try:
                status, _rid = analyst_review(
                    conn,
                    settings,
                    fixture_id,
                    purpose=purpose,
                    scout_hash=scout_id,
                    now=moment,
                )
            except Exception as exc:
                stats.analyst_call_failed += 1
                logger.warning("analyst 失败 fixture={}: {}", fixture_id, exc)
                continue
            if status == INSERTED:
                stats.analyst_inserted += 1
            elif status == KNOWN:
                stats.analyst_known += 1
            else:
                stats.analyst_parse_failed += 1
    return stats


def _latest_scout_id(conn: sqlite3.Connection, fixture_id: int) -> str:
    """最新 llm 轨工件 id（revision_of 引用目标，analyst 产出前即 scout 行）。"""
    row = latest_forecast_asof(conn, fixture_id, utc_now_iso(), track="llm")
    return str(row["id"]) if row else ""


def _is_pool_fixture(conn: sqlite3.Connection, fixture_id: int, moment: str) -> bool:
    """该场是否属于当期在售彩池（决定 analyst 的熔断用途档）。"""
    for market_code in ("ttt14", "pick9"):
        for period in pool_store.list_pool_periods(conn, market_code):
            deadline = period["sales_deadline"]
            if deadline is None or str(deadline) < moment:
                continue
            for row in pool_store.pool_matches_for_period(conn, int(period["id"])):
                if (
                    pool_store.match_fixture_id(
                        conn,
                        str(row["kickoff_utc"]),
                        str(row["home_team"]),
                        str(row["away_team"]),
                    )
                    == fixture_id
                ):
                    return True
    return False


def gate_stats_dict(stats: GateStats) -> dict[str, object]:
    """统计转字典（flow 日志/CLI 输出用）。"""
    return {
        "pairs": stats.pairs,
        "divergent": stats.divergent,
        "routed": stats.routed,
        "analyst_inserted": stats.analyst_inserted,
        "analyst_known": stats.analyst_known,
        "analyst_parse_failed": stats.analyst_parse_failed,
        "analyst_call_failed": stats.analyst_call_failed,
    }
