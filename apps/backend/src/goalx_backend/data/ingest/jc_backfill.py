"""
JC 历史回填编排（票 67）：uniform 按日反查 matchId → 票 70 采集器逐场。

分工（2026-09-26 to-tickets 对账）：采集器=票 70 交付，本模块零解析代码
——只做枚举/排期/预算：

- **枚举**：uniform 赛果 ``fetch_uniform_results``（生产已有）按日反查
  该日全部 matchId（JC 覆盖与赛事范围无关——非 JC 场 fixedBonus 返回
  ``oddsHistory={}`` 合法空，raw 落盘防重查）；
- **排期**：日清单新→旧（新数据 TTG 轨迹密度高、近因价值大）；一日
  done 判据=该日全部 mid 裸键 raw 在（次夜零成本跳过）；
- **预算**：夜班殿后相位（源T 补欠优先）——run_night 源T 清单跑完后以
  剩余预算推进（十年 ≈3-4 万场 ≈5 夜；20/min 滑窗沿用）；
- **audit/对账**在 jc_audit.py（本模块只回填）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import jc, srct, uniform

BACKFILL_SLEEP_SECONDS = 3.2  # 20/min 滑窗口径的请求间距（官方域沿用同护栏）


@dataclass
class JcBackfillStats:
    """一次回填推进的统计。"""

    date_from: str = ""
    date_to: str = ""
    days_attempted: int = 0
    days_done: int = 0  # 该日全部 mid 裸键已齐（零请求跳过计入）
    mids_seen: int = 0
    matches_collected: int = 0  # bronze 非空场
    empty: int = 0  # oddsHistory={} 合法空
    stopped: str | None = None  # budget；None=区间跑完
    failed_days: dict[str, str] = field(default_factory=dict)


def enumerate_day_mids(client: httpx.Client, settings: Settings, day: str) -> list[str]:
    """一日 uniform 赛果 → matchId 清单（升序去重）。"""
    rows = uniform.fetch_uniform_results(client, settings, day, day)
    return sorted({str(row["matchId"]) for row in rows if row.get("matchId")})


def day_missing_mids(
    store: CorpusStore, client: httpx.Client, settings: Settings, day: str
) -> tuple[list[str], int]:
    """
    一日待采 mid 清单（裸键缺的）＋该日总数。

    返回 (missing, total)；total 用于 done 判据记账。
    """
    mids = enumerate_day_mids(client, settings, day)
    missing = [m for m in mids if not store.has(jc.JC_PROVIDER, jc.SP_DATASET, m)]
    return missing, len(mids)


def backfill_range(  # noqa: PLR0913 接缝参数随防封/预算/抽样累加
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    *,
    date_to: str,
    date_from: str,
    budget: srct.NightBudget,
    sleeper: Callable[[float], None] = time.sleep,
    sleep_seconds: float = BACKFILL_SLEEP_SECONDS,
    day_cap: int | None = None,
) -> JcBackfillStats:
    """
    区间回填（新→旧逐日；预算触顶记 stopped 正常返回，进度已落库）。

    day_cap：单日采集 mid 上限（抽样冒烟用；缺省全量）。
    """
    stats = JcBackfillStats(date_from=date_from, date_to=date_to)
    start = date.fromisoformat(date_to)
    end = date.fromisoformat(date_from)
    current = start
    while current >= end:
        day = current.isoformat()
        stats.days_attempted += 1
        try:
            budget.charge(1)  # 枚举（uniform 按日）也是线上请求
            missing, total = day_missing_mids(store, client, settings, day)
        except srct.NightStop as stop:
            stats.stopped = stop.reason
            return stats
        except (httpx.HTTPError, RuntimeError) as exc:
            stats.failed_days[day] = f"{type(exc).__name__}: {exc}"[:120]
            logger.warning("jc backfill {} 枚举失败（{}）", day, exc)
            current -= timedelta(days=1)
            continue
        stats.mids_seen += total
        if not missing:
            stats.days_done += 1
            current -= timedelta(days=1)
            continue
        if day_cap is not None:
            missing = missing[:day_cap]
        for mid in missing:
            before = jc.JcCollectStats()
            try:
                jc.collect_match(store, settings, client, mid, stats=before)
            except srct.NightStop as stop:
                stats.stopped = stop.reason
                return stats
            budget.charge(before.requests)
            stats.matches_collected += before.bronze_new
            stats.empty += before.empty
            if before.failed:
                logger.warning("jc backfill {}:{} 失败 {}", day, mid, before.failed)
            if sleep_seconds:
                sleeper(sleep_seconds)
        current -= timedelta(days=1)
    return stats


def night_backfill_phase(
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    budget: srct.NightBudget,
    *,
    today: date,
    years: float = 10.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> JcBackfillStats:
    """
    夜班殿后相位（票 67 排期裁决）：源T 清单跑完后以剩余预算推进。

    区间=今日-3 回溯 10 年（新→旧）；预算尽即停（次日续——done 日入
    `jc_backfill_days` 日账零成本跳过）。十年一轮后每夜真零请求心跳。
    """
    date_to = (today - timedelta(days=3)).isoformat()
    try:
        oldest = today.replace(year=today.year - int(years))
    except ValueError:
        oldest = today.replace(year=today.year - int(years), day=28)  # 闰日守卫
    date_from = oldest.isoformat()
    return backfill_range(
        store,
        settings,
        client,
        date_to=date_to,
        date_from=date_from,
        budget=budget,
        sleeper=sleeper,
    )
