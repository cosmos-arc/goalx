"""冻结决策快照与执行对账（caliber-loop 票 06）的行为断言。"""

from __future__ import annotations

import json
import sqlite3

from goalx_backend.betting import frozen as fz
from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.models import MatchCodeInput, SaleStatusInput, SnapshotInput, Tier

KICKOFF = "2026-10-17T18:00:00+00:00"
AS_OF = "2026-10-16T14:04:00+00:00"
BUSINESS_DATE = "2026-10-16"
SOURCE_TIME = "2026-10-16T14:00:00+00:00"
OBSERVED = "2026-10-16T14:03:00+00:00"
STALE_TIME = "2026-10-15T02:00:00+00:00"


def _seed_fixture(db: sqlite3.Connection, n: int, *, kickoff: str = KICKOFF) -> int:
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"主队{n}")
    away = fx_store.upsert_team(db, f"客队{n}")
    fixture = fx_store.upsert_fixture(db, competition, kickoff, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date=BUSINESS_DATE,
            code=f"周六{n:03d}",
        ),
    )
    db.commit()
    return fixture


def _add_quotes(
    db: sqlite3.Connection,
    fixture: int,
    jc: dict[str, float],
    anchor: dict[str, float],
    *,
    stale_anchor: bool = False,
) -> None:
    for sel, odds in jc.items():
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="sporttery",
                odds=odds,
                captured_at=SOURCE_TIME,
                observed_at=OBSERVED,
                source_updated_at=SOURCE_TIME,
            ),
        )
    anchor_time = STALE_TIME if stale_anchor else SOURCE_TIME
    for sel, odds in anchor.items():
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="odds_api:pinnacle",
                odds=odds,
                captured_at=anchor_time,
                observed_at=anchor_time,
                source_updated_at=anchor_time,
            ),
        )
    db.commit()


def _add_sale(
    db: sqlite3.Connection, fixture: int, *, single: bool | None = True
) -> None:
    fx_store.append_sale_status(
        db,
        SaleStatusInput(
            fixture_id=fixture,
            market_code="had",
            sale_state="on_sale",
            single_eligible=single,
            observed_at=OBSERVED,
        ),
    )
    db.commit()


def _seed_draw(db: sqlite3.Connection, fixture: int, home: int, away: int) -> None:
    db.execute(
        "INSERT INTO draw_results (fixture_id, home_goals, away_goals, void, source,"
        " created_at) VALUES (?, ?, ?, 0, 'test', '2026-10-18T00:00:00+00:00')",
        (fixture, home, away),
    )
    db.commit()


def test_frozen_five_reason_paths(db: sqlite3.Connection) -> None:
    """五原因码路径：stale_quote/no_edge/ineligible/below_threshold/would_bet。"""
    from goalx_backend.betting.funnel import record_funnel_day

    # would_bet：jc h=3.0 锚 h=2.0 → p_sharp_h≈0.49 → proxy=3.0×0.49−1>0
    winner = _seed_fixture(db, 0)
    _add_quotes(
        db, winner, {"h": 3.0, "d": 3.3, "a": 3.5}, {"h": 2.0, "d": 3.4, "a": 4.0}
    )
    _add_sale(db, winner)
    # below_threshold：默认负代理（jc≈锚 → proxy≈−haircut 面）
    loser = _seed_fixture(db, 1)
    _add_quotes(
        db, loser, {"h": 2.1, "d": 3.4, "a": 3.2}, {"h": 2.2, "d": 3.5, "a": 3.3}
    )
    _add_sale(db, loser)
    # stale_quote：锚陈旧（L3 流失）
    stale = _seed_fixture(db, 2)
    _add_quotes(
        db,
        stale,
        {"h": 2.0, "d": 3.3, "a": 3.5},
        {"h": 2.2, "d": 3.4, "a": 3.6},
        stale_anchor=True,
    )
    _add_sale(db, stale)
    # ineligible：单固不合规（L5 流失）
    not_single = _seed_fixture(db, 3)
    _add_quotes(
        db, not_single, {"h": 2.0, "d": 3.3, "a": 3.5}, {"h": 2.2, "d": 3.4, "a": 3.6}
    )
    _add_sale(db, not_single, single=False)

    record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    report = fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)
    # 4 场 × 3 选向 = 12 卡
    assert report.n_cards == 12
    assert report.by_reason == {
        "would_bet": 1,  # winner 的 h 侧
        "below_threshold": 5,  # winner d/a + loser 三向
        "stale_quote": 3,  # stale 三向
        "ineligible": 3,  # not_single 三向
    }
    assert report.n_would_bet == 1
    assert report.would_bet_proxy_stats["n"] == 1
    assert report.would_bet_proxy_stats["mean"] is not None
    assert report.would_bet_proxy_stats["mean"] > 0
    # 规则版本进记录；不写 bets 表
    row = db.execute(
        "SELECT * FROM frozen_decisions WHERE decision='would_bet'"
    ).fetchone()
    assert row["rule_version"] == fz.RULE_VERSION
    assert row["odds"] == 3.0
    assert db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 0


def test_frozen_no_edge_path(db: sqlite3.Connection) -> None:
    """no_edge：L4 估值缺（构造 p_sharp 缺——jc 齐但锚被剔后 valuation 空）。"""
    from goalx_backend.betting.funnel import record_funnel_day

    fixture = _seed_fixture(db, 0)
    _add_quotes(
        db, fixture, {"h": 2.1, "d": 3.4, "a": 3.2}, {"h": 2.2, "d": 3.5, "a": 3.3}
    )
    _add_sale(db, fixture)
    record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    # 人工把 p_sharp 置空模拟估值缺（L4 面）——重跑冻结
    db.execute(
        "UPDATE funnel_fixture_days SET p_sharp_h=NULL, p_sharp_d=NULL, p_sharp_a=NULL,"
        " value_proxy_h=NULL, value_proxy_d=NULL, value_proxy_a=NULL"
    )
    db.commit()
    report = fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)
    assert report.by_reason.get("no_edge") == 3


def test_frozen_reconciliation_traceable(db: sqlite3.Connection) -> None:
    """would_bet 对账：结算面+收盘漂移+漏斗收盘列回填；幂等重跑。"""
    from goalx_backend.betting.funnel import record_funnel_day

    fixture = _seed_fixture(db, 0)
    _add_quotes(
        db, fixture, {"h": 3.0, "d": 3.3, "a": 3.5}, {"h": 2.0, "d": 3.4, "a": 4.0}
    )
    _add_sale(db, fixture)
    record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)
    assert (
        db.execute(
            "SELECT COUNT(*) FROM frozen_decisions WHERE decision='would_bet'"
        ).fetchone()[0]
        == 1
    )

    def close_provider(home, away, kickoff, selection):
        return (0.52, "srct:1x2:177", "s1")

    # 无赛果：只回填收盘面，still_pending
    stats = fz.reconcile_frozen_decisions(
        db, close_provider, now="2026-10-17T00:00:00+00:00"
    )
    assert stats["still_pending"] == 1
    assert stats["closed"] == 1
    row = db.execute(
        "SELECT p_sharp_close, drift_after_lock, settled_outcome FROM frozen_decisions"
        " WHERE decision='would_bet'"
    ).fetchone()
    assert row["p_sharp_close"] == 0.52
    assert row["drift_after_lock"] is not None
    assert row["settled_outcome"] is None

    # 赛果落地（主胜 2-1）：won + 平注利润 = odds−1
    _seed_draw(db, fixture, 2, 1)
    stats2 = fz.reconcile_frozen_decisions(
        db, close_provider, now="2026-10-18T00:00:00+00:00"
    )
    assert stats2["settled"] == 1
    assert stats2["paper_profit_flat"] > 0
    row2 = db.execute(
        "SELECT settled_outcome, settled_profit, reconciled_at FROM frozen_decisions"
        " WHERE decision='would_bet'"
    ).fetchone()
    assert row2["settled_outcome"] == "won"
    assert row2["settled_profit"] == round(3.0 - 1.0, 4)
    # 漏斗明细收盘列回填（可追溯至收盘）
    frow = db.execute(
        "SELECT p_sharp_close_h FROM funnel_fixture_days WHERE fixture_id=?",
        (fixture,),
    ).fetchone()
    assert frow["p_sharp_close_h"] == 0.52
    # 幂等：重跑不再结算（settled_outcome 已置，pending 查询排除）
    stats3 = fz.reconcile_frozen_decisions(
        db, close_provider, now="2026-10-19T00:00:00+00:00"
    )
    assert stats3["considered"] == 0


def test_frozen_summary_and_idempotent_day(db: sqlite3.Connection) -> None:
    """日终合并报告面 + 同业务日重跑幂等。"""
    from goalx_backend.betting.funnel import record_funnel_day

    fixture = _seed_fixture(db, 0)
    _add_quotes(
        db, fixture, {"h": 3.0, "d": 3.3, "a": 3.5}, {"h": 2.0, "d": 3.4, "a": 4.0}
    )
    _add_sale(db, fixture)
    record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)
    fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)  # 幂等
    assert db.execute("SELECT COUNT(*) FROM frozen_decisions").fetchone()[0] == 3
    summary = fz.frozen_summary(db, BUSINESS_DATE)
    assert summary["frozen_by_reason"] == {
        "would_bet:would_bet": 1,
        "not:below_threshold": 2,
    }
    assert summary["funnel_on_sale"] == 1
    assert "不自动写 bets" in summary["note"]


def test_settings_free() -> None:
    """依赖面守卫（Settings 可构）。"""
    assert Settings() is not None
    assert json.dumps({"ok": 1})


def test_frozen_dual_would_bet_selections_close_backfill(
    db: sqlite3.Connection,
) -> None:
    """同场双 would_bet 选向：两选向漏斗收盘列各自回填（correctness 修）。"""
    from goalx_backend.betting.funnel import record_funnel_day

    fixture = _seed_fixture(db, 0)
    # 双正代理：h 与 d 都 O_JC×p_sharp−1>0（锚宽 JC 更宽）
    _add_quotes(
        db,
        fixture,
        {"h": 3.0, "d": 3.6, "a": 3.5},
        {"h": 2.0, "d": 3.0, "a": 4.0},
    )
    _add_sale(db, fixture)
    record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    report = fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)
    assert report.by_reason.get("would_bet") == 2  # h 与 d

    def close_provider(home, away, kickoff, selection):
        return (0.52, "srct:1x2:177", "s1")

    _seed_draw(db, fixture, 1, 1)  # 平局：d 中 h 失
    fz.reconcile_frozen_decisions(db, close_provider, now="2026-10-18T00:00:00+00:00")
    frow = db.execute(
        "SELECT p_sharp_close_h, p_sharp_close_d FROM funnel_fixture_days"
        " WHERE fixture_id = ?",
        (fixture,),
    ).fetchone()
    assert frow["p_sharp_close_h"] == 0.52  # 两选向各自回填（守卫按目标列）
    assert frow["p_sharp_close_d"] == 0.52
