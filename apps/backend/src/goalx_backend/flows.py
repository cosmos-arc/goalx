"""
Prefect flows（ADR 0005）：定时采集、历史导入、训练与结算批跑。

流程体是 goalx_backend.tasks 的薄 adapter——任务实现只有一份，cli 与
flows 共用（票 35 证据契约因此不可能再漂移）；deployment 由本地
Prefect server 调度（见 README「运行采集」）。

- jingcai_snapshot_flow：竞彩全玩法快照（销售期高频，如每 30 分钟）
- eu_odds_snapshot_flow：欧赔快照 + join + credit 记账（均匀轮询；
  kickoff −30/−10/−1min 窗口加密在部署侧以更细 cron 间隔实现，见 README）
- eu_odds_closing_flow：收盘窗口快照（票 32/35）
- fd_history_import_flow：历史底座一次性导入（幂等可重跑）
- weekly_train_flow：DC 分池周训练（Tier1 五大，票 26）
- forecast_daily_flow：每日在售场次 ML Forecast 生成（票 27）
- settlement_flow：开奖后结算批跑（每日数次）
- draw_results_sync_flow：源D 结果页赛果自动同步（票 42）
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from loguru import logger
from prefect import flow

from goalx_backend import tasks
from goalx_backend.betting.ledger_audit import audit_ledger
from goalx_backend.data.ingest.zucai import stats_dict
from goalx_backend.evaluation import clv as clv_mod
from goalx_backend.modelling.dc_model import TIER1_COMPETITIONS


@flow(name="jingcai-snapshot", log_prints=True)
def jingcai_snapshot_flow() -> dict[str, int]:
    """拉取竞彩官方全玩法报价：原始证据落盘 + append-only 入库（票 35）。"""
    stats = tasks.jingcai_snapshot()
    logger.info(
        "jingcai snapshot: {} matches, {} snapshots ({} dup)",
        stats.matches,
        stats.snapshots,
        stats.duplicate_snapshots,
    )
    return {"matches": stats.matches, "snapshots": stats.snapshots}


@flow(name="eu-odds-snapshot", log_prints=True)
def eu_odds_snapshot_flow() -> dict[str, int]:
    """欧赔采集 + 竞彩 join + 逐请求 credit 记账（超预算抛 CreditBudgetExceeded）。"""
    stats = tasks.eu_odds_snapshot()
    logger.info(
        "eu odds: {} events, {} snapshots, credits={}, unmatched={}",
        stats.events,
        stats.snapshots,
        stats.credits_used,
        stats.unmatched,
    )
    return {
        "events": stats.events,
        "snapshots": stats.snapshots,
        "credits_used": stats.credits_used,
    }


@flow(name="eu-odds-closing", log_prints=True)
def eu_odds_closing_flow(window_minutes: int = 35) -> dict[str, int]:
    """收盘窗口快照（票 32/35）：与常规采集共享月预算（同一记账路径）。"""
    stats = tasks.eu_odds_closing(window_minutes=window_minutes)
    logger.info(
        "eu closing: {} events, {} snapshots, credits={}",
        stats.events,
        stats.snapshots,
        stats.credits_used,
    )
    return {
        "events": stats.events,
        "snapshots": stats.snapshots,
        "credits_used": stats.credits_used,
    }


@flow(name="fd-history-import", log_prints=True)
def fd_history_import_flow() -> dict[str, int]:
    """导入五大 2023-26 历史底座（幂等）。"""
    stats = tasks.fd_history_import()
    logger.info("fd history: {} rows written, {} skipped", stats.written, stats.skipped)
    return {"rows": stats.rows, "written": stats.written, "skipped": stats.skipped}


@flow(name="weekly-train", log_prints=True)
def weekly_train_flow(
    competitions: tuple[str, ...] = TIER1_COMPETITIONS,
) -> dict[str, int]:
    """DC 分池周训练（基准 + bootstrap CI 工件，票 26）。"""
    return tasks.weekly_train(competitions)


@flow(name="forecast-daily", log_prints=True)
def forecast_daily_flow(business_date: str | None = None) -> dict[str, int]:
    """每日在售竞彩场次 ML Forecast 生成（幂等，跳过清单入日志，票 27）。"""
    return tasks.forecast_daily(business_date)


@flow(name="settlement-sweep", log_prints=True)
def settlement_flow() -> dict[str, int]:
    """结算批跑（配合开奖导入；paper/live 统一引擎）。"""
    stats = tasks.settlement_sweep()
    logger.info("settlement: {}", stats)
    return stats


@flow(name="draw-results-sync", log_prints=True)
def draw_results_sync_flow() -> dict[str, object]:
    """赛果自动同步（票 42）：源D 结果页；无待出赛果零成本跳过。"""
    stats = tasks.draw_results_sync()
    logger.info("draw sync: {}", stats)
    return stats


@flow(name="pool-snapshot", log_prints=True)
def pool_snapshot_flow() -> dict[str, object]:
    """彩池同步（票 43）：源B 期次/对阵/人气分布（幂等；份额追加快照）。"""
    stats = tasks.pool_snapshot()
    logger.info("pool snapshot: {}", stats_dict(stats))
    return stats_dict(stats)


@flow(name="daily-capture", log_prints=True)
def daily_capture_flow() -> dict[str, object]:
    """销售日两拍采集（票 37 协议 v1）：竞彩→预测→范围内欧赔，顺序固定。"""
    jingcai = jingcai_snapshot_flow()
    forecast = forecast_daily_flow()
    eu = eu_odds_snapshot_flow()
    return {"jingcai": jingcai, "forecast": forecast, "eu": eu}


@flow(name="daily-wrap", log_prints=True)
def daily_wrap_flow() -> dict[str, object]:
    """日终收尾（票 37）：结算批跑 + CLV 对账 + 只读账务核查。"""
    settlement = settlement_flow()
    with tasks.task_conn() as conn:
        clv_stats: dict[str, Any] = asdict(clv_mod.reconcile_clv(conn))
        findings = len(audit_ledger(conn)["manual_review"])
    logger.info(
        "daily wrap: settlement={}, clv={}, audit_findings={}",
        settlement,
        clv_stats,
        findings,
    )
    return {"settlement": settlement, "clv": clv_stats, "audit_findings": findings}


@flow(name="intel-collect", log_prints=True)
def intel_collect_flow() -> dict[str, object]:
    """情报采集（票 09）：当期彩池场次内部推导情报（幂等，零外部请求）。"""
    stats = tasks.intel_collection()
    logger.info("intel collect: {}", stats)
    return stats


@flow(name="scout-line", log_prints=True)
def scout_line_flow() -> dict[str, object]:
    """Scout 线（票 10）：读已存证情报出三项概率（跟 intel-collect 后）。"""
    stats = tasks.scout_line()
    logger.info("scout line: {}", stats)
    return stats
