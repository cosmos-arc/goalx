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
    # uniform 族官方赛果（票 44 实测直通：jc 族 403 是端点级，本端点同域同头可用）
    sporttery_uniform_url: str = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"
    # openfootball/football.json raw（票 44 对账源：CC0 静态文件，无 key 无 WAF）
    openfootball_base_url: str = (
        "https://raw.githubusercontent.com/openfootball/football.json/master"
    )
    # Understat xG（票 45）：robots.txt 全站 Disallow——个人研究低频使用
    # （默认每日 1 首页 + 5 联赛文件 = 6 请求 ≤10 上限，见 schedules）
    understat_base_url: str = "https://understat.com"
    # 源B（票 43 代称表）：传统足彩期次/对阵/人气分布（官方销量无源，走 AI 代采）
    zucai_base_url: str = "https://www.okooo.com"
    # 源B 移动端（票 49 采集先行）：欧指变化时序（三步预热 Referer 链）
    srcb_mobile_base: str = "https://m.okooo.com"
    # football-data.org 免费档（票 09：12 项 standings；免费注册，无 key 时跳过）
    fdorg_base_url: str = "https://api.football-data.org"
    fdorg_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("GOALX_FDORG_API_KEY", "FDORG_API_KEY"),
    )

    # --- The Odds API credit 预算护栏（票 20：免费档 500/月） ---
    odds_api_daily_credit_budget: float = 40.0
    odds_api_monthly_credit_budget: float = 480.0
    # 冻结采集范围（票 37 运行协议）：逗号分隔 sport key；空=动态发现全部足球
    odds_api_sport_scope: str | None = None

    # --- PropLine 免费层（票 50 互备源）：按请求数/天计费，非 credit ---
    propline_base_url: str = "https://api.prop-line.com/v1"
    propline_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("GOALX_PROPLINE_API_KEY", "PROPLINE_API_KEY"),
    )
    propline_daily_request_budget: int = 900  # 免费层 1000，留 10% 余量（404 也记 1）
    # X-Daily-Remaining 低于此值停止后续 sport（省出安全边际，票 50）
    propline_remaining_floor: int = 20
    # 冻结采集范围：逗号分隔 sport key；空=取库内 competitions 的 sport key
    propline_sport_scope: str | None = None

    # --- GLM 线（票 08：Coding Plan key 实测定案） ---
    glm_api_key: str = Field(
        default="", validation_alias=AliasChoices("GOALX_GLM_API_KEY", "GLM_API_KEY")
    )
    # 默认 Coding Plan 端面（订阅额度内零边际成本），配额耗尽降级按量端面
    glm_base_url: str = Field(
        default="https://open.bigmodel.cn/api/coding/paas/v4",
        validation_alias=AliasChoices("GOALX_GLM_BASE_URL", "GLM_BASE_URL"),
    )
    glm_payg_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_scout_model: str = "glm-5.3-flash"
    glm_analyst_model: str = "glm-5.3"
    glm_fallback_model: str = "glm-4.7-flash"
    glm_monthly_budget_cny: float = 360.0  # $50 硬上限（票 02）
    fusion_ml_weight: float = 0.5  # LEAP log-pool 的 ML 权重（票 12）
    # M3 证伪开关（票 13：证伪=停 analyst/停 fused，scout 与存证保留）
    m3_analyst_enabled: bool = True
    m3_fusion_enabled: bool = True
    glm_request_timeout: float = 90.0

    @property
    def is_production(self) -> bool:
        """Whether the process runs with production posture."""
        return self.environment is Environment.PRODUCTION


def get_settings() -> Settings:
    """Load settings from the process environment (or ``.env``)."""
    return Settings()
