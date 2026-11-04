"""GoalX FastAPI application factory and system routes."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from loguru import logger
from pydantic import BaseModel

from goalx_backend.api import bets as bets_api
from goalx_backend.api import evidence as evidence_api
from goalx_backend.api import fixtures as fixtures_api
from goalx_backend.api import markets as markets_api
from goalx_backend.api import pool as pool_api
from goalx_backend.api import results as results_api
from goalx_backend.api import validation as validation_api
from goalx_backend.config import Environment, Settings, get_settings
from goalx_backend.db import connect as db_connect
from goalx_backend.db import current_version as db_current_version
from goalx_backend.migrations import MIGRATIONS
from goalx_backend.run_fingerprint import process_face

OPENAPI_DESCRIPTION = (
    "GoalX backend API. The contract under `contracts/openapi/v1.json` is the "
    "reviewed source of truth; this application must serve exactly that document."
)


class StatusResponse(BaseModel):
    """Machine-readable service identity."""

    app_name: str
    app_version: str
    environment: Environment


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the GoalX ASGI application."""
    resolved = settings if settings is not None else get_settings()
    # 运行指纹启动时算一次（票 06）：git_sha/dirty/config_hash/schema。
    # git 子进程只在启动跑一次;schema 探测走只读连接,库未迁移/缺席=
    # unknown——healthz 探针路径零子进程零库访问。
    try:
        fp_conn = db_connect(resolved.db_path, readonly=True)
        try:
            runtime_face = process_face(resolved, conn=fp_conn)
        finally:
            fp_conn.close()
    except sqlite3.Error:
        runtime_face = process_face(resolved)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        """Log process start and stop for local observability."""
        logger.info(
            "{} v{} starting ({})",
            resolved.app_name,
            resolved.app_version,
            resolved.environment,
        )
        yield
        logger.info("{} stopping", resolved.app_name)

    app = FastAPI(
        title=resolved.app_name,
        version=resolved.app_version,
        description=OPENAPI_DESCRIPTION,
        lifespan=lifespan,
        servers=[{"url": "http://127.0.0.1:8000", "description": "Local development"}],
        license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
        openapi_tags=[
            {
                "name": "system",
                "description": "Health, readiness and service identity.",
            },
            {"name": "fixtures", "description": "竞彩场次对照与赔率时序(今日页)。"},
            {
                "name": "markets",
                "description": "玩法轴读模型(进球类 ttg/crs 报价与矩阵推导概率/EV)。",
            },
            {"name": "bets", "description": "注级建议、票级回录与复盘列表。"},
            {
                "name": "pool",
                "description": "传统足彩彩池期次/对阵/分布与代采入口(14场任9)。",
            },
            {"name": "results", "description": "开奖导入、结算批跑与资金池。"},
            {
                "name": "validation",
                "description": "回测 run/指标与纸面三条件进度(验证页)。",
            },
            {
                "name": "evidence",
                "description": "LLM 线证据面：证据卡/证据链/复核队列/盲评(M3, 票 14)。",
            },
        ],
    )
    app.state.settings = resolved
    app.state.runtime_face = runtime_face

    @app.get("/healthz", tags=["system"], summary="Liveness probe")
    async def health_check() -> dict[str, str]:
        """Report whether the process is alive (with startup runtime fingerprint)."""
        # 指纹字段见 run_fingerprint.process_face（票 06 运行隔离）:
        # git_sha/git_dirty/config_hash/schema_version——dirty=true 时
        # 晨检亮黄（人裁决①:照跑不拒）。
        return {"status": "ok", **app.state.runtime_face}

    @app.get(
        "/readyz",
        tags=["system"],
        summary="Readiness probe",
        responses={503: {"description": "业务库缺失/不可读或 schema 版本与部署不一致"}},
    )
    async def readiness_check() -> dict[str, str]:
        """
        Report whether the service can accept traffic（票 10④）。

        轻量检查：业务库存在可读 + schema 版本 == 部署迁移链末版（低/高
        都不可服务）。不做 corpus 扫描/研究库/数据新鲜度——那由晨检与页面
        降级表达。
        """
        expected = MIGRATIONS[-1][0]
        try:
            conn = db_connect(resolved.db_path, readonly=True)
            try:
                version = db_current_version(conn)
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            raise HTTPException(status_code=503, detail=f"业务库不可读: {exc}") from exc
        if version != expected:
            raise HTTPException(
                status_code=503,
                detail=f"schema 版本不一致: db={version} deployed={expected}——先跑迁移",
            )
        return {"status": "ready", "schema_version": str(version)}

    @app.get(
        "/api/v1/status",
        tags=["system"],
        summary="Service identity",
        response_model=StatusResponse,
    )
    async def get_status() -> StatusResponse:
        """Return service name, version and environment."""
        return StatusResponse(
            app_name=resolved.app_name,
            app_version=resolved.app_version,
            environment=resolved.environment,
        )

    app.include_router(fixtures_api.router)
    app.include_router(markets_api.router)
    app.include_router(bets_api.router)
    app.include_router(pool_api.router)
    app.include_router(results_api.router)
    app.include_router(validation_api.router)
    app.include_router(evidence_api.router)
    return app


app = create_app()
