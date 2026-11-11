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
from goalx_backend.betting import versioning
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


# ---- 纯选票规则（prereg 修订①-④）----


def _make_v1_live_with(db: sqlite3.Connection, params: au.AutoV1Params) -> None:
    """注册指定参数集的引擎三元组并人审升 live-paper（票 01 闸同链）。"""
    estimator, calibration, fingerprint = au.engine_version_triple(params)
    version_id = versioning.register_version(
        db,
        estimator_version=estimator,
        calibration_version=calibration,
        params_version=fingerprint,
        label=au.AUTO_V1,
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


def _make_v1_live(db: sqlite3.Connection) -> None:
    """注册 auto-v1 引擎三元组并人审升 live-paper（票 01 闸 fail-closed）。"""
    _make_v1_live_with(db, au.AUTO_V1_PARAMS)


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


def test_quarter_kelly_tax_free_cap_boundary() -> None:
    """票 12 税感知帽边界两侧：奖金 ¥9,999 直过、¥10,001 压帽。

    odds=2.0、prob=0.75：kelly=(0.75×2−1)/1=0.5 → quarter×bankroll
    =0.125×bankroll。stake_cap 放大使其不绑，只剩免税帽约束：
    - bankroll 39,996 → 原始注额 ¥4,999.50，奖金 ¥9,999（免征桶内）→ 直过；
    - bankroll 40,004 → 原始注额 ¥5,000.50，奖金 ¥10,001（越线全额 20%）
      → 压帽 floor(10000/2)=¥5,000，奖金恰 ¥10,000（"不超过 1 万"免）。
    """
    from goalx_backend.betting import tax
    from goalx_backend.betting.staking import tax_free_stake_cap

    params = au.AutoV1Params(stake_cap=1_000_000.0)
    under = au.quarter_kelly_stake(0.75, 2.0, 39_996.0, params)
    assert under == 4999.5  # 奖金 9,999：桶内直过
    over = au.quarter_kelly_stake(0.75, 2.0, 40_004.0, params)
    assert over == tax_free_stake_cap(2.0) == 5000.0  # 压帽到元
    # tax 视图复核：压帽前越线全额计税、压帽后免征（接 betting/tax 现有件）
    assert tax.tax_on_prize(5000.5 * 2.0) == (2000.2, 8000.8)
    assert tax.tax_on_prize(over * 2.0) == (0.0, 10_000.0)


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

    _make_v1_live(db)
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

    _make_v1_live(db)
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

    monkeypatch.setattr(au, "validate_selections", _reject)
    _make_v1_live(db)
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

    _make_v1_live(db)
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

    _make_v1_live(db)
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
    assert version == 57
    assert migrate(db) == 57  # 同版本重跑零效果
    assert db.execute("SELECT COUNT(*) c FROM auto_paper_runs").fetchone()["c"] == 0


def test_live_paper_singles_walk_tax_free_sizing(db: sqlite3.Connection) -> None:
    """
    票 12 参数层接线 e2e：live-paper 下单走税感知 sizing（票 01 状态消费同链）。

    高注码参数（大资金+高单注帽）使 quarter-Kelly 原始注额越免税线：
    注册**该参数指纹**的版本并人审逐级升 live-paper 后下单（票 01 闸同链
    fail-closed），落库注额=免税帽 floor(¥10,000/赔率)——奖金 ≤¥1 万
    永处免税桶。手算对照：prob(ttg=2)≈0.26129、odds=5.74 → 原始注额
    ¥5,272.19（>帽）→ 压帽 ¥1,742（奖金 ¥9,999.08）。
    """
    from goalx_backend.betting import tax
    from goalx_backend.betting.staking import tax_free_stake_cap

    fixture = _seed_fixture(db, "巳", code="周六021")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture, 1.3, 1.1)
    odds = _seed_positive_single(db, fixture, "2", ev=0.5)
    db.commit()

    params = au.AutoV1Params(initial_bankroll=200_000.0, stake_cap=10_000.0)
    _make_v1_live_with(db, params)
    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR, params=params)
    assert stats["bets"] == 1
    row = db.execute(
        "SELECT stake FROM bets WHERE strategy_version = 'auto-v1'"
    ).fetchone()
    assert row["stake"] == pytest.approx(tax_free_stake_cap(odds))
    assert row["stake"] == pytest.approx(1742.0)  # 手算：floor(10000/5.74)
    assert row["stake"] * odds <= tax.TAX_FREE_THRESHOLD  # 免税桶不变量


def test_parlay_stake_pressed_into_tax_free_bucket(db: sqlite3.Connection) -> None:
    """票 12：串关同压免税帽——联合赔率口径（整注语义，与 suggest_stake 同）。

    两腿 hhad 各 odds 9.68 → 联合 93.7024；parlay_stake 放大到 ¥50,000
    越线 → 压帽 floor(10000/93.7024)=¥106（奖金 ¥9,932.45 免征）。
    """
    from goalx_backend.betting.staking import tax_free_stake_cap

    f1 = _seed_fixture(db, "午", code="周日011")
    f2 = _seed_fixture(db, "未", code="周日012")
    matrix = _matrix(1.3, 1.1)
    for fixture in (f1, f2):
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture, 1.3, 1.1)
        prob = matrix.hhad(-1.0)["h"]
        odds = round(1.8 / prob, 2)
        _snap(db, fixture, "hhad", "h", odds, meta={"goal_line": "-1"})
        _snap(db, fixture, "ttg", "2", 1.2)  # 单关面负 EV → 只走串关路径
    db.commit()
    leg_odds = round(1.8 / matrix.hhad(-1.0)["h"], 2)

    params = au.AutoV1Params(parlay_stake=50_000.0)
    _make_v1_live_with(db, params)
    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR, params=params)
    assert stats["bets"] == 1  # 一张 2串1
    stake = db.execute(
        "SELECT stake FROM bets WHERE strategy_version = 'auto-v1'"
    ).fetchone()["stake"]
    assert stake == pytest.approx(tax_free_stake_cap(leg_odds * leg_odds))
    assert stake == pytest.approx(106.0)
    assert stake * leg_odds * leg_odds <= 10_000.0


def test_parlay_tax_cap_collapse_skips_with_trace(db: sqlite3.Connection) -> None:
    """correctness F1：串关压帽塌残注/负注额——跳过留痕不裸崩，台账可分。

    cap ¥100：floor(100/93.70)=¥1 残注（<¥2 竞彩最低注）→ 跳过计
    rejected+tax_cap_skipped；cap 负值 → 帽为负钳 0 同跳过（BetDraft
    校验不得裸冒泡——构造已挪进 _place 的 try，ValidationError 归入
    rejected 而非整锚崩）。全跳过日 zero_note=tax_cap_skipped。
    """
    f1 = _seed_fixture(db, "申", code="周一011")
    f2 = _seed_fixture(db, "酉", code="周一012")
    matrix = _matrix(1.3, 1.1)
    for fixture in (f1, f2):
        _seed_on_sale(db, fixture)
        _seed_forecast(db, fixture, 1.3, 1.1)
        prob = matrix.hhad(-1.0)["h"]
        odds = round(1.8 / prob, 2)
        _snap(db, fixture, "hhad", "h", odds, meta={"goal_line": "-1"})
        _snap(db, fixture, "ttg", "2", 1.2)  # 单关面负 EV → 只走串关路径
    db.commit()
    leg_odds = round(1.8 / matrix.hhad(-1.0)["h"], 2)
    assert leg_odds * leg_odds > 93.0  # 联合赔率量级钉住（93.70）

    for cap, anchor in ((100.0, "21:00"), (-50.0, "21:30")):
        params = au.AutoV1Params(parlay_stake=50_000.0, tax_free_prize_cap=cap)
        _make_v1_live_with(db, params)
        stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=anchor, params=params)
        assert stats["bets"] == 0
        assert stats["tax_cap_skipped"] == 1
        assert stats["gate_rejected"] >= 1  # 跳过计 rejected 语义（票 12 批修 F1）
        assert (
            db.execute(
                "SELECT COUNT(*) c FROM bets WHERE strategy_version='auto-v1'"
            ).fetchone()["c"]
            == 0
        )
        note = db.execute(
            "SELECT zero_note FROM auto_paper_runs"
            " WHERE business_date=? AND anchor_label=?",
            (BUSINESS_DATE, anchor),
        ).fetchone()["zero_note"]
        assert note == "tax_cap_skipped"


def test_single_tax_cap_residual_skips_not_places(db: sqlite3.Connection) -> None:
    """correctness F1 单关同款：cap ¥6 → floor(6/5.74)=¥1 残注——跳过留痕不下残注。"""
    fixture = _seed_fixture(db, "戌", code="周一021")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture, 1.3, 1.1)
    _seed_positive_single(db, fixture, "2", ev=0.5)  # odds 5.74
    db.commit()

    params = au.AutoV1Params(tax_free_prize_cap=6.0)
    _make_v1_live_with(db, params)
    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR, params=params)
    assert stats["bets"] == 0
    assert stats["tax_cap_skipped"] == 1
    stakes = db.execute(
        "SELECT COUNT(*) c FROM bets WHERE strategy_version='auto-v1'"
    ).fetchone()["c"]
    assert stakes == 0  # 残注路径：跳过（无 ¥1 注落库）


def test_shadow_version_refused_and_live_paper_places(db: sqlite3.Connection) -> None:
    """
    vision-build 票 01 e2e：shadow 态版本被引擎拒单且留痕；人审升
    live-paper 后放行。

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
    estimator, calibration, fingerprint = au.engine_version_triple(au.AUTO_V1_PARAMS)
    version_id = versioning.register_version(
        db,
        estimator_version=estimator,
        calibration_version=calibration,
        params_version=fingerprint,
        label=au.AUTO_V1,
    )
    versioning.transition_version(
        db, version_id, StrategyState.SHADOW, actor="human:test", reason="影子起跑"
    )

    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 0
    assert stats["version_not_live"] == 1
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM bets WHERE strategy_version='auto-v1'"
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
    live_stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert live_stats["bets"] == 3


def test_zero_rounded_stake_skips_not_aborts(db: sqlite3.Connection) -> None:
    """correctness F1：quarter-Kelly 取整到 ¥0 的微 edge——跳过计数，锚完整收班。"""
    fixture = _seed_fixture(db, "癸", code="周四001")
    _seed_on_sale(db, fixture)
    _seed_forecast(db, fixture, 1.3, 1.1)
    prob = float(_matrix(1.3, 1.1).ttg()["2"])
    _snap(db, fixture, "ttg", "2", round((1 + 1e-5) / prob, 4))  # EV=1e-5 → stake≈0
    db.commit()

    _make_v1_live(db)
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

    _make_v1_live(db)
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

    _make_v1_live(db)
    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] >= 1  # 合线场照常
    rows = db.execute(
        "SELECT DISTINCT l.fixture_id FROM bets b JOIN bet_legs l ON l.bet_id=b.id"
        " WHERE b.strategy_version='auto-v1' AND l.market_code='hhad'"
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

    _make_v1_live(db)
    stats = au.run_auto_v1(db, run_at=RUN_AT, anchor_label=ANCHOR)
    assert stats["bets"] == 0  # 两场唯一正 EV 面（ttg 单关）都被玩法闸拦下
    assert stats["candidates"] == 0
    assert stats["excluded_market_sale_stopped"] == 1
    assert stats["excluded_market_single_ineligible"] == 1
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM bets WHERE strategy_version='auto-v1'"
        ).fetchone()["c"]
        == 0
    )
