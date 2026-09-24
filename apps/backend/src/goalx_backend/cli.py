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
    uv run python -m goalx_backend.cli pool-sync
    uv run python -m goalx_backend.cli understat-sync [--seasons 2021 2022 ...]
    uv run python -m goalx_backend.cli xg-compare --seasons 2022 2023 2024 2025 2026
    uv run python -m goalx_backend.cli srct-collect --date YYYY-MM-DD
    uv run python -m goalx_backend.cli srct-night [--no-window] [--request-cap N]
    uv run python -m goalx_backend.cli srct-night --list
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime

import httpx
from loguru import logger

from goalx_backend import tasks
from goalx_backend.betting.ledger_audit import audit_ledger
from goalx_backend.config import Settings, get_settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import reconcile
from goalx_backend.data import results as rs_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import (
    caiguo,
    fdhist,
    openfootball,
    sporttery,
    srct,
    srct_night,
    uniform,
)
from goalx_backend.db import connect, migrate
from goalx_backend.evaluation import backtest as bt
from goalx_backend.evaluation import baseline
from goalx_backend.evaluation import clv as clv_mod
from goalx_backend.evaluation import haircut as hc
from goalx_backend.evaluation import metrics as ev
from goalx_backend.evaluation.corpus import completeness_report
from goalx_backend.evaluation.drift_replay import drift_replay_report
from goalx_backend.evaluation.pool_replay import pool_replay_report
from goalx_backend.evaluation.xg_compare import run_xg_comparison
from goalx_backend.modelling import team_align
from goalx_backend.modelling.dc_model import TIER1_COMPETITIONS
from goalx_backend.tasks import task_conn


def _cmd_migrate() -> None:
    """执行 schema 迁移。"""
    conn = connect()
    version = migrate(conn)
    conn.close()
    logger.info("schema at version {}", version)


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


def _cmd_backtest(args: argparse.Namespace) -> None:
    """跑一次回测（walk-forward）+ 指标分层汇总。"""
    with task_conn() as conn:
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
            fair_source=args.fair_source,
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


def _cmd_baseline_compare() -> None:
    """基准分期质检报告 + psc/avgc 对照新 run(旧 run 保留,票 34)。"""
    with task_conn() as conn:
        sys.stdout.write(
            json.dumps(
                baseline.baseline_quality_report(conn), ensure_ascii=False, indent=2
            )
            + "\n"
        )
        haircut, _, _ = hc.calibrated_haircut(conn)
        results = baseline.run_baseline_comparison(
            conn,
            bt.BacktestParams(haircut=haircut),
            base_label=f"baseline-{datetime.now(tz=UTC).strftime('%Y%m%d')}",
        )
        for source, summary in results.items():
            ev.compute_run_metrics(conn, int(summary["run_id"]))
            logger.info("baseline {} run={}", source, summary)


def _cmd_calibrate_haircut() -> None:
    """竞彩 vs 欧共识配对样本 → haircut 分布校准。"""
    with task_conn() as conn:
        for row in hc.calibrate_haircuts(conn):
            logger.info("{}", row)


def _cmd_closing_snapshot() -> None:
    """收盘窗口尽力快照（kickoff 前 35 分钟内的场次）。"""
    stats = tasks.eu_odds_closing()
    logger.info(
        "closing: events={} snapshots={} credits={}",
        stats.events,
        stats.snapshots,
        stats.credits_used,
    )


def _cmd_clv_reconcile() -> None:
    """已结算注单 CLV 对账 + 报表。"""
    with task_conn() as conn:
        stats = clv_mod.reconcile_clv(conn)
        logger.info("recorded={} skipped={}", stats.recorded, len(stats.skipped))
        logger.info("report: {}", clv_mod.clv_report(conn))


def _cmd_understat_sync(args: argparse.Namespace) -> None:
    """Understat xG 同步（票 45）：默认当前季；--seasons 2021 2022 … 回填历史。"""
    seasons = tuple(args.seasons) if args.seasons else None
    leagues = tuple(args.leagues) or None
    logger.info("understat sync: {}", tasks.understat_sync(seasons, leagues=leagues))


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
            with httpx.Client() as owned:
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
        "failed": stats.failed,
        "parse_version": srct.PARSE_VERSION,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _parse_success_rate(stats: srct.SrctCollectStats) -> float | None:
    """解析成功率（无解析样本返回 None——空日不折算成 1.0）。"""
    denom = stats.parsed_ok + len(stats.parse_failed)
    return round(stats.parsed_ok / denom, 4) if denom else None


def _cmd_srct_night(
    args: argparse.Namespace,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    now_fn: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] | None = None,
    seasons: tuple[srct_night.SeasonWindow, ...] | None = None,
) -> None:
    """
    源T夜班（票 55 切片 13）：窗口内按预算推进 Phase1；--list 只读查摘要。

    settings/client/now_fn/sleeper/seasons 注入口只服务测试接缝；缺省走
    真实配置、真实连接、本机墙钟、真实防封间隔与 Phase1 全表（窗口外
    直接 window_closed，白天冒烟走 --no-window）。
    """
    resolved = settings if settings is not None else get_settings()
    window = None if args.no_window else srct_night.NIGHT_WINDOW
    store = CorpusStore(resolved.corpus_root)
    try:
        if args.list:
            payload = {"nights": store.night_summaries(limit=args.limit)}
        else:
            with ExitStack() as stack:
                run_client = (
                    client
                    if client is not None
                    else stack.enter_context(httpx.Client())
                )
                summary = srct_night.run_night(
                    store,
                    resolved,
                    run_client,
                    now_fn=now_fn,
                    window=window,
                    request_cap=args.request_cap,
                    sleeper=sleeper,
                    seasons=seasons,
                )
            payload = asdict(summary)
    finally:
        store.close()
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


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
            with httpx.Client() as client:
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


def _cmd_pool_sync() -> None:
    """彩池同步：源B 期次/对阵/人气分布（幂等，票 43）。"""
    stats = tasks.pool_snapshot()
    logger.info(
        "pool sync: periods={} matches={} share_rows={} missing_shares={}",
        stats.period_nos,
        stats.matches,
        stats.share_rows,
        stats.missing_shares,
    )


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
        logger.info("overall coverage: {:.1%}", report.coverage)
        if report.unmatched:
            logger.warning("unmatched: {}", report.unmatched)


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
    backtest.add_argument(
        "--fair-source",
        choices=("auto", "psc", "avgc"),
        default="auto",
        help="公允基准来源: auto=PSC 优先 AvgC 兜底; psc/avgc=只用该源(票 34)",
    )
    sub.add_parser("baseline-compare", help="基准分期质检+psc/avgc 对照新 run(票 34)")
    sub.add_parser("calibrate-haircut", help="haircut 配对样本校准(票 30)")
    sub.add_parser("closing-snapshot", help="收盘窗口尽力快照(票 32)")
    sub.add_parser("clv-reconcile", help="已结算注单 CLV 对账+报表(票 32)")
    sub.add_parser("pool-sync", help="手动拉一次彩池期次/对阵/人气分布(票 43)")
    understat = sub.add_parser(
        "understat-sync", help="Understat xG 特征同步(票 45;默认当前季)"
    )
    understat.add_argument(
        "--seasons", nargs="*", help="回填赛季起始年(如 2021 2022 …,默认当前季)"
    )
    understat.add_argument(
        "--leagues",
        nargs="*",
        default=[],
        help="understat slug(默认五大;俄超按需传 rfpl)",
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
    corpus = sub.add_parser(
        "corpus-report", help="十年语料报表(票 46;完整性+可选交叉验证)"
    )
    corpus.add_argument("--seasons", nargs="*", help="fd 季键(默认 1617..2627)")
    corpus.add_argument(
        "--cross-check",
        action="store_true",
        help="附 openfootball 比分对 fdhist 交叉验证(拉重叠联赛赛季文件)",
    )
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
    srct_night_parser = sub.add_parser(
        "srct-night",
        help="源T夜班推进Phase1回填(票55切片13;预算/熔断/断点续传,摘要落库)",
    )
    srct_night_parser.add_argument(
        "--request-cap",
        type=int,
        default=srct.NIGHT_REQUEST_CAP,
        help=f"当夜请求预算上限(默认 {srct.NIGHT_REQUEST_CAP})",
    )
    srct_night_parser.add_argument(
        "--no-window",
        action="store_true",
        help="跳过 01:00-08:00 窗口判断(白天冒烟/手工回补用)",
    )
    srct_night_parser.add_argument(
        "--list", action="store_true", help="只读查最近夜班摘要(不发请求)"
    )
    srct_night_parser.add_argument(
        "--limit", type=int, default=20, help="--list 行数(默认 20)"
    )
    sub.add_parser(
        "seed-demo", help="写入演示/E2E 种子(只允许隔离库, 拒绝写主库伪造实采)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    args = build_parser().parse_args(argv)
    handlers: dict[str, Callable[[], None]] = {
        "migrate": _cmd_migrate,
        "ingest-jingcai": _cmd_ingest_jingcai,
        "reprocess-sporttery": _cmd_reprocess_sporttery,
        "ingest-odds": _cmd_ingest_odds,
        "ingest-hist": _cmd_ingest_hist,
        "settle": _cmd_settle,
        "sync-draw-results": _cmd_sync_draw_results,
        "official-reconcile": _cmd_official_reconcile,
        "audit-ledger": _cmd_audit_ledger,
        "train-models": lambda: _cmd_train_models(args),
        "forecast": lambda: _cmd_forecast(args),
        "align-report": _cmd_align_report,
        "set-alias": lambda: _cmd_set_alias(args),
        "backtest": lambda: _cmd_backtest(args),
        "baseline-compare": _cmd_baseline_compare,
        "calibrate-haircut": _cmd_calibrate_haircut,
        "closing-snapshot": _cmd_closing_snapshot,
        "clv-reconcile": _cmd_clv_reconcile,
        "pool-sync": _cmd_pool_sync,
        "understat-sync": lambda: _cmd_understat_sync(args),
        "xg-compare": lambda: _cmd_xg_compare(args),
        "corpus-report": lambda: _cmd_corpus_report(args),
        "pool-replay-report": lambda: _cmd_pool_replay_report(args),
        "drift-replay-report": lambda: _cmd_drift_replay_report(args),
        "srct-collect": lambda: _cmd_srct_collect(args),
        "srct-night": lambda: _cmd_srct_night(args),
        "seed-demo": _cmd_seed_demo,
    }
    handlers[args.command]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
