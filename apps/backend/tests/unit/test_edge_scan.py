"""autoresearch 票 01/04：探索轨扫描器阵列（S1/S4/S5 + 台账 + 周报）。

口径直证：S1 分桶去水数学/越门判定/幂等；S4 观测日计数（含无信号日记零）；
S5 滚存回看（缺席自补+近窗保鲜）；十年回放面；周报 digest；CLI/晨检接线。
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import duckdb
import pytest

from goalx_backend.cli import _cmd_edge_scan
from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.data import results as rs_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_asof, srct_odds
from goalx_backend.db import connect, connect_research, migrate
from goalx_backend.evaluation import accuracy, edge_scan
from goalx_backend.models import DrawResultInput, MatchCodeInput, SnapshotInput
from goalx_backend.morning_check import check_edge_scan

_KICKOFF = "2026-11-09T18:00:00+00:00"
_BUSINESS_DATE = "2026-11-09"


def _seed_fixture(
    conn: sqlite3.Connection,
    *,
    kickoff: str,
    business_date: str,
    home_goals: int,
    away_goals: int,
    jc_odds: tuple[float, float, float] | None = None,
    jc_captured_at: str | None = None,
    sharp_odds: tuple[tuple[str, tuple[float, float, float]], ...] = (),
) -> int:
    """一场已结算竞彩场：场档+业务日码+赛果+可选 JC/sharp 报价时间线。"""
    tag = kickoff[11:16].replace(":", "")
    comp = fx_store.upsert_competition(conn, "测试联赛")
    home = fx_store.upsert_team(conn, f"主队{tag}")
    away = fx_store.upsert_team(conn, f"客队{tag}")
    fixture = fx_store.upsert_fixture(conn, comp, kickoff, home, away)
    fx_store.upsert_match_code(
        conn,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date=business_date,
            code=f"周一{fixture}",
        ),
    )
    rs_store.upsert_draw_result(
        conn,
        DrawResultInput(
            fixture_id=fixture, home_goals=home_goals, away_goals=away_goals
        ),
    )
    if jc_odds is not None and jc_captured_at is not None:
        for selection, odds in zip(("h", "d", "a"), jc_odds, strict=True):
            fx_store.insert_odds_snapshot(
                conn,
                SnapshotInput(
                    fixture_id=fixture,
                    market_code="had",
                    selection_code=selection,
                    source="sporttery",
                    odds=odds,
                    captured_at=jc_captured_at,
                    # PIT 纪律：无 observed_at 的行不进 as-of 读取
                    observed_at=jc_captured_at,
                ),
            )
    for stamp, odds in sharp_odds:
        for selection, price in zip(("h", "d", "a"), odds, strict=True):
            fx_store.insert_odds_snapshot(
                conn,
                SnapshotInput(
                    fixture_id=fixture,
                    market_code="had",
                    selection_code=selection,
                    source="odds_api:pinnacle",
                    odds=price,
                    captured_at=stamp,
                    observed_at=stamp,
                ),
            )
    conn.commit()
    return fixture


# ---- S1：分桶口径 + 越门 + 幂等 ----


def test_s1_bucket_devig_and_segment_math(db: sqlite3.Connection) -> None:
    """(1.25,6,12) 去水 → 三向落 p65+/p10-20/p00-10 三段，数值精确。"""
    _seed_fixture(
        db,
        kickoff=_KICKOFF,
        business_date=_BUSINESS_DATE,
        home_goals=1,
        away_goals=2,  # 客胜
        jc_odds=(1.25, 6.0, 12.0),
        jc_captured_at="2026-11-09T15:00:00+00:00",
    )
    stats = edge_scan.run_s1(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    payload = json.loads(
        db.execute(
            "SELECT payload FROM edge_observations WHERE scanner='s1' AND window_key=?",
            (_BUSINESS_DATE,),
        ).fetchone()[0]
    )
    # 去水：1/1.25=.8, 1/6≈.16667, 1/12≈.08333 → sum=1.05 → a≈.079365
    seg_a = payload["segments"]["p00-10:a"]
    assert seg_a["n"] == 1
    assert seg_a["mean_prob"] == pytest.approx(0.079365, abs=1e-5)
    assert seg_a["actual_freq"] == 1.0  # 客胜命中
    assert seg_a["dev"] == pytest.approx(1.0 - 0.079365, abs=1e-5)
    assert payload["segments"]["p65+:h"]["actual_freq"] == 0.0
    assert stats["coverage"] == {
        "n_settled": 1,
        "n_with_jc": 1,
        "n_incomplete_triple": 0,
    }
    # n=1 < S1_MIN_N：不越门不落发现
    assert stats["findings_landed"] == 0
    assert db.execute("SELECT COUNT(*) FROM edge_findings").fetchone()[0] == 0


def test_s1_gate_crossing_lands_finding_idempotent(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 场客胜 2（a 段 dev≈.587>2se≈.312、h 段 dev≈−.762 也越门）→ 落发现。"""
    monkeypatch.setattr(edge_scan, "S1_MIN_N", 3)
    for kickoff, goals in (
        ("2026-11-09T10:00:00+00:00", (1, 2)),  # 客胜
        ("2026-11-09T11:00:00+00:00", (0, 3)),  # 客胜
        ("2026-11-09T12:00:00+00:00", (2, 2)),  # 平
    ):
        _seed_fixture(
            db,
            kickoff=kickoff,
            business_date=_BUSINESS_DATE,
            home_goals=goals[0],
            away_goals=goals[1],
            jc_odds=(1.25, 6.0, 12.0),
            jc_captured_at="2026-11-09T09:00:00+00:00",
        )
    ctx = edge_scan.ScanContext(business_date=_BUSINESS_DATE)
    first = edge_scan.run_s1(db, ctx)
    assert first["findings_landed"] == 2  # p00-10:a（客胜高估偏差）+ p65+:h
    row = db.execute(
        "SELECT metric_value, n, se, window, status FROM edge_findings"
        " WHERE segment_key='p00-10:a'"
    ).fetchone()
    assert row["metric_value"] == pytest.approx(0.5873016, abs=1e-5)
    assert row["se"] == pytest.approx(0.1560622, abs=1e-5)
    assert row["window"].endswith(_BUSINESS_DATE)
    assert row["status"] == "new"
    # 同窗重跑：发现 INSERT OR IGNORE + 观测 REPLACE → 各仍原行数
    second = edge_scan.run_s1(db, ctx)
    assert second["findings_landed"] == 0
    assert db.execute("SELECT COUNT(*) FROM edge_findings").fetchone()[0] == 2
    assert (
        db.execute(
            "SELECT COUNT(*) FROM edge_observations WHERE scanner='s1'"
        ).fetchone()[0]
        == 1
    )


def test_s1_window_scopes_by_business_date(db: sqlite3.Connection) -> None:
    """窗内只含业务日落在 [from..end] 的场（窗口切片正确性）。"""
    _seed_fixture(
        db,
        kickoff=_KICKOFF,
        business_date=_BUSINESS_DATE,
        home_goals=1,
        away_goals=2,
        jc_odds=(1.25, 6.0, 12.0),
        jc_captured_at="2026-11-09T15:00:00+00:00",
    )
    _seed_fixture(  # 窗外（>90 日前）
        db,
        kickoff="2026-08-01T18:00:00+00:00",
        business_date="2026-08-01",
        home_goals=0,
        away_goals=1,
        jc_odds=(1.25, 6.0, 12.0),
        jc_captured_at="2026-08-01T15:00:00+00:00",
    )
    stats = edge_scan.run_s1(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    assert stats["coverage"]["n_settled"] == 1


# ---- 票 32：Romano-Wolf stepdown（S1 多重校正，21 段族；只收紧不放宽） ----


def _rw_world(
    matches: list[tuple[tuple[float, float, float], int]],
) -> tuple[list[edge_scan.ObsRow], list[tuple[float, float, float]]]:
    """[(去水三向, 实现向下标), ...] → 全向 ObsRow 行 + match 级概率三元组。"""
    rows: list[edge_scan.ObsRow] = []
    triples: list[tuple[float, float, float]] = []
    for probs, outcome in matches:
        triples.append(probs)
        for idx, direction in enumerate(("h", "d", "a")):
            rows.append(
                edge_scan.ObsRow(
                    direction=direction, prob=probs[idx], hit=(outcome == idx)
                )
            )
    return rows, triples


def _lone_borderline_world(
    n: int = 120,
) -> tuple[list[edge_scan.ObsRow], list[tuple[float, float, float]], int]:
    """同质市场 (0.15, 0.15, 0.70)：d 段 z≈2.05 刚过未校正门，h/a 段安静。"""
    n_d = 26  # af=0.2167, dev=0.0667, se=sqrt(.15·.85/120)=0.03260 → z≈2.045
    n_h = 14  # af=0.1167 → z≈−1.02（未过门）
    n_a = n - n_d - n_h  # af≈0.6667 → z≈−0.80（未过门）
    matches = (
        [
            ((0.15, 0.15, 0.70), 1),
        ]
        * n_d
        + [((0.15, 0.15, 0.70), 0)] * n_h
        + [((0.15, 0.15, 0.70), 2)] * n_a
    )
    rows, triples = _rw_world(matches)
    return rows, triples, n_d


class TestRomanoWolfStepdown:
    def test_lone_borderline_significant_blocked_by_rw(self):
        """审计反例形态：单段 z≈2.05 过未校正门，但 21 段族 FWER 下不再越门。"""
        rows, triples, n_d = _lone_borderline_world()
        segments = edge_scan.calibration_segments(rows)
        d_seg = segments["p10-20:d"]
        assert d_seg["n"] == 120  # 段 n=带内方向行数（全 120 场的 d 行）
        assert d_seg["actual_freq"] == pytest.approx(n_d / 120, abs=1e-6)
        assert d_seg["crossed"] is True  # 未校正门（n≥100 且 |z|>2）恰过
        rw = edge_scan.romano_wolf_stepdown(triples, segments)
        assert rw["p10-20:d"]["p_raw"] == pytest.approx(0.0407, abs=2e-3)
        assert rw["p10-20:d"]["p_rw"] > 0.05  # 族 max 分布下不再显著
        assert rw["p10-20:d"]["crossed_rw"] is False  # 只收紧：uncrossed→False

    def test_strong_deviation_still_passes_rw(self):
        """强偏离不被 RW 封死：z≈10.7 → p_rw=1/(B+1) 量级，仍越门落发现。"""
        matches = (
            [((0.15, 0.15, 0.70), 1)] * 60
            + [((0.15, 0.15, 0.70), 0)] * 12
            + [((0.15, 0.15, 0.70), 2)] * 48
        )
        rows, triples = _rw_world(matches)
        segments = edge_scan.calibration_segments(rows)
        rw = edge_scan.romano_wolf_stepdown(triples, segments)
        assert segments["p10-20:d"]["crossed"] is True
        assert rw["p10-20:d"]["p_rw"] <= edge_scan.S1_RW_ALPHA
        assert rw["p10-20:d"]["crossed_rw"] is True

    def test_rw_never_loosens_and_p_rw_dominates_p_raw(self):
        """结构不变量：crossed_rw ⊆ crossed；p_rw ≥ p_raw（max 统计量 ≥ 单统计量）。"""
        rows, triples, _ = _lone_borderline_world()
        segments = edge_scan.calibration_segments(rows)
        rw = edge_scan.romano_wolf_stepdown(triples, segments)
        for key, seg in segments.items():
            entry = rw[key]
            assert entry["p_raw"] >= 0.0
            assert entry["p_rw"] >= entry["p_raw"] - 1e-12
            if not seg["crossed"]:
                assert entry["crossed_rw"] is False  # 未过未校正门者 RW 不可能放开

    def test_family_is_full_21_segments_absent_as_p_one(self):
        """族=注册全 21 段（E-32 修订 C 同精神）：缺组段以 p=1 入族不缺席。"""
        rows, triples, _ = _lone_borderline_world()
        segments = edge_scan.calibration_segments(rows)
        rw = edge_scan.romano_wolf_stepdown(triples, segments)
        assert len(rw) == 21  # 7 价格带 × 3 方向
        absent = rw["p40-50:h"]
        assert absent["p_raw"] == 1.0
        assert absent["p_rw"] == 1.0
        assert absent["crossed_rw"] is False

    def test_empty_window_fail_closed(self):
        """空窗不炸不误判：全段 p=1 不越门。"""
        rw = edge_scan.romano_wolf_stepdown([], {})
        assert len(rw) == 21
        assert all(e["p_rw"] == 1.0 and not e["crossed_rw"] for e in rw.values())

    def test_deterministic_given_same_inputs(self):
        """固定种子：同输入重跑逐位一致（台账幂等/审计可复现的前提）。"""
        rows, triples, _ = _lone_borderline_world()
        segments = edge_scan.calibration_segments(rows)
        first = edge_scan.romano_wolf_stepdown(triples, segments)
        second = edge_scan.romano_wolf_stepdown(triples, segments)
        assert first == second

    def test_calibrated_world_no_rw_crossing(self):
        """市场恰校准世界（种子固定）：RW 后无越门段（FWER 语义的合成对照）。"""
        import random

        rng = random.Random(20261110)
        matches = []
        triple = (0.15, 0.15, 0.70)
        for _ in range(120):
            u = rng.random()
            outcome = 0 if u < 0.15 else (1 if u < 0.30 else 2)
            matches.append((triple, outcome))
        rows, triples = _rw_world(matches)
        segments = edge_scan.calibration_segments(rows)
        rw = edge_scan.romano_wolf_stepdown(triples, segments)
        assert not any(e["crossed_rw"] for e in rw.values())


def test_s1_rw_fields_in_payload_and_strong_landing(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """票 32 接线：RW 读数进观测 payload 与发现 notes；强偏离仍落发现。"""
    monkeypatch.setattr(edge_scan, "S1_MIN_N", 3)
    for kickoff, goals in (
        ("2026-11-09T10:00:00+00:00", (1, 2)),
        ("2026-11-09T11:00:00+00:00", (0, 3)),
        ("2026-11-09T12:00:00+00:00", (2, 2)),
    ):
        _seed_fixture(
            db,
            kickoff=kickoff,
            business_date=_BUSINESS_DATE,
            home_goals=goals[0],
            away_goals=goals[1],
            jc_odds=(1.25, 6.0, 12.0),
            jc_captured_at="2026-11-09T09:00:00+00:00",
        )
    stats = edge_scan.run_s1(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    payload = json.loads(
        db.execute(
            "SELECT payload FROM edge_observations WHERE scanner='s1' AND window_key=?",
            (_BUSINESS_DATE,),
        ).fetchone()[0]
    )
    assert payload["rw"] == {
        "method": "romano-wolf-stepdown/1",
        "alpha": edge_scan.S1_RW_ALPHA,
        "replicates": edge_scan.S1_RW_REPLICATES,
        "family_size": 21,
    }
    seg_a = payload["segments"]["p00-10:a"]
    assert seg_a["p_raw"] == pytest.approx(0.00017, abs=5e-5)  # z≈3.76 双侧
    assert seg_a["p_rw"] <= edge_scan.S1_RW_ALPHA
    assert seg_a["crossed_rw"] is True
    # 返回面双口径并列（新旧对照读数不互吞）
    assert "p00-10:a" in stats["crossed"]
    assert "p00-10:a" in stats["crossed_rw"]
    # 发现行 notes 带 RW 读数（审计可复原）
    notes = json.loads(
        db.execute(
            "SELECT notes FROM edge_findings WHERE segment_key='p00-10:a'"
        ).fetchone()[0]
    )
    assert notes["crossed_rw"] is True
    assert notes["p_rw"] == seg_a["p_rw"]


def test_s1_rw_blocks_landing_when_stepdown_rejects(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """票 32 门叠加：RW 拒绝（p_rw>α）→ 未校正越门段不落发现（只收紧）。"""
    monkeypatch.setattr(edge_scan, "S1_MIN_N", 3)

    def reject_all(match_probs, segments, **_kwargs):
        return {
            key: {"p_raw": 1.0, "p_rw": 0.5, "crossed_rw": False}
            for key in edge_scan._S1_FAMILY_KEYS
        }

    monkeypatch.setattr(edge_scan, "romano_wolf_stepdown", reject_all)
    for kickoff, goals in (
        ("2026-11-09T10:00:00+00:00", (1, 2)),
        ("2026-11-09T11:00:00+00:00", (0, 3)),
        ("2026-11-09T12:00:00+00:00", (2, 2)),
    ):
        _seed_fixture(
            db,
            kickoff=kickoff,
            business_date=_BUSINESS_DATE,
            home_goals=goals[0],
            away_goals=goals[1],
            jc_odds=(1.25, 6.0, 12.0),
            jc_captured_at="2026-11-09T09:00:00+00:00",
        )
    stats = edge_scan.run_s1(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    assert stats["crossed"]  # 未校正口径仍越门（对照读数保留）
    assert stats["crossed_rw"] == []  # RW 拒绝 → 不落发现
    assert stats["findings_landed"] == 0
    assert db.execute("SELECT COUNT(*) FROM edge_findings").fetchone()[0] == 0
    payload = json.loads(
        db.execute(
            "SELECT payload FROM edge_observations WHERE scanner='s1' AND window_key=?",
            (_BUSINESS_DATE,),
        ).fetchone()[0]
    )
    assert payload["segments"]["p00-10:a"]["crossed"] is True  # 旧口径如实留痕
    assert payload["segments"]["p00-10:a"]["crossed_rw"] is False


# ---- S1 十年回放面（口径验证） ----


def test_s1_decade_reads_research_face(db: sqlite3.Connection, tmp_path: Path) -> None:
    """研究库 fair_probs+ftr → 同分桶判定；只落观测不落发现。"""
    research = connect_research(tmp_path / "research.db")
    try:
        research.execute(
            "INSERT INTO backtest_runs (id, label, params, status, created_at)"
            " VALUES (1, 'decade-test', '{}', 'done', '2026-11-08T00:00:00+00:00')"
        )
        for i, ftr in enumerate(("H", "H", "A")):
            research.execute(
                "INSERT INTO backtest_predictions"
                " (run_id, match_key, competition, season, match_date, home_team,"
                "  away_team, ftr, had_probs, fair_probs, fair_source,"
                "  model_fingerprint, train_window_end)"
                " VALUES (1, ?, 'x', '2025-26', '2026-05-01', 'H', 'A', ?, '{}', ?,"
                "  'psc', 'fp', '2026-04-30')",
                (
                    f"m{i}",
                    ftr,
                    json.dumps({"h": 0.8, "d": 0.15, "a": 0.05}),
                ),
            )
        research.commit()
        result = edge_scan.run_s1_decade(db, research_conn=research)
        assert result["run_label"] == "decade-test"
        seg_a = result["segments"]["p00-10:a"]
        assert seg_a["n"] == 3
        assert seg_a["actual_freq"] == pytest.approx(1 / 3)
        assert seg_a["crossed"] is False  # n=3<100 不越门
        assert (
            db.execute(
                "SELECT COUNT(*) FROM edge_observations WHERE scanner='s1'"
                " AND window_key='decade:1'"
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM edge_findings").fetchone()[0] == 0
    finally:
        research.close()


# ---- S4：陈盘观测日 ----


def test_s4_observation_day_counts(db: sqlite3.Connection) -> None:
    """
    一场完整信号链：JC 定格 −3h（决策时点陈旧 120min）+ sharp 'a' 概率
    大涨（drift≥1.5pp）→ confirmed；决策后 JC a 赔缩 → fired+命中。
    另一场无 JC 价 → had_quote 闸外；第三场有 JC 无 sharp → 陈盘但无分辨。
    """
    # 场 1：完整链
    fixture1 = _seed_fixture(
        db,
        kickoff=_KICKOFF,
        business_date=_BUSINESS_DATE,
        home_goals=0,
        away_goals=1,
        jc_odds=(2.0, 3.4, 4.2),  # 决策组：定格于 −3h
        jc_captured_at="2026-11-09T15:00:00+00:00",
        sharp_odds=(
            ("2026-11-09T14:59:00+00:00", (2.2, 3.5, 4.0)),  # 冻结端参考
            ("2026-11-09T16:58:00+00:00", (2.2, 3.5, 2.8)),  # 决策端：a 大涨
        ),
    )
    # 决策后（−30min）JC 再调盘：a 赔 4.2→3.4（a 隐含上行 → 与 drift 同号）
    for selection, odds in (("h", 2.0), ("d", 3.4), ("a", 3.4)):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture1,
                market_code="had",
                selection_code=selection,
                source="sporttery",
                odds=odds,
                captured_at="2026-11-09T17:30:00+00:00",
                observed_at="2026-11-09T17:30:00+00:00",
            ),
        )
    db.commit()
    # 场 2：无 JC 价（had_quote 闸外）——同业务日
    _seed_fixture(
        db,
        kickoff="2026-11-09T19:00:00+00:00",
        business_date=_BUSINESS_DATE,
        home_goals=1,
        away_goals=1,
    )
    # 场 3：JC 定格 −3h 但无 sharp 锚 → 陈盘可数、drift 不可算不进 confirmed
    _seed_fixture(
        db,
        kickoff="2026-11-09T20:00:00+00:00",
        business_date=_BUSINESS_DATE,
        home_goals=2,
        away_goals=0,
        jc_odds=(2.5, 3.2, 3.0),
        jc_captured_at="2026-11-09T17:00:00+00:00",
    )
    stats = edge_scan.run_s4(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    assert stats["n_fixtures"] == 3
    assert stats["n_observed"] == 2  # 场 2 无 JC 价不可观测
    assert stats["n_stale"] == 2  # 两场定格均 ≥15min
    assert stats["n_confirmed"] == 1  # 场 3 无 drift
    assert stats["n_fired"] == 1
    assert stats["n_hit"] == 1
    payload = json.loads(
        db.execute(
            "SELECT payload FROM edge_observations WHERE scanner='s4' AND window_key=?",
            (_BUSINESS_DATE,),
        ).fetchone()[0]
    )
    assert payload["hit_rate"] == 1.0


def test_s4_zero_signal_day_records_honest_zero(db: sqlite3.Connection) -> None:
    """无信号日如实记零（积累器不缺日）。"""
    stats = edge_scan.run_s4(db, edge_scan.ScanContext(business_date="2026-11-01"))
    assert stats == {
        "scanner": "s4",
        "n_fixtures": 0,
        "n_observed": 0,
        "n_stale": 0,
        "n_confirmed": 0,
        "n_fired": 0,
        "n_hit": 0,
    }
    assert (
        db.execute(
            "SELECT COUNT(*) FROM edge_observations WHERE scanner='s4'"
            " AND window_key='2026-11-01'"
        ).fetchone()[0]
        == 1
    )


# ---- S4 × corpus as-of（票 19：§4.1#7 接线债——0 有分辨根因=sharp 端点
# ---- 读 silver 最新视图，历史时点"消失"；接线=青铜重放注入缝） ----

_SID = "95019"
# cid177 轨迹行（published 北京墙钟）：定格端旧价 → 决策端新价（a 赔缩）
_PIN_ROW_OLD = "2.50|3.40|2.80|11-09 21:50|0.9|0.9|0.9|2026"  # 13:50 UTC
_PIN_ROW_NEW = "2.90|3.40|2.40|11-09 23:50|0.9|0.9|0.9|2026"  # 15:50 UTC


def _pin177_envelope(fetched_at: str, rows: list[str]) -> dict:
    """一个含 cid177 轨迹的 odds_1x2d bronze 信封（票 19 测试件）。"""
    return {
        "provider": srct.SRCT_PROVIDER,
        "dataset": srct.ODDS_DATASET,
        "sid": _SID,
        "fetched_at": fetched_at,
        "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
        "raw_sha": "0" * 64,
        "payload": {
            "meta": {},
            "game": ["177|800001|TestPinnacle|2.50|3.40|2.80|"],
            "game_detail": [f"800001^{';'.join(rows)};"],
        },
    }


def _build_s4_corpus(
    tmp_path: Path,
) -> tuple[CorpusStore, str]:
    """
    攒 S4 票 19 语料：三信封（定格前/决策前/开球后夜拍）→ silver → duck。

    第三信封（开球后）再物化把 silver 观测时刻整体后移——夜扫查昨日
    决策时点在 silver 上空（§4.1#7 根因形态），青铜重放可复原。
    """
    store = CorpusStore(tmp_path / "corpus")
    store.append_bronze(
        srct.SRCT_PROVIDER,
        srct.ODDS_DATASET,
        [
            _pin177_envelope("2026-11-09T14:00:00+00:00", [_PIN_ROW_OLD]),
            _pin177_envelope("2026-11-09T16:30:00+00:00", [_PIN_ROW_OLD, _PIN_ROW_NEW]),
            # 夜拍（开球后 8h）：同载荷再抓再物化——silver 最新视图后移
            _pin177_envelope("2026-11-10T02:00:00+00:00", [_PIN_ROW_OLD, _PIN_ROW_NEW]),
        ],
    )
    srct_odds.build_odds_change_events(
        store,
        # kickoff 北京墙钟 11-10 02:00 = UTC 11-09 18:00（= _KICKOFF）
        meta={
            _SID: {
                "sid": _SID,
                "kickoff": datetime(2026, 11, 10, 2, 0),
                "league": "英超",
            }
        },
    )
    return store, str(corpus_duckdb.build_corpus_duckdb(store))


def _seed_s4_corpus_fixture(db: sqlite3.Connection) -> None:
    """一场 JC 定格 15:00、决策 17:00、决策后 17:30 再调盘的已结算场。"""
    fixture = _seed_fixture(
        db,
        kickoff=_KICKOFF,
        business_date=_BUSINESS_DATE,
        home_goals=0,
        away_goals=1,
        jc_odds=(2.2, 3.1, 3.5),
        jc_captured_at="2026-11-09T15:00:00+00:00",
    )
    for selection, odds in (("h", 2.2), ("d", 3.1), ("a", 3.0)):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=selection,
                source="sporttery",
                odds=odds,
                captured_at="2026-11-09T17:30:00+00:00",
                observed_at="2026-11-09T17:30:00+00:00",
            ),
        )
    db.commit()


def test_s4_corpus_asof_resolver_reads_history(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """
    票 19 验收主路径：夜扫（silver 已被开球后信封后移）下，resolver 在场
    时定格/决策两端走青铜重放 → 有分辨非零；payload 口径注记=corpus as-of。
    """
    store, duck_path = _build_s4_corpus(tmp_path)
    duck_con = duckdb.connect(duck_path, read_only=True)
    try:
        _seed_s4_corpus_fixture(db)
        stats = edge_scan.run_s4(
            db,
            edge_scan.ScanContext(
                business_date=_BUSINESS_DATE,
                duck_con=duck_con,
                resolve_sid=lambda fid: _SID,
                asof_resolver=partial(srct_asof.asof_rows, store, duck_con),
            ),
        )
        assert stats["n_observed"] == 1
        assert stats["n_stale"] == 1
        assert stats["n_confirmed"] == 1  # 修复前=0（silver 历史时点消失）
        assert stats["n_fired"] == 1
        assert stats["n_hit"] == 1
        payload = json.loads(
            db.execute(
                "SELECT payload FROM edge_observations WHERE scanner='s4'"
                " AND window_key=?",
                (_BUSINESS_DATE,),
            ).fetchone()[0]
        )
        assert payload["sharp_source"] == "corpus_asof"
    finally:
        duck_con.close()
        store.close()


def test_s4_without_resolver_pins_the_coverage_gap(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """
    根因钉死（回退面）：同语料同场，无 resolver 时 silver 最新视图回答
    不了历史时点 → 有分辨如实记零（非断线，§4.1#7 原形态）；口径注记=corpus。
    """
    store, duck_path = _build_s4_corpus(tmp_path)
    duck_con = duckdb.connect(duck_path, read_only=True)
    try:
        _seed_s4_corpus_fixture(db)
        stats = edge_scan.run_s4(
            db,
            edge_scan.ScanContext(
                business_date=_BUSINESS_DATE,
                duck_con=duck_con,
                resolve_sid=lambda fid: _SID,
            ),
        )
        assert stats["n_stale"] == 1
        assert stats["n_confirmed"] == 0  # 0 有分辨（接线前形态）
        payload = json.loads(
            db.execute(
                "SELECT payload FROM edge_observations WHERE scanner='s4'"
                " AND window_key=?",
                (_BUSINESS_DATE,),
            ).fetchone()[0]
        )
        assert payload["sharp_source"] == "corpus"
    finally:
        duck_con.close()
        store.close()


def test_s4_sharp_source_annotation_legacy(db: sqlite3.Connection) -> None:
    """无 corpus 桥（duck_con 缺席）：口径注记=legacy（不冒充 corpus 读法）。"""
    _seed_fixture(
        db,
        kickoff=_KICKOFF,
        business_date=_BUSINESS_DATE,
        home_goals=1,
        away_goals=1,
        jc_odds=(2.2, 3.1, 3.5),
        jc_captured_at="2026-11-09T15:00:00+00:00",
    )
    edge_scan.run_s4(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    payload = json.loads(
        db.execute(
            "SELECT payload FROM edge_observations WHERE scanner='s4' AND window_key=?",
            (_BUSINESS_DATE,),
        ).fetchone()[0]
    )
    assert payload["sharp_source"] == "legacy"


def test_s4_zero_signal_day_with_resolver_records_honest_zero(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """无信号日记零纪律在 as-of 接线后不变（红线：不因数据源升级冒充信号）。"""
    store, duck_path = _build_s4_corpus(tmp_path)
    duck_con = duckdb.connect(duck_path, read_only=True)
    try:
        stats = edge_scan.run_s4(
            db,
            edge_scan.ScanContext(
                business_date="2026-11-01",
                duck_con=duck_con,
                resolve_sid=lambda fid: _SID,
                asof_resolver=partial(srct_asof.asof_rows, store, duck_con),
            ),
        )
        assert stats == {
            "scanner": "s4",
            "n_fixtures": 0,
            "n_observed": 0,
            "n_stale": 0,
            "n_confirmed": 0,
            "n_fired": 0,
            "n_hit": 0,
        }
    finally:
        duck_con.close()
        store.close()


def test_digest_includes_srct_season_rhythm(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """
    票 19 周报数据积累面：锚书当季（周窗末日所在季）入库节奏节——
    cid177 场次在库 + 最新观测；duck 桥缺席时该节如实 None 不冒充。
    """
    store, duck_path = _build_s4_corpus(tmp_path)
    duck_con = duckdb.connect(duck_path, read_only=True)
    try:
        report = edge_scan.build_edge_digest(
            db, week_end=_BUSINESS_DATE, duck_con=duck_con
        )
        rhythm = report["srct_rhythm"]
        assert rhythm is not None
        assert rhythm["season"] == "2026-27"  # 周窗末日 2026-11-09 所在季
        assert rhythm["n_sids"] == 1  # _build_s4_corpus 一 sid cid177 轨迹
        assert rhythm["latest_observed"] == "2026-11-10T02:00:00+00:00"
        md = edge_scan._render_markdown(report)
        assert "2026-27" in md
        assert "1 场在库" in md
    finally:
        duck_con.close()
        store.close()
    # 桥缺席：节存在但 None（不静默省节——周报读者要能看出积累面断了）
    without_duck = edge_scan.build_edge_digest(db, week_end=_BUSINESS_DATE)
    assert without_duck["srct_rhythm"] is None
    assert "入库节奏" in edge_scan._render_markdown(without_duck)
    assert "桥缺席" in edge_scan._render_markdown(without_duck)


# ---- S5：滚存回看 ----


def _seed_pool_period(
    db: sqlite3.Connection,
    *,
    period_no: str,
    sales: float | None,
    rollover_in: float | None,
    pool_after: float | None,
    published_at: str,
) -> None:
    period = pool_store.upsert_pool_period(db, "ttt14", period_no, None)
    pool_store.upsert_pool_state(
        db,
        period,
        sales_amount=sales,
        rollover_in=rollover_in,
        prize_tiers={
            "sfc": {
                "pool_after": pool_after,
                "tiers": [
                    {
                        "level": "一等奖",
                        "stake_count": 1.0,
                        "prize_amount": 5_000_000.0,
                    }
                ],
            }
        },
        published_at=published_at,
        source="official",
    )
    db.commit()


def test_s5_replay_and_refresh_window(db: sqlite3.Connection) -> None:
    """首跑=历史回放（含滚存标注）；已观测旧期冻结、近窗期次保鲜重扫。"""
    _seed_pool_period(
        db,
        period_no="26001",
        sales=10_000.0,
        rollover_in=1_000.0,
        pool_after=500.0,
        published_at="2026-10-01T16:00:00+00:00",
    )
    _seed_pool_period(  # 近窗发布：次日仍重扫
        db,
        period_no="26230",
        sales=20_000.0,
        rollover_in=0.0,
        pool_after=0.0,
        published_at="2026-11-06T16:00:00+00:00",
    )
    ctx = edge_scan.ScanContext(business_date="2026-11-07")
    first = edge_scan.run_s5(db, ctx)
    assert first["periods_scanned"] == 2
    assert first["rollover_in_periods"] == 1
    assert first["rollover_out_periods"] == 1
    payload = json.loads(
        db.execute(
            "SELECT payload FROM edge_observations WHERE scanner='s5'"
            " AND window_key='ttt14:26001'"
        ).fetchone()[0]
    )
    assert payload["boost_ratio"] == 0.1
    assert payload["rollover_out"] is True
    assert payload["games"]["sfc"]["top_prize"] == 5_000_000.0
    # 次日重跑：旧期冻结，近窗期次（11-06 发布，保鲜窗内）重扫
    second = edge_scan.run_s5(db, edge_scan.ScanContext(business_date="2026-11-08"))
    assert second["periods_scanned"] == 1


# ---- 周报 digest + 台账状态 ----


def test_digest_and_finding_lifecycle(db: sqlite3.Connection) -> None:
    """周报结构（本周发现/晋升建议）+ mark_finding_status 人裁通道。"""
    db.execute(
        """
        INSERT INTO edge_findings (scanner, segment_key, metric_value, n, se,
                                   window, status, found_at, notes)
        VALUES ('s1', 'p00-10:a', 0.587, 3, 0.156, '2026-08-11..2026-11-09',
                'new', '2026-11-09T16:00:00+00:00', '{}')
        """
    )
    db.execute(
        """
        INSERT INTO edge_observations (scanner, window_key, payload, created_at)
        VALUES ('s1', '2026-11-09', ?, '2026-11-09T16:00:00+00:00'),
               ('s4', '2026-11-09', ?, '2026-11-09T16:00:00+00:00')
        """,
        (
            json.dumps(
                {
                    "face": "runtime",
                    "window": "2026-08-12..2026-11-09",
                    "segments": {"p00-10:a": {"n": 3, "crossed": True}},
                }
            ),
            json.dumps({"business_date": "2026-11-09", "n_observed": 5}),
        ),
    )
    db.commit()
    report = edge_scan.build_edge_digest(db, week_end="2026-11-09")
    assert report["week"] == {"from": "2026-11-03", "to": "2026-11-09"}
    assert report["findings"]["week"][0]["segment_key"] == "p00-10:a"
    # 仍 new 且最新观测仍越门 → 晋升建议（人裁，不自动）
    assert report["findings"]["promotion_suggestions"] == ["s1:p00-10:a"]
    assert report["s4"]["days"][0]["n_observed"] == 5
    changed = edge_scan.mark_finding_status(
        db, scanner="s1", segment_key="p00-10:a", status="promoted"
    )
    assert changed == 1
    assert (
        db.execute(
            "SELECT status FROM edge_findings WHERE segment_key='p00-10:a'"
        ).fetchone()[0]
        == "promoted"
    )
    with pytest.raises(ValueError, match="promoted"):
        edge_scan.mark_finding_status(
            db, scanner="s1", segment_key="p00-10:a", status="new"
        )


def test_dead_finding_is_sticky(db: sqlite3.Connection) -> None:
    """人裁 dead 段粘滞：滚动窗滑日后同段再越门不落新行（spec 死刑留痕）。"""
    db.execute(
        """
        INSERT INTO edge_findings (scanner, segment_key, metric_value, n, se,
                                   window, status, found_at, notes)
        VALUES ('s1', 'p00-10:a', -0.1, 120, 0.01, '2026-08-11..2026-11-08',
                'dead', '2026-11-08T16:00:00+00:00', '{}')
        """
    )
    db.commit()
    landed = edge_scan.insert_findings(
        db,
        scanner="s1",
        window="2026-08-12..2026-11-09",
        segments={"p00-10:a": {"crossed": True, "dev": -0.11, "n": 130, "se": 0.01}},
        extra_notes={"face": "runtime"},
    )
    assert landed == 0  # 已裁段不再进台账
    assert db.execute("SELECT COUNT(*) FROM edge_findings").fetchone()[0] == 1


def test_edge_scan_day_isolates_single_scanner_failure(
    db: sqlite3.Connection,
) -> None:
    """单扫描器炸只降级自身，其余照常留痕（无信号日记零保证不丢）。"""

    def boom(conn: sqlite3.Connection, ctx: edge_scan.ScanContext) -> dict[str, Any]:
        raise RuntimeError("s1 断线")

    original = edge_scan.SCANNERS["s1"]
    edge_scan.SCANNERS["s1"] = replace(original, run=boom)
    try:
        stats = edge_scan.edge_scan_day(db, business_date=_BUSINESS_DATE, now=None)
    finally:
        edge_scan.SCANNERS["s1"] = original
    assert stats["s1"] == {"error": "s1 断线", "degraded": True}
    # s2/s4/s5 照常留痕
    assert stats["s2"]["scanner"] == "s2"
    assert stats["s4"]["scanner"] == "s4"
    assert stats["s5"]["scanner"] == "s5"
    assert (
        db.execute(
            "SELECT COUNT(*) FROM edge_observations WHERE scanner='s4'"
        ).fetchone()[0]
        == 1
    )


# ---- CLI + 晨检接线 ----


def test_cli_edge_scan_single_scanner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--scanner s4 单扫 + --no-corpus 冒烟（真源网络永不进测试）。"""
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    try:
        migrate(conn)
        _seed_fixture(
            conn,
            kickoff=_KICKOFF,
            business_date=_BUSINESS_DATE,
            home_goals=1,
            away_goals=1,
            jc_odds=(2.0, 3.4, 4.2),
            jc_captured_at="2026-11-09T15:00:00+00:00",
        )
    finally:
        conn.close()
    settings = Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    _cmd_edge_scan(
        argparse.Namespace(
            scanner="s4",
            window_end=_BUSINESS_DATE,
            face="runtime",
            digest=False,
            mark=None,
            no_corpus=True,
        ),
        settings=settings,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["s4"]["n_observed"] == 1
    assert payload["s4"]["n_confirmed"] == 0  # 无 sharp 锚：有分辨记零


def test_cli_edge_scan_composes_asof_resolver(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    票 19 组合根接线：CLI 日扫在 corpus 在场时注入青铜重放——留痕口径
    注记=corpus_asof 且历史时点有分辨（--no-corpus 不建树不注入）。
    """
    # 语料树先建（duck 桥打开前在位）
    _build_s4_corpus(tmp_path)  # 语料落 tmp_path/"corpus"（同根复用）
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    try:
        migrate(conn)
        _seed_s4_corpus_fixture(conn)
        # CLI 的 resolve_sid 走 mapping 面：物化链在场（票 77 语义）
        conn.execute(
            """
            INSERT INTO source_match_links
                (fixture_id, source, source_match_id, method, status, meta,
                 mapped_at)
            SELECT id, 'srct', ?, 'primary_key', 'linked', '{}',
                   '2026-11-09T00:00:00+00:00'
            FROM fixtures WHERE kickoff_utc = ?
            """,
            (_SID, _KICKOFF),
        )
        conn.commit()
    finally:
        conn.close()
    settings = Settings(corpus_root=tmp_path / "corpus", db_path=db_path)
    _cmd_edge_scan(
        argparse.Namespace(
            scanner="s4",
            window_end=_BUSINESS_DATE,
            face="runtime",
            digest=False,
            mark=None,
            no_corpus=False,
        ),
        settings=settings,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["s4"]["n_confirmed"] == 1  # as-of 接线后有分辨非零
    with sqlite3.connect(db_path) as check:
        observation = json.loads(
            check.execute(
                "SELECT payload FROM edge_observations WHERE scanner='s4'"
                " AND window_key=?",
                (_BUSINESS_DATE,),
            ).fetchone()[0]
        )
    assert observation["sharp_source"] == "corpus_asof"


def test_morning_check_edge_scan_item(db: sqlite3.Connection) -> None:
    """s1/s2/s4 留痕新鲜=绿；断流/首跑前=黄（观察项不红）。"""
    fresh = datetime(2026, 11, 9, 1, 0, tzinfo=UTC)
    item = check_edge_scan(db, now=fresh)
    assert item.status == "degraded"  # 无留痕：黄不红
    db.execute(
        """
        INSERT INTO edge_observations (scanner, window_key, payload, created_at)
        VALUES ('s1', '2026-11-08', '{}', '2026-11-08T16:00:00+00:00'),
               ('s2', '2026-11-08', '{}', '2026-11-08T16:00:00+00:00'),
               ('s4', '2026-11-08', '{}', '2026-11-08T16:00:00+00:00')
        """
    )
    db.commit()
    item = check_edge_scan(db, now=fresh)
    assert item.status == "ok"
    assert "9.0h" in item.detail


def test_decade_observation_does_not_mask_stale_daily_face(
    db: sqlite3.Connection,
) -> None:
    """decade 验证行不计新鲜度：日频面断流不被手动十年回放掩盖（F1）。"""
    db.execute(
        """
        INSERT INTO edge_observations (scanner, window_key, payload, created_at)
        VALUES ('s1', '2026-11-08', '{}', '2026-11-08T16:00:00+00:00'),
               ('s1', 'decade:23', '{}', '2026-11-09T00:00:00+00:00'),
               ('s4', '2026-11-08', '{}', '2026-11-08T16:00:00+00:00')
        """
    )
    db.commit()
    # s1 运行面留痕 33h 前（>26h 黄）；decade 行刚写不得救绿
    item = check_edge_scan(db, now=datetime(2026, 11, 10, 1, 0, tzinfo=UTC))
    assert item.status == "degraded"
    assert edge_scan.latest_observation_at(db, "s1") == "2026-11-08T16:00:00+00:00"


def test_s5_null_published_at_always_refreshes(db: sqlite3.Connection) -> None:
    """发布日未知（NULL published_at）的已观测期次不冻结（F2）。"""
    period = pool_store.upsert_pool_period(db, "ttt14", "26240", None)
    pool_store.upsert_pool_state(
        db,
        period,
        sales_amount=None,
        rollover_in=None,
        prize_tiers=None,  # 首夜只有销量面之外的空 state
        published_at=None,
        source="official",
    )
    db.commit()
    ctx = edge_scan.ScanContext(business_date="2026-11-08")
    first = edge_scan.run_s5(db, ctx)
    assert first["periods_scanned"] == 1
    # 官方更正后补 prize_tiers（published_at 仍 NULL）→ 次日必须重扫
    pool_store.upsert_pool_state(
        db,
        period,
        sales_amount=10_000.0,
        rollover_in=2_000.0,
        prize_tiers={"sfc": {"pool_after": 0.0, "tiers": []}},
        published_at=None,
        source="official",
    )
    db.commit()
    second = edge_scan.run_s5(db, edge_scan.ScanContext(business_date="2026-11-09"))
    assert second["periods_scanned"] == 1
    payload = json.loads(
        db.execute(
            "SELECT payload FROM edge_observations"
            " WHERE scanner='s5' AND window_key='ttt14:26240'"
        ).fetchone()[0]
    )
    assert payload["rollover_in"] == 2_000.0  # 更正后的值已进留痕


def test_future_business_date_rejected(db: sqlite3.Connection) -> None:
    """远未来观察日拒绝（空窗会霸占周报最新窗位，F5）。"""
    with pytest.raises(ValueError, match="晚于北京今日"):
        edge_scan.run_s4(db, edge_scan.ScanContext(business_date="2027-01-01"))
    with pytest.raises(ValueError, match="晚于北京今日"):
        edge_scan.run_s5(db, edge_scan.ScanContext(business_date="2027-01-01"))


# ---- S2：模型校准 + EV 实现差（票 02） ----

# 矩阵：had=(h0.95, d0.02, a0.03) 最热向 h 置信 0.95；ttg 档1=0.98；
# crs 最热 1:0=0.95——0:1 客胜时三面最热向全落 bin9、had/crs 未命中。
_S2_MATRIX = [[0.0] * 10 for _ in range(10)]
_S2_MATRIX[1][0] = 0.95
_S2_MATRIX[0][1] = 0.03
_S2_MATRIX[0][0] = 0.02


def _seed_scored_fixture(
    db: sqlite3.Connection,
    *,
    tag: str,
    kickoff: str,
    business_date: str,
    home_goals: int,
    away_goals: int,
) -> None:
    """一场已结算竞彩场 + 赛前 ML Forecast → 评分链真实落 forecast_scores 行。"""
    fid = _seed_fixture(
        db,
        kickoff=kickoff,
        business_date=business_date,
        home_goals=home_goals,
        away_goals=away_goals,
    )
    db.execute(
        "INSERT INTO forecasts (fixture_id, track, model_version, issued_at,"
        " content_hash, payload) VALUES (?, 'ml', 'dc-test', ?, ?, ?)",
        (
            fid,
            "2026-11-09T08:00:00+00:00",
            f"hash-{tag}",
            json.dumps({"matrix": _S2_MATRIX, "lambda_home": 1.0, "lambda_away": 0.8}),
        ),
    )
    db.commit()


def _s2_payload(db: sqlite3.Connection) -> dict[str, Any]:
    return json.loads(
        db.execute(
            "SELECT payload FROM edge_observations WHERE scanner='s2' AND window_key=?",
            (_BUSINESS_DATE,),
        ).fetchone()[0]
    )


def test_s2_calibration_bins_and_gate(db: sqlite3.Connection) -> None:
    """三场客胜：had/crs 落 b9 全未中（dev=−.95>2se）、ttg b9 命中不越门。"""
    for i in range(3):
        _seed_scored_fixture(
            db,
            tag=f"c{i}",
            kickoff=f"2026-11-09T1{i}:00:00+00:00",
            business_date=_BUSINESS_DATE,
            home_goals=0,
            away_goals=1,
        )
    accuracy.score_settled_fixtures(db)
    assert edge_scan.S2_MIN_N == 200  # 票面冻结门（防常量漂移）
    stats = edge_scan.run_s2(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    payload = _s2_payload(db)
    had_b9 = payload["calibration"]["had:b9"]
    assert had_b9["n"] == 3
    assert had_b9["mean_prob"] == pytest.approx(0.95)
    assert had_b9["actual_freq"] == 0.0
    assert had_b9["dev"] == pytest.approx(-0.95)
    assert had_b9["se"] == pytest.approx(math.sqrt(0.95 * 0.05 / 3), abs=1e-6)
    assert had_b9["crossed"] is False  # n=3<200：不越门
    ttg_b9 = payload["calibration"]["ttg:b9"]
    assert ttg_b9["actual_freq"] == 1.0  # 客胜 0:1 总进球 1 → 档"1"命中
    assert payload["ev"] == {}  # 纸面注未起量：面 B 如实空
    assert stats["findings_landed"] == 0
    assert stats["ev_bets"] == 0


def test_s2_log_loss_third_metric_face(db: sqlite3.Connection) -> None:
    """
    票 32 面 C：log loss 第三指标进 S2 报告面（观测面，不进发现门）。

    同三场客胜世界：had 实现向 'a' p=.03 → LL=−ln(.03)（尾部过自信示范：
    skill<0）；ttg 实现档 "1" p=.95 → LL=−ln(.95)（skill>0）；无 RW/越门
    字段——log loss 不新增多重比较面。
    """
    for i in range(3):
        _seed_scored_fixture(
            db,
            tag=f"l{i}",
            kickoff=f"2026-11-09T1{i}:40:00+00:00",
            business_date=_BUSINESS_DATE,
            home_goals=0,
            away_goals=1,
        )
    accuracy.score_settled_fixtures(db)
    stats = edge_scan.run_s2(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    payload = _s2_payload(db)
    ll = payload["log_loss"]
    assert set(ll) == {"had", "ttg", "crs"}
    assert ll["had"] == {
        "market": "had",
        "n": 3,
        "n_classes": 3,
        "mean_log_loss": pytest.approx(-math.log(0.03), abs=1e-6),
        "uniform_anchor": pytest.approx(math.log(3), abs=1e-6),
        "skill_vs_uniform": pytest.approx(1 + math.log(0.03) / math.log(3), abs=1e-6),
    }
    assert ll["had"]["skill_vs_uniform"] < 0  # 尾部过自信：对均匀锚反向
    # ttg 档 "1"=P(1:0)+P(0:1)=0.95+0.03=0.98（矩阵两格并入同档）
    assert ll["ttg"]["mean_log_loss"] == pytest.approx(-math.log(0.98), abs=1e-6)
    assert ll["ttg"]["n_classes"] == 8
    assert ll["ttg"]["skill_vs_uniform"] > 0
    # log loss 不产发现、不进越门返回面
    assert stats["findings_landed"] == 0
    assert stats["crossed"] == []


def test_s2_calibration_gate_crossing_idempotent(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """降门后 had:b9/crs:b9 越门落发现；同窗重跑幂等不双行。"""
    monkeypatch.setattr(edge_scan, "S2_MIN_N", 3)
    for i in range(3):
        _seed_scored_fixture(
            db,
            tag=f"g{i}",
            kickoff=f"2026-11-09T1{i}:30:00+00:00",
            business_date=_BUSINESS_DATE,
            home_goals=0,
            away_goals=1,
        )
    accuracy.score_settled_fixtures(db)
    ctx = edge_scan.ScanContext(business_date=_BUSINESS_DATE)
    first = edge_scan.run_s2(db, ctx)
    assert sorted(first["crossed"]) == ["crs:b9", "had:b9"]
    assert first["findings_landed"] == 2
    row = db.execute(
        "SELECT metric_value, n, se, window, status FROM edge_findings"
        " WHERE scanner='s2' AND segment_key='had:b9'"
    ).fetchone()
    assert row["metric_value"] == pytest.approx(-0.95)
    assert row["window"].endswith(_BUSINESS_DATE)
    assert row["status"] == "new"
    second = edge_scan.run_s2(db, ctx)
    assert second["findings_landed"] == 0
    assert (
        db.execute("SELECT COUNT(*) FROM edge_findings WHERE scanner='s2'").fetchone()[
            0
        ]
        == 2
    )


def test_s2_window_scopes_by_kickoff_date(db: sqlite3.Connection) -> None:
    """kickoff UTC 日期窗切片：窗外（>90 日前）评分行不进面 A。"""
    _seed_scored_fixture(
        db,
        tag="w1",
        kickoff="2026-11-09T18:00:00+00:00",
        business_date=_BUSINESS_DATE,
        home_goals=0,
        away_goals=1,
    )
    _seed_scored_fixture(  # 窗外
        db,
        tag="w2",
        kickoff="2026-08-01T18:00:00+00:00",
        business_date="2026-08-01",
        home_goals=0,
        away_goals=1,
    )
    accuracy.score_settled_fixtures(db)
    stats = edge_scan.run_s2(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    assert stats["calib_binned"] == 3  # 窗内一场：had/ttg/crs 三行进箱
    payload = _s2_payload(db)
    assert set(payload["calibration"]) == {"had:b9", "ttg:b9", "crs:b9"}


def _seed_paper_bet(
    db: sqlite3.Connection,
    *,
    strategy_version: str | None,
    snap_ev_model: float | None,
    status: str,
    profit: float | None,
    settled_at: str | None,
    stake: float = 2.0,
    price_source: str | None = "system_paper",
    purchased: int = 0,
) -> None:
    db.execute(
        """
        INSERT INTO bets (mode, market_kind, purchased, stake, created_at,
                          status, profit, settled_at, strategy_version,
                          snap_ev_model, price_source)
        VALUES ('paper', 'fixed', ?, ?, '2026-11-09T05:00:00+00:00',
                ?, ?, ?, ?, ?, ?)
        """,
        (
            purchased,
            stake,
            status,
            profit,
            settled_at,
            strategy_version,
            snap_ev_model,
            price_source,
        ),
    )
    db.commit()


def test_s2_ev_realization_math(db: sqlite3.Connection) -> None:
    """配对差=实现单位损益−模型 EV：均值/样本 SE 数值钉死（2 负 1 正）。"""
    _seed_paper_bet(
        db,
        strategy_version="auto-v1",
        snap_ev_model=0.1,
        status="lost",
        profit=-2.0,
        settled_at="2026-11-08T20:00:00+00:00",
    )
    _seed_paper_bet(
        db,
        strategy_version="auto-v1",
        snap_ev_model=0.1,
        status="lost",
        profit=-2.0,
        settled_at="2026-11-08T20:05:00+00:00",
    )
    _seed_paper_bet(
        db,
        strategy_version="auto-v1",
        snap_ev_model=0.1,
        status="won",
        profit=6.0,
        settled_at="2026-11-08T20:10:00+00:00",
    )
    edge_scan.run_s2(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    seg = _s2_payload(db)["ev"]["ev:auto-v1"]
    assert seg["n"] == 3
    assert seg["mean_ev"] == pytest.approx(0.1)
    assert seg["mean_realized"] == pytest.approx(1 / 3)  # (−1−1+3)/3
    assert seg["dev"] == pytest.approx(0.2333333, abs=1e-6)
    assert seg["se"] == pytest.approx(1.088662, abs=1e-5)  # pstdev([−1.1,−1.1,2.9])/√3
    assert seg["crossed"] is False


def test_s2_ev_gate_and_exclusions(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """越门落发现（分来源段）；窗外/void/manual_research/无快照/open 不进分母。"""
    monkeypatch.setattr(edge_scan, "S2_MIN_N", 2)
    for i in range(2):  # 输注 diff −1.5 ×2
        _seed_paper_bet(
            db,
            strategy_version="auto-v1",
            snap_ev_model=0.5,
            status="lost",
            profit=-2.0,
            settled_at=f"2026-11-08T21:0{i}:00+00:00",
        )
    _seed_paper_bet(  # 低赔赢注 diff 0.0 → 段有方差且 |dev|=1>2se≈0.82
        db,
        strategy_version="auto-v1",
        snap_ev_model=0.5,
        status="won",
        profit=1.0,
        settled_at="2026-11-08T21:05:00+00:00",
    )
    _seed_paper_bet(  # 窗外（settled >90 日前）
        db,
        strategy_version="auto-v1",
        snap_ev_model=0.5,
        status="lost",
        profit=-2.0,
        settled_at="2026-08-01T21:00:00+00:00",
    )
    _seed_paper_bet(  # void：本金退回无实现信号
        db,
        strategy_version="auto-v1",
        snap_ev_model=0.5,
        status="void",
        profit=0.0,
        settled_at="2026-11-08T21:10:00+00:00",
    )
    _seed_paper_bet(  # manual_research：正式分母排除口径
        db,
        strategy_version="manual",
        snap_ev_model=0.5,
        status="lost",
        profit=-2.0,
        settled_at="2026-11-08T21:11:00+00:00",
        price_source="manual_research",
    )
    _seed_paper_bet(  # 无 EV 快照：非模型口径可判面
        db,
        strategy_version="auto-v1",
        snap_ev_model=None,
        status="lost",
        profit=-2.0,
        settled_at="2026-11-08T21:12:00+00:00",
    )
    _seed_paper_bet(  # 未结算
        db,
        strategy_version="auto-v1",
        snap_ev_model=0.5,
        status="open",
        profit=None,
        settled_at=None,
    )
    _seed_paper_bet(  # 携带 actual 条款的异常旧行：正式面同款排除（单位不符）
        db,
        strategy_version="auto-v1",
        snap_ev_model=0.5,
        status="lost",
        profit=-3.0,
        settled_at="2026-11-08T21:14:00+00:00",
    )
    db.execute(
        "UPDATE bets SET actual_stake = 3.0 WHERE rowid = (SELECT MAX(rowid) FROM bets)"
    )
    db.commit()
    _seed_paper_bet(  # 无策略版本 → manual 段（非 manual_research 价源行）
        db,
        strategy_version=None,
        snap_ev_model=0.5,
        status="lost",
        profit=-2.0,
        settled_at="2026-11-08T21:13:00+00:00",
        price_source=None,
    )
    stats = edge_scan.run_s2(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    assert stats["ev_bets"] == 4  # 3 auto-v1 + 1 manual 段
    assert stats["crossed"] == ["ev:auto-v1"]  # manual 段 n=1<门不越
    assert stats["findings_landed"] == 1
    rows = db.execute(
        "SELECT segment_key, metric_value, n, se FROM edge_findings"
        " WHERE scanner='s2' AND segment_key LIKE 'ev:%'"
    ).fetchall()
    assert [r["segment_key"] for r in rows] == ["ev:auto-v1"]
    assert rows[0]["metric_value"] == pytest.approx(-1.0)  # (−1.5−1.5+0)/3
    assert rows[0]["n"] == 3
    assert rows[0]["se"] == pytest.approx(0.408248, abs=1e-5)
    seg_manual = _s2_payload(db)["ev"]["ev:manual"]
    assert seg_manual["n"] == 1
    assert seg_manual["crossed"] is False


def test_s2_ev_zero_variance_not_crossing(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """恒定 diff（同质注批）SE 不可估 → 不越门不落发现（Spec 审查裁定）。"""
    monkeypatch.setattr(edge_scan, "S2_MIN_N", 2)
    for i in range(3):  # 全输同 EV 同注额：diff 恒 −1.5
        _seed_paper_bet(
            db,
            strategy_version="auto-v1",
            snap_ev_model=0.5,
            status="lost",
            profit=-2.0,
            settled_at=f"2026-11-08T22:0{i}:00+00:00",
        )
    stats = edge_scan.run_s2(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    seg = _s2_payload(db)["ev"]["ev:auto-v1"]
    assert seg["dev"] == pytest.approx(-1.5)
    assert seg["se"] == 0.0
    assert seg["crossed"] is False
    assert stats["findings_landed"] == 0


def test_s2_ev_excludes_rebound_purchased_bets(db: sqlite3.Connection) -> None:
    """回录锁定价重绑行不进面 B：重绑刷 locked_odds 不刷快照，跨条款配对失真（F1）。"""
    _seed_paper_bet(  # 建注即决策（未回录）——面 B 分母内
        db,
        strategy_version="auto-v1",
        snap_ev_model=0.1,
        status="lost",
        profit=-2.0,
        settled_at="2026-11-08T20:00:00+00:00",
    )
    _seed_paper_bet(  # 已回录：record_purchase 可能重绑价而快照停在发现价 → 排除
        db,
        strategy_version=None,
        snap_ev_model=0.1,
        status="lost",
        profit=-2.0,
        settled_at="2026-11-08T20:01:00+00:00",
        purchased=1,
    )
    stats = edge_scan.run_s2(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    assert stats["ev_bets"] == 1
    assert set(_s2_payload(db)["ev"]) == {"ev:auto-v1"}


def test_s2_coverage_matches_segment_denominators(db: sqlite3.Connection) -> None:
    """零注额行不进段也不进 coverage——两数可对账（F2）。"""
    _seed_paper_bet(
        db,
        strategy_version="auto-v1",
        snap_ev_model=0.1,
        status="lost",
        profit=-2.0,
        settled_at="2026-11-08T20:00:00+00:00",
    )
    _seed_paper_bet(  # CHECK(stake>=0) 允许的零注额行：无单位损益
        db,
        strategy_version="auto-v1",
        snap_ev_model=0.1,
        status="lost",
        profit=0.0,
        settled_at="2026-11-08T20:01:00+00:00",
        stake=0.0,
    )
    stats = edge_scan.run_s2(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    payload = _s2_payload(db)
    assert stats["ev_bets"] == 1
    assert payload["coverage"]["ev_bets"] == sum(s["n"] for s in payload["ev"].values())


def test_edge_scan_day_rolls_back_failed_scanner_writes(
    db: sqlite3.Connection,
) -> None:
    """写相炸的扫描器其未提交事务被回滚：发现行不被后续扫描器顺带落库（F3）。"""

    def half_written_then_boom(
        conn: sqlite3.Connection, ctx: edge_scan.ScanContext
    ) -> dict[str, Any]:
        # 模拟 insert_findings 已 BEGIN IMMEDIATE 落行后、commit 前崩
        conn.execute(
            """
            INSERT INTO edge_findings (scanner, segment_key, metric_value, n, se,
                                       window, status, found_at, notes)
            VALUES ('s2', 'ev:auto-v1', -1.0, 250, 0.1, 'x..2026-11-09',
                    'new', '2026-11-09T16:00:00+00:00', '{}')
            """
        )
        raise RuntimeError("s2 写相崩溃")

    original = edge_scan.SCANNERS["s2"]
    edge_scan.SCANNERS["s2"] = replace(original, run=half_written_then_boom)
    try:
        stats = edge_scan.edge_scan_day(db, business_date=_BUSINESS_DATE, now=None)
    finally:
        edge_scan.SCANNERS["s2"] = original
    assert stats["s2"] == {"error": "s2 写相崩溃", "degraded": True}
    # 半套发现行被回滚；s4/s5 照常留痕（其 commit 不再顺带落 s2 的写入）
    assert db.execute("SELECT COUNT(*) FROM edge_findings").fetchone()[0] == 0
    assert stats["s4"]["scanner"] == "s4"
    assert stats["s5"]["scanner"] == "s5"


def test_s2_daily_face_records_honest_zero(db: sqlite3.Connection) -> None:
    """空库日扫：面 A/B 如实零段（未起量期不缺留痕）。"""
    stats = edge_scan.run_s2(db, edge_scan.ScanContext(business_date=_BUSINESS_DATE))
    assert stats["calib_binned"] == 0
    assert stats["ev_bets"] == 0
    assert stats["crossed"] == []
    payload = _s2_payload(db)
    assert payload["calibration"] == {}
    assert payload["ev"] == {}


def test_digest_includes_s2_sections(db: sqlite3.Connection) -> None:
    """周报 S2 段（模型分箱表+EV）与按扫描器的晋升建议。"""
    db.execute(
        """
        INSERT INTO edge_findings (scanner, segment_key, metric_value, n, se,
                                   window, status, found_at, notes)
        VALUES ('s2', 'had:b9', -0.95, 250, 0.014, '2026-08-12..2026-11-09',
                'new', '2026-11-09T16:00:00+00:00', '{}')
        """
    )
    db.execute(
        """
        INSERT INTO edge_observations (scanner, window_key, payload, created_at)
        VALUES ('s2', '2026-11-09', ?, '2026-11-09T16:00:00+00:00')
        """,
        (
            json.dumps(
                {
                    "window": "2026-08-12..2026-11-09",
                    "coverage": {"calib_binned": 250, "ev_bets": 0},
                    "calibration": {
                        "had:b9": {
                            "n": 250,
                            "mean_prob": 0.95,
                            "actual_freq": 0.9,
                            "dev": -0.05,
                            "se": 0.014,
                            "crossed": True,
                        }
                    },
                    "ev": {},
                }
            ),
        ),
    )
    db.commit()
    report = edge_scan.build_edge_digest(db, week_end=_BUSINESS_DATE)
    assert report["s2"]["latest_window"] == "2026-08-12..2026-11-09"
    assert report["s2"]["calibration"]["had:b9"]["crossed"] is True
    # s2 段在 s2 自己的最新观测里仍越门 → 晋升建议（按扫描器各查各面）
    assert report["findings"]["promotion_suggestions"] == ["s2:had:b9"]
    md = edge_scan._render_markdown(report)
    assert "## S2 模型分箱表（最新窗）" in md
    assert "| had:b9 | 250 | 0.95 | 0.9" in md
    assert "越门 had:b9" in md  # 摘要行含两面越门段（不只 EV）


def test_digest_s1_rw_and_s2_log_loss_faces(db: sqlite3.Connection) -> None:
    """
    票 32 周报面：S1 段带 RW 列（晋升建议按 RW 口径）+ S2 log loss 表与判读分层。

    s1 段 crossed（未校正）与 crossed_rw 分列——RW 拒绝的段不进晋升建议
    （只收紧）；无 RW 字段的旧 payload 段回退未校正口径不误杀。s2 log_loss
    表随最新观测进周报 + 冻结判读分层文字随报告展示。
    """
    db.execute(
        """
        INSERT INTO edge_findings (scanner, segment_key, metric_value, n, se,
                                   window, status, found_at, notes)
        VALUES ('s1', 'p10-20:d', -0.077, 116, 0.033, '2026-08-12..2026-11-09',
                'new', '2026-11-09T16:00:00+00:00', '{}'),
               ('s1', 'p00-10:a', 0.587, 120, 0.156, '2026-08-12..2026-11-09',
                'new', '2026-11-09T16:00:00+00:00', '{}')
        """
    )
    db.execute(
        """
        INSERT INTO edge_observations (scanner, window_key, payload, created_at)
        VALUES ('s1', '2026-11-09', ?, '2026-11-09T16:00:00+00:00'),
               ('s2', '2026-11-09', ?, '2026-11-09T16:00:00+00:00')
        """,
        (
            json.dumps(
                {
                    "face": "runtime",
                    "window": "2026-08-12..2026-11-09",
                    "segments": {
                        # 待晋升人裁段：未校正越门、RW 拒绝（票 32 对照读数形态）
                        "p10-20:d": {
                            "n": 116,
                            "mean_prob": 0.2,
                            "actual_freq": 0.123,
                            "dev": -0.077,
                            "se": 0.033,
                            "crossed": True,
                            "p_raw": 0.021,
                            "p_rw": 0.063,
                            "crossed_rw": False,
                        },
                        "p00-10:a": {
                            "n": 120,
                            "mean_prob": 0.08,
                            "actual_freq": 0.667,
                            "dev": 0.587,
                            "se": 0.156,
                            "crossed": True,
                            "p_raw": 0.0002,
                            "p_rw": 0.001,
                            "crossed_rw": True,
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "window": "2026-08-12..2026-11-09",
                    "coverage": {"calib_binned": 3, "ev_bets": 0},
                    "calibration": {},
                    "ev": {},
                    "log_loss": {
                        "had": {
                            "market": "had",
                            "n": 3,
                            "n_classes": 3,
                            "mean_log_loss": 3.506557,
                            "uniform_anchor": 1.098612,
                            "skill_vs_uniform": -2.191713,
                        }
                    },
                }
            ),
        ),
    )
    db.commit()
    report = edge_scan.build_edge_digest(db, week_end=_BUSINESS_DATE)
    # 晋升建议=RW 后仍越门（p00-10:a）；p10-20:d 未校正越门但 RW 拒绝 → 不建议
    assert report["findings"]["promotion_suggestions"] == ["s1:p00-10:a"]
    assert report["s2"]["log_loss"]["had"]["n"] == 3
    md = edge_scan._render_markdown(report)
    assert "| p10-20:d | 116" in md  # S1 表段行
    assert "| 0.063 |" in md  # S1 表 RW p 列
    assert "## S2 log loss 第三指标（最新窗；票 32，观测面不进发现门）" in md
    assert "| had | 3 | 3 | 3.506557 | 1.098612 | -2.191713 |" in md
    assert "log loss 判读分层（Wheatcroft 2021，RPS 之争对冲）" in md
    assert any("层 0 结构" in line for line in edge_scan.S2_LOSS_READING_RULE)
