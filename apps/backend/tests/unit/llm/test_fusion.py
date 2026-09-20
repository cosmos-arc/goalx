"""票 12 融合单测：log-pool 数学/工件独立性/无源跳过/源更新再融合。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from goalx_backend.config import Settings
from goalx_backend.llm.fusion import fuse_fixture, fusion_sweep, log_pool
from goalx_backend.modelling.forecast import insert_forecast

_KICKOFF = "2026-09-26T19:00:00+00:00"


def _settings(w: float = 0.5) -> Settings:
    return Settings(glm_api_key="k", fusion_ml_weight=w)


def _ml_matrix_payload() -> dict[str, object]:
    # had 边际 ≈ (0.53, 0.27, 0.20)
    return {
        "matrix": [
            [0.12, 0.10, 0.06],
            [0.20, 0.08, 0.04],
            [0.20, 0.13, 0.07],
        ],
        "lambda_home": 1.2,
        "lambda_away": 0.9,
    }


def _seed_fixture(db: sqlite3.Connection) -> int:
    cur = db.execute(
        "INSERT INTO competitions (name, tier, odds_api_sport_key, created_at)"
        " VALUES ('英超', 'tier1', 'soccer_epl', '2026-09-01T00:00:00+00:00')"
    )
    comp_id = cur.lastrowid
    ids = {}
    for name in ("A 队", "B 队"):
        cur = db.execute(
            "INSERT INTO teams (canonical_name, created_at)"
            " VALUES (?, '2026-09-01T00:00:00+00:00')",
            (name,),
        )
        ids[name] = cur.lastrowid
    cur = db.execute(
        "INSERT INTO fixtures (competition_id, kickoff_utc, home_team_id, away_team_id)"
        " VALUES (?, ?, ?, ?)",
        (comp_id, _KICKOFF, ids["A 队"], ids["B 队"]),
    )
    return int(cur.lastrowid)


def _seed_llm(
    db: sqlite3.Connection,
    fixture_id: int,
    triple: tuple[float, float, float],
    issued_at: str = "2026-09-19T10:50:00+00:00",
) -> None:
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="llm",
        model_version="glm:glm-5.3-flash",
        content_hash=f"llm-{fixture_id}-{issued_at}",
        payload={
            "h": triple[0],
            "d": triple[1],
            "a": triple[2],
            "rationale": "x",
            "model": "glm-5.3-flash",
            "intel_count": 0,
            "intel_ids": [],
        },
        issued_at=issued_at,
    )


def _seed_ml(db: sqlite3.Connection, fixture_id: int) -> None:
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="ml",
        model_version="dc:1",
        content_hash=f"ml-{fixture_id}",
        payload=_ml_matrix_payload(),
        issued_at="2026-09-19T10:00:00+00:00",
    )


def test_log_pool_math() -> None:
    p, q = (0.53, 0.27, 0.20), (0.30, 0.30, 0.40)
    # w=0.5 几何平均，归一
    fused = log_pool(p, q, 0.5)
    assert sum(fused) == pytest.approx(1.0)
    assert all(x > 0 for x in fused)
    # 权重端点退化
    assert log_pool(p, q, 1.0) == pytest.approx(p)
    assert log_pool(p, q, 0.0) == pytest.approx(q)
    # 零概率钳制不炸
    zero = log_pool((0.0, 0.5, 0.5), (1.0, 0.0, 0.0), 0.5)
    assert sum(zero) == pytest.approx(1.0)
    # 融合值落在两源之间（几何池化性质）
    assert min(p[0], q[0]) <= fused[0] <= max(p[0], q[0])


def test_fuse_fixture_inserts_fused_track(db: sqlite3.Connection) -> None:
    fixture_id = _seed_fixture(db)
    _seed_ml(db, fixture_id)
    _seed_llm(db, fixture_id, (0.30, 0.30, 0.40))
    status, row_id = fuse_fixture(
        db, _settings(), fixture_id, now="2026-09-19T23:59:00+00:00"
    )
    assert status == "inserted"
    row = db.execute(
        "SELECT track, model_version, payload FROM forecasts WHERE id = ?", (row_id,)
    ).fetchone()
    assert row["track"] == "fused"
    assert row["model_version"] == "leap:w=0.5"
    payload = json.loads(str(row["payload"]))
    assert payload["method"] == "log_pool"
    assert sum(payload[k] for k in ("h", "d", "a")) == pytest.approx(1.0)
    ml_id = db.execute(
        "SELECT id FROM forecasts WHERE fixture_id=? AND track='ml'", (fixture_id,)
    ).fetchone()["id"]
    assert payload["sources"]["ml"] == ml_id  # 双源引用

    # 同内容重跑幂等；ml/llm 原行数不变（独立工件）
    status2, _ = fuse_fixture(
        db, _settings(), fixture_id, now="2026-09-20T00:05:00+00:00"
    )
    assert status2 == "known"
    assert (
        db.execute(
            "SELECT COUNT(*) AS n FROM forecasts WHERE fixture_id=?", (fixture_id,)
        ).fetchone()["n"]
        == 3
    )

    # 源更新（新 llm 行）→ hash 变 → 再融合出新 fused 行
    _seed_llm(db, fixture_id, (0.45, 0.30, 0.25), issued_at="2026-09-20T11:00:00+00:00")
    stats = fusion_sweep(db, _settings(), now="2026-09-20T11:30:00+00:00")
    assert stats.inserted == 1
    n_fused = db.execute(
        "SELECT COUNT(*) AS n FROM forecasts WHERE fixture_id=? AND track='fused'",
        (fixture_id,),
    ).fetchone()["n"]
    assert n_fused == 2


def test_fuse_fixture_without_llm_skipped(db: sqlite3.Connection) -> None:
    fixture_id = _seed_fixture(db)
    _seed_ml(db, fixture_id)  # 只有 ML
    status, row_id = fuse_fixture(db, _settings(), fixture_id)
    assert status == "skipped"
    assert row_id is None
    stats = fusion_sweep(db, _settings())
    assert stats.pairs == 0
    assert stats.inserted == 0  # 宁缺毋假
