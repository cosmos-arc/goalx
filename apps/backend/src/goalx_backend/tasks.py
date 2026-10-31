"""
共享任务体：cli 与 Prefect flows 调用同一实现（ADR-0005）。

背景：cli ingest-jingcai 曾绕过 capture_jingcai 静默丢失证据存证——同一
任务两份实现必然漂移（2026-09 架构评审候选 4）。Prefect flow、cli 命令
与 api 触发端点是同一任务的三个 adapter；本模块是任务实现的唯一住所。

不含调度的运维命令（backtest/校准/报表等）只有 cli 一个 adapter，仍直接
使用 task_conn 壳。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

import duckdb
import httpx
from loguru import logger

from goalx_backend.betting import frozen as frozen_mod
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
from goalx_backend.data import corpus_duckdb, mapping, reconcile
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import (
    clubelo,
    fdhist,
    guardian,
    oddsapi,
    openfootball,
    sporttery,
    srcb,
    srct,
    srct_night,
    srct_remat,
    srct_results,
    srct_shift,
    understat,
    uniform,
    zucai_official,
)
from goalx_backend.data.ingest.shell import browser_ssl_context, polite_client
from goalx_backend.data.quote_evidence import (
    adjudicate_had_quote,
    sharp_probs_asof,
)
from goalx_backend.db import connect, migrate, utc_now_iso
from goalx_backend.evaluation import clv as clv_mod
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
from goalx_backend.modelling.forecast import generate_forecasts
from goalx_backend.modelling.team_align import (
    alias_index,
    clubelo_alias_for_team,
    record_srct_aliases,
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
    return {
        "generated": stats.generated,
        "duplicates": stats.duplicates,
        "xg_blended": stats.xg_blended,
        "xg_unresolved": stats.xg_unresolved,
        "skipped": len(stats.skipped),
    }


def odds_anchor_dense() -> dict[str, object]:
    """
    双锚临场采样（票 47 修正设计，2026-09-21 用户裁决锚=竞彩停售）。

    评估锚：开球前 5 分钟桶内的已 join 场次定向拉取（meta anchor=kickoff，
    CLV 收盘基准收紧）；决策锚：停售探测检出 on_sale→stopped 迁移后立即
    拉取（meta anchor=sale_stop，决策时点公允线，票 48 陈盘信号的数据
    供给）。两锚零候选零请求；失败不重试（30 分钟 closing 循环兜底）。
    """
    settings = get_settings()
    with task_conn() as conn, polite_client() as client:
        now = datetime.now(UTC)
        # 探测先行（零 credit）：kickoff 锚腿异常不阻断停售检测与状态时间线
        probe = sporttery.probe_sale_stops(
            conn, settings, client, raw_root=settings.observations_dir
        )
        bucket_start, bucket_end = oddsapi.kickoff_bucket_bounds(now)
        kickoff_targets = [
            (str(row["odds_api_event_id"]), str(row["odds_api_sport_key"]))
            for row in fx_store.joined_fixtures_in_window(
                conn, bucket_start, bucket_end
            )
        ]
        stop_targets = [
            (str(row["odds_api_event_id"]), str(row["odds_api_sport_key"]))
            for row in probe.stopped
        ]
        kickoff = _isolated_anchor_pull(
            conn,
            settings,
            client,
            anchor=oddsapi.ANCHOR_KICKOFF,
            targets=kickoff_targets,
        )
        sale_stop = _isolated_anchor_pull(
            conn,
            settings,
            client,
            anchor=oddsapi.ANCHOR_SALE_STOP,
            targets=stop_targets,
        )
    return {
        "kickoff_anchor": {
            "events": kickoff.events,
            "credits": kickoff.credits_used,
        },
        "sale_stop_probe": {
            "candidates": probe.candidates,
            "stopped": len(probe.stopped),
        },
        "sale_stop_anchor": {
            "events": sale_stop.events,
            "credits": sale_stop.credits_used,
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
    """
    resolved = settings if settings is not None else get_settings()
    store = CorpusStore(resolved.corpus_root)
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
            )
    finally:
        store.close()
    payload = srct_shift.stats_dict(stats)
    # 拍后收尾（odds-read-switch 票 01/03）：先映射同步（读侧短连接），
    # 再增量物化（写侧持锁）——顺序不可反，remat 持写锁时映射 reader
    # 会降级空转。各自降级不拖垮拍班本体（同 wrap 流降级哲学）
    try:
        payload["mapping_sync"] = mapping_sync()
    except (sqlite3.Error, duckdb.Error, OSError) as exc:
        logger.warning("post-beat mapping sync degraded: {}", exc)
        payload["mapping_sync"] = {"degraded": str(exc)}
    try:
        payload["remat"] = srct_remat_run()
    except (sqlite3.Error, duckdb.Error, OSError) as exc:
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
    return {
        "links": links.as_dict() if links else None,
        "srct": srct_stats.as_dict(),
        "uniform": asdict(stats),
        "uniform_reconcile": reconcile.stats_dict(rec),
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
    """
    with task_conn() as conn, corpus_anchor() as duck_con:
        stats = mapping.sync_fixture_links(
            conn,
            duck_con,
            team_aliases=team_align_srct_sets(conn),
            live_rows=_live_mapping_rows(),
        )
        aliases = record_srct_aliases(conn, stats.srct_alias_pairs)
        clubelo_report = sync_clubelo_aliases(conn, clubelo.distinct_clubs(conn))
    return {
        **stats.as_dict(),
        "srct_aliases_added": aliases,
        "clubelo": clubelo_report.as_dict(),
    }


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
    verdict = adjudicate_had_quote(conn, fid, decision_iso)
    sharp = sharp_probs_asof(conn, fid, decision_iso)
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

    窗口=拍点已过且在 6h 迟到视界内（kickoff ∈ [now+offset−6h, now+offset]）；
    锚书=拍点定向拉取（复用 odds_anchor_dense 通道，零候选零请求）；
    竞彩=拍点时点最新 as-of 读（调盘面，龄如实记）；停售边界
    purchasable=拍点时点（target_instant）停售状态；迟到补拍 late_flag。
    now/pull/decision_at 注入点供测试（缺省=当前时刻/真锚拉取/拉取后取
    当前时刻）——判定 as-of 用 decision_at（拉取后），任务时刻只管窗口
    （measure-04：本拍新抓报价不再被任务时刻滤掉）。
    """
    settings = get_settings()
    anchor_pull = pull or _isolated_anchor_pull
    out: dict[str, object] = {}
    with task_conn() as conn, polite_client() as client:
        moment = now or datetime.now(UTC)
        for offset_h, label in TRACK2_OFFSETS:
            offset = timedelta(hours=offset_h)
            horizon = timedelta(hours=TRACK2_CATCHUP_HOURS)
            start = (moment + offset - horizon).isoformat(timespec="seconds")
            end = (moment + offset).isoformat(timespec="seconds")
            targets = fx_store.joined_fixtures_in_window(conn, start, end)
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
                if int(row["id"]) in fresh
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
                        conn, row, offset=offset, snap_type=label, decision=decision
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
    with task_conn() as conn:
        report = record_funnel_day(conn).as_dict()
        bdate = str(report["business_date"])
        report["frozen"] = frozen_mod.record_frozen_decisions(
            conn, business_date=bdate
        ).as_dict()
        report["liveness"] = funnel_liveness(conn)
    return report


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
    """M3 评测协议（票 13）：三列报告 + 赛后复核入队 + 控制事件。"""
    with task_conn() as conn:
        report = m3_protocol_report(conn)
        review = enqueue_post_settle(conn)
        record_control_events(conn, get_settings())
    return {
        "tier_a": report.tier_a.verdict,
        "tier_b": report.tier_b.verdict,
        "paired": report.paired_fused_vs_ml.pairs,
        "post_settle_enqueued": review.enqueued,
    }
