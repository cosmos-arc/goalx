"""回测引擎 v2 测试（backtest-decade 票 13：gold 消费/era 分层/防前视/run 隔离）。

合成 gold 行（conftest.gold_env 直写 parquet + 建桥）喂真实引擎全链：
era1/era2 公允链、窗口与卫生单一入口、sid 键位、结算路径统一、run 隔离
幂等。真源网络永不进测试。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import numpy as np
import pytest

from goalx_backend.data import gold as gold_mod
from goalx_backend.evaluation import backtest as bt
from goalx_backend.modelling.dc_model import TrainingRow

STRENGTH = {"A": 0.5, "B": 0.2, "C": -0.1, "D": -0.3, "E": 0.0, "F": -0.4}

ERA1_BALANCED = (2.30, 3.30, 3.10)  # 与实力无关的固定盘（断言可控）
ERA2_ANCHOR = (1.95, 3.40, 3.90)
ERA2_CONS = (0.50, 0.26, 0.24)


def _gold_row(
    sid: str,
    day: str,
    home: str,
    away: str,
    gh: int,
    ga: int,
    *,
    season: str = "2016-17",
    era: str = "psc_proxy",
    psc: tuple[float, float, float] | None = ERA1_BALANCED,
    anchor: tuple[float, float, float] | None = None,
    cons: tuple[float, float, float] | None = None,
    admin_excluded: bool = False,
    played: bool = True,
) -> dict[str, Any]:
    """gold match_features 种子行（收盘族三元组按需给）。"""
    from datetime import datetime

    return {
        "sid": sid,
        "league": "英超",
        "kickoff": datetime.fromisoformat(f"{day}T20:00:00"),
        "season": season,
        "home": home,
        "away": away,
        "home_goals": gh if played else None,
        "away_goals": ga if played else None,
        "stage": "常规轮",
        "era": era,
        "admin_excluded": admin_excluded,
        "psc_home": psc[0] if psc else None,
        "psc_draw": psc[1] if psc else None,
        "psc_away": psc[2] if psc else None,
        "close1x2_h": anchor[0] if anchor else None,
        "close1x2_d": anchor[1] if anchor else None,
        "close1x2_a": anchor[2] if anchor else None,
        "close1x2_cons_h": cons[0] if cons else None,
        "close1x2_cons_d": cons[1] if cons else None,
        "close1x2_cons_a": cons[2] if cons else None,
        "version": gold_mod.GOLD_VERSION,
    }


def _era1_row(sid: str, day: str, home: str, away: str, gh: int, ga: int) -> dict:
    return _gold_row(sid, day, home, away, gh, ga)


def _era2_row(sid: str, day: str, home: str, away: str, gh: int, ga: int) -> dict:
    return _gold_row(
        sid,
        day,
        home,
        away,
        gh,
        ga,
        season="2024-25",
        era=gold_mod.ERA_TRAJECTORY,
        psc=None,
        anchor=ERA2_ANCHOR,
        cons=ERA2_CONS,
    )


def seed_rows(*, rounds: int = 14, start: date = date(2016, 8, 6)) -> list[dict]:
    """合成联赛：每周 3 场（6 队），era1 起步；末两周接 era2 场。"""
    rng = np.random.default_rng(21)
    teams = sorted(STRENGTH)
    rows: list[dict] = []
    sid = 8000
    for round_no in range(rounds):
        day = (start + timedelta(days=7 * round_no)).isoformat()
        for i in range(3):
            home, away = teams[i], teams[5 - i]
            lam_h = float(np.exp(0.3 + STRENGTH[home] - STRENGTH[away]))
            lam_a = float(np.exp(STRENGTH[away] - STRENGTH[home]))
            gh, ga = int(rng.poisson(lam_h)), int(rng.poisson(lam_a))
            sid += 1
            maker = _era2_row if round_no >= rounds - 2 else _era1_row
            rows.append(maker(str(sid), day, home, away, gh, ga))
    return rows


def _gold_match(**overrides: Any) -> bt.GoldMatch:
    """GoldMatch 最小构造（fair/candidates 单测用）。"""
    base: dict[str, Any] = {
        "sid": "s1",
        "competition": "E0",
        "match_date": "2021-10-02",
        "season": "2021-22",
        "home": "A",
        "away": "B",
        "fthg": 2,
        "ftag": 1,
        "era": "psc_proxy",
        "ref": 0,
        "psc_odds": (2.2, 3.4, 3.2),
        "avgc_odds": (2.1, 3.3, 3.1),
        "anchor_odds": (None, None, None),
        "cons_probs": (None, None, None),
    }
    return bt.GoldMatch(**(base | overrides))


def test_match_week_grouping() -> None:
    rows = [
        _gold_match(sid="1", match_date="2024-09-14"),
        _gold_match(sid="2", match_date="2024-09-15"),
        _gold_match(sid="3", match_date="2024-09-21"),
    ]
    weeks = bt.group_by_week(rows)
    assert len(weeks) == 2
    assert [m.sid for m in weeks[0][1]] == ["1", "2"]


def test_assert_no_lookahead_trips_on_leak() -> None:
    train = [TrainingRow("2024-09-15", "A", "B", 1, 0)]
    with pytest.raises(AssertionError, match="look-ahead"):
        bt.assert_no_lookahead(train, ["2024-09-14"])
    # 严格早于 → 通过
    bt.assert_no_lookahead(train, ["2024-09-16"])


def test_fair_probs_era1_psc_preferred_avgc_fallback() -> None:
    probs, source = bt.fair_probs_from_gold(_gold_match())
    assert source == "psc"
    assert sum(probs.values()) == pytest.approx(1.0)
    probs, source = bt.fair_probs_from_gold(_gold_match(psc_odds=(None, None, None)))
    assert source == "avgc"
    assert (
        bt.fair_probs_from_gold(
            _gold_match(psc_odds=(None, None, None), avgc_odds=(None, None, None))
        )
        is None
    )


def test_fair_probs_era2_anchor_then_cons() -> None:
    match = _gold_match(
        era=gold_mod.ERA_TRAJECTORY, anchor_odds=ERA2_ANCHOR, cons_probs=ERA2_CONS
    )
    probs, source = bt.fair_probs_from_gold(match)
    assert source == "traj_anchor"  # 锚优先（与 era1 PSC 同为尖货收盘）
    assert sum(probs.values()) == pytest.approx(1.0, abs=1e-9)
    probs, source = bt.fair_probs_from_gold(
        _gold_match(era=gold_mod.ERA_TRAJECTORY, cons_probs=ERA2_CONS)
    )
    assert source == "traj_cons"  # 锚缺席 → 共识归一兜底
    assert probs["h"] == pytest.approx(ERA2_CONS[0])
    # era2 无轨迹收盘即无基准：psc 在场也不算（era 正典声明优先）
    assert (
        bt.fair_probs_from_gold(
            _gold_match(era=gold_mod.ERA_TRAJECTORY, psc_odds=(2.2, 3.4, 3.2))
        )
        is None
    )


def test_fair_source_selectors_override_era_chain() -> None:
    match = _gold_match(
        era=gold_mod.ERA_TRAJECTORY,
        psc_odds=(2.2, 3.4, 3.2),
        anchor_odds=ERA2_ANCHOR,
        cons_probs=ERA2_CONS,
    )
    # psc/avgc = 源选择器（票 34 对照），跨 era 作用于配对行
    probs, source = bt.fair_probs_from_gold(match, fair_source="psc")
    assert source == "psc"
    assert probs is not None
    assert (
        bt.fair_probs_from_gold(
            _gold_match(avgc_odds=(None, None, None)), fair_source="avgc"
        )
        is None
    )  # 强制缺失源 → 无基准，不兜底


def test_simulated_jc_odds_haircut() -> None:
    fair = {"h": 0.5, "d": 0.3, "a": 0.2}
    odds = bt.simulated_jc_odds(fair, haircut=0.10)
    assert odds["h"] == pytest.approx(2.0 * 0.9)
    # 概率域隐含 = fair / (1 - haircut)
    assert 1.0 / odds["h"] == pytest.approx(0.5 / 0.9)


def test_market_implied_matrix_recovers_had() -> None:
    matrix = bt.market_implied_matrix({"h": 0.45, "d": 0.28, "a": 0.27})
    assert matrix is not None
    had = matrix.had()
    assert had["h"] == pytest.approx(0.45, abs=0.01)
    assert had["a"] == pytest.approx(0.27, abs=0.01)


def test_pick_hhad_line_balances_sides() -> None:
    matrix = bt.market_implied_matrix({"h": 0.62, "d": 0.22, "a": 0.16})
    assert matrix is not None
    line = bt.pick_hhad_line(matrix)
    probs = matrix.hhad(float(line))
    assert abs(probs["h"] - probs["a"]) < 0.1
    assert line <= 0  # 主热门 → 需要让球


def test_kelly_stake_cap_and_threshold() -> None:
    params = bt.BacktestParams()
    # 大 EV：1/4 Kelly × 5000 > 50 → 封顶
    stake, kelly = bt.kelly_stake(0.8, 2.0, params)
    assert stake == 50.0
    assert kelly == pytest.approx(0.6)
    # 小 EV：EV>0 但按比例下注
    stake, _ = bt.kelly_stake(0.52, 2.0, params)
    assert 0 < stake <= 50.0
    # EV=0 → 0
    assert bt.kelly_stake(0.5, 2.0, params) == (0.0, 0.0)


def test_candidates_had_only_respect_price_bounds() -> None:
    # v1 had-only：极端热门市场（尾部隐含概率 <2%）不产生候选（竞彩实际不报价）
    params = bt.BacktestParams(max_sim_odds=50.0, haircut=0.11)
    from goalx_backend.modelling.score_matrix import ScoreMatrix

    matrix = ScoreMatrix.from_lambdas(2.4, 1.0)
    match = _gold_match()
    extreme_jc = bt.simulated_jc_odds({"h": 0.91, "d": 0.05, "a": 0.04}, 0.11)
    candidates = bt._candidates_for_match(matrix, extreme_jc, params, match)
    # h(0.98)/a(22.2) 在界内，d(17.8) 在界内 → 3 个 had 候选
    assert {cand.selection_code for cand in candidates} == {"h", "d", "a"}
    assert all(cand.odds <= 50.0 for cand in candidates)
    # 尾部隐含 1.4% 的选项被下限排除
    tail_jc = bt.simulated_jc_odds({"h": 0.955, "d": 0.031, "a": 0.014}, 0.11)
    tail_candidates = bt._candidates_for_match(matrix, tail_jc, params, match)
    assert all(cand.selection_code != "a" for cand in tail_candidates)


def test_run_backtest_end_to_end(gold_env) -> None:
    with gold_env(seed_rows()) as (face, duck_con, store):
        params = bt.BacktestParams(
            competitions=("E0",),
            min_train_matches=9,
            ev_threshold=0.0,  # 便于产生注单的测试设置
        )
        result = bt.run_backtest(face, duck_con, store, params, label="smoke")
        assert result.predictions >= 1
        assert result.bets >= 0
        summary = bt.run_summary(face, result.run_id)
        assert summary is not None
        assert summary["predictions"] == result.predictions
        # era 分层与源声明随预测行落库
        preds = face.execute(
            "SELECT * FROM backtest_predictions WHERE run_id = ? ORDER BY match_date",
            (result.run_id,),
        ).fetchall()
        sources = {str(p["fair_source"]) for p in preds}
        assert sources == {"psc", "traj_anchor"}  # era1 均衡盘 + era2 锚
        eras = {str(p["era"]) for p in preds}
        assert eras == {"psc_proxy", "trajectory"}
        for p in preds:
            assert str(p["match_key"]).isdigit()  # sid（合成树为数字串）
            assert str(p["ftr"]) in ("H", "D", "A")
        # 注单走 Settlement 代码路径：won/lost + 每注有 EV/Kelly/盈亏；legs 键=sid
        rows = face.execute(
            "SELECT * FROM backtest_bets WHERE run_id = ?", (result.run_id,)
        ).fetchall()
        for row in rows:
            assert str(row["status"]) in ("won", "lost", "void")
            legs = json.loads(str(row["legs"]))
            assert legs
            assert all("match_key" in leg for leg in legs)
            if row["kind"] == "parlay2":
                assert len(legs) == 2
                assert legs[0]["match_key"] != legs[1]["match_key"]
        # run params 钉 gold 溯源（版本/构建戳/digest/成熟度基准日=gold 构建侧）
        run = face.execute(
            "SELECT params FROM backtest_runs WHERE id = ?", (result.run_id,)
        ).fetchone()
        run_params = json.loads(str(run["params"]))
        assert run_params["gold_version"] == gold_mod.GOLD_VERSION
        assert run_params["gold_maturity_today"] == "2026-10-08"
        assert run_params["gold_input_digests"] == {"srct/fixture_universe": "deadbeef"}
        assert run_params["data_source"] == "gold/match_features"
        assert run_params["price_model"] == "simulated_jc"


def test_run_backtest_run_isolation(gold_env) -> None:
    with gold_env(seed_rows()) as (face, duck_con, store):
        params = bt.BacktestParams(
            competitions=("E0",), min_train_matches=9, ev_threshold=0.0
        )
        first = bt.run_backtest(face, duck_con, store, params, label="run-1")
        second = bt.run_backtest(face, duck_con, store, params, label="run-2")
        assert first.run_id != second.run_id
        counts = face.execute(
            "SELECT run_id, COUNT(*) AS c FROM backtest_bets GROUP BY run_id"
        ).fetchall()
        by_run = {int(r["run_id"]): int(r["c"]) for r in counts}
        assert by_run[first.run_id] == by_run.get(second.run_id, 0)
        statuses = face.execute("SELECT status FROM backtest_runs").fetchall()
        assert {str(r["status"]) for r in statuses} == {"done"}


def test_run_backtest_window_and_hygiene(gold_env) -> None:
    """窗口前场次只进训练池；行政判赛/未赛行不进预测面（单一取数口）。"""
    rows = seed_rows()
    # 2016-08 窗口前的 7 月热身行（只训练）+ 行政判赛行 + 未赛行
    rows.append(_era1_row("7001", "2016-07-09", "A", "B", 2, 0))
    rows.append(_era1_row("7002", "2016-07-16", "C", "D", 1, 1))
    rows.append(_era1_row("7003", "2016-08-20", "E", "F", 3, 0))
    rows[-1]["admin_excluded"] = True
    rows.append(_gold_row("7004", "2016-08-27", "A", "C", 0, 0, played=False))
    with gold_env(rows) as (face, duck_con, store):
        params = bt.BacktestParams(competitions=("E0",), min_train_matches=9)
        result = bt.run_backtest(face, duck_con, store, params, label="window")
        keys = {
            str(r["match_key"])
            for r in face.execute(
                "SELECT match_key FROM backtest_predictions WHERE run_id = ?",
                (result.run_id,),
            ).fetchall()
        }
        assert "7003" not in keys  # 行政判赛：训练/预测两 face 同口剔除
        assert "7004" not in keys  # 未赛行
        dates = [
            str(r["match_date"])
            for r in face.execute(
                "SELECT match_date FROM backtest_predictions WHERE run_id = ?",
                (result.run_id,),
            ).fetchall()
        ]
        assert dates  # 窗口前只训练
        assert min(dates) >= bt.DECADE_START


def test_run_backtest_missing_gold_view_records_honest_empty(gold_env) -> None:
    """视图缺席（空语料/未建桥）= 合法初生态：run 完成并如实标注，不冒充数据。"""
    with gold_env([]) as (face, duck_con, store):  # 空行集 → 无 parquet → 无视图
        params = bt.BacktestParams(competitions=("E0",))
        result = bt.run_backtest(face, duck_con, store, params, label="empty")
        assert result.predictions == 0
        run = face.execute(
            "SELECT params, status FROM backtest_runs WHERE id = ?",
            (result.run_id,),
        ).fetchone()
        assert str(run["status"]) == "done"
        assert json.loads(str(run["params"]))["gold_view"] == "missing"
        assert json.loads(str(run["params"]))["gold_rows"] == 0


def test_run_backtest_marks_failed_run(gold_env, monkeypatch) -> None:
    with gold_env(seed_rows()) as (face, duck_con, store):
        # 非 ValueError（引擎对 fit 的 ValueError 有容错跳周，RuntimeError 走失败面）
        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("fit exploded")

        monkeypatch.setattr(bt, "fit_dc_model", _boom)
        params = bt.BacktestParams(competitions=("E0",), min_train_matches=9)
        with pytest.raises(RuntimeError, match="fit exploded"):
            bt.run_backtest(face, duck_con, store, params, label="boom")
        row = face.execute(
            "SELECT status FROM backtest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert str(row["status"]) == "failed"


def test_backtest_lookahead_guard_via_weeks(gold_env) -> None:
    # 端到端防前视：训练截止取周最早日前一天，训练集不可能含当周比赛
    with gold_env(seed_rows()) as (face, duck_con, store):
        params = bt.BacktestParams(competitions=("E0",), min_train_matches=9)
        result = bt.run_backtest(face, duck_con, store, params, label="wf")
        preds = face.execute(
            """
            SELECT p.match_date, p.train_window_end FROM backtest_predictions p
            WHERE p.run_id = ?
            """,
            (result.run_id,),
        ).fetchall()
        for row in preds:
            assert str(row["train_window_end"]) < str(row["match_date"])


def test_run_backtest_replay_deterministic(gold_env) -> None:
    """十年窗一致性（spec 测试决策）：同输入重放输出逐字节一致（除 run 身份）。"""
    with gold_env(seed_rows()) as (face, duck_con, store):
        params = bt.BacktestParams(
            competitions=("E0",), min_train_matches=9, ev_threshold=0.0
        )
        first = bt.run_backtest(face, duck_con, store, params, label="replay-a")
        second = bt.run_backtest(face, duck_con, store, params, label="replay-b")

        def predictions(run_id: int) -> list[tuple[object, ...]]:
            return [
                tuple(row)
                for row in face.execute(
                    """
                    SELECT match_key, competition, season, match_date, home_team,
                           away_team, era, ftr, had_probs, fair_probs, fair_source,
                           model_fingerprint, train_window_end
                    FROM backtest_predictions WHERE run_id = ? ORDER BY match_key
                    """,
                    (run_id,),
                ).fetchall()
            ]

        def bets(run_id: int) -> list[tuple[object, ...]]:
            return [
                tuple(row)
                for row in face.execute(
                    """
                    SELECT kind, competition, placed_week, stake, legs, ev, kelly,
                           status, payout, profit, detail
                    FROM backtest_bets WHERE run_id = ? ORDER BY kind, placed_week, legs
                    """,
                    (run_id,),
                ).fetchall()
            ]

        assert predictions(first.run_id) == predictions(second.run_id)
        assert bets(first.run_id) == bets(second.run_id)
        assert bt.run_summary(face, first.run_id) == bt.run_summary(face, second.run_id)


def test_fair_probs_nan_odds_is_no_baseline() -> None:
    """correctness F1 回归：NaN 收盘价=无基准（否定式比较放行 NaN 会炸 Shin）。"""
    nan = float("nan")
    # era2 锚含 NaN → 回落共识，不抛
    probs, source = bt.fair_probs_from_gold(
        _gold_match(
            era=gold_mod.ERA_TRAJECTORY,
            anchor_odds=(nan, 3.40, 3.90),
            cons_probs=ERA2_CONS,
        )
    )
    assert source == "traj_cons"
    assert probs["h"] == pytest.approx(ERA2_CONS[0])
    # era1 psc 含 NaN → 该源无基准；avgc 兜底（候选链语义）
    probs, source = bt.fair_probs_from_gold(_gold_match(psc_odds=(nan, 3.3, 3.1)))
    assert source == "avgc"
    # 两源皆不可用（psc 含 NaN、avgc 缺）→ 无基准，不得进 Shin
    assert (
        bt.fair_probs_from_gold(
            _gold_match(psc_odds=(nan, 3.3, 3.1), avgc_odds=(None, None, None))
        )
        is None
    )


def test_run_backtest_invalid_window_raises(gold_env) -> None:
    """correctness F2 回归：坏 start/end fail-loud，不冒充视图缺失的空 run。"""
    with gold_env([]) as (face, duck_con, store):
        for bad in (
            bt.BacktestParams(competitions=("E0",), start="2016-8-1"),
            bt.BacktestParams(competitions=("E0",), end="2026-13-45"),
        ):
            with pytest.raises(ValueError, match="非法"):
                bt.run_backtest(face, duck_con, store, bad, label="bad-window")
        assert face.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0


def test_duplicate_gold_sid_dedups(gold_env) -> None:
    """correctness F3 回归：重复 sid 去重——三面身份一致，禁同场串关不破。"""
    rows = seed_rows()
    rows.append(dict(rows[-1]))  # 同 sid 同日重复行（上游腐败形态）
    with gold_env(rows) as (face, duck_con, store):
        params = bt.BacktestParams(
            competitions=("E0",), min_train_matches=9, ev_threshold=0.0
        )
        result = bt.run_backtest(face, duck_con, store, params, label="dup-sid")
        stored = face.execute(
            "SELECT COUNT(*) FROM backtest_predictions WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()[0]
        assert result.predictions == stored  # 计数与表行一致（不再 OR IGNORE 吞）
        for row in face.execute(
            "SELECT legs FROM backtest_bets WHERE run_id = ? AND kind = 'parlay2'",
            (result.run_id,),
        ).fetchall():
            legs = json.loads(str(row["legs"]))
            assert legs[0]["match_key"] != legs[1]["match_key"]  # 禁同场串关
        run = face.execute(
            "SELECT params FROM backtest_runs WHERE id = ?", (result.run_id,)
        ).fetchone()
        assert json.loads(str(run["params"]))["gold_duplicate_sids"] == 1


def test_mid_week_start_keeps_in_window_matches(gold_env) -> None:
    """correctness F4 回归：start 门逐场——跨 start 周内的窗口内场次不丢。"""
    # 7 月三轮热身（9 行泊松得分，让首 ISO 周即过 min_train 且 DC 可辨识；
    # sid 加前缀避免与主段 seed 的 8001+ 撞号——那会被取数侧去重吃掉）
    warmup = seed_rows(rounds=3, start=date(2016, 7, 5))
    for row in warmup:
        row["sid"] = f"j{row['sid']}"
    rows = warmup
    # 首 ISO 周（2016-W31）分两天：周二 08-02（窗口前）+ 周六 08-06（窗口内）
    rows.append(_era1_row("6101", "2016-08-02", "A", "B", 1, 1))
    rows.append(_era1_row("6102", "2016-08-02", "C", "D", 0, 2))
    rows.extend(seed_rows(rounds=4, start=date(2016, 8, 6)))  # 首轮 08-06 周六
    with gold_env(rows) as (face, duck_con, store):
        params = bt.BacktestParams(
            competitions=("E0",), start="2016-08-04", min_train_matches=9
        )
        result = bt.run_backtest(face, duck_con, store, params, label="mid-week")
        dates = sorted(
            {
                str(r["match_date"])
                for r in face.execute(
                    "SELECT match_date FROM backtest_predictions WHERE run_id = ?",
                    (result.run_id,),
                ).fetchall()
            }
        )
        # 首周内 ≥ start 的场次（08-06 周六）必须有预测；08-02 场次被逐场
        # 门剔除并计入 skip——整周跳过（旧行为）会把 08-06 一起丢掉
        assert "2016-08-06" in dates
        assert "2016-08-02" not in dates
        assert all(d >= "2016-08-04" for d in dates)
        assert result.skipped.get("pre_start_matches", 0) == 11  # 9 热身 + 2×08-02


def test_cli_backtest_without_bridge_exits_clean(monkeypatch, tmp_path) -> None:
    """correctness F5 回归：corpus.duckdb 不存在=干净退出带指引，非裸栈。"""
    import argparse

    from goalx_backend.cli import _cmd_backtest

    monkeypatch.setenv("GOALX_CORPUS_ROOT", str(tmp_path / "corpus"))
    monkeypatch.setenv("GOALX_DB_PATH", str(tmp_path / "goalx.db"))
    args = argparse.Namespace(
        label="no-bridge",
        haircut="0.10",
        competitions=["E0"],
        start=bt.DECADE_START,
        end=None,
        no_parlay=False,
        min_train=100,
        fair_source="auto",
    )
    with pytest.raises(SystemExit) as excinfo:
        _cmd_backtest(args)
    assert excinfo.value.code == 2
