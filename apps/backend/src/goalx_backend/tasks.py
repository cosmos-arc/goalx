"""
共享任务体：cli 与 Prefect flows 调用同一实现（ADR-0005）。

背景：cli ingest-jingcai 曾绕过 capture_jingcai 静默丢失证据存证——同一
任务两份实现必然漂移（2026-09 架构评审候选 4）。Prefect flow、cli 命令
与 api 触发端点是同一任务的三个 adapter；本模块是任务实现的唯一住所。

不含调度的运维命令（backtest/校准/报表等）只有 cli 一个 adapter，仍直接
使用 task_conn 壳。
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import sqlite3
import time
import zlib
from collections.abc import Callable, Generator
from concurrent.futures.process import BrokenProcessPool
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import cast

import duckdb
import httpx
from loguru import logger

from goalx_backend.betting import frozen as frozen_mod
from goalx_backend.betting import probes
from goalx_backend.betting.funnel import (
    TRACK2_OFFSETS,
    Track2Capture,
    funnel_liveness,
    has_track2_snapshot,
    record_funnel_day,
    record_track2_snapshot,
    rederive_purchasable,
)
from goalx_backend.betting.settle import run_settlement
from goalx_backend.config import Settings, get_settings
from goalx_backend.data import (
    corpus_duckdb,
    gold,
    mapping,
    quote_evidence,
    reconcile,
    today,
)
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import (
    clubelo,
    fdhist,
    guardian,
    news_pit,
    oddsapi,
    openfootball,
    propline,
    sporttery,
    srcb,
    srct,
    srct_asof,
    srct_market,
    srct_night,
    srct_prematch,
    srct_remat,
    srct_results,
    srct_shift,
    understat,
    uniform,
    zucai_official,
)
from goalx_backend.data.ingest.shell import browser_ssl_context, polite_client
from goalx_backend.data.quote_evidence import (
    bind_quote_reads,
)
from goalx_backend.db import atomic, connect, migrate, utc_now_iso
from goalx_backend.evaluation import accuracy, anchor_sentinel, edge_scan
from goalx_backend.evaluation import clv as clv_mod
from goalx_backend.evaluation.baseline_freeze import (
    anchor_label_at,
    effective_cutoff,
    freeze_anchor_baselines,
)
from goalx_backend.llm.collect import collect_pool_intel
from goalx_backend.llm.collect import stats_dict as intel_stats_dict
from goalx_backend.llm.fusion import fusion_stats_dict, fusion_sweep
from goalx_backend.llm.gate import gate_stats_dict, gate_sweep
from goalx_backend.llm.m3_report import m3_protocol_report
from goalx_backend.llm.okooo_formation import collect_injury_intel, injury_stats_dict
from goalx_backend.llm.review import enqueue_post_settle
from goalx_backend.llm.scout import scout_stats_dict, scout_sweep
from goalx_backend.llm.sina_intel import collect_sina_injury_intel, sina_stats_dict
from goalx_backend.modelling.dc_model import TIER1_COMPETITIONS, train_competition
from goalx_backend.modelling.forecast import generate_forecasts, record_coverage
from goalx_backend.modelling.team_align import (
    alias_index,
    clubelo_alias_for_team,
    english_alias_census,
    record_srct_aliases,
    record_srct_en_aliases,
    srct_en_team_ids,
    sync_clubelo_aliases,
)
from goalx_backend.modelling.team_align import (
    srct_alias_sets as team_align_srct_sets,
)


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


def propline_capture() -> propline.ProplineRunStats:
    """
    PropLine 手动快照（2026-11-01 票 06 裁决：留库 + manual CLI 入口）。

    三源停采后无调度入口——本入口保住备用重启能力（美洲/日职覆盖），
    日限额护栏由 provider 侧 429 fail-closed 保证。
    """
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        return propline.fetch_and_store_odds(
            conn, settings, client, raw_root=settings.observations_dir
        )


def eu_odds_closing(window_minutes: int = 35) -> dict[str, object]:
    """收盘窗口尽力快照（票 32/35）：与常规采集共享月预算（同一记账路径）。"""
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        stats = oddsapi.fetch_closing_window(
            conn,
            settings,
            client,
            window_minutes=window_minutes,
            raw_root=settings.observations_dir,
        )
    # 三键口径 = 旧 flow 返回面（票 03 不变量：载荷形状不变）
    return {
        "events": stats.events,
        "snapshots": stats.snapshots,
        "credits_used": stats.credits_used,
    }


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
        # auto-paper 票 01：skip 三因逐场落覆盖账（普查 REPLACE，幂等）
        coverage = record_coverage(conn, business_date=resolved, skipped=stats.skipped)
    return {
        "generated": stats.generated,
        "duplicates": stats.duplicates,
        "xg_blended": stats.xg_blended,
        "xg_unresolved": stats.xg_unresolved,
        "skipped": len(stats.skipped),
        "covered": coverage["covered"],
    }


def baseline_freeze() -> dict[str, int]:
    """
    三锚市场基准冻结（auto-paper 票 01）：读库 as-of 冻结，无采集副作用。

    锚标签取当前北京时之前最近的冻结锚（12:30/16:30/21:00；定拍 cron 在
    锚后 3 分钟）。cutoff 封顶锚+30min 窗——晚到补跑不把补跑时刻当截止，
    锚后 >30min 的价不倒灌该锚。语料缺席降级——欧赔共识走 legacy 行源
    （通常空，eu_absent 计数），JC 面照冻不阻断。
    """
    settings = get_settings()
    label = anchor_label_at()
    if label is None:
        raise RuntimeError("当前北京时早于首锚 12:30，无锚可冻（误拍防护）")
    business_date = fx_store.beijing_business_date()
    cutoff = effective_cutoff(label, business_date)
    with task_conn() as conn:
        duck_con = None
        try:
            duck_con = corpus_duckdb.connect(settings)
        except Exception:
            logger.warning("corpus duckdb 缺席：eu_consensus 降级 legacy 行源")
        try:
            stats = freeze_anchor_baselines(
                conn,
                anchor_label=label,
                cutoff_at=cutoff,
                business_date=business_date,
                duck_con=duck_con,
            )
        finally:
            if duck_con is not None:
                duck_con.close()
    return {
        "fixtures": stats.fixtures,
        "jc_frozen": stats.jc_frozen,
        "eu_frozen": stats.eu_frozen,
        "jc_absent": stats.jc_absent,
        "eu_absent": stats.eu_absent,
        "duplicates": stats.duplicates,
    }


def auto_paper() -> dict[str, int]:
    """
    auto-v2 探针阵列落注（edge-program 票 01；auto-v1 冻结版自换栈起停新注）。

    锚后 1 分钟跑当前锚的探针组合+落注。校准探针（常亮）：claimed-EV
    每场最优 top-3 全四玩法平注 ¥2（¥6/晚），edge_source=calibration_probe；
    陈盘/滚存/价格带探针熄灯记零注留痕（点亮=扫描器晋升+人裁改参数升
    版本）；彩池三玩法期次一票随本引擎落（source=auto-v2，¥20/晚测量帽
    内）；官方性同比对与台账沿用 v1 通道。语料缺席降级 legacy reads
    （与 freeze 同款）。
    """
    label = anchor_label_at()
    if label is None:
        raise RuntimeError("当前北京时早于首锚 12:30，无锚可跑（误拍防护）")
    settings = get_settings()
    with task_conn() as conn:
        reads = None
        try:
            duck_con = corpus_duckdb.connect(settings)
        except Exception:
            duck_con = None
            logger.warning("corpus duckdb 缺席：auto-v2 资格校验降级 legacy reads")
        try:
            if duck_con is not None:
                reads = bind_quote_reads(
                    conn, duck_con, lambda fid: mapping.srct_sid_for_fixture(conn, fid)
                )
            stats = probes.run_auto_v2(
                conn, run_at=datetime.now(UTC), anchor_label=label, reads=reads
            )
        finally:
            if duck_con is not None:
                duck_con.close()
    logger.info("auto-v2 anchor: {}", stats)
    return stats


def odds_anchor_dense() -> dict[str, object]:
    """
    停售加密探测 + 开售锚轮询（票 47/04；feed-cutover 票 01 收窄）。

    原双锚的 odds_api 拉取双腿已拔（2026-11-07）：三源停采后零候选
    零产出空转，锚真值已由源T as-of 永久承担（CLV=srct_closing_prob、
    陈盘=sharp as-of，2026-09-25 裁决）。本任务唯一活腿=探针——候选面
    已切广义已链（odds_api ∪ 源T 链），停售加密采样与显式 stopped
    迁移检测自此复活。零候选零请求（开售轮询带内除外，票 04）；探测
    异常降级留痕不炸拍（票 04 F1：让位一拍，不丢跨拍状态）。
    """
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        try:
            probe = sporttery.probe_sale_stops(
                conn, settings, client, raw_root=settings.observations_dir
            )
        except (httpx.HTTPError, ValueError, sqlite3.Error) as exc:
            logger.warning("sale probe degraded（{}）：本拍停售检测/开售轮询让位", exc)
            probe = sporttery.SaleStopProbe()
    return {
        "sale_stop_probe": {
            "candidates": probe.candidates,
            "stopped": len(probe.stopped),
        },
    }


def _isolated_anchor_pull(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    anchor: str,
    targets: list[tuple[str, str]],
) -> oddsapi.OddsIngestStats:
    """
    单腿锚拉取（票 47）：异常降级为零统计不炸整跑（本拍放弃，下拍兜底）。

    已知残留：停售迁移已被消费而锚拉取失败时，该场决策锚缺失（30 分钟
    closing 循环只兜底开球前 35 分钟内的场次）——传输层 3 次重试后仍
    失败属罕见，接受并留痕日志。
    """
    try:
        return oddsapi.fetch_anchor_snapshots(
            conn,
            settings,
            client,
            anchor=anchor,
            targets=targets,
            raw_root=settings.observations_dir,
        )
    except (oddsapi.CreditBudgetExceeded, httpx.HTTPError) as exc:
        logger.warning("anchor pull {} failed (leg degraded): {}", anchor, exc)
        return oddsapi.OddsIngestStats()


def srcb_collect() -> dict[str, object]:
    """
    源B变化时序采集（票 49 采集先行，2026-09-21 用户裁决）。

    低频回溯式：未开赛彩池场次（36h 窗）× 17 核心 pid，每日两拍；连续
    拉取并集自然积累完整路径。只攒语料不接消费端——mid↔fixture 身份
    绑定与交叉验证属票 49 后半程。
    """
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        upcoming = srcb.upcoming_pool_mids(
            conn, now_utc=datetime.now(UTC).isoformat(timespec="seconds")
        )
        stats = srcb.collect_srcb_changes(
            conn, settings, client, mids=[m["mid"] for m in upcoming]
        )
    return {
        "mids": len(stats.mids),
        # infohub 段拒发数（审计批D #8）：>0 即 alias 面仍在窗外流动
        "non_okooo_mids": stats.non_okooo_mids,
        "requests": stats.requests,
        "rows_added": stats.rows_added,
        "rows_absorbed": stats.rows_absorbed,
        "failed": len(stats.failed),
    }


def srct_night_run(  # noqa: PLR0913 运行旋钮+测试注入口，cli/flow 共用
    *,
    request_cap: int | None = None,
    no_window: bool = False,
    list_mode: bool = False,
    limit: int = 20,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    now_fn: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] | None = None,
    seasons: tuple[srct_night.SeasonWindow, ...] | None = None,
    jc_phase: bool = True,
) -> dict[str, object]:
    """
    源T夜班（票 55 切片 13）：窗口内按预算推进 Phase1 回填批。

    窗口外零成本返回（deployment 01:00 触发总在窗内，此判保手工误触）；
    摘要已落 checkpoint 库，这里只回计数（failed 采样防 flow 日志爆量）。
    list_mode=True 只读查最近摘要（不发请求）。settings/client/now_fn/
    sleeper/seasons/jc_phase 注入口只服务测试接缝（cli/flow 共用本函数，
    缺省走真实配置、真实连接、本机墙钟与 Phase1 全表）。
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
    try:
        if list_mode:
            return {"nights": store.night_summaries(limit=limit)}
        window = None if no_window else srct_night.NIGHT_WINDOW
        with ExitStack() as stack:
            run_client = (
                client
                if client is not None
                else stack.enter_context(httpx.Client(verify=browser_ssl_context()))
            )
            summary = srct_night.run_night(
                store,
                resolved,
                run_client,
                now_fn=now_fn,
                window=window,
                request_cap=(
                    srct.NIGHT_REQUEST_CAP if request_cap is None else request_cap
                ),
                sleeper=sleeper,
                seasons=seasons,
                jc_phase=jc_phase,
            )
        payload = asdict(summary)
        payload["failed"] = dict(
            list(summary.failed.items())[: srct_night.FAILED_SAMPLE_CAP]
        )
        return payload
    finally:
        store.close()


def srct_shift_run(
    *,
    request_cap: int | None = None,
    no_window: bool = False,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> dict[str, object]:
    """
    源T 当期班（票 65）：在售清单 → 四类拍决策 → raw+bronze append。

    夜窗让位/预算触顶都正常返回（拍键幂等，下轮自愈）；这里只回计数。
    no_window=True 跳过夜窗让位（白天冒烟/手工回补）。settings/client
    注入口只服务测试接缝（cli/flow 共用本函数，缺省真实装配）。

    赛前拍相位（票 14）：主库解析 JC 板上场已链候选注入 run_shift（同一
    now 钟面）；解析失败降级空候选继续拍班（拍键幂等下轮自愈），payload
    留 prematch_degraded 注记。
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
    now = datetime.now(UTC)
    prematch: list[tuple[str, datetime]] = []
    prematch_degraded: str | None = None
    try:
        with task_conn() as conn:
            prematch = srct_prematch.jc_prematch_candidates(conn, now)
    except (sqlite3.Error, ValueError) as exc:  # I3：坏 kickoff 串只降级本相位
        prematch_degraded = f"{type(exc).__name__}: {exc}"[:120]
        logger.warning("prematch 候选解析降级（{}）：本轮赛前拍让位", exc)
    try:
        with ExitStack() as stack:
            run_client = (
                client
                if client is not None
                else stack.enter_context(httpx.Client(verify=browser_ssl_context()))
            )
            stats = srct_shift.run_shift(
                store,
                resolved,
                run_client,
                request_cap=(
                    srct_shift.SHIFT_REQUEST_CAP if request_cap is None else request_cap
                ),
                enforce_window=not no_window,
                now_fn=lambda: now,
                prematch_candidates=prematch,
            )
    finally:
        store.close()
    payload = srct_shift.stats_dict(stats)
    if prematch_degraded is not None:
        payload["prematch_degraded"] = prematch_degraded
    # 拍后收尾（odds-read-switch 票 01/03）：先映射同步（读侧短连接），
    # 再增量物化（写侧持锁）——顺序不可反，remat 持写锁时映射 reader
    # 会降级空转。各自降级不拖垮拍班本体（同 wrap 流降级哲学）
    # 票 03 C5：EOFError=无锁读者撞拍班并发追加撕裂的 gzip member——
    # remat 全量 3h 不许被它整个炸掉，降级+下轮重试
    try:
        payload["mapping_sync"] = mapping_sync()
    except (sqlite3.Error, duckdb.Error, OSError, EOFError) as exc:
        logger.warning("post-beat mapping sync degraded: {}", exc)
        payload["mapping_sync"] = {"degraded": str(exc)}
    try:
        payload["remat"] = srct_remat_run()
    except (sqlite3.Error, duckdb.Error, OSError, EOFError) as exc:
        logger.warning("post-beat remat degraded: {}", exc)
        payload["remat"] = {"degraded": str(exc)}
    return payload


def srct_remat_run(
    *, full: bool = False, settings: Settings | None = None
) -> dict[str, object]:
    """
    源T silver 增量物化（票 11）：夜班后定拍，A 线参考链保鲜。

    fixture/xg 按水位门控全量；odds 水位增量（触及分区重写）；护栏命中
    自动全量兜底。full=True 强制全量（维护口径）。增量语义与护栏面见
    data/ingest/srct_remat 模块注释；settings 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
    try:
        report = srct_remat.run_remat(store, full=full)
    finally:
        store.close()
    return asdict(report)


def settlement_sweep() -> dict[str, int]:
    """结算批跑（配合开奖导入；paper/live 统一引擎）。"""
    with task_conn() as conn:
        return run_settlement(conn)


def _gold_rebuild_market_worker(
    settings: Settings,
) -> srct_market.SilverMarketReport:
    """段体（market）：子进程内自建 store，退出即由 OS 全额回收。"""
    store = CorpusStore(settings.corpus_root)
    try:
        return srct_market.build_market_quotes(store)
    finally:
        store.close()


def _gold_rebuild_gold_worker(settings: Settings) -> gold.GoldBuildReport:
    """段体（gold）：face 迁移 + 语料桥连接 + 全量重算，全在子进程内。"""
    store = CorpusStore(settings.corpus_root)
    try:
        face = connect(settings.db_path)
        try:
            migrate(face)  # admin_match_exclusions 缺表即 fail-closed 的前提
            duck_con = corpus_duckdb.connect(settings)
            try:
                return gold.build_match_features(store, face, duck_con)
            finally:
                duck_con.close()
        finally:
            face.close()
    finally:
        store.close()


def _run_segment_in_child[T](func: Callable[[Settings], T], settings: Settings) -> T:
    """
    段级进程隔离（gold-rebuild 内存收口票 04，票 03 裁决 P2）。

    重段各起独立 spawn 子进程：段退出 OS 全额回收，Python arena 高水位
    残留根治（gunicorn max_requests 同哲学；2026-11-06 晨间 30GB swap
    实录的挤压根因）。ProcessPoolExecutor 会复用 worker，必须**每段独立
    with 块**——段末 shutdown 退出进程才回收。子进程不持锁：父进程全程
    独占 silver_build_lock，窗口语义不变。
    """
    ctx = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
        return ex.submit(func, settings).result()


def _segment_in_child_or_defer[T](
    func: Callable[[Settings], T],
    settings: Settings,
    tag: str,
    payload: dict[str, object],
) -> tuple[T | None, dict[str, object] | None]:
    """
    段子进程执行；硬死（OOM 等）→（None, deferred 报告）让位明日自愈。

    只捕 BrokenProcessPool：spawn 基建本身失败（executor 信号量 OSError
    等）不转 deferred 而硬闯 Crashed——环境级故障需人眼，静默让位会把
    每日定拍变成永久跳过（correctness 2026-11-06 裁决=fail-loud）。
    """
    try:
        return _run_segment_in_child(func, settings), None
    except BrokenProcessPool as exc:
        note = f"deferred_segment_child_lost_{tag}: {exc}"
        logger.warning("gold-rebuild {}", note)
        return None, {**payload, "deferred": note}


def gold_rebuild(  # noqa: PLR0911 deferred 状态机：锁忙/三探/两段子进程硬死，线性分支即审计面
    *, settings: Settings | None = None
) -> dict[str, object]:
    """
    Gold 日频重算（misprice 票 03①）：market_quote 保鲜 + gold 全量重建。

    remat 08:10 只物化 odds/fixture/xg——market_quote（gold 收盘族输入）
    曾停摆月余无任何红面（2026-10-07 后 ou_line_drift 对当月全缺，票 03④
    根因坐实）。本任务银段先重物化 market_quote（asian_odds+over_down
    bronze 幂等全量）再 gold 全量重算，08:40 定拍赶在 09:05 晨检 gold
    新鲜度面前。settings 注入口只服务测试接缝。

    内存护栏（票 03 运行实录 2026-11-02 首班；票 04 内存收口）：market/
    gold 两重段各起 spawn 子进程执行——段退出 OS 全额回收，arena 残留
    根治；子进程硬死（OOM 等）→ BrokenProcessPool → deferred 让位明日
    自愈，不硬闯 Crashed。

    构建互斥（boundary-hardening 票 02）：market_quote 面与手工
    srct-market/srct-odds 同走 silver_build_lock（同一把 srct_remat_lock）
    ——撞锁（如 08:10 全量 remat 未收班）即 deferred 让位，不并行重建。

    余量探测在预算内（票 07③）：先持 silver_build_lock（重任务并发
    预算=1），锁内测余量——"通过余量检查"即蕴含"独占预算"，启动瞬间
    两任务同时通过检查后各自分配内存的窗口被关死。口径/下限与 remat
    全量同源（srct_remat.headroom_bytes / FULL_REMAT_MIN_HEADROOM_BYTES）。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    store = CorpusStore(resolved.corpus_root)
    try:
        with srct_remat.silver_build_lock(store):
            headroom = srct_remat.headroom_bytes()
            if headroom < srct_remat.FULL_REMAT_MIN_HEADROOM_BYTES:
                note = f"deferred_low_headroom: {headroom / 1024**3:.1f}GiB"
                logger.warning("gold-rebuild {}", note)
                return {"deferred": note}  # with 出口释放预算，明日定拍自愈
            market_report, deferred = _segment_in_child_or_defer(
                _gold_rebuild_market_worker, resolved, "market", {}
            )
            if deferred is not None:
                return deferred
            market_report = cast(
                "srct_market.SilverMarketReport", market_report
            )  # helper 契约：二者恰一非 None，cast 仅收窄
            # 段间余量复探（2026-11-05 OOM 收口）：子进程退出=OS 全额回收，
            # 复探见真值；不足即 deferred（明日自愈）不硬闯 Crashed
            headroom = srct_remat.headroom_bytes()
            if headroom < srct_remat.FULL_REMAT_MIN_HEADROOM_BYTES:
                note = (
                    f"deferred_midrun_low_headroom_before_gold:"
                    f" {headroom / 1024**3:.1f}GiB"
                )
                logger.warning("gold-rebuild {}", note)
                return {"deferred": note, "market": asdict(market_report)}
            report, deferred = _segment_in_child_or_defer(
                _gold_rebuild_gold_worker,
                resolved,
                "gold",
                {"market": asdict(market_report)},
            )
            if deferred is not None:
                return deferred
            report = cast("gold.GoldBuildReport", report)  # 同上
            # correctness F3：API 只读连接短暂持锁会让视图刷新（写开库）
            # IOException——gold/meta 已落盘，视图路径绑定刷新本近幂等，
            # 短重试后仍占则记账跳过（明日自愈），不让日频面假红
            # 桥重建段前同探（本段只 CREATE OR REPLACE VIEW=元数据级，
            # 廉价；探的是后续首个消费方进场前的系统余量水位）
            headroom = srct_remat.headroom_bytes()
            if headroom < srct_remat.FULL_REMAT_MIN_HEADROOM_BYTES:
                note = (
                    f"deferred_midrun_low_headroom_before_duckdb:"
                    f" {headroom / 1024**3:.1f}GiB"
                )
                logger.warning("gold-rebuild {}", note)
                return {
                    "deferred": note,
                    "market": asdict(market_report),
                    "gold": asdict(report),
                }
            duckdb_note = "view_refresh_skipped:duck-lock"
            for attempt in range(3):
                try:
                    duckdb_note = str(corpus_duckdb.build_corpus_duckdb(store))
                    break
                except duckdb.Error as exc:
                    logger.warning(
                        "corpus.duckdb 视图刷新锁占（第 {} 次）：{}", attempt + 1, exc
                    )
                    time.sleep(10)
    except srct_remat.RematLockBusy as exc:
        note = f"deferred_silver_build_lock_busy: {exc}"
        logger.warning("gold-rebuild {}", note)
        return {"deferred": note}
    finally:
        store.close()
    return {
        "market": asdict(market_report),
        "gold": {**asdict(report), "duckdb": duckdb_note},
    }


def draw_results_sync() -> dict[str, object]:
    """
    赛果自动同步（票 76 切换：源T 日页物化落事实，官方 uniform 兜底+审计）。

    链序即分工：映射链刷新（票 77，kickoff canonical 顺带校准）→ 源T
    物化（零请求，已覆盖场落事实）→ uniform 同步（no-冲正规则天然把它
    限制在差集场与官方 void 观测——审计源顺手救兜底）。语料桥缺席 →
    源T 段降级跳过，uniform 独走（票 44 行为不变）。无待出赛果时
    uniform 零请求跳过（零成本，与 eu-odds-closing 同模式）。
    """
    settings = get_settings()
    with (
        task_conn() as conn,
        corpus_anchor() as duck_con,
        polite_client() as client,
    ):
        links: mapping.MappingSyncStats | None = None
        if duck_con is not None:
            links = mapping.sync_fixture_links(
                conn, duck_con, team_aliases=team_align_srct_sets(conn)
            )
        srct_stats = srct_results.materialize_results(conn, duck_con)
        stats, rec = uniform.sync_uniform_results(conn, settings, client)
        # 赛果落定后自动评分（auto-paper 票 02，prereg acc-v1）：幂等，
        # 同结果重跑被 UNIQUE 吸收；结果更正走 fingerprint 覆盖重评
        accuracy_stats = accuracy.score_settled_fixtures(conn)
    return {
        "links": links.as_dict() if links else None,
        "srct": srct_stats.as_dict(),
        "uniform": asdict(stats),
        "uniform_reconcile": reconcile.stats_dict(rec),
        "accuracy": asdict(accuracy_stats),
    }


def official_results_reconcile() -> dict[str, object]:
    """
    赛果日终审计（票 76 收敛）：openfootball 比分对账，不落事实。

    审计面收敛史：票 44 起 uniform 落事实 + 源D(caiguo)/openfootball 双
    审计；票 76 起源T 物化落事实，uniform 转审计（对账段随
    draw-results-sync 内联运行，draw_sync_runs 留痕），源D 退役——与
    uniform 同属官方发布族，第三份对账冗余（模块留档作备用源）。
    """
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        of_rec = openfootball.reconcile_openfootball(
            conn, settings, client, alias_index=alias_index(conn)
        )
    return {"openfootball": reconcile.stats_dict(of_rec)}


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


@contextmanager
def corpus_anchor() -> Generator[duckdb.DuckDBPyConnection | None]:
    """Srct 收盘锚连接统一开关（票 75）：corpus_duckdb 只读连接，库缺席降级 None。"""
    try:
        con = corpus_duckdb.connect(get_settings())
    except (duckdb.Error, OSError) as exc:
        logger.warning("clv srct anchor unavailable, degraded: {}", exc)
        con = None
    try:
        yield con
    finally:
        if con is not None:
            con.close()


def mapping_sync() -> dict[str, object]:
    """
    跨源映射同步（票 77）：链物化 + kickoff 校准 + 别名补源。

    物化 fixture↔源T sid 链、kickoff canonical 校准、srct/clubelo 别名
    补源。语料桥缺席（首夜 silver 落地前/库未建）降级零动作不抛错——
    与 CLV 收盘锚同款降级哲学。别名落库走属主（modelling/team_align）。

    live 兜底（odds-read-switch 票 01）：当期班 srct_shift_matches 行
    （checkpoint 缺席降级空表）参与在售场解析——universe 优先、开球
    ±10min 硬门。

    英文别名活水（identity-alias 票 01）：已链场次重解析归档 odds 原页
    取源T英文名 → srct_en 别名（队级幂等门兜常态成本）；clubelo 桥以
    srct_en 解饿；尾附竞彩 universe 英文可解析普查。
    """
    with task_conn() as conn, corpus_anchor() as duck_con:
        stats = mapping.sync_fixture_links(
            conn,
            duck_con,
            team_aliases=team_align_srct_sets(conn),
            live_rows=_live_mapping_rows(),
        )
        aliases = record_srct_aliases(conn, stats.srct_alias_pairs)
        en_pairs, en_raw_misses = _srct_en_alias_pairs(conn)
        en_added, en_conflicts = record_srct_en_aliases(conn, en_pairs)
        clubelo_report = sync_clubelo_aliases(conn, clubelo.distinct_clubs(conn))
        census = english_alias_census(conn)
    return {
        **stats.as_dict(),
        "srct_aliases_added": aliases,
        "srct_en_aliases_added": en_added,
        "srct_en_raw_misses": en_raw_misses,
        "srct_en_conflicts": en_conflicts,
        "clubelo": clubelo_report.as_dict(),
        "english_alias_census": census,
    }


def _srct_en_alias_pairs(
    conn: sqlite3.Connection, *, corpus_root: Path | None = None
) -> tuple[list[tuple[int, str]], int]:
    """
    已链场次 → (team_id, 源T英文名) 对子（identity-alias 票 01）。

    按 sid 读归档 odds_1x2d 原页重解析（零网络）；缺原页/坏档（截断 gz
    抛 EOFError·zlib.error，非 OSError 子类）/页在而英文名缺（形态漂移）
    一律计数进 misses 不抛错——该队留普查未解析清单，晨检守门兜底。
    队级幂等门=已持 srct_en 别名的队跳过——首拍回填历史面，此后每拍
    近零读。``corpus_root`` 显式参数供测试隔离（缺省=运行 settings）。
    """
    store = CorpusStore(corpus_root or get_settings().corpus_root)
    skip = srct_en_team_ids(conn)
    pairs: list[tuple[int, str]] = []
    misses = 0
    try:
        for row in mapping.linked_sid_teams(conn, skip):
            try:
                body = store.read_raw(
                    srct.SRCT_PROVIDER, srct.ODDS_DATASET, row.sid, ext=".js"
                )
            except (OSError, EOFError, zlib.error):
                misses += 1
                continue
            home_en, away_en = srct.parse_odds_page_names(body)
            for team_id, name in (
                (row.home_team_id, home_en),
                (row.away_team_id, away_en),
            ):
                if team_id in skip:
                    continue
                if name:
                    pairs.append((team_id, name))
                else:
                    misses += 1
    finally:
        store.close()
    return pairs, misses


def _live_mapping_rows() -> list[mapping.UniverseRow] | None:
    """
    当期班 srct_shift_matches → 映射 live 行。

    checkpoint 读失败返回 None（=未知，空≠无：同步侧不撤现有 live 链）；
    正常返回投影行（含空表——已知空走镜像真值撤链）。
    """
    store = CorpusStore(get_settings().corpus_root)
    try:
        rows = store.shift_matches()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("live mapping rows unavailable, degraded: {}", exc)
        return None
    finally:
        store.close()
    return list(mapping.live_universe_rows(rows.values()))


def mapping_audit() -> dict[str, object]:
    """映射审计报告（票 77）：分桶未映射/歧义率 + 歧义队列 + 起步门。"""
    with task_conn() as conn:
        return mapping.audit_mapping(conn)


def clubelo_elo_for_team(canonical_name: str, on_date: date) -> dict[str, object]:
    """
    点时 Elo 查询按中文 canonical 队名走通（票 77 验收）：别名桥 → 评级区间。

    未接通（无 clubelo 别名/区间不覆盖）各态诚实返回，不猜值。
    """
    with task_conn() as conn:
        team = fx_store.find_team_by_name(conn, canonical_name)
        if team is None:
            return {"team": canonical_name, "status": "team_unknown"}
        alias = clubelo_alias_for_team(conn, int(team["id"]))
        if alias is None:
            return {"team": canonical_name, "status": "alias_unmapped"}
        elo = clubelo.rating_at(conn, alias, on_date)
        return {
            "team": canonical_name,
            "club": alias,
            "date": on_date.isoformat(),
            "elo": elo,
            "status": "ok" if elo is not None else "rating_uncovered",
        }


def guardian_sync(*, request_cap: int | None = None) -> dict[str, object]:
    """卫报新闻语料同步（票 79）：回填翻页/日增量，断点续跑幂等（直连）。"""
    settings = get_settings()
    store = CorpusStore(settings.corpus_root)
    try:
        with httpx.Client(verify=browser_ssl_context()) as client:
            stats = guardian.sync_guardian(
                store, settings, client, request_cap=request_cap
            )
    finally:
        store.close()
    return guardian.stats_dict(stats)


def news_pit_beat(*, request_cap: int | None = None) -> dict[str, object]:
    """
    新闻/伤停 PIT 前向夜拍（autoresearch 票 17，§4.1#11）。

    daily-wrap 23:30 deployment 内定拍（零新增 cron）：当日窗文章以拍纪元
    observed_at 落 ``news_article_pit`` 专表（append-only，晚到不回灌）；
    共享卫报 500/日请求账本（语料轨同 ledger，防双花）。手动触发=同命令
    cli（``goalx news-pit-beat``，冒烟/手工回补）。settings 注入口只服务
    测试接缝。
    """
    settings = get_settings()
    store = CorpusStore(settings.corpus_root)
    try:
        with httpx.Client(verify=browser_ssl_context()) as client:
            stats = news_pit.run_news_pit_beat(
                store, settings, client, request_cap=request_cap
            )
    finally:
        store.close()
    return news_pit.stats_dict(stats)


def guardian_status() -> dict[str, object]:
    """卫报双轨报告（票 17）：回填存量清单 + 增量通道（夜拍）只读面。"""
    settings = get_settings()
    store = CorpusStore(settings.corpus_root)
    try:
        return news_pit.dual_track_face(store, settings)
    finally:
        store.close()


def clubelo_sync(*, backfill: bool = False) -> dict[str, object]:
    """
    Clubelo Elo 评级同步（票 74）。

    日拍 = 单请求当日全量快照（09:10 调度）；backfill=True 在快照后逐队
    拉全历史区间（一次性，不进调度）。站点不可达时抛出由调用方处置。
    """
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        if backfill:
            stats = clubelo.backfill_history(conn, settings, client, date.today())
        else:
            stats = clubelo.sync_snapshot(conn, settings, client, date.today())
    return clubelo.stats_dict(stats)


def pool_snapshot() -> dict[str, object]:
    """彩池同步（票 68 官方化）：体彩官方在售对阵+上期彩果 → pool 域表。"""
    settings = get_settings()
    with task_conn() as conn, httpx.Client(verify=browser_ssl_context()) as client:
        stats = zucai_official.sync_current(conn, settings, client)
    return zucai_official.stats_dict(stats)


def pool_backfill(periods: int = 20) -> dict[str, object]:
    """彩池历史彩果回填（票 68）：V2 逐期倒查官方注数/奖池/销量。"""
    settings = get_settings()
    with task_conn() as conn, httpx.Client(verify=browser_ssl_context()) as client:
        stats = zucai_official.backfill_draws(conn, settings, client, periods=periods)
    return zucai_official.stats_dict(stats)


def intel_collection() -> dict[str, object]:
    """
    情报采集（票 09）：内部推导 + 澳客伤停 + 新浪伤停。

    幂等；单一源故障不阻塞其余。
    """
    with task_conn() as conn:
        stats = collect_pool_intel(conn)
    injury: dict[str, object] = {}
    try:
        with (
            task_conn() as conn,
            httpx.Client(verify=browser_ssl_context()) as client,
        ):
            injury = injury_stats_dict(collect_injury_intel(conn, client))
    except httpx.HTTPError as exc:
        logger.warning("澳客伤停采集整体失败（推导情报不受影响）: {}", exc)
        injury = {"error": str(exc)}
    sina: dict[str, object] = {}
    try:
        with (
            task_conn() as conn,
            httpx.Client(verify=browser_ssl_context()) as client,
        ):
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


_CONTROL_CATEGORY = "m3_control"


def record_control_events(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """M3 证伪控制事件（票 13）：关闭的开关每日记一行（同 note 24h 内不重复）。"""
    events: list[str] = []
    disabled: list[str] = []
    if not settings.m3_analyst_enabled:
        disabled.append("analyst_disabled")
    if not settings.m3_fusion_enabled:
        disabled.append("fused_disabled")
    day_start = utc_now_iso()[:11] + "00:00:00+00:00"
    for note in disabled:
        if not rs.category_note_count(conn, _CONTROL_CATEGORY, note, day_start):
            # 记账须 atomic（票 02 correctness）：record_cost 裸 INSERT 无
            # commit——task_conn close 即回滚，控制事件从未落账（glm_chat
            # 记账同族坑，caliber-loop 2026-10-16 实锤先例）
            with atomic(conn):
                rs.record_cost(
                    conn, _CONTROL_CATEGORY, units=0.0, amount_cny=0.0, note=note
                )
            events.append(note)
    return events


TRACK2_CATCHUP_HOURS = 6  # 拍点错过后的迟到补拍视界（迟到旗如实记）


def _capture_track2_fixture(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    offset: timedelta,
    snap_type: str,
    decision: datetime,
    duck_con: quote_evidence.DuckCon | None = None,
) -> str:
    """
    单场拍点采集（票 05，逐场隔离单元）：'ok' | 'no_anchor'。

    四时钟（measure-04）：任务时刻（窗口计算，不落行）/ 目标拍点
    target_instant / 实际决策时刻 decision（拉取完成后，判定 as-of 与
    captured_at 同源）/ 响应观测时刻（证据表 observed_at，PIT 上界=
    决策时刻）——本拍新抓的报价不再被任务时刻滤掉。
    锚缺失不落库（留给 6h 视界下拍补采——空快照会锁死补拍窗口）；
    停售边界按拍点时点：stopped→不可购；无观测→不可购；存储 unknown
    值（SELL_STATUS_MAP 兜底产物）→ 同样不可购——非 on_sale 一律不
    冒充可购（measure-03）。
    """
    fid = int(row["id"])
    target_instant = (
        datetime.fromisoformat(str(row["kickoff_utc"])) - offset
    ).isoformat(timespec="seconds")
    decision_iso = decision.isoformat(timespec="seconds")
    # 票 07：班域构造 reader（sid 解析注入），单参调用
    reader = bind_quote_reads(
        conn, duck_con, lambda f: mapping.srct_sid_for_fixture(conn, f)
    )
    verdict = reader.adjudicate_had_quote(fid, decision_iso)
    sharp = reader.sharp_probs_asof(fid, decision_iso)
    if sharp is None:
        return "no_anchor"
    sale = fx_store.latest_sale_status_asof(conn, fid, "had", target_instant)
    if sale is None:
        purchasable, reason = False, "sale_status_unknown"
    elif str(sale["sale_state"]) == "on_sale":
        purchasable, reason = True, None
    elif str(sale["sale_state"]) == "stopped":
        purchasable, reason = False, "sale_stopped"
    elif str(sale["sale_state"]) == "not_on_sale":
        purchasable, reason = False, "sale_not_on_sale"
    else:
        purchasable, reason = False, "sale_status_unknown_value"
    record_track2_snapshot(
        conn,
        Track2Capture(
            fixture_id=fid,
            snap_type=snap_type,
            target_instant=target_instant,
            captured_at=decision_iso,
            purchasable=purchasable,
            purchasable_reason=reason,
            jc_odds=dict(verdict.jc_odds),
            jc_age_seconds=verdict.jc_age_seconds,
            p_sharp=sharp[0],
            anchor_ref=sharp[1],
        ),
    )
    return "ok"


# 锚拉取缝协议：与 _isolated_anchor_pull 同签名（anchor/targets 关键字）
_AnchorPull = Callable[..., "oddsapi.OddsIngestStats"]


def track2_snapshots(
    *,
    now: datetime | None = None,
    pull: _AnchorPull | None = None,
    decision_at: datetime | None = None,
) -> dict[str, object]:
    """
    轨二拍点采样（caliber-loop 票 05）：开球前 24h/12h 整点两源快照。

    候选面=广义 joined（odds_api 历史链 ∪ srct 链，票 01 单一出处语义；
    odds-read-switch 小账：原 odds_api 单一过滤 9-25 后结构性零候选）。
    窗口=拍点已过且在 6h 迟到视界内（kickoff ∈ [now+offset−6h, now+offset]）；
    锚书=拍点定向拉取（仅 odds_api 链场；corpus 链场证据直读源T 免拉取，
    零候选零请求）；竞彩=拍点时点最新 as-of 读（调盘面，龄如实记）；停售
    边界 purchasable=拍点时点（target_instant）停售状态；迟到补拍
    late_flag。now/pull/decision_at 注入点供测试（缺省=当前时刻/真锚拉取/
    拉取后取当前时刻）——判定 as-of 用 decision_at（拉取后），任务时刻只管
    窗口（measure-04：本拍新抓报价不再被任务时刻滤掉）。
    """
    settings = get_settings()
    anchor_pull = pull or _isolated_anchor_pull
    out: dict[str, object] = {}
    with (
        task_conn() as conn,
        polite_client() as client,
        corpus_duckdb.reader() as duck_con,
    ):
        moment = now or datetime.now(UTC)
        for offset_h, label in TRACK2_OFFSETS:
            offset = timedelta(hours=offset_h)
            horizon = timedelta(hours=TRACK2_CATCHUP_HOURS)
            start = (moment + offset - horizon).isoformat(timespec="seconds")
            end = (moment + offset).isoformat(timespec="seconds")
            in_window = fx_store.fixtures_in_window(conn, start, end)
            joined = mapping.joined_fixture_ids(conn, [int(r["id"]) for r in in_window])
            targets = [row for row in in_window if int(row["id"]) in joined]
            fresh: list[int] = []
            for row in targets:
                fid = int(row["id"])
                if not has_track2_snapshot(conn, fid, label):
                    fresh.append(fid)
            pulls: list[tuple[str, str]] = [
                (
                    str(row["odds_api_event_id"]),
                    str(row["odds_api_sport_key"]),
                )
                for row in targets
                if int(row["id"]) in fresh and row["odds_api_event_id"] is not None
            ]
            if pulls:
                anchor_pull(
                    conn, settings, client, anchor=f"track2_{label}", targets=pulls
                )
            decision = decision_at or datetime.now(UTC)
            captured = skipped_no_anchor = errors = 0
            for row in targets:
                fid = int(row["id"])
                if int(row["id"]) not in fresh:
                    continue
                try:
                    outcome = _capture_track2_fixture(
                        conn,
                        row,
                        offset=offset,
                        snap_type=label,
                        decision=decision,
                        duck_con=duck_con,
                    )
                except (ValueError, LookupError) as exc:
                    # 逐场隔离（锚腿先例）：单场毒数据不炸整拍，后续场照拍
                    errors += 1
                    logger.warning(
                        "track2 {} 拍点 {} 采集失败: {}", label, row["id"], exc
                    )
                    continue
                if outcome == "ok":
                    captured += 1
                elif outcome == "no_anchor":
                    skipped_no_anchor += 1
            out[label] = {
                "candidates": len(targets),
                "captured": captured,
                "skipped_no_anchor": skipped_no_anchor,
                "errors": errors,
                "anchor_pulls": len(pulls),
            }
        out["purchasable_corrected"] = rederive_purchasable(conn)
    return out


def funnel_day() -> dict[str, object]:
    """机会漏斗日拍（票 04+06）：五级分母+轨一估值+冻结决策。"""
    # EU 行源（票 05）：漏斗判定的共识/锚读 corpus（日拍一次短连接）
    with task_conn() as conn, corpus_duckdb.reader() as duck_con:
        report = record_funnel_day(
            conn,
            reads=bind_quote_reads(
                conn, duck_con, lambda fid: mapping.srct_sid_for_fixture(conn, fid)
            ),
        ).as_dict()
        bdate = str(report["business_date"])
        report["frozen"] = frozen_mod.record_frozen_decisions(
            conn, business_date=bdate
        ).as_dict()
        report["liveness"] = funnel_liveness(conn)
    return report


def freshness_shadow_day(business_date: str | None = None) -> dict[str, object]:
    """
    新鲜度双口径影子日拍（票 08）：五时钟+规则/观测双结论落库。

    复用今日读模型（sid/corpus 同层解析）；观测口径窗宽=同 300s（人
    裁决最尖对照）。仅记录——读数与票 09 预注册检查点对齐（11-13 首读）。
    过去日拒绝重放（correctness F2）：无冻结 as_of 的回放会把当日读成
    kickoff_passed→REPLACE 无声销毁原始快照——回放须带冻结时点另开
    通道；未来日允许（预拍当日晚场）。
    """
    bdate = business_date or fx_store.beijing_business_date()
    today_bdate = fx_store.beijing_business_date()
    if bdate < today_bdate:
        raise ValueError(
            f"拒绝重放过去日 {bdate}：无冻结 as_of 的回放会销毁当日原始"
            + "快照（REPLACE）；当日补拍直接跑不带 --date"
        )
    with task_conn() as conn, corpus_duckdb.reader() as duck_con:
        rows = today.build_today_view(conn, bdate, duck_con=duck_con)
        first_quote = rows[0].had_quote if rows else None
        counts = quote_evidence.record_freshness_shadow(
            conn, rows, bdate, as_of=first_quote.as_of if first_quote else utc_now_iso()
        )
        return {
            "business_date": bdate,
            "counts": counts,
            "summary": quote_evidence.freshness_shadow_summary(conn, bdate),
        }


def edge_scan_day(business_date: str | None = None) -> dict[str, object]:
    """
    探索轨日扫（autoresearch 票 01/04）：wrap 收尾后跑全部在册扫描器。

    观察日=北京业务日昨日（昨日场已结算）；S4 sharp 锚走 corpus 桥，
    库缺席降级 None（drift 不可算→有分辨记零不冒充）。读写同 task_conn。
    票 19：S4 sharp 端点双端历史时点走青铜重放（srct_asof 注入缝——
    silver 最新视图回答不了夜扫查昨日决策时点，§4.1#7 接线债）。
    """
    with task_conn() as conn, corpus_anchor() as duck_con:
        store = (
            CorpusStore(get_settings().corpus_root) if duck_con is not None else None
        )
        try:
            return edge_scan.edge_scan_day(
                conn,
                business_date=business_date,
                duck_con=duck_con,
                resolve_sid=lambda fid: mapping.srct_sid_for_fixture(conn, fid),
                asof_resolver=(
                    partial(srct_asof.asof_rows, store, duck_con)
                    if store is not None and duck_con is not None
                    else None
                ),
            )
        finally:
            if store is not None:
                store.close()


def edge_digest_week() -> dict[str, object]:
    """
    探索轨周报（票 04）：各扫描器窗口统计+新台账行+晋升建议。

    票 19：加锚书当季入库节奏节（duck 桥缺席=节 None，不冒充）。
    """
    with task_conn() as conn, corpus_anchor() as duck_con:
        store = CorpusStore(get_settings().corpus_root)
        try:
            report = edge_scan.build_edge_digest(
                conn,
                week_end=edge_scan.yesterday_business_date(),
                store=store,
                duck_con=duck_con,
            )
        finally:
            store.close()
    return report


def anchor_sentinel_capture(
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """
    断点哨兵采集面（票 34 设计稿 §二）：窄化 propline 拉取。

    仅 pinnacle（服务端 bookmakers 过滤，~50-200 请求/日，护栏 900 原样
    复用）仅活跃 sport（已链 36h ∪ JC FD 待链 7d，零请求选择）；滚中行
    写侧防护（commence 已过只入观察不入对账）。零候选零请求空转正常。
    settings/client/now 注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    moment = now or datetime.now(UTC)
    with ExitStack() as stack:
        conn = stack.enter_context(connect(resolved.db_path))
        migrate(conn)
        run_client = (
            client if client is not None else stack.enter_context(polite_client())
        )
        sports = propline.sentinel_sport_keys(conn, moment)
        stats = propline.fetch_and_store_odds(
            conn,
            resolved,
            run_client,
            raw_root=resolved.observations_dir,
            now=moment,
            sport_keys=sports,
            books=(propline.SENTINEL_BOOK,),
            skip_commenced=True,
        )
    return {
        "sports": sports,
        "requests": stats.requests_used,
        "snapshots": stats.ingest.snapshots,
        "duplicate_snapshots": stats.ingest.duplicate_snapshots,
        "post_commence": stats.post_commence,
        "skipped_no_books": stats.skipped_no_books,
        "unavailable_sports": stats.unavailable_sports,
        "daily_remaining": stats.daily_remaining,
    }


def anchor_sentinel_weekly(
    *,
    iso_year: int | None = None,
    iso_week: int | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    srct_reader: anchor_sentinel.SrctReader | None = None,
) -> dict[str, object]:
    """
    断点哨兵对账面（票 34 设计稿 §四）：周一 08:50 三轴计算+留痕+周报 md。

    缺省=最近完整 ISO 周（晚于它的周拒收——未来周行会钉死晨检面，
    独立复核 F3）；srct 腿只读消费 srct_pinnacle_closing_triplet（与
    生产 CLV 锚同一函数=同一语义，生产第一锚零改动）。语料桥缺席/视图
    缺（金丝雀读）=deferred 不落行（防"全覆盖塌缩"假 alert 留痕；晨检
    8 天无新周行条款兜底可见）。iso_year/iso_week/settings/now/srct_reader
    注入口只服务测试接缝。
    """
    resolved = settings if settings is not None else get_settings()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"运行面库不存在: {resolved.db_path}")
    if (iso_year is None) != (iso_week is None):
        msg = "--iso-year 与 --iso-week 须成对给出（缺省=最近完整周）"
        raise ValueError(msg)
    latest_year, latest_week = anchor_sentinel.latest_complete_iso_week(now)
    if iso_year is None or iso_week is None:
        iso_year, iso_week = latest_year, latest_week
    elif (iso_year, iso_week) > (latest_year, latest_week):
        msg = (
            f"拒绝未来 ISO 周 {iso_year}-W{iso_week:02d}"
            f"（最近完整周={latest_year}-W{latest_week:02d}；未来周行会钉死晨检面）"
        )
        raise ValueError(msg)
    conn = connect(resolved.db_path)
    try:
        migrate(conn)
        # 桥连接活到 compute 结束（独立复核 F1：srct 腿闭包在 compute 期逐场
        # 查询，提前 close=逐场降级 None=整周假 1b alert）
        duck_con = None
        try:
            if srct_reader is None:
                try:
                    duck_con = corpus_duckdb.connect(resolved)
                    quote_evidence.assert_odds_change_event_view(duck_con)
                except (duckdb.Error, OSError) as exc:
                    note = f"corpus_anchor_unavailable: {exc}"
                    logger.warning("anchor-sentinel-weekly {}", note)
                    return {
                        "deferred": note,
                        "iso_year": iso_year,
                        "iso_week": iso_week,
                    }
                srct_reader = anchor_sentinel.make_srct_reader(conn, duck_con)
            report = anchor_sentinel.compute_week(
                conn, iso_year, iso_week, srct_reader=srct_reader, now=now
            )
        finally:
            if duck_con is not None:
                duck_con.close()
        store = CorpusStore(resolved.corpus_root)
        try:
            path = anchor_sentinel.write_week_report(store, report)
        finally:
            store.close()
    finally:
        conn.close()
    return {**report, "md_path": str(path)}


def frozen_reconcile() -> dict[str, object]:
    """冻结决策执行对账（票 06）：结算面+源T 收盘锚+锁后漂移。"""
    with task_conn() as conn, corpus_anchor() as anchor:
        if anchor is None:
            return {
                "considered": 0,
                "settled": 0,
                "closed": 0,
                "close_backfilled": 0,
                "still_pending": 0,
                "funnel_close_healed": 0,
                "paper_profit_flat": 0.0,
                "missing_close_total": frozen_mod.missing_close_total(conn),
                "note": "corpus anchor 不可用，本拍跳过",
            }
        provider = _srct_close_provider(anchor)
        stats = frozen_mod.reconcile_frozen_decisions(conn, provider)
    logger.info("frozen reconcile: {}", stats)
    return stats


def _srct_close_provider(
    duck_con: duckdb.DuckDBPyConnection,
) -> frozen_mod.CloseProvider:
    """收盘概率提供方（clv.srct_closing_prob 同口径；注入缝供测试）。"""

    def provide(
        home: str, away: str, kickoff_utc: str, selection: str
    ) -> tuple[float, str, str] | None:
        return clv_mod.srct_closing_prob(duck_con, home, away, kickoff_utc, selection)

    return provide


def m3_evaluation() -> dict[str, object]:
    """
    M3 评测协议（票 13）：三列报告 + 赛后复核入队 + 控制事件。

    票 05：市场基准 corpus 行源，与验证 API 同口径——定时面不接会让
    corpus-only 场次在 API 进评分集合、在本拍落 no_market_baseline。
    """
    with task_conn() as conn, corpus_duckdb.reader() as duck_con:
        report = m3_protocol_report(
            conn,
            duck_con=duck_con,
            asof_batch=(
                # 请求域 resolver：内部多次样本构建共享青铜扫 memo
                srct_asof.batch_asof_resolver(
                    CorpusStore(get_settings().corpus_root), duck_con
                )
                if duck_con is not None
                else None
            ),
        )
        review = enqueue_post_settle(conn)
        record_control_events(conn, get_settings())
    return {
        "tier_a": report.tier_a.verdict,
        "tier_b": report.tier_b.verdict,
        "paired": report.paired_fused_vs_ml.pairs,
        "post_settle_enqueued": review.enqueued,
    }
