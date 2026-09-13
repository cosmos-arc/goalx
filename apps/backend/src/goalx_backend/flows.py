"""
Prefect flows（ADR 0005）：定时采集、历史导入、训练与结算批跑。

流程体刻意保持薄（服务层承载全部逻辑，单测覆盖在服务层）；
deployment 由本地 Prefect server 调度（见 README「运行采集」）。

- jingcai_snapshot_flow：竞彩全玩法快照（销售期高频，如每 30 分钟）
- eu_odds_snapshot_flow：欧赔快照 + join + credit 记账（均匀轮询；
  kickoff −30/−10/−1min 窗口加密在部署侧以更细 cron 间隔实现，见 README）
- fd_history_import_flow：历史底座一次性导入（幂等可重跑）
- weekly_train_flow：DC 分池周训练（Tier1 五大，票 26）
- forecast_daily_flow：每日在售场次 ML Forecast 生成（票 27）
- settlement_flow：开奖后结算批跑（每日数次）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from loguru import logger
from prefect import flow, get_run_logger

from goalx_backend.config import get_settings
from goalx_backend.db import connect, migrate
from goalx_backend.dc_model import TIER1_COMPETITIONS, train_competition
from goalx_backend.forecast import generate_forecasts
from goalx_backend.ingest import fdhist, oddsapi, sporttery
from goalx_backend.ingest.oddsapi import polite_client
from goalx_backend.services import run_settlement


@flow(name="jingcai-snapshot", log_prints=True)
def jingcai_snapshot_flow() -> dict[str, int]:
    """拉取竞彩官方全玩法报价：原始证据落盘 + append-only 入库（票 35）。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with polite_client() as client:
            stats = sporttery.capture_jingcai(
                conn, settings, client, raw_root=settings.observations_dir
            )
        logger.info(
            "jingcai snapshot: {} matches, {} snapshots ({} dup)",
            stats.matches,
            stats.snapshots,
            stats.duplicate_snapshots,
        )
        return {"matches": stats.matches, "snapshots": stats.snapshots}
    finally:
        conn.close()


@flow(name="eu-odds-snapshot", log_prints=True)
def eu_odds_snapshot_flow() -> dict[str, int]:
    """欧赔采集 + 竞彩 join + 逐请求 credit 记账（超预算抛 CreditBudgetExceeded）。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with polite_client() as client:
            stats = oddsapi.fetch_and_store_odds(
                conn, settings, client, raw_root=settings.observations_dir
            )
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
    finally:
        conn.close()


@flow(name="eu-odds-closing", log_prints=True)
def eu_odds_closing_flow(window_minutes: int = 35) -> dict[str, int]:
    """收盘窗口快照（票 32/35）：与常规采集共享月预算（同一记账路径）。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with polite_client() as client:
            stats = oddsapi.fetch_closing_window(
                conn,
                settings,
                client,
                window_minutes=window_minutes,
                raw_root=settings.observations_dir,
            )
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
    finally:
        conn.close()


@flow(name="fd-history-import", log_prints=True)
def fd_history_import_flow() -> dict[str, int]:
    """导入五大 2023-26 历史底座（幂等）。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with polite_client() as client:
            stats = fdhist.import_history(conn, settings, client)
        get_run_logger().info(
            "fd history: %s rows written, %s skipped", stats.written, stats.skipped
        )
        return {"rows": stats.rows, "written": stats.written, "skipped": stats.skipped}
    finally:
        conn.close()


@flow(name="weekly-train", log_prints=True)
def weekly_train_flow(
    competitions: tuple[str, ...] = TIER1_COMPETITIONS,
) -> dict[str, int]:
    """DC 分池周训练（基准 + bootstrap CI 工件，票 26）。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        stats: dict[str, int] = {}
        for competition in competitions:
            run = train_competition(
                conn,
                competition,
                models_dir=settings.models_dir,
                half_life_days=settings.dc_half_life_days,
                n_boot=settings.bootstrap_samples,
                seed=settings.bootstrap_seed,
            )
            stats[competition] = run.base.n_matches
            logger.info(
                "trained {}: {} matches, {} boot artifacts",
                competition,
                run.base.n_matches,
                len(run.bootstrap),
            )
        return stats
    finally:
        conn.close()


@flow(name="forecast-daily", log_prints=True)
def forecast_daily_flow(business_date: str | None = None) -> dict[str, int]:
    """每日在售竞彩场次 ML Forecast 生成（幂等，跳过清单入日志，票 27）。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        cst = timezone(timedelta(hours=8))  # businessDate 为北京日期(spec §3)
        resolved = business_date or datetime.now(UTC).astimezone(cst).strftime(
            "%Y-%m-%d"
        )
        stats = generate_forecasts(
            conn,
            business_date=resolved,
            models_dir=str(settings.models_dir),
        )
        logger.info(
            "forecasts: {} generated, {} dup, {} skipped: {}",
            stats.generated,
            stats.duplicates,
            len(stats.skipped),
            stats.skipped[:10],
        )
        return {
            "generated": stats.generated,
            "duplicates": stats.duplicates,
            "skipped": len(stats.skipped),
        }
    finally:
        conn.close()


@flow(name="settlement-sweep", log_prints=True)
def settlement_flow() -> dict[str, int]:
    """结算批跑（配合开奖导入；paper/live 统一引擎）。"""
    conn = connect(get_settings().db_path)
    try:
        migrate(conn)
        stats = run_settlement(conn)
        logger.info("settlement: {}", stats)
        return stats
    finally:
        conn.close()
