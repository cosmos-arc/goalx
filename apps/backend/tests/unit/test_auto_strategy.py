"""auto-paper 票 03：auto-v1 策略落注。

钉住四件事：选票规则=prereg 修订①-④（每场一注 top-3 单关限 crs/ttg、
串关限 had/hhad 前五单场≤2）、注码=quarter-Kelly 封顶手算一致、
同锚重跑幂等（既有同键注回带零新增）、零候选日台账留痕（没跑/没得买
可分）。官方性通道：价一致→system_paper+快照身份；闸门拒绝留痕。
"""

from __future__ import annotations

import sqlite3
import zoneinfo
from datetime import datetime

import pytest

from goalx_backend.betting import auto_strategy as au
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


# ---- 纯选票规则（prereg 修订①-④）----


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
    params = au.AutoV1Params()
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


def test_select_parlays_top5_and_per_match_cap() -> None:
    params = au.AutoV1Params()
    # 6 个正 EV 单腿（联合 EV 由腿 EV 决定），场 1 EV 最高会霸榜
    picks = [
        _pick(1, "had", 0.5, 2.4),  # EV 0.2
        _pick(2, "had", 0.5, 2.3),  # EV 0.15
        _pick(3, "had", 0.5, 2.2),  # EV 0.1
        _pick(4, "had", 0.5, 2.1),  # EV 0.05
        _pick(5, "had", 0.5, 2.05),  # EV 0.025
        _pick(6, "had", 0.5, 2.02),  # EV 0.01
    ]
    chosen = au.select_parlays(picks, params)
    assert len(chosen) == 5
    per_match: dict[int, int] = {}
    for a, b in chosen:
        assert a.fixture_id != b.fixture_id
        for p in (a, b):
            per_match[p.fixture_id] = per_match.get(p.fixture_id, 0) + 1
    assert max(per_match.values()) <= params.parlay_per_match


def test_quarter_kelly_cap() -> None:
    params = au.AutoV1Params()
    # kelly=(p*o-1)/(o-1)=0.0625 → quarter×bankroll=¥15.6 → 封顶 ¥2
    assert au.quarter_kelly_stake(0.25, 5.0, 1000.0, params) == 2.0
    # 小 edge 小注：kelly=(p*o-1)/(o-1)=0.005 → quarter×¥1000=¥1.25
    assert au.quarter_kelly_stake((1 + 0.005 * 0.5) / 1.5, 1.5, 1000.0, params) == 1.25


# ---- 锚整跑（隔离库 + 冻结时钟）----


def test_anchor_run_places_singles_with_kelly_and_idempotent_rerun(
    db: sqlite3.Connection,
) -> None:
    f1 = _seed_fixture(db, "甲", code="周六001")
    f2 = _seed_fixture(db, "乙", code="周六002")
    f3 = _seed_fixture(db, "丙", code="周六003")
    for fixture in (f1, f2, f3):
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture, 1.3, 1.1)
        # 单关面：三场各一条 ttg 正 EV（修订④限 crs/ttg）
        _seed_positive_single(db, fixture, "2", ev=0.5)
        # had 面负 EV（EV=-0.05 逐向反推）→ 串关池空（本测聚焦单关路径）
        for sel, prob in _matrix(1.3, 1.1).had().items():
            _snap(db, fixture, "had", sel, round(0.95 / prob, 2))
    db.commit()

    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 3
    assert stats["gate_rejected"] == 0
    rows = db.execute(
        "SELECT stake, strategy_version, price_source, market_code, locked_odds"
        " FROM bets b JOIN bet_legs l ON l.bet_id = b.id"
        " WHERE b.strategy_version = 'auto-v1'"
    ).fetchall()
    assert len(rows) == 3
    prob = float(_matrix(1.3, 1.1).ttg()["2"])
    odds = round(1.5 / prob, 2)
    expected_stake = au.quarter_kelly_stake(prob, odds, 1000.0, au.AUTO_V1_PARAMS)
    for row in rows:
        assert row["strategy_version"] == "auto-v1"
        assert row["price_source"] == "system_paper"
        assert row["market_code"] == "ttg"
        assert row["stake"] == pytest.approx(expected_stake)

    # 同锚重跑：幂等（零新增，统计如实报 replayed）
    rerun = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert rerun["bets"] == 0
    assert rerun["replayed"] == 3
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM bets WHERE strategy_version='auto-v1'"
        ).fetchone()["c"]
        == 3
    )

    # 台账：行存在=跑过，bets 记首次
    run_row = db.execute(
        "SELECT bets, zero_note FROM auto_paper_runs"
        " WHERE business_date=? AND anchor_label=?",
        (BUSINESS_DATE, ANCHOR),
    ).fetchone()
    assert run_row["bets"] == 3  # 首跑注数不因重跑清零（MAX 合并）
    assert run_row["zero_note"] is None


def test_hhad_parlay_places_when_positive(db: sqlite3.Connection) -> None:
    f1 = _seed_fixture(db, "丁", code="周日001")
    f2 = _seed_fixture(db, "戊", code="周日002")
    matrix = _matrix(1.3, 1.1)
    for fixture in (f1, f2):
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture, 1.3, 1.1)
        # hhad 整数线 −1：主胜两球+ → 正 EV 高价
        prob = matrix.hhad(-1.0)["h"]
        odds = round(1.8 / prob, 2)
        _snap(db, fixture, "hhad", "h", odds, meta={"goal_line": "-1"})
        # 单关面无正 EV（负价 ttg）；had 无行 → 串关池只 hhad
        _snap(db, fixture, "ttg", "2", 1.2)
    db.commit()

    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 1  # 一张 2串1（两腿不同场）
    legs = db.execute(
        "SELECT l.fixture_id, l.market_code,"
        " json_extract(l.meta, '$.goal_line') AS goal_line FROM bets b"
        " JOIN bet_legs l ON l.bet_id = b.id WHERE b.strategy_version='auto-v1'"
    ).fetchall()
    assert len(legs) == 2
    assert {leg["fixture_id"] for leg in legs} == {f1, f2}
    assert all(leg["market_code"] == "hhad" for leg in legs)
    stake = db.execute(
        "SELECT stake FROM bets WHERE strategy_version='auto-v1'"
    ).fetchone()["stake"]
    assert stake == pytest.approx(2.0)  # 修订④：串关各 ¥2


def test_gate_rejection_recorded_via_place(db: sqlite3.Connection, monkeypatch) -> None:
    """闸门拒绝路径（票验收）：资格闸抛 → 该注弃置并计入 gate_rejected。"""
    f1 = _seed_fixture(db, "己", code="周一001")
    f2 = _seed_fixture(db, "庚", code="周一002")
    for fixture in (f1, f2):
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture, 1.3, 1.1)
        prob = _matrix(1.3, 1.1).had()["h"]
        _snap(db, fixture, "had", "h", round(1.5 / prob, 2))  # 正 EV 串关腿
        _snap(db, fixture, "had", "d", round(1.5 / _matrix(1.3, 1.1).had()["d"], 2))
        _snap(db, fixture, "had", "a", round(1.5 / _matrix(1.3, 1.1).had()["a"], 2))
        _snap(db, fixture, "ttg", "2", 1.2)  # 单关面负 EV
    db.commit()

    def _reject(conn, legs, *, as_of=None, reads=None):
        raise au.BetEligibilityError("fixture 不可投: 测试注入拒绝")

    monkeypatch.setattr(au, "validate_had_selections", _reject)
    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 0
    assert stats["gate_rejected"] >= 1
    note = db.execute(
        "SELECT zero_note FROM auto_paper_runs"
        " WHERE business_date=? AND anchor_label=?",
        (BUSINESS_DATE, ANCHOR),
    ).fetchone()["zero_note"]
    assert note == "gate_rejected"
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM bets WHERE strategy_version='auto-v1'"
        ).fetchone()["c"]
        == 0
    )


def test_zero_candidate_day_ledger(db: sqlite3.Connection) -> None:
    fixture = _seed_fixture(db, "辛", code="周二001")
    _seed_on_sale(db, fixture)
    # 无 forecast、无正 EV → candidates 含排除账、零注留痕
    _snap(db, fixture, "ttg", "2", 1.2)
    db.commit()

    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 0
    assert stats["candidates"] == 0
    assert stats["excluded_no_model"] == 1
    row = db.execute(
        "SELECT zero_note FROM auto_paper_runs"
        " WHERE business_date=? AND anchor_label=?",
        (BUSINESS_DATE, ANCHOR),
    ).fetchone()
    assert row["zero_note"] == "no_candidates"


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

    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
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
    assert version == 45
    assert migrate(db) == 45  # 同版本重跑零效果
    assert db.execute("SELECT COUNT(*) c FROM auto_paper_runs").fetchone()["c"] == 0


def test_zero_rounded_stake_skips_not_aborts(db: sqlite3.Connection) -> None:
    """correctness F1：quarter-Kelly 取整到 ¥0 的微 edge——跳过计数，锚完整收班。"""
    fixture = _seed_fixture(db, "癸", code="周四001")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture, 1.3, 1.1)
    prob = float(_matrix(1.3, 1.1).ttg()["2"])
    _snap(db, fixture, "ttg", "2", round((1 + 1e-5) / prob, 4))  # EV=1e-5 → stake≈0
    db.commit()

    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 0
    assert stats["gate_rejected"] == 1  # 零注跳过计拒绝不炸锚
    row = db.execute(
        "SELECT zero_note FROM auto_paper_runs"
        " WHERE business_date=? AND anchor_label=?",
        (BUSINESS_DATE, ANCHOR),
    ).fetchone()
    assert row["zero_note"] == "gate_rejected"


def test_parlay_content_key_survives_reorder(db: sqlite3.Connection) -> None:
    """correctness F2：晚到观测重排串关候选——同对内容键吸收重跑零新增。"""
    fixtures = [
        _seed_fixture(db, tag, code=f"周五00{i}")
        for i, tag in enumerate(("甲", "乙", "丙"), 1)
    ]
    matrix = _matrix(1.3, 1.1)
    prob = matrix.hhad(-1.0)["h"]
    for fixture in fixtures:
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture, 1.3, 1.1)
        _snap(db, fixture, "hhad", "h", round(1.5 / prob, 2), meta={"goal_line": "-1"})
    db.commit()

    first = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert first["bets"] == 3  # 三对串关（每场恰 2 张）
    # 晚到观测（seen_at ≤ 锚）改联合 EV 排序后重跑
    _snap(
        db,
        fixtures[0],
        "hhad",
        "h",
        round(2.5 / prob, 2),
        meta={"goal_line": "-1"},
        at="2026-11-08T12:59:00+00:00",
    )
    db.commit()
    rerun = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert rerun["bets"] == 0
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM bets WHERE strategy_version='auto-v1'"
        ).fetchone()["c"]
        == 3
    )


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

    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] >= 1  # 合线场照常
    rows = db.execute(
        "SELECT DISTINCT l.fixture_id FROM bets b JOIN bet_legs l ON l.bet_id=b.id"
        " WHERE b.strategy_version='auto-v1' AND l.market_code='hhad'"
    ).fetchall()
    assert f_bad not in {int(r["fixture_id"]) for r in rows}  # 混线/坏线场被排除
