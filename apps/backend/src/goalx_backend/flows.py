"""
Prefect flows（ADR 0005）：组合面编排（票 03 后只剩非同构形状）。

流程体是 goalx_backend.tasks 的薄 adapter——任务实现只有一份，cli 与
flows 共用（票 35 证据契约因此不可能再漂移）；deployment 由本地
Prefect server 调度（见 README「运行采集」）。单 cron 同构壳（调 tasks
函数、记日志、回 stats）自 deepen-20260928 票 02/03 起由数据集注册表
（datasets.py）生成；本文件只剩：

- 组合面（多 flow 编排）：daily-capture / weekly-refresh / daily-wrap
- 双 cron 面：draw-results-sync（单 flow 双 deployment，schedules 手写）
- 组合面子件：jingcai-snapshot / fd-history-import / weekly-train /
  forecast-daily / settlement-sweep
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import date
from typing import Any

from loguru import logger
from prefect import flow

from goalx_backend import tasks
from goalx_backend.betting import frozen as frozen_mod
from goalx_backend.betting.ledger_audit import audit_ledger
from goalx_backend.data import price_ledger
from goalx_backend.evaluation import clv as clv_mod
from goalx_backend.modelling.dc_model import TIER1_COMPETITIONS


@flow(name="jingcai-snapshot", log_prints=True)
def jingcai_snapshot_flow() -> dict[str, object]:
    """拉取竞彩官方全玩法报价：原始证据落盘 + append-only 入库（票 35）。"""
    stats = tasks.jingcai_snapshot()
    if stats.no_market_matches or stats.empty_markets:
        # 缺账面（票 03）：玩法块缩面/缺席——官方改版候选，不得静默
        logger.warning(
            "jingcai 缺账：{} 场零玩法块，缺席玩法 {}",
            stats.no_market_matches,
            stats.empty_markets,
        )
    logger.info(
        "jingcai snapshot: {} matches, {} snapshots ({} dup)",
        stats.matches,
        stats.snapshots,
        stats.duplicate_snapshots,
    )
    return {
        "matches": stats.matches,
        "snapshots": stats.snapshots,
        "no_market_matches": stats.no_market_matches,
        "empty_markets": stats.empty_markets,
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


@flow(name="weekly-refresh", log_prints=True)
def weekly_refresh_flow() -> dict[str, object]:
    """
    周刷新（票 54）：fdhist 幂等重导（新赛果/新季行落库）→ DC 周训练。

    2026-09-22 实证：工件停在 9-18 而 2627 数据 9-20 落库——训练域吃不到
    新季导致升班马 no_mapping、预测覆盖受损。定拍周一 06:10（fd.co.uk
    周末赛果数日滞后，周一导入已含上周场次）。训练域 = 五大 + N1
    （与 data/models 现役工件池一致）。
    """
    import_stats = fd_history_import_flow()
    matches = weekly_train_flow((*TIER1_COMPETITIONS, "N1"))
    return {"import": import_stats, "trained": matches}


@flow(name="forecast-daily", log_prints=True)
def forecast_daily_flow(business_date: str | None = None) -> dict[str, int]:
    """每日在售竞彩场次 ML Forecast 生成（幂等，跳过清单入日志，票 27）。"""
    return tasks.forecast_daily(business_date)


@flow(name="auto-paper", log_prints=True)
def auto_paper_flow() -> dict[str, int]:
    """auto-v1 策略落注（auto-paper 票 03）：锚后 1 分钟跑当前锚。"""
    stats = tasks.auto_paper()
    logger.info("auto-paper: {}", stats)
    return stats


@flow(name="baseline-freeze", log_prints=True)
def baseline_freeze_flow() -> dict[str, int]:
    """三锚市场基准冻结（auto-paper 票 01）：12:33/16:33/21:33 读库 as-of 冻结。"""
    stats = tasks.baseline_freeze()
    logger.info("baseline freeze: {}", stats)
    return stats


@flow(name="settlement-sweep", log_prints=True)
def settlement_flow() -> dict[str, int]:
    """结算批跑（配合开奖导入；paper/live 统一引擎）。"""
    stats = tasks.settlement_sweep()
    logger.info("settlement: {}", stats)
    return stats


@flow(name="draw-results-sync", log_prints=True)
def draw_results_sync_flow() -> dict[str, object]:
    """赛果自动同步（票 44 切换后）：官方 uniform；无待出赛果零成本跳过。"""
    stats = tasks.draw_results_sync()
    logger.info("draw sync: {}", stats)
    return stats


@flow(name="daily-capture", log_prints=True)
def daily_capture_flow() -> dict[str, object]:
    """
    销售日两拍采集（票 37 协议 v1；2026-09-25 停采重整后改段）：竞彩→预测。

    欧赔聚合段与 PropLine 互备拍已按 2026-09-25 裁决摘除（国际书商
    赔率全走源T 六端点，欧赔聚合无存在必要；死包装函数随
    review-20260928 票 04 清除）。竞彩官方 SP 历史与赛前实时拍归
    语料层（票 65/67）。
    """
    jingcai = jingcai_snapshot_flow()
    forecast = forecast_daily_flow()
    # 票 08：新鲜度双口径影子拍挂采集后（correctness F1：观测口径要量
    # "价久未变但刚确认过"，必须紧跟竞彩采集拍（10:00/12:30/16:30/19:00
    # 定拍）——23:30 wrap 拍的观测龄恒 4h+ 会把双口径读成常数；日拍
    # REPLACE 最新一拍生效，wrap 不再拍。采集为体、影子为测量，影子
    # 失败不拖采集。
    try:
        freshness_shadow = tasks.freshness_shadow_day()
    except Exception as exc:
        logger.warning("freshness shadow snap degraded: {}", exc)
        freshness_shadow = {"error": str(exc)}
    return {
        "jingcai": jingcai,
        "forecast": forecast,
        "freshness_shadow": freshness_shadow,
    }


@flow(name="daily-wrap", log_prints=True)
def daily_wrap_flow() -> dict[str, object]:
    """日终收尾：结算批跑 + 漏斗/冻结决策日拍 + 冻结对账 + CLV 对账 + 账务核查。"""
    settlement = settlement_flow()
    funnel = tasks.funnel_day()
    # 票 02：M3 独立异常边界（同 freshness_shadow 先例，L128-132）：报告读
    # corpus 追加窗口可撞并发读 EOFError（写完重读正常，非永久损坏）——
    # 降级不拖其后的冻结对账/CLV/账本回填/账务核查；失败不得记成通过
    # （结算在 M3 之前已执行，受影响的是后续对账审计）。
    try:
        m3 = tasks.m3_evaluation()
    except Exception as exc:
        logger.warning("m3 evaluation degraded: {}", exc)
        m3 = {"error": str(exc), "degraded": True}
    frozen_recon = tasks.frozen_reconcile()
    with tasks.task_conn() as conn:
        # 票 75：odds_api closing 判死后，收盘锚由源T cid177 接管
        # （corpus_anchor 库缺席降级 None，不打断对账）
        with tasks.corpus_anchor() as anchor:
            clv_stats: dict[str, Any] = asdict(
                clv_mod.reconcile_clv(conn, duck_con=anchor)
            )
            # 同步报价账本 sweep（measure-07）：源T as-of 主锚回填 NULL 参考行
            # （降级不打断——语料桥/账本异常不殃及 wrap 主体，同上惯例）
            try:
                ledger_filled = price_ledger.backfill_missing_reference(conn, anchor)
                ledger_face: dict[str, Any] = price_ledger.ledger_summary(conn)
            except sqlite3.Error as exc:
                logger.warning("price ledger sweep degraded: {}", exc)
                ledger_filled, ledger_face = -1, {}
        findings = len(audit_ledger(conn)["manual_review"])
        # 日终冻结汇总面（measure-08：含税前/税后 tax_face）
        frozen_face = frozen_mod.frozen_summary(conn, str(funnel["business_date"]))
    # 探索轨日扫（autoresearch 票 01/04）：结算之后才有"昨日已结算场"——
    # 挂 wrap 尾部（赛果已落、漏斗已拍）；降级不拖返回体（同 M3 先例）。
    # 周一（业务日）加跑周报 digest（周一版覆盖前 7 日）。
    try:
        edge_scan = tasks.edge_scan_day()
    except Exception as exc:
        logger.warning("edge scan degraded: {}", exc)
        edge_scan = {"error": str(exc), "degraded": True}
    edge_digest: dict[str, object] | None = None
    if date.fromisoformat(str(funnel["business_date"])).weekday() == 0:
        try:
            edge_digest = tasks.edge_digest_week()
        except Exception as exc:
            logger.warning("edge digest degraded: {}", exc)
            edge_digest = {"error": str(exc), "degraded": True}
    logger.info(
        "daily wrap: settlement={}, funnel={}, clv={}," + " audit_findings={}, m3={}",
        settlement,
        funnel,
        clv_stats,
        findings,
        m3,
    )
    return {
        "settlement": settlement,
        "funnel": funnel,
        # 票 02：M3 面（含降级 error 标记）进返回体——失败在结果里可见，
        # 不只埋在日志（验收：注入异常 → 返回体含 error）
        "m3": m3,
        "frozen_reconcile": frozen_recon,
        "clv": clv_stats,
        "audit_findings": findings,
        "price_ledger": {"backfilled": ledger_filled, **ledger_face},
        "frozen_summary": frozen_face,
        "edge_scan": edge_scan,
        "edge_digest": edge_digest,
    }
