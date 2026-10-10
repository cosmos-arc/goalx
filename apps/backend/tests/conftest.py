"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient

# 测试不继承本地部署配置（票 37：本地 .env 冻结的采集范围会改变采集行为）
os.environ["GOALX_ODDS_API_SPORT_SCOPE"] = ""

from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb, gold
from goalx_backend.data.corpus_store import (
    GOLD_DATASET,
    GOLD_PROVIDER,
    CorpusStore,
)
from goalx_backend.data.silver import write_dataset_meta, write_partition
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


# ---- 十年引擎（票 13）合成 gold 环境 ----
# 直写 gold parquet（gold._SCHEMA）+ 建桥 + 迁移后的运行面库：绕过采集链，
# 引擎消费面（match_features 视图）与生产同构。行构造器在各测试文件本地
# 定义（importlib 模式下跨文件不可裸 import conftest 符号）。


@pytest.fixture
def gold_env(tmp_path: Path):
    """工厂：种 gold 行集 → 建桥 → 上下文管理器 yielding (face, duck_con, store)。

    face 为迁移后的运行面 sqlite（backtest 表就绪）；duck_con 挂
    match_features 视图与 face 只读 ATTACH。_meta 钉的成熟度基准日
    = 2026-10-08（era1/era2 样本全放行）。用法：
    ``with gold_env(rows) as (face, duck_con, store): ...``
    """
    settings = Settings(
        corpus_root=tmp_path / "corpus",
        db_path=tmp_path / "goalx.db",
    )

    @contextmanager
    def make(
        rows: list[dict[str, Any]],
    ) -> Iterator[tuple[sqlite3.Connection, duckdb.DuckDBPyConnection, CorpusStore]]:
        face = connect(settings.db_path)
        migrate(face)
        store = CorpusStore(settings.corpus_root)
        root = store.gold_path(GOLD_PROVIDER, GOLD_DATASET)
        if rows:  # 空行集=空语料（无 parquet → 建桥跳过 gold 视图，同真树初生态）
            write_partition(root / "all", rows, gold._SCHEMA)
        write_dataset_meta(
            root,
            {
                "gold_version": gold.GOLD_VERSION,
                "built_at": "2026-10-08T00:00:00",
                "rows": len(rows),
                "maturity_today": "2026-10-08",
                "input_digests": {"srct/fixture_universe": "deadbeef"},
            },
        )
        corpus_duckdb.build_corpus_duckdb(store)
        duck_con = corpus_duckdb.connect(settings)
        try:
            yield face, duck_con, store
        finally:
            duck_con.close()
            face.close()

    return make
