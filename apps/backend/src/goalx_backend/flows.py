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

from dataclasses import asdict
from typing import Any

from loguru import logger
from prefect import flow

from goalx_backend import tasks
from goalx_backend.betting.ledger_audit import audit_ledger
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
    return {"jingcai": jingcai, "forecast": forecast}


@flow(name="daily-wrap", log_prints=True)
def daily_wrap_flow() -> dict[str, object]:
    """日终收尾（票 37）：结算批跑 + CLV 对账 + 只读账务核查。"""
    settlement = settlement_flow()
    m3 = tasks.m3_evaluation()
    with tasks.task_conn() as conn:
        # 票 75：odds_api closing 判死后，收盘锚由源T cid177 接管
        # （corpus_anchor 库缺席降级 None，不打断对账）
        with tasks.corpus_anchor() as anchor:
            clv_stats: dict[str, Any] = asdict(
                clv_mod.reconcile_clv(conn, duck_con=anchor)
            )
        findings = len(audit_ledger(conn)["manual_review"])
    logger.info(
        "daily wrap: settlement={}, clv={}, audit_findings={}, m3={}",
        settlement,
        clv_stats,
        findings,
        m3,
    )
    return {"settlement": settlement, "clv": clv_stats, "audit_findings": findings}
