"""
Application settings loaded from environment variables.

All runtime knobs live here so configuration has exactly one source of truth.
Environment variables use the ``GOALX_`` prefix (``GOALX_ENVIRONMENT`` etc.).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """Deployment environments the backend understands."""

    DEVELOPMENT = "development"
    TESTING = "testing"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """GoalX backend runtime settings."""

    model_config = SettingsConfigDict(
        env_prefix="GOALX_",
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
        extra="ignore",
    )

    app_name: str = "goalx-backend"
    app_version: str = "0.1.0"
    environment: Environment = Environment.DEVELOPMENT

    # --- 存储（ADR 0003：SQLite WAL 单机） ---
    db_path: Path = Path("data/goalx.db")
    observations_dir: Path = Path("data/observations")  # 原始响应 gzip 存档（票 35）

    # --- ML 线（票 26/27：DC 工件与训练参数） ---
    models_dir: Path = Path("data/models")
    dc_half_life_days: float = 365.0  # 时间衰减半衰期（约 1 年起步）
    bootstrap_samples: int = 50  # bootstrap CI 重采样数（0 关闭）
    bootstrap_seed: int = 20260913

    # --- 数据源（票 02/07 选型；key 与 .env 模板的无前缀名兼容） ---
    sporttery_calculator_url: str = (
        "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry"
    )
    sporttery_referer: str = "https://www.sporttery.cn/"
    odds_api_base_url: str = "https://api.the-odds-api.com/v4"
    odds_api_key: str = Field(
        default="", validation_alias=AliasChoices("GOALX_ODDS_API_KEY", "ODDS_API_KEY")
    )
    api_football_key: str = Field(
        default="",
        validation_alias=AliasChoices("GOALX_API_FOOTBALL_KEY", "API_FOOTBALL_KEY"),
    )
    fd_base_url: str = "https://www.football-data.co.uk/mmz4281"
    # 源D 结果页（票 42 实证：访问限制仅要求浏览器 UA；?e=业务日）
    caiguo_base_url: str = "https://live.500.com/jczq.php"
    # 源B（票 43 代称表）：传统足彩期次/对阵/人气分布（官方销量无源，走 AI 代采）
    zucai_base_url: str = "https://www.okooo.com"

    # --- The Odds API credit 预算护栏（票 20：免费档 500/月） ---
    odds_api_daily_credit_budget: float = 40.0
    odds_api_monthly_credit_budget: float = 480.0
    # 冻结采集范围（票 37 运行协议）：逗号分隔 sport key；空=动态发现全部足球
    odds_api_sport_scope: str | None = None

    @property
    def is_production(self) -> bool:
        """Whether the process runs with production posture."""
        return self.environment is Environment.PRODUCTION


def get_settings() -> Settings:
    """Load settings from the process environment (or ``.env``)."""
    return Settings()
