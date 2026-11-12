"""auto 引擎共享执行件（源自 auto-v1 落注引擎，auto-paper 票 03）。

auto-v1 引擎已于 2026-11-11 人裁退役（v2 唯一现役）：本文件钉住共享
执行件的现役行为（经 probes.run_auto_v2 或直测）——选票规则=每场一注
top-N EV 降序（select_singles）、落注通道=官方性同比对+建注闸
（place_single：价一致→system_paper+快照身份、闸门拒绝留痕）、同锚
重跑幂等（既有同键注回带零新增）、零候选日台账留痕（没跑/没得买可
分）；以及退役兼容面——auto-v1 史行照常结算/记账（不改写）。
"""

from __future__ import annotations

import sqlite3
import zoneinfo
from datetime import datetime

import pytest

from goalx_backend.betting import auto_strategy as au
from goalx_backend.betting import probes, versioning
from goalx_backend.betting.versioning import StrategyState
from goalx_backend.data import fixtures as fx_store
from goalx_backend.modelling.forecast import insert_forecast
from goalx_backend.modelling.score_matrix import ScoreMatrix
from goalx_backend.models import MatchCodeInput, SaleStatusInput, SnapshotInput, Tier

BEIJING = zoneinfo.ZoneInfo("Asia/Shanghai")
RUN_AT = datetime(2026, 11, 8, 21, 1, tzinfo=BEIJING)  # 21:00 锚后 1 分钟
ANCHOR = "21:00"
BUSINESS_DATE = "2026-11-08"
KICKOFF = "2026-11-08T15:00:00+00:00"  # 23:00 北京时，锚后 2 小时（>40min 门槛）
SEEN_AT = "2026-11-08T12:50:00+00:00"  # 锚前有效观测


def _matrix(home_lam: float, away_lam: float) -> ScoreMatrix:
    """确定性矩阵：泊松权重 0..7 截断归一。"""
    import math

    def poisson(lam: float, k: int) -> float:
        return math.exp(-lam) * lam**k / math.factorial(k)

    grid = [
        [poisson(home_lam, i) * poisson(away_lam, j) for j in range(8)]
        for i in range(8)
    ]
    total = sum(map(sum, grid))
    return ScoreMatrix(
        tuple(tuple(cell / total for cell in row) for row in grid),
        lam_home=home_lam,
        lam_away=away_lam,
    )


def _payload(home_lam: float, away_lam: float) -> dict[str, object]:
    matrix = _matrix(home_lam, away_lam)
    return {
        "matrix": [list(row) for row in matrix.grid],
        "lambda_home": home_lam,
        "lambda_away": away_lam,
        "had_probs": matrix.had(),
    }


def _seed_fixture(db: sqlite3.Connection, tag: str, *, code: str) -> int:
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"主队{tag}")
    away = fx_store.upsert_team(db, f"客队{tag}")
    fixture = fx_store.upsert_fixture(db, competition, KICKOFF, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture, kind="jingcai", business_date=BUSINESS_DATE, code=code
        ),
    )
    return fixture


def _seed_on_sale(db: sqlite3.Connection, fixture_id: int) -> None:
    fx_store.append_sale_status(
        db,
        SaleStatusInput(
            fixture_id=fixture_id,
            market_code=None,
            sale_state="on_sale",
            single_eligible=True,
            observed_at=SEEN_AT,
        ),
    )


def _seed_forecast(
    db: sqlite3.Connection, fixture_id: int, home_lam: float, away_lam: float
) -> None:
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="ml",
        model_version="test-matrix-v1",
        content_hash=f"hash-{fixture_id}",
        payload=_payload(home_lam, away_lam),
        issued_at=SEEN_AT,
    )


def _snap(
    db: sqlite3.Connection,
    fixture_id: int,
    market: str,
    sel: str,
    odds: float,
    *,
    meta: dict[str, str] | None = None,
    at: str = SEEN_AT,
) -> None:
    fx_store.insert_odds_snapshot(
        db,
        SnapshotInput(
            fixture_id=fixture_id,
            market_code=market,
            selection_code=sel,
            source="sporttery",
            odds=odds,
            captured_at=at,
            observed_at=at,
            meta=meta,
        ),
    )


def _seed_positive_single(
    db: sqlite3.Connection, fixture_id: int, selection: str, *, ev: float = 0.5
) -> float:
    """种一条使该 ttg 选项 EV=+ev 的 sporttery 价（锚前观测）。"""
    prob = float(_matrix(1.3, 1.1).ttg()[selection])
    odds = round((1.0 + ev) / prob, 2)
    _snap(db, fixture_id, "ttg", selection, odds)
    return odds


# ---- 纯选票规则（共享件直测）----


def _make_v2_live(db: sqlite3.Connection) -> None:
    """注册 auto-v2 引擎三元组并人审升 live-paper（票 01 闸 fail-closed）。"""
    estimator, calibration, fingerprint = au.engine_version_triple(
        probes.AUTO_V2_PARAMS
    )
    version_id = versioning.register_version(
        db,
        estimator_version=estimator,
        calibration_version=calibration,
        params_version=fingerprint,
        label=probes.AUTO_V2,
    )
    for target in (
        StrategyState.SHADOW,
        StrategyState.PROBE,
        StrategyState.CANDIDATE,
        StrategyState.LIVE_PAPER,
    ):
        versioning.transition_version(
            db, version_id, target, actor="human:test", reason="测试晋升"
        )


def _pick(
    fid: int, market: str, prob: float, odds: float, code: str = "周六001"
) -> au.Pick:
    return au.Pick(
        fixture_id=fid,
        match_code=code,
        market_code=market,
        selection_code="2",
        goal_line=None,
        prob=prob,
        odds=odds,
        kickoff_utc=KICKOFF,
    )


def test_select_singles_per_fixture_best_then_top3() -> None:
    params = probes.AUTO_V2_PARAMS
    # 场 1 两个正 EV 候选（ttg+crs）→ 只取最优；场 2/3 各一；场 4 负 EV 出局
    picks = [
        _pick(1, "ttg", 0.20, 5.0),  # EV 0.0
        _pick(1, "crs", 0.10, 12.0),  # EV 0.2 ← 场 1 最优
        _pick(2, "ttg", 0.30, 4.0),  # EV 0.2
        _pick(3, "crs", 0.05, 30.0),  # EV 0.5
        _pick(4, "ttg", 0.50, 1.5),  # EV -0.25 出局
        _pick(5, "ttg", 0.10, 9.0),  # EV -0.1 出局
    ]
    chosen = au.select_singles(picks, params)
    assert [p.fixture_id for p in chosen] == [3, 1, 2]  # EV 降序 top-3


def test_place_single_zero_stake_rejected(db: sqlite3.Connection) -> None:
    """correctness F1（共享件直测）：注额 ≤0 → rejected 留痕，不进闸门。"""
    fixture = _seed_fixture(db, "子", code="周六041")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture, 1.3, 1.1)
    _seed_positive_single(db, fixture, "2")
    db.commit()
    pick = _pick(fixture, "ttg", 0.26, 5.74)
    outcome = au.place_single(
        db,
        pick,
        stake=0.0,
        request_id="test:zero-stake",
        run_iso=RUN_AT.isoformat(),
        reads=None,
        strategy_version=probes.AUTO_V2,
    )
    assert outcome == "rejected"
    assert db.execute("SELECT COUNT(*) c FROM bets").fetchone()["c"] == 0


# ---- 锚整跑（隔离库 + 冻结时钟，现役路径=auto-v2）----


def test_anchor_run_places_flat_singles_and_idempotent_rerun(
    db: sqlite3.Connection,
) -> None:
    f1 = _seed_fixture(db, "甲", code="周六001")
    f2 = _seed_fixture(db, "乙", code="周六002")
    f3 = _seed_fixture(db, "丙", code="周六003")
    for fixture in (f1, f2, f3):
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture, 1.3, 1.1)
        # 单关面：三场各一条 ttg 正 EV（v2 全四玩法开放，本测聚焦 ttg 路径）
        _seed_positive_single(db, fixture, "2", ev=0.5)
        # had 面负 EV（EV=-0.05 逐向反推）→ had 保底槽不硬凑（无正 EV had）
        for sel, prob in _matrix(1.3, 1.1).had().items():
            _snap(db, fixture, "had", sel, round(0.95 / prob, 2))
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 3
    assert stats["gate_rejected"] == 0
    rows = db.execute(
        "SELECT stake, strategy_version, price_source, market_code, locked_odds"
        " FROM bets b JOIN bet_legs l ON l.bet_id = b.id"
        " WHERE b.strategy_version = 'auto-v2'"
    ).fetchall()
    assert len(rows) == 3
    for row in rows:
        assert row["strategy_version"] == "auto-v2"
        assert row["price_source"] == "system_paper"
        assert row["market_code"] == "ttg"
        assert row["stake"] == pytest.approx(2.0)  # 平注（探针等权测量）

    # 同锚重跑：幂等（零新增，统计如实报 replayed）
    rerun = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert rerun["bets"] == 0
    assert rerun["replayed"] == 3
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM bets WHERE strategy_version='auto-v2'"
        ).fetchone()["c"]
        == 3
    )

    # 台账：行存在=跑过，bets 记首次
    run_row = db.execute(
        "SELECT bets, zero_note FROM auto_paper_runs"
        " WHERE business_date=? AND anchor_label=? AND strategy='auto-v2'",
        (BUSINESS_DATE, ANCHOR),
    ).fetchone()
    assert run_row["bets"] == 3  # 首跑注数不因重跑清零（MAX 合并）
    assert run_row["zero_note"] is None


def test_gate_rejection_recorded_via_place(db: sqlite3.Connection, monkeypatch) -> None:
    """闸门拒绝路径（票验收）：资格闸抛 → 该注弃置并计入 gate_rejected。"""
    f1 = _seed_fixture(db, "己", code="周一001")
    f2 = _seed_fixture(db, "庚", code="周一002")
    for fixture in (f1, f2):
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture, 1.3, 1.1)
        prob = _matrix(1.3, 1.1).had()["h"]
        _snap(
            db, fixture, "had", "h", round(1.5 / prob, 2)
        )  # 正 EV 单关（v2 had 面开放）
        _snap(db, fixture, "had", "d", round(1.5 / _matrix(1.3, 1.1).had()["d"], 2))
        _snap(db, fixture, "had", "a", round(1.5 / _matrix(1.3, 1.1).had()["a"], 2))
        _snap(db, fixture, "ttg", "2", 1.2)  # 负 EV
    db.commit()

    def _reject(conn, legs, *, as_of=None, reads=None):
        raise au.BetEligibilityError("fixture 不可投: 测试注入拒绝")

    monkeypatch.setattr(au, "validate_selections", _reject)
    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 0
    assert stats["gate_rejected"] == 2
    note = db.execute(
        "SELECT zero_note FROM auto_paper_runs"
        " WHERE business_date=? AND anchor_label=?",
        (BUSINESS_DATE, ANCHOR),
    ).fetchone()["zero_note"]
    assert note == "gate_rejected"
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM bets WHERE strategy_version='auto-v2'"
        ).fetchone()["c"]
        == 0
    )


def test_price_drift_skips_bet(db: sqlite3.Connection) -> None:
    fixture = _seed_fixture(db, "壬", code="周三001")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture, 1.3, 1.1)
    _seed_positive_single(db, fixture, "2", ev=0.5)
    db.commit()
    # 锚后价漂：晚到的调盘观测（锚 13:00Z 后、运行 13:01Z 前可见）
    prob = float(_matrix(1.3, 1.1).ttg()["2"])
    _snap(db, fixture, "ttg", "2", round(1.2 / prob, 2), at="2026-11-08T13:00:30+00:00")
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 0
    assert stats["price_drift"] == 1
    note = db.execute(
        "SELECT zero_note FROM auto_paper_runs"
        " WHERE business_date=? AND anchor_label=?",
        (BUSINESS_DATE, ANCHOR),
    ).fetchone()["zero_note"]
    assert note == "price_drift"


def test_migration_v43_idempotent(db: sqlite3.Connection) -> None:
    from goalx_backend.db import migrate

    version = migrate(db)
    assert version == 61
    assert migrate(db) == 61  # 同版本重跑零效果
    assert db.execute("SELECT COUNT(*) c FROM auto_paper_runs").fetchone()["c"] == 0


# ---- auto-v1 退役兼容面（2026-11-11 人裁：史行照读，不改写）----


def test_retired_v1_history_still_settles_and_ledgers(db: sqlite3.Connection) -> None:
    """auto-v1 退役兼容面：史行照常结算/记账，不改写。

    引擎代码退役后 strategy_version='auto-v1' 的既有注仍是结算范围
    （settle 不按引擎分叉——scope 只认 mode/status）与台账读面
    （account 策略面）的正式成员；历史账面只读，永不回溯改写。
    """
    from goalx_backend.betting import account
    from goalx_backend.betting import store as bt_store
    from goalx_backend.betting.settle import run_settlement
    from goalx_backend.data.ingest.results import import_draw_results
    from goalx_backend.models import BetMode, DrawResultInput, LegInput, MarketKind

    fixture = _seed_fixture(db, "亥", code="周六031")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture, 1.3, 1.1)
    _snap(db, fixture, "had", "h", 3.4)
    db.commit()
    bet_id = bt_store.create_bet(
        db,
        BetMode.PAPER,
        MarketKind.FIXED,
        2.0,
        strategy_version=au.AUTO_V1,
        price_source="system_paper",
        client_request_id=f"auto-v1:{BUSINESS_DATE}:{ANCHOR}:s:{fixture}:had",
    )
    bt_store.add_leg(
        db,
        bet_id,
        LegInput(
            fixture_id=fixture, market_code="had", selection_code="h", locked_odds=3.4
        ),
    )
    db.commit()

    stats = run_settlement(db)  # 开赛前无结果 → 仍 open 不误结
    assert stats["still_open"] >= 1
    import_draw_results(
        db, [DrawResultInput(fixture_id=fixture, home_goals=2, away_goals=0)]
    )
    db.commit()
    settled = run_settlement(db)
    assert settled["won"] >= 1  # 主胜 2:0 → had h 中
    row = db.execute(
        "SELECT status, payout, profit, strategy_version FROM bets WHERE id = ?",
        (bet_id,),
    ).fetchone()
    assert row["status"] == "won"
    assert row["strategy_version"] == "auto-v1"  # 结算不改写引擎标签
    assert row["profit"] == pytest.approx(2.0 * 3.4 - 2.0)

    # 台账读面：全账视图显式成面（auto-v1 史行与人工纸面分列不吞没）
    faces = account.paper_account_face(db)
    v1_face = next(f for f in faces.strategies if f.strategy == "auto-v1")
    assert v1_face.bets_n == 1
    assert v1_face.profit_settled == pytest.approx(4.8)


def test_record_anchor_run_zero_note_inference(db: sqlite3.Connection) -> None:
    """台账 zero_note 推断链（共享件直测；v1 时代的 tax_cap_skipped 键留档）。"""
    au.record_anchor_run(
        db,
        BUSINESS_DATE,
        "21:00",
        stats={"candidates": 1, "bets": 0, "slips": 0, "tax_cap_skipped": 1},
        strategy=au.AUTO_V1,  # 史行策略键照写照读（台账兼容面）
    )
    au.record_anchor_run(
        db,
        BUSINESS_DATE,
        "21:30",
        stats={"candidates": 1, "bets": 0, "slips": 0, "gate_rejected": 1},
        strategy=au.AUTO_V1,
    )
    notes = {
        row["anchor_label"]: row["zero_note"]
        for row in db.execute(
            "SELECT anchor_label, zero_note FROM auto_paper_runs"
            " WHERE business_date=? AND strategy=?",
            (BUSINESS_DATE, au.AUTO_V1),
        )
    }
    assert notes == {"21:00": "tax_cap_skipped", "21:30": "gate_rejected"}


# ---- 版本闸 e2e（票 01 消费端最小接线，改挂 v2）----


def test_shadow_version_refused_and_live_paper_places(db: sqlite3.Connection) -> None:
    """
    vision-build 票 01 e2e（v2 宿主）：shadow 态版本被引擎拒单且留痕；
    人审升 live-paper 后放行。

    下单前查状态（消费端最小接线）：非 live 态整锚跳过留痕
    （zero_note=version_not_live），升 live 后同锚正常落注。
    """
    fixtures = [
        _seed_fixture(db, tag, code=f"周六1{i}")
        for i, tag in enumerate(("A", "B", "C"), 1)
    ]
    for fixture in fixtures:
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture, 1.3, 1.1)
        _seed_positive_single(db, fixture, "2", ev=0.5)
    db.commit()

    # 注册本引擎三元组（与闸同一推导）的版本并人审升到 shadow
    estimator, calibration, fingerprint = au.engine_version_triple(
        probes.AUTO_V2_PARAMS
    )
    version_id = versioning.register_version(
        db,
        estimator_version=estimator,
        calibration_version=calibration,
        params_version=fingerprint,
        label=probes.AUTO_V2,
    )
    versioning.transition_version(
        db, version_id, StrategyState.SHADOW, actor="human:test", reason="影子起跑"
    )

    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 0
    assert stats["version_not_live"] == 1
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM bets WHERE strategy_version='auto-v2'"
        ).fetchone()["c"]
        == 0
    )
    note = db.execute(
        "SELECT zero_note FROM auto_paper_runs"
        " WHERE business_date=? AND anchor_label=?",
        (BUSINESS_DATE, ANCHOR),
    ).fetchone()["zero_note"]
    assert note == "version_not_live"

    # 人审逐级升 live-paper：同锚放行（下注照常）
    for target in (
        StrategyState.PROBE,
        StrategyState.CANDIDATE,
        StrategyState.LIVE_PAPER,
    ):
        versioning.transition_version(
            db, version_id, target, actor="human:test", reason="过门放量"
        )
    live_stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert live_stats["bets"] == 3


# ---- sizing 面（票 12 税感知帽，v2 守卫宿主）----


def test_live_paper_singles_walk_tax_free_sizing(db: sqlite3.Connection) -> None:
    """
    票 12 参数层接线 e2e（v2 宿主）：live-paper 下单走 sizing 面
    （平注压免税帽守卫，票 01 状态消费同链）。

    sizing=min(注码, staking.tax_free_stake_cap(odds))——flat ¥2 恒不触帽
    （无行为变化，守卫宿主见 probes），落库注额 ×赔率 ≤ 免税线不变量；
    压帽行为由 test_probes 极端赔率例钉住（本测钉「无行为变化」侧）。
    """
    from goalx_backend.betting import tax
    from goalx_backend.betting.staking import tax_free_stake_cap

    fixture = _seed_fixture(db, "巳", code="周六021")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture, 1.3, 1.1)
    odds = _seed_positive_single(db, fixture, "2", ev=0.5)  # odds=5.74 → 帽 ¥1742
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 1
    row = db.execute(
        "SELECT stake FROM bets WHERE strategy_version = 'auto-v2'"
    ).fetchone()
    assert row["stake"] == pytest.approx(min(2.0, tax_free_stake_cap(odds)))
    assert row["stake"] == pytest.approx(2.0)  # 今日口径：守卫 no-op
    assert row["stake"] * odds <= tax.TAX_FREE_THRESHOLD  # 免税桶不变量


# ---- 候选面守卫（correctness F4/票 14，共享件经 v2 路径）----


def test_kickoff_within_guard_window_excluded(db: sqlite3.Connection) -> None:
    """可购窗口预筛（prereg 近似口径）：开球早于锚+缓冲+前置→排除留痕。"""
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, "近开球队")
    away = fx_store.upsert_team(db, "近开客队")
    # 锚 21:00 北京时（13:00Z）；开球 12:45Z（20:45 北京）早于锚 → 不可购
    fixture = fx_store.upsert_fixture(
        db, competition, "2026-11-08T12:45:00+00:00", home, away
    )
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date=BUSINESS_DATE,
            code="周六051",
        ),
    )
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture, 1.3, 1.1)
    _seed_positive_single(db, fixture, "2", ev=0.5)  # 正 EV 也不进候选
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["candidates"] == 0
    assert stats["excluded_not_playable"] == 1
    assert stats["bets"] == 0


def test_hhad_mixed_or_bad_line_excluded_not_crash(db: sqlite3.Connection) -> None:
    """correctness F4：goal_line 非数值/混线——场排除（不绑错线不炸锚）。"""
    f_ok = _seed_fixture(db, "子", code="周六009")
    f_ok2 = _seed_fixture(db, "寅", code="周六011")
    f_bad = _seed_fixture(db, "丑", code="周六010")
    matrix = _matrix(1.3, 1.1)
    line_ok = ({"goal_line": "-1"},) * 3
    cases = (
        (f_ok, line_ok),
        (f_ok2, line_ok),
        (f_bad, ({"goal_line": "x"}, {"goal_line": "-2"}, {"goal_line": "-1"})),
    )
    for fixture, metas in cases:
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture, 1.3, 1.1)
        prob = matrix.hhad(-1.0)["h"]
        for sel, meta in zip(("h", "d", "a"), metas, strict=False):
            _snap(db, fixture, "hhad", sel, round(1.5 / prob, 2), meta=meta)
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] >= 1  # 合线场照常
    rows = db.execute(
        "SELECT DISTINCT l.fixture_id FROM bets b JOIN bet_legs l ON l.bet_id=b.id"
        " WHERE b.strategy_version='auto-v2' AND l.market_code='hhad'"
    ).fetchall()
    assert f_bad not in {int(r["fixture_id"]) for r in rows}  # 混线/坏线场被排除


def test_prescreen_rejects_stopped_or_non_single_market(db: sqlite3.Connection) -> None:
    """票 14 反例转正：had 在售但 ttg 停售且不允许单关 → ttg 拒（玩法级预筛）。

    外部审计缺陷 5：预筛原硬编码 had 在售判定（latest_sale_status_asof
    的 market 参数固定 "had"），非 had 票蹭 had 在售过闸；现按玩法行
    （had/hhad/ttg/crs/hafu）分别验在售与单关资格（与下游建注闸同源，
    market_eligibility_problem 单源两引用）。
    """
    stopped = _seed_fixture(db, "卯", code="周日003")
    _seed_on_sale(db, stopped)  # 比赛级行 on_sale（had 面可投）
    _seed_forecast(db, stopped, 1.3, 1.1)
    fx_store.append_sale_status(
        db,
        SaleStatusInput(
            fixture_id=stopped,
            market_code="ttg",
            sale_state="stopped",
            single_eligible=False,
            observed_at=SEEN_AT,
        ),
    )
    _seed_positive_single(db, stopped, "2", ev=0.5)  # ttg 正 EV（否则不会入选）

    not_single = _seed_fixture(db, "辰", code="周日004")
    _seed_on_sale(db, not_single)
    _seed_forecast(db, not_single, 1.3, 1.1)
    fx_store.append_sale_status(
        db,
        SaleStatusInput(
            fixture_id=not_single,
            market_code="ttg",
            sale_state="on_sale",
            single_eligible=False,  # 在售但非单固
            observed_at=SEEN_AT,
        ),
    )
    _seed_positive_single(db, not_single, "2", ev=0.5)
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 0  # 两场唯一正 EV 面（ttg 单关）都被玩法闸拦下
    assert stats["candidates"] == 0
    assert stats["excluded_market_sale_stopped"] == 1
    assert stats["excluded_market_single_ineligible"] == 1
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM bets WHERE strategy_version='auto-v2'"
        ).fetchone()["c"]
        == 0
    )
