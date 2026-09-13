"""
goalx 命令行入口：迁移与一次性采集/训练/预测任务。

日常定时采集走 Prefect deployments；本 CLI 覆盖初始化与手工补跑：

    uv run python -m goalx_backend.cli migrate
    uv run python -m goalx_backend.cli ingest-jingcai
    uv run python -m goalx_backend.cli ingest-odds
    uv run python -m goalx_backend.cli ingest-hist
    uv run python -m goalx_backend.cli settle
    uv run python -m goalx_backend.cli train-models [--bootstrap N]
    uv run python -m goalx_backend.cli forecast [--date YYYY-MM-DD]
    uv run python -m goalx_backend.cli align-report
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

from loguru import logger

from goalx_backend import backtest as bt
from goalx_backend import clv as clv_mod
from goalx_backend import evaluation as ev
from goalx_backend import haircut as hc
from goalx_backend import team_align
from goalx_backend.config import get_settings
from goalx_backend.db import connect, migrate
from goalx_backend.dc_model import TIER1_COMPETITIONS, train_competition
from goalx_backend.forecast import generate_forecasts
from goalx_backend.ingest import fdhist, oddsapi, sporttery
from goalx_backend.ingest.oddsapi import polite_client
from goalx_backend.ledger_audit import audit_ledger
from goalx_backend.services import run_settlement


def _cmd_migrate() -> None:
    """执行 schema 迁移。"""
    conn = connect()
    version = migrate(conn)
    conn.close()
    logger.info("schema at version {}", version)


def _cmd_ingest_jingcai() -> None:
    """手动拉一次竞彩快照。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with polite_client() as client:
            payload = sporttery.fetch_calculator_payload(settings, client)
        stats = sporttery.store_matches(conn, sporttery.parse_matches(payload))
        logger.info(
            "matches={} snapshots={} dup={}",
            stats.matches,
            stats.snapshots,
            stats.duplicate_snapshots,
        )
    finally:
        conn.close()


def _cmd_ingest_odds() -> None:
    """手动拉一次欧赔(走 credit 护栏)。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with polite_client() as client:
            stats = oddsapi.fetch_and_store_odds(conn, settings, client)
        logger.info(
            "events={} snapshots={} credits={} unmatched={}",
            stats.events,
            stats.snapshots,
            stats.credits_used,
            stats.unmatched,
        )
    finally:
        conn.close()


def _cmd_ingest_hist() -> None:
    """导入历史底座(五大三季)。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with polite_client() as client:
            stats = fdhist.import_history(conn, settings, client)
        logger.info(
            "rows={} written={} skipped={}", stats.rows, stats.written, stats.skipped
        )
    finally:
        conn.close()


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
    conn = connect()
    try:
        migrate(conn)
        logger.info("settlement: {}", run_settlement(conn))
    finally:
        conn.close()


def _cmd_train_models(args: argparse.Namespace) -> None:
    """训练五大 DC 模型（基准 + bootstrap 工件落盘）。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        for competition in args.competitions:
            run = train_competition(
                conn,
                competition,
                models_dir=settings.models_dir,
                half_life_days=args.half_life,
                n_boot=args.bootstrap,
                seed=settings.bootstrap_seed,
            )
            logger.info(
                "trained {}: matches={} window={}~{} boots={}",
                competition,
                run.base.n_matches,
                run.base.train_window_start,
                run.base.train_window_end,
                len(run.bootstrap),
            )
    finally:
        conn.close()


def _cmd_forecast(args: argparse.Namespace) -> None:
    """对一个业务日生成 ML Forecast（幂等）。"""
    settings = get_settings()
    cst = timezone(timedelta(hours=8))
    business_date = args.date or datetime.now(UTC).astimezone(cst).strftime("%Y-%m-%d")
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        stats = generate_forecasts(
            conn,
            business_date=business_date,
            models_dir=str(settings.models_dir),
        )
        logger.info(
            "forecasts for {}: generated={} dup={} skipped={}",
            business_date,
            stats.generated,
            stats.duplicates,
            stats.skipped,
        )
    finally:
        conn.close()


def _cmd_backtest(args: argparse.Namespace) -> None:
    """跑一次回测（walk-forward）+ 指标分层汇总。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        haircut, source, n = hc.calibrated_haircut(conn)
        if args.haircut != "auto":
            haircut, source = args.haircut, "cli"
        logger.info("haircut={} (source={}, n={})", haircut, source, n)
        params = bt.BacktestParams(
            competitions=tuple(args.competitions),
            seasons=tuple(args.seasons),
            haircut=haircut,
            parlay2=not args.no_parlay,
            min_train_matches=args.min_train,
        )
        result = bt.run_backtest(conn, params, label=args.label)
        ev.compute_run_metrics(conn, result.run_id)
        logger.info(
            "run={} predictions={} bets={} staked={:.2f} profit={:.2f} roi={:.2%}",
            result.run_id,
            result.predictions,
            result.bets,
            result.staked,
            result.profit,
            result.roi,
        )
    finally:
        conn.close()


def _cmd_calibrate_haircut() -> None:
    """竞彩 vs 欧共识配对样本 → haircut 分布校准。"""
    conn = connect()
    try:
        migrate(conn)
        for row in hc.calibrate_haircuts(conn):
            logger.info("{}", row)
    finally:
        conn.close()


def _cmd_closing_snapshot() -> None:
    """收盘窗口尽力快照（kickoff 前 35 分钟内的场次）。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with polite_client() as client:
            stats = oddsapi.fetch_closing_window(conn, settings, client)
        logger.info(
            "closing: events={} snapshots={} credits={}",
            stats.events,
            stats.snapshots,
            stats.credits_used,
        )
    finally:
        conn.close()


def _cmd_clv_reconcile() -> None:
    """已结算注单 CLV 对账 + 报表。"""
    conn = connect()
    try:
        migrate(conn)
        stats = clv_mod.reconcile_clv(conn)
        logger.info("recorded={} skipped={}", stats.recorded, len(stats.skipped))
        logger.info("report: {}", clv_mod.clv_report(conn))
    finally:
        conn.close()


def _cmd_set_alias(args: argparse.Namespace) -> None:
    """人工覆盖：给 canonical 球队追加 manual 别名（即时生效，票 25）。"""
    conn = connect()
    try:
        migrate(conn)
        row = conn.execute(
            "SELECT id FROM teams WHERE canonical_name = ?", (args.team,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"球队不存在: {args.team}")
        conn.execute(
            """
            INSERT OR IGNORE INTO team_aliases (team_id, source, alias)
            VALUES (?, 'manual', ?)
            """,
            (int(row["id"]), args.alias),
        )
        conn.commit()
        logger.info("alias set: {} -> {}", args.alias, args.team)
    finally:
        conn.close()


def _cmd_align_report() -> None:
    """五大 hist 队名对当前别名的覆盖率报告。"""
    conn = connect()
    try:
        migrate(conn)
        report = team_align.build_alignment_report(conn)
        for competition, item in sorted(report.per_competition.items()):
            logger.info(
                "{}: {}/{} matched", competition, item["matched"], item["total"]
            )
        logger.info("overall coverage: {:.1%}", report.coverage)
        if report.unmatched:
            logger.warning("unmatched: {}", report.unmatched)
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    """CLI 参数。"""
    parser = argparse.ArgumentParser(prog="goalx", description="goalx 运维命令")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="执行 schema 迁移")
    sub.add_parser("ingest-jingcai", help="手动拉一次竞彩快照")
    sub.add_parser("ingest-odds", help="手动拉一次欧赔(走 credit 护栏)")
    sub.add_parser("ingest-hist", help="导入五大三季历史底座")
    sub.add_parser("audit-ledger", help="只读核查旧账与更正历史")
    sub.add_parser("settle", help="手动结算批跑")
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
    alias = sub.add_parser("set-alias", help="人工覆盖球队别名(票 25)")
    alias.add_argument("team", help="canonical 球队名(中文)")
    alias.add_argument("alias", help="外部别名(如 fd 英文名)")
    backtest = sub.add_parser("backtest", help="跑一次 walk-forward 回测(票 28/29)")
    backtest.add_argument("--label", default="manual", help="run 标签")
    backtest.add_argument(
        "--haircut",
        default="auto",
        help="auto=校准值(不足回落默认) 或直接给数值(如 0.10)",
    )
    backtest.add_argument(
        "--competitions",
        nargs="*",
        default=list(TIER1_COMPETITIONS),
        help="fd 联赛代码",
    )
    backtest.add_argument(
        "--seasons",
        nargs="*",
        default=["2324", "2425", "2526"],
        help="回测赛季",
    )
    backtest.add_argument("--no-parlay", action="store_true", help="关闭 2串1 模拟")
    backtest.add_argument(
        "--min-train",
        type=int,
        default=100,
        help="每周最小训练样本",
    )
    sub.add_parser("calibrate-haircut", help="haircut 配对样本校准(票 30)")
    sub.add_parser("closing-snapshot", help="收盘窗口尽力快照(票 32)")
    sub.add_parser("clv-reconcile", help="已结算注单 CLV 对账+报表(票 32)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    args = build_parser().parse_args(argv)
    handlers: dict[str, Callable[[], None]] = {
        "migrate": _cmd_migrate,
        "ingest-jingcai": _cmd_ingest_jingcai,
        "ingest-odds": _cmd_ingest_odds,
        "ingest-hist": _cmd_ingest_hist,
        "settle": _cmd_settle,
        "audit-ledger": _cmd_audit_ledger,
        "train-models": lambda: _cmd_train_models(args),
        "forecast": lambda: _cmd_forecast(args),
        "align-report": _cmd_align_report,
        "set-alias": lambda: _cmd_set_alias(args),
        "backtest": lambda: _cmd_backtest(args),
        "calibrate-haircut": _cmd_calibrate_haircut,
        "closing-snapshot": _cmd_closing_snapshot,
        "clv-reconcile": _cmd_clv_reconcile,
    }
    handlers[args.command]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
