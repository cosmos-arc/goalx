"""
LEAP 三项层融合（票 12，ADR-0009）。

log-pool（几何加权）：``p_fused ∝ p_ml^w × p_llm^(1-w)`` 后归一，
w 取 ML 权重（缺省 0.5，票 12 定案）。融合产物落
``forecasts(track='fused')``，payload 引用双源 forecast id——独立
工件，绝不覆写 ml/llm 行（票 04 冻结）；**融合线不建注**。

无 LLM forecast 的场次不产 fused（宁缺毋假）；任一源更新后 content
hash 变化自然触发再融合。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from goalx_backend.config import Settings
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.llm.gate import latest_triple
from goalx_backend.modelling.forecast import (
    forecasts_for_track,
    insert_forecast,
    latest_forecast_asof,
)

_EPS = 1e-9  # 零概率钳制（log-pool 定义域保护）


@dataclass
class FusionStats:
    """一次融合扫描的计数。"""

    pairs: int = 0
    inserted: int = 0
    skipped_known: int = 0


def log_pool(
    p_ml: tuple[float, float, float],
    p_llm: tuple[float, float, float],
    w_ml: float,
) -> tuple[float, float, float]:
    """几何加权池化 + 归一（w_ml=1 退化为 ML，0 退化为 LLM）。"""
    pooled = [
        (max(p, _EPS) ** w_ml) * (max(q, _EPS) ** (1 - w_ml))
        for p, q in zip(p_ml, p_llm, strict=True)
    ]
    total = sum(pooled)
    return tuple(x / total for x in pooled)


def _latest_forecast_id(
    conn: sqlite3.Connection, fixture_id: int, track: str, as_of: str
) -> int | None:
    """as_of 时点最新一轨工件 id（融合产物的 sources 引用）。"""
    row = latest_forecast_asof(conn, fixture_id, as_of, track=track)
    return int(row["id"]) if row else None


def fuse_fixture(
    conn: sqlite3.Connection,
    settings: Settings,
    fixture_id: int,
    *,
    now: str | None = None,
) -> tuple[str, int | None]:
    """
    一场 → log-pool 融合 → forecasts(track='fused')。

    返回 (inserted/known/skipped, 新行 id)；无任一源返回 (skipped, None)。
    """
    moment = now or utc_now_iso()
    ml = latest_triple(conn, fixture_id, "ml")
    llm = latest_triple(conn, fixture_id, "llm")
    if ml is None or llm is None:
        return "skipped", None
    ml_id = _latest_forecast_id(conn, fixture_id, "ml", moment)
    llm_id = _latest_forecast_id(conn, fixture_id, "llm", moment)
    h, d, a = log_pool(ml, llm, settings.fusion_ml_weight)
    payload: dict[str, object] = {
        "h": h,
        "d": d,
        "a": a,
        "method": "log_pool",
        "w_ml": settings.fusion_ml_weight,
        "sources": {"ml": ml_id, "llm": llm_id},
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with atomic(conn):
        row_id = insert_forecast(
            conn,
            fixture_id=fixture_id,
            track="fused",
            model_version=f"leap:w={settings.fusion_ml_weight}",
            content_hash=content_hash,
            payload=payload,
            issued_at=moment,
        )
    if row_id is not None:
        return "inserted", row_id
    return "known", None


def fusion_sweep(
    conn: sqlite3.Connection, settings: Settings, *, now: str | None = None
) -> FusionStats:
    """全量双轨齐备场次融合（源更新 → hash 变 → 自然再融合）。"""
    stats = FusionStats()
    ml_fixtures = {int(r["fixture_id"]) for r in forecasts_for_track(conn, "ml")}
    fixture_ids = sorted(
        {
            int(r["fixture_id"])
            for r in forecasts_for_track(conn, "llm")
            if int(r["fixture_id"]) in ml_fixtures
        }
    )
    for fixture_id in fixture_ids:
        stats.pairs += 1
        status, _row_id = fuse_fixture(conn, settings, fixture_id, now=now)
        if status == "inserted":
            stats.inserted += 1
        elif status == "known":
            stats.skipped_known += 1
        else:
            stats.pairs -= 1  # 无源不计配对
    return stats


def fusion_stats_dict(stats: FusionStats) -> dict[str, object]:
    """统计转字典（flow 日志/CLI 输出用）。"""
    return {
        "pairs": stats.pairs,
        "inserted": stats.inserted,
        "skipped_known": stats.skipped_known,
    }
