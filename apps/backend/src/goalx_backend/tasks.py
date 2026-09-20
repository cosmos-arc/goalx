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

import httpx
from loguru import logger

from goalx_backend.betting.settle import run_settlement
from goalx_backend.config import get_settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import reconcile
from goalx_backend.data import results as rs
from goalx_backend.data.ingest import (
    caiguo,
    fdhist,
    oddsapi,
    openfootball,
    sporttery,
    understat,
    uniform,
    zucai,
)
from goalx_backend.data.ingest.oddsapi import polite_client
from goalx_backend.db import connect, migrate
from goalx_backend.llm.collect import collect_pool_intel
from goalx_backend.llm.collect import stats_dict as intel_stats_dict
from goalx_backend.llm.fusion import fusion_stats_dict, fusion_sweep
from goalx_backend.llm.gate import gate_stats_dict, gate_sweep
from goalx_backend.llm.m3_report import m3_protocol_report
from goalx_backend.llm.okooo_formation import collect_injury_intel, injury_stats_dict
from goalx_backend.llm.protocol import record_control_events
from goalx_backend.llm.review import enqueue_post_settle
from goalx_backend.llm.scout import scout_stats_dict, scout_sweep
from goalx_backend.llm.sina_intel import collect_sina_injury_intel, sina_stats_dict
from goalx_backend.modelling.dc_model import TIER1_COMPETITIONS, train_competition
from goalx_backend.modelling.forecast import generate_forecasts
from goalx_backend.modelling.team_align import alias_index


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
    """每日在售竞彩场次 ML Forecast 生成（幂等，票 27；五大附 xG blend 票 47）。"""
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
        "xg_blended": stats.xg_blended,
        "xg_unresolved": stats.xg_unresolved,
        "skipped": len(stats.skipped),
    }


def settlement_sweep() -> dict[str, int]:
    """结算批跑（配合开奖导入；paper/live 统一引擎）。"""
    with task_conn() as conn:
        return run_settlement(conn)


def draw_results_sync() -> dict[str, object]:
    """
    赛果自动同步（票 44 切换后为官方 uniform 源，终态落事实）。

    票 42 时代为源D 页面导入；候选业务日由库内待出赛果推导，
    无待出赛果时不发任何请求（零成本跳过，与 eu-odds-closing 同模式）。
    源D 降为审计源（official_results_reconcile）。
    """
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        stats, _rec = uniform.sync_uniform_results(conn, settings, client)
    return uniform.stats_dict(stats)


def official_results_reconcile() -> dict[str, object]:
    """
    赛果日终审计（票 44）：源D 页面对账 + openfootball 比分对账，不落事实。

    官方 uniform 同步（draw-results-sync）负责落库；本任务只交叉核对：
    源D 审计窗口为近 7 天已开赛场次（含已落果日），openfootball 为窗口内
    已映射联赛。
    """
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        caiguo_rec = caiguo.audit_draw_results(conn, settings, client)
        of_rec = openfootball.reconcile_openfootball(
            conn, settings, client, alias_index=alias_index(conn)
        )
    return {
        "caiguo": reconcile.stats_dict(caiguo_rec),
        "openfootball": reconcile.stats_dict(of_rec),
    }


def understat_sync(
    seasons: tuple[str, ...] | None = None,
    *,
    leagues: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """
    Understat xG 特征同步（票 45）。

    默认五大当前季（6 请求/日 ≤10 上限）；seasons 显式传入即回填历史
    赛季（一次性，不进调度）；leagues 缺省五大（俄超 rfpl 按需）。
    """
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        stats = understat.sync_understat(
            conn,
            settings,
            client,
            alias=alias_index(conn),
            seasons=seasons,
            leagues=leagues or rs.UNDERSTAT_DEFAULT_LEAGUES,
        )
    return understat.stats_dict(stats)


def pool_snapshot() -> zucai.PoolSyncStats:
    """彩池同步（票 43）：源B 期次/对阵/人气分布 → pool 域表。"""
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        return zucai.sync_pool_data(conn, settings, client)


def intel_collection() -> dict[str, object]:
    """
    情报采集（票 09）：内部推导 + 澳客伤停 + 新浪伤停。

    幂等；单一源故障不阻塞其余。
    """
    with task_conn() as conn:
        stats = collect_pool_intel(conn)
    injury: dict[str, object] = {}
    try:
        with task_conn() as conn, httpx.Client() as client:
            injury = injury_stats_dict(collect_injury_intel(conn, client))
    except httpx.HTTPError as exc:
        logger.warning("澳客伤停采集整体失败（推导情报不受影响）: {}", exc)
        injury = {"error": str(exc)}
    sina: dict[str, object] = {}
    try:
        with task_conn() as conn, httpx.Client() as client:
            sina = sina_stats_dict(collect_sina_injury_intel(conn, client))
    except httpx.HTTPError as exc:
        logger.warning("新浪伤停采集整体失败（其余情报不受影响）: {}", exc)
        sina = {"error": str(exc)}
    return {**intel_stats_dict(stats), "injury": injury, "sina": sina}


def scout_line() -> dict[str, object]:
    """Scout 线（票 10/11）：GLM 三项概率 → gate JS 散度路由 → analyst 复核。"""
    settings = get_settings()
    with task_conn() as conn:
        scout = scout_stats_dict(scout_sweep(conn, settings))
        gate = gate_stats_dict(gate_sweep(conn, settings))
        fused = fusion_stats_dict(fusion_sweep(conn, settings))
    return {**scout, "gate": gate, "fusion": fused}


def m3_evaluation() -> dict[str, object]:
    """M3 评测协议（票 13）：三列报告 + 赛后复核入队 + 控制事件。"""
    with task_conn() as conn:
        report = m3_protocol_report(conn)
        review = enqueue_post_settle(conn)
        record_control_events(conn, get_settings())
    return {
        "tier_a": report["tier_a"]["verdict"],
        "tier_b": report["tier_b"]["verdict"],
        "paired": report["paired_fused_vs_ml"]["pairs"],
        "post_settle_enqueued": review.enqueued,
    }
