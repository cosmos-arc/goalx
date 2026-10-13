"""十年分时代技能报告（票 19）：纯 sqlite 读面的行为断言。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.evaluation import decade_skill as dsk

PROBS_HDA = {"h": 0.45, "d": 0.30, "a": 0.25}


def _seed_run(
    conn: sqlite3.Connection, label: str, params: dict[str, Any] | None = None
) -> int:
    payload = json.dumps(params or {"haircut": 0.1, "markets": ["had"]})
    cur = conn.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at)"
        " VALUES (?, ?, 'done', '2026-10-14T00:00:00+00:00')",
        (label, payload),
    )
    return int(cur.lastrowid or 0)


def _seed_pred(
    conn: sqlite3.Connection,
    run_id: int,
    key: str,
    *,
    era: str | None,
    ftr: str | None = "H",
    competition: str = "E0",
    season: str = "2023-24",
    had: dict[str, float] | None = None,
    fair: dict[str, float] | None = None,
    fair_source: str = "psc",
) -> None:
    conn.execute(
        """
        INSERT INTO backtest_predictions
        (run_id, match_key, competition, season, match_date, home_team, away_team,
         era, ftr, had_probs, fair_probs, fair_source, model_fingerprint,
         train_window_end)
        VALUES (?, ?, ?, ?, '2023-10-01', 'H', 'A', ?, ?, ?, ?, ?, 'fp', '2023-09-25')
        """,
        (
            run_id,
            key,
            competition,
            season,
            era,
            ftr,
            json.dumps(had or PROBS_HDA),
            json.dumps(fair or PROBS_HDA),
            fair_source,
        ),
    )


def _seed_bet(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    legs: list[dict[str, Any]],
    hit_flags: list[dict[str, bool]],
    status: str = "won",
    stake: float = 100.0,
    profit: float = 50.0,
    ev: float = 0.1,
    competition: str = "E0",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO backtest_bets
        (run_id, kind, competition, placed_week, stake, legs, ev, kelly,
         status, payout, profit, detail)
        VALUES (?, ?, ?, '2023-W40', ?, ?, ?, 0.1, ?, ?, ?, ?)
        """,
        (
            run_id,
            "single" if len(legs) == 1 else "parlay2",
            competition,
            stake,
            json.dumps(legs, ensure_ascii=False),
            ev,
            status,
            stake + profit,
            profit,
            json.dumps({"notes": [], "legs": hit_flags}, ensure_ascii=False),
        ),
    )
    return int(cur.lastrowid or 0)


def _leg(
    key: str,
    market: str = "had",
    odds: float = 2.0,
    model_prob: float = 0.6,
) -> dict[str, Any]:
    return {
        "match_key": key,
        "market_code": market,
        "selection_code": "h",
        "odds": odds,
        "model_prob": model_prob,
        "goal_line": None,
    }


@pytest.fixture
def store(tmp_path: Path) -> CorpusStore:
    return CorpusStore(tmp_path)


def test_prediction_faces_grouping_and_skill(db, store):
    """era/era:联赛/赛季分层各就位；完美模型 vs 均匀基准 skill=1。"""
    run_id = _seed_run(db, "t19")
    uniform = {"h": 1 / 3, "d": 1 / 3, "a": 1 / 3}
    perfect = {"h": 1.0, "d": 0.0, "a": 0.0}
    _seed_pred(db, run_id, "s1", era="psc_proxy", had=perfect, fair=uniform)
    _seed_pred(
        db,
        run_id,
        "s2",
        era="psc_proxy",
        competition="D1",
        had=perfect,
        fair=uniform,
    )
    _seed_pred(
        db,
        run_id,
        "s3",
        era="trajectory",
        season="2024-25",
        had=perfect,
        fair=uniform,
    )
    payload = dsk.build_decade_skill_report(db, store, run_id=run_id)
    faces = payload["prediction_faces"]
    assert faces["overall"]["n"] == 3
    # 完美模型 rps=0 → skill=1（两腿一胜一负仍全对三分类中的 H 桶）
    assert faces["era:psc_proxy"]["n"] == 2
    assert faces["era:psc_proxy"]["skill_rps"] == pytest.approx(1.0)
    assert faces["era:psc_proxy:lg:E0"]["n"] == 1
    assert faces["season:2024-25"]["n"] == 1
    assert payload["fair_source_mix"]["psc_proxy"] == {"psc": 2}


def test_era_null_rows_counted_not_grouped(db, store):
    """v25 前旧行（era 空）：不进 era 面，n_era_missing 如实计数。"""
    run_id = _seed_run(db, "old")
    _seed_pred(db, run_id, "s1", era=None)
    payload = dsk.build_decade_skill_report(db, store, run_id=run_id)
    assert "era:None" not in payload["prediction_faces"]
    assert payload["n_era_missing_predictions"] == 1
    assert payload["prediction_faces"]["overall"]["n"] == 1


def test_bet_faces_market_dedup_and_hit_rate(db, store):
    """同玩法串关只计一次；void 不进命中率；混合 era 串关进 mixed 面。"""
    run_id = _seed_run(db, "t19")
    _seed_pred(db, run_id, "s1", era="psc_proxy")
    _seed_pred(db, run_id, "s2", era="psc_proxy")
    _seed_pred(db, run_id, "s3", era="trajectory")
    _seed_bet(
        db,
        run_id,
        legs=[_leg("s1"), _leg("s2")],
        hit_flags=[{"hit": True, "void": False}, {"hit": True, "void": False}],
        profit=260.0,
    )
    _seed_bet(
        db,
        run_id,
        legs=[_leg("s3", market="ttg")],
        hit_flags=[{"hit": False, "void": False}],
        status="lost",
        profit=-100.0,
    )
    _seed_bet(
        db,
        run_id,
        legs=[_leg("s1", market="hhad", odds=2.4)],
        hit_flags=[{"hit": False, "void": True}],
        status="void",
        profit=0.0,
    )
    payload = dsk.build_decade_skill_report(db, store, run_id=run_id)
    faces = payload["betting_faces"]
    # 同玩法 parlay2 在 mkt:had 只计一注
    assert faces["era:psc_proxy"]["n"] == 2  # parlay + void hhad
    assert faces["era:psc_proxy:mkt:had"]["n"] == 1
    assert faces["era:psc_proxy:mkt:hhad"]["n"] == 1
    # era face: psc 2 注（1 won 1 void）→ hit_rate 只看 won/(won+lost)
    assert faces["era:psc_proxy"]["hit_rate"] == 1.0
    assert faces["era:trajectory:mkt:ttg"]["hit_rate"] == 0.0
    assert faces["era:psc_proxy:mkt:had"]["roi_stake_weighted"] == pytest.approx(2.6)


def test_mixed_era_parlay_goes_to_mixed_face(db, store):
    """跨时代串关：era 归 mixed（只计数），不进单时代面。"""
    run_id = _seed_run(db, "t19")
    _seed_pred(db, run_id, "s1", era="psc_proxy")
    _seed_pred(db, run_id, "s2", era="trajectory")
    _seed_bet(
        db,
        run_id,
        legs=[_leg("s1"), _leg("s2")],
        hit_flags=[{"hit": True, "void": False}, {"hit": True, "void": False}],
    )
    payload = dsk.build_decade_skill_report(db, store, run_id=run_id)
    assert payload["betting_faces"]["era:mixed"]["n"] == 1
    assert "era:psc_proxy" not in payload["betting_faces"]
    assert "era:trajectory" not in payload["betting_faces"]


def test_leg_face_brier_skill_and_calibration(db, store):
    """腿面：p_model vs 隐含(1−haircut)/odds 的 Brier skill 手算对齐。"""
    run_id = _seed_run(db, "t19", params={"haircut": 0.1, "markets": ["had"]})
    _seed_pred(db, run_id, "s1", era="psc_proxy")
    # odds=1.8 → implied=0.9/1.8=0.5；hit=True
    _seed_bet(
        db,
        run_id,
        legs=[_leg("s1", odds=1.8, model_prob=0.7)],
        hit_flags=[{"hit": True, "void": False}],
    )
    payload = dsk.build_decade_skill_report(db, store, run_id=run_id)
    face = payload["leg_faces"]["had"]
    assert face["n"] == 1
    assert face["implied_mean"] == pytest.approx(0.5)
    assert face["brier_model"] == pytest.approx((0.7 - 1.0) ** 2)
    assert face["brier_implied"] == pytest.approx((0.5 - 1.0) ** 2)
    assert face["skill_brier"] == pytest.approx(1 - 0.09 / 0.25)
    assert face["model_prob_mean"] == pytest.approx(0.7)


def test_ev_realization_ratio(db, store):
    """EV 实现率=Σ利润/Σ(ev×stake)；负/零期望分母置空。"""
    run_id = _seed_run(db, "t19")
    _seed_pred(db, run_id, "s1", era="psc_proxy")
    _seed_bet(  # 期望 0.2×100=20，实现 50 → 2.5
        db,
        run_id,
        legs=[_leg("s1")],
        hit_flags=[{"hit": True, "void": False}],
        ev=0.2,
        profit=50.0,
    )
    payload = dsk.build_decade_skill_report(db, store, run_id=run_id)
    ev = payload["ev_realization"]["psc_proxy"]
    assert ev["expected_profit"] == pytest.approx(20.0)
    assert ev["realized_profit"] == pytest.approx(50.0)
    assert ev["realization"] == pytest.approx(2.5)


def test_default_run_picks_latest_done_and_missing_raises(db, store):
    """缺省 run=最新 done；显式不存在 run 报错。"""
    _seed_run(db, "older")
    latest = _seed_run(db, "newer")
    payload = dsk.build_decade_skill_report(db, store)
    assert payload["run_id"] == latest
    with pytest.raises(ValueError, match="不存在"):
        dsk.build_decade_skill_report(db, store, run_id=999)
    from goalx_backend.db import connect, migrate

    conn2 = connect(":memory:")
    migrate(conn2)
    try:
        with pytest.raises(ValueError, match="done"):
            dsk.build_decade_skill_report(conn2, store)
    finally:
        conn2.close()


def test_report_files_written_with_verdicts(db, store):
    """reports/ 双视图落盘；MD 含结论段与 era 对照表。"""
    run_id = _seed_run(db, "t19")
    _seed_pred(db, run_id, "s1", era="psc_proxy")
    _seed_bet(
        db,
        run_id,
        legs=[_leg("s1")],
        hit_flags=[{"hit": True, "void": False}],
    )
    dsk.build_decade_skill_report(db, store, run_id=run_id, today=None)
    reports = store.root / "reports"
    assert (reports / "decade-skill.json").exists()
    md = (reports / "decade-skill.md").read_text(encoding="utf-8")
    assert "## 结论" in md
    assert "psc_proxy" in md
    assert "era 对照" in md


def test_bet_without_detail_raises(db, store):
    """缺结算 detail 的注（老形态）：腿级面不可信，fail-loud。"""
    run_id = _seed_run(db, "old")
    _seed_pred(db, run_id, "s1", era="psc_proxy")
    _seed_bet(
        db,
        run_id,
        legs=[_leg("s1")],
        hit_flags=[{"hit": True, "void": False}],
    )
    db.execute("UPDATE backtest_bets SET detail = NULL")
    with pytest.raises(ValueError, match="detail"):
        dsk.build_decade_skill_report(db, store, run_id=run_id)
