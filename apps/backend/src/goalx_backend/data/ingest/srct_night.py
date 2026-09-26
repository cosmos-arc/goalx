"""
源T 夜班调度与请求预算（票 55 切片 13）：Phase1 回填批自动推进。

编排层（采集循环本体在 srct.collect_day）：按季生成日页任务清单 →
夜班窗口（01:00-08:00 家宽，本机墙钟 Asia/Shanghai）内逐日推进 →
~8K 请求/日预算触顶即停 / 连败 5 场熔断当晚收手留痕 → 每夜摘要落
checkpoint 库（srct_night_summaries，`goalx srct-night --list` 晨检）。

季窗 8 月 1 日-次年 7 月 31 日连续 12 个月（非 8-5 月）：CorpusScope 含
挪超/瑞超夏季历，日期集必须全年连续，季标签只作批次记账。采集顺序 =
最新季最新日倒序——趁源端页面还在热区先固化，越老越后。当季上界 =
前天（today-2）：Over 页只取完场行，凌晨收尾场次存在滞后，隔一日采集
零在跑风险。

断点续传语义：done 由本层报（srct_day_status），日页 raw 在而状态缺 =
中断日，次夜仍 pending，collect_day 按 checkpoint 只补缺口零重抓。日页
伪 200（404 内容）记 not_found 留痕，不再夜夜重试——但伪 200 与传输失败
同样计入连败（源端软封锁的最常见形态不许绕开熔断）。场级失败（传输类）
不拦 done——自动前进 + 每夜 failed 摘要可见，单场缺口走手动
`srct-collect --date` 低成本回补（成功件已在 checkpoint，只补失败端点）；
熔断只对系统性故障（连续 5 次失败事件）生效。

老季深度分层（票 18，Phase2/3 扩展批）：起始年 ≤2019 的季窗默认浅深
（每场只打 日页+1x2 轨迹 两请求，跳过深端点——老场深页大概率空，
空页请求纯浪费）；每季首个 pending 日全深探针，统计/亚盘多庄页非空即
升全深（宁可错升不错漏）。判定落 checkpoint（srct_season_depth，跨夜
不重探）；升深后浅深期 done 日自动重开端点集（day/odds 缓存命中，只补
深端点；端点集注册表驱动，票 59 起）。

挂接：Prefect srct-night deployment 每日 01:00（schedules.py）；窗口逐日
复判（越 08:00 当场收手），手工白天冒烟走 `--no-window`。
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import cast

import httpx
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct
from goalx_backend.rate_limit import default_limiter

# 夜班窗口（spec story 3）：家宽低峰 01:00-08:00（本机墙钟，不跨午夜）
NIGHT_WINDOW: tuple[dt_time, dt_time] = (dt_time(1, 0), dt_time(8, 0))
# 当季采集上界回看天数：隔一日再采，Over 页完场行稳态
CURRENT_SEASON_LAG_DAYS = 2
# 摘要 failed 采样上限（全量进日志，行内截断防爆；tasks/flow 返回同口径）
FAILED_SAMPLE_CAP = 20
# 票 18 老季分层：起始年 ≤2019 的季窗默认浅深（两请求/场），探针可升
# 全深；Phase1 窗口（2023/24 起）不落此界，不受影响
LAYERED_SEASON_MAX_START_YEAR = 2019


@dataclass(frozen=True)
class SeasonWindow:
    """一个回填季：8 月 1 日起的连续 12 个月；end=None = 当季开区间。"""

    label: str
    start: str
    end: str | None


# Phase1（ADR-0011 决策 5）：三完整季 + 当季 ≈15.5K 场 ≈47K 请求 ≈6 夜班。
# 顺序即处理顺序（最新季优先）；2020 前两请求分层不在本批。
PHASE1_SEASONS: tuple[SeasonWindow, ...] = (
    SeasonWindow("2026/27", "2026-08-01", None),
    SeasonWindow("2025/26", "2025-08-01", "2026-07-31"),
    SeasonWindow("2024/25", "2024-08-01", "2025-07-31"),
    SeasonWindow("2023/24", "2023-08-01", "2024-07-31"),
)


def season_dates(season: SeasonWindow, today: date) -> list[str]:
    """一季日期串（升序）；当季截到 today-2（完场稳态）。"""
    start = date.fromisoformat(season.start)
    end = (
        date.fromisoformat(season.end)
        if season.end is not None
        else today - timedelta(days=CURRENT_SEASON_LAG_DAYS)
    )
    return [
        (start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)
    ]


def phase1_dates(
    today: date, seasons: tuple[SeasonWindow, ...] = PHASE1_SEASONS
) -> list[tuple[str, str]]:
    """(季标签, 日期) 任务清单：最新季优先，季内最新日倒序。"""
    tasks: list[tuple[str, str]] = []
    for season in seasons:  # seasons 本身已按最新在前排列
        tasks.extend((season.label, d) for d in reversed(season_dates(season, today)))
    return tasks


def pending_dates(
    store: CorpusStore,
    today: date,
    seasons: tuple[SeasonWindow, ...] = PHASE1_SEASONS,
) -> list[tuple[str, str]]:
    """未完成日（夜班待办）：done/not_found 之外全是。"""
    settled = store.day_status_dates("done") | store.day_status_dates("not_found")
    return [(s, d) for s, d in phase1_dates(today, seasons) if d not in settled]


def _is_layered(season: SeasonWindow) -> bool:
    """该季窗是否落老季分层界（票 18：起始年 ≤2019）。"""
    return int(season.start[:4]) <= LAYERED_SEASON_MAX_START_YEAR


def _probe_verdict(stats: srct.SrctCollectStats) -> str | None:
    """
    探针日判定（票 18）：full / shallow / None（不断案）。

    宁可错升不错漏（漏=数据永久缺口，升=多花 1/3 请求）：正证据即升全深；
    零证据须当日干净跑完且有场次才降浅深——中断/任一失败/解析失败/零场
    都留待次夜下一 pending 日重探（不重探指已断案的季，见 srct_season_depth）。
    """
    if (
        stats.stats_nonempty
        or stats.asian_odds_nonempty
        or stats.over_down_nonempty
        or stats.detail_nonempty
        or stats.analysis_nonempty
    ):
        return srct.DEPTH_FULL
    if stats.stopped is not None or stats.failed or stats.parse_failed:
        return None
    if not stats.scope_sids:
        return None
    return srct.DEPTH_SHALLOW


def _resolve_depth(
    season: str,
    windows: dict[str, SeasonWindow],
    depths: dict[str, str],
) -> tuple[str, bool]:
    """该日采集深度：返回 (depth, 是否探针日)。老季无判定=全深探针。"""
    window = windows.get(season)
    if window is None or not _is_layered(window):
        return srct.DEPTH_FULL, False  # Phase1 季窗：不分层
    known = depths.get(season)
    if known is None:
        return srct.DEPTH_FULL, True
    return known, False


def _record_probe(
    store: CorpusStore,
    depths: dict[str, str],
    season: str,
    day: str,
    stats: srct.SrctCollectStats,
) -> None:
    """探针日收尾断案并持久（None=不断案，次夜下一 pending 日重探）。"""
    verdict = _probe_verdict(stats)
    if verdict is None:
        return
    depths[season] = verdict
    store.set_season_depth(season, verdict)
    logger.info("srct night {}: 老季深度判定 {}（探针日 {}）", season, verdict, day)


def _depth_backfill_dates(
    store: CorpusStore,
    today: date,
    seasons: tuple[SeasonWindow, ...],
    depths: dict[str, str],
) -> list[tuple[str, str]]:
    """
    补抓清单（票 18 升深 + 票 63 规格扩展泛化）。

    判据=done 日 CorpusScope 场次的任一当前深端点 raw 缺席（跳过端点不记
    checkpoint；传输失败同形态——重试无害且自愈，预算/熔断照护栏计）。
    重跑 day/odds/已采端点命中缓存零重抓，只补缺的端点。适用面：

    - Phase1（不分层季）：规格 v2 新四端点（票 59-62）落地前 done 的存量日
      自动补采新端点（~492 日 ≈ 21.6K 请求 ≈ 2.7 夜，票 63 测算）；
    - 老季分层：浅深判定季不重开（深端点已判空，重开纯浪费请求），
      全深季照常补。

    顺序=补欠在前、pending 殿后（票 63 裁决：存量日深页有源端老化风险，
    ~2.7 夜补完再续推进新日期；季窗内均为最新日倒序）。
    """
    candidates = [
        season
        for season in seasons
        if not (_is_layered(season) and depths.get(season.label) != srct.DEPTH_FULL)
    ]
    if not candidates:
        return []
    day_sids: dict[str, list[str]] = {}
    for line in store.iter_bronze_lines(srct.SRCT_PROVIDER, srct.DAY_DATASET):
        row = json.loads(line)
        payload = cast("dict[str, object] | None", row.get("payload"))
        if payload is not None:
            day_sids[str(row["sid"])] = srct.day_page_sids(payload)
    done = store.day_status_dates("done")
    deep_datasets = srct.deep_endpoint_datasets()  # 注册表驱动（票 59 起）
    backfill: list[tuple[str, str]] = []
    for season in candidates:
        for day in reversed(season_dates(season, today)):
            if day not in done:
                continue
            if any(
                not store.has(srct.SRCT_PROVIDER, dataset, sid)
                for sid in day_sids.get(day, [])
                for dataset in deep_datasets
            ):
                backfill.append((season.label, day))
    return backfill


@dataclass
class SrctNightSummary:
    """一夜运行摘要（持久化行 + CLI/flow 返回口径）。"""

    night_date: str
    started_at: str
    ended_at: str = ""
    stop_reason: str = "completed"  # completed/budget/circuit/window_closed
    dates_attempted: int = 0
    dates_done: int = 0
    dates_not_found: int = 0
    pending_before: int = 0
    pending_after: int = 0
    requests: int = 0
    raw_new: int = 0
    parsed_ok: int = 0  # 吸收进 bronze 的行数
    bronze_repaired: int = 0
    xg_matches: int = 0
    parse_failed_count: int = 0
    failed_count: int = 0
    budget_cap: int = srct.NIGHT_REQUEST_CAP
    failed: dict[str, str] = field(default_factory=dict)

    def to_row(self) -> dict[str, object]:
        """持久化行（failed 采样截断；字段序 = checkpoint 表列）。"""
        row: dict[str, object] = asdict(self)
        row["failed_json"] = json.dumps(
            dict(list(self.failed.items())[:FAILED_SAMPLE_CAP]),
            ensure_ascii=False,
        )
        del row["failed"]
        return row


def _in_window(now: datetime, window: tuple[dt_time, dt_time]) -> bool:
    """窗口内判断（start ≤ t < end；01:00-08:00 不跨午夜）。"""
    return window[0] <= now.time() < window[1]


def _absorb(summary: SrctNightSummary, stats: srct.SrctCollectStats) -> None:
    """一日统计并进夜摘要（requests 除外——权威口径是预算计费，见 run_night）。"""
    summary.raw_new += stats.raw_new
    summary.parsed_ok += stats.parsed_ok
    summary.bronze_repaired += stats.bronze_repaired
    summary.xg_matches += stats.xg_matches
    summary.parse_failed_count += len(stats.parse_failed)
    summary.failed.update(stats.failed)


def run_night(  # noqa: PLR0912, PLR0913, PLR0915, C901 接缝与逐日编排分支随护栏/分层累加
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    *,
    today: date | None = None,
    now_fn: Callable[[], datetime] | None = None,
    window: tuple[dt_time, dt_time] | None = NIGHT_WINDOW,
    request_cap: int = srct.NIGHT_REQUEST_CAP,
    failure_streak_cap: int = srct.FAILURE_STREAK_CAP,
    seasons: tuple[SeasonWindow, ...] | None = None,
    jitter: tuple[float, float] | None = srct.JITTER_RANGE,
    sleeper: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
    jc_phase: bool = True,
) -> SrctNightSummary:
    """
    推进一夜 Phase1：pending 清单逐日 collect_day 至预算/熔断/清单尽。

    窗口外直接返回 window_closed（不落摘要行——没干活不留痕）；
    干过活的夜（含零 pending 心跳、预算/熔断停机）都落一行摘要。
    """
    clock = now_fn if now_fn is not None else datetime.now
    sleep_fn = sleeper if sleeper is not None else time.sleep
    scoped_seasons = PHASE1_SEASONS if seasons is None else seasons
    started = clock()
    summary = SrctNightSummary(
        night_date=started.date().isoformat(),
        started_at=started.isoformat(timespec="seconds"),
        budget_cap=request_cap,
    )
    if window is not None and not _in_window(started, window):
        summary.stop_reason = "window_closed"
        return summary
    run_today = today if today is not None else started.date()
    tasks = pending_dates(store, run_today, scoped_seasons)
    summary.pending_before = len(tasks)
    depths = store.season_depths()
    # 补抓（票 18 升深 + 票 63 规格扩展）：done 日缺当前深端点 raw 即重开
    # （缓存命中零重抓，只补缺端点）；浅深判定老季不重开。补欠在前、
    # pending 殿后（存量日深页老化风险 > 新日页下线风险）。
    tasks = _depth_backfill_dates(store, run_today, scoped_seasons, depths) + tasks
    windows = {season.label: season for season in scoped_seasons}
    budget = srct.NightBudget(
        request_cap=request_cap, failure_streak_cap=failure_streak_cap
    )
    limiter = default_limiter()  # 跨日期共享滑窗（20/min 硬顶连续生效）
    stopped = False
    for season, day in tasks:
        # 逐日复判窗口：长夜（退避封顶 60s ×N）越 08:00 当场收手
        if window is not None and not _in_window(clock(), window):
            summary.stop_reason = "window_closed"
            stopped = True
            break
        depth, probing = _resolve_depth(season, windows, depths)
        summary.dates_attempted += 1
        try:
            stats = srct.collect_day(
                store,
                settings,
                client,
                date=day,
                jitter=jitter,
                sleeper=sleep_fn,
                rng=rng,
                rate_limiter=limiter,
                budget=budget,
                depth=depth,
            )
        except srct.SrctContentError as exc:
            store.set_day_status(day, "not_found")
            summary.dates_not_found += 1
            logger.warning("srct night {}: 日页伪 200，记 not_found（{}）", day, exc)
            try:
                budget.note(True)  # 伪 200 = 失败事件（软封锁形态不许绕开熔断）
            except srct.NightStop as stop:
                summary.stop_reason = stop.reason
                stopped = True
                break
            continue
        except httpx.HTTPError as exc:
            summary.failed[f"{day}:day_page"] = str(exc)[:120]
            try:
                budget.note(True)  # 日页级失败同样计入连败
            except srct.NightStop as stop:
                summary.stop_reason = stop.reason
                stopped = True
                break  # 熔断也落摘要留痕
            continue
        _absorb(summary, stats)
        if probing:
            _record_probe(store, depths, season, day, stats)
        if stats.stopped is not None:
            summary.stop_reason = stats.stopped
            stopped = True
            break
        store.set_day_status(day, "done")
        summary.dates_done += 1
        logger.info(
            "srct night {} {}: +{} raw / {} req（{} 剩余预算）",
            season,
            day,
            stats.raw_new,
            stats.requests,
            request_cap - budget.requests,
        )
    if not stopped:
        summary.stop_reason = "completed"
    # JC 历史回填殿后相位（票 67）：源T 补欠优先，剩余预算推进十年回填
    # （官方域；预算尽/熔断已停则相位自然跳过——只在源T 干净跑完时挂）
    if jc_phase and summary.stop_reason == "completed":
        from goalx_backend.data.ingest import jc_backfill  # noqa: PLC0415 防环局部导入

        try:
            jc_stats = jc_backfill.night_backfill_phase(
                store, settings, client, budget, today=run_today, sleeper=sleep_fn
            )
        except srct.NightStop as stop:  # 预算尽=相位正常停（摘要照落，进度在库）
            jc_stats = jc_backfill.JcBackfillStats(stopped=stop.reason)
        if jc_stats.stopped is not None:
            summary.stop_reason = f"jc_{jc_stats.stopped}"
        logger.info(
            "srct night {}: jc 回填殿后——{} 日尝试/{} 场采/空 {}",
            summary.night_date,
            jc_stats.days_attempted,
            jc_stats.matches_collected,
            jc_stats.empty,
        )
    # requests 权威口径 = 预算计费（含重试/失败路径/触顶未发的那一次）——
    # stats.requests 在异常中断路径会丢已发请求的计数
    summary.requests = budget.requests
    summary.pending_after = len(pending_dates(store, run_today, scoped_seasons))
    summary.failed_count = len(summary.failed)
    summary.ended_at = clock().isoformat(timespec="seconds")
    store.record_night_summary(summary.to_row())
    logger.info(
        "srct night {}: {}，dates {}/{}，req {}/{}，pending {}→{}",
        summary.night_date,
        summary.stop_reason,
        summary.dates_done,
        summary.dates_attempted,
        summary.requests,
        summary.budget_cap,
        summary.pending_before,
        summary.pending_after,
    )
    return summary
