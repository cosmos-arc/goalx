"""票 13 M3 评测报告单测：配对统计/两档达标/情报质量（离线种子）。"""

from __future__ import annotations

import sqlite3

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.results import DrawResultInput, upsert_draw_result
from goalx_backend.evaluation.m3_report import (
    TIER_A_MIN_PAIRS,
    intel_quality,
    m3_protocol_report,
    paired_fused_vs_ml,
    report_summary,
)
from goalx_backend.llm.review import record_blind_review
from goalx_backend.llm.store import IntelDraft, insert_intel_observation
from goalx_backend.modelling.forecast import insert_forecast
from goalx_backend.models import MatchCodeInput, SnapshotInput

_KICKOFF = "2026-09-20T19:00:00+00:00"


def _seed_one(
    db: sqlite3.Connection,
    fixture_id: int,
    *,
    ml: tuple[float, float, float],
    llm: tuple[float, float, float],
    fused: tuple[float, float, float] | None,
    home_goals: int,
    away_goals: int,
    intel: bool = False,
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO competitions (name, tier, created_at)"
        " VALUES ('英超', 'tier1', '2026-09-01T00:00:00+00:00')"
    )
    comp_id = db.execute("SELECT id FROM competitions WHERE name = '英超'").fetchone()[
        "id"
    ]
    ids = {}
    for name in (f"主{fixture_id}", f"客{fixture_id}"):
        cur = db.execute(
            "INSERT INTO teams (canonical_name, created_at)"
            " VALUES (?, '2026-09-01T00:00:00+00:00')",
            (name,),
        )
        ids[name] = cur.lastrowid
    db.execute(
        """
        INSERT INTO fixtures
            (id, competition_id, kickoff_utc, home_team_id, away_team_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (fixture_id, comp_id, _KICKOFF, ids[f"主{fixture_id}"], ids[f"客{fixture_id}"]),
    )
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="ml",
        model_version="dc:1",
        content_hash=f"ml-{fixture_id}",
        payload={
            "matrix": [[0, 0, ml[2]], [0, ml[1], 0], [ml[0], 0, 0]],
            "lambda_home": 1.0,
            "lambda_away": 1.0,
        },
        issued_at="2026-09-20T10:00:00+00:00",
    )
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="llm",
        model_version="glm:1",
        content_hash=f"llm-{fixture_id}",
        payload={
            "h": llm[0],
            "d": llm[1],
            "a": llm[2],
            "intel_ids": [],
            "intel_count": 0,
        },
        issued_at="2026-09-20T10:50:00+00:00",
    )
    if fused is not None:
        insert_forecast(
            db,
            fixture_id=fixture_id,
            track="fused",
            model_version="leap:w=0.5",
            content_hash=f"fu-{fixture_id}",
            payload={"h": fused[0], "d": fused[1], "a": fused[2], "method": "log_pool"},
            issued_at="2026-09-20T10:55:00+00:00",
        )
    upsert_draw_result(
        db,
        DrawResultInput(
            fixture_id=fixture_id,
            home_goals=home_goals,
            away_goals=away_goals,
            source="test",
            published_at="2026-09-20T22:00:00+00:00",
        ),
    )
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture_id,
            kind="jingcai",
            business_date="2026-09-20",
            code=f"周日{fixture_id:03d}",
        ),
    )
    for sel, odds in zip(("h", "d", "a"), (2.4, 3.3, 2.9), strict=True):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture_id,
                market_code="had",
                selection_code=sel,
                source="odds_api:pin",
                odds=odds,
                captured_at="2026-09-20T09:00:00+00:00",
                observed_at="2026-09-20T09:00:00+00:00",
            ),
        )
    if intel:
        insert_intel_observation(
            db,
            fixture_id,
            IntelDraft(
                kind="伤停",
                text="主队伤停：x",
                source="okooo.com/formation",
                collected_at="2026-09-20T09:00:00+00:00",
                collector="okooo-formation",
                raw_payload={"a": 1},
            ),
        )


def test_paired_stats_and_tiers_small_sample(db: sqlite3.Connection) -> None:
    # fused 更准的一场 + ml 更准的一场
    _seed_one(
        db,
        1,
        ml=(0.5, 0.3, 0.2),
        llm=(0.3, 0.4, 0.3),
        fused=(0.45, 0.3, 0.25),
        home_goals=2,
        away_goals=0,
    )
    _seed_one(
        db,
        2,
        ml=(0.2, 0.3, 0.5),
        llm=(0.4, 0.3, 0.3),
        fused=(0.15, 0.25, 0.60),
        home_goals=0,
        away_goals=1,
    )
    paired = paired_fused_vs_ml(db)
    assert paired["pairs"] == 2
    assert paired["fused_win_rate"] == 0.5  # 一胜一负
    report = m3_protocol_report(db)
    assert report["tier_a"]["verdict"] == "未证明（样本不足）"  # 2 < 200
    assert report["tier_b"]["verdict"] == "未证明（样本不足）"
    assert report["tier_a"]["checks"]["pairs_ge_200"] is False
    assert "配对 2 场" in report_summary(report)


def test_tier_threshold_constants_frozen() -> None:
    # 票 05 冻结阈值——常量被意外改动时此测试失败
    assert TIER_A_MIN_PAIRS == 200


def test_intel_quality_columns(db: sqlite3.Connection) -> None:
    _seed_one(
        db,
        1,
        ml=(0.5, 0.3, 0.2),
        llm=(0.3, 0.4, 0.3),
        fused=None,
        home_goals=1,
        away_goals=0,
        intel=True,
    )
    _seed_one(
        db,
        2,
        ml=(0.4, 0.3, 0.3),
        llm=(0.3, 0.4, 0.3),
        fused=None,
        home_goals=0,
        away_goals=1,
        intel=False,
    )
    q = intel_quality(db)
    assert q["covered"] == 1
    assert q["coverage"] == 0.5
    assert q["median_hours_to_kickoff"] is not None
    assert q["median_hours_to_kickoff"] > 0
    assert q["avg_sources"] == 1.0


def test_blind_summary_in_report(db: sqlite3.Connection) -> None:
    _seed_one(
        db,
        1,
        ml=(0.5, 0.3, 0.2),
        llm=(0.3, 0.4, 0.3),
        fused=None,
        home_goals=1,
        away_goals=0,
    )
    record_blind_review(db, cycle="W1", fixture_id=1, choice="llm")
    report = m3_protocol_report(db)
    assert report["blind_review"]["total"] == 1
    assert report["blind_review"]["llm_share"] == 1.0
