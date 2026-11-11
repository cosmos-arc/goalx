"""
edge-program 票 01：auto-v2 探针阵列 + 每注 CLV。

钉住五件事：校准探针落账带 edge_source 标签与平注金额（claimed-EV
top-3 全四玩法）、熄灯探针零注留痕（stats 可见）、同锚重跑幂等、
池面 ¥20/晚测量帽、CLV 回填口径（源T 收盘三向 Shin 去水 − 入场隐含；
非 had 腿/无锚诚实 NULL）。分来源账户面（P&L+CLV 滚动）同缝钉。

had 资格闸（validate_selections）以真实时钟为判定时点，与固定
种子时刻天然相斥——本文件按 test_gate_rejection 先例整组旁路；闸门
本体由 v1 测试与 quote-reads seam 测试钉。
"""

from __future__ import annotations

import sqlite3
import zoneinfo
from datetime import datetime

import duckdb
import pytest

from goalx_backend import odds_math as om
from goalx_backend.betting import account, probes, versioning
from goalx_backend.betting import auto_strategy as au
from goalx_backend.betting.settle import run_settlement
from goalx_backend.betting.versioning import StrategyState
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.ingest.results import import_draw_results
from goalx_backend.markets import SELECTIONS
from goalx_backend.modelling.forecast import insert_forecast
from goalx_backend.modelling.score_matrix import ScoreMatrix
from goalx_backend.models import (
    DrawResultInput,
    MatchCodeInput,
    SaleStatusInput,
    SnapshotInput,
    Tier,
)

BEIJING = zoneinfo.ZoneInfo("Asia/Shanghai")
RUN_AT = datetime(2026, 11, 9, 21, 1, tzinfo=BEIJING)  # 21:00 锚后 1 分钟
ANCHOR = "21:00"
BUSINESS_DATE = "2026-11-09"
KICKOFF = "2026-11-09T15:00:00+00:00"  # 23:00 北京时，锚后 2 小时
SEEN_AT = "2026-11-09T12:50:00+00:00"  # 锚前有效观测


@pytest.fixture(autouse=True)
def _bypass_had_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """had 资格闸旁路（见模块 docstring）：本文件钉引擎行为非闸门行为。"""
    monkeypatch.setattr(au, "validate_selections", lambda *args, **kwargs: None)


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


def _seed_fixture(db: sqlite3.Connection, tag: str, *, code: str) -> int:
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"主队{tag}")
    away = fx_store.upsert_team(db, f"客队{tag}")
    fixture = fx_store.upsert_fixture(db, competition, KICKOFF, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date=BUSINESS_DATE,
            code=code,
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


def _seed_forecast(db: sqlite3.Connection, fixture_id: int) -> None:
    matrix = _matrix(1.3, 1.1)
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="ml",
        model_version="test-matrix-v1",
        content_hash=f"hash-{fixture_id}",
        payload={
            "matrix": [list(row) for row in matrix.grid],
            "lambda_home": 1.3,
            "lambda_away": 1.1,
            "had_probs": matrix.had(),
        },
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


def _seed_positive_ev_boards(db: sqlite3.Connection, fixture_id: int) -> None:
    """四玩法各一条正 EV sporttery 价（锚前观测）。"""
    matrix = _matrix(1.3, 1.1)
    for market, probs in (
        ("had", matrix.had()),
        ("ttg", matrix.ttg()),
        ("crs", matrix.crs()),
        ("hhad", matrix.hhad(-1.0)),
    ):
        best = max(probs, key=lambda sel: probs[sel])
        _snap(
            db,
            fixture_id,
            market,
            best,
            round(1.5 / probs[best], 2),
            meta={"goal_line": "-1"} if market == "hhad" else None,
        )


def _v2_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT b.id, b.stake, b.edge_source, b.clv, l.market_code, l.locked_odds
        FROM bets b JOIN bet_legs l ON l.bet_id = b.id
        WHERE b.strategy_version = 'auto-v2'
        ORDER BY b.id
        """
    ).fetchall()


# ---- 校准探针：标签落账 + 平注 + 全四玩法面 ----


def _make_engine_live(db: sqlite3.Connection, *, params: object, label: str) -> None:
    """注册引擎三元组并人审升 live-paper（票 01 闸 fail-closed，v1/v2 通用）。"""
    estimator, calibration, fingerprint = au.engine_version_triple(params)
    version_id = versioning.register_version(
        db,
        estimator_version=estimator,
        calibration_version=calibration,
        params_version=fingerprint,
        label=label,
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


def _make_v2_live(db: sqlite3.Connection) -> None:
    """auto-v2 引擎注册+人审升 live-paper（_make_engine_live 便捷口）。"""
    _make_engine_live(db, params=probes.AUTO_V2_PARAMS, label=probes.AUTO_V2)


def test_calibration_probe_labels_and_flat_stake(db: sqlite3.Connection) -> None:
    fixtures = [
        _seed_fixture(db, tag, code=f"周日00{i}")
        for i, tag in enumerate(("甲", "乙", "丙"), 1)
    ]
    for fixture in fixtures:
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture)
        _seed_positive_ev_boards(db, fixture)
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 3  # 三场各一注（每场最优），未超 top-3
    assert stats["probe_stale_line"] == 0  # 熄灯探针零注留痕
    assert stats["probe_rollover"] == 0
    assert stats["probe_price_band"] == 0
    rows = _v2_rows(db)
    assert len(rows) == 3
    for row in rows:
        assert row["edge_source"] == "calibration_probe"
        assert row["stake"] == 2.0  # 平注非 Kelly（探针等权测量）
        assert row["clv"] is None  # 未结算不回填
    # 四玩法都进了候选（v1 时代 had 单关面不存在）
    assert {row["market_code"] for row in rows} <= {"had", "hhad", "crs", "ttg"}

    # 同锚重跑幂等：零新增，统计报 replayed
    rerun = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert rerun["bets"] == 0
    assert rerun["replayed"] == 3
    assert len(_v2_rows(db)) == 3


def test_calibration_probe_had_face_reaches_clv_coverage(
    db: sqlite3.Connection,
) -> None:
    """had 正 EV 单关落账（v1 时代 had 只有串关面，v2 单关面打开 CLV 覆盖）。"""
    fixture = _seed_fixture(db, "丁", code="周一001")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture)
    had = _matrix(1.3, 1.1).had()
    for sel, prob in had.items():  # 全三向正 EV 高价
        _snap(db, fixture, "had", sel, round(1.5 / prob, 2))
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 1
    rows = _v2_rows(db)
    assert rows[0]["market_code"] == "had"
    assert rows[0]["edge_source"] == "calibration_probe"


def test_calibration_probe_reserves_had_clv_slot(db: sqlite3.Connection) -> None:
    """had 保底槽：crs/ttg 长票霸榜时末槽让位最优 had 单关（CLV 不断流）。

    十年逆向选择实证=claimed-EV 排名几乎恒被 crs/ttg 占据，而源T 收盘
    锚只覆盖 had——无保底则每注 CLV 仪器饿死（Spec 审查发现）。
    """
    fixtures = [
        _seed_fixture(db, tag, code=f"周六00{i}")
        for i, tag in enumerate(("丑", "寅", "卯"), 1)
    ]
    matrix = _matrix(1.3, 1.1)
    for fixture in fixtures:
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture)
        # crs 最优 EV 显著压过 had（+80% vs +20%）→ 纯排名 top-3 全 crs
        crs = matrix.crs()
        best_crs = max(crs, key=lambda sel: crs[sel])
        _snap(db, fixture, "crs", best_crs, round(1.8 / crs[best_crs], 2))
        had = matrix.had()
        for sel, prob in had.items():
            _snap(db, fixture, "had", sel, round(1.2 / prob, 2))
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 3
    rows = _v2_rows(db)
    assert {row["market_code"] for row in rows} == {"crs", "had"}
    had_rows = [row for row in rows if row["market_code"] == "had"]
    assert len(had_rows) == 1  # 末槽让位：三场 crs 霸榜仍有一注 had
    assert all(row["edge_source"] == "calibration_probe" for row in rows)
    # 每注仍平注 ¥2（帽不破）
    assert all(row["stake"] == 2.0 for row in rows)


def test_calibration_probe_no_had_candidate_no_slot(db: sqlite3.Connection) -> None:
    """无正 EV had 候选：保底槽不硬凑（诚实零 had，CLV 缺省如实）。"""
    fixture = _seed_fixture(db, "辰", code="周日009")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture)
    ttg = _matrix(1.3, 1.1).ttg()
    best = max(ttg, key=lambda sel: ttg[sel])
    _snap(db, fixture, "ttg", best, round(1.5 / ttg[best], 2))
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 1
    rows = _v2_rows(db)
    assert rows[0]["market_code"] == "ttg"


def test_backfill_clv_fixture_row_absent_degrades(
    db: sqlite3.Connection, monkeypatch
) -> None:
    """correctness：场次档案缺席（fixture 行被删的史行）=无锚降级，不炸 wrap。"""
    fixture = _seed_fixture(db, "亥", code="周五021")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture)
    had = _matrix(1.3, 1.1).had()
    for sel, prob in had.items():
        _snap(db, fixture, "had", sel, round(1.5 / prob, 2))
    db.commit()
    _make_v2_live(db)
    probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    _settle_bets(db, [fixture])
    monkeypatch.setattr(
        probes.fx_store, "get_fixture", lambda *args, **kwargs: None
    )  # 档案面缺席（史行 fixture 行不存在的等价态）
    monkeypatch.setattr(
        probes.quote_evidence,
        "srct_pinnacle_closing_triplet",
        lambda *args, **kwargs: (2.0, 3.4, 3.6),
    )
    filled = probes.backfill_bet_clv(db, duckdb.connect(":memory:"))
    assert filled == {"filled": 0, "skipped_non_had": 0, "skipped_no_anchor": 1}
    assert all(row["clv"] is None for row in _v2_rows(db))


def test_calibration_probe_stake_pressed_into_tax_free_cap(
    db: sqlite3.Connection,
) -> None:
    """税帽守卫（auto-v1 退役迁宿，2026-11-11）：平注压免税帽在 v2 活宿主。

    sizing 规则=min(注码, staking.tax_free_stake_cap(odds))——今日口径
    flat ¥2 恒不触帽（帽 <¥2 须 odds>5000，竞彩无此价=无行为变化）；本测
    用极端价 6000 证明规则在链上：stake=min(2, floor(10000/6000))=¥1，
    奖金 ≤¥1 万永处免税桶（票 12 规则，引擎层实现=tax_free_stake_cap）。
    """
    from goalx_backend.betting.staking import tax_free_stake_cap

    fixture = _seed_fixture(db, "戌", code="周二011")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture)
    _snap(db, fixture, "ttg", "2", 6000.0)  # 极端正 EV 价：帽 floor(10000/6000)=¥1
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 1
    rows = _v2_rows(db)
    assert rows[0]["stake"] == pytest.approx(min(2.0, tax_free_stake_cap(6000.0)))
    assert rows[0]["stake"] == 1.0
    assert rows[0]["stake"] * float(rows[0]["locked_odds"]) <= 10_000.0


def test_zero_signal_day_ledger(db: sqlite3.Connection) -> None:
    """无候选日：台账留痕 no_candidates，熄灯探针在 detail_json 可见。"""
    import json

    fixture = _seed_fixture(db, "戊", code="周二001")
    _seed_on_sale(db, fixture)
    _snap(db, fixture, "ttg", "2", 1.2)  # 无 forecast → no_model 排除
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 0
    assert stats["candidates"] == 0
    assert stats["excluded_no_model"] == 1
    row = db.execute(
        "SELECT zero_note, detail_json FROM auto_paper_runs"
        " WHERE business_date=? AND anchor_label=?",
        (BUSINESS_DATE, ANCHOR),
    ).fetchone()
    assert row["zero_note"] == "no_candidates"
    detail = json.loads(row["detail_json"])
    assert detail["probe_stale_line"] == 0  # 熄灯留痕进台账
    assert detail["probe_price_band"] == 0


# ---- CLV 回填：源T 收盘锚口径 ----


def _settle_bets(db: sqlite3.Connection, fixtures: list[int]) -> None:
    import_draw_results(
        db,
        [
            DrawResultInput(fixture_id=fid, home_goals=2, away_goals=0)
            for fid in fixtures
        ],
    )
    db.commit()
    run_settlement(db)


def test_backfill_clv_single_had(db: sqlite3.Connection, monkeypatch) -> None:
    fixture = _seed_fixture(db, "己", code="周三001")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture)
    had = _matrix(1.3, 1.1).had()
    for sel, prob in had.items():
        _snap(db, fixture, "had", sel, round(1.5 / prob, 2))
    db.commit()
    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 1
    _settle_bets(db, [fixture])

    triplet = (2.0, 3.4, 3.6)  # 收盘三向（手算锚）
    monkeypatch.setattr(
        probes.quote_evidence,
        "srct_pinnacle_closing_triplet",
        lambda *args, **kwargs: triplet,
    )
    filled = probes.backfill_bet_clv(db, duckdb.connect(":memory:"))
    assert filled == {"filled": 1, "skipped_non_had": 0, "skipped_no_anchor": 0}
    row = _v2_rows(db)[0]
    selection = db.execute(
        "SELECT selection_code FROM bet_legs"
        " WHERE bet_id = (SELECT id FROM bets WHERE strategy_version='auto-v2')"
    ).fetchone()["selection_code"]
    close_prob = om.shin_implied(triplet)[SELECTIONS.index(selection)]
    expected = close_prob - 1.0 / float(row["locked_odds"])
    assert row["clv"] == pytest.approx(expected)

    # 幂等：重跑零新增（clv 已非 NULL 不再触碰）
    again = probes.backfill_bet_clv(db, duckdb.connect(":memory:"))
    assert again["filled"] == 0


def test_backfill_clv_skips_non_had_and_missing_anchor(
    db: sqlite3.Connection, monkeypatch
) -> None:
    f_had = _seed_fixture(db, "庚", code="周四001")
    f_ttg = _seed_fixture(db, "辛", code="周四002")
    matrix = _matrix(1.3, 1.1)
    for fixture in (f_had, f_ttg):
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture)
    for sel, prob in matrix.had().items():
        _snap(db, f_had, "had", sel, round(1.5 / prob, 2))
    _snap(db, f_ttg, "ttg", "2", round(1.5 / matrix.ttg()["2"], 2))
    db.commit()
    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 2
    _settle_bets(db, [f_had, f_ttg])

    def _none(*args, **kwargs):
        return None  # 无锚（CorpusScope 外）

    monkeypatch.setattr(probes.quote_evidence, "srct_pinnacle_closing_triplet", _none)
    filled = probes.backfill_bet_clv(db, duckdb.connect(":memory:"))
    # ttg 单关=非 had 票；had 单关=无锚票——两类都诚实 NULL
    assert filled == {"filled": 0, "skipped_non_had": 1, "skipped_no_anchor": 1}
    assert all(row["clv"] is None for row in _v2_rows(db))
    # 语料整体缺席（duck_con=None）同口径：不炸不倒填
    absent = probes.backfill_bet_clv(db, None)
    assert absent["filled"] == 0


def test_backfill_clv_two_leg_parlay_joint(db: sqlite3.Connection, monkeypatch) -> None:
    """auto-v1 史行（had 两腿串关）也回填：票级=Π收盘 − Π隐含。

    v1 引擎已退役（2026-11-11）——史行经 store 建库口直造（退役兼容面：
    CLV 回填范围按 strategy_version 非空判，不按引擎分叉）。
    """
    from goalx_backend.betting import store as bt_store
    from goalx_backend.models import BetMode, LegInput, MarketKind

    fixtures = [
        _seed_fixture(db, tag, code=f"周五00{i}")
        for i, tag in enumerate(("壬", "癸"), 1)
    ]
    matrix = _matrix(1.3, 1.1)
    for fixture in fixtures:
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture)
        for sel, prob in matrix.had().items():
            _snap(db, fixture, "had", sel, round(1.5 / prob, 2))
        _snap(db, fixture, "ttg", "2", 1.2)  # 单关面负 EV
    db.commit()
    bet_id = bt_store.create_bet(
        db,
        BetMode.PAPER,
        MarketKind.FIXED,
        2.0,
        strategy_version=au.AUTO_V1,
        price_source="system_paper",
        client_request_id=(
            f"auto-v1:{BUSINESS_DATE}:{ANCHOR}:p:{min(fixtures)}-{max(fixtures)}"
        ),
    )
    for fixture in fixtures:
        bt_store.add_leg(
            db,
            bet_id,
            LegInput(
                fixture_id=fixture,
                market_code="had",
                selection_code="h",
                locked_odds=round(1.5 / matrix.had()["h"], 2),
            ),
        )
    db.commit()
    _settle_bets(db, fixtures)

    triplets = {fixtures[0]: (2.0, 3.4, 3.6), fixtures[1]: (2.2, 3.2, 3.4)}

    def _by_args(con, home, away, kickoff, **kwargs):
        return triplets[fixtures[0]] if "壬" in str(home) else triplets[fixtures[1]]

    monkeypatch.setattr(
        probes.quote_evidence, "srct_pinnacle_closing_triplet", _by_args
    )
    filled = probes.backfill_bet_clv(db, duckdb.connect(":memory:"))
    assert filled["filled"] == 1
    legs = db.execute(
        "SELECT l.locked_odds, l.selection_code, l.fixture_id"
        " FROM bets b JOIN bet_legs l ON l.bet_id = b.id"
        " WHERE b.strategy_version = 'auto-v1'"
    ).fetchall()
    joint_close = 1.0
    joint_implied = 1.0
    for leg in legs:
        triplet = triplets[int(leg["fixture_id"])]
        joint_close *= om.shin_implied(triplet)[SELECTIONS.index(leg["selection_code"])]
        joint_implied *= 1.0 / float(leg["locked_odds"])
    clv = db.execute(
        "SELECT clv FROM bets WHERE strategy_version='auto-v1'"
    ).fetchone()["clv"]
    assert clv == pytest.approx(joint_close - joint_implied)


# ---- 池面测量帽 ----


def _stub_pool_face(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tag: str
) -> None:
    """期次/选票面打桩：pick9 九场各一选=恰 1 组合 ¥2（帽扣减粒度）。"""
    from goalx_backend.data import pool as pool_store

    period_id = pool_store.upsert_pool_period(db, "pick9", f"262{tag}", None)

    def _active_period(conn, market, now_iso):
        if market != "pick9":
            return None
        return {"id": period_id, "sales_deadline": None}

    monkeypatch.setattr(au, "_active_period", _active_period)
    monkeypatch.setattr(
        au,
        "_pool_picks",
        lambda conn, pid, market, now: {i: ["3"] for i in range(1, 10)},  # 池码 3=主胜
    )


def test_pool_budget_gate_skips_when_exhausted(
    db: sqlite3.Connection, monkeypatch
) -> None:
    """帽余量不足一组合注额：整票跳过；足额时正常落且按组合扣减。"""
    _stub_pool_face(db, monkeypatch, tag="01")
    placed = au.place_pool_slips(
        db,
        run_at=RUN_AT,
        params=probes.AUTO_V2_PARAMS,
        source="auto-v2",
        stake_budget=2.0,
    )
    assert placed["slips"] == 1

    _stub_pool_face(db, monkeypatch, tag="02")  # 新期次（排除幂等 skip 干扰）
    capped = au.place_pool_slips(
        db,
        run_at=RUN_AT,
        params=probes.AUTO_V2_PARAMS,
        source="auto-v2",
        stake_budget=1.99,
    )
    assert capped["slips"] == 0
    # 已落组合恰 ¥2；帽余量读数（当业务日）与帽口径一致
    stake = db.execute(
        "SELECT COALESCE(SUM(c.stake), 0) AS s FROM combinations c"
        " JOIN bet_slips s2 ON s2.id = c.slip_id WHERE s2.source = 'auto-v2'"
    ).fetchone()["s"]
    assert stake == 2.0
    assert probes._pool_stake_today(db, "auto-v2", BUSINESS_DATE) == 2.0
    # 无帽（v1 冻结口径）：第三期照落
    _stub_pool_face(db, monkeypatch, tag="03")
    legacy = au.place_pool_slips(
        db, run_at=RUN_AT, params=probes.AUTO_V2_PARAMS, source="auto-v2"
    )
    assert legacy["slips"] == 1


def test_pool_stake_today_filters_business_date(db: sqlite3.Connection) -> None:
    """跨业务日旧票不占今日帽（placed_at 北京日切）。"""
    for day, stake in (("2026-11-08T13:00:00+00:00", 10.0), (RUN_AT.isoformat(), 4.0)):
        db.execute(
            "INSERT INTO bet_slips (mode, source, placed_at, created_at)"
            " VALUES ('paper', 'auto-v2', ?, ?)",
            (day, day),
        )
        slip = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.execute(
            "INSERT INTO combinations (slip_id, seq, stake, selections)"
            " VALUES (?, 0, ?, '[]')",
            (slip, stake),
        )
    db.commit()
    assert probes._pool_stake_today(db, "auto-v2", BUSINESS_DATE) == 4.0


# ---- 分来源账户面 ----


def test_edge_source_faces_pnl_and_clv_rolling(
    db: sqlite3.Connection, monkeypatch
) -> None:
    fixture = _seed_fixture(db, "子", code="周六001")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture)
    had = _matrix(1.3, 1.1).had()
    for sel, prob in had.items():
        _snap(db, fixture, "had", sel, round(1.5 / prob, 2))
    db.commit()
    _make_v2_live(db)
    probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    _settle_bets(db, [fixture])
    monkeypatch.setattr(
        probes.quote_evidence,
        "srct_pinnacle_closing_triplet",
        lambda *args, **kwargs: (2.0, 3.4, 3.6),
    )
    probes.backfill_bet_clv(db, duckdb.connect(":memory:"))

    faces = account.edge_source_faces(db)
    assert [f.source for f in faces] == ["calibration_probe"]
    face = faces[0]
    assert face.bets_n == 1
    assert face.settled_n == 1
    assert face.stake_settled == 2.0
    assert face.clv_n == 1
    assert face.clv_mean is not None
    assert face.clv_mean_last50 == face.clv_mean  # 窗内唯一注
    assert face.clv_se_last50 is None  # n=1 无标准误

    # 账户全账面带分来源行（API 消费同一读面）
    full = account.paper_account_face(db)
    assert [f.source for f in full.per_edge_source] == ["calibration_probe"]


def test_edge_source_clv_se_clusters_by_fixture(db: sqlite3.Connection) -> None:
    """票 13 反例③（外部审计数值例）：同场复制行 CLV SE 按场并一。

    两场各 25 注、场内 CLV 相同（0.03/0.01）：按行独立口径 SE=0.001429
    （同场多锚重复贡献证据，精度虚增 7 倍）；按场聚类（同场多锚并一）
    收敛到两独立场均值的量级 SE=0.01。
    """
    fixture_a = _seed_fixture(db, "乾", code="周日101")
    fixture_b = _seed_fixture(db, "坤", code="周日102")
    for fixture, clv in ((fixture_a, 0.03), (fixture_b, 0.01)):
        for _ in range(25):
            db.execute(
                "INSERT INTO bets (mode, market_kind, stake, created_at, status,"
                " payout, profit, settled_at, strategy_version, edge_source, clv)"
                " VALUES ('paper', 'fixed', 2.0, '2026-11-09T12:00:00+00:00',"
                " 'won', 4.0, 2.0, '2026-11-09T13:00:00+00:00', 'auto-v2',"
                " 'calibration_probe', ?)",
                (clv,),
            )
            bet_id = int(
                db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            )
            db.execute(
                "INSERT INTO bet_legs (bet_id, fixture_id, market_code,"
                " selection_code, locked_odds) VALUES (?, ?, 'had', 'h', 2.0)",
                (bet_id, fixture),
            )
    db.commit()

    faces = account.edge_source_faces(db)
    assert [f.source for f in faces] == ["calibration_probe"]
    face = faces[0]
    assert face.bets_n == 50
    assert face.clv_n == 50
    assert face.clv_mean == pytest.approx(0.02)  # 按场并一均值=场均值再跨场平均
    assert face.clv_mean_last50 == pytest.approx(0.02)
    assert face.clv_se_last50 == pytest.approx(0.01)  # 行独立口径为 0.001429


def test_migration_v46_idempotent(db: sqlite3.Connection) -> None:
    from goalx_backend.db import migrate

    assert migrate(db) == 59
    assert migrate(db) == 59
    cols = {row["name"] for row in db.execute("PRAGMA table_info(bets)").fetchall()}
    assert {"edge_source", "clv"} <= cols
    run_cols = {
        row["name"]
        for row in db.execute("PRAGMA table_info(auto_paper_runs)").fetchall()
    }
    assert "strategy" in run_cols  # 台账引擎分账行（correctness F2）


def test_cutover_day_ledger_rows_per_engine(db: sqlite3.Connection) -> None:
    """correctness F2：换栈日 v1/v2 同日同锚——台账分行不混账。

    v1 引擎已退役（2026-11-11）：v1 行经共享台账写口直建（史行形态），
    v2 行走现役引擎——同日同锚两行各账的语义不变。
    """
    import json

    fixture = _seed_fixture(db, "巳", code="周一010")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture)
    _seed_positive_ev_boards(db, fixture)
    db.commit()
    au.record_anchor_run(
        db,
        BUSINESS_DATE,
        ANCHOR,
        stats={"candidates": 1, "bets": 1, "slips": 0},
        strategy=au.AUTO_V1,
    )
    _make_v2_live(db)
    probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)

    rows = db.execute(
        "SELECT strategy, bets, detail_json FROM auto_paper_runs"
        " WHERE business_date=? AND anchor_label=? ORDER BY strategy",
        (BUSINESS_DATE, ANCHOR),
    ).fetchall()
    assert [row["strategy"] for row in rows] == ["auto-v1", "auto-v2"]  # 两行各账
    v2_detail = json.loads(
        next(r for r in rows if r["strategy"] == "auto-v2")["detail_json"]
    )
    assert "probe_stale_line" in v2_detail  # v2 统计不被 v1 行吞没


def test_backfill_clv_bad_selection_code_degrades(
    db: sqlite3.Connection, monkeypatch
) -> None:
    """correctness F1：had 市场但闭集外选择码（历史脏行）=无锚降级，不炸 wrap。"""
    fixture = _seed_fixture(db, "午", code="周一011")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture)
    had = _matrix(1.3, 1.1).had()
    for sel, prob in had.items():
        _snap(db, fixture, "had", sel, round(1.5 / prob, 2))
    db.commit()
    _make_v2_live(db)
    probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    _settle_bets(db, [fixture])
    db.execute("PRAGMA foreign_keys=OFF")  # 脏行注入绕 FK（历史库病态行模拟）
    db.execute(
        "UPDATE bet_legs SET selection_code='qq'"
        " WHERE bet_id=(SELECT id FROM bets WHERE strategy_version='auto-v2')"
    )
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(
        probes.quote_evidence,
        "srct_pinnacle_closing_triplet",
        lambda *args, **kwargs: (2.0, 3.4, 3.6),
    )
    filled = probes.backfill_bet_clv(db, duckdb.connect(":memory:"))
    assert filled == {"filled": 0, "skipped_non_had": 1, "skipped_no_anchor": 0}


def test_calibration_probe_sparse_night_appends_had(db: sqlite3.Connection) -> None:
    """不满员+场外有正 EV had：补位不降级既有 EV 信号（稀疏夜规则）。"""
    f_crs = _seed_fixture(db, "未", code="周一012")
    f_had = _seed_fixture(db, "申", code="周一013")
    matrix = _matrix(1.3, 1.1)
    _seed_on_sale(db, f_crs)
    _seed_forecast(db, f_crs)
    crs = matrix.crs()
    best_crs = max(crs, key=lambda sel: crs[sel])
    _snap(db, f_crs, "crs", best_crs, round(1.5 / crs[best_crs], 2))
    _seed_on_sale(db, f_had)
    _seed_forecast(db, f_had)
    had = matrix.had()
    for sel, prob in had.items():
        _snap(db, f_had, "had", sel, round(1.5 / prob, 2))
    db.commit()

    _make_v2_live(db)
    stats = probes.run_auto_v2(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 2  # crs 保留 + had 补位（各一场，¥4 < 帽）
    rows = _v2_rows(db)
    assert {row["market_code"] for row in rows} == {"crs", "had"}
