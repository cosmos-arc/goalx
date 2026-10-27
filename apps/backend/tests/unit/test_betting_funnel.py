"""机会漏斗观测（caliber-loop 票 04）：五级分母/流失原因/轨一估值的行为断言。"""

from __future__ import annotations

import json
import sqlite3

from goalx_backend.betting import funnel as fn
from goalx_backend.data import fixtures as fx_store
from goalx_backend.models import MatchCodeInput, SaleStatusInput, SnapshotInput, Tier

KICKOFF = "2026-10-17T18:00:00+00:00"
AS_OF = "2026-10-16T14:04:00+00:00"  # 冻结时点（kickoff 前一日）
BUSINESS_DATE = "2026-10-16"
SOURCE_TIME = "2026-10-16T14:00:00+00:00"
OBSERVED = "2026-10-16T14:03:00+00:00"
STALE_TIME = "2026-10-15T02:00:00+00:00"  # 超 300s 窗的陈报价


def _seed_fixture(db: sqlite3.Connection, *, kickoff: str = KICKOFF, n: int = 0) -> int:
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


def _add_jc(
    db: sqlite3.Connection,
    fixture: int,
    prices: dict[str, float | None],
    *,
    captured: str = SOURCE_TIME,
    observed: str | None = OBSERVED,
) -> None:
    for sel, odds in prices.items():
        if odds is None:
            continue
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="sporttery",
                odds=odds,
                captured_at=captured,
                observed_at=observed,
                source_updated_at=captured,
            ),
        )
    db.commit()


def _add_anchor(
    db: sqlite3.Connection,
    fixture: int,
    prices: dict[str, float | None],
    *,
    captured: str = SOURCE_TIME,
    observed: str = OBSERVED,
) -> None:
    for sel, odds in prices.items():
        if odds is None:
            continue
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="odds_api:pinnacle",
                odds=odds,
                captured_at=captured,
                observed_at=observed,
                source_updated_at=captured,
            ),
        )
    db.commit()


def _add_sale(
    db: sqlite3.Connection,
    fixture: int,
    *,
    state: str = "on_sale",
    single: bool | None = True,
) -> None:
    fx_store.append_sale_status(
        db,
        SaleStatusInput(
            fixture_id=fixture,
            market_code="had",
            sale_state=state,
            single_eligible=single,
            observed_at=OBSERVED,
        ),
    )
    db.commit()


def _seed_full_pass(db: sqlite3.Connection, n: int = 0) -> int:
    fixture = _seed_fixture(db, n=n)
    _add_jc(db, fixture, {"h": 2.10, "d": 3.40, "a": 3.20})
    _add_anchor(db, fixture, {"h": 2.20, "d": 3.50, "a": 3.30})
    _add_sale(db, fixture)
    return fixture


def test_funnel_all_pass(db: sqlite3.Connection) -> None:
    """全过：五级计数全等；轨一价值代理三向落库；幂等重跑不重复。"""
    fixture = _seed_full_pass(db)
    report = fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    assert report.n_on_sale == 1
    assert report.n_quote_matched == 1
    assert report.n_fresh == 1
    assert report.n_valuation == 1
    assert report.n_eligible == 1
    assert report.loss_reasons == {}
    row = db.execute(
        "SELECT * FROM funnel_fixture_days WHERE fixture_id = ?", (fixture,)
    ).fetchone()
    assert row["level_reached"] == fn.L_ELIGIBLE
    assert row["loss_reason"] is None
    assert row["anchor_ref"] == "pinnacle"
    # 价值代理 = O_JC × p_sharp − 1（h 侧：2.10×p_sharp_h−1）
    assert row["jc_odds_h"] == 2.10
    assert row["value_proxy_h"] is not None
    # 不写 bets 表（票 04 纪律）
    assert db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 0
    # 幂等：同业务日重跑不重复行
    fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    assert db.execute("SELECT COUNT(*) FROM funnel_days").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM funnel_fixture_days").fetchone()[0] == 1


def test_funnel_partial_losses_level_chain(db: sqlite3.Connection) -> None:
    """部分流失：逐级计数与流失原因各归其级（验收：分母链构造断言）。"""
    full = _seed_full_pass(db, n=0)
    # L2 流失：无锚书报价（jc 齐）
    no_anchor = _seed_fixture(db, n=1)
    _add_jc(db, no_anchor, {"h": 2.0, "d": 3.3, "a": 3.5})
    _add_sale(db, no_anchor)
    # L3 流失：锚书报价超窗陈旧
    stale_anchor = _seed_fixture(db, n=2)
    _add_jc(db, stale_anchor, {"h": 2.0, "d": 3.3, "a": 3.5})
    _add_anchor(
        db,
        stale_anchor,
        {"h": 2.2, "d": 3.4, "a": 3.6},
        captured=STALE_TIME,
        observed=STALE_TIME,  # 采集没跑：本机很久未见新报价
    )
    _add_sale(db, stale_anchor)
    # L5 流失：单固不合规
    not_single = _seed_fixture(db, n=3)
    _add_jc(db, not_single, {"h": 2.0, "d": 3.3, "a": 3.5})
    _add_anchor(db, not_single, {"h": 2.2, "d": 3.4, "a": 3.6})
    _add_sale(db, not_single, single=False)
    # L1 外：当日有销售编号但已开赛（在售分母排除）
    kicked = _seed_fixture(db, kickoff="2026-10-16T10:00:00+00:00", n=4)
    _add_jc(db, kicked, {"h": 2.0, "d": 3.3, "a": 3.5})
    report = fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    assert report.n_on_sale == 4  # kicked 不进分母
    assert report.n_quote_matched == 3  # no_anchor 在 L2 流失
    assert report.n_fresh == 2  # stale_anchor 在 L3 流失
    assert report.n_valuation == 2  # devig 恒可出（fresh 锚在场）
    assert report.n_eligible == 1  # not_single 在 L5 流失
    assert report.loss_reasons == {
        "l2_anchor_absent": 1,
        "l3_anchor_stale": 1,
        "l5_single_not_eligible": 1,
    }
    assert full  # 全过夹具已由 test_funnel_all_pass 覆盖语义
    # 轨二列可空（票 05 填充）+ 收盘对账列可空（票 06 回填）
    row = db.execute(
        "SELECT track2_json, p_sharp_close_h FROM funnel_fixture_days"
        " WHERE fixture_id = ?",
        (full,),
    ).fetchone()
    assert row["track2_json"] is None
    assert row["p_sharp_close_h"] is None


def test_funnel_all_rejected(db: sqlite3.Connection) -> None:
    """全拒：在售有分母、以下全零、流失原因如实（零注可审计区分）。"""
    _seed_fixture(db, n=0)  # 无任何报价/状态
    report = fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    assert report.n_on_sale == 1
    assert report.n_eligible == 0
    base = {
        "h": {"n": 0, "mean": None, "max": None},
        "d": {"n": 0, "mean": None, "max": None},
        "a": {"n": 0, "mean": None, "max": None},
    }
    for sel, face in report.value_proxy_stats.items():
        if sel != "track2_compare":
            assert face == base[sel]
    assert "l2_jc_no_quote" in report.loss_reasons


def test_funnel_liveness_and_day_row(db: sqlite3.Connection) -> None:
    """判活=漏斗表最新业务日；汇总行 JSON 可读回。"""
    assert fn.funnel_liveness(db) is None
    _seed_full_pass(db)
    fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    assert fn.funnel_liveness(db) == BUSINESS_DATE
    row = fn.funnel_day_row(db, BUSINESS_DATE)
    assert row is not None
    assert json.loads(row["loss_reasons"]) == {}
    stats = json.loads(row["value_proxy_stats"])
    assert stats["h"]["n"] == 1


def test_value_proxy_matches_spec_example(db: sqlite3.Connection) -> None:
    """价值代理口径：O_JC × p_sharp − 1（spec story 11 示例同构）。"""
    fixture = _seed_fixture(db)
    _add_jc(db, fixture, {"h": 2.10, "d": 3.40, "a": 3.20})
    # 均价 2.2/3.5/3.3 devig 后 p_sharp_h≈0.43 量级——断言口径非精确值
    _add_anchor(db, fixture, {"h": 2.20, "d": 3.50, "a": 3.30})
    _add_sale(db, fixture)
    fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    row = db.execute(
        "SELECT value_proxy_h, p_sharp_h, jc_odds_h FROM funnel_fixture_days"
        " WHERE fixture_id = ?",
        (fixture,),
    ).fetchone()
    assert row["value_proxy_h"] == round(row["jc_odds_h"] * row["p_sharp_h"] - 1.0, 4)


def test_funnel_stopped_sale_attributed_to_l5(db: sqlite3.Connection) -> None:
    """停售归因 L5（correctness F1）：adjudicate 对 stopped 短路，不编造 L2 原因。"""
    fixture = _seed_fixture(db, n=9)
    _add_jc(db, fixture, {"h": 2.0, "d": 3.3, "a": 3.5})
    _add_anchor(db, fixture, {"h": 2.2, "d": 3.4, "a": 3.6})
    _add_sale(db, fixture, state="stopped")
    report = fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    assert report.loss_reasons == {"l5_sale_stopped": 1}
    assert report.n_eligible == 0


def test_funnel_duplicate_match_code_deduped(db: sqlite3.Connection) -> None:
    """同场同日双销售编号（correctness F2）：去重不炸 UNIQUE、不双计。"""

    fixture = _seed_full_pass(db, n=8)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date=BUSINESS_DATE,
            code="周六999",  # 改号：同场第二编号
        ),
    )
    db.commit()
    report = fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    assert report.n_on_sale == 1
    assert report.n_eligible == 1
    assert (
        db.execute(
            "SELECT COUNT(*) FROM funnel_fixture_days WHERE fixture_id = ?",
            (fixture,),
        ).fetchone()[0]
        == 1
    )


# ---- 票 05：轨二拍点与双轨对照 ----


def test_track2_snapshot_idempotent_and_merge(db: sqlite3.Connection) -> None:
    """拍点落库幂等覆盖；漏斗行在则合并 track2_json；迟到旗如实。"""
    fixture = _seed_full_pass(db)
    fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    cap = fn.Track2Capture(
        fixture_id=fixture,
        snap_type="24h",
        target_instant="2026-10-16T02:00:00+00:00",
        captured_at="2026-10-16T02:00:10+00:00",
        purchasable=True,
        jc_odds={"h": 2.1, "d": 3.4, "a": 3.2},
        p_sharp={"h": 0.43, "d": 0.30, "a": 0.27},
        anchor_ref="pinnacle",
    )
    proxy = fn.record_track2_snapshot(db, cap)
    assert proxy["h"] == round(2.1 * 0.43 - 1.0, 4)
    row = db.execute(
        "SELECT * FROM track2_snapshots WHERE fixture_id = ?", (fixture,)
    ).fetchone()
    assert row["purchasable"] == 1
    assert row["late_flag"] == 0
    # 漏斗行 track2_json 已合并
    frow = db.execute(
        "SELECT track2_json FROM funnel_fixture_days WHERE fixture_id = ?",
        (fixture,),
    ).fetchone()
    assert "24h" in json.loads(frow["track2_json"])
    # 迟到补拍：覆盖同键，late_flag=1
    cap_late = fn.Track2Capture(
        fixture_id=fixture,
        snap_type="24h",
        target_instant="2026-10-16T02:00:00+00:00",
        captured_at="2026-10-16T05:00:00+00:00",  # 迟到 3h
        purchasable=False,
        purchasable_reason="sale_stopped",
        jc_odds={},
        p_sharp={},
    )
    fn.record_track2_snapshot(db, cap_late)
    row2 = db.execute(
        "SELECT late_flag, purchasable FROM track2_snapshots WHERE fixture_id = ?",
        (fixture,),
    ).fetchone()
    assert row2["late_flag"] == 1
    assert row2["purchasable"] == 0
    assert db.execute("SELECT COUNT(*) FROM track2_snapshots").fetchone()[0] == 1


def test_dual_track_face_in_day_report(db: sqlite3.Connection) -> None:
    """双轨对照面：分阶段均值+停售计数+衰减链进日终报告。"""
    fixture = _seed_full_pass(db)
    fn.record_track2_snapshot(
        db,
        fn.Track2Capture(
            fixture_id=fixture,
            snap_type="24h",
            target_instant="2026-10-16T02:00:00+00:00",
            captured_at="2026-10-16T02:00:05+00:00",
            purchasable=True,
            jc_odds={"h": 2.5, "d": 3.4, "a": 3.2},
            jc_age_seconds=4200.0,  # > 新鲜窗 → 资格分层披露面计数
            p_sharp={"h": 0.50, "d": 0.30, "a": 0.20},
            anchor_ref="pinnacle",
        ),
    )
    report = fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    compare = report.value_proxy_stats["track2_compare"]
    assert compare["purchasable"]["24h"] == {"eligible": 1, "total": 1}
    assert compare["mean_value_proxy"]["24h"]["h"] == round(2.5 * 0.50 - 1.0, 4)
    assert compare["mean_value_proxy"]["freeze"]["h"] is not None  # 轨一并列
    assert compare["late_snapshots"] == {"24h": 0, "12h": 0}
    # 资格差异分层披露（票 05）：JC 陈旧计数 + 锚身份分布，headline 不变
    assert compare["qualification"]["jc_stale"]["24h"] == 1
    assert compare["qualification"]["anchor_ref_mix"]["24h"] == {"pinnacle": 1}
    # 日行 JSON 持久化 track2_compare
    day = fn.funnel_day_row(db, BUSINESS_DATE)
    assert "track2_compare" in json.loads(day["value_proxy_stats"])


def test_track2_stopped_snapshot_not_in_proxy_means(db: sqlite3.Connection) -> None:
    """停售拍点（不可购）：仍落库但不进代理均值（可购边界语义）。"""
    fixture = _seed_full_pass(db)
    fn.record_track2_snapshot(
        db,
        fn.Track2Capture(
            fixture_id=fixture,
            snap_type="12h",
            target_instant="2026-10-16T08:00:00+00:00",
            captured_at="2026-10-16T08:00:05+00:00",
            purchasable=False,
            purchasable_reason="sale_stopped",
            jc_odds={"h": 2.5},
            p_sharp={"h": 0.5},
        ),
    )
    report = fn.record_funnel_day(db, as_of=AS_OF, business_date=BUSINESS_DATE)
    compare = report.value_proxy_stats["track2_compare"]
    assert compare["purchasable"]["12h"] == {"eligible": 0, "total": 1}
    assert compare["mean_value_proxy"]["12h"]["h"] is None


def _seed_joined_fixture(db: sqlite3.Connection, kickoff: str, n: int) -> int:

    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"主{n}")
    away = fx_store.upsert_team(db, f"客{n}")
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
    fx_store.set_odds_api_join(db, fixture, f"evt{n}", "soccer_epl", "manual")
    db.commit()
    return fixture


def test_track2_task_window_idempotent_late(tmp_path, monkeypatch) -> None:
    """task 级：窗口命中（拍点已过≤6h）才拍；幂等跳过；迟到旗；零候选零拉取。"""
    import goalx_backend.tasks as tasks_mod
    from goalx_backend import db as goalx_db

    db_path = tmp_path / "t.db"
    conn = goalx_db.connect(db_path)
    goalx_db.migrate(conn)
    now = "2026-10-17T14:00:00+00:00"
    # 拍点=开球−24h：kickoff=10-18T14 → 拍点=now（窗口内）
    hit24 = _seed_joined_fixture(conn, "2026-10-18T14:00:00+00:00", 0)
    # 12h 档命中：拍点=10-17T14=now ✓（开球 10-18T02）
    hit12 = _seed_joined_fixture(conn, "2026-10-18T02:00:00+00:00", 1)
    # 锚书报价在场（F1：无锚不落库——种上拍点前可见的 pinnacle 三向）
    _add_anchor(
        conn,
        hit24,
        {"h": 2.2, "d": 3.4, "a": 3.6},
        captured="2026-10-17T13:00:00+00:00",
        observed="2026-10-17T13:00:00+00:00",
    )
    _add_anchor(
        conn,
        hit12,
        {"h": 2.2, "d": 3.4, "a": 3.6},
        captured="2026-10-17T13:00:00+00:00",
        observed="2026-10-17T13:00:00+00:00",
    )
    # 窗外：拍点 7h 前（超 6h 视界）
    missed = _seed_joined_fixture(conn, "2026-10-18T07:00:00+00:00", 2)
    # 窗外：拍点未到（kickoff − 24h > now）
    future = _seed_joined_fixture(conn, "2026-10-19T20:00:00+00:00", 3)
    pulls: list[str] = []

    def fake_pull(conn, settings, client, *, anchor, targets):
        pulls.append(anchor)
        return tasks_mod.oddsapi.OddsIngestStats()

    monkeypatch.setenv("GOALX_DB_PATH", str(db_path))
    from datetime import datetime as _dt

    result = tasks_mod.track2_snapshots(
        now=_dt.fromisoformat(now),
        pull=fake_pull,
        decision_at=_dt.fromisoformat(now),
    )
    assert result["24h"]["captured"] == 1
    assert result["12h"]["captured"] == 1
    assert set(pulls) == {"track2_24h", "track2_12h"}  # 只拉命中窗
    # 停售边界按拍点时点：未种状态 → 未知不冒充可购（F3 口径）
    row = conn.execute(
        "SELECT purchasable, purchasable_reason, late_flag"
        " FROM track2_snapshots WHERE fixture_id=?",
        (hit24,),
    ).fetchone()
    assert row["purchasable"] == 0
    assert row["purchasable_reason"] == "sale_status_unknown"
    assert row["late_flag"] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM track2_snapshots WHERE fixture_id=?",
            (missed,),
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM track2_snapshots WHERE fixture_id=?",
            (future,),
        ).fetchone()[0]
        == 0
    )
    # 幂等：重拍零新请求零新行
    pulls.clear()
    result2 = tasks_mod.track2_snapshots(
        now=_dt.fromisoformat(now),
        pull=fake_pull,
        decision_at=_dt.fromisoformat(now),
    )
    assert pulls == []
    assert result2["24h"]["captured"] == 0
    # 迟到：把 now 推 2h——hit24 拍点已过 2h 仍可补拍（late_flag=1）
    late_now = _dt.fromisoformat("2026-10-17T16:00:00+00:00")
    conn.execute("DELETE FROM track2_snapshots WHERE fixture_id = ?", (hit24,))
    conn.commit()
    tasks_mod.track2_snapshots(now=late_now, pull=fake_pull, decision_at=late_now)
    row2 = conn.execute(
        "SELECT late_flag FROM track2_snapshots WHERE fixture_id=?", (hit24,)
    ).fetchone()
    assert row2["late_flag"] == 1
    assert hit12
    conn.close()


def test_track2_sale_states_four_way_and_stale_correction(
    tmp_path, monkeypatch
) -> None:
    """四态判定 + 存量冒充行纠正（measure-03：非 on_sale 一律不冒充可购）。"""
    import goalx_backend.tasks as tasks_mod
    from goalx_backend import db as goalx_db

    db_path = tmp_path / "t.db"
    conn = goalx_db.connect(db_path)
    goalx_db.migrate(conn)
    now = "2026-10-17T14:00:00+00:00"
    fid_sale = _seed_joined_fixture(conn, "2026-10-18T14:00:00+00:00", 10)
    fid_stop = _seed_joined_fixture(conn, "2026-10-18T14:00:00+00:00", 11)
    fid_unknown = _seed_joined_fixture(conn, "2026-10-18T14:00:00+00:00", 12)
    for fid in (fid_sale, fid_stop, fid_unknown):
        _add_anchor(
            conn,
            fid,
            {"h": 2.2, "d": 3.4, "a": 3.6},
            captured="2026-10-17T13:00:00+00:00",
            observed="2026-10-17T13:00:00+00:00",
        )
    states = {fid_sale: "on_sale", fid_stop: "stopped", fid_unknown: "unknown"}
    for fid, state in states.items():
        fx_store.append_sale_status(
            conn,
            SaleStatusInput(
                fixture_id=fid,
                market_code="had",
                sale_state=state,
                single_eligible=True,
                observed_at="2026-10-17T13:30:00+00:00",
            ),
        )
    # 存量冒充行：老分支落下的 purchasable=1 + 实际停售（拍点已出窗，不重拍）
    fid_stale = _seed_joined_fixture(conn, "2026-10-16T14:00:00+00:00", 13)
    fn.record_track2_snapshot(
        conn,
        fn.Track2Capture(
            fixture_id=fid_stale,
            snap_type="24h",
            target_instant="2026-10-15T14:00:00+00:00",
            captured_at="2026-10-15T14:00:05+00:00",
            purchasable=True,
            purchasable_reason=None,
            jc_odds={"h": 2.5},
            p_sharp={"h": 0.5},
        ),
    )
    fx_store.append_sale_status(
        conn,
        SaleStatusInput(
            fixture_id=fid_stale,
            market_code="had",
            sale_state="stopped",
            single_eligible=True,
            observed_at="2026-10-15T13:00:00+00:00",
        ),
    )
    conn.commit()
    monkeypatch.setenv("GOALX_DB_PATH", str(db_path))
    from datetime import datetime as _dt

    def fake_pull(conn, settings, client, *, anchor, targets):
        return tasks_mod.oddsapi.OddsIngestStats()

    result = tasks_mod.track2_snapshots(
        now=_dt.fromisoformat(now),
        pull=fake_pull,
        decision_at=_dt.fromisoformat(now),
    )
    assert result["24h"]["captured"] == 3
    # 四态之三（无行态见 test_track2_task_window_idempotent_late）
    expected = {
        fid_sale: (1, None),
        fid_stop: (0, "sale_stopped"),
        fid_unknown: (0, "sale_status_unknown_value"),
    }
    for fid, (flag, reason) in expected.items():
        row = conn.execute(
            "SELECT purchasable, purchasable_reason FROM track2_snapshots"
            " WHERE fixture_id=?",
            (fid,),
        ).fetchone()
        assert row["purchasable"] == flag
        assert row["purchasable_reason"] == reason
    # 存量纠正：只降不升，冒充行改判 sale_stopped
    assert result["purchasable_corrected"] == 1
    row_stale = conn.execute(
        "SELECT purchasable, purchasable_reason FROM track2_snapshots"
        " WHERE fixture_id=?",
        (fid_stale,),
    ).fetchone()
    assert row_stale["purchasable"] == 0
    assert row_stale["purchasable_reason"] == "sale_stopped"
    conn.close()


def test_track2_same_run_quote_not_filtered_by_task_clock(
    tmp_path, monkeypatch
) -> None:
    """四时钟（measure-04）：本拍新抓的报价不被任务时刻滤掉。"""
    import goalx_backend.tasks as tasks_mod
    from goalx_backend import db as goalx_db

    db_path = tmp_path / "t.db"
    conn = goalx_db.connect(db_path)
    goalx_db.migrate(conn)
    now = "2026-10-17T14:00:00+00:00"  # 任务时刻（窗口计算）
    fid = _seed_joined_fixture(conn, "2026-10-18T14:00:00+00:00", 14)

    # 锚报价在任务时刻之后才落库且 observed_at=任务时刻+1s——拉取腿产物
    def seeding_pull(conn, settings, client, *, anchor, targets):
        _add_anchor(
            conn,
            fid,
            {"h": 2.2, "d": 3.4, "a": 3.6},
            captured="2026-10-17T14:00:01+00:00",
            observed="2026-10-17T14:00:01+00:00",
        )
        return tasks_mod.oddsapi.OddsIngestStats()

    monkeypatch.setenv("GOALX_DB_PATH", str(db_path))
    from datetime import datetime as _dt

    result = tasks_mod.track2_snapshots(
        now=_dt.fromisoformat(now),
        pull=seeding_pull,
        decision_at=_dt.fromisoformat("2026-10-17T14:00:30+00:00"),
    )
    # 旧行为：observed > 任务时刻 → no_anchor；新行为：决策时刻可见 → 采集
    assert result["24h"]["captured"] == 1
    assert result["24h"]["skipped_no_anchor"] == 0
    row = conn.execute(
        "SELECT captured_at FROM track2_snapshots WHERE fixture_id=?",
        (fid,),
    ).fetchone()
    assert row["captured_at"] == "2026-10-17T14:00:30+00:00"
    conn.close()


def test_track2_task_zero_candidates_zero_pulls(tmp_path, monkeypatch) -> None:
    """零候选日：零请求零落库（既有判活模式）。"""
    import goalx_backend.tasks as tasks_mod
    from goalx_backend import db as goalx_db

    db_path = tmp_path / "t2.db"
    conn = goalx_db.connect(db_path)
    goalx_db.migrate(conn)

    def fake_pull(*args, **kwargs):
        raise AssertionError("零候选不得发起锚拉取")

    monkeypatch.setenv("GOALX_DB_PATH", str(db_path))
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    result = tasks_mod.track2_snapshots(
        now=_dt(2026, 10, 17, 14, 0, tzinfo=_UTC), pull=fake_pull
    )
    for label in ("24h", "12h"):
        assert result[label] == {
            "candidates": 0,
            "captured": 0,
            "skipped_no_anchor": 0,
            "errors": 0,
            "anchor_pulls": 0,
        }
    conn.close()
