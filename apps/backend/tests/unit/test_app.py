"""Unit tests for the application factory, system routes and settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend.config import Environment, Settings, get_settings
from goalx_backend.export_openapi import export_contract
from goalx_backend.main import create_app

pytestmark = pytest.mark.unit


def test_healthz_reports_ok(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_ready(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_status_reports_service_identity(client: TestClient) -> None:
    response = client.get("/api/v1/status")

    assert response.status_code == 200
    assert response.json() == {
        "app_name": "goalx-backend",
        "app_version": "0.1.0",
        "environment": "testing",
    }


def test_responses_use_json_content_type(client: TestClient) -> None:
    response = client.get("/api/v1/status")

    assert response.headers["content-type"] == "application/json"


def test_production_environment_is_detected_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALX_ENVIRONMENT", "production")

    settings = get_settings()

    assert settings.environment is Environment.PRODUCTION
    assert settings.is_production is True


def test_default_environment_is_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOALX_ENVIRONMENT", raising=False)

    settings = Settings(_env_file=None)

    assert settings.environment is Environment.DEVELOPMENT
    assert settings.is_production is False


def test_export_contract_writes_openapi_document(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "v1.json"

    written = export_contract(target)

    assert written == target
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["info"]["title"] == "goalx-backend"
    assert "/api/v1/status" in document["paths"]


def test_create_app_accepts_explicit_settings() -> None:
    custom = Settings(
        app_name="custom-name",
        app_version="9.9.9",
        environment=Environment.PRODUCTION,
    )

    app = create_app(custom)
    with TestClient(app) as test_client:
        response = test_client.get("/api/v1/status")

    assert response.json() == {
        "app_name": "custom-name",
        "app_version": "9.9.9",
        "environment": "production",
    }
