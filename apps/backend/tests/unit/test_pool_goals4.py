"""goals4 进球档面（pool-mapping-goals4/02）回归。

钉住：详情 selections 按玩法分形状（ttt14 三向不变 / goals4 八档）、
档位边际=比分矩阵行和/列和（3+ 聚尾）、无 ML 如实 none、生成器护栏 422。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.db import connect, migrate
from goalx_backend.main import create_app
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


def _seed(
    db: sqlite3.Connection, *, market: str, with_forecast: bool, match_count: int = 1
) -> int:
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
    # 场 1 映射真实 fixture；其余为结构占位（票 08 归属守卫下，构票测试
    # 需期次真有场 2..N 才合法——不映射只影响概率/联合面，不影响组合）
    pool_store.replace_pool_matches(
        db,
        pid,
        [
            pool_store.PoolMatchInput(
                match_seq=seq,
                source_match_id="2042441" if seq == 1 else None,
                league="意甲",
                kickoff_utc=_POOL_KICKOFF,
                home_team="亚特兰大" if seq == 1 else f"主队{seq}",
                away_team="帕尔马" if seq == 1 else f"客队{seq}",
                euro_odds=(None, None, None),
            )
            for seq in range(1, match_count + 1)
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


def test_goals4_joint_served_from_matrix(db: sqlite3.Connection) -> None:
    """票 10 验收②：服务面联合=矩阵格聚合，边缘和==档位边际（逐位钉死）。"""
    pid = _seed(db, market="goals4", with_forecast=True)
    views = pool_store.match_views(db, pid, _AS_OF)
    joint = views[0].goals4_joint
    assert joint is not None
    prob = {s.code: s.prob for s in views[0].selections}
    # 格值显式钉死（非边际连乘：连乘 h1×a1=0.3×0.3=0.09 ≠ 格 0.2）
    assert joint["h1"]["a1"] == 0.2
    assert joint["h2"]["a2"] == 0.35
    assert joint["h3"]["a3"] == 0.1
    for i in range(4):
        row_sum = sum(joint[f"h{i}"][f"a{j}"] for j in range(4))
        col_sum = sum(joint[f"h{j}"][f"a{i}"] for j in range(4))
        # 服务面两侧独立取整（4/6 位），边缘一致在浮点容差内钉死
        assert row_sum == pytest.approx(prob[f"h{i}"], abs=1e-4)
        assert col_sum == pytest.approx(prob[f"a{i}"], abs=1e-4)


def test_goals4_joint_none_without_forecast_or_other_market(
    db: sqlite3.Connection,
) -> None:
    """无预测 goals4 / 非 goals4 期次：联合恒 None（不虚构）。"""
    pid = _seed(db, market="goals4", with_forecast=False)
    assert pool_store.match_views(db, pid, _AS_OF)[0].goals4_joint is None
    ttt = _seed(db, market="ttt14", with_forecast=True)
    assert pool_store.match_views(db, ttt, _AS_OF)[0].goals4_joint is None


def test_goals4_pool_slip_preview_and_create(tmp_path: Path) -> None:
    """票 10 验收①：goals4 构票贯通——预览 0 读数（不完备）/计数（完备）+ 落库。

    隔离库（correctness E1）：共享 client fixture 的应用按 CWD 相对路径
    开 data/goalx.db——从仓根跑会把测试票写进真实工作库；本测试自建
    临时库并显式传 pool_period_id（期次校验一并覆盖）。
    """
    db_path = tmp_path / "goals4-slip.db"
    db = connect(db_path)
    migrate(db)
    pid = _seed(db, market="goals4", with_forecast=False, match_count=4)
    db.commit()  # _seed 不自提交；app 是独立连接，先落盘再开门
    client = TestClient(
        create_app(
            settings=Settings(
                db_path=db_path, research_db_path=tmp_path / "research.db"
            )
        )
    )
    client.__enter__()
    try:
        # 不完备预览（<4 场 / 单侧缺）：诚实 0，不 400
        incomplete = client.post(
            "/api/v1/pool-slips/preview",
            json={
                "market_code": "goals4",
                "picks": [{"match_seq": 1, "selection_code": "h0"}],
            },
        )
        assert incomplete.status_code == 200, incomplete.text
        assert incomplete.json()["combination_count"] == 0
        # 完备票：4 场各双档（第 1 场主档复式 h0/h1）→ 2 注
        picks = [
            {"match_seq": 1, "selection_code": "h0"},
            {"match_seq": 1, "selection_code": "h1"},
            {"match_seq": 1, "selection_code": "a2"},
            {"match_seq": 2, "selection_code": "h3"},
            {"match_seq": 2, "selection_code": "a2"},
            {"match_seq": 3, "selection_code": "h2"},
            {"match_seq": 3, "selection_code": "a2"},
            {"match_seq": 4, "selection_code": "h1"},
            {"match_seq": 4, "selection_code": "a2"},
        ]
        preview = client.post(
            "/api/v1/pool-slips/preview",
            json={"market_code": "goals4", "picks": picks},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["combination_count"] == 2
        created = client.post(
            "/api/v1/pool-slips",
            json={
                "mode": "paper",
                "pool_period_id": pid,
                "market_code": "goals4",
                "picks": picks,
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["combination_count"] == 2
        assert body["pool_stake_total"] == 4  # 2 注 × ¥2
        rows = db.execute(
            "SELECT selections FROM combinations WHERE slip_id = ?",
            (body["id"],),
        ).fetchall()
        assert len(rows) == 2
        first = json.loads(rows[0]["selections"])
        assert len(first) == 8  # 4 场 × (主档+客档)
        assert {"match_seq": 1, "selection_code": "h0"} in first
        assert {"match_seq": 1, "selection_code": "a2"} in first
    finally:
        client.__exit__(None, None, None)
        db.close()
