"""统一限流闸门测试（票 57）：limits 滑动窗口组件语义（共享 rate_limit 模块）。"""

from __future__ import annotations

import time

from limits import RateLimitItemPerSecond

from goalx_backend.rate_limit import default_limiter, throttle


def test_throttle_empty_window_passes_without_wait() -> None:
    waits: list[float] = []
    throttle(default_limiter(), RateLimitItemPerSecond(1), waits.append)
    assert waits == []  # 空窗直过，零等待


def test_throttle_full_window_waits_for_slide() -> None:
    """满窗后按 reset 时间等到窗口滑出才放行（MemoryStorage 无外部存储）。"""
    limiter = default_limiter()
    item = RateLimitItemPerSecond(1)  # 1s 窗，组件最小粒度
    waits: list[float] = []

    def sleep_and_record(seconds: float) -> None:
        waits.append(seconds)
        time.sleep(seconds)

    throttle(limiter, item, lambda _s: None)  # 占满窗
    throttle(limiter, item, sleep_and_record)
    assert 0 < waits[0] <= 1.05
