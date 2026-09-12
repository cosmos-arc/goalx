"""
Application settings loaded from environment variables.

All runtime knobs live here so configuration has exactly one source of truth.
Environment variables use the ``GOALX_`` prefix (``GOALX_ENVIRONMENT`` etc.).
"""

from __future__ import annotations

from enum import StrEnum

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
        extra="ignore",
    )

    app_name: str = "goalx-backend"
    app_version: str = "0.1.0"
    environment: Environment = Environment.DEVELOPMENT

    @property
    def is_production(self) -> bool:
        """Whether the process runs with production posture."""
        return self.environment is Environment.PRODUCTION


def get_settings() -> Settings:
    """Load settings from the process environment (or ``.env``)."""
    return Settings()
