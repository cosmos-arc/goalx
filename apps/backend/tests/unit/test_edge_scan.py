"""autoresearch 票 01/04：探索轨扫描器阵列（S1/S4/S5 + 台账 + 周报）。

口径直证：S1 分桶去水数学/越门判定/幂等；S4 观测日计数（含无信号日记零）；
S5 滚存回看（缺席自补+近窗保鲜）；十年回放面；周报 digest；CLI/晨检接线。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from goalx_backend.cli import _cmd_edge_scan
from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.data import results as rs_store
from goalx_backend.db import connect, connect_research, migrate
from goalx_backend.evaluation import edge_scan
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
    # s4/s5 照常留痕
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


def test_morning_check_edge_scan_item(db: sqlite3.Connection) -> None:
    """s1/s4 留痕新鲜=绿；断流/首跑前=黄（观察项不红）。"""
    fresh = datetime(2026, 11, 9, 1, 0, tzinfo=UTC)
    item = check_edge_scan(db, now=fresh)
    assert item.status == "degraded"  # 无留痕：黄不红
    db.execute(
        """
        INSERT INTO edge_observations (scanner, window_key, payload, created_at)
        VALUES ('s1', '2026-11-08', '{}', '2026-11-08T16:00:00+00:00'),
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
