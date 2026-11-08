"""
准确率报告 API（auto-paper-20261106 票 02）：只读聚合面。

数据面在 evaluation/accuracy.build_accuracy_report（与晨检/覆盖账同源）；
本模块只留路由与 HTTP 形状。判读规则文字冻结随报告下发，前端原样展示。
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from goalx_backend.api.deps import get_db
from goalx_backend.evaluation.accuracy import build_accuracy_report

router = APIRouter(tags=["accuracy"])
DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


class CalibBinView(BaseModel):
    """一个校准箱：模型最热向置信度 vs 实际命中率。"""

    model_config = ConfigDict(from_attributes=True)

    bin_index: int
    n: int
    mean_conf: float
    hit_rate: float


class MarketRowView(BaseModel):
    """月×玩法×联赛 一行（competition=None 为该月该玩法总体行）。"""

    model_config = ConfigDict(from_attributes=True)

    month: str
    market: str
    competition: str | None
    metric_kind: str
    n: int
    skipped: int
    model_metric: float | None
    model_logloss: float | None
    jc_baseline: float | None
    eu_baseline: float | None
    diff_jc: float | None
    diff_jc_se: float | None
    diff_eu: float | None
    diff_eu_se: float | None
    n_baseline: int
    cum_n_baseline: int
    verdict_ready: bool
    calibration: list[CalibBinView]


class CoverageRowView(BaseModel):
    """覆盖率首行（与 forecast_coverage 同源）。"""

    model_config = ConfigDict(from_attributes=True)

    month: str
    total: int
    covered: int
    not_fd_league: int
    no_model: int
    no_mapping: int
    unknown: int


class MonthBlockView(BaseModel):
    """一个月块：覆盖首行 + 玩法行（总体行在前）。"""

    coverage: CoverageRowView
    rows: list[MarketRowView]


class AccuracyReportView(BaseModel):
    """前瞻准确率报告（prereg acc-v1 冻结口径）。"""

    reading_rule: str
    months: list[MonthBlockView]


@router.get(
    "/api/v1/accuracy/report",
    summary="前瞻准确率报告(月×玩法×联赛)",
    response_model=AccuracyReportView,
)
async def get_accuracy_report(db: DbDep) -> AccuracyReportView:
    """只读派生面：评分行聚合 + 覆盖账同源首行；判读规则文字随报告。"""
    report = build_accuracy_report(db)
    return AccuracyReportView(
        reading_rule=report.reading_rule,
        months=[
            MonthBlockView(
                coverage=CoverageRowView.model_validate(coverage),
                rows=[MarketRowView.model_validate(row) for row in rows],
            )
            for coverage, rows in report.months
        ],
    )
