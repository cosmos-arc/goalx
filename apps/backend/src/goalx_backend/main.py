"""GoalX FastAPI application factory and system routes."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger
from pydantic import BaseModel

from goalx_backend.api import bets as bets_api
from goalx_backend.api import fixtures as fixtures_api
from goalx_backend.api import results as results_api
from goalx_backend.config import Environment, Settings, get_settings

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
            {"name": "bets", "description": "注级建议、票级回录与复盘列表。"},
            {"name": "results", "description": "开奖导入、结算批跑与资金池。"},
        ],
    )
    app.state.settings = resolved

    @app.get("/healthz", tags=["system"], summary="Liveness probe")
    async def health_check() -> dict[str, str]:
        """Report whether the process is alive."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["system"], summary="Readiness probe")
    async def readiness_check() -> dict[str, str]:
        """Report whether the service can accept traffic."""
        return {"status": "ready"}

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
    app.include_router(bets_api.router)
    app.include_router(results_api.router)
    return app


app = create_app()
