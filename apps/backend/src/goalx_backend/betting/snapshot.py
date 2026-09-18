"""
注级 EV 概率快照（票 41）：建注锁定时刻的双口径概率与 EV 双存。

- 共识口径：锁定时点前最新欧赔完整三向共识的 Shin 概率
  （quote_evidence.eu_consensus_asof，与今日页/研究页同源同口径）；
- 模型口径：锁定时点已发出的最新赛前 Forecast 的 DC 三向概率
  （latest_forecast_asof，与前瞻冻结同语义——之后的预测不进快照）；
- EV = 联合概率 × 联合锁定赔率 − 1：单关即腿值；串关按联合概率与
  联合赔率（两腿独立假设已声明，与 CLV 票级口径一致）；
- 范围：只对全部腿均为 had 的注落快照（v1 可映射口径，与 CLV 同界）；
  非 had 腿与不可得口径一律存 None，不倒填、不伪造。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from goalx_backend.data import quote_evidence
from goalx_backend.db import utc_now_iso
from goalx_backend.modelling.forecast import (
    forecast_matrix_from_payload,
    latest_forecast_asof,
)
from goalx_backend.models import LegInput

_PROB_DECIMALS = 6


@dataclass(frozen=True)
class BetEvSnapshot:
    """一注锁定时刻的 EV 快照（双存；任一口径缺失为 None）。"""

    prob_consensus: float | None
    prob_model: float | None
    ev_consensus: float | None
    ev_model: float | None

    @property
    def present(self) -> bool:
        """任一口径有值即视为有快照（历史注/口径外注为全 None）。"""
        return any(
            value is not None
            for value in (
                self.prob_consensus,
                self.prob_model,
                self.ev_consensus,
                self.ev_model,
            )
        )


EMPTY_SNAPSHOT = BetEvSnapshot(
    prob_consensus=None, prob_model=None, ev_consensus=None, ev_model=None
)


def _model_had_probs_asof(
    conn: sqlite3.Connection, fixture_id: int, as_of: str
) -> dict[str, float] | None:
    """as_of 已发出的最新 Forecast 三向概率（老 payload 从矩阵重推）。"""
    forecast = latest_forecast_asof(conn, fixture_id, as_of)
    if forecast is None:
        return None
    payload = json.loads(str(forecast["payload"]))
    had = payload.get("had_probs")
    if had is None and "matrix" in payload:
        had = forecast_matrix_from_payload(payload).had()
    if not had:
        return None
    return {sel: float(had[sel]) for sel in ("h", "d", "a")}


def _joint_probability(
    legs: list[LegInput], probs_by_fixture: dict[int, dict[str, float] | None]
) -> float | None:
    """串关联合概率 = 各腿概率连乘（独立假设）；任一腿缺概率 → None。"""
    joint = 1.0
    for leg in legs:
        probs = probs_by_fixture.get(leg.fixture_id)
        if probs is None or leg.selection_code not in probs:
            return None
        joint *= probs[leg.selection_code]
    return joint


def _round(value: float | None) -> float | None:
    """None 直通；数值按共识/研究页展示精度（6 位小数）落库。"""
    return None if value is None else round(value, _PROB_DECIMALS)


def build_ev_snapshot(
    conn: sqlite3.Connection, legs: list[LegInput], *, as_of: str | None = None
) -> BetEvSnapshot:
    """
    建注时点的注级 EV 快照（票 41，双存裁决）。

    非 had 腿（v1 可映射口径之外）返回空快照——与 CLV 的可映射范围同界，
    扩市场时两处一起扩。串关同场多腿在建注链路已被竞彩规则拒绝，这里
    不再防御。
    """
    if not legs or any(leg.market_code != "had" for leg in legs):
        return EMPTY_SNAPSHOT
    moment = as_of or utc_now_iso()
    fixture_ids = sorted({leg.fixture_id for leg in legs})
    consensus_by_fixture = {
        fixture_id: quote_evidence.eu_consensus_asof(conn, fixture_id, moment)
        for fixture_id in fixture_ids
    }
    model_by_fixture = {
        fixture_id: _model_had_probs_asof(conn, fixture_id, moment)
        for fixture_id in fixture_ids
    }
    joint_odds = 1.0
    for leg in legs:
        joint_odds *= leg.locked_odds
    consensus_joint = _joint_probability(legs, consensus_by_fixture)
    model_joint = _joint_probability(legs, model_by_fixture)
    return BetEvSnapshot(
        prob_consensus=_round(consensus_joint),
        prob_model=_round(model_joint),
        ev_consensus=_round(
            consensus_joint * joint_odds - 1.0 if consensus_joint is not None else None
        ),
        ev_model=_round(
            model_joint * joint_odds - 1.0 if model_joint is not None else None
        ),
    )
