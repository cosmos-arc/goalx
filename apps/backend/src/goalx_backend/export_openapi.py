"""
Export the OpenAPI contract from the application.

The reviewed contract lives at `contracts/openapi/v1.json`. This module
regenerates it from the application so the file never drifts from the code:

    task contract-export

CI fails if regenerating produces a diff (see `task check-contract`).
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from goalx_backend.config import Environment, Settings
from goalx_backend.main import create_app

# apps/backend/src/goalx_backend/export_openapi.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACT_PATH = REPO_ROOT / "contracts" / "openapi" / "v1.json"

# Fixed identity so the exported document is independent of ambient env vars.
EXPORT_SETTINGS = Settings(
    app_name="goalx-backend",
    app_version="0.1.0",
    environment=Environment.TESTING,
)


def export_contract(target: Path = CONTRACT_PATH) -> Path:
    """Write the canonical OpenAPI document and return its path."""
    app = create_app(EXPORT_SETTINGS)
    document = app.openapi()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("OpenAPI contract written to {}", target)
    return target


if __name__ == "__main__":
    export_contract()
