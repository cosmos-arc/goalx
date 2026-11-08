#!/usr/bin/env python3
"""票 06 前置：十年 sfc 官方彩果回填驱动（2026-11-08 批次 α）。

`goalx-backend pool-backfill` 只会从当期号直线下倒查，跨年号段
（26001→26000→…→25001）会烧上千个无效请求。本驱动按年分段：
start=YY180（该年最大期号之上），periods=190 覆盖 YY001..YY180——
每年仅 ~25 个无效号段请求。幂等（已有官方 prize_tiers 的期跳过），
断点重跑零损耗。只回填 sfc（14场）——V2 任9 面历史无奖级，H7 十年
主指标只吃 sfc；goals4 无滚存不相关。

用法：uv run python .scratch/autoresearch-20261107/pool_backfill_tenyear.py
"""

from __future__ import annotations

import httpx
from loguru import logger

from goalx_backend.config import get_settings
from goalx_backend.data.ingest import zucai_official
from goalx_backend.data.ingest.shell import browser_ssl_context
from goalx_backend.db import connect, migrate

YEARS = range(26, 16, -1)  # 2026 → 2017（十年，含 17）


def main() -> None:
    settings = get_settings()
    conn = connect()
    try:
        migrate(conn)
        with httpx.Client(verify=browser_ssl_context()) as client:
            for yy in YEARS:
                start = f"{yy}180"
                logger.info("回填年段 {}：start={} periods=190", yy, start)
                stats = zucai_official.backfill_draws(
                    conn,
                    settings,
                    client,
                    periods=190,
                    start=start,
                    games=(zucai_official.GAME_SFC,),
                )
                logger.info(
                    "年段 {} 完成：pages={} periods={} empty={}",
                    yy,
                    stats.pages,
                    len(stats.period_nos),
                    stats.empty_periods,
                )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
