"""验证 API（票 31；票 34 重订边界）：HTTP 映射层，规则在 evaluation/validation。"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from goalx_backend.api.deps import get_db
from goalx_backend.evaluation import baseline
from goalx_backend.evaluation import forward_validation as fwd
from goalx_backend.evaluation.validation import (
    BacktestRunDetailView,
    BacktestRunView,
    ValidationProgressView,
    backtest_run_details,
    validation_progress,
)
from goalx_backend.evaluation.validation import list_backtest_runs as list_runs
from goalx_backend.llm import m3_report

router = APIRouter(tags=["validation"])
DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


@router.get(
    "/api/v1/backtest/runs",
    summary="回测 run 列表",
    response_model=list[BacktestRunView],
)
async def list_backtest_runs(db: DbDep) -> list[BacktestRunView]:
    """全部回测 run（新→旧），附 overall 指标摘要。"""
    return list_runs(db)


@router.get(
    "/api/v1/backtest/runs/{run_id}",
    summary="回测 run 详情(分层指标)",
    response_model=BacktestRunDetailView,
    responses={404: {"description": "run 不存在"}},
)
async def get_backtest_run(run_id: int, db: DbDep) -> BacktestRunDetailView:
    """一个 run 的全部 scope 指标（overall/联赛/赛季/玩法下注侧）。"""
    detail = backtest_run_details(db, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="run not found")
    return detail


@router.get(
    "/api/v1/validation/progress",
    summary="纸面三条件进度、mode 隔离计数与前瞻评分",
    response_model=ValidationProgressView,
)
async def get_validation_progress(
    db: DbDep,
    yield_mode: Annotated[
        str, Query(pattern="^(paper|live)$", description="收益曲线 mode")
    ] = "paper",
) -> ValidationProgressView:
    """
    验证页主数据（票 34 边界）：

    - CLV beat：唯一纸面注（单关/2串1 分开报告）≥200 且 beat ≥60%；
    - 市场 skill：前瞻评分集合（冻结赛前 Forecast + 同期市场基准），
      不读取任何历史回测 run；
    - 复核：无记录 = 未评估（不做真空通过）；
    - 整赛季：独立显示，未验收前不通过。
    """
    return validation_progress(db, yield_mode=yield_mode)


@router.get(
    "/api/v1/validation/forward-skill",
    summary="前瞻评分集合分组报告",
    response_model=dict[str, Any],
)
async def get_forward_skill(db: DbDep) -> dict[str, Any]:
    """冻结赛前 Forecast × 同期市场基准的分组 skill 与排除分母（票 34）。"""
    return fwd.forward_skill_report(db)


@router.get(
    "/api/v1/validation/m3-protocol",
    summary="M3 评测协议报告(三轨参考列+两档冻结阈值)",
    response_model=dict[str, Any],
)
async def get_m3_protocol(db: DbDep) -> dict[str, Any]:
    """
    三列（ml/llm/fused）前瞻评分 + 配对 RPS/DM + Tier A/B 达标状态行。

    （票 05 冻结阈值，票 13 落库）。LLM/Fused 永远是参考列——真钱资格
    三条件只读 ML 轨（票 04 冻结）。
    """
    return m3_report.m3_protocol_report(db)


@router.get(
    "/api/v1/backtest/baseline-quality",
    summary="历史基准分期/来源质检",
    response_model=dict[str, Any],
)
async def get_baseline_quality(db: DbDep) -> dict[str, Any]:
    """PSC/AvgC 按 2025-07-23 切期的数量/缺失/overround/两源差异（票 34）。"""
    return baseline.baseline_quality_report(db)
