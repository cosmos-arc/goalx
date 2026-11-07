"""票 09：更正通道 null 半场=保持库内现值（人裁决 2026-11-07）。

首写 null=真未知不变；更正载荷 null 半场=保持现值，预览与导入同语义——
已结 hafu 注仅更正全场比分时按新比分重结算，预览显真实资金差非"—"。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from goalx_backend.betting import store as bt
from goalx_backend.betting.bets import BetDraft, create_bet_with_legs
from goalx_backend.betting.settle import preview_draw_result_change, run_settlement
from goalx_backend.data import fixtures as fx
from goalx_backend.data import results as rs
from goalx_backend.data.ingest.results import import_draw_results
from goalx_backend.models import BetMode, DrawResultInput, LegInput


def make_fixture(db: sqlite3.Connection) -> int:
    competition = fx.upsert_competition(db, "test")
    home = fx.upsert_team(db, "home")
    away = fx.upsert_team(db, "away")
    return fx.upsert_fixture(db, competition, "2026-09-01T12:00:00+00:00", home, away)


def revision_count(db: sqlite3.Connection) -> int:
    return int(
        db.execute("SELECT COUNT(*) AS n FROM draw_result_revisions").fetchone()["n"]
    )


def test_first_write_null_halves_are_unknown(db: sqlite3.Connection) -> None:
    fixture = make_fixture(db)
    import_draw_results(
        db, [DrawResultInput(fixture_id=fixture, home_goals=2, away_goals=0)]
    )
    row = rs.get_draw_result(db, fixture)
    assert row["half_home_goals"] is None
    assert row["half_away_goals"] is None


def test_correction_keeps_stored_halves(db: sqlite3.Connection) -> None:
    fixture = make_fixture(db)
    import_draw_results(
        db,
        [
            DrawResultInput(
                fixture_id=fixture,
                home_goals=2,
                away_goals=0,
                half_home_goals=1,
                half_away_goals=0,
            )
        ],
    )
    corrected = DrawResultInput(
        fixture_id=fixture, home_goals=0, away_goals=1, correction_reason="客队逆转"
    )
    import_draw_results(db, [corrected])
    row = rs.get_draw_result(db, fixture)
    assert (row["home_goals"], row["away_goals"]) == (0, 1)
    assert (row["half_home_goals"], row["half_away_goals"]) == (1, 0)  # 保持现值
    revision = db.execute(
        "SELECT replacement FROM draw_result_revisions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    replacement = json.loads(revision["replacement"])
    assert (replacement["half_home_goals"], replacement["half_away_goals"]) == (
        1,
        0,
    )  # 修订记录真实终态，残余审计（previous 有半场/replacement null）恒 0


def test_noop_correction_without_halves_returns_early(db: sqlite3.Connection) -> None:
    """同全场、null 半场的更正=无变化早退——不要求 correction_reason、不落修订。"""
    fixture = make_fixture(db)
    import_draw_results(
        db,
        [
            DrawResultInput(
                fixture_id=fixture,
                home_goals=2,
                away_goals=0,
                half_home_goals=1,
                half_away_goals=0,
            )
        ],
    )
    before = revision_count(db)
    import_draw_results(
        db, [DrawResultInput(fixture_id=fixture, home_goals=2, away_goals=0)]
    )
    assert revision_count(db) == before


def test_hafu_bet_preview_agrees_with_import(db: sqlite3.Connection) -> None:
    """票面验收：已结 hafu 注仅更正全场 → 半场保持 → 重结算，预览=导入真实资金差。"""
    fixture = make_fixture(db)
    bet = create_bet_with_legs(
        db,
        BetDraft(
            mode=BetMode.PAPER,
            stake=20,
            legs=[
                LegInput(
                    fixture_id=fixture,
                    market_code="hafu",
                    selection_code="hh",
                    locked_odds=3,
                )
            ],
        ),
    )
    import_draw_results(
        db,
        [
            DrawResultInput(
                fixture_id=fixture,
                home_goals=2,
                away_goals=0,
                half_home_goals=1,
                half_away_goals=0,
            )
        ],
    )
    run_settlement(db)
    row = bt.get_bet(db, bet)
    assert row["status"] == "won"
    assert row["payout"] == 60.0

    corrected = DrawResultInput(
        fixture_id=fixture, home_goals=0, away_goals=2, correction_reason="改判"
    )
    preview = preview_draw_result_change(db, [corrected])
    affected = preview.affected_bets[0]
    assert affected.status_projected == "lost"  # 非 open/非"—"
    assert affected.payout_projected == 0.0
    assert affected.delta_payout == -60.0
    # 预览的 replacement 显示真实终态（半场保持），非 null
    assert preview.results[0].replacement["half_home_goals"] == 1

    import_draw_results(db, [corrected])
    row = bt.get_bet(db, bet)
    assert row["status"] == "lost"
    assert row["payout"] == 0.0
    kept = rs.get_draw_result(db, fixture)
    assert (kept["half_home_goals"], kept["half_away_goals"]) == (1, 0)


def test_import_payload_rejects_duplicate_fixtures() -> None:
    """票 09 correctness 修复：重复 fixture 载荷在入口即拒——预览(批前快照)与
    导入(逐行重取)在重复 fixture 上会分叉，一个闸门管两面。"""
    from pydantic import ValidationError

    from goalx_backend.api.results import DrawResultImport

    with pytest.raises(ValidationError, match="fixture_id 重复"):
        DrawResultImport(
            results=[
                {"fixture_id": 1, "home_goals": 2, "away_goals": 0},
                {"fixture_id": 1, "home_goals": 0, "away_goals": 2},
            ]
        )
