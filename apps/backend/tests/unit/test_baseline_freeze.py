"""auto-paper 票 01：市场基准同窗冻结 + 预测覆盖账。

钉住三件事：as-of 防泄漏（拍点后到价不进锚快照）、write-once 幂等
（同锚重跑被 UNIQUE 吸收零新增）、覆盖账普查（covered/skip 三因逐场）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from goalx_backend.data import fixtures as fx_store
from goalx_backend.evaluation.baseline_freeze import (
    anchor_label_at,
    freeze_anchor_baselines,
)
from goalx_backend.markets import SELECTIONS
from goalx_backend.modelling.forecast import insert_forecast, record_coverage
from goalx_backend.models import MatchCodeInput, SaleStatusInput, SnapshotInput, Tier

CUTOFF = "2026-11-07T04:33:00+00:00"  # 12:33 北京时 = 12:30 锚后 3 分钟
BEFORE = "2026-11-07T04:28:00+00:00"  # 锚前可见
AFTER = "2026-11-07T04:40:00+00:00"  # 锚后到价（不得进快照）
KICKOFF = "2026-11-07T19:00:00+00:00"
BUSINESS_DATE = "2026-11-07"
BEIJING = ZoneInfo("Asia/Shanghai")


def _seed_fixture(db: sqlite3.Connection, tag: str, *, code: str) -> int:
    """一场带当日竞彩销售码的场次。"""
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"冻结主队{tag}")
    away = fx_store.upsert_team(db, f"冻结客队{tag}")
    fixture = fx_store.upsert_fixture(db, competition, KICKOFF, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture, kind="jingcai", business_date=BUSINESS_DATE, code=code
        ),
    )
    db.commit()
    return fixture


def _seed_jc_triple(
    db: sqlite3.Connection, fixture_id: int, prices: tuple[float, float, float], at: str
) -> None:
    for sel, odds in zip(SELECTIONS, prices, strict=True):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture_id,
                market_code="had",
                selection_code=sel,
                source="sporttery",
                odds=odds,
                captured_at=at,
                observed_at=at,
            ),
        )
    db.commit()


def _snapshots(db: sqlite3.Connection, fixture_id: int) -> dict[tuple[str, str], Any]:
    return {
        (str(r["source"]), str(r["anchor_label"])): json.loads(str(r["prices"]))
        for r in db.execute(
            "SELECT source, anchor_label, prices FROM market_baseline_snapshots"
            " WHERE fixture_id = ?",
            (fixture_id,),
        )
    }


def test_anchor_label_at_maps_beijing_clock() -> None:
    def at(hhmm: str) -> str | None:
        naive = datetime.fromisoformat(f"2026-11-07T{hhmm}")
        return anchor_label_at(naive.replace(tzinfo=BEIJING))

    assert at("12:33") == "12:30"
    assert at("13:05") == "12:30"
    assert at("16:40") == "16:30"
    assert at("21:05") == "21:00"
    assert at("23:59") == "21:00"
    assert at("09:00") is None  # 早于首锚：误拍防护


def test_freeze_jc_asof_and_write_once(db: sqlite3.Connection) -> None:
    fixture = _seed_fixture(db, "A", code="001")
    _seed_jc_triple(db, fixture, (2.10, 3.20, 3.40), BEFORE)
    # 锚后到价：h 从 2.10 跳 1.85——不得进 12:30 锚
    fx_store.insert_odds_snapshot(
        db,
        SnapshotInput(
            fixture_id=fixture,
            market_code="had",
            selection_code="h",
            source="sporttery",
            odds=1.85,
            captured_at=AFTER,
            observed_at=AFTER,
        ),
    )
    fx_store.append_sale_status(
        db,
        SaleStatusInput(
            fixture_id=fixture,
            market_code="had",
            sale_state="on_sale",
            observed_at=BEFORE,
        ),
    )
    db.commit()

    stats = freeze_anchor_baselines(
        db, anchor_label="12:30", cutoff_at=CUTOFF, business_date=BUSINESS_DATE
    )
    assert stats.fixtures == 1
    assert stats.jc_frozen == 1
    assert stats.jc_absent == 0
    assert stats.eu_frozen == 0  # 无 srct 链 → 无共识，计数不伪造
    assert stats.eu_absent == 1
    assert stats.duplicates == 0

    rows = _snapshots(db, fixture)
    jc = rows[("jc_official", "12:30")]
    assert jc == {"a": 3.40, "d": 3.20, "h": 2.10}  # 锚后 1.85 被挡
    sale_state = db.execute(
        "SELECT sale_state FROM market_baseline_snapshots WHERE fixture_id = ?"
        " AND source = 'jc_official'",
        (fixture,),
    ).fetchone()["sale_state"]
    assert sale_state == "on_sale"

    # write-once：同锚重跑零新增，UNIQUE 吸收计 duplicates
    rerun = freeze_anchor_baselines(
        db, anchor_label="12:30", cutoff_at=CUTOFF, business_date=BUSINESS_DATE
    )
    assert rerun.jc_frozen == 0
    assert rerun.duplicates == 1
    assert len(_snapshots(db, fixture)) == 1  # 无 eu 行 → 仍只有 1 行


def test_freeze_incomplete_triple_absent(db: sqlite3.Connection) -> None:
    fixture = _seed_fixture(db, "B", code="002")
    # 只种 h/a 两腿（odds_snapshots append-only，不可删改）
    for sel, odds in zip(SELECTIONS, (2.10, 3.40), strict=False):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="sporttery",
                odds=odds,
                captured_at=BEFORE,
                observed_at=BEFORE,
            ),
        )
    db.commit()
    stats = freeze_anchor_baselines(
        db, anchor_label="16:30", cutoff_at=CUTOFF, business_date=BUSINESS_DATE
    )
    assert stats.jc_frozen == 0
    assert stats.jc_absent == 1  # 三向不全不落行
    assert _snapshots(db, fixture) == {}


def test_freeze_rejects_unknown_anchor(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="09:00"):
        freeze_anchor_baselines(
            db, anchor_label="09:00", cutoff_at=CUTOFF, business_date=BUSINESS_DATE
        )


def test_record_coverage_census(db: sqlite3.Connection) -> None:
    covered_fx = _seed_fixture(db, "C", code="003")
    skipped_fx = _seed_fixture(db, "D", code="004")
    unlisted_fx = _seed_fixture(db, "E", code="005")
    insert_forecast(
        db,
        fixture_id=covered_fx,
        track="ml",
        model_version="dc-test",
        content_hash="hash-c",
        payload={"matrix": [[0.5]]},
        issued_at=BEFORE,
    )
    db.commit()
    skipped = [
        {
            "fixture_id": str(skipped_fx),
            "reason": "not_fd_league",
            "detail": None,
        },
    ]
    stats = record_coverage(db, business_date=BUSINESS_DATE, skipped=skipped)
    assert stats == {"total": 3, "covered": 1, "skipped": 2}

    rows = {
        int(r["fixture_id"]): r
        for r in db.execute(
            "SELECT fixture_id, covered, reason FROM forecast_coverage"
            " WHERE business_date = ?",
            (BUSINESS_DATE,),
        )
    }
    assert rows[covered_fx]["covered"] == 1
    assert rows[covered_fx]["reason"] is None
    assert rows[skipped_fx]["covered"] == 0
    assert rows[skipped_fx]["reason"] == "not_fd_league"
    assert rows[unlisted_fx]["covered"] == 0
    assert rows[unlisted_fx]["reason"] == "unknown"  # 防御：不在清单不猜

    # 普查 REPLACE 幂等：重跑同输入计数不变
    rerun = record_coverage(db, business_date=BUSINESS_DATE, skipped=skipped)
    assert rerun == stats
