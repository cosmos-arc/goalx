"""Regression scenarios for real-money recording and result corrections."""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend.ingest.results import import_draw_results
from goalx_backend.models import BetMode, DrawResultInput, LegInput
from goalx_backend.services import (
    BetDraft,
    create_bet_with_legs,
    record_purchase,
    run_settlement,
)
from goalx_backend.settlement import LegSpec, ResultFacts, settle_fixed_bet
from goalx_backend.store import betting as bt
from goalx_backend.store import fixtures as fx


def make_bet(db: sqlite3.Connection, mode: BetMode = BetMode.LIVE) -> tuple[int, int]:
    competition = fx.upsert_competition(db, "test")
    home = fx.upsert_team(db, "home")
    away = fx.upsert_team(db, "away")
    fixture = fx.upsert_fixture(
        db, competition, "2026-09-01T12:00:00+00:00", home, away
    )
    bet = create_bet_with_legs(
        db,
        BetDraft(
            mode=mode,
            stake=10,
            legs=[
                LegInput(
                    fixture_id=fixture,
                    market_code="had",
                    selection_code="h",
                    locked_odds=2,
                )
            ],
        ),
    )
    return bet, fixture


@pytest.mark.parametrize(("goals", "payout"), [(2, 3.6), (0, 0)])
def test_two_leg_void_keeps_remaining_result(goals: int, payout: float) -> None:
    outcome = settle_fixed_bet(
        2,
        [
            LegSpec(
                fixture_id=1, market_code="had", selection_code="h", locked_odds=1.8
            ),
            LegSpec(fixture_id=2, market_code="had", selection_code="h", locked_odds=2),
        ],
        {
            1: ResultFacts(home_goals=goals, away_goals=1),
            2: ResultFacts(home_goals=0, away_goals=0, void=True),
        },
    )
    assert outcome.payout == pytest.approx(payout)
    assert outcome.status == ("won" if payout else "lost")


def test_unpurchased_live_never_receives_money(db: sqlite3.Connection) -> None:
    bet, fixture = make_bet(db)
    import_draw_results(
        db, [DrawResultInput(fixture_id=fixture, home_goals=2, away_goals=0)]
    )
    run_settlement(db)
    assert (bt.bankroll_balance(db) or 0) == 0
    assert bt.get_bet(db, bet)["purchased"] == 0


@pytest.mark.parametrize("duplicate_in_request", [True, False])
def test_purchase_cannot_debit_twice(
    db: sqlite3.Connection, duplicate_in_request: bool
) -> None:
    bet, _ = make_bet(db)
    if duplicate_in_request:
        with pytest.raises(ValueError):
            record_purchase(db, [bet, bet])
        assert bt.bankroll_balance(db) is None
        assert bt.list_slips(db) == []
    else:
        slip = record_purchase(db, [bet])
        with pytest.raises(ValueError):
            record_purchase(db, [bet])
        assert bt.bankroll_balance(db) == -10
        assert bt.get_bet(db, bet)["slip_id"] == slip
        assert len(bt.list_slips(db)) == 1


def test_result_correction_reverses_money_once(db: sqlite3.Connection) -> None:
    bet, fixture = make_bet(db)
    record_purchase(db, [bet])
    import_draw_results(
        db, [DrawResultInput(fixture_id=fixture, home_goals=2, away_goals=0)]
    )
    run_settlement(db)
    assert bt.bankroll_balance(db) == 10
    corrected = DrawResultInput(
        fixture_id=fixture,
        home_goals=0,
        away_goals=2,
        correction_reason="official correction",
    )
    import_draw_results(db, [corrected])
    assert bt.get_bet(db, bet)["status"] == "lost"
    assert bt.bankroll_balance(db) == -10
    count = len(bt.list_bankroll_events(db))
    import_draw_results(db, [corrected])
    run_settlement(db)
    assert len(bt.list_bankroll_events(db)) == count
    assert bt.bankroll_balance(db) == -10


@pytest.mark.parametrize("mode", [BetMode.PAPER, BetMode.LIVE])
def test_loss_to_win_correction_and_audit(
    db: sqlite3.Connection, mode: BetMode
) -> None:
    from goalx_backend.ledger_audit import audit_ledger

    bet, fixture = make_bet(db, mode)
    record_purchase(db, [bet])
    import_draw_results(
        db, [DrawResultInput(fixture_id=fixture, home_goals=0, away_goals=2)]
    )
    run_settlement(db)
    before = bt.bankroll_balance(db) or 0
    import_draw_results(
        db,
        [
            DrawResultInput(
                fixture_id=fixture,
                home_goals=2,
                away_goals=0,
                correction_reason="official revision",
            )
        ],
    )
    assert bt.get_bet(db, bet)["payout"] == 20
    assert (bt.bankroll_balance(db) or 0) - before == (
        20 if mode is BetMode.LIVE else 0
    )
    assert audit_ledger(db)["manual_review"] == []
    revision = db.execute("SELECT * FROM draw_result_revisions").fetchone()
    assert revision["reason"] == "official revision"
    assert len(db.execute("SELECT * FROM settlement_revisions").fetchall()) == 2
    for table in ("draw_result_revisions", "settlement_revisions"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(f"DELETE FROM {table}")


def test_correction_without_reason_rolls_back_batch(db: sqlite3.Connection) -> None:
    bet, fixture = make_bet(db)
    record_purchase(db, [bet])
    import_draw_results(
        db, [DrawResultInput(fixture_id=fixture, home_goals=2, away_goals=0)]
    )
    run_settlement(db)
    before = list(db.iterdump())
    with pytest.raises(ValueError, match="correction_reason"):
        import_draw_results(
            db,
            [
                DrawResultInput(
                    fixture_id=fixture,
                    home_goals=1,
                    away_goals=0,
                    correction_reason="valid change",
                ),
                DrawResultInput(fixture_id=fixture, home_goals=0, away_goals=2),
            ],
        )
    assert list(db.iterdump()) == before


@pytest.mark.parametrize("operation", ["purchase", "settlement", "correction"])
def test_ledger_write_failure_rolls_back_operation(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    bet, fixture = make_bet(db)
    if operation != "purchase":
        record_purchase(db, [bet])
        import_draw_results(
            db, [DrawResultInput(fixture_id=fixture, home_goals=2, away_goals=0)]
        )
    if operation == "correction":
        run_settlement(db)
    db.commit()
    before = list(db.iterdump())
    original = bt.record_bankroll_event

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(bt, "record_bankroll_event", fail_after_write)

    def invoke() -> None:
        if operation == "purchase":
            record_purchase(db, [bet])
        elif operation == "settlement":
            run_settlement(db)
        else:
            import_draw_results(
                db,
                [
                    DrawResultInput(
                        fixture_id=fixture,
                        home_goals=0,
                        away_goals=2,
                        correction_reason="revision",
                    )
                ],
            )

    with pytest.raises(RuntimeError, match="disk failure"):
        invoke()
    assert list(db.iterdump()) == before


def test_invalid_purchase_batch_and_bet_creation_are_atomic(
    db: sqlite3.Connection,
) -> None:
    bet, fixture = make_bet(db)
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(LookupError):
        record_purchase(db, [bet, 999])
    assert list(db.iterdump()) == before
    with pytest.raises(sqlite3.IntegrityError):
        create_bet_with_legs(
            db,
            BetDraft(
                mode=BetMode.LIVE,
                stake=10,
                legs=[
                    LegInput(
                        fixture_id=fixture,
                        market_code="had",
                        selection_code="h",
                        locked_odds=2,
                    ),
                    LegInput(
                        fixture_id=999,
                        market_code="had",
                        selection_code="h",
                        locked_odds=2,
                    ),
                ],
            ),
        )
    assert list(db.iterdump()) == before


def test_concurrent_purchase_has_one_winner(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from goalx_backend.db import connect, migrate

    path = tmp_path / "concurrent.db"
    conn = connect(path)
    migrate(conn)
    bet, _ = make_bet(conn)
    conn.commit()
    barrier = Barrier(2)

    def purchase() -> str:
        local = connect(path)
        try:
            barrier.wait(timeout=5)
            record_purchase(local, [bet])
            return "created"
        except ValueError:
            return "rejected"
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: purchase(), range(2)))
    assert sorted(outcomes) == ["created", "rejected"]
    assert bt.bankroll_balance(conn) == -10
    assert len(bt.list_slips(conn)) == 1
    conn.close()


def test_correction_can_reopen_then_finalize_without_double_payment(
    db: sqlite3.Connection,
) -> None:
    bet, fixture = make_bet(db)
    db.execute(
        "UPDATE bet_legs SET market_code='hafu', selection_code='hh' WHERE bet_id=?",
        (bet,),
    )
    db.commit()
    record_purchase(db, [bet])
    full = DrawResultInput(
        fixture_id=fixture,
        home_goals=2,
        away_goals=0,
        half_home_goals=1,
        half_away_goals=0,
    )
    import_draw_results(db, [full])
    run_settlement(db)
    assert bt.bankroll_balance(db) == 10
    import_draw_results(
        db,
        [
            DrawResultInput(
                fixture_id=fixture,
                home_goals=2,
                away_goals=0,
                correction_reason="half time under review",
            )
        ],
    )
    row = bt.get_bet(db, bet)
    assert row["status"] == "open"
    assert row["settled_at"] is None
    assert row["payout"] is None
    assert bt.bankroll_balance(db) == -10
    import_draw_results(
        db, [full.model_copy(update={"correction_reason": "half time confirmed"})]
    )
    assert bt.get_bet(db, bet)["status"] == "won"
    assert bt.bankroll_balance(db) == 10
    run_settlement(db)
    assert bt.bankroll_balance(db) == 10


def test_legacy_anomalies_are_reported_without_repair(db: sqlite3.Connection) -> None:
    from goalx_backend.ledger_audit import audit_ledger

    bet, fixture = make_bet(db)
    import_draw_results(
        db, [DrawResultInput(fixture_id=fixture, home_goals=2, away_goals=0)]
    )
    run_settlement(db)
    bt.record_bankroll_event(db, "bet_payout", 20, bet_id=bet)
    db.commit()
    before = list(db.iterdump())
    db.execute("PRAGMA query_only=ON")
    report = audit_ledger(db)
    assert report["repairs_applied"] is False
    assert "events_without_live_purchase" in str(report["manual_review"])
    assert list(db.iterdump()) == before
    db.execute("PRAGMA query_only=OFF")
    with pytest.raises(ValueError, match="audit-ledger"):
        import_draw_results(
            db,
            [
                DrawResultInput(
                    fixture_id=fixture,
                    home_goals=0,
                    away_goals=2,
                    correction_reason="official correction",
                )
            ],
        )
    assert list(db.iterdump()) == before


def test_nested_rollback_preserves_callers_uncommitted_work(
    db: sqlite3.Connection,
) -> None:
    bet, _ = make_bet(db)
    before = list(db.iterdump())
    with pytest.raises(LookupError):
        record_purchase(db, [bet, 999])
    assert list(db.iterdump()) == before
    assert db.in_transaction


def test_correction_does_not_silently_repair_legacy_rule_errors(
    db: sqlite3.Connection,
) -> None:
    bet, fixture = make_bet(db, BetMode.PAPER)
    import_draw_results(
        db, [DrawResultInput(fixture_id=fixture, home_goals=2, away_goals=0)]
    )
    run_settlement(db)
    db.execute("UPDATE bets SET status='void', payout=10, profit=0 WHERE id=?", (bet,))
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(ValueError, match="audit-ledger"):
        import_draw_results(
            db,
            [
                DrawResultInput(
                    fixture_id=fixture,
                    home_goals=3,
                    away_goals=0,
                    correction_reason="official revision",
                )
            ],
        )
    assert list(db.iterdump()) == before
