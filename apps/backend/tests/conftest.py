"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from goalx_backend.config import Settings
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
