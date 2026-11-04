"""验证 API（票 31；票 34 重订边界）：HTTP 映射层，规则在 evaluation/validation。"""

from __future__ import annotations

import sqlite3
from typing import Annotated

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query

from goalx_backend.api.deps import get_db, get_research_db
from goalx_backend.config import get_settings
from goalx_backend.data import corpus_duckdb
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct_asof
from goalx_backend.evaluation import baseline
from goalx_backend.evaluation import forward_validation as fwd
from goalx_backend.evaluation.baseline import BaselineQualityReport
from goalx_backend.evaluation.forward_validation import ForwardSkillReport
from goalx_backend.evaluation.validation import (
    BacktestRunDetailView,
    BacktestRunView,
    ValidationProgressView,
    backtest_run_details,
    validation_progress,
)
from goalx_backend.evaluation.validation import list_backtest_runs as list_runs
from goalx_backend.llm import m3_report
from goalx_backend.llm.m3_report import M3ProtocolReport
from goalx_backend.llm.review import verdict_counts as review_verdict_counts

router = APIRouter(tags=["validation"])
DbDep = Annotated[sqlite3.Connection, Depends(get_db)]
# 回测表读面走研究库（票 07②：与运行面库分文件）
ResearchDbDep = Annotated[sqlite3.Connection, Depends(get_research_db)]


def _asof_batch(
    duck_con: duckdb.DuckDBPyConnection | None,
) -> fwd.AsOfBatch | None:
    """票 05：青铜重放批解析器（corpus 缺席 None=无重放面，维持旧口径）。"""
    if duck_con is None:
        return None
    return srct_asof.batch_asof_resolver(
        CorpusStore(get_settings().corpus_root), duck_con
    )


@router.get(
    "/api/v1/backtest/runs",
    summary="回测 run 列表",
    response_model=list[BacktestRunView],
)
async def list_backtest_runs(db: ResearchDbDep) -> list[BacktestRunView]:
    """全部回测 run（新→旧），附 overall 指标摘要（读研究库）。"""
    return list_runs(db)


@router.get(
    "/api/v1/backtest/runs/{run_id}",
    summary="回测 run 详情(分层指标)",
    response_model=BacktestRunDetailView,
    responses={404: {"description": "run 不存在"}},
)
async def get_backtest_run(run_id: int, db: ResearchDbDep) -> BacktestRunDetailView:
    """一个 run 的全部 scope 指标（overall/联赛/赛季/玩法下注侧;研究库）。"""
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
    research_db: ResearchDbDep,
    yield_mode: Annotated[
        str, Query(pattern="^(paper|live)$", description="收益曲线 mode")
    ] = "paper",
) -> ValidationProgressView:
    """
    验证页主数据（票 34 边界）：

    - CLV beat：唯一纸面注（单关/2串1 分开报告）≥200 且 beat ≥60%；
    - 市场 skill：前瞻评分集合（冻结赛前 Forecast + 同期市场基准），
      不读取任何历史回测 run，只认部署版本（票 09 裁决）；
    - 复核：误导率 ≤10% 且 ≥10 条（counts 在 api 层取——evaluation 不引 llm）；
    - 整赛季：独立显示，未验收前不通过。
    """
    with corpus_duckdb.reader() as duck_con:
        return validation_progress(
            db,
            research_conn=research_db,
            yield_mode=yield_mode,
            review_counts=review_verdict_counts(db),
            duck_con=duck_con,
            asof_batch=_asof_batch(duck_con),
        )


@router.get(
    "/api/v1/validation/forward-skill",
    summary="前瞻评分集合分组报告",
    response_model=ForwardSkillReport,
)
async def get_forward_skill(db: DbDep) -> ForwardSkillReport:
    """
    冻结赛前 Forecast × 同期市场基准的分组 skill 与排除分母（票 34）。

    票 05：市场基准接 corpus 行源（srct 链场次 corpus 唯一真值 + 历史
    时点青铜重放；无语料环境自动降级 legacy odds_snapshots 口径）。
    """
    with corpus_duckdb.reader() as duck_con:
        return fwd.forward_skill_report(
            db, duck_con=duck_con, asof_batch=_asof_batch(duck_con)
        )


@router.get(
    "/api/v1/validation/m3-protocol",
    summary="M3 评测协议报告(三轨参考列+两档冻结阈值)",
    response_model=M3ProtocolReport,
)
async def get_m3_protocol(db: DbDep) -> M3ProtocolReport:
    """
    三列（ml/llm/fused）前瞻评分 + 配对 RPS/DM + Tier A/B 达标状态行。

    （票 05 冻结阈值，票 13 落库）。LLM/Fused 永远是参考列——真钱资格
    三条件只读 ML 轨（票 04 冻结）。

    票 03 判词语义：Tier A 的 ECE 检查为三键按样本数（场次数）加权真算（原桩恒 None
    已修）；Tier B 增方向核验——判词三态：达标候选（须用户显式裁决
    切换）/ 未达标 / 方向反向（融合显著更差）——未达标（DM 双侧只测
    有差异，显著更差不再误判达标候选）。
    """
    with corpus_duckdb.reader() as duck_con:
        return m3_report.m3_protocol_report(
            db, duck_con=duck_con, asof_batch=_asof_batch(duck_con)
        )


@router.get(
    "/api/v1/backtest/baseline-quality",
    summary="历史基准分期/来源质检",
    response_model=BaselineQualityReport,
)
async def get_baseline_quality(db: DbDep) -> BaselineQualityReport:
    """PSC/AvgC 按 2025-07-23 切期的数量/缺失/overround/两源差异（票 34）。"""
    return baseline.baseline_quality_report(db)
