"""The served OpenAPI document must equal the reviewed contract file.

If this test fails after an intentional API change, run `task contract-export`,
review the contract diff, and commit it together with the code change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend.export_openapi import EXPORT_SETTINGS
from goalx_backend.main import create_app

pytestmark = pytest.mark.integration

# apps/backend/tests/contract/test_openapi_conformance.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACT_PATH = REPO_ROOT / "contracts" / "openapi" / "v1.json"


def test_served_openapi_equals_reviewed_contract() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    with TestClient(create_app(EXPORT_SETTINGS)) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json() == contract
