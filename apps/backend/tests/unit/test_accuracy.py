"""前瞻准确率评分（auto-paper 票 02，prereg acc-v1）回归。

钉住：合成已知矩阵 + 已知赛果的 RPS/Brier/log-loss 手算值；hhad 整数线
限定与 skip_reason 行；hafu 缺半场 skip；双基准差值（票 01 快照同窗）；
幂等（同结果吸收 / 结果更正覆盖重评）；报告月块聚合与覆盖账同源对账。
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goalx_backend import odds_math as om
from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.db import connect, migrate
from goalx_backend.evaluation import accuracy, metrics
from goalx_backend.main import create_app
from goalx_backend.models import MatchCodeInput

_KICKOFF = "2026-11-02T19:45:00+00:00"
_AS_OF = "2026-11-02T19:40:00+00:00"

# 已知矩阵：P(1:0)=0.5, P(0:1)=0.3, P(0:0)=0.2，其余格 0
# → had=(h0.5, d0.2, a0.3)；ttg: 档0=0.2, 档1=0.8；crs: 1:0=0.5, 0:1=0.3, 0:0=0.2
_MATRIX = [[0.0] * 10 for _ in range(10)]
_MATRIX[1][0] = 0.5
_MATRIX[0][1] = 0.3
_MATRIX[0][0] = 0.2
_LAMBDAS = {"lambda_home": 1.0, "lambda_away": 0.8}

_RESULT_1_0 = {"home_goals": 1, "away_goals": 0}


def _seed_fixture(
    db: sqlite3.Connection, *, fid_tag: str = "a", result: dict[str, int | None]
) -> int:
    comp = fx_store.upsert_competition(db, "英超")
    home = fx_store.upsert_team(db, f"主队{fid_tag}")
    away = fx_store.upsert_team(db, f"客队{fid_tag}")
    fid = fx_store.upsert_fixture(db, comp, _KICKOFF, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fid,
            kind="jingcai",
            business_date="2026-11-02",
            code=f"周一00{fid_tag}",
        ),
    )
    db.execute(
        "INSERT INTO forecasts (fixture_id, track, model_version, issued_at,"
        " content_hash, payload) VALUES (?, 'ml', 'dc-test', ?, ?, ?)",
        (
            fid,
            "2026-11-02T10:00:00+00:00",
            f"hash-{fid_tag}",
            json.dumps({"matrix": _MATRIX, **_LAMBDAS}),
        ),
    )
    db.execute(
        "INSERT INTO draw_results (fixture_id, source, home_goals, away_goals,"
        " half_home_goals, half_away_goals, void, created_at)"
        " VALUES (?, 'srct', ?, ?, ?, ?, 0, '2026-11-02T21:00:00+00:00')",
        (
            fid,
            result["home_goals"],
            result["away_goals"],
            result.get("half_home_goals"),
            result.get("half_away_goals"),
        ),
    )
    db.commit()
    return fid


def _seed_baseline(
    db: sqlite3.Connection, fid: int, *, source: str, prices: dict[str, float]
) -> None:
    db.execute(
        "INSERT INTO market_baseline_snapshots"
        " (business_date, fixture_id, anchor_label, cutoff_at, source, market,"
        "  prices, observed_at, sale_state, created_at)"
        " VALUES ('2026-11-02', ?, '12:30', '2026-11-02T04:30:00+00:00', ?, 'had',"
        "  ?, NULL, 'on_sale', '2026-11-02T04:31:00+00:00')",
        (fid, source, json.dumps(prices)),
    )
    db.commit()


def _rows(db: sqlite3.Connection, fid: int) -> dict[str, sqlite3.Row]:
    return {
        str(r["market"]): r
        for r in db.execute(
            "SELECT * FROM forecast_scores WHERE fixture_id = ?", (fid,)
        ).fetchall()
    }


def test_hand_computed_metrics_pinned(db: sqlite3.Connection) -> None:
    """票 02 验收：合成已知矩阵+已知赛果（1:0），RPS/Brier/log-loss 手算钉死。"""
    fid = _seed_fixture(db, result=_RESULT_1_0)
    stats = accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    assert stats.scored == 5
    rows = _rows(db, fid)
    # had：p=(0.5,0.2,0.3)，实现 h → RPS=1/2[(0.5-1)²+(0.7-1)²]=0.17
    assert rows["had"]["model_metric"] == pytest.approx(0.17)
    assert rows["had"]["pred_prob_outcome"] == pytest.approx(0.5)
    assert rows["had"]["outcome"] == "h"
    # hhad 无让球线行 → skip_reason=no_line（prereg：非整数/无线进覆盖账）
    assert rows["hhad"]["skip_reason"] == "no_line"
    # ttg：总进球 1 → 档"1"；Brier=Σ(p−o)²=(0.2)²+(0.8−1)²=0.08；logloss=-ln(0.8)
    assert rows["ttg"]["outcome"] == "1"
    assert rows["ttg"]["model_metric"] == pytest.approx(0.08)
    assert rows["ttg"]["model_logloss"] == pytest.approx(-math.log(0.8))
    # crs：实现 "1:0"；Brier=(1-0.5)²+0.3²+0.2²=0.38；logloss=-ln(0.5)
    assert rows["crs"]["outcome"] == "1:0"
    assert rows["crs"]["model_metric"] == pytest.approx(0.38)
    assert rows["crs"]["model_logloss"] == pytest.approx(-math.log(0.5))
    # hafu：缺半场 → skip_reason 行（源T 物化无半场）
    assert rows["hafu"]["skip_reason"] == "no_half_score"


def test_log_loss_by_market_face(db: sqlite3.Connection) -> None:
    """
    票 32：log loss 第三指标读面（S2 面 C 数源）——手算钉死。

    同一已知矩阵+1:0：had LL=−ln(.5)（k=3）/ttg LL=−ln(.8)（k=8）/
    crs LL=−ln(.5)（k=31=28 精确格+三「其他」档）；skill=1−LL/ln(k)。
    hhad 无线=skip 行不进面；窗口按 kickoff UTC 日期切片（与面 A 同口径）。
    """
    _seed_fixture(db, fid_tag="ll", result=_RESULT_1_0)
    accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    faces = accuracy.log_loss_by_market(
        db, date_from="2026-11-01", date_to="2026-11-30"
    )
    had = faces["had"]
    assert had.n == 1
    assert had.n_classes == 3
    assert had.mean_log_loss == pytest.approx(-math.log(0.5))
    assert had.uniform_anchor == pytest.approx(math.log(3))
    assert had.skill_vs_uniform == pytest.approx(1 - (-math.log(0.5)) / math.log(3))
    ttg = faces["ttg"]
    assert ttg.n_classes == 8
    assert ttg.mean_log_loss == pytest.approx(-math.log(0.8), abs=1e-6)
    crs = faces["crs"]
    assert crs.n_classes == 31
    assert crs.mean_log_loss == pytest.approx(-math.log(0.5), abs=1e-6)
    assert "hhad" not in faces  # skip 行（无线）不进面
    assert "hafu" not in faces  # skip 行（无半场）不进面
    # 窗外（>90 日）不进面
    out = accuracy.log_loss_by_market(db, date_from="2027-01-01", date_to="2027-03-01")
    assert out == {}


def test_hhad_integer_line_scored(db: sqlite3.Connection) -> None:
    """hhad 整数线：让球线 -1 自 odds meta 读出，按调整后结果评 RPS。"""
    fid = _seed_fixture(db, fid_tag="b", result=_RESULT_1_0)
    db.execute(
        "INSERT INTO odds_snapshots (fixture_id, market_code, selection_code,"
        " source, odds, captured_at, observed_at, meta, created_at)"
        " VALUES (?, 'hhad', 'h', 'sporttery', 2.5, '2026-11-02T02:00:00+00:00',"
        " '2026-11-02T02:00:00+00:00', ?, 'x')",
        (fid, json.dumps({"goal_line": "-1"})),
    )
    db.commit()
    accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    rows = _rows(db, fid)
    hhad = rows["hhad"]
    assert hhad["skip_reason"] is None
    assert hhad["hhad_goal_line"] == -1.0
    # 线 -1：1:0→d(0.5)；0:1→a(0.3)；0:0→a(-1<0, 0.2) ⇒ (0, 0.5, 0.5)
    # 实现调整后结果 d → RPS=1/2[(0-0)²+(0.5-1)²]=0.125
    assert hhad["model_metric"] == pytest.approx(0.125)
    assert hhad["outcome"] == "d"
    # 非整数线 → skip_reason=non_integer_line
    fid2 = _seed_fixture(db, fid_tag="c", result=_RESULT_1_0)
    db.execute(
        "INSERT INTO odds_snapshots (fixture_id, market_code, selection_code,"
        " source, odds, captured_at, observed_at, meta, created_at)"
        " VALUES (?, 'hhad', 'h', 'sporttery', 2.5, '2026-11-02T02:00:00+00:00',"
        " '2026-11-02T02:00:00+00:00', ?, 'x')",
        (fid2, json.dumps({"goal_line": "-0.5"})),
    )
    db.commit()
    accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    assert _rows(db, fid2)["hhad"]["skip_reason"] == "non_integer_line"


def test_dual_baseline_diffs(db: sqlite3.Connection) -> None:
    """had 双锚差值：jc SP 归一化 + eu 概率，同窗（cutoff ≤ 开球）快照。"""
    fid = _seed_fixture(db, fid_tag="d", result=_RESULT_1_0)
    _seed_baseline(db, fid, source="jc_official", prices={"h": 2.0, "d": 3.2, "a": 3.8})
    _seed_baseline(
        db, fid, source="eu_consensus", prices={"h": 0.5, "d": 0.25, "a": 0.25}
    )
    accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    had = _rows(db, fid)["had"]
    jc_probs = om.normalized_implied((2.0, 3.2, 3.8))
    assert had["jc_baseline_metric"] == pytest.approx(metrics.rps(jc_probs, 0))
    assert had["diff_jc"] == pytest.approx(0.17 - metrics.rps(jc_probs, 0))
    assert had["eu_baseline_metric"] == pytest.approx(metrics.rps((0.5, 0.25, 0.25), 0))
    # 无快照场：差值 NULL 不删行（unknown 差值语义）
    fid2 = _seed_fixture(db, fid_tag="e", result=_RESULT_1_0)
    accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    empty = _rows(db, fid2)["had"]
    assert empty["diff_jc"] is None
    assert empty["eu_baseline_metric"] is None


def _seed_hhad_line(db: sqlite3.Connection, fid: int, line: str = "-1") -> None:
    db.execute(
        "INSERT INTO odds_snapshots (fixture_id, market_code, selection_code,"
        " source, odds, captured_at, observed_at, meta, created_at)"
        " VALUES (?, 'hhad', 'h', 'sporttery', 2.5, '2026-11-02T02:00:00+00:00',"
        " '2026-11-02T02:00:00+00:00', ?, 'x')",
        (fid, json.dumps({"goal_line": line})),
    )
    db.commit()


def test_idempotent_and_result_correction(db: sqlite3.Connection) -> None:
    """全齐场同结果重跑整场吸收；结果更正 → fingerprint 变化覆盖重评。"""
    fid = _seed_fixture(
        db,
        fid_tag="f",
        result={**_RESULT_1_0, "half_home_goals": 1, "half_away_goals": 0},
    )
    _seed_hhad_line(db, fid)
    first = accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    assert first.scored == 5
    again = accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    assert again.scored == 0
    assert again.absorbed == 1
    had_before = _rows(db, fid)["had"]["model_metric"]
    # 更正通道：0:1 改判（半场随之变）
    db.execute(
        "UPDATE draw_results SET home_goals = 0, away_goals = 1,"
        " half_home_goals = 0, half_away_goals = 1 WHERE fixture_id = ?",
        (fid,),
    )
    db.commit()
    fixed = accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    assert fixed.rescored == 1
    rows = _rows(db, fid)
    assert rows["had"]["outcome"] == "a"
    assert rows["had"]["model_metric"] != had_before
    assert rows["hafu"]["outcome"] == "aa"


def test_hhad_backfill_reopens_skip_row(db: sqlite3.Connection) -> None:
    """correctness：hhad 欠线行不算终态——线回填后重评清除 skip。"""
    fid = _seed_fixture(db, fid_tag="h", result=_RESULT_1_0)
    first = accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    assert first.skipped_hhad_line == 1
    assert _rows(db, fid)["hhad"]["skip_reason"] == "no_line"
    # 重跑不吸收（欠线重评口保持）
    again = accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    assert again.absorbed == 0
    # 让球线回填（PIT=赛前）→ 重评出指标行
    _seed_hhad_line(db, fid)
    after = accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    hhad = _rows(db, fid)["hhad"]
    assert hhad["skip_reason"] is None
    assert hhad["model_metric"] is not None
    assert after.skipped_hhad_line == 0


def test_report_aggregates_and_coverage_reconciles(db: sqlite3.Connection) -> None:
    """报告月块：覆盖首行与 forecast_coverage 同源对账（票 02 验收）。"""
    fid = _seed_fixture(db, fid_tag="g", result=_RESULT_1_0)
    comp = fx_store.upsert_competition(db, "西甲")
    other = fx_store.upsert_fixture(
        db,
        comp,
        _KICKOFF,
        fx_store.upsert_team(db, "他队主"),
        fx_store.upsert_team(db, "他队客"),
    )
    db.execute(
        "INSERT INTO forecast_coverage (business_date, fixture_id, covered, reason,"
        " recorded_at) VALUES ('2026-11-02', ?, 1, NULL, '2026-11-02T23:00:00+00:00'),"
        " ('2026-11-02', ?, 0, 'not_fd_league', '2026-11-02T23:00:00+00:00')",
        (fid, other),
    )
    db.commit()
    accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    report = accuracy.build_accuracy_report(db)
    assert report.reading_rule
    (coverage, rows) = report.months[0]
    assert coverage.month == "2026-11"
    assert coverage.covered == 1
    assert coverage.not_fd_league == 1
    had_overall = next(r for r in rows if r.market == "had" and r.competition is None)
    assert had_overall.n == 1
    assert had_overall.model_metric == pytest.approx(0.17)
    assert had_overall.diff_jc is None  # 无快照=unknown 差值
    assert had_overall.verdict_ready is False  # n<1800 只报不判
    hhad_overall = next(r for r in rows if r.market == "hhad" and r.competition is None)
    assert hhad_overall.skipped == 1
    # 校准箱自 payload 即时派生：最热向=h(0.5)，命中（1:0=h）→ 箱5 hit=1
    calib = {b.bin_index: b for b in had_overall.calibration}
    assert calib[5].n == 1
    assert calib[5].mean_conf == pytest.approx(0.5)
    assert calib[5].hit_rate == 1.0
    # 联赛拆解行同报
    league_row = next(r for r in rows if r.market == "had" and r.competition == "英超")
    assert league_row.n == 1


def test_accuracy_report_api(tmp_path: Path) -> None:
    """/api/v1/accuracy/report：月块+判读规则文字+覆盖首行（契约面）。"""
    db_path = tmp_path / "accuracy.db"
    conn = connect(db_path)
    migrate(conn)
    _seed_fixture(conn, result=_RESULT_1_0)
    accuracy.score_settled_fixtures(conn, now_iso=_AS_OF)
    conn.close()
    with TestClient(create_app(settings=Settings(db_path=db_path))) as test_client:
        resp = test_client.get("/api/v1/accuracy/report")
        assert resp.status_code == 200
        body = resp.json()
        assert body["reading_rule"]
        assert len(body["months"]) == 1
        block = body["months"][0]
        assert block["coverage"]["month"] == "2026-11"
        had = next(
            r
            for r in block["rows"]
            if r["market"] == "had" and r["competition"] is None
        )
        assert had["n"] == 1
        assert had["model_metric"] == pytest.approx(0.17)
        assert had["calibration"][0]["bin_index"] == 5


def test_accuracy_report_api_empty_state(tmp_path: Path) -> None:
    """空库=空月块列表（诚实空态，不伪造行）。"""
    db_path = tmp_path / "empty.db"
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    with TestClient(create_app(settings=Settings(db_path=db_path))) as test_client:
        resp = test_client.get("/api/v1/accuracy/report")
        assert resp.status_code == 200
        body = resp.json()
        assert body["months"] == []
        assert "n≥1800" in body["reading_rule"]


def test_duplicate_match_card_dedup(db: sqlite3.Connection) -> None:
    """correctness F1：一场两卡（改期重出）只评一次，月归属=业务日最大。"""
    fid = _seed_fixture(db, fid_tag="i", result=_RESULT_1_0)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fid, kind="jingcai", business_date="2026-10-31", code="周六001"
        ),
    )
    db.commit()
    stats = accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    assert stats.scored == 5  # 不是 10
    rows = db.execute(
        "SELECT COUNT(*) AS n, MAX(month_key) AS m FROM forecast_scores"
        " WHERE fixture_id = ?",
        (fid,),
    ).fetchone()
    assert rows["n"] == 5
    assert rows["m"] == "2026-11"  # 取业务日最大的一卡


def test_voided_fixture_excluded_from_report(db: sqlite3.Connection) -> None:
    """ADR-0001：void 后评分行不再进报告与累计门槛。"""
    fid = _seed_fixture(db, fid_tag="j", result=_RESULT_1_0)
    accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    report = accuracy.build_accuracy_report(db)
    assert report.months[0][1]  # 有行
    db.execute("UPDATE draw_results SET void = 1 WHERE fixture_id = ?", (fid,))
    db.commit()
    after = accuracy.build_accuracy_report(db)
    assert all(rows == () for _, rows in after.months)  # 剔除后无玩法行


def test_coverage_only_month_emits_block(db: sqlite3.Connection) -> None:
    """月块=评分月∪覆盖月：覆盖先行、评分未到的月也出块（首行对账不断档）。"""
    comp = fx_store.upsert_competition(db, "法甲")
    other = fx_store.upsert_fixture(
        db,
        comp,
        "2026-10-05T18:00:00+00:00",
        fx_store.upsert_team(db, "十月主队"),
        fx_store.upsert_team(db, "十月客队"),
    )
    db.execute(
        "INSERT INTO forecast_coverage (business_date, fixture_id, covered, reason,"
        " recorded_at) VALUES ('2026-10-05', ?, 0, 'no_model',"
        " '2026-10-05T23:00:00+00:00')",
        (other,),
    )
    db.commit()
    report = accuracy.build_accuracy_report(db)
    months = [block[0].month for block in report.months]
    assert "2026-10" in months
    block = next(b for b in report.months if b[0].month == "2026-10")
    assert block[0].no_model == 1
    assert block[1] == ()


def test_naive_beijing_kickoff_scores(db: sqlite3.Connection) -> None:
    """legacy naive 北京墙钟 kickoff 不因字典序错位丢赛前 Forecast。"""
    fid = _seed_fixture(db, fid_tag="k", result=_RESULT_1_0)
    # 覆写为 naive 墙钟：北京 19:45 = 11:45Z；已种 Forecast issued=10:00Z 赛前
    db.execute(
        "UPDATE fixtures SET kickoff_utc = '2026-11-02T19:45:00' WHERE id = ?",
        (fid,),
    )
    db.commit()
    stats = accuracy.score_settled_fixtures(db, now_iso=_AS_OF)
    assert stats.skipped_no_forecast == 0
    assert stats.scored == 5
