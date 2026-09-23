"""
统一限流闸门（用户裁定 HTTP 套件之 limits 件，2026-09-23）。

限流一律时间滑动窗口（``MovingWindowRateLimiter`` + ``MemoryStorage``，
进程内、无外部存储依赖）。跨域共享：data/ingest 与 llm 采集器同用。

语义约定：固定礼貌间距 sleep（0.3/0.5s）是**间距参数**，窗口只加顶不改
间距——把均匀间距换成可突发的滑窗对 WAF 敏感源是行为退化（票 57 不变量）。
每次线上尝试前过闸；满窗时睡到窗口滑出再打。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from limits import RateLimitItem
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter


def default_limiter() -> MovingWindowRateLimiter:
    """新建滑动窗口限流器（每次采集调用一个，随调用生命周期）。"""
    return MovingWindowRateLimiter(MemoryStorage())


def throttle(
    limiter: MovingWindowRateLimiter,
    item: RateLimitItem,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """限流闸门：满窗时睡到窗口滑出再打（sleeper 注入口留给测试）。"""
    while not limiter.hit(item):
        wait = float(limiter.get_window_stats(item).reset_time) - time.time()
        sleeper(max(wait, 0.05))
