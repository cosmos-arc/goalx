"""Regression scenarios for real-money recording and result corrections."""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend.betting import store as bt
from goalx_backend.betting.bets import (
    ActualLegOdds,
    ActualTerms,
    BetDraft,
    IntentConflictError,
    create_bet_with_legs,
    intent_fingerprint,
    record_purchase,
)
from goalx_backend.betting.settle import run_settlement
from goalx_backend.data import fixtures as fx
from goalx_backend.data.ingest.results import import_draw_results
from goalx_backend.models import BetMode, DrawResultInput, LegInput
from goalx_backend.settlement import LegSpec, ResultFacts, settle_fixed_bet


def _purchase_paper_unchecked(db: sqlite3.Connection, bet: int) -> None:
    """paper 直写票态：record_purchase 已带 had 资格校验（审计批C 附录④，
    按当下时钟判开赛），本文件 paper 注挂 2026-09 固定开球场（结算/台账
    焦点）恒拒——直写 slip/attach 与校验前路径等价（paper 无 bankroll 事件）。"""
    from goalx_backend.db import utc_now_iso

    slip = bt.create_slip(db, BetMode.PAPER)
    bt.attach_bets_to_slip(db, slip, [bet], utc_now_iso())


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
    from goalx_backend.betting.ledger_audit import audit_ledger

    bet, fixture = make_bet(db, mode)
    if mode is BetMode.PAPER:
        _purchase_paper_unchecked(db, bet)
    else:
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


def test_correction_reverse_and_refinalize_keeps_halves_without_double_payment(
    db: sqlite3.Connection,
) -> None:
    """票 09 语义更新：null 半场=保持现值（原 null=撤销半场→reopen 的路径废除）。

    资金一次性不变量不变：won→lost 冲回一次，再改判回 won 重付一次，
    全程无双付。"""
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
                home_goals=0,
                away_goals=2,
                correction_reason="客胜改判",
            )
        ],
    )
    row = bt.get_bet(db, bet)
    assert row["status"] == "lost"  # 半场保持 (1,0) → hafu ha≠hh，不 reopen
    assert row["payout"] == 0.0
    assert bt.bankroll_balance(db) == -10
    import_draw_results(
        db, [full.model_copy(update={"correction_reason": "再改判回主胜"})]
    )
    assert bt.get_bet(db, bet)["status"] == "won"
    assert bt.bankroll_balance(db) == 10
    run_settlement(db)
    assert bt.bankroll_balance(db) == 10


def test_legacy_anomalies_are_reported_without_repair(db: sqlite3.Connection) -> None:
    from goalx_backend.betting.ledger_audit import audit_ledger

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


def test_paper_lock_with_actuals_rejected_zero_write(
    db: sqlite3.Connection,
) -> None:
    """票 01 验收 1：paper 锁票携带 actuals 整票拒绝——票/时间线/actual
    列零部分写入；无 actuals 的正常 paper 路径不受影响。"""
    bet, fixture = make_bet(db, BetMode.PAPER)
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(ValueError, match="paper 注不接受实际条款"):
        record_purchase(
            db,
            [bet],
            actuals={
                bet: ActualTerms(
                    stake=200, leg_odds=[ActualLegOdds(fixture_id=fixture, odds=99)]
                )
            },
        )
    assert list(db.iterdump()) == before
    _purchase_paper_unchecked(db, bet)
    assert bt.get_bet(db, bet)["purchased"] == 1


def test_paper_actuals_never_reach_formal_results(db: sqlite3.Connection) -> None:
    """票 01 验收 4（报告一案例 1 贯通反例）：system_paper 行即使携带
    actual 99（修复前异常旧行），结算与正式成绩全链按绑定价。"""
    from goalx_backend.betting.ledger_audit import audit_ledger

    bet, fixture = make_bet(db, BetMode.PAPER)
    db.execute("UPDATE bets SET price_source='system_paper' WHERE id=?", (bet,))
    db.commit()
    _purchase_paper_unchecked(db, bet)
    bt.set_actual_terms(db, bet, 200, {fixture: 99})  # 模拟旧行落列
    import_draw_results(
        db, [DrawResultInput(fixture_id=fixture, home_goals=2, away_goals=0)]
    )
    run_settlement(db)
    row = bt.get_bet(db, bet)
    assert row["status"] == "won"
    assert row["payout"] == pytest.approx(20.0)  # 10×2 绑定价, 非 200×99
    assert bt.settled_purchased_bets(db) == []  # 异常行不进正式分母
    assert "paper_actual_terms_anomaly" in str(audit_ledger(db)["manual_review"])


def test_correction_converges_legacy_paper_actuals_settlement(
    db: sqlite3.Connection,
) -> None:
    """票 01 correctness F1：已结 paper+actuals 旧行的更正通道不被砖死——
    旧账与修复前口径一致时 validate 放行，resettle 按绑定价收敛并留修订。"""
    from goalx_backend.betting.ledger_audit import audit_ledger
    from goalx_backend.betting.settle import evaluate_bet, preview_draw_result_change
    from goalx_backend.models import BetStatus, SettlementInput
    from goalx_backend.settlement import detail_payload

    bet, fixture = make_bet(db, BetMode.PAPER)
    db.execute("UPDATE bets SET price_source='system_paper' WHERE id=?", (bet,))
    db.commit()
    _purchase_paper_unchecked(db, bet)
    bt.set_actual_terms(db, bet, 200, {fixture: 99})  # 旧行落列
    import_draw_results(
        db, [DrawResultInput(fixture_id=fixture, home_goals=2, away_goals=0)]
    )
    # 模拟修复前引擎按 actuals 落的旧账（200×99=19800）
    legacy = evaluate_bet(db, bt.get_bet(db, bet), legacy_paper_actuals=True)
    assert legacy.payout == pytest.approx(19800.0)
    bt.save_settlement(
        db,
        SettlementInput(
            bet_id=bet,
            status=BetStatus(legacy.status),
            stake=legacy.stake,
            payout=legacy.payout,
            profit=legacy.profit,
            detail=detail_payload(legacy),
        ),
        reason="pre-fix actual-terms settlement",
    )
    db.commit()
    corrected = DrawResultInput(
        fixture_id=fixture, home_goals=0, away_goals=2, correction_reason="revision"
    )
    preview = preview_draw_result_change(db, [corrected])
    assert preview.affected_bets[0].payout_projected == 0.0  # 预览=绑定价投影
    import_draw_results(db, [corrected])  # 修复前此处被 validate 砖死
    row = bt.get_bet(db, bet)
    assert row["status"] == "lost"  # 收敛到绑定价口径
    assert row["payout"] == 0.0
    revisions = db.execute("SELECT * FROM settlement_revisions ORDER BY id").fetchall()
    assert len(revisions) == 2  # 旧账 + 更正收敛, 历史入修订不丢失
    assert "paper_actual_terms_anomaly" in str(audit_ledger(db)["manual_review"])


def test_review_excludes_paper_actuals_anomaly(db: sqlite3.Connection) -> None:
    """票 01 correctness F2：异常旧行在复盘面与正式分母同边界。"""
    from goalx_backend.evaluation.bet_review import review_views

    bet, fixture = make_bet(db, BetMode.PAPER)
    db.execute("UPDATE bets SET price_source='system_paper' WHERE id=?", (bet,))
    db.commit()
    _purchase_paper_unchecked(db, bet)
    bt.set_actual_terms(db, bet, 200, {fixture: 99})
    view = review_views(db, [bt.get_bet(db, bet)])[bet]
    assert view.forward == "excluded_actual_terms"


def test_same_key_race_returns_existing_bet(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """票 06 correctness F6a：预检后、INSERT 前并发落同键注——竞态败方
    回带既有注 id，不落双行，事务状态干净。"""
    _bet, fixture = make_bet(db)
    draft = BetDraft(
        mode=BetMode.LIVE,
        stake=10,
        legs=[
            LegInput(
                fixture_id=fixture, market_code="had", selection_code="h", locked_odds=2
            )
        ],
        client_request_id="race-key-1",
    )
    original_create = bt.create_bet

    def racing_create(conn: sqlite3.Connection, *args: object, **kwargs: object) -> int:
        # 预检已过、本 INSERT 前：另一请求先用同键落注（无腿裸行即可）
        if not conn.execute(
            "SELECT 1 FROM bets WHERE client_request_id = ?", ("race-key-1",)
        ).fetchone():
            conn.execute(
                "INSERT INTO bets (mode, market_kind, stake, created_at,"
                " client_request_id) VALUES"
                " ('live', 'fixed', 10, '2026-11-04T00:00:00+00:00', 'race-key-1')"
            )
        return original_create(conn, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bt, "create_bet", racing_create)
    returned = create_bet_with_legs(db, draft)
    rows = db.execute(
        "SELECT id FROM bets WHERE client_request_id = 'race-key-1'"
    ).fetchall()
    assert len(rows) == 1  # 不落双行
    assert returned == int(rows[0]["id"])  # 回带既有
    db.commit()  # 竞态处理后连接可继续正常工作（无残留失败语句）


def test_same_key_race_intent_conflict_raises(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """票 09：同键异意图在竞态窗与串行预检两路都不静默回带——域层抛
    IntentConflictError（HTTP 层映射 409）；无指纹竞态行（存量）仍回带。"""
    _bet, fixture = make_bet(db)
    legs = [
        LegInput(
            fixture_id=fixture, market_code="had", selection_code="h", locked_odds=2
        )
    ]
    draft = BetDraft(
        mode=BetMode.LIVE,
        stake=10,
        legs=legs,
        client_request_id="race-key-2",
        # 本次提交意图指纹（paper/¥2）
        intent_fingerprint=intent_fingerprint(BetMode.PAPER, 2.0, legs),
    )
    raced_fp = intent_fingerprint(BetMode.LIVE, 200.0, legs)  # 既有注：live/¥200
    original_create = bt.create_bet

    def racing_create(conn: sqlite3.Connection, *args: object, **kwargs: object) -> int:
        if not conn.execute(
            "SELECT 1 FROM bets WHERE client_request_id = ?", ("race-key-2",)
        ).fetchone():
            conn.execute(
                "INSERT INTO bets (mode, market_kind, stake, created_at,"
                " client_request_id, intent_fingerprint) VALUES"
                " ('live', 'fixed', 200, '2026-11-04T00:00:00+00:00',"
                " 'race-key-2', ?)",
                (raced_fp,),
            )
        return original_create(conn, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bt, "create_bet", racing_create)
    # UNIQUE 竞态路：指纹不符 → IntentConflictError，不回带
    with pytest.raises(IntentConflictError) as race_exc:
        create_bet_with_legs(db, draft)
    assert race_exc.value.stake == 200.0
    # 串行预检路（既有行已在库）：同抛
    with pytest.raises(IntentConflictError):
        create_bet_with_legs(db, draft)
    db.commit()


def test_intent_fingerprint_canonicalization() -> None:
    """票 09 correctness F1/F3：规范形哈希的健壮性——混合 goal_line 排序
    不抛、int/float 同值同哈希、腿序无关。"""
    leg_none = LegInput(
        fixture_id=1, market_code="had", selection_code="h", locked_odds=2
    )
    leg_line = LegInput(
        fixture_id=2,
        market_code="had",
        selection_code="h",
        locked_odds=2,
        goal_line=2.5,
    )
    # 混合 goal_line（None/float 并存）不抛 TypeError（F1）
    assert intent_fingerprint(BetMode.PAPER, 2.0, [leg_none, leg_line])
    # int/float 同值同哈希（F3：直传 int 的未来调用者不产假 409）
    assert intent_fingerprint(BetMode.PAPER, 2, [leg_none]) == intent_fingerprint(
        BetMode.PAPER, 2.0, [leg_none]
    )
    # 腿序无关（串关 A+B 与 B+A 同意图）
    assert intent_fingerprint(BetMode.PAPER, 2.0, [leg_none, leg_line]) == (
        intent_fingerprint(BetMode.PAPER, 2.0, [leg_line, leg_none])
    )


def test_other_unique_violation_still_raises(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """票 06 correctness F6c：非 client_request_id 的唯一冲突照抛——
    列名判别不吞无关 IntegrityError。"""
    _bet, fixture = make_bet(db)

    def raise_other(conn: sqlite3.Connection, *args: object, **kwargs: object) -> int:
        raise sqlite3.IntegrityError("UNIQUE constraint failed: bets.some_other_column")

    monkeypatch.setattr(bt, "create_bet", raise_other)
    with pytest.raises(sqlite3.IntegrityError, match="some_other_column"):
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
                    )
                ],
                client_request_id="k",
            ),
        )
