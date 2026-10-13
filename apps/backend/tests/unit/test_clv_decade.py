"""CLV 十年仪表盘（票 20）：结构校验/早锁价值/线漂移的行为断言。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

import pytest

from goalx_backend.data import gold as gold_mod
from goalx_backend.evaluation import clv_decade as clvd

DAY = "2025-10-01"
PROBS = {"h": 0.45, "d": 0.30, "a": 0.25}


def _gold_row(sid: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sid": sid,
        "league": "英超",
        "kickoff": datetime.fromisoformat(f"{DAY}T20:00:00"),
        "season": "2025-26",
        "home": "A",
        "away": "B",
        "home_goals": 2,
        "away_goals": 1,
        "stage": "常规轮",
        "era": gold_mod.ERA_TRAJECTORY,
        "admin_excluded": False,
        "version": gold_mod.GOLD_VERSION,
    }
    return base | overrides


def _seed_run(face: sqlite3.Connection, haircut: float = 0.1) -> int:
    cur = face.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at)"
        " VALUES ('t20', ?, 'done', '2026-10-14T00:00:00+00:00')",
        (json.dumps({"haircut": haircut, "markets": ["had", "hhad"]}),),
    )
    return int(cur.lastrowid or 0)


def _seed_pred(face: sqlite3.Connection, run_id: int, key: str) -> None:
    face.execute(
        """
        INSERT INTO backtest_predictions
        (run_id, match_key, competition, season, match_date, home_team, away_team,
         era, ftr, had_probs, fair_probs, fair_source, model_fingerprint,
         train_window_end)
        VALUES (?, ?, 'E0', '2025-26', '2025-10-01', 'A', 'B', NULL, 'H', ?, ?,
                'psc', 'fp', '2025-09-25')
        """,
        (run_id, key, json.dumps(PROBS), json.dumps(PROBS)),
    )


def _seed_bet(
    face: sqlite3.Connection, run_id: int, legs: list[dict[str, Any]]
) -> None:
    hits = [{"hit": True, "void": False} for _ in legs]
    face.execute(
        """
        INSERT INTO backtest_bets
        (run_id, kind, competition, placed_week, stake, legs, ev, kelly,
         status, payout, profit, detail)
        VALUES (?, 'single', 'E0', '2025-W40', 100, ?, 0.1, 0.1, 'won', 200, 100, ?)
        """,
        (run_id, json.dumps(legs), json.dumps({"notes": [], "legs": hits})),
    )


def _leg(key: str, market: str = "had", odds: float = 2.0) -> dict[str, Any]:
    return {
        "match_key": key,
        "market_code": market,
        "selection_code": "h",
        "odds": odds,
        "model_prob": 0.5,
        "goal_line": None,
    }


def _devig_h(h: float, d: float, a: float) -> float:
    inv = [1 / h, 1 / d, 1 / a]
    return inv[0] / sum(inv)


def test_structural_identity_and_implied_band(gold_env):
    """had：odds×fair_p ≡ 1−haircut；hhad：隐含 p 带内计数。"""
    rows = [_gold_row("s1"), _gold_row("s2")]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face, haircut=0.1)
        _seed_pred(face, run_id, "s1")
        _seed_pred(face, run_id, "s2")
        # had：fair h=0.45, odds=2.0 → 0.9 = 1−0.1 恒等
        _seed_bet(face, run_id, [_leg("s1", "had", 2.0)])
        # hhad：odds=1.8 → implied=0.9/1.8=0.5
        _seed_bet(face, run_id, [_leg("s2", "hhad", 1.8)])
        payload = clvd.build_clv_decade_report(face, duck_con, store, run_id=run_id)
    had = payload["structural"]["had"]
    assert had["identity_median"] == pytest.approx(0.9, abs=1e-6)
    assert had["identity_outside"] == 0
    hhad = payload["structural"]["hhad"]
    assert hhad["implied_median"] == pytest.approx(0.5)
    assert hhad["implied_out_of_band"] == 0
    assert "identity_median" not in hhad  # 构造恒等式如实声明（had 专属实证）


def test_structural_band_is_legal_post_haircut_floor(gold_env):
    """贴护栏上限的合法腿（odds=50→implied=(1−h)/50≈0.018<0.02）不算带外。"""
    rows = [_gold_row("s1")]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face, haircut=0.1)
        _seed_pred(face, run_id, "s1")
        _seed_bet(face, run_id, [_leg("s1", "ttg", 50.0)])
        payload = clvd.build_clv_decade_report(face, duck_con, store, run_id=run_id)
    ttg = payload["structural"]["ttg"]
    assert ttg["implied_min"] == pytest.approx(0.9 / 50.0)
    assert ttg["implied_out_of_band"] == 0  # 法定下界=(1−h)×MIN<0.018


def test_early_lock_beat_math_q24h(gold_env):
    """era2 q24h 对 close1x2 锚：开盘价更高 → 市场向选中侧前移=beat。"""
    rows = [
        _gold_row(
            "s1",
            close1x2_h=2.0,
            close1x2_d=3.4,
            close1x2_a=3.8,
            q24h1x2_h=2.4,
            q24h1x2_d=3.4,
            q24h1x2_a=3.8,
        )
    ]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s1")
        _seed_bet(face, run_id, [_leg("s1", "had", 2.0)])
        payload = clvd.build_clv_decade_report(face, duck_con, store, run_id=run_id)
    face_row = payload["early_lock"]["q24h:trajectory"]
    expected_clv = _devig_h(2.0, 3.4, 3.8) - _devig_h(2.4, 3.4, 3.8)
    assert face_row["n"] == 1
    assert face_row["beat_rate"] == 1.0
    assert face_row["clv_median"] == pytest.approx(expected_clv, abs=1e-6)
    # 联赛分层面（gold league 列）与 era 面同值
    league_row = payload["early_lock_by_league"]["q24h:trajectory:英超"]
    assert league_row["clv_median"] == pytest.approx(expected_clv, abs=1e-6)


def test_early_lock_psh_pair_era1_and_family_split(gold_env):
    """era1 PSH→PSC 族内对；q24h 与 psh 同场并存时各成面不混。"""
    rows = [
        _gold_row(
            "s1",
            era=gold_mod.ERA_PSC_PROXY,
            psh_home=2.6,
            psh_draw=3.4,
            psh_away=3.8,
            psc_home=2.0,
            psc_draw=3.4,
            psc_away=3.8,
            q24h1x2_h=2.2,
            q24h1x2_d=3.4,
            q24h1x2_a=3.8,
            close1x2_h=2.0,
            close1x2_d=3.4,
            close1x2_a=3.8,
        )
    ]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s1")
        _seed_bet(face, run_id, [_leg("s1", "had", 2.0)])
        payload = clvd.build_clv_decade_report(face, duck_con, store, run_id=run_id)
    psh = payload["early_lock"]["psh_open:psc_proxy"]
    q24 = payload["early_lock"]["q24h:psc_proxy"]
    assert psh["n"] == 1
    assert psh["clv_median"] == pytest.approx(
        _devig_h(2.0, 3.4, 3.8) - _devig_h(2.6, 3.4, 3.8), abs=1e-6
    )
    assert q24["n"] == 1  # q24h 与 psh 同场并存：各成面不混


def test_pick_dedup_and_market_na_coverage(gold_env):
    """同 (sid,sel) 两注只算一次早锁位移；hhad/ttg 计 early_n_a。"""
    rows = [
        _gold_row(
            "s1",
            close1x2_h=2.0,
            close1x2_d=3.4,
            close1x2_a=3.8,
            q24h1x2_h=2.4,
            q24h1x2_d=3.4,
            q24h1x2_a=3.8,
        )
    ]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s1")
        _seed_bet(face, run_id, [_leg("s1", "had", 2.0)])
        _seed_bet(face, run_id, [_leg("s1", "had", 2.0)])  # 同选样第二注
        _seed_bet(face, run_id, [_leg("s1", "ttg", 8.0)])
        payload = clvd.build_clv_decade_report(face, duck_con, store, run_id=run_id)
    assert payload["n_legs"] == 3
    assert payload["n_unique_had_picks"] == 1
    assert payload["early_lock"]["q24h:trajectory"]["n"] == 1
    assert payload["coverage"]["had:trajectory"]["legs"] == 2
    assert payload["coverage"]["ttg:trajectory"]["early_n_a"] == 1


def test_missing_gold_sid_counted_no_gold(gold_env):
    """腿的 gold 行缺席：coverage 记 no_gold，不出早锁面。"""
    rows = [_gold_row("s1")]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s1")
        _seed_pred(face, run_id, "s2")  # 无 gold 行
        _seed_bet(face, run_id, [_leg("s2", "had", 2.0)])
        payload = clvd.build_clv_decade_report(face, duck_con, store, run_id=run_id)
    assert payload["coverage"]["had:no_gold"]["legs"] == 1
    assert payload["early_lock"] == {}


def test_drift_picked_vs_all(gold_env):
    """线漂移描述面：被下注场次与全预测场次分列。"""
    rows = [
        _gold_row("s1", ah_line_drift=0.25, ou_line_drift=-0.5),
        _gold_row("s2", ah_line_drift=0.0, ou_line_drift=0.0),
    ]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s1")
        _seed_pred(face, run_id, "s2")
        _seed_bet(face, run_id, [_leg("s1", "had", 2.0)])
        payload = clvd.build_clv_decade_report(face, duck_con, store, run_id=run_id)
    picked = payload["drift"]["picked"]["ah_line_drift"]
    overall = payload["drift"]["all"]["ah_line_drift"]
    assert picked["n"] == 1
    assert picked["median"] == 0.25
    assert overall["n"] == 2
    assert overall["median"] == pytest.approx(0.125)


def test_devig_guards_skip_degenerate_family(gold_env):
    """族内赔率缺失/≤1：整族不可用，早锁面不产生。"""
    rows = [
        _gold_row(
            "s1",
            close1x2_h=2.0,
            close1x2_d=3.4,
            close1x2_a=3.8,
            q24h1x2_h=0.9,  # ≤1 退化
            q24h1x2_d=3.4,
            q24h1x2_a=3.8,
        )
    ]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s1")
        _seed_bet(face, run_id, [_leg("s1", "had", 2.0)])
        payload = clvd.build_clv_decade_report(face, duck_con, store, run_id=run_id)
    assert payload["early_lock"] == {}


def test_degenerate_leg_odds_counted_not_fatal(gold_env):
    """odds=0/缺失的腐败腿：计数 degenerate，报告照常（correctness W1）。"""
    rows = [_gold_row("s1")]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s1")
        leg0 = _leg("s1", "had", 2.0)
        leg0["odds"] = 0.0
        leg_missing = _leg("s1", "hhad", 1.8)
        del leg_missing["odds"]
        _seed_bet(face, run_id, [leg0])
        _seed_bet(face, run_id, [leg_missing])
        payload = clvd.build_clv_decade_report(face, duck_con, store, run_id=run_id)
    assert payload["structural"]["had"]["degenerate"] == 1
    assert payload["structural"]["hhad"]["degenerate"] == 1
    assert payload["structural"]["had"]["n"] == 1


def test_duplicate_sid_gold_rows_first_wins(gold_env):
    """重复 sid gold 行：定序取首（与引擎面同谱系，correctness W2）。"""
    rows = [
        _gold_row(
            "s1",
            league="英超",
            q24h1x2_h=2.4,
            q24h1x2_d=3.4,
            q24h1x2_a=3.8,
            close1x2_h=2.0,
            close1x2_d=3.4,
            close1x2_a=3.8,
        ),
        _gold_row(  # 同 sid 更晚日：重复行不得顶替首行
            "s1",
            league="英超",
            kickoff=datetime.fromisoformat("2025-10-02T20:00:00"),
            q24h1x2_h=5.0,
            q24h1x2_d=3.4,
            q24h1x2_a=3.8,
            close1x2_h=2.0,
            close1x2_d=3.4,
            close1x2_a=3.8,
        ),
    ]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s1")
        _seed_bet(face, run_id, [_leg("s1", "had", 2.0)])
        payload = clvd.build_clv_decade_report(face, duck_con, store, run_id=run_id)
    clv = payload["early_lock"]["q24h:trajectory"]["clv_median"]
    assert clv == pytest.approx(
        _devig_h(2.0, 3.4, 3.8) - _devig_h(2.4, 3.4, 3.8), abs=1e-6
    )


def test_beats_half_survives_rounding(gold_env):
    """beat 判定用未圆整计数：2/3 过半不被 round(0.6667) 类圆整吞掉。"""
    face = clvd._EarlyFace()
    face.add(0.01)
    face.add(0.02)
    face.add(-0.03)
    as_dict = face.as_dict()
    assert as_dict["beat_rate"] == 0.6667
    assert as_dict["beats_half"] is True


def test_era_null_gold_row_goes_to_unknown_bucket(gold_env):
    """gold 行 era 为空：归 unknown 桶，不落字面 'None'（correctness I2）。"""
    rows = [_gold_row("s1", era=None)]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s1")
        _seed_bet(face, run_id, [_leg("s1", "had", 2.0)])
        payload = clvd.build_clv_decade_report(face, duck_con, store, run_id=run_id)
    assert payload["coverage"]["had:unknown"]["legs"] == 1
    assert not any(":None" in key for key in payload["coverage"])


def test_report_files_written(gold_env):
    """reports/ 双视图落盘，MD 含三面标题与判读注意。"""
    rows = [
        _gold_row(
            "s1",
            close1x2_h=2.0,
            close1x2_d=3.4,
            close1x2_a=3.8,
            q24h1x2_h=2.4,
            q24h1x2_d=3.4,
            q24h1x2_a=3.8,
        )
    ]
    with gold_env(rows) as (face, duck_con, store):
        run_id = _seed_run(face)
        _seed_pred(face, run_id, "s1")
        _seed_bet(face, run_id, [_leg("s1", "had", 2.0)])
        clvd.build_clv_decade_report(face, duck_con, store, run_id=run_id)
        reports = store.root / "reports"
        assert (reports / "clv-decade.json").exists()
        md = (reports / "clv-decade.md").read_text(encoding="utf-8")
    assert "结构性校验" in md
    assert "早锁价值" in md
    assert "线漂移" in md
    assert "判读注意" in md
