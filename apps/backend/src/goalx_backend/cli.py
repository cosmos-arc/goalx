"""
goalx 命令行入口：迁移与一次性采集/训练/预测任务。

与 Prefect flows 对应的命令调用 goalx_backend.tasks 的同一实现（证据
契约只有一份）；无 flow 对应的运维命令（回测/校准/报表）直接用
task_conn 壳。日常定时采集走 Prefect deployments；本 CLI 覆盖初始化与
手工补跑：

    uv run python -m goalx_backend.cli migrate
    uv run python -m goalx_backend.cli ingest-jingcai
    uv run python -m goalx_backend.cli reprocess-sporttery
    uv run python -m goalx_backend.cli ingest-odds
    uv run python -m goalx_backend.cli ingest-hist
    uv run python -m goalx_backend.cli settle
    uv run python -m goalx_backend.cli sync-draw-results
    uv run python -m goalx_backend.cli train-models [--bootstrap N]
    uv run python -m goalx_backend.cli forecast [--date YYYY-MM-DD]
    uv run python -m goalx_backend.cli align-report
    uv run python -m goalx_backend.cli accuracy-score
    uv run python -m goalx_backend.cli pool-sync
    uv run python -m goalx_backend.cli understat-sync [--seasons 2021 2022 ...]
    uv run python -m goalx_backend.cli xg-compare --seasons 2022 2023 2024 2025 2026
    uv run python -m goalx_backend.cli srct-collect --date YYYY-MM-DD
    uv run python -m goalx_backend.cli srct-night [--no-window] [--request-cap N]
    uv run python -m goalx_backend.cli srct-night --list
    uv run python -m goalx_backend.cli srct-shift [--no-window] [--request-cap N]
    uv run python -m goalx_backend.cli jc-collect --match-id 1234567 [--match-id …]
    uv run python -m goalx_backend.cli srct-silver
    uv run python -m goalx_backend.cli srct-odds
    uv run python -m goalx_backend.cli srct-gate
    uv run python -m goalx_backend.cli archive-538
    uv run python -m goalx_backend.cli gold-build
    uv run python -m goalx_backend.cli edge-scan [--scanner s1] [--face decade]
    uv run python -m goalx_backend.cli version-register --estimator E \
        --calibration C --params P --label L
    uv run python -m goalx_backend.cli version-promote --id 1 --to live-paper \
        --reason "过门放量"
    uv run python -m goalx_backend.cli hypothesis-mark --h-id H4 --to supported \
        --reason "滞后案例率 63.6% 远超门（人裁）"
    uv run python -m goalx_backend.cli contraction-set --level L1 --reason "主钟触发"
    uv run python -m goalx_backend.cli contraction-read
    uv run python -m goalx_backend.cli proposal-register <提案文件>\
      [--proposal-id P-… --h-id H9]
    uv run python -m goalx_backend.cli monthly-read  # 月读报告（vision 票 04）：
    # 八节只读报告落语料树 reports/monthly/；裁定动作走上方人 CLI，报告只读
    uv run python -m goalx_backend.cli shadow-retrain  # 影子重训拍（vision 票 07）：
    # 漂移 PSI/月度兜底→重训独立目录→注册 shadow→同流量双写；永无自动晋升
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast

import duckdb
import httpx
from loguru import logger

from goalx_backend import datasets, tasks
from goalx_backend.betting import funnel as fn
from goalx_backend.betting import versioning
from goalx_backend.betting.ledger_audit import audit_ledger
from goalx_backend.config import Settings, get_settings
from goalx_backend.data import (
    corpus_duckdb,
    corpus_gate,
    delay_survival,
    mapping,
    reconcile,
)
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import (
    archive538,
    caiguo,
    elo_silver,
    fdhist,
    jc,
    jc_audit,
    jc_backfill,
    jc_silver,
    news_pit,
    openfootball,
    sporttery,
    srct,
    srct_asof,
    srct_market,
    srct_odds,
    srct_remat,
    srct_results,
    srct_silver,
    uniform,
)
from goalx_backend.data.ingest import guardian_silver as gs
from goalx_backend.data.ingest.shell import browser_ssl_context
from goalx_backend.db import atomic, connect, connect_research, migrate, table_exists
from goalx_backend.evaluation import backtest as bt
from goalx_backend.evaluation import (
    baseline,
    contraction,
    edge_scan,
    hypothesis_registry,
    market_lambda,
    quality_gate,
    rag,
    unlock_report,
    weekly_agent,
)
from goalx_backend.evaluation import bucket_reaudit as bkt
from goalx_backend.evaluation import clv as clv_mod
from goalx_backend.evaluation import clv_decade as clvd
from goalx_backend.evaluation import decade_skill as dsk
from goalx_backend.evaluation import guardrail_report as gr
from goalx_backend.evaluation import haircut as hc
from goalx_backend.evaluation import haircut_decade as hcd
from goalx_backend.evaluation import metrics as ev
from goalx_backend.evaluation import model_layers as mlp
from goalx_backend.evaluation import multiline_bounds as mlb
from goalx_backend.evaluation import news_signal as nsw
from goalx_backend.evaluation import pbo_feasibility as pbo
from goalx_backend.evaluation import price_edge as pe
from goalx_backend.evaluation import signal_attribution as sig
from goalx_backend.evaluation.drift_replay import drift_replay_report
from goalx_backend.evaluation.pool_replay import pool_replay_report
from goalx_backend.evaluation.xg_compare import run_xg_comparison
from goalx_backend.llm import guardian_extract as gx
from goalx_backend.modelling import team_align
from goalx_backend.modelling.calibration import CALIBRATION_KINDS
from goalx_backend.modelling.dc_model import TIER1_COMPETITIONS
from goalx_backend.modelling.fusion import FUSION_MODES
from goalx_backend.modelling.residual_layer import RESIDUAL_MODES
from goalx_backend.morning_check import run_morning_check
from goalx_backend.tasks import (
    clubelo_elo_for_team,
    corpus_anchor,
    mapping_audit,
    mapping_sync,
    six_axes_derive,
    six_axes_report,
    task_conn,
    tm_coach_crosscheck,
    tm_coach_report,
)


def _cmd_migrate() -> None:
    """执行 schema 迁移。"""
    conn = connect()
    version = migrate(conn)
    conn.close()
    logger.info("schema at version {}", version)


def _cmd_version_register(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    策略版本注册（vision-build 票 01，人 CLI）：三元组幂等落账。

    estimator/calibration/params 三元组=版本身份（内容寻址标识：dc 工件
    身份 / 校准口径 / auto_strategy 参数指纹）；重复注册同三元组返回同 id。
    settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    conn = connect(resolved.db_path)
    try:
        migrate(conn)
        # 新建判定在注册前（幂等命中=旧版本；F3：旧版本不得事后补冻结面）
        was_new = (
            versioning.find_version_id(
                conn,
                estimator_version=args.estimator,
                calibration_version=args.calibration,
                params_version=args.params,
            )
            is None
        )
        version_id = versioning.register_version(
            conn,
            estimator_version=args.estimator,
            calibration_version=args.calibration,
            params_version=args.params,
            label=args.label,
        )
        # 票 11 prereg：注册即冻结门指标面（首写恒胜；幂等命中旧版本且无
        # 冻结面=事后补冻结，freeze_gate_spec 拒绝——correctness F3）
        quality_gate.freeze_gate_spec(conn, version_id, newly_registered=was_new)
        state = versioning.current_state(conn, version_id)
        # 幂等命中回显库内首注册 label（correctness F4：三元组=身份，
        # 后到的 --label 不改写既有行，回显不得谎报）
        stored_label = versioning.version_label(conn, version_id)
    finally:
        conn.close()
    payload: dict[str, Any] = {
        "id": version_id,
        "label": stored_label,
        "state": state.value if state is not None else None,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    logger.info(
        "version registered: id={} label={} state={}",
        payload["id"],
        payload["label"],
        payload["state"],
    )


def _cmd_version_promote(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    策略版本晋升/转移（vision-build 票 01，人 CLI）：写 transitions。

    晋升恒人审——agent 无该写路径；谓词拒绝（跳级/终态复出）fail-loud。
    票 11 硬门：shadow→probe 晋升前查门账（最新门评估须通过记录，
    不过=GateNotPassed 拒绝留痕——门账即证据面，transitions 零写入）。
    settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    target = versioning.StrategyState(args.to)
    conn = connect(resolved.db_path)
    try:
        migrate(conn)
        from_state = versioning.current_state(conn, args.id)
        if (
            from_state is versioning.StrategyState.SHADOW
            and target is versioning.StrategyState.PROBE
        ):
            # F6：查门账与落转移同一事务（atomic 嵌套=SAVEPOINT）——check
            # 与 act 之间无窗口，复评翻挂不得溜进已开闸的转移
            with atomic(conn):
                quality_gate.require_gate_pass(conn, args.id)
                versioning.transition_version(
                    conn,
                    args.id,
                    target,
                    actor="human:cli",
                    reason=args.reason,
                )
        else:
            versioning.transition_version(
                conn,
                args.id,
                target,
                actor="human:cli",
                reason=args.reason,
            )
        to_state = versioning.current_state(conn, args.id)
    finally:
        conn.close()
    payload: dict[str, Any] = {
        "id": args.id,
        "from_state": from_state.value if from_state is not None else None,
        "to_state": to_state.value if to_state is not None else None,
        "actor": "human:cli",
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    logger.info("version transition: {}", payload)


def _cmd_quality_gate_evaluate(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    Challenger 质量门评估（vision-build 票 11，人 CLI）：回放窗校准对比。

    冻结指标（注册时）在十年语料 PIT 回放窗（现代窗留出只读）上评估
    challenger 不劣于 champion 的显著水平；结果落门账（append-only，
    可复算）+ 对比报告落语料树 reports/quality-gate.{json,md}。
    shadow→probe 晋升（version-promote）读门账硬门。settings 注入口只
    服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    conn = connect(resolved.db_path)
    store = CorpusStore(resolved.corpus_root)
    research = connect_research(resolved.research_db_path)
    try:
        migrate(conn)
        champion_version = args.champion_version or versioning.live_version_id(conn)
        evaluation = quality_gate.evaluate_quality_gate(
            research,
            conn,
            challenger_version_id=args.challenger_version,
            champion_run_id=args.champion_run,
            challenger_run_id=args.challenger_run,
            champion_version_id=champion_version,
        )
        json_path, md_path = quality_gate.write_report(store, evaluation)
    finally:
        research.close()
        store.close()
        conn.close()
    payload = quality_gate.evaluation_payload(evaluation)
    payload["files"] = {"json": str(json_path), "md": str(md_path)}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    logger.info(
        "quality gate: challenger={} passed={} ({})",
        args.challenger_version,
        evaluation.passed,
        evaluation.hang_reason or "过门",
    )


def _cmd_hypothesis_mark(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    假设注册表状态翻转（vision-build 票 02，人 CLI）：终态人审落账。

    终态 supported|refuted 恒人审（agent 不自动定生死）；点亮
    registered→running 亦可。谓词拒绝（终态翻案/非法转移）fail-loud。
    settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    target = hypothesis_registry.HypothesisState(args.to)
    conn = connect(resolved.db_path)
    try:
        migrate(conn)
        from_state = hypothesis_registry.current_status(conn, args.h_id)
        if from_state is None:
            raise hypothesis_registry.UnknownHypothesis(
                f"假设未注册: {args.h_id}"
            ) from None
        hypothesis_registry.transition_hypothesis(
            conn,
            args.h_id,
            target,
            actor="human:cli",
            reason=args.reason,
        )
    finally:
        conn.close()
    payload: dict[str, Any] = {
        "h_id": args.h_id,
        "from_state": from_state.value,
        "to_state": target.value,
        "actor": "human:cli",
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    logger.info("hypothesis transition: {}", payload)


def _cmd_contraction_set(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    收缩级别切换（vision-build 票 08，人 CLI）：写 transitions 留痕。

    级别变更恒人审——agent 无该写路径；谓词拒绝（跳级/自环）fail-loud。
    settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    target = contraction.ContractionLevel(args.level)
    conn = connect(resolved.db_path)
    try:
        migrate(conn)
        from_level = contraction.set_level(
            conn,
            target,
            actor="human:cli",
            reason=args.reason,
        )
        to_level = contraction.current_level(conn)
    finally:
        conn.close()
    payload: dict[str, Any] = {
        "from_level": from_level.value,
        "to_level": to_level.value,
        "actor": "human:cli",
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    logger.info("contraction transition: {}", payload)


def _cmd_contraction_read(*, settings: Settings | None = None) -> None:
    """
    收缩双钟读数（vision-build 票 08）：双钟读数+落账+现级动作面。

    首读口（真数据面）与月读消费面；每次读每钟落一行（观测流水，
    UNIQUE 只防同秒双写——F3 裁定口径）。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    conn = connect(resolved.db_path)
    try:
        migrate(conn)
        clv, exploration = contraction.record_clock_readings(conn)
        level = contraction.current_level(conn)
    finally:
        conn.close()
    payload: dict[str, Any] = {
        "level": level.value,
        "clv_main": {
            "status": clv.status,
            "fired": clv.fired,
            "n": clv.n,
            "mean": clv.mean,
            "se": clv.se,
        },
        "exploration": {
            "status": exploration.status,
            "fired": exploration.fired,
            "survivors": exploration.survivors,
            "new_findings": exploration.new_findings,
        },
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    logger.info("contraction clocks: {}", payload)


def _cmd_proposal_register(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    周批提案人审注册（vision-build 票 03，人 CLI）：过审提案 freeze 进注册表。

    两态：缺省 --proposal-id/--h-id=只读清单（人审 checklist + 提案概要，
    零写入）；两旗齐给=把该提案经 register_hypothesis（actor='human:cli'）
    注册冻结。前置哨（无对抗质疑反驳/撞 refuted 库）fail-loud 零写入。
    settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    proposal_file = Path(args.proposal_file)
    if not proposal_file.is_file():
        raise FileNotFoundError(f"提案文件不存在: {proposal_file}")
    beat = weekly_agent.parse_beat_file(proposal_file)
    proposals: list[dict[str, Any]] = cast(
        "list[dict[str, Any]]", beat.get("proposals", [])
    )
    rejected = cast("list[dict[str, Any]]", beat.get("rejected", []))
    if args.proposal_id is None or args.h_id is None:
        payload: dict[str, Any] = {
            "mode": "review",
            "file": str(proposal_file),
            "run_date": beat.get("run_date"),
            "checklist": list(weekly_agent.HUMAN_REVIEW_CHECKLIST),
            "proposals": [
                {
                    "id": str(p.get("id")),
                    "statement": str(p.get("statement")),
                    "ledger_ref": str(p.get("ledger_ref")),
                    "critiqued": bool(str(p.get("critique") or "").strip()),
                }
                for p in proposals
            ],
            "rejected": [
                {"reasons": [str(r) for r in item.get("reasons", [])]}
                for item in rejected
            ],
            "hint": "人审确认 checklist 后：proposal-register <file>"
            + " --proposal-id <id> --h-id <新h_id> [--family <族>] 完成注册",
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return
    if not str(args.h_id).strip():
        raise SystemExit("--h-id 不能为空")
    conn = connect(resolved.db_path)
    try:
        migrate(conn)
        result = weekly_agent.register_from_proposal(
            conn,
            proposal_file,
            proposal_id=str(args.proposal_id),
            h_id=str(args.h_id),
            family=str(args.family) if args.family else None,
        )
    finally:
        conn.close()
    payload = {
        **result,
        "actor": "human:cli",
        "checklist": list(weekly_agent.HUMAN_REVIEW_CHECKLIST),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    logger.info("proposal registered: {}", payload)


def _cmd_ingest_jingcai() -> None:
    """手动拉一次竞彩快照（与 flow 同一实现：证据落盘 + 入库）。"""
    stats = tasks.jingcai_snapshot()
    logger.info(
        "matches={} snapshots={} dup={}",
        stats.matches,
        stats.snapshots,
        stats.duplicate_snapshots,
    )


def _cmd_reprocess_sporttery() -> None:
    """重解析 sporttery 原始证据重放入库（票 38 存量单固修正，幂等）。"""
    settings = get_settings()
    with task_conn() as conn:
        stats = sporttery.reprocess_observations(conn, settings.observations_dir)
    logger.info(
        "observations={} reparsed={} no_raw={} matches={} snapshots={} dup={}",
        stats.observations,
        stats.reparsed,
        stats.skipped_no_raw,
        stats.matches,
        stats.snapshots,
        stats.duplicate_snapshots,
    )


def _cmd_ingest_odds() -> None:
    """手动拉一次欧赔(走 credit 护栏)。"""
    stats = tasks.eu_odds_snapshot()
    logger.info(
        "events={} snapshots={} credits={} unmatched={}",
        stats.events,
        stats.snapshots,
        stats.credits_used,
        stats.unmatched,
    )


def _cmd_propline_capture() -> None:
    """手动拉一次 PropLine 快照(备用源重启入口，票 06 裁决)。"""
    stats = tasks.propline_capture()
    logger.info(
        "events={} snaps={} dup={} req={} no_books={} unmatched={} avail={}",
        stats.ingest.events,
        stats.ingest.snapshots,
        stats.ingest.duplicate_snapshots,
        stats.requests_used,
        stats.skipped_no_books,
        stats.unmatched,
        stats.unavailable_sports,
    )


def _cmd_ingest_hist() -> None:
    """导入历史底座(六大联赛全季)。"""
    stats = tasks.fd_history_import()
    logger.info(
        "rows={} written={} skipped={} failed_files={}",
        stats.rows,
        stats.written,
        stats.skipped,
        stats.failed_files,
    )


def _cmd_audit_ledger() -> None:
    """Read legacy balances and corrections without migration or ledger writes."""
    conn = connect(readonly=True)
    try:
        sys.stdout.write(
            json.dumps(audit_ledger(conn), ensure_ascii=False, indent=2) + "\n"
        )
    finally:
        conn.close()


def _cmd_settle() -> None:
    """手动结算批跑。"""
    logger.info("settlement: {}", tasks.settlement_sweep())


def _cmd_multiline_bounds() -> None:
    """OU 端点敏感性分析 + AH 联合一致性报告（measure-10，B 线可行性）。"""
    with _corpus_handles() as (duck_con, store), task_conn() as conn:
        haircut, source, _n = hc.calibrated_haircut(conn)
        payload = mlb.run_bounds_study(duck_con, store, haircut=haircut)
    logger.info(
        "multiline bounds: {} (haircut={}, source={})",
        payload["conclusion"],
        haircut,
        source,
    )


def _cmd_frozen_reconcile() -> None:
    """手动跑一次冻结对账批跑（measure-01：含已结算行晚到收盘回填）。"""
    logger.info("frozen reconcile: {}", tasks.frozen_reconcile())


def _cmd_funnel_report(args: argparse.Namespace) -> None:
    """机会漏斗日拍（票 04）：记录/重放一日 + 判活（漏斗表最新业务日）。"""
    with tasks.task_conn() as conn:
        report = fn.record_funnel_day(conn, as_of=args.as_of)
        payload: dict[str, Any] = report.as_dict()
        payload["liveness"] = fn.funnel_liveness(conn)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    logger.info(
        "funnel-report: {} 在售 {}→匹配 {}→时点 {}→估值 {}→资格 {} 流失 {} 判活={}",
        payload["business_date"],
        payload["n_on_sale"],
        payload["n_quote_matched"],
        payload["n_fresh"],
        payload["n_valuation"],
        payload["n_eligible"],
        payload["loss_reasons"],
        payload["liveness"],
    )


def _cmd_freshness_shadow(args: argparse.Namespace) -> None:
    """新鲜度双口径影子日拍（票 08）：五时钟+双结论落库（可回放指定日）。"""
    payload = tasks.freshness_shadow_day(business_date=args.date)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    logger.info("freshness-shadow: {}", payload["counts"])


def _cmd_sync_draw_results() -> None:
    """手动跑一次赛果自动同步（官方 uniform，与 flow 同一实现；票 44）。"""
    logger.info("draw sync: {}", tasks.draw_results_sync())


def _cmd_official_reconcile() -> None:
    """手动跑一次赛果日终审计（票 44）：源D+openfootball 清单报告。"""
    stats = tasks.official_results_reconcile()
    for source, report in stats.items():
        logger.info("reconcile {}: {}", source, report)
    with task_conn() as conn:
        for source in (caiguo.SOURCE, uniform.SOURCE, openfootball.SOURCE):
            row = reconcile.latest_reconciliation(conn, source)
            if row is None:
                continue
            manual = json.loads(row["pending_manual"])
            for entry in manual:
                logger.warning(
                    "pending manual [{}] {} {} {} ({})",
                    source,
                    entry.get("business_date"),
                    entry.get("code"),
                    entry.get("reason"),
                    entry.get("detail", ""),
                )


def _cmd_train_models(args: argparse.Namespace) -> None:
    """训练五大 DC 模型（基准 + bootstrap 工件落盘）。"""
    matches = tasks.weekly_train(
        tuple(args.competitions),
        half_life_days=args.half_life,
        n_boot=args.bootstrap,
    )
    for competition, n_matches in matches.items():
        logger.info("trained {}: matches={}", competition, n_matches)


def _cmd_forecast(args: argparse.Namespace) -> None:
    """对一个业务日生成 ML Forecast（幂等）。"""
    business_date = args.date or fx_store.beijing_business_date()
    stats = tasks.forecast_daily(business_date)
    logger.info(
        "forecasts for {}: generated={} dup={} skipped={}",
        business_date,
        stats["generated"],
        stats["duplicates"],
        stats["skipped"],
    )


@contextmanager
def _corpus_handles() -> Generator[tuple[duckdb.DuckDBPyConnection, CorpusStore]]:
    """
    回测消费面共享脚手架：corpus.duckdb 只读连接 + 语料树句柄。

    未建桥（corpus.duckdb 不存在）= 运维前置缺失，干净退出而非裸栈
    （correctness F5；引擎级 honest-missing 只覆盖"桥在、视图缺"形态）。
    """
    settings = get_settings()
    store = CorpusStore(settings.corpus_root)
    try:
        duck_con = corpus_duckdb.connect(settings)
    except (duckdb.Error, OSError) as exc:
        store.close()
        sys.stderr.write(f"corpus.duckdb 不可用（先跑 gold-build 建桥）: {exc}\n")
        raise SystemExit(2) from exc
    try:
        yield duck_con, store
    finally:
        duck_con.close()
        store.close()


@contextmanager
def _research_conn() -> Generator[sqlite3.Connection]:
    """研究库连接壳（票 07②）：backtest 表读写面，独立于运行面库。"""
    conn = connect_research()
    try:
        yield conn
    finally:
        conn.close()


def _cmd_research_backfill() -> None:
    """
    研究库历史回填（票 07②）：运行面库 backtest 四表一次性搬入研究库。

    保 id、幂等可重跑（研究库已有该 id 即跳过）；SQL 归 evaluation
    包（表归属闸），本入口只做编排。
    """
    settings = get_settings()
    if not settings.db_path.exists():
        raise SystemExit(f"运行面库不存在: {settings.db_path}")
    warm = connect_research(settings.research_db_path)  # 先建研究库 schema
    warm.close()
    ops = connect(settings.db_path)
    try:
        if not table_exists(ops, "backtest_runs"):
            raise SystemExit(
                f"运行面库无 backtest 表（未迁移？）：{settings.db_path}"
                + "——先跑 goalx-backend migrate"
            )
        copied = bt.backfill_research(ops, settings.research_db_path)
    finally:
        ops.close()
    for table, n in copied.items():
        logger.info("research-backfill {}: {} 行", table, n)
    sys.stdout.write(
        json.dumps(
            {"research_db": str(settings.research_db_path), "rows": copied},
            ensure_ascii=False,
        )
        + "\n"
    )
    logger.info(
        "research-backfill 共 {} 行 → {}",
        sum(copied.values()),
        settings.research_db_path,
    )


def _cmd_backtest(args: argparse.Namespace) -> None:
    """跑一次十年回测（gold 消费，walk-forward）+ 指标分层汇总。"""
    with (
        _corpus_handles() as (duck_con, store),
        task_conn() as conn,
        _research_conn() as rconn,
    ):
        haircut, source, n = hc.calibrated_haircut(conn)
        if args.haircut is not None:
            haircut, source = args.haircut, "cli"
        logger.info("haircut={} (source={}, n={})", haircut, source, n)
        params = bt.BacktestParams(
            competitions=tuple(args.competitions),
            start=args.start,
            end=args.end,
            haircut=haircut,
            parlay2=not args.no_parlay,
            min_train_matches=args.min_train,
            fair_source=args.fair_source,
            markets=tuple(args.markets),
            model=args.model,
            bet_filter=args.bet_filter,
            calibration=args.calibration,
            calibration_min_samples=args.calibration_min_samples,
            calibration_window=args.calibration_window,
            fusion=args.fusion,
            fusion_min_samples=args.fusion_min_samples,
            fusion_window=args.fusion_window,
            residual=args.residual,
            residual_min_samples=args.residual_min_samples,
            residual_window=args.residual_window,
        )
        result = bt.run_backtest(
            rconn,
            duck_con,
            store,
            params,
            label=args.label,
            fingerprint_conn=conn,
        )
        ev.compute_run_metrics(rconn, result.run_id)
        logger.info(
            "run={} predictions={} bets={} staked={:.2f} profit={:.2f} roi={:.2%}",
            result.run_id,
            result.predictions,
            result.bets,
            result.staked,
            result.profit,
            result.roi,
        )


def _cmd_baseline_compare() -> None:
    """基准分期质检报告 + psc/avgc 对照新 run(旧 run 保留,票 34)。"""
    with (
        _corpus_handles() as (duck_con, store),
        task_conn() as conn,
        _research_conn() as rconn,
    ):
        sys.stdout.write(
            json.dumps(
                baseline.baseline_quality_report(conn).model_dump(),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        haircut, _, _ = hc.calibrated_haircut(conn)
        results = baseline.run_baseline_comparison(
            rconn,
            duck_con,
            store,
            bt.BacktestParams(haircut=haircut),
            base_label=f"baseline-{datetime.now(tz=UTC).strftime('%Y%m%d')}",
            fingerprint_conn=conn,
        )
        for source, summary in results.items():
            ev.compute_run_metrics(rconn, int(summary["run_id"]))
            logger.info("baseline {} run={}", source, summary)


def _cmd_unlock_report() -> None:
    """hhad/ttg 解锁判定报告（票 15：真实 AH/OU 线 vs 1X2 反推，十年分 era×联赛）。"""
    with _corpus_handles() as (duck_con, store):
        payload = unlock_report.build_unlock_report(store, duck_con)
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    logger.info(
        "unlock-report: {} 场 → reports/{}.json/.md（门=报告+用户点头，引擎不动）",
        payload["n_matches"],
        unlock_report.REPORT_BASENAME,
    )


def _cmd_haircut_decade() -> None:
    """Haircut 十年重估（票 17：JC SP×gold era 收盘 + overround 1.129 对照）。"""
    settings = get_settings()
    store = CorpusStore(settings.corpus_root)
    duck_con = corpus_duckdb.connect(settings)
    try:
        with task_conn() as conn:
            payload = hcd.calibrate_decade(conn, store, duck_con)
    finally:
        duck_con.close()
        store.close()
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    for row in payload["calibrations"]:
        logger.info(
            "haircut-decade {} = {} (source={}, n={})",
            row["scope"],
            row["haircut"],
            row["source"],
            row["n_samples"],
        )


def _cmd_guardrail_report(args: argparse.Namespace) -> None:
    """护栏十年复验（票 18：触发面+被拦结局，只描述只举证，不改参数）。"""
    with _corpus_handles() as (duck_con, store):
        payload = gr.build_guardrail_report(store, duck_con, haircut=args.haircut)
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    logger.info(
        "guardrail-report: {} 场 → reports/{}.json/.md（参数变更=用户点头）",
        payload["n_matches_with_fair"],
        gr.REPORT_BASENAME,
    )


def _cmd_decade_skill(args: argparse.Namespace) -> None:
    """十年分时代技能报告（票 19：era×联赛×玩法，预测面+下注面+EV 实现率）。"""
    store = CorpusStore(get_settings().corpus_root)
    try:
        with task_conn() as conn, _research_conn() as rconn:
            payload = dsk.build_decade_skill_report(
                rconn, store, run_id=args.run_id, fingerprint_conn=conn
            )
    finally:
        store.close()
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    logger.info(
        "decade-skill-report: run={}（{} 预测/{} 注）→ reports/{}.json/.md",
        payload["run_id"],
        payload["n_predictions"],
        payload["n_bets"],
        dsk.REPORT_BASENAME,
    )


def _cmd_clv_decade(args: argparse.Namespace) -> None:
    """CLV 十年仪表盘（票 20：结构校验+早锁价值+线漂移，观察性不回写）。"""
    with (
        _corpus_handles() as (duck_con, store),
        task_conn() as conn,
        _research_conn() as rconn,
    ):
        payload = clvd.build_clv_decade_report(
            rconn, duck_con, store, run_id=args.run_id, fingerprint_conn=conn
        )
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    logger.info(
        "clv-decade-report: run={}（{} 腿/had 独立选样 {}）→ reports/{}.json/.md",
        payload["run_id"],
        payload["n_legs"],
        payload["n_unique_had_picks"],
        clvd.REPORT_BASENAME,
    )


def _cmd_news_signal(args: argparse.Namespace) -> None:
    """事件信号 PIT 回放（票 05：事件×残差×price-in，观察性）。"""
    with (
        _corpus_handles() as (duck_con, store),
        task_conn() as conn,
        _research_conn() as rconn,
    ):
        payload = nsw.build_news_signal_report(
            rconn, duck_con, store, run_id=args.run_id, fingerprint_conn=conn
        )
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    logger.info(
        "news-signal-report: run={}（{} 事件/{} 场）→ reports/{}.json/.md",
        payload["run_id"],
        payload["event_face"]["n_events"],
        payload["event_face"]["n_fixtures_with_events"],
        nsw.REPORT_BASENAME,
    )


def _cmd_guardian_news_funnel(args: argparse.Namespace) -> None:
    """扩样收敛表（票 07：表先行——候选→独立场次→可检验，预算另裁）。"""
    settings = get_settings()
    with (
        _corpus_handles() as (duck_con, store),
        task_conn() as conn,
        _research_conn() as rconn,
    ):
        # 票 07②：预测面读研究库；NewsContext 的运行面句柄（fixtures 等）仍走运行面库
        predicted, tested = ev.latest_done_run_prediction_sids(rconn)
        table = gs.convergence_table(
            gs.NewsContext(duck_con=duck_con, conn=conn, settings=settings),
            store,
            predicted,
            tested,
            competition=args.competition,
            seasons=tuple(args.seasons),
        )
    sys.stdout.write(json.dumps(table, ensure_ascii=False, indent=2) + "\n")
    logger.info(
        "guardian-news-funnel: {} 候选 → {} 独立场次 → {} 可检验（表先行,预算另裁）",
        table["total"]["candidates"],
        table["total"]["fixtures_with_news"],
        table["total"]["fixtures_testable"],
    )


def _cmd_guardian_news_pilot(args: argparse.Namespace) -> None:
    """NewsShock P0 抽取器试点（票 04：卫报→事件 silver，有界范围）。"""
    settings = get_settings()
    with _corpus_handles() as (duck_con, store), task_conn() as conn:
        report = gs.build_news_events(
            gs.NewsContext(duck_con=duck_con, conn=conn, settings=settings),
            store,
            competition=args.competition,
            seasons=tuple(args.seasons),
            article_limit=args.limit,
            fixture_budget=args.fixture_budget,
            extractor=gx.make_extractor(conn, settings),
        )
    sys.stdout.write(json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n")
    logger.info(
        "guardian-news-pilot: {} 场链接 / {} 候选 / {} 事件（{} 次调用）",
        report.linked_sids,
        report.candidates,
        report.events,
        report.llm_calls,
    )


def _cmd_bucket_reaudit(args: argparse.Namespace) -> None:
    """错价桶方向映射限定复核（票 03：一次性，结论上限=探索性）。"""
    with (
        _corpus_handles() as (duck_con, store),
        task_conn() as conn,
        _research_conn() as rconn,
    ):
        payload = bkt.build_bucket_reaudit_report(
            rconn, duck_con, store, run_id=args.run_id, fingerprint_conn=conn
        )
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    logger.info(
        "bucket-reaudit: run={} 桶臂 {} 注 roi {} vs 对照 roi {} → {}",
        payload["run_id"],
        payload["arm"]["n"],
        payload["arm"]["roi_flat"],
        payload["control"]["roi_flat"],
        payload["conclusion"],
    )


def _cmd_signal_attribution(args: argparse.Namespace) -> None:
    """信号级归因（票 21：残差×收盘族特征分桶，观察性探索报告）。"""
    with (
        _corpus_handles() as (duck_con, store),
        task_conn() as conn,
        _research_conn() as rconn,
    ):
        payload = sig.build_signal_attribution_report(
            rconn, duck_con, store, run_id=args.run_id, fingerprint_conn=conn
        )
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    logger.info(
        "signal-attribution-report: run={}（{} 样本）→ reports/{}.json/.md",
        payload["run_id"],
        payload["n_samples"],
        sig.REPORT_BASENAME,
    )


def _cmd_model_layers(args: argparse.Namespace) -> None:
    """模型层对照（model-iterate 票 01：校准变体 vs 基线十年对照）。"""
    treatments: list[tuple[str, int]] = []
    raw = list(args.treatment or [])
    for item in raw:
        spec = str(item)
        label, _, run_id = spec.rpartition(":")
        if not label or not run_id.isdigit():
            raise SystemExit(f"--treatment 格式 label:run_id（得到 {spec!r}）")
        treatments.append((label, int(run_id)))
    if not treatments:
        raise SystemExit("至少一个 --treatment label:run_id")
    store = CorpusStore(get_settings().corpus_root)
    try:
        with task_conn() as conn, _research_conn() as rconn:
            payload = mlp.build_model_layers_report(
                rconn,
                store,
                treatments=treatments,
                baseline_run_id=args.baseline_run_id,
                fingerprint_conn=conn,
            )
    finally:
        store.close()
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    logger.info(
        "model-layers-report: 基线 run={} × {} 变体 → reports/{}.json/.md",
        payload["baseline"]["run_id"],
        len(treatments),
        mlp.REPORT_BASENAME,
    )


def _cmd_market_lambda_report() -> None:
    """market-λ 联合反推 P1（票 16：1X2+OU 联合 vs 单 1X2 反推对照）。"""
    with _corpus_handles() as (duck_con, store):
        payload = market_lambda.build_market_lambda_report(store, duck_con)
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    logger.info(
        "market-lambda-p1: {} 场 → reports/{}.json/.md（转正=用户裁决）",
        payload["n_sampled"],
        market_lambda.REPORT_BASENAME,
    )


def _cmd_calibrate_haircut() -> None:
    """竞彩 vs 欧共识配对样本 → haircut 分布校准（票 05 分代：新场 corpus）。"""
    with task_conn() as conn, corpus_duckdb.reader() as duck_con:
        for row in hc.calibrate_haircuts(conn, duck_con=duck_con):
            logger.info("{}", row)


def _cmd_clv_reconcile() -> None:
    """已结算注单 CLV 对账 + 报表（票 75 起 srct 收盘锚接管）。"""
    with task_conn() as conn:
        with tasks.corpus_anchor() as anchor:
            stats = clv_mod.reconcile_clv(conn, duck_con=anchor)
        logger.info("recorded={} skipped={}", stats.recorded, len(stats.skipped))
        logger.info("report: {}", clv_mod.clv_report(conn).model_dump())


def _cmd_xg_compare(args: argparse.Namespace) -> None:
    """XG 融合实证对比（票 45）：报告打 stdout（语料须先 understat-sync 回填）。"""
    with task_conn() as conn:
        report = run_xg_comparison(
            conn,
            leagues=tuple(args.leagues) or rs_store.UNDERSTAT_DEFAULT_LEAGUES,
            seasons=tuple(args.seasons),
            train_seasons=args.train_seasons,
            shrink_k=tuple(float(k) for k in args.shrink_k),
        )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _cmd_pbo_feasibility(args: argparse.Namespace) -> None:
    """CSCV-PBO 可行性核验（票 33）：试验账只读分析，verdict JSON 打 stdout。"""
    verdict = pbo.assess_pbo_feasibility(pbo.load_trial_ledger(args.ledger))
    sys.stdout.write(
        json.dumps(pbo.verdict_payload(verdict), ensure_ascii=False, indent=2) + "\n"
    )


def _cmd_srct_collect(
    args: argparse.Namespace,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> None:
    """
    源T轨迹语料按日采集（票 55 切片 11）：raw+checkpoint 断点闭环。

    settings/client 注入口只服务测试接缝；缺省走真实配置与连接。
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
    try:
        if client is None:
            with httpx.Client(verify=browser_ssl_context()) as owned:
                stats = srct.collect_day(store, resolved, owned, date=args.date)
        else:
            stats = srct.collect_day(store, resolved, client, date=args.date)
    finally:
        store.close()
    payload = {
        "date": stats.date,
        "scope_sids": stats.scope_sids,
        "day_page_cached": stats.day_page_cached,
        "requests": stats.requests,
        "raw_new": stats.raw_new,
        "skipped": stats.skipped,
        "parsed_ok": stats.parsed_ok,
        "parse_failed": stats.parse_failed,
        "parse_success_rate": _parse_success_rate(stats),
        "xg_matches": stats.xg_matches,
        "asian_odds_nonempty": stats.asian_odds_nonempty,
        "asian_odds_books": stats.asian_odds_books,
        "over_down_nonempty": stats.over_down_nonempty,
        "over_down_books": stats.over_down_books,
        "detail_nonempty": stats.detail_nonempty,
        "analysis_nonempty": stats.analysis_nonempty,
        "bronze_repaired": stats.bronze_repaired,
        "failed": stats.failed,
        "parse_version": srct.PARSE_VERSION,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _parse_success_rate(stats: srct.SrctCollectStats) -> float | None:
    """解析成功率（无解析样本返回 None——空日不折算成 1.0）。"""
    denom = stats.parsed_ok + len(stats.parse_failed)
    return round(stats.parsed_ok / denom, 4) if denom else None


def _cmd_srct_silver(
    args: argparse.Namespace,
    *,
    settings: Settings | None = None,
) -> None:
    """
    源T silver 重物化 + DuckDB 只读桥（票 55/56 切片 14/16）。

    fixture_universe + xg_observation 从 bronze 幂等重建（分区/排序/版本
    戳），corpus.duckdb 视图刷新（archive_538 已入库则一并挂视图），附跨面
    冒烟（运行面 hist_matches 经 ATTACH 只读计数）。构建走
    silver_build_lock（票 02）：与 srct-remat 定时构建互斥，撞锁立即退出
    报持锁信息。settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
    try:
        with srct_remat.silver_build_lock(store):
            report = srct_silver.build_fixture_universe(store)
            xg_report = srct_silver.build_xg_observations(store)
            duckdb_path = corpus_duckdb.build_corpus_duckdb(store)
    except srct_remat.RematLockBusy as exc:
        raise _silver_lock_busy_exit("srct-silver", exc) from exc
    except sqlite3.OperationalError as exc:
        raise _silver_lock_unavailable_exit("srct-silver", exc) from exc
    finally:
        store.close()
    hist_count: int | None = None
    try:
        con = corpus_duckdb.connect(resolved)
        try:
            hist_count = corpus_duckdb.hist_matches_count(con)
        finally:
            con.close()
    except (duckdb.Error, OSError) as exc:
        logger.warning("srct-silver 跨面冒烟不可用：{}", exc)
    payload = {
        "fixture": asdict(report),
        "xg": asdict(xg_report),
        "duckdb": str(duckdb_path),
        "cross_face_smoke": {"goalx_hist_matches": hist_count},
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _silver_lock_busy_exit(cmd: str, exc: srct_remat.RematLockBusy) -> SystemExit:
    """手工 silver 构建撞锁（票 02 人裁决）：立即退出并报持锁信息。"""
    return SystemExit(f"{cmd}: silver 构建锁被占（{exc}），另一构建进行中，退出")


def _silver_lock_unavailable_exit(
    cmd: str, exc: sqlite3.OperationalError
) -> SystemExit:
    """
    获锁短事务撞锁表写锁忙（BEGIN IMMEDIATE 超时）。

    持锁方不可读，同样立即退出不构建（correctness F2，票 02）——裸栈
    不给人读信息。
    """
    return SystemExit(f"{cmd}: remat 锁表繁忙/不可读（{exc}），退出不构建")


def _cmd_srct_odds(
    args: argparse.Namespace,
    *,
    settings: Settings | None = None,
) -> None:
    """
    源T 赔率事件重物化 + DuckDB 只读桥（票 55/56 切片 15）。

    bookmaker 字典（数据派生全量，无 tier/is_core）+ odds_change_event
    （1x2+亚盘变化事件流）从 bronze 幂等重建，corpus.duckdb 视图刷新
    （含两新视图）。报告含门④转换完整性记账（unexplained_gap 须为 0）。
    构建走 silver_build_lock（票 02）：与 srct-remat/gold-rebuild 定时
    构建互斥，撞锁立即退出报持锁信息（人裁决 2026-11-02）。
    settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
    try:
        with srct_remat.silver_build_lock(store):
            book_report = srct_odds.build_bookmakers(store)
            odds_report = srct_odds.build_odds_change_events(store)
            duckdb_path = corpus_duckdb.build_corpus_duckdb(store)
    except srct_remat.RematLockBusy as exc:
        raise _silver_lock_busy_exit("srct-odds", exc) from exc
    except sqlite3.OperationalError as exc:
        raise _silver_lock_unavailable_exit("srct-odds", exc) from exc
    finally:
        store.close()
    payload = {
        "bookmaker": asdict(book_report),
        "odds": asdict(odds_report),
        "duckdb": str(duckdb_path),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_srct_market(
    args: argparse.Namespace,
    *,
    settings: Settings | None = None,
) -> None:
    """
    源T 规格v2 四新端点 silver 重物化 + DuckDB 桥（票 64）。

    market_quote（asian_odds+over_down 两 bronze → 初/即时/终报价表）、
    match_detail / match_analysis 按场摘要从 bronze 幂等重建，
    corpus.duckdb 视图刷新（含三新视图）。构建走 silver_build_lock
    （票 02）：与 gold-rebuild 定拍/其余手工构建互斥，撞锁立即退出报
    持锁信息。settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
    try:
        with srct_remat.silver_build_lock(store):
            market_report = srct_market.build_market_quotes(store)
            detail_report = srct_market.build_detail_faces(store)
            analysis_report = srct_market.build_analysis_faces(store)
            duckdb_path = corpus_duckdb.build_corpus_duckdb(store)
    except srct_remat.RematLockBusy as exc:
        raise _silver_lock_busy_exit("srct-market", exc) from exc
    except sqlite3.OperationalError as exc:
        raise _silver_lock_unavailable_exit("srct-market", exc) from exc
    finally:
        store.close()
    payload = {
        "market": asdict(market_report),
        "detail": asdict(detail_report),
        "analysis": asdict(analysis_report),
        "duckdb": str(duckdb_path),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_srct_gate(
    args: argparse.Namespace,
    *,
    settings: Settings | None = None,
) -> None:
    """
    Phase1 五门报告（票 55/56 切片 17）。

    三对账（fdhist×源T 场次 / PSC×cid177 / understat×源T xG）+ 门④转换
    完整性 + 门⑤管线健康 + 逐赛事×书商 coverage 矩阵；JSON/MD 两视图落
    语料树 reports/（数据面资产不进 repo），JSON 同步 stdout。门=报告+
    用户点头——overall 只给 provisional/pending_user，不放行。settings
    注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    # 门① 读运行面 admin_match_exclusions（A4 排除表）——先确保库已迁移到
    # 建表版本，缺表即炸（fail-closed），不静默跳过排除纪律。库文件缺失
    # 同样 fail-loud：connect 会新建空库，空面会出"全 insufficient"假报告
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    face = connect(resolved.db_path)
    try:
        migrate(face)
    finally:
        face.close()
    store = CorpusStore(resolved.corpus_root)
    try:
        report = corpus_gate.build_phase1_gate_report(store, resolved)
    finally:
        store.close()
    reports = store.root / corpus_gate.REPORTS_DIR
    payload = {**report, "md_path": str(reports / f"{corpus_gate.REPORT_BASENAME}.md")}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_morning_check() -> None:
    """09:05 晨检代码化（ingest-orchestration 票 01/05）：21 项只读红绿面。"""
    items = run_morning_check(get_settings())
    for item in items:
        sys.stdout.write(f"[{item.status}] {item.name}: {item.detail}\n")
    fails = sum(1 for item in items if item.is_fail)
    degraded = sum(1 for item in items if item.status == "degraded")
    ok = len(items) - fails - degraded
    sys.stdout.write(f"共 {len(items)} 项：fail={fails} degraded={degraded} ok={ok}\n")
    if fails:
        raise SystemExit(1)


def _cmd_news_pit_beat(args: argparse.Namespace) -> None:
    """新闻/伤停 PIT 前向夜拍（票 17）：手动/冒烟触发（定拍面在 daily-wrap）。"""
    kwargs: dict[str, int] = {}
    if args.request_cap:
        kwargs["request_cap"] = args.request_cap
    payload = tasks.news_pit_beat(**kwargs)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_rag_embed(args: argparse.Namespace) -> None:
    """
    RAG 增量嵌入（vision-build 票 06）：夜拍/晨检后增量 embed 新语料/新教训。

    幂等：content_hash 相同跳过（同模型口径重跑零效果）；换模型=对新
    model_id 全量重嵌（rag_embeddings 主键吸收，旧口径原地保留）。首跑
    会拉 fastembed 模型（运行期下载，约 0.22GB，缓存后零下载）。
    --limit=最多嵌 N 条**新**条目（correctness F5：过滤已嵌后计数——
    同参数重跑能推进分段回填，非前缀截断；无 limit=全量）。
    """
    settings = get_settings()
    store = CorpusStore(settings.corpus_root)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        if args.namespace == "guardian":
            embedder = rag.FastembedEmbedder(
                model_name=rag.GUARDIAN_MODEL, dim=rag.GUARDIAN_DIM
            )
        else:
            embedder = rag.FastembedEmbedder()
        if args.namespace == "lessons":
            namespace = rag.NAMESPACE_LESSON
            items: Iterable[tuple[str, str]] = rag.lesson_texts()
        else:
            namespace = rag.NAMESPACE_GUARDIAN
            items = rag.guardian_article_texts(store)
        stats = rag.embed_pending(
            conn,
            namespace=namespace,
            items=items,
            embedder=embedder,
            max_new=args.limit,
        )
    finally:
        conn.close()
        store.close()
    payload = {
        "namespace": namespace,
        "model_id": embedder.model_id,
        **stats,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    logger.info("rag-embed: {}", payload)


def _cmd_guardian_status() -> None:
    """卫报双轨报告（autoresearch 票 17）：回填存量清单 + 增量通道夜拍面（只读）。"""
    payload = tasks.guardian_status()
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _edge_scan_payload(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    duck_con: duckdb.DuckDBPyConnection | None,
    resolved: Settings,
) -> dict[str, Any]:
    """
    edge-scan 三面分派：--digest 周报 / --face decade 十年面 / 日扫。

    票 19：日扫 S4 与周报都接 corpus as-of 面——duck 桥在场时青铜重放
    注入（srct_asof 组合根注入，同 price-edge 先例）；缺席回退 silver
    最新视图口径（sharp_source 注记随读法）。
    """
    business_date = args.window_end or edge_scan.yesterday_business_date()
    if args.digest:
        store = CorpusStore(resolved.corpus_root)
        try:
            report = edge_scan.build_edge_digest(
                conn, week_end=business_date, store=store, duck_con=duck_con
            )
        finally:
            store.close()
        return {
            **report,
            "md_path": str(
                resolved.corpus_root
                / edge_scan.REPORTS_DIR
                / f"{edge_scan.REPORT_BASENAME}.md"
            ),
        }
    if args.face == "decade":
        research = connect_research(resolved.research_db_path)
        try:
            return edge_scan.run_s1_decade(conn, research_conn=research)
        finally:
            research.close()
    # corpus 在场才开语料树（青铜重放行源）；缺席不冷启建树
    store = CorpusStore(resolved.corpus_root) if duck_con is not None else None
    try:
        ctx = edge_scan.ScanContext(
            business_date=business_date,
            duck_con=duck_con,
            resolve_sid=lambda fid: mapping.srct_sid_for_fixture(conn, fid),
            asof_resolver=(
                partial(srct_asof.asof_rows, store, duck_con)
                if store is not None and duck_con is not None
                else None
            ),
        )
        names = edge_scan.ACTIVE_SCANNERS if args.scanner == "all" else (args.scanner,)
        return {
            "business_date": business_date,
            **{name: edge_scan.SCANNERS[name].run(conn, ctx) for name in names},
        }
    finally:
        if store is not None:
            store.close()


def _cmd_edge_scan(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    探索轨扫描器阵列（autoresearch 票 01/04）：日扫/回填/十年面/周报。

    默认=全部在册扫描器对昨日观察日跑一轮（同 wrap 夜拍）；--scanner 单
    扫；--face decade=s1 研究库十年回放（口径验证面）；--digest=周报
    （含 --mark 台账状态人裁）。JSON 同步 stdout。settings 注入口只服务
    测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    if args.mark:
        scanner, segment, status = (p.strip() for p in args.mark.split(","))
        conn = connect(resolved.db_path)
        try:
            migrate(conn)
            updated = edge_scan.mark_finding_status(
                conn, scanner=scanner, segment_key=segment, status=status
            )
        finally:
            conn.close()
        payload: dict[str, Any] = {"mark": args.mark, "updated": updated}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return
    duck_con = None
    if not args.no_corpus:
        try:
            duck_con = corpus_duckdb.connect(resolved)
        except (duckdb.Error, OSError) as exc:
            sys.stderr.write(f"corpus.duckdb 不可用（S4 drift 将记零）: {exc}\n")
    try:
        conn = connect(resolved.db_path)
        try:
            migrate(conn)  # 写台账前确保 schema 在位（task_conn 同款）
            payload = _edge_scan_payload(conn, args, duck_con, resolved)
        finally:
            conn.close()
    finally:
        if duck_con is not None:
            duck_con.close()
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_delay_audit(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    延迟生存率 Stage 1 到达率审计（票 07 预注册首跑/shadow 复跑）。

    四层分母+主指标（Wilson CI）按预注册口径只读账本出报告，落语料树
    reports/（JSON+MD 两视图），JSON 同步 stdout。收盘面=corpus duckdb
    源T 主锚收盘（clv.srct_closing_prob 同口径；缺桥 fail-loud——
    没有收盘的主指标不完整，不静默降级。--skip-close 仅供无语料环境
    的口径冒烟）。settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    duck_con: duckdb.DuckDBPyConnection | None = None
    close_provider: delay_survival.CloseProvider | None = None
    if not args.skip_close:
        duck_con = corpus_duckdb.connect(resolved)
        duck_face = duck_con

        def provide(
            home: str, away: str, kickoff_utc: str, selection: str
        ) -> tuple[float, str, str] | None:
            return clv_mod.srct_closing_prob(
                duck_face, home, away, kickoff_utc, selection
            )

        close_provider = provide
    store = CorpusStore(resolved.corpus_root)
    try:
        conn = connect(resolved.db_path, readonly=True)
        try:
            report = delay_survival.build_arrival_audit_report(
                conn,
                date_from=args.date_from,
                date_to=args.date_to,
                close_provider=close_provider,
                store=store,
            )
        finally:
            conn.close()
    finally:
        store.close()
        if duck_con is not None:
            duck_con.close()
    reports = store.root / delay_survival.REPORTS_DIR
    payload = {
        **report,
        "md_path": str(reports / f"{delay_survival.REPORT_BASENAME}.md"),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_price_edge_audit(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    价格存活检查点读数（票 09 预注册；双周检查点，11-13 首读）。

    按预注册口径只读运行面库出报告，落语料树 reports/（JSON+MD 两视图），
    JSON 同步 stdout。共识参考走 corpus duckdb（12 书均值+Shin，PIT
    as-of 确认时刻）；--no-corpus 仅供无语料环境的口径冒烟（corpus 有链
    场次全落 missing_reference，不冒充）。settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    duck_con: duckdb.DuckDBPyConnection | None = None
    if not args.no_corpus:
        try:
            duck_con = corpus_duckdb.connect(resolved)
        except (duckdb.Error, OSError) as exc:
            sys.stderr.write(f"corpus.duckdb 不可用（先跑 gold-build 建桥）: {exc}\n")
            raise SystemExit(2) from exc
    store = CorpusStore(resolved.corpus_root)
    try:
        conn = connect(resolved.db_path, readonly=True)
        try:
            report = pe.build_price_edge_report(
                conn,
                date_from=args.date_from,
                date_to=args.date_to,
                duck_con=duck_con,
                store=store,
                asof_resolver=(
                    # 票 04：历史时点青铜重放（ingest 层实现，组合根注入；
                    # corpus 缺席时无历史重放面，回退最新视图口径）
                    partial(srct_asof.asof_rows, store, duck_con)
                    if duck_con is not None
                    else None
                ),
            )
        except sqlite3.OperationalError as exc:
            # <v39 未迁移库（correctness F6）：记录面缺席=管道未部署，
            # 干净退出给迁移提示，不裸栈
            sys.stderr.write(f"运行面库缺记录面（先 migrate 到 v39）: {exc}\n")
            raise SystemExit(2) from exc
        finally:
            conn.close()
    finally:
        store.close()
        if duck_con is not None:
            duck_con.close()
    reports = store.root / pe.REPORTS_DIR
    payload = {
        **report,
        "md_path": str(reports / f"{pe.REPORT_BASENAME}.md"),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_repair_sale_status(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    历史销售态语义一次性修正（票 09；dry-run 默认，--apply 才动库）。

    按计算器端点 raw 证据重解析（修正后 SELL_STATUS_MAP）对账
    sale_statuses 与 price_deviation_ledger 历史行：旧映射把在售记成
    停售、未开售记成 unknown。dry-run 数据面零改动（migrate 幂等 DDL
    先行）；--apply 前置=v32 已迁移，UPDATE 防线触发器在原子壳内
    摘除→改→按原文重建（中途崩全回滚）。JSON 审计同步 stdout。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    conn = connect(resolved.db_path)
    try:
        migrate(conn)
        report = sporttery.repair_sale_status_semantics(
            conn, raw_root=resolved.observations_dir, apply=bool(args.apply)
        )
    finally:
        conn.close()
    payload = {
        "applied": report.applied,
        "observations": report.observations,
        "payloads_read": report.payloads_read,
        "payloads_missing": report.payloads_missing,
        "sale_statuses": {
            "inspected": report.sale_statuses_inspected,
            "fixes": report.sale_statuses_fixes,
        },
        "ledger": {"inspected": report.ledger_inspected, "fixes": report.ledger_fixes},
        "unresolved_rows": report.unresolved_rows,
        "sale_statuses_unlinked": report.sale_statuses_unlinked,
        "ledger_unlinked": report.ledger_unlinked,
        "by_transition": report.by_transition,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_jc_collect(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    竞彩官方 SP 历史采集（票 70）：getFixedBonusV1 → jc provider raw+bronze。

    一场一请求全量返回（幂等缓存）；oddsHistory={} 合法空只落 raw。
    settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
    try:
        with httpx.Client(verify=browser_ssl_context()) as client:
            stats = jc.JcCollectStats()
            for match_id in args.match_id:
                jc.collect_match(store, resolved, client, match_id, stats=stats)
    finally:
        store.close()
    sys.stdout.write(json.dumps(asdict(stats), ensure_ascii=False, indent=2) + "\n")


def _cmd_jc_backfill(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    JC 历史回填批（票 67）：uniform 按日反查 → fixedBonus 逐场（官方域）。

    --day-cap 抽样冒烟；done 日零成本跳过（裸键判据）。settings 注入口
    只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
    budget = srct.NightBudget(request_cap=args.request_cap)
    stats = jc_backfill.JcBackfillStats(date_from=args.date_from, date_to=args.date_to)
    try:
        with httpx.Client(verify=browser_ssl_context()) as client:
            stats = jc_backfill.backfill_range(
                store,
                resolved,
                client,
                date_to=args.date_to,
                date_from=args.date_from,
                budget=budget,
                day_cap=args.day_cap,
            )
    except srct.NightStop as stop:
        stats.stopped = stop.reason  # 预算尽=正常停（进度已落库，独立窗可续）
    finally:
        store.close()
    sys.stdout.write(json.dumps(asdict(stats), ensure_ascii=False, indent=2) + "\n")


def _cmd_jc_audit(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    JC audit 面（票 67）：TTG 按年密度 + 存档最早年限 + cid1129 对账。

    --date 抽样对账日（可多次，须为语料已有日页的日期）；存档探针二分
    约 5-10 请求（官方域）。报告 JSON 落 stdout。
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
    density = jc_audit.ttg_density_by_year(store)
    earliest: int | None = None
    report: dict[str, object] = {
        "ttg_density_by_year": {y: asdict(d) for y, d in density.by_year.items()},
    }
    try:
        if args.probe_archive:
            with httpx.Client(verify=browser_ssl_context()) as client:
                earliest = jc_audit.earliest_archive_year(client, resolved)
        if args.date:
            with httpx.Client(verify=browser_ssl_context()) as client:
                recon = jc_audit.reconcile_cid1129(
                    store, resolved, client, dates=args.date
                )
            report["reconcile"] = {
                "matched": recon.matched,
                "unmatched_names": recon.unmatched_names,
                "comparable": recon.comparable,
                "consistent": recon.consistent,
                "consistency_rate": recon.consistency_rate,
                "rows": [asdict(r) for r in recon.rows],
            }
    finally:
        store.close()
    report["earliest_archive_year"] = earliest
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _cmd_jc_silver(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    Jc silver 重物化 + DuckDB 桥（票 72）：jc_sp_change_event 幂等重建。

    变化事件流语义与书商层 odds_change_event 同构（心跳丢/A→B→A 保留/
    同刻并列规则）；报告含门④记账（unexplained_gap 须为 0）。
    kickoff 冗余列优先注入源T canonical 覆写（票 77 映射链；旧桥缺席/
    无链场次回退 jc 台账值）。
    """
    resolved = settings if settings is not None else get_settings()
    with task_conn() as conn, corpus_anchor() as duck_con:
        overrides = mapping.srct_kickoff_overrides(conn, duck_con)
    store = CorpusStore(resolved.corpus_root)
    try:
        report = jc_silver.build_sp_change_events(store, kickoff_overrides=overrides)
        duckdb_path = corpus_duckdb.build_corpus_duckdb(store)
    finally:
        store.close()
    payload = {
        **asdict(report),
        "duckdb": str(duckdb_path),
        "kickoff_overrides": len(overrides),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_srct_results(args: argparse.Namespace) -> None:
    """源T 日页赛果物化 + 覆盖差集报告（票 76，零新请求）。"""
    payload: dict[str, object]
    with task_conn() as conn, corpus_anchor() as duck_con:
        coverage = srct_results.coverage_diff_report(conn, duck_con)
        if args.coverage_only:
            payload = {"coverage": coverage}
        else:
            stats = srct_results.materialize_results(conn, duck_con)
            payload = {**stats.as_dict(), "coverage_after": coverage}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_elo_build(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    自算 Elo 全量重算 + silver 物化 + DuckDB 桥（票 78，幂等零请求）。

    fd hist 热身（英文名空间）→ 重叠期配对桥 → fixture_universe 折叠
    （中文名空间）→ elo_self 数据集；报告含逐留出季 RPS 对照与头部排名。
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
    try:
        with task_conn() as conn, corpus_anchor() as duck_con:
            if duck_con is None:
                sys.stderr.write("corpus.duckdb unavailable, cannot build\n")
                raise SystemExit(2)
            report = elo_silver.build_elo_self(store, conn, duck_con)
        duckdb_path = corpus_duckdb.build_corpus_duckdb(store)
    finally:
        store.close()
    payload = {**asdict(report), "duckdb": str(duckdb_path)}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_gold_build(
    args: argparse.Namespace, *, settings: Settings | None = None
) -> None:
    """
    Gold 特征面全量重算（backtest-decade B 相，幂等零请求）。

    委派 tasks.gold_rebuild（ADR-0005 单一实现，misprice 票 03① 起定拍
    08:40 同源）：余量护栏先行（<6GiB 整轮 deferred 零写入），银段先重物化
    market_quote（remat 不覆盖，曾停摆月余），再 gold 全量重算 +
    corpus.duckdb 刷新。settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    try:
        payload = tasks.gold_rebuild(settings=resolved)
    except FileNotFoundError:
        # 运行面库缺失（任务体首查）：原样 fail-loud，不并入桥断线文案
        raise
    except (duckdb.Error, OSError) as exc:
        message = "corpus.duckdb unavailable（先跑 srct-silver/"
        sys.stderr.write(message + "srct-odds/srct-market）: " + str(exc) + "\n")
        raise SystemExit(2) from exc
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_mapping_sync(args: argparse.Namespace) -> None:
    """跨源映射同步（票 77）：物化链 + kickoff 校准 + 别名补源。"""
    report = mapping_sync()
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _cmd_mapping_audit(args: argparse.Namespace) -> None:
    """映射审计报告（票 77）：分桶未映射/歧义率 + 起步门。"""
    report = mapping_audit()
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _cmd_clubelo_elo(args: argparse.Namespace) -> None:
    """点时 Elo 按中文 canonical 队名查询（票 77 别名桥验收）。"""
    report = clubelo_elo_for_team(args.team, date.fromisoformat(args.date))
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _cmd_tm_coach_report() -> None:
    """TM 教练层覆盖报告（autoresearch 票 18 验收①；只读）。"""
    report = tm_coach_report()
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _cmd_tm_coach_crosscheck(args: argparse.Namespace) -> None:
    """交叉核验报告（autoresearch 票 18 验收②；srct 教练名，只读）。"""
    report = tm_coach_crosscheck(window_days=args.window_days)
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _cmd_six_axes_derive() -> None:
    """六轴 ②④ 两面重建（vision-build 票 09 运维步；幂等全量）。"""
    report = six_axes_derive()
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _cmd_six_axes_report() -> None:
    """六轴只读报告（票 09 验收面：②覆盖切片 + ④派生现态）。"""
    report = six_axes_report()
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _cmd_archive_538(
    args: argparse.Namespace,
    *,
    settings: Settings | None = None,
    csv_path: Path | None = None,
) -> None:
    """
    538 终版档案一次性入库（票 55/56 切片 16）。

    原档进 raw + CorpusScope silver 小表（幂等重建；树内已有 raw 时源档
    缺席也可重建）。settings/csv_path 注入口只服务测试接缝；缺省源档取
    research/21 留档。
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
    try:
        report = archive538.ingest_archive_538(
            store, csv_path if csv_path is not None else args.csv
        )
        duckdb_path = corpus_duckdb.build_corpus_duckdb(store)
    finally:
        store.close()
    payload = {**asdict(report), "duckdb": str(duckdb_path)}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def completeness_report(
    conn: sqlite3.Connection,
    *,
    competitions: tuple[str, ...],
    seasons: tuple[str, ...],
) -> dict[str, Any]:
    """Fdhist 语料完整性：每联赛每季行数/收盘缺口；缺季即缺口（显式列出）。"""
    coverage = {
        (str(row["competition"]), str(row["season"])): row
        for row in rs_store.hist_season_coverage(conn, competitions)
    }
    per_competition: dict[str, dict[str, dict[str, int]]] = {}
    total_rows = 0
    for competition in competitions:
        entry: dict[str, dict[str, int]] = {}
        for season in seasons:
            row = coverage.get((competition, season))
            if row is None:
                entry[season] = {"rows": 0}
                continue
            total_rows += int(row["rows"])
            entry[season] = {
                "rows": int(row["rows"]),
                "psc_missing": int(row["psc_missing"]),
                "avgc_missing": int(row["avgc_missing"]),
            }
        per_competition[competition] = entry
    return {
        "seasons": list(seasons),
        "total_rows": total_rows,
        "per_competition": per_competition,
    }


def _cmd_corpus_report(args: argparse.Namespace) -> None:
    """十年语料报表（票 46）：完整性 + 可选 openfootball 交叉验证。"""
    seasons = tuple(args.seasons) if args.seasons else fdhist.SEASONS
    with task_conn() as conn:
        report: dict[str, object] = {
            "completeness": completeness_report(
                conn, competitions=fdhist.FD_COMPETITIONS, seasons=seasons
            )
        }
        if args.cross_check:
            with httpx.Client(verify=browser_ssl_context()) as client:
                report["cross_check"] = openfootball.cross_check_dict(
                    openfootball.cross_check_fdhist(
                        conn, get_settings(), client, seasons=seasons
                    )
                )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _cmd_pool_replay_report(args: argparse.Namespace) -> None:
    """彩池 v2 搏冷十年复验报告（票 51）：只读，纯函数确定性。"""
    seasons = tuple(args.seasons) if args.seasons else fdhist.SEASONS
    with task_conn() as conn:
        report = pool_replay_report(
            conn, competitions=fdhist.FD_COMPETITIONS, seasons=seasons
        )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _cmd_drift_replay_report(args: argparse.Namespace) -> None:
    """开→收漂移复验报告（票 54）：只读，纯函数确定性。"""
    seasons = tuple(args.seasons) if args.seasons else fdhist.SEASONS
    with task_conn() as conn:
        report = drift_replay_report(
            conn, competitions=fdhist.FD_COMPETITIONS, seasons=seasons
        )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _cmd_pool_backfill(args: argparse.Namespace) -> None:
    """彩池历史彩果回填：官方 V2 逐期倒查（票 68；已有官方面自动跳过）。"""
    payload = tasks.pool_backfill(periods=args.periods)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _cmd_set_alias(args: argparse.Namespace) -> None:
    """人工覆盖：给 canonical 球队追加 manual 别名（即时生效，票 25）。"""
    with task_conn() as conn:
        try:
            team_align.set_manual_alias(conn, args.team, args.alias)
        except LookupError as exc:
            raise SystemExit(str(exc)) from exc
        logger.info("alias set: {} -> {}", args.alias, args.team)


def _cmd_align_report() -> None:
    """五大 hist 队名对当前别名的覆盖率报告。"""
    with task_conn() as conn:
        report = team_align.build_alignment_report(conn)
        for competition, item in sorted(report.per_competition.items()):
            logger.info(
                "{}: {}/{} matched", competition, item["matched"], item["total"]
            )
        logger.info("overall coverage: {:.1%} ({})", report.coverage, report.status)
        if report.status == "degraded":
            logger.warning(
                "alignment degraded：覆盖率 < {:.0%}（identity 票 02 守门阈值）",
                team_align.ALIGNMENT_DEGRADED_COVERAGE,
            )
        if report.unmatched:
            logger.warning("unmatched: {}", report.unmatched)


def _cmd_accuracy_score() -> None:
    """前瞻准确率评分（auto-paper 票 02）：已结算 ML 覆盖场逐玩法评（幂等）。"""
    from goalx_backend.evaluation import accuracy  # noqa: PLC0415

    with task_conn() as conn:
        stats = accuracy.score_settled_fixtures(conn)
    logger.info("accuracy scores: {}", stats)


def _cmd_seed_demo() -> None:
    """写入演示/E2E 种子（拒绝非隔离库；票 36）。"""
    from goalx_backend.data.ingest.demo import seed_demo  # noqa: PLC0415

    conn = connect()
    try:
        migrate(conn)
        info = seed_demo(conn)
        logger.info("demo seeded: {}", info)
    finally:
        conn.close()


def _cmd_e2e_corpus() -> None:
    """生成 corpus 主链 e2e 夹具（boundary-hardening 票 06；须隔离库）。"""
    from goalx_backend.data.ingest.e2e_corpus import (  # noqa: PLC0415
        build,
        ensure_isolated_db,
        require_isolated_paths,
    )

    require_isolated_paths(get_settings())  # 裸跑守卫:缺省路径=主库/真实语料树,拒
    conn = connect()
    try:
        migrate(conn)
        ensure_isolated_db(conn)  # 同 seed-demo 红线：拒绝写主库伪造实采
    finally:
        conn.close()
    manifest = build(get_settings())
    logger.info("e2e corpus fixture: {}", manifest["manifest_path"])


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 随票累加的平铺 argparse
    """CLI 参数。"""
    parser = argparse.ArgumentParser(prog="goalx", description="goalx 运维命令")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="执行 schema 迁移")
    sub.add_parser("ingest-jingcai", help="手动拉一次竞彩快照")
    sub.add_parser(
        "reprocess-sporttery",
        help="重解析 sporttery 原始证据入库(票 38 存量单固修正,幂等)",
    )
    sub.add_parser("ingest-odds", help="手动拉一次欧赔(走 credit 护栏)")
    sub.add_parser(
        "propline-capture", help="手动拉一次 PropLine 快照(备用源重启入口,票 06)"
    )
    sub.add_parser("ingest-hist", help="导入五大三季历史底座")
    sub.add_parser("audit-ledger", help="只读核查旧账与更正历史")
    sub.add_parser("settle", help="手动结算批跑")
    sub.add_parser(
        "sync-draw-results",
        help="手动跑一次赛果自动同步(官方源,票 44)",
    )
    sub.add_parser(
        "official-reconcile",
        help="赛果日终审计+清单报告(源D+openfootball,票 44)",
    )
    train = sub.add_parser("train-models", help="训练五大 DC 模型(票 26)")
    train.add_argument(
        "--competitions",
        nargs="*",
        default=list(TIER1_COMPETITIONS),
        help="fd 联赛代码(默认五大,可加 N1 荷甲)",
    )
    train.add_argument(
        "--half-life", type=float, default=365.0, help="时间衰减半衰期(天)"
    )
    train.add_argument(
        "--bootstrap", type=int, default=50, help="bootstrap 重采样数(0 关闭)"
    )
    forecast = sub.add_parser("forecast", help="生成一个业务日的 ML Forecast(票 27)")
    forecast.add_argument("--date", help="业务日 YYYY-MM-DD(默认今天,北京时间)")
    sub.add_parser("align-report", help="hist 队名对齐覆盖率报告(票 25)")
    sub.add_parser(
        "accuracy-score",
        help="前瞻准确率评分(已结算ML覆盖场,幂等,auto-paper票02)",
    )
    alias = sub.add_parser("set-alias", help="人工覆盖球队别名(票 25)")
    alias.add_argument("team", help="canonical 球队名(中文)")
    alias.add_argument("alias", help="外部别名(如 fd 英文名)")
    backtest = sub.add_parser(
        "backtest", help="跑一次十年回测(gold 消费,walk-forward,票 13)"
    )
    sub.add_parser(
        "research-backfill",
        help="运行面库 backtest 历史一次性搬入研究库(票 07②,幂等)",
    )
    backtest.add_argument("--label", default="manual", help="run 标签")
    backtest.add_argument(
        "--haircut",
        type=float,
        default=None,
        help="auto=校准值(不足回落默认) 或直接给数值(如 0.10)；缺省=auto",
    )
    backtest.add_argument(
        "--competitions",
        nargs="*",
        default=list(bt.DECADE_COMPETITIONS),
        help="fd 联赛代码(缺省=11 重叠联赛全量)",
    )
    backtest.add_argument(
        "--start",
        default=bt.DECADE_START,
        help="窗口起点(北京墙钟日,含端点;此前场次只进训练池)",
    )
    backtest.add_argument(
        "--end",
        default=None,
        help="窗口终点(北京墙钟日,含端点;缺省=数据尾)",
    )
    backtest.add_argument("--no-parlay", action="store_true", help="关闭 2串1 模拟")
    backtest.add_argument(
        "--markets",
        nargs="*",
        default=["had", "hhad", "ttg"],
        help="下注玩法(2026-10-10 解锁裁决后缺省三玩法)",
    )
    backtest.add_argument(
        "--min-train",
        type=int,
        default=100,
        help="每周最小训练样本",
    )
    backtest.add_argument(
        "--fair-source",
        choices=("auto", "psc", "avgc"),
        default="auto",
        help="公允基准来源: auto=时代感知正典链; psc/avgc=只用该源(票 34)",
    )
    backtest.add_argument(
        "--bet-filter",
        choices=list(bt.VALID_BET_FILTERS),
        default="off",
        help="下注过滤(票 04):off=缺省;buckets=错价桶门控;random=同接受率安慰剂",
    )
    backtest.add_argument(
        "--model",
        choices=list(bt.VALID_MODELS),
        default="dc",
        help="模型线(票 02):dc=缺省 Dixon-Coles;gbm=lightgbm had-only",
    )
    backtest.add_argument(
        "--calibration",
        choices=list(CALIBRATION_KINDS),
        default="off",
        help="校准层(票 01):off=缺省不变;temperature/isotonic=按联赛walk-forward",
    )
    backtest.add_argument(
        "--calibration-min-samples",
        type=int,
        default=200,
        help="校准冷启动阈值(联赛内历史不足即透传)",
    )
    backtest.add_argument(
        "--calibration-window",
        type=int,
        default=1000,
        help="校准拟合尾随窗样本数(0=全历史)",
    )
    backtest.add_argument(
        "--fusion",
        choices=list(FUSION_MODES),
        default="off",
        help="市场融合层(票 02):off=缺省不变;global/era=α 尾随窗网格学习",
    )
    backtest.add_argument(
        "--fusion-min-samples",
        type=int,
        default=200,
        help="融合冷启动阈值(联赛内历史不足即不融合)",
    )
    backtest.add_argument(
        "--fusion-window",
        type=int,
        default=1000,
        help="α 学习尾随窗样本数(0=全历史)",
    )
    backtest.add_argument(
        "--residual",
        choices=list(RESIDUAL_MODES),
        default="off",
        help="判别层(票 03):off=缺省不变;on=错价桶特征残差调整;"
        + "placebo=特征错位安慰剂对照",
    )
    backtest.add_argument(
        "--residual-min-samples",
        type=int,
        default=300,
        help="判别层冷启动阈值(联赛内历史不足即透传)",
    )
    backtest.add_argument(
        "--residual-window",
        type=int,
        default=1000,
        help="判别层拟合尾随窗样本数(0=全历史)",
    )
    sub.add_parser("baseline-compare", help="基准分期质检+psc/avgc 对照新 run(票 34)")
    guardrail = sub.add_parser(
        "guardrail-report",
        help="护栏十年复验(票 18:触发面+被拦结局,只描述不改参数)",
    )
    guardrail.add_argument(
        "--haircut",
        type=float,
        default=0.10,
        help="护栏阈值用的 haircut(与引擎 run 同值复验;默认 0.10)",
    )
    sub.add_parser(
        "haircut-decade",
        help="haircut 十年重估(票 17:JC SP×gold 收盘,sp-close:* 行,引擎默认不动)",
    )
    sub.add_parser(
        "market-lambda-report",
        help="market-λ 联合反推 P1(票 16:1X2+OU 联合 vs 单反推,不接引擎)",
    )
    sub.add_parser(
        "unlock-report",
        help="hhad/ttg 解锁判定报告(票 15:真实线 vs 反推,分era×联赛,不改动引擎)",
    )
    decade_skill_parser = sub.add_parser(
        "decade-skill-report",
        help="十年分时代技能报告(票 19:era×联赛×玩法,预测面+下注面)",
    )
    decade_skill_parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="目标 run(缺省=最新 done run;冒烟 run 会顶替同名报告,"
        + "十年报告建议显式指定 run-id)",
    )
    clv_decade_parser = sub.add_parser(
        "clv-decade-report",
        help="CLV 十年仪表盘(票 20:结构校验+早锁价值+线漂移,观察性)",
    )
    clv_decade_parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="目标 run(缺省=最新 done run;十年报告建议显式指定)",
    )
    signal_parser = sub.add_parser(
        "signal-attribution-report",
        help="信号级归因(票 21:残差×收盘族特征分桶,观察性探索)",
    )
    signal_parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="目标 run(缺省=最新 done run;十年报告建议显式指定)",
    )
    news_parser = sub.add_parser(
        "guardian-news-pilot",
        help="NewsShock P0 抽取器试点(票 04:卫报→事件silver,有界)",
    )
    news_parser.add_argument(
        "--competition", default="E0", help="fd 联赛码(缺省 E0 英超)"
    )
    news_parser.add_argument(
        "--seasons",
        nargs="*",
        default=["2024-25", "2025-26"],
        help="试点赛季(缺省 2024-25+2025-26;注意 E0 轨迹收盘自 2024-25 起,"
        + "2023-24 无引擎预测面)",
    )
    news_parser.add_argument(
        "--limit",
        type=int,
        default=400,
        help="LLM 抽取文章上限(成本护栏)",
    )
    news_parser.add_argument(
        "--fixture-budget",
        type=int,
        default=None,
        help="按独立场次的有界预算(票07;预算在 --limit 文章池内生效)",
    )
    funnel_parser = sub.add_parser(
        "guardian-news-funnel",
        help="扩样收敛表(票07:候选→独立场次→可检验;表先行预算另裁)",
    )
    funnel_parser.add_argument(
        "--competition",
        type=str,
        default="E0",
        help="联赛码(缺省 E0)",
    )
    funnel_parser.add_argument(
        "--seasons",
        type=str,
        nargs="+",
        default=["2024-25", "2025-26"],
        help="赛季(缺省 2024-25+2025-26 两季,票07 裁决)",
    )
    bucket_reaudit_parser = sub.add_parser(
        "bucket-reaudit",
        help="错价桶方向映射限定复核(票03:严格正侧+等量分层对照,一次性)",
    )
    bucket_reaudit_parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="源 run(须与 signal-attribution 报告同 run;缺省=最新 done)",
    )
    news_signal_parser = sub.add_parser(
        "news-signal-report",
        help="事件信号PIT回放(票 05:事件×残差×price-in,观察性)",
    )
    news_signal_parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="预测 run(残差来源;缺省=最新 done)",
    )
    model_layers_parser = sub.add_parser(
        "model-layers-report",
        help="模型层对照(model-iterate 票 01:校准变体 vs 基线)",
    )
    model_layers_parser.add_argument(
        "--baseline-run-id",
        type=int,
        required=True,
        help="基线 run(通常=十年基线 run=5)",
    )
    model_layers_parser.add_argument(
        "--treatment",
        action="append",
        required=True,
        help="变体 label:run_id(可重复,如 t01-iso:6)",
    )
    sub.add_parser("calibrate-haircut", help="haircut 配对样本校准(票 30)")
    sub.add_parser("clv-reconcile", help="已结算注单 CLV 对账+报表(票 32)")
    elo_query = sub.add_parser(
        "clubelo-elo",
        help="点时 Elo 按中文 canonical 队名查询(票 77 别名桥)",
    )
    elo_query.add_argument("--team", required=True, help="canonical 中文队名")
    elo_query.add_argument("--date", required=True, help="查询日 YYYY-MM-DD")
    sub.add_parser(
        "tm-coach-report",
        help="TM教练层联赛×赛季覆盖报告(autoresearch票18;只读)",
    )
    tm_cross = sub.add_parser(
        "tm-coach-crosscheck",
        help="srct教练名交叉核验(autoresearch票18;只读,诚实小样本)",
    )
    tm_cross.add_argument(
        "--window-days", type=int, default=45, help="边界前后观察窗(默认 45 日)"
    )
    sub.add_parser(
        "six-axes-derive",
        help="六轴②阵型族条件面+④底蕴硬层重建(vision-build票09;幂等)",
    )
    sub.add_parser(
        "six-axes-report",
        help="六轴只读报告(票09验收:②覆盖英超/英冠切片+④派生现态)",
    )
    sub.add_parser(
        "mapping-sync",
        help="跨源映射同步:sid 链+kickoff 校准+别名补源(含 srct_en 英文活水与普查)",
    )
    sub.add_parser(
        "mapping-audit",
        help="映射审计:分桶未映射/歧义率+起步门(票 77)",
    )
    xgcmp = sub.add_parser(
        "xg-compare", help="xG 融合实证对比报告(票 45;语料先 understat-sync)"
    )
    xgcmp.add_argument("--seasons", nargs="+", required=True, help="目标赛季起始年")
    xgcmp.add_argument(
        "--leagues", nargs="*", default=[], help="understat slug(默认五大)"
    )
    xgcmp.add_argument(
        "--train-seasons", type=int, default=3, help="训练窗赛季数(默认 3)"
    )
    xgcmp.add_argument(
        "--shrink-k", nargs="*", default=["6"], help="收缩先验 k 值列表(默认 6)"
    )
    pbo_parser = sub.add_parser(
        "pbo-feasibility",
        help="CSCV-PBO 可行性核验(票 33;试验账只读,verdict JSON)",
    )
    pbo_parser.add_argument(
        "--ledger", required=True, help="EM 试验账 CSV 路径(轮 1 冻结档)"
    )
    corpus = sub.add_parser(
        "corpus-report", help="十年语料报表(票 46;完整性+可选交叉验证)"
    )
    corpus.add_argument("--seasons", nargs="*", help="fd 季键(默认 1617..2627)")
    corpus.add_argument(
        "--cross-check",
        action="store_true",
        help="附 openfootball 比分对 fdhist 交叉验证(拉重叠联赛赛季文件)",
    )
    backfill = sub.add_parser(
        "pool-backfill",
        help="彩池历史彩果回填(票68;官方V2逐期倒查,已有官方面跳过)",
    )
    backfill.add_argument("--periods", type=int, default=20, help="回溯期数(默认 20)")
    replay = sub.add_parser(
        "pool-replay-report",
        help="彩池v2搏冷十年复验报告(票 51;冷门EV分布/分期/早期镜,只读)",
    )
    replay.add_argument("--seasons", nargs="*", help="fd 季键(默认 1617..2627)")
    drift = sub.add_parser(
        "drift-replay-report",
        help="开→收漂移复验报告(票 54;早锁vs等待/价值蒸发/实测ROI,只读)",
    )
    drift.add_argument("--seasons", nargs="*", help="fd 季键(默认 1617..2627)")
    srct_collect = sub.add_parser(
        "srct-collect",
        help="源T轨迹语料按日采集(票55切片11;端点模板须进本地.env,断点可续)",
    )
    srct_collect.add_argument("--date", required=True, help="业务日 YYYY-MM-DD")
    srct_results_parser = sub.add_parser(
        "srct-results",
        help="源T日页赛果物化+覆盖差集报告(票76;零新请求,--coverage-only只读)",
    )
    srct_results_parser.add_argument(
        "--coverage-only",
        action="store_true",
        help="只出竞彩待出×源T覆盖差集报告,不落事实",
    )
    sub.add_parser(
        "elo-build",
        help="自算Elo全量重算+silver物化(票78;fd热身+配对桥+语料折叠,幂等)",
    )
    sub.add_parser(
        "multiline-bounds-report",
        help="OU 端点敏感性分析+AH 联合一致性(measure-10:B线可行性,抽样口径)",
    )
    sub.add_parser(
        "frozen-reconcile",
        help="冻结决策对账批跑(measure-01:结算+收盘回填,已结算缺收盘行独立补齐,幂等)",
    )
    sub.add_parser(
        "funnel-report",
        help="机会漏斗日拍(票04:五级分母+轨一估值;判活=漏斗表最新业务日)",
    ).add_argument(
        "--as-of",
        type=str,
        default=None,
        help="冻结时点 ISO(缺省=当前时刻;重放历史日拍用)",
    )
    sub.add_parser(
        "freshness-shadow-day",
        help="新鲜度双口径影子日拍(票08:五时钟+规则/观测双结论,仅记录不改规则)",
    ).add_argument(
        "--date",
        type=str,
        default=None,
        help="业务日 北京日期(缺省=今天;仅当日/未来日——过去日无冻结回放会销毁原始快照)",
    )
    sub.add_parser(
        "gold-build",
        help="gold特征面全量重算(票03起=market_quote保鲜+gold重建同一实现,幂等)",
    )
    sub.add_parser(
        "srct-silver",
        help="源T silver 重物化+DuckDB 只读桥(票56切片14/16;fixture+xg 幂等重建)",
    )
    sub.add_parser(
        "srct-odds",
        help="源T 赔率事件重物化(票56切片15;bookmaker 字典+odds_change_event 幂等重建)",
    )
    sub.add_parser(
        "srct-market",
        help="重物化 market_quote/detail/analysis silver + duckdb 桥（票 64）",
    )
    sub.add_parser(
        "srct-gate",
        help="Phase1 五门报告(票56切片17;三对账+转换完整性+管线健康,JSON/MD 两视图)",
    )
    delay_audit = sub.add_parser(
        "delay-audit",
        help="延迟生存率Stage1到达率审计(票07;四层分母+主指标Wilson CI,预注册口径)",
    )
    delay_audit.add_argument(
        "--date-from", required=True, help="回看窗起始日 YYYY-MM-DD(含)"
    )
    delay_audit.add_argument(
        "--date-to", required=True, help="回看窗截止日 YYYY-MM-DD(含)"
    )
    delay_audit.add_argument(
        "--skip-close",
        action="store_true",
        help="跳过收盘面(无语料环境口径冒烟;主指标收盘列如实置空)",
    )
    price_edge = sub.add_parser(
        "price-edge-audit",
        help="价格存活检查点读数(票09;确认价EV中位数+簇bootstrap,预注册口径)",
    )
    price_edge.add_argument(
        "--date-from",
        required=True,
        help="协议窗起始日 YYYY-MM-DD(含;累计销售日门槛自该日起算)",
    )
    price_edge.add_argument(
        "--date-to", required=True, help="读数截止日 YYYY-MM-DD(含)"
    )
    price_edge.add_argument(
        "--no-corpus",
        action="store_true",
        help="不开语料duckdb(参考仅legacy欧赔行可算;冒烟用,生产勿用)",
    )
    rag_embed_parser = sub.add_parser(
        "rag-embed",
        help=(
            "RAG增量嵌入(票06;幂等content_hash跳过;"
            "--namespace lessons|guardian [--limit N];首跑拉模型约0.22GB)"
        ),
    )
    rag_embed_parser.add_argument(
        "--namespace",
        required=True,
        choices=("lessons", "guardian"),
        help="索引命名空间（教训条目/卫报语料）",
    )
    rag_embed_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最多嵌 N 条新条目(过滤已嵌后计数,同参重跑可推进分段回填;缺省全量)",
    )
    sub.add_parser(
        "morning-check",
        help="09:05晨检代码化(ingest-orchestration票01/05;21项只读红绿面,任何fail退出1)",
    )
    news_pit_parser = sub.add_parser(
        "news-pit-beat",
        help="新闻/伤停 PIT 前向夜拍手动触发(票17;定拍面挂 daily-wrap 23:30)",
    )
    news_pit_parser.add_argument(
        "--request-cap",
        type=int,
        default=None,
        help=f"单拍请求上限(默认 {news_pit.BEAT_REQUEST_CAP})",
    )
    sub.add_parser(
        "guardian-status",
        help="卫报双轨报告(autoresearch票17;回填存量清单+增量通道夜拍,只读)",
    )
    edge_parser = sub.add_parser(
        "edge-scan",
        help="探索轨扫描器阵列(autoresearch票01/04;S1校准/S4陈盘/S5滚存,发现台账+周报)",
    )
    edge_parser.add_argument(
        "--scanner",
        choices=("all", *edge_scan.SCANNERS),
        default="all",
        help="单扫描器重跑(默认全部在册)",
    )
    edge_parser.add_argument(
        "--window-end",
        help="观察日 YYYY-MM-DD(默认北京业务日昨日;回填用)",
    )
    edge_parser.add_argument(
        "--face",
        choices=("runtime", "decade"),
        default="runtime",
        help="s1 数据面:运行面滚动窗/研究库十年回放(口径验证)",
    )
    edge_parser.add_argument(
        "--digest",
        action="store_true",
        help="出周报 digest(窗口统计+新台账行+晋升建议,两视图落语料树)",
    )
    edge_parser.add_argument(
        "--mark",
        help="台账状态人裁: scanner,segment,status(promoted|dead)",
    )
    edge_parser.add_argument(
        "--no-corpus",
        action="store_true",
        help="不开语料duckdb(S4 drift记零;冒烟用,生产勿用)",
    )
    repair_parser = sub.add_parser(
        "repair-sale-status",
        help="历史销售态语义修正(票09;旧映射在售误记停售,dry-run默认)",
    )
    repair_parser.add_argument(
        "--apply",
        action="store_true",
        help="实改(前置v32迁移;UPDATE防线触发器摘除后按原文重建)",
    )
    jc_audit_parser = sub.add_parser(
        "jc-audit",
        help="JC audit 面(票67;TTG 按年密度+存档最早年限+官方vs cid1129 对账)",
    )
    jc_audit_parser.add_argument(
        "--date", action="append", help="对账抽样日 YYYY-MM-DD(可多次)"
    )
    jc_audit_parser.add_argument(
        "--probe-archive", action="store_true", help="二分探存档最早年限(~10 请求)"
    )
    jc_backfill_parser = sub.add_parser(
        "jc-backfill",
        help="JC 历史回填批(票67;uniform 按日反查→逐场,done 日零成本跳过)",
    )
    jc_backfill_parser.add_argument(
        "--date-to", required=True, help="起始日(新) YYYY-MM-DD"
    )
    jc_backfill_parser.add_argument(
        "--date-from", required=True, help="截止日(旧) YYYY-MM-DD"
    )
    jc_backfill_parser.add_argument(
        "--request-cap", type=int, default=srct.NIGHT_REQUEST_CAP
    )
    jc_backfill_parser.add_argument(
        "--day-cap", type=int, default=None, help="单日 mid 上限(抽样冒烟)"
    )
    sub.add_parser(
        "jc-silver",
        help="jc_sp_change_event silver 重物化(票72;与书商层事件流同构,幂等)",
    )
    jc_collect = sub.add_parser(
        "jc-collect",
        help="竞彩官方SP历史采集(票70;jc provider 独立落,HAD/HHAD/TTG+CRS/HAFU 留档)",
    )
    jc_collect.add_argument(
        "--match-id", action="append", required=True, help="uniform matchId(可多次)"
    )
    archive = sub.add_parser(
        "archive-538",
        help="538 终版档案一次性入库(票56切片16;原档进raw+CorpusScope silver 小表)",
    )
    archive.add_argument(
        "--csv",
        type=Path,
        default=archive538.DEFAULT_CSV_PATH,
        help="原档 CSV 路径(默认 research/21 留档)",
    )
    sub.add_parser(
        "seed-demo", help="写入演示/E2E 种子(只允许隔离库, 拒绝写主库伪造实采)"
    )
    sub.add_parser(
        "e2e-corpus",
        help="生成 corpus 主链 e2e 夹具(四场制式, 只允许隔离库; 票 06)",
    )
    version_register_parser = sub.add_parser(
        "version-register",
        help="策略版本注册(vision票01;三元组幂等落账,人CLI)",
    )
    version_register_parser.add_argument(
        "--estimator", required=True, help="估计器版本标识(dc 工件身份)"
    )
    version_register_parser.add_argument(
        "--calibration", required=True, help="校准口径标识"
    )
    version_register_parser.add_argument(
        "--params", required=True, help="参数指纹(auto_strategy 参数内容寻址)"
    )
    version_register_parser.add_argument(
        "--label", required=True, help="人读标签(如 auto-v2)"
    )
    version_promote_parser = sub.add_parser(
        "version-promote",
        help="策略版本晋升/转移(vision票01;恒人审写transitions,非法转移拒)",
    )
    version_promote_parser.add_argument("--id", type=int, required=True)
    version_promote_parser.add_argument(
        "--to",
        required=True,
        choices=tuple(state.value for state in versioning.StrategyState),
        help="目标状态",
    )
    version_promote_parser.add_argument(
        "--reason", required=True, help="转移理由(落 transitions 审计)"
    )
    quality_gate_parser = sub.add_parser(
        "quality-gate-evaluate",
        help="challenger质量门评估(vision票11;回放窗校准对比,落账+报告)",
    )
    quality_gate_parser.add_argument(
        "--challenger-version", type=int, required=True, help="challenger 策略版本 id"
    )
    quality_gate_parser.add_argument(
        "--champion-run", type=int, required=True, help="champion 回放 run id(研究库)"
    )
    quality_gate_parser.add_argument(
        "--challenger-run",
        type=int,
        required=True,
        help="challenger 回放 run id(研究库)",
    )
    quality_gate_parser.add_argument(
        "--champion-version",
        type=int,
        default=None,
        help="champion 策略版本 id(缺省=当前 live 态版本)",
    )
    hypothesis_mark_parser = sub.add_parser(
        "hypothesis-mark",
        help="假设注册表状态翻转(vision票02;终态恒人审,agent不自动定生死)",
    )
    hypothesis_mark_parser.add_argument(
        "--h-id", required=True, help="假设 id（如 H1）"
    )
    hypothesis_mark_parser.add_argument(
        "--to",
        required=True,
        choices=tuple(state.value for state in hypothesis_registry.HypothesisState),
        help="目标状态（supported|refuted=终态，仅人审）",
    )
    hypothesis_mark_parser.add_argument(
        "--reason", required=True, help="翻转理由(落账审计)"
    )
    contraction_set_parser = sub.add_parser(
        "contraction-set",
        help="收缩级别切换(vision票08;恒人CLI写transitions,相邻一级)",
    )
    contraction_set_parser.add_argument(
        "--level",
        required=True,
        choices=tuple(level.value for level in contraction.ContractionLevel),
        help="目标收缩级别(L0 全速…L4 全停只留漂移监测)",
    )
    contraction_set_parser.add_argument(
        "--reason", required=True, help="切换理由(落账审计)"
    )
    sub.add_parser(
        "contraction-read",
        help="收缩双钟读数+落账(vision票08;首读/月读口,每次读每钟一行观测流水)",
    )
    proposal_register_parser = sub.add_parser(
        "proposal-register",
        help="周批提案人审注册(vision票03;缺省=checklist清单,--proposal-id/--h-id=freeze进注册表)",
    )
    proposal_register_parser.add_argument(
        "proposal_file", help="提案文件路径（周拍 JSON，语料树 reports/proposals/）"
    )
    proposal_register_parser.add_argument(
        "--proposal-id", help="要注册的提案 id（如 P-20261116-01）"
    )
    proposal_register_parser.add_argument(
        "--h-id", help="注册用的假设 id（新 h_id，改预测=新 h_id）"
    )
    proposal_register_parser.add_argument(
        "--family", help="假设族（缺省=h_id；家族复试继承 attempt 计数）"
    )
    # 注册数据集子命令（票 02 起：guardian-sync / pool-sync / srct-night）
    datasets.add_cli_subparsers(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    args = build_parser().parse_args(argv)
    handlers: dict[str, Callable[[], None]] = {
        "migrate": _cmd_migrate,
        "ingest-jingcai": _cmd_ingest_jingcai,
        "reprocess-sporttery": _cmd_reprocess_sporttery,
        "ingest-odds": _cmd_ingest_odds,
        "propline-capture": _cmd_propline_capture,
        "ingest-hist": _cmd_ingest_hist,
        "settle": _cmd_settle,
        "funnel-report": lambda: _cmd_funnel_report(args),
        "freshness-shadow-day": lambda: _cmd_freshness_shadow(args),
        "multiline-bounds-report": _cmd_multiline_bounds,
        "frozen-reconcile": _cmd_frozen_reconcile,
        "guardian-news-funnel": lambda: _cmd_guardian_news_funnel(args),
        "sync-draw-results": _cmd_sync_draw_results,
        "official-reconcile": _cmd_official_reconcile,
        "audit-ledger": _cmd_audit_ledger,
        "train-models": lambda: _cmd_train_models(args),
        "forecast": lambda: _cmd_forecast(args),
        "align-report": _cmd_align_report,
        "accuracy-score": _cmd_accuracy_score,
        "set-alias": lambda: _cmd_set_alias(args),
        "backtest": lambda: _cmd_backtest(args),
        "research-backfill": _cmd_research_backfill,
        "baseline-compare": _cmd_baseline_compare,
        "unlock-report": _cmd_unlock_report,
        "market-lambda-report": _cmd_market_lambda_report,
        "haircut-decade": _cmd_haircut_decade,
        "guardrail-report": lambda: _cmd_guardrail_report(args),
        "decade-skill-report": lambda: _cmd_decade_skill(args),
        "clv-decade-report": lambda: _cmd_clv_decade(args),
        "signal-attribution-report": lambda: _cmd_signal_attribution(args),
        "model-layers-report": lambda: _cmd_model_layers(args),
        "guardian-news-pilot": lambda: _cmd_guardian_news_pilot(args),
        "news-signal-report": lambda: _cmd_news_signal(args),
        "bucket-reaudit": lambda: _cmd_bucket_reaudit(args),
        "calibrate-haircut": _cmd_calibrate_haircut,
        "clv-reconcile": _cmd_clv_reconcile,
        "pool-backfill": lambda: _cmd_pool_backfill(args),
        "xg-compare": lambda: _cmd_xg_compare(args),
        "pbo-feasibility": lambda: _cmd_pbo_feasibility(args),
        "corpus-report": lambda: _cmd_corpus_report(args),
        "pool-replay-report": lambda: _cmd_pool_replay_report(args),
        "drift-replay-report": lambda: _cmd_drift_replay_report(args),
        "srct-collect": lambda: _cmd_srct_collect(args),
        "srct-results": lambda: _cmd_srct_results(args),
        "elo-build": lambda: _cmd_elo_build(args),
        "gold-build": lambda: _cmd_gold_build(args),
        "srct-silver": lambda: _cmd_srct_silver(args),
        "srct-odds": lambda: _cmd_srct_odds(args),
        "srct-market": lambda: _cmd_srct_market(args),
        "srct-gate": lambda: _cmd_srct_gate(args),
        "delay-audit": lambda: _cmd_delay_audit(args),
        "price-edge-audit": lambda: _cmd_price_edge_audit(args),
        "edge-scan": lambda: _cmd_edge_scan(args),
        "morning-check": _cmd_morning_check,
        "news-pit-beat": lambda: _cmd_news_pit_beat(args),
        "guardian-status": _cmd_guardian_status,
        "rag-embed": lambda: _cmd_rag_embed(args),
        "repair-sale-status": lambda: _cmd_repair_sale_status(args),
        "jc-collect": lambda: _cmd_jc_collect(args),
        "jc-silver": lambda: _cmd_jc_silver(args),
        "jc-backfill": lambda: _cmd_jc_backfill(args),
        "jc-audit": lambda: _cmd_jc_audit(args),
        "mapping-sync": lambda: _cmd_mapping_sync(args),
        "mapping-audit": lambda: _cmd_mapping_audit(args),
        "clubelo-elo": lambda: _cmd_clubelo_elo(args),
        "tm-coach-report": _cmd_tm_coach_report,
        "tm-coach-crosscheck": lambda: _cmd_tm_coach_crosscheck(args),
        "six-axes-derive": _cmd_six_axes_derive,
        "six-axes-report": _cmd_six_axes_report,
        "archive-538": lambda: _cmd_archive_538(args),
        "seed-demo": _cmd_seed_demo,
        "e2e-corpus": _cmd_e2e_corpus,
        "version-register": lambda: _cmd_version_register(args),
        "version-promote": lambda: _cmd_version_promote(args),
        "quality-gate-evaluate": lambda: _cmd_quality_gate_evaluate(args),
        "hypothesis-mark": lambda: _cmd_hypothesis_mark(args),
        "contraction-set": lambda: _cmd_contraction_set(args),
        "contraction-read": _cmd_contraction_read,
        "proposal-register": lambda: _cmd_proposal_register(args),
    }
    # 注册数据集命令（票 02 起）：dispatch 与手写面向同一 tasks 函数；
    # cli=False 记录不派生（official-reconcile 手写 handler 带 pending_manual 走查）
    for spec in datasets.cli_specs():
        handlers[spec.command] = partial(datasets.run_cli, spec, args)
    handlers[args.command]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
