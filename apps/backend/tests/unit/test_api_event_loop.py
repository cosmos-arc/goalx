"""票 07①：同步 I/O 端点不占事件循环——并发探针下健康端点时延有界。

today/research/pool-sync 曾是 ``async def`` 里做 duckdb/sqlite/外网同步
I/O，慢查询期间事件循环被头部请求独占。改为普通 ``def``（线程池）后，
同事件循环内并发请求健康端点的调度→响应时延必须有界。若回归为
``async def``，探针的 ``asyncio.sleep`` 本身被卡住，时延 ≥ 慢查询时长。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from goalx_backend.api import fixtures as fixtures_api
from goalx_backend.config import Settings
from goalx_backend.main import create_app

SLOW_SECONDS = 0.8  # 慢查询模拟时长；async 阻塞下探针总时延 ≥ 该值
PROBE_BOUND_SECONDS = 0.4  # 线程池分派下的上界（含 0.1s 让位睡眠）


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _app(tmp_path: Path) -> FastAPI:
    from goalx_backend.db import connect, migrate

    db_path = tmp_path / "ops.db"
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    return create_app(settings=Settings(db_path=db_path))


async def _run(app: FastAPI, path: str, *, post: bool = False) -> float:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:

        async def slow() -> None:
            response = await (ac.post(path) if post else ac.get(path))
            response.raise_for_status()

        async def probe() -> float:
            t0 = time.monotonic()
            await asyncio.sleep(0.1)  # 让慢请求先分派（旧实现下此处即被卡）
            response = await ac.get("/healthz")
            assert response.status_code == 200
            return time.monotonic() - t0

        _, latency = await asyncio.gather(slow(), probe())
    return latency


@pytest.mark.anyio
async def test_today_page_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """今日页慢查询（duckdb+sqlite）期间健康端点不被头部阻塞。"""

    def slow_view(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        time.sleep(SLOW_SECONDS)
        return []

    monkeypatch.setattr(fixtures_api, "build_today_view", slow_view)
    monkeypatch.setattr(
        fixtures_api.bucket_face, "attach_review_faces", lambda *a, **k: None
    )
    monkeypatch.setattr(
        fixtures_api.bucket_face, "attach_live_faces", lambda *a, **k: None
    )
    monkeypatch.setattr(
        fixtures_api.corpus_duckdb, "reader", lambda: contextlib.nullcontext(None)
    )
    latency = await _run(_app(tmp_path), "/api/v1/fixtures/today")
    assert latency < PROBE_BOUND_SECONDS, (
        f"healthz 调度→响应 {latency:.2f}s ≥ {PROBE_BOUND_SECONDS}s："
        "今日页同步 I/O 回归到了事件循环内"
    )


@pytest.mark.anyio
async def test_pool_sync_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """彩池同步（外网+库写）期间健康端点不被头部阻塞。"""
    from goalx_backend import tasks as tasks_mod

    def slow_sync() -> dict[str, object]:
        time.sleep(SLOW_SECONDS)
        return {}

    monkeypatch.setattr(tasks_mod, "pool_snapshot", slow_sync)
    latency = await _run(_app(tmp_path), "/api/v1/pool-sync/run", post=True)
    assert latency < PROBE_BOUND_SECONDS, (
        f"healthz 调度→响应 {latency:.2f}s ≥ {PROBE_BOUND_SECONDS}s："
        "彩池同步同步 I/O 回归到了事件循环内"
    )


def test_slow_endpoints_are_threadpool_routed() -> None:
    """机械面钉死：三端点必须是普通 def（async def 即回归票 07①）。"""
    import inspect

    from goalx_backend.api import pool as pool_api

    for module, name in (
        (fixtures_api, "get_today_fixtures"),
        (fixtures_api, "get_fixture_research"),
        (pool_api, "run_pool_sync"),
    ):
        assert not inspect.iscoroutinefunction(getattr(module, name)), (
            f"{name} 是 async def：同步 I/O 会占事件循环（票 07①）"
        )
