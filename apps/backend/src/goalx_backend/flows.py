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
"""

from __future__ import annotations

from loguru import logger
from prefect import flow

from goalx_backend import tasks
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
