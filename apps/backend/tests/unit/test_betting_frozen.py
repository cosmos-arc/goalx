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
    source_time: str = SOURCE_TIME,
    observed: str = OBSERVED,
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
                captured_at=source_time,
                observed_at=observed,
                source_updated_at=source_time,
            ),
        )
    anchor_time = STALE_TIME if stale_anchor else source_time
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


def test_frozen_close_backfill_after_settlement(db: sqlite3.Connection) -> None:
    """赛果先到、收盘晚到：盲结算不丢收盘（measure-01 晚场主路径）。"""
    from goalx_backend.betting.funnel import record_funnel_day

    fixture = _seed_fixture(db, 0)
    _add_quotes(
        db, fixture, {"h": 3.0, "d": 3.3, "a": 3.5}, {"h": 2.0, "d": 3.4, "a": 4.0}
    )
    _add_sale(db, fixture)
    record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)
    _seed_draw(db, fixture, 2, 1)  # 主胜：would_bet 的 h 侧 won

    def no_close(home, away, kickoff, selection):
        return None

    # 第一拍（23:30 wrap）：收盘语料未到 → 盲结算，缺收盘如实计数
    stats = fz.reconcile_frozen_decisions(db, no_close, now="2026-10-17T23:30:00+00:00")
    assert stats["settled"] == 1
    assert stats["missing_close_total"] == 1
    row = db.execute(
        "SELECT settled_outcome, p_sharp_close FROM frozen_decisions"
        " WHERE decision='would_bet'"
    ).fetchone()
    assert row["settled_outcome"] == "won"
    assert row["p_sharp_close"] is None

    def close_provider(home, away, kickoff, selection):
        return (0.52, "srct:1x2:177", "s1")

    # 第二拍（次晨收盘语料到达）：已结算行独立补收盘，不重复结算
    stats2 = fz.reconcile_frozen_decisions(
        db, close_provider, now="2026-10-18T06:00:00+00:00"
    )
    assert stats2["settled"] == 0
    assert stats2["paper_profit_flat"] == 0.0
    assert stats2["close_backfilled"] == 1
    assert stats2["missing_close_total"] == 0
    row2 = db.execute(
        "SELECT p_sharp_close, drift_after_lock, settled_outcome, settled_profit"
        " FROM frozen_decisions WHERE decision='would_bet'"
    ).fetchone()
    assert row2["p_sharp_close"] == 0.52
    assert row2["drift_after_lock"] is not None
    assert row2["settled_outcome"] == "won"  # 结算面未被重算
    assert row2["settled_profit"] == round(3.0 - 1.0, 4)
    frow = db.execute(
        "SELECT p_sharp_close_h FROM funnel_fixture_days WHERE fixture_id=?",
        (fixture,),
    ).fetchone()
    assert frow["p_sharp_close_h"] == 0.52  # 漏斗收盘列同步补齐
    summary = fz.frozen_summary(db, BUSINESS_DATE)
    assert summary["reconciliation"]["missing_close"] == 0

    # 第三拍：结算与收盘全齐 → 扫描集空（幂等）
    stats3 = fz.reconcile_frozen_decisions(
        db, close_provider, now="2026-10-19T00:00:00+00:00"
    )
    assert stats3["considered"] == 0

    # 收盘写一次：已落 close 不被后续拍改写（即使 provider 值变化）
    def moved_close(home, away, kickoff, selection):
        return (0.60, "srct:1x2:177", "s1")

    db.execute(
        "UPDATE frozen_decisions SET settled_outcome = NULL, p_sharp_close = NULL,"
        " reconciled_at = NULL WHERE decision = 'would_bet' AND settled_outcome = 'won'"
    )
    db.commit()
    fz.reconcile_frozen_decisions(
        db, close_provider, now="2026-10-19T01:00:00+00:00"
    )  # 重新结算+落收盘 0.52
    fz.reconcile_frozen_decisions(
        db, moved_close, now="2026-10-19T02:00:00+00:00"
    )  # settled 已回 → 不进扫描集
    row3 = db.execute(
        "SELECT p_sharp_close FROM frozen_decisions WHERE decision='would_bet'"
    ).fetchone()
    assert row3["p_sharp_close"] == 0.52


def test_frozen_funnel_close_heal_after_lost_write(db: sqlite3.Connection) -> None:
    """崩溃窗口丢写治愈：frozen 已有收盘而漏斗列丢失 → 收尾 pass 补齐。"""
    from goalx_backend.betting.funnel import record_funnel_day

    fixture = _seed_fixture(db, 0)
    _add_quotes(
        db, fixture, {"h": 3.0, "d": 3.3, "a": 3.5}, {"h": 2.0, "d": 3.4, "a": 4.0}
    )
    _add_sale(db, fixture)
    record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)

    def close_provider(home, away, kickoff, selection):
        return (0.52, "srct:1x2:177", "s1")

    # 收盘先落（frozen 行有 close），模拟漏斗列写入丢失（崩溃窗口）
    fz.reconcile_frozen_decisions(db, close_provider, now="2026-10-17T00:00:00+00:00")
    db.execute(
        "UPDATE funnel_fixture_days SET p_sharp_close_h = NULL WHERE fixture_id = ?",
        (fixture,),
    )
    db.commit()
    # 赛果落地重跑：frozen close 已在（写一次守卫跳过），漏斗列由治愈 pass 补齐
    _seed_draw(db, fixture, 2, 1)
    stats = fz.reconcile_frozen_decisions(
        db, close_provider, now="2026-10-18T00:00:00+00:00"
    )
    assert stats["settled"] == 1
    assert stats["funnel_close_healed"] == 1
    frow = db.execute(
        "SELECT p_sharp_close_h FROM funnel_fixture_days WHERE fixture_id=?",
        (fixture,),
    ).fetchone()
    assert frow["p_sharp_close_h"] == 0.52


def test_frozen_write_once_and_divergence_ledger(db: sqlite3.Connection) -> None:
    """write-once：重跑不覆盖已落卡；would_bet→not 落分歧账；对账列存活。"""
    from goalx_backend.betting.funnel import record_funnel_day

    fixture = _seed_fixture(db, 0)
    _add_quotes(
        db, fixture, {"h": 3.0, "d": 3.3, "a": 3.5}, {"h": 2.0, "d": 3.4, "a": 4.0}
    )
    _add_sale(db, fixture)
    record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    report = fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)
    assert report.n_divergences == 0

    # 对账先落（close+settle）——旧实现同日重跑会 DELETE 毁掉这些列
    def close_provider(home, away, kickoff, selection):
        return (0.52, "srct:1x2:177", "s1")

    _seed_draw(db, fixture, 2, 1)
    fz.reconcile_frozen_decisions(db, close_provider, now="2026-10-18T00:00:00+00:00")

    # 输入翻转（价值代理转负）→ 重算 not；存储卡不动，分歧落账
    db.execute(
        "UPDATE funnel_fixture_days SET value_proxy_h = -0.05 WHERE fixture_id = ?",
        (fixture,),
    )
    db.commit()
    report2 = fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)
    assert report2.n_divergences == 1
    div = report2.divergences[0]
    assert div["fixture_id"] == fixture
    assert div["selection"] == "h"
    assert div["stored"] == "would_bet"
    assert div["recomputed"] == "not"
    assert div["stored_reason"] == "would_bet"
    assert div["recomputed_reason"] == "below_threshold"
    assert div["status"] == "open"
    assert div["rule_version"] == fz.RULE_VERSION

    # 已落卡未被覆盖：决策与对账回填列原样存活
    row = db.execute(
        "SELECT decision, reason_code, settled_outcome, p_sharp_close, settled_profit"
        " FROM frozen_decisions WHERE fixture_id=? AND selection_code='h'",
        (fixture,),
    ).fetchone()
    assert row["decision"] == "would_bet"
    assert row["reason_code"] == "would_bet"
    assert row["settled_outcome"] == "won"
    assert row["p_sharp_close"] == 0.52
    assert row["settled_profit"] == round(3.0 - 1.0, 4)

    # 分歧账幂等：同分歧再跑不重复；日终汇总带分歧计数
    report3 = fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)
    assert report3.n_divergences == 1
    n = db.execute(
        "SELECT COUNT(*) FROM frozen_decision_divergences WHERE business_date = ?",
        (BUSINESS_DATE,),
    ).fetchone()[0]
    assert n == 1
    summary = fz.frozen_summary(db, BUSINESS_DATE)
    assert summary["frozen_divergences"] == 1
    # 汇总面 ≡ 存储（write-once 权威口径）：重算翻转不带动汇总计数
    assert report3.n_would_bet == 1
    assert report3.by_reason.get("would_bet") == 1
    assert report3.by_reason.get("below_threshold") == 2
    # 输入恢复（代理回正）再跑：分歧收敛——账面仍 1 行但状态转 resolved
    db.execute(
        "UPDATE funnel_fixture_days SET value_proxy_h = 0.4 WHERE fixture_id = ?",
        (fixture,),
    )
    db.commit()
    report4 = fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)
    assert report4.n_divergences == 1
    assert report4.divergences[0]["status"] == "resolved"


def test_frozen_dual_business_date_single_card(db: sqlite3.Connection) -> None:
    """跨业务日重复卡：一场两日在售只冻结最早日 would_bet，对账只结算一次。"""
    from goalx_backend.betting.funnel import record_funnel_day

    fixture = _seed_fixture(db, 0)
    _add_quotes(
        db, fixture, {"h": 3.0, "d": 3.3, "a": 3.5}, {"h": 2.0, "d": 3.4, "a": 4.0}
    )
    _add_sale(db, fixture)
    record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)

    # 次日改号在售：新报价面 + 第二业务日漏斗/冻结
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date="2026-10-17",
            code="周六900",
        ),
    )
    _add_quotes(
        db,
        fixture,
        {"h": 3.0, "d": 3.3, "a": 3.5},
        {"h": 2.0, "d": 3.4, "a": 4.0},
        source_time="2026-10-17T14:00:00+00:00",
        observed="2026-10-17T14:03:00+00:00",
    )
    record_funnel_day(db, as_of="2026-10-17T14:04:00+00:00", business_date="2026-10-17")
    fz.record_frozen_decisions(db, business_date="2026-10-17")

    # 同场同选向只有最早业务日一张 would_bet 卡
    assert (
        db.execute(
            "SELECT COUNT(*) FROM frozen_decisions WHERE decision = 'would_bet'"
        ).fetchone()[0]
        == 1
    )
    row = db.execute(
        "SELECT business_date FROM frozen_decisions WHERE decision = 'would_bet'"
    ).fetchone()
    assert row["business_date"] == BUSINESS_DATE

    # 对账只结算一次（跨业务日去重）
    def close_provider(home, away, kickoff, selection):
        return (0.52, "srct:1x2:177", "s1")

    _seed_draw(db, fixture, 2, 1)
    stats = fz.reconcile_frozen_decisions(
        db, close_provider, now="2026-10-18T00:00:00+00:00"
    )
    assert stats["settled"] == 1


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


def test_drift_identity_gating_and_anchor_ref_persisted(db) -> None:
    """参考身份贯通（report-audit-20261027 票 03）：

    - anchor_ref 入冻结账（主锚入账行="pinnacle"）；
    - 同类（anchor×源T 主锚收盘）→ 漂移照算；
    - 跨类（欧共识入账 × 主锚收盘）→ 漂移置空 + mismatch 计数 +
      close_ref 落列——参考切换不伪装成市场漂移。
    """
    from goalx_backend.betting.funnel import record_funnel_day

    def _consensus_quotes(fx: int) -> None:
        # 只放非主锚书（欧共识回退路径）→ funnel anchor_ref=eu_consensus[...]
        for book in ("odds_api:bookA", "odds_api:bookB"):
            for sel, odds in {"h": 2.0, "d": 3.4, "a": 4.0}.items():
                fx_store.insert_odds_snapshot(
                    db,
                    SnapshotInput(
                        fixture_id=fx,
                        market_code="had",
                        selection_code=sel,
                        source=book,
                        odds=odds,
                        captured_at=SOURCE_TIME,
                        observed_at=OBSERVED,
                        source_updated_at=SOURCE_TIME,
                    ),
                )
        db.commit()

    # 行 1：主锚入账（_add_quotes 放 odds_api:pinnacle）
    anchor_fx = _seed_fixture(db, 0)
    _add_quotes(
        db, anchor_fx, {"h": 3.0, "d": 3.3, "a": 3.5}, {"h": 2.0, "d": 3.4, "a": 4.0}
    )
    _add_sale(db, anchor_fx)
    # 行 2：共识入账（无主锚书——只灌 JC + 两本欧书）
    cons_fx = _seed_fixture(db, 1)
    for sel, odds in {"h": 3.0, "d": 3.3, "a": 3.5}.items():
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=cons_fx,
                market_code="had",
                selection_code=sel,
                source="sporttery",
                odds=odds,
                captured_at=SOURCE_TIME,
                observed_at=OBSERVED,
                source_updated_at=SOURCE_TIME,
            ),
        )
    _consensus_quotes(cons_fx)
    _add_sale(db, cons_fx)

    record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    fz.record_frozen_decisions(db, business_date=BUSINESS_DATE)

    refs = {
        int(r["fixture_id"]): r["anchor_ref"]
        for r in db.execute(
            "SELECT fixture_id, anchor_ref FROM frozen_decisions"
            " WHERE decision='would_bet'"
        )
    }
    assert refs[anchor_fx] == "pinnacle"
    assert refs[cons_fx].startswith("eu_consensus")

    def close_provider(home, away, kickoff, selection):
        return (0.52, "srct:1x2:177", "s1")

    stats = fz.reconcile_frozen_decisions(
        db, close_provider, now="2026-10-18T00:00:00+00:00"
    )
    rows = {
        int(r["fixture_id"]): r
        for r in db.execute(
            "SELECT fixture_id, drift_after_lock, close_ref FROM frozen_decisions"
            " WHERE decision='would_bet'"
        )
    }
    # 同类（pinnacle × 源T 收盘）→ 漂移照算 + close_ref 落列
    assert rows[anchor_fx]["drift_after_lock"] is not None
    assert rows[anchor_fx]["close_ref"] == "srct:1x2:177"
    # 跨类（共识入账 × 主锚收盘）→ 漂移置空 + mismatch 计数
    assert rows[cons_fx]["drift_after_lock"] is None
    assert rows[cons_fx]["close_ref"] == "srct:1x2:177"
    assert stats["drift_identity_mismatch"] == 1
    summary = fz.frozen_summary(db, BUSINESS_DATE)
    assert summary["reconciliation"]["drift_identity_mismatch"] == 1
