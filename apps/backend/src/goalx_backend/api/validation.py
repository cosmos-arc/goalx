"""验证 API（票 31；票 34 重订边界）：前瞻成绩、mode 隔离与看板数据。"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from goalx_backend import baseline
from goalx_backend import clv as clv_mod
from goalx_backend import forward_validation as fwd
from goalx_backend.api.deps import get_db

router = APIRouter(tags=["validation"])
DbDep = Annotated[sqlite3.Connection, Depends(get_db)]

CLV_MIN_BETS = 200  # 纸面三条件之一：≥200 唯一注才评估 beat rate（票 10/34）
CLV_BEAT_TARGET = 0.60
ROLLING_WINDOW = 100  # 滚动 yield 窗口（票 12）
MIN_FORWARD_SAMPLES = fwd.MIN_GROUP_SAMPLES


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


class ModeCounts(BaseModel):
    """一个 mode 的注/腿/场次计数（唯一 Bet 为验证分母，票 34）。"""

    bets: int
    unique_bets: int
    legs: int
    fixtures: int
    staked: float
    profit: float


class ValidationProgressView(BaseModel):
    """验证页数据：三条件进度 + mode 隔离计数/收益 + CLV + 前瞻评分。"""

    conditions: list[ConditionProgress]
    paper: ModeCounts
    live: ModeCounts
    unpurchased_open: int
    yield_curve: list[YieldPoint]
    yield_curve_mode: str
    clv: dict[str, Any]
    forward: dict[str, Any]
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


def _settled_bet_rows(
    db: sqlite3.Connection, mode: str
) -> tuple[list[sqlite3.Row], int]:
    """某 mode 已结算已购注（stable 排序）+ 被决策身份去重合并的重复数。"""
    rows = db.execute(
        """
        SELECT b.*, GROUP_CONCAT(
            l.fixture_id || ':' || l.market_code || ':' || l.selection_code
            || ':' || l.locked_odds, '|'
        ) AS decision_legs
        FROM bets b
        LEFT JOIN bet_legs l ON l.bet_id = b.id
        WHERE b.status IN ('won', 'lost', 'void')
          AND b.market_kind = 'fixed' AND b.purchased = 1 AND b.mode = ?
        GROUP BY b.id
        ORDER BY b.settled_at, b.id
        """,
        (mode,),
    ).fetchall()
    seen: dict[tuple[str, tuple[str, ...], str], sqlite3.Row] = {}
    duplicates = 0
    for row in rows:
        key = (
            str(row["mode"]),
            tuple(sorted(str(row["decision_legs"] or "").split("|"))),
            str(row["placed_at"] or ""),
        )
        if key in seen:
            duplicates += 1
        else:
            seen[key] = row
    deduped = sorted(seen.values(), key=lambda r: (str(r["settled_at"]), int(r["id"])))
    return deduped, duplicates


def _mode_counts(db: sqlite3.Connection, mode: str) -> ModeCounts:
    """Mode 计数：唯一 Bet（验证分母）与原始 Bet/腿/场次分列（票 34）。"""
    deduped, duplicates = _settled_bet_rows(db, mode)
    raw = len(deduped) + duplicates
    fixtures: set[int] = set()
    legs = 0
    for row in deduped:
        for part in str(row["decision_legs"] or "").split("|"):
            if part:
                legs += 1
                fixtures.add(int(part.split(":")[0]))
    return ModeCounts(
        bets=raw,
        unique_bets=len(deduped),
        legs=legs,
        fixtures=len(fixtures),
        staked=round(sum(float(r["stake"]) for r in deduped), 2),
        profit=round(sum(float(r["profit"] or 0.0) for r in deduped), 2),
    )


def _yield_curve(rows: list[sqlite3.Row]) -> list[YieldPoint]:
    """唯一注的累计与滚动 yield 曲线（票 34：滚动窗口用唯一纸面 Bet）。"""
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
        window_stake = sum(s for s, _ in window)
        rolling = sum(p for _, p in window) / window_stake if window_stake > 0 else None
        points.append(
            YieldPoint(
                index=i,
                cumulative_yield=profit / staked if staked > 0 else 0.0,
                rolling_yield=rolling,
            )
        )
    return points


def _season_condition(db: DbDep) -> ConditionProgress:
    """整赛季条件：独立显示；缺真实整赛季证据前恒未完成（票 34 验收 2）。"""
    rows, _ = _settled_bet_rows(db, "paper")
    kickoffs = [str(r["placed_at"] or r["created_at"])[:10] for r in rows]
    span = f"{min(kickoffs)}~{max(kickoffs)}" if kickoffs else "无样本"
    return ConditionProgress(
        key="full_season",
        label="整赛季纸面样本覆盖",
        achieved=False,
        current=f"{len(rows)} 唯一注 / {span}(覆盖未验收)",
        target="一个完整销售赛季的窗口覆盖",
    )


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
    clv_report = clv_mod.clv_report(db)
    paper_clv_bets = (
        clv_report["singles"]["paper"]["n_bets"]
        + clv_report["parlay2"]["paper"]["n_bets"]
    )
    paper_group = [
        g
        for g in (clv_report["singles"]["paper"], clv_report["parlay2"]["paper"])
        if g["n_bets"]
    ]
    beats = sum(
        round(g["beat_rate"] * g["n_bets"]) if g["beat_rate"] is not None else 0
        for g in paper_group
    )
    paper_beat = beats / paper_clv_bets if paper_clv_bets else None
    clv_ok = (
        paper_clv_bets >= CLV_MIN_BETS
        and paper_beat is not None
        and paper_beat >= CLV_BEAT_TARGET
    )
    conditions = [
        ConditionProgress(
            key="clv_beat",
            label=f"纸面 CLV beat ≥60% 且 ≥{CLV_MIN_BETS} 唯一注",
            achieved=clv_ok,
            current=(
                f"beat={paper_beat:.1%} @ {paper_clv_bets} 唯一注"
                if paper_beat is not None
                else f"@ {paper_clv_bets} 唯一注"
            ),
            target=f"≥60% @ ≥{CLV_MIN_BETS} 唯一注",
        )
    ]

    forward = fwd.forward_skill_report(db)
    eligible = [
        (version, metrics)
        for version, metrics in forward["groups"].items()
        if not metrics.get("insufficient_samples")
    ]
    best = max((m["skill_rps"] for _, m in eligible), default=None)
    skill_ok = best is not None and best >= 0.0
    group_summary = (
        "; ".join(
            f"{v}: n={m['n']} skill={m['skill_rps']:+.4f}"
            + ("(样本不足)" if m.get("insufficient_samples") else "")
            for v, m in forward["groups"].items()
        )
        or "无前瞻样本"
    )
    conditions.append(
        ConditionProgress(
            key="market_skill",
            label=f"前瞻对市场 skill ≥ 0 (RPS, ≥{MIN_FORWARD_SAMPLES} 场)",
            achieved=skill_ok,
            current=group_summary,
            target="≥ 0",
        )
    )
    conditions.append(
        ConditionProgress(
            key="review_errors",
            label="复核无系统性错误",
            achieved=False,
            current="无复核记录(未评估)",
            target="无系统性错误",
        )
    )
    conditions.append(_season_condition(db))

    paper_rows, _ = _settled_bet_rows(db, "paper")
    live_rows, _ = _settled_bet_rows(db, "live")
    unpurchased = db.execute(
        """
        SELECT COUNT(*) AS c FROM bets WHERE purchased = 0
          AND status = 'open' AND market_kind = 'fixed'
        """
    ).fetchone()["c"]
    run_row = db.execute(
        """
        SELECT * FROM backtest_runs WHERE status = 'done'
        ORDER BY created_at DESC, id DESC LIMIT 1
        """
    ).fetchone()
    latest_view: BacktestRunView | None = (
        _run_view(run_row, _overall_metrics(db, int(run_row["id"])))
        if run_row is not None
        else None
    )
    return ValidationProgressView(
        conditions=conditions,
        paper=_mode_counts(db, "paper"),
        live=_mode_counts(db, "live"),
        unpurchased_open=int(unpurchased),
        yield_curve=_yield_curve(paper_rows if yield_mode == "paper" else live_rows),
        yield_curve_mode=yield_mode,
        clv=clv_report,
        forward=forward,
        latest_run=latest_view,
    )


@router.get(
    "/api/v1/validation/forward-skill",
    summary="前瞻评分集合分组报告",
    response_model=dict[str, Any],
)
async def get_forward_skill(db: DbDep) -> dict[str, Any]:
    """冻结赛前 Forecast × 同期市场基准的分组 skill 与排除分母（票 34）。"""
    return fwd.forward_skill_report(db)


@router.get(
    "/api/v1/backtest/baseline-quality",
    summary="历史基准分期/来源质检",
    response_model=dict[str, Any],
)
async def get_baseline_quality(db: DbDep) -> dict[str, Any]:
    """PSC/AvgC 按 2025-07-23 切期的数量/缺失/overround/两源差异（票 34）。"""
    return baseline.baseline_quality_report(db)
