"""
共享任务体：cli 与 Prefect flows 调用同一实现（ADR-0005）。

背景：cli ingest-jingcai 曾绕过 capture_jingcai 静默丢失证据存证——同一
任务两份实现必然漂移（2026-09 架构评审候选 4）。Prefect flow 与 cli 命令
是同一任务的两个 adapter；本模块是任务实现的唯一住所。

不含调度的运维命令（backtest/校准/报表等）只有 cli 一个 adapter，仍直接
使用 task_conn 壳。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager

from goalx_backend.betting.settle import run_settlement
from goalx_backend.config import get_settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.ingest import caiguo, fdhist, oddsapi, sporttery, zucai
from goalx_backend.data.ingest.oddsapi import polite_client
from goalx_backend.db import connect, migrate
from goalx_backend.llm.collect import collect_pool_intel
from goalx_backend.llm.collect import stats_dict as intel_stats_dict
from goalx_backend.modelling.dc_model import TIER1_COMPETITIONS, train_competition
from goalx_backend.modelling.forecast import generate_forecasts


@contextmanager
def task_conn() -> Generator[sqlite3.Connection]:
    """任务壳：connect → migrate → finally close（cli/flows 共享）。"""
    conn = connect()
    try:
        migrate(conn)
        yield conn
    finally:
        conn.close()


def jingcai_snapshot() -> sporttery.IngestStats:
    """竞彩全玩法快照：原始证据落盘 + append-only 入库（票 35）。"""
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        return sporttery.capture_jingcai(
            conn, settings, client, raw_root=settings.observations_dir
        )


def eu_odds_snapshot() -> oddsapi.OddsIngestStats:
    """欧赔采集 + 竞彩 join + 逐请求 credit 记账（超预算抛 CreditBudgetExceeded）。"""
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        return oddsapi.fetch_and_store_odds(
            conn, settings, client, raw_root=settings.observations_dir
        )


def eu_odds_closing(window_minutes: int = 35) -> oddsapi.OddsIngestStats:
    """收盘窗口尽力快照（票 32/35）：与常规采集共享月预算（同一记账路径）。"""
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        return oddsapi.fetch_closing_window(
            conn,
            settings,
            client,
            window_minutes=window_minutes,
            raw_root=settings.observations_dir,
        )


def fd_history_import() -> fdhist.HistImportStats:
    """导入五大 2023-26 历史底座（幂等可重跑）。"""
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        return fdhist.import_history(conn, settings, client)


def weekly_train(
    competitions: tuple[str, ...] = TIER1_COMPETITIONS,
    *,
    half_life_days: float | None = None,
    n_boot: int | None = None,
) -> dict[str, int]:
    """DC 分池周训练（基准 + bootstrap CI 工件，票 26）；缺省参数取 settings。"""
    settings = get_settings()
    matches: dict[str, int] = {}
    with task_conn() as conn:
        for competition in competitions:
            run = train_competition(
                conn,
                competition,
                models_dir=settings.models_dir,
                half_life_days=(
                    settings.dc_half_life_days
                    if half_life_days is None
                    else half_life_days
                ),
                n_boot=settings.bootstrap_samples if n_boot is None else n_boot,
                seed=settings.bootstrap_seed,
            )
            matches[competition] = run.base.n_matches
    return matches


def forecast_daily(business_date: str | None = None) -> dict[str, int]:
    """每日在售竞彩场次 ML Forecast 生成（幂等，票 27）。"""
    settings = get_settings()
    resolved = business_date or fx_store.beijing_business_date()
    with task_conn() as conn:
        stats = generate_forecasts(
            conn,
            business_date=resolved,
            models_dir=str(settings.models_dir),
        )
    return {
        "generated": stats.generated,
        "duplicates": stats.duplicates,
        "skipped": len(stats.skipped),
    }


def settlement_sweep() -> dict[str, int]:
    """结算批跑（配合开奖导入；paper/live 统一引擎）。"""
    with task_conn() as conn:
        return run_settlement(conn)


def draw_results_sync() -> dict[str, object]:
    """
    赛果自动同步（票 42）：源D 结果页 → import_draw_results。

    候选业务日由库内待出赛果推导；无待出赛果时不发任何请求（零成本
    跳过，与 eu-odds-closing 同模式）。
    """
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        stats = caiguo.sync_draw_results(conn, settings, client)
    return caiguo.stats_dict(stats)


def pool_snapshot() -> zucai.PoolSyncStats:
    """彩池同步（票 43）：源B 期次/对阵/人气分布 → pool 域表。"""
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        return zucai.sync_pool_data(conn, settings, client)


def intel_collection() -> dict[str, object]:
    """情报采集（票 09）：当期彩池场次内部推导情报（幂等，零外部请求）。"""
    with task_conn() as conn:
        stats = collect_pool_intel(conn)
    return intel_stats_dict(stats)
