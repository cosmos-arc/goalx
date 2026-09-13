"""验证 API（票 31）：回测 run 列表/指标与纸面三条件进度看板数据。"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from goalx_backend import clv as clv_mod
from goalx_backend.api.deps import get_db

router = APIRouter(tags=["validation"])
DbDep = Annotated[sqlite3.Connection, Depends(get_db)]

CLV_MIN_BETS = 200  # 纸面三条件之一：≥200 注才评估 beat rate（票 10）
CLV_BEAT_TARGET = 0.60
ROLLING_WINDOW = 100  # 滚动 yield 窗口（票 12）


class BacktestRunView(BaseModel):
    """一行回测 run。"""

    id: int
    label: str
    status: str
    created_at: str
    finished_at: str | None = None
    summary: dict[str, Any] | None = None
    overall_metrics: dict[str, Any] | None = None


class BacktestRunDetailView(BacktestRunView):
    """run 详情：分层指标（overall/联赛/赛季/玩法）。"""

    metrics: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ConditionProgress(BaseModel):
    """纸面转真金三条件之一的进度（票 10）。"""

    key: str
    label: str
    achieved: bool
    current: str
    target: str


class YieldPoint(BaseModel):
    """累计/滚动收益曲线上的一个点。"""

    index: int
    cumulative_yield: float
    rolling_yield: float | None = None


class ValidationProgressView(BaseModel):
    """验证页数据：三条件进度 + 滚动收益 + CLV 摘要。"""

    conditions: list[ConditionProgress]
    settled_bets: int
    yield_curve: list[YieldPoint]
    clv: dict[str, Any]
    latest_run: BacktestRunView | None = None


def _run_view(row: sqlite3.Row, overall: dict[str, Any] | None) -> BacktestRunView:
    summary = json.loads(row["summary"]) if row["summary"] else None
    return BacktestRunView(
        id=int(row["id"]),
        label=str(row["label"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        finished_at=row["finished_at"],
        summary=summary,
        overall_metrics=overall,
    )


def _overall_metrics(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT metrics FROM backtest_metrics WHERE run_id = ? AND scope = 'overall'",
        (run_id,),
    ).fetchone()
    return dict(json.loads(row["metrics"])) if row else None


@router.get(
    "/api/v1/backtest/runs",
    summary="回测 run 列表",
    response_model=list[BacktestRunView],
)
async def list_backtest_runs(db: DbDep) -> list[BacktestRunView]:
    """全部回测 run（新→旧），附 overall 指标摘要。"""
    rows = db.execute(
        "SELECT * FROM backtest_runs ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return [_run_view(row, _overall_metrics(db, int(row["id"]))) for row in rows]


@router.get(
    "/api/v1/backtest/runs/{run_id}",
    summary="回测 run 详情(分层指标)",
    response_model=BacktestRunDetailView,
    responses={404: {"description": "run 不存在"}},
)
async def get_backtest_run(run_id: int, db: DbDep) -> BacktestRunDetailView:
    """一个 run 的全部 scope 指标（overall/联赛/赛季/玩法下注侧）。"""
    row = db.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    metrics_rows = db.execute(
        "SELECT scope, metrics FROM backtest_metrics WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    metrics = {str(m["scope"]): dict(json.loads(m["metrics"])) for m in metrics_rows}
    view = _run_view(row, metrics.get("overall"))
    return BacktestRunDetailView(**view.model_dump(), metrics=metrics)


def _yield_curve(db: sqlite3.Connection) -> list[YieldPoint]:
    """已结算 paper/live 注的累计与滚动 100 注 yield 曲线。"""
    rows = db.execute(
        """
        SELECT stake, profit FROM bets
        WHERE status IN ('won', 'lost', 'void') AND market_kind = 'fixed'
        ORDER BY settled_at, id
        """
    ).fetchall()
    points: list[YieldPoint] = []
    staked = 0.0
    profit = 0.0
    window: list[tuple[float, float]] = []
    for i, row in enumerate(rows, start=1):
        stake, bet_profit = float(row["stake"]), float(row["profit"] or 0.0)
        staked += stake
        profit += bet_profit
        window.append((stake, bet_profit))
        if len(window) > ROLLING_WINDOW:
            window.pop(0)
        rolling = (
            sum(p for _, p in window) / sum(s for s, _ in window)
            if sum(s for s, _ in window) > 0
            else None
        )
        points.append(
            YieldPoint(
                index=i,
                cumulative_yield=profit / staked if staked > 0 else 0.0,
                rolling_yield=rolling,
            )
        )
    return points


@router.get(
    "/api/v1/validation/progress",
    summary="纸面三条件进度与滚动收益",
    response_model=ValidationProgressView,
)
async def get_validation_progress(db: DbDep) -> ValidationProgressView:
    """
    验证页主数据：CLV beat / 市场 skill / 复核零错误三条件 + 滚动 yield。

    - CLV：来自已对账 clv_records（票 32）；
    - skill：最新回测 run 的 overall RPS skill（票 29）；
    - 复核系统性错误：M3 复核队列落地前恒为「无记录」空态。
    """
    clv_report = clv_mod.clv_report(db)
    n_clv = int(clv_report["n_records"])
    beat = clv_report["beat_rate_overall"]
    clv_ok = n_clv >= CLV_MIN_BETS and beat is not None and beat >= CLV_BEAT_TARGET
    conditions = [
        ConditionProgress(
            key="clv_beat",
            label=f"CLV beat rate ≥60% 且 ≥{CLV_MIN_BETS} 注",
            achieved=clv_ok,
            current=(
                f"beat={beat:.1%} @ {n_clv} 注" if beat is not None else f"@ {n_clv} 注"
            ),
            target=f"≥60% @ ≥{CLV_MIN_BETS} 注",
        ),
    ]
    run_row = db.execute(
        """
        SELECT * FROM backtest_runs WHERE status = 'done'
        ORDER BY created_at DESC, id DESC LIMIT 1
        """
    ).fetchone()
    latest_view: BacktestRunView | None = None
    if run_row is not None:
        latest_view = _run_view(run_row, _overall_metrics(db, int(run_row["id"])))
        overall = latest_view.overall_metrics or {}
        skill = overall.get("skill_rps")
        conditions.append(
            ConditionProgress(
                key="market_skill",
                label="对市场 skill ≥ 0 (RPS)",
                achieved=skill is not None and float(skill) >= 0.0,
                current=f"skill={float(skill):+.4f}" if skill is not None else "无回测",
                target="≥ 0",
            )
        )
    else:
        conditions.append(
            ConditionProgress(
                key="market_skill",
                label="对市场 skill ≥ 0 (RPS)",
                achieved=False,
                current="无回测",
                target="≥ 0",
            )
        )
    conditions.append(
        ConditionProgress(
            key="review_errors",
            label="复核无系统性错误",
            achieved=True,
            current="无记录(M3 前空态)",
            target="无系统性错误",
        )
    )
    settled = db.execute(
        "SELECT COUNT(*) AS c FROM bets WHERE status IN ('won','lost','void')"
    ).fetchone()["c"]
    return ValidationProgressView(
        conditions=conditions,
        settled_bets=int(settled),
        yield_curve=_yield_curve(db),
        clv=clv_report,
        latest_run=latest_view,
    )
