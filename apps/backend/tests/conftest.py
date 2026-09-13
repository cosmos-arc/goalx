"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

# 测试不继承本地部署配置（票 37：本地 .env 冻结的采集范围会改变采集行为）
os.environ["GOALX_ODDS_API_SPORT_SCOPE"] = ""

from goalx_backend.config import Settings
from goalx_backend.db import connect, migrate
from goalx_backend.export_openapi import EXPORT_SETTINGS
from goalx_backend.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings shared with the contract exporter."""
    return EXPORT_SETTINGS


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """ASGI test client bound to a fresh application."""
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Fresh migrated in-memory database per test."""
    conn = connect(":memory:")
    migrate(conn)
    yield conn
    conn.close()
