"""
Prefect flows（ADR 0005）：定时采集、历史导入与结算批跑。

流程体刻意保持薄（服务层承载全部逻辑，单测覆盖在服务层）；
deployment 由本地 Prefect server 调度（见 README「运行采集」）。

- jingcai_snapshot_flow：竞彩全玩法快照（销售期高频，如每 30 分钟）
- eu_odds_snapshot_flow：欧赔快照 + join + credit 记账（kickoff 前窗口加密）
- fd_history_import_flow：历史底座一次性导入（幂等可重跑）
- settlement_flow：开奖后结算批跑（每日数次）
"""

from __future__ import annotations

import httpx
from loguru import logger
from prefect import flow, get_run_logger

from goalx_backend.config import get_settings
from goalx_backend.db import connect, migrate
from goalx_backend.ingest import fdhist, oddsapi, sporttery
from goalx_backend.services import run_settlement


@flow(name="jingcai-snapshot", log_prints=True)
def jingcai_snapshot_flow() -> dict[str, int]:
    """拉取竞彩官方全玩法报价并 append-only 入库。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with httpx.Client() as client:
            payload = sporttery.fetch_calculator_payload(settings, client)
        stats = sporttery.store_matches(conn, sporttery.parse_matches(payload))
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
    """欧赔采集 + 竞彩 join + credit 记账（超预算抛 CreditBudgetExceeded）。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with httpx.Client() as client:
            stats = oddsapi.fetch_and_store_odds(conn, settings, client)
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


@flow(name="fd-history-import", log_prints=True)
def fd_history_import_flow() -> dict[str, int]:
    """导入五大 2023-26 历史底座（幂等）。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with httpx.Client() as client:
            stats = fdhist.import_history(conn, settings, client)
        get_run_logger().info(
            "fd history: %s rows written, %s skipped", stats.written, stats.skipped
        )
        return {"rows": stats.rows, "written": stats.written, "skipped": stats.skipped}
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
