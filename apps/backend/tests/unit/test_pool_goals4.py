"""goals4 进球档面（pool-mapping-goals4/02）回归。

钉住：详情 selections 按玩法分形状（ttt14 三向不变 / goals4 八档）、
档位边际=比分矩阵行和/列和（3+ 聚尾）、无 ML 如实 none、生成器护栏 422。
"""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.models import MatchCodeInput

_POOL_KICKOFF = "2026-11-02T16:00:00+00:00"  # 官方源日期精度假时刻
_AS_OF = "2026-11-02T08:00:00+00:00"

# 2×2 已知矩阵（3+ 聚尾可验）：P(主0)=0.2 P(主1)=0.3 P(主2)=0.4 P(主3+)=0.1
# P(客0)=0.25 P(客1)=0.25 P(客2)=0.5? —— 由格值显式钉死，不靠巧合
_MATRIX = [
    [0.1, 0.1, 0.0, 0.0],
    [0.1, 0.2, 0.0, 0.0],
    [0.05, 0.0, 0.35, 0.0],
    [0.0, 0.0, 0.0, 0.1],
]


def _seed(db: sqlite3.Connection, *, market: str, with_forecast: bool) -> int:
    comp = fx_store.upsert_competition(db, "意甲")
    h = fx_store.upsert_team(db, "亚特兰大")
    a = fx_store.upsert_team(db, "帕尔马")
    fid = fx_store.upsert_fixture(db, comp, "2026-11-02T19:45:00+00:00", h, a)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fid,
            kind="jingcai",
            business_date="2026-11-02",
            code="周一004",
            source_match_id="2042441",
        ),
    )
    if with_forecast:
        db.execute(
            "INSERT INTO forecasts (fixture_id, track, model_version, issued_at,"
            " content_hash, payload) VALUES (?, 'ml', 'dc-test', ?, ?, ?)",
            (
                fid,
                "2026-11-02T02:00:00+00:00",
                f"hash-{market}-{with_forecast}",
                json.dumps({"matrix": _MATRIX, "lambda_home": 1.5, "lambda_away": 1.0}),
            ),
        )
    pid = pool_store.upsert_pool_period(
        db, market, "26226", "2026-11-02T14:00:00+00:00"
    )
    pool_store.replace_pool_matches(
        db,
        pid,
        [
            pool_store.PoolMatchInput(
                match_seq=1,
                source_match_id="2042441",
                league="意甲",
                kickoff_utc=_POOL_KICKOFF,
                home_team="亚特兰大",
                away_team="帕尔马",
                euro_odds=(None, None, None),
            )
        ],
    )
    return pid


def test_goals4_selections_are_goal_bands(db: sqlite3.Connection) -> None:
    """goals4 期次：8 档、概率=矩阵行/列和、3+ 聚尾、无份额 EV=None。"""
    pid = _seed(db, market="goals4", with_forecast=True)
    views = pool_store.match_views(db, pid, _AS_OF)
    assert len(views) == 1
    sels = views[0].selections
    assert [s.code for s in sels] == ["h0", "h1", "h2", "h3", "a0", "a1", "a2", "a3"]
    assert [s.label for s in sels][:4] == ["主0球", "主1球", "主2球", "主3+球"]
    prob = {s.code: s.prob for s in sels}
    # 行和=主队档（含 3+ 聚尾：第4行起并入 h3）
    assert prob["h0"] == 0.2
    assert prob["h1"] == 0.3
    assert prob["h2"] == 0.4
    assert prob["h3"] == 0.1
    # 列和=客队档
    assert prob["a0"] == 0.25
    assert prob["a1"] == 0.3
    assert prob["a2"] == 0.35
    assert prob["a3"] == 0.1
    assert all(s.prob_source == "model" for s in sels)
    assert all(s.share is None for s in sels)
    assert all(s.ev is None for s in sels)


def test_goals4_without_forecast_is_none(db: sqlite3.Connection) -> None:
    """无 ML 预测：8 档如实 prob=None / prob_source=none（无三向兜底混充）。"""
    pid = _seed(db, market="goals4", with_forecast=False)
    views = pool_store.match_views(db, pid, _AS_OF)
    assert len(views[0].selections) == 8
    assert all(s.prob is None and s.prob_source == "none" for s in views[0].selections)


def test_ttt14_selections_unchanged(db: sqlite3.Connection) -> None:
    """ttt14 期次：三向形状与无预测口径维持原样（回流路径零改动）。"""
    pid = _seed(db, market="ttt14", with_forecast=False)
    views = pool_store.match_views(db, pid, _AS_OF)
    assert [s.code for s in views[0].selections] == ["h", "d", "a"]
    assert [s.label for s in views[0].selections] == ["胜", "平", "负"]


def test_generators_reject_goals4(client: TestClient) -> None:
    """搏冷/目标票生成器对 goals4 一场两档语义 422，不产废票。"""
    for path, body in (
        ("/api/v1/pool/cold-variants", {"period_no": "26226", "market_code": "goals4"}),
        (
            "/api/v1/pool/target-plan",
            {"period_no": "26226", "market_code": "goals4", "target_amount": 100},
        ),
    ):
        resp = client.post(path, json=body)
        assert resp.status_code == 422, (path, resp.status_code, resp.text)


def test_goals4_mapping_reaches_llm_targets(db: sqlite3.Connection) -> None:
    """goals4 在售期映射命中 → LLM 定向层可见（scout POOL 用途/gate 判定）。

    钉死 correctness F1：四个 llm 消费面曾只遍历 (ttt14, pick9)，
    goals4 映射修好了也到不了定向层。
    """
    from goalx_backend.llm import gate as llm_gate
    from goalx_backend.llm import scout as llm_scout
    from goalx_backend.llm.client import Purpose

    _seed(db, market="goals4", with_forecast=False)
    moment = "2026-11-02T08:00:00+00:00"
    targets = llm_scout.scout_targets(db, now=moment)
    pool_fids = [fid for fid, purpose in targets.items() if purpose is Purpose.POOL]
    assert pool_fids, targets
    assert llm_gate._is_pool_fixture(db, fixture_id=pool_fids[0], moment=moment)
