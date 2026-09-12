"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from goalx_backend.config import Environment, Settings
from goalx_backend.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings independent of the ambient environment."""
    return Settings(
        app_name="goalx-backend",
        app_version="0.1.0",
        environment=Environment.TESTING,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """ASGI test client bound to a fresh application."""
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client
