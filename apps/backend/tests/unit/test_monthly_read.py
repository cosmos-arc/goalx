"""vision-build 票 04：月读报告生成器（人的裁定面）。

八节只读报告的管线级钉住：报告月推导（每月 1 日报送覆盖刚结束的
自然月）、八节齐全+每节「建议动作+CLI」只读提示、报告文件 append-only
拍次（auto-paper 03 同缝）、票 08 缝回填（四信号触发史落账+B.3 到期
读数进⑥节）、双锚一致性断言（fixture 两锚差超界样例触发告警=硬验收）、
下月预算规则可复算。裁定动作全人 CLI——报告不写任何决策状态。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import NormalDist

import pytest

from goalx_backend.betting import versioning
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.evaluation import monthly_read

_RUN_AT = datetime(2026, 12, 1, 1, 35, tzinfo=UTC)  # 北京 12-01 09:35（首读）


def _store(tmp_path: Path) -> CorpusStore:
    return CorpusStore(tmp_path / "corpus")


def _seed_fixture(db: sqlite3.Connection, *, tag: str = "a", kickoff: str) -> int:
    """场次脚手架（fx_store 属主面建行；月读测试各节共用）。"""
    from goalx_backend.data import fixtures as fx_store

    comp = fx_store.upsert_competition(db, "英超")
    home = fx_store.upsert_team(db, f"主队{tag}")
    away = fx_store.upsert_team(db, f"客队{tag}")
    return fx_store.upsert_fixture(db, comp, kickoff, home, away)


# ---- 骨架：八节齐全 + 报告文件 + 票 08 缝落账 + 幂等重跑 ----


def test_run_monthly_read_eight_sections_and_files(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """首读跑通全八节：报告月=刚结束自然月；JSON+MD 落语料树 reports/monthly/；
    四信号评估落账恰四行；双钟读数落账两行。"""
    report = monthly_read.run_monthly_read(
        db, store=_store(tmp_path), now=_RUN_AT, lessons_dir=tmp_path
    )
    assert report["schema"] == "monthly-read/1"
    assert report["report_month"] == "2026-11"  # 12-01 报送 11 月
    assert report["run_date"] == "2026-12-01"
    sections = report["sections"]
    for key in (
        "gate_progress",
        "clv_dual_anchor",
        "shadow_compare",
        "version_events",
        "forced_failure",
        "clocks",
        "lessons_view",
        "budget_suggestion",
    ):
        assert key in sections, key
    # 文件双视图（语料树 reports/monthly/，票 03 同先例）
    base = tmp_path / "corpus" / "reports" / "monthly"
    json_path = base / "monthly-read-202611.json"
    md_path = base / "monthly-read-202611.md"
    assert json_path.is_file()
    assert md_path.is_file()
    assert (
        json.loads(json_path.read_text(encoding="utf-8"))["report_month"] == "2026-11"
    )
    md = md_path.read_text(encoding="utf-8")
    for header in ("①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧"):
        assert header in md, header
    assert "goalx" in md  # 建议动作+对应 CLI 命令只读提示
    # 票 08 缝：四信号评估落账（B.3 timer 不落账）；双钟读数落账
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM rescan_signal_events WHERE period = '2026-11'"
        ).fetchone()["c"]
        == 4
    )
    assert (
        db.execute("SELECT COUNT(*) c FROM contraction_clock_readings").fetchone()["c"]
        == 2
    )


def test_rerun_same_month_beat_increments_append_only(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """幂等重跑（auto-paper 03 同缝）：同月重跑报告文件拍次递增不覆盖；
    信号账同期吸收（每月一读）；首拍文件内容不被改写；双钟读数账=每次
    月读每钟追加一行（观测流水非吸收面，F3 裁定口径——重跑用不同 now）。"""
    store = _store(tmp_path)
    first = monthly_read.run_monthly_read(
        db, store=store, now=_RUN_AT, lessons_dir=tmp_path
    )
    assert first["beat"] == 1
    base = tmp_path / "corpus" / "reports" / "monthly"
    first_json = (base / "monthly-read-202611.json").read_text(encoding="utf-8")
    assert (
        db.execute("SELECT COUNT(*) c FROM contraction_clock_readings").fetchone()["c"]
        == 2
    )
    # 真实重跑=不同时刻（同 now 会靠 UNIQUE 同秒防重假吸收）
    second = monthly_read.run_monthly_read(
        db,
        store=store,
        now=_RUN_AT.replace(second=_RUN_AT.second + 30),
        lessons_dir=tmp_path,
    )
    assert second["beat"] == 2
    assert (base / "monthly-read-202611-2.json").is_file()
    assert (base / "monthly-read-202611-2.md").is_file()
    # append-only：首拍文件原样保留
    assert (base / "monthly-read-202611.json").read_text(encoding="utf-8") == first_json
    # 信号账 UNIQUE(signal, period) 吸收同期重跑（账=每月首读）
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM rescan_signal_events WHERE period = '2026-11'"
        ).fetchone()["c"]
        == 4
    )
    # 双钟读数账：每次月读每钟一行（两次读=4 行，F3）
    assert (
        db.execute("SELECT COUNT(*) c FROM contraction_clock_readings").fetchone()["c"]
        == 4
    )


def test_report_month_from_mid_month_run(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """报送月推导只看业务日所在月的前一自然月（月中补跑同报上月）。"""
    report = monthly_read.run_monthly_read(
        db,
        store=_store(tmp_path),
        now=datetime(2026, 12, 15, 8, 0, tzinfo=UTC),
        lessons_dir=tmp_path,
    )
    assert report["report_month"] == "2026-11"


# ---- ① 六门进度：家族总尝试数 + DSR/BH 校正读数框架 ----


def test_gate_progress_dsr_bh_framework(db: sqlite3.Connection) -> None:
    """DSR 期望 max-z（B&LdP 2014 公式独立复算）+ BH 临界梯按家族总尝试数。"""
    from goalx_backend.evaluation import hypothesis_registry as hr

    for i in range(4):
        hr.record_attempt(db, "H1", window_key=f"s1:w{i}")  # 同窗族计数=4
    section = monthly_read.gate_progress(db)
    family = next(f for f in section["families"] if f["family"] == "H1")
    assert family["m_attempts"] == 4  # v51 种子 H1 running，可计数
    # DSR 期望 max-z 独立复算（B&LdP：(1−γ)Φ⁻¹(1−1/N)+γΦ⁻¹(1−1/(N·e))）
    gamma = 0.5772156649015329
    nd = NormalDist()
    expected = (1 - gamma) * nd.inv_cdf(1 - 1 / 4) + gamma * nd.inv_cdf(
        1 - 1 / (4 * 2.718281828459045)
    )
    assert family["dsr_expected_max_z"] == pytest.approx(expected)
    # BH 临界梯（α=0.05）：k=1 阈=α/m，k=m 阍=α
    assert family["bh_thresholds"]["k1"] == pytest.approx(0.05 / 4)
    assert family["bh_thresholds"]["k_max"] == pytest.approx(0.05)
    # 注册表状态面随行（六步门逐门统计量接缝说明在场）
    assert section["gate_statistics_seam"]
    assert any(row["h_id"] == "H1" for row in section["registry_rows"])


def test_gate_progress_dsr_single_attempt_is_zero(db: sqlite3.Connection) -> None:
    """m=1：期望 max-z 退化 0（单次抽取期望即 0，不产 inf）。"""
    from goalx_backend.evaluation import hypothesis_registry as hr

    hr.record_attempt(db, "H4", window_key="s4:w1")  # H4 registered 可点亮计数
    section = monthly_read.gate_progress(db)
    family = next(f for f in section["families"] if f["family"] == "H4")
    assert family["m_attempts"] == 1
    assert family["dsr_expected_max_z"] == 0.0


# ---- ② CLV 审计双锚（硬验收：fixture 两锚差超界样例触发告警） ----


def _seed_anchored_leg(
    db: sqlite3.Connection,
    *,
    tag: str,
    close_prob: float,
    settled_at: str = "2026-11-06T21:00:00+00:00",
) -> tuple[int, int]:
    """一条已结算 auto 纸面注 + had/h 腿 + srct Pinnacle 主锚对账行。"""
    fid = _seed_fixture(db, tag=tag, kickoff="2026-11-05T18:00:00+00:00")
    cur = db.execute(
        "INSERT INTO bets (mode, market_kind, purchased, stake, placed_at,"
        " created_at, status, payout, profit, settled_at, strategy_version)"
        " VALUES ('paper', 'fixed', 1, 10.0, '2026-11-05T10:00:00+00:00',"
        " '2026-11-05T10:00:00+00:00', 'won', 19.0, 9.0, ?, 'auto-v1')",
        (settled_at,),
    )
    bet_id = int(cur.lastrowid)
    db.execute(
        "INSERT INTO bet_legs (bet_id, fixture_id, market_code, selection_code,"
        " locked_odds) VALUES (?, ?, 'had', 'h', 2.0)",
        (bet_id, fid),
    )
    db.execute(
        "INSERT INTO clv_records (bet_id, fixture_id, market_code,"
        " selection_code, taken_odds, close_prob, clv_prob, close_source,"
        " computed_at, close_basis) VALUES (?, ?, 'had', 'h', 2.0, ?, 0.0,"
        " 'srct_1x2_closing', '2026-11-06T22:00:00+00:00', 'srct_pinnacle')",
        (bet_id, fid, close_prob),
    )
    db.commit()
    return bet_id, fid


def _seed_consensus_snapshots(
    db: sqlite3.Connection, fixture_id: int, *, odds: tuple[float, float, float]
) -> None:
    """两本 legacy 欧赔书的完整三向快照（kickoff 前观测）。"""
    for book in ("odds_api:bet365", "odds_api:unibet"):
        for selection, price in zip(("h", "d", "a"), odds, strict=True):
            db.execute(
                "INSERT INTO odds_snapshots (fixture_id, market_code,"
                " selection_code, source, purpose, odds, captured_at,"
                " observed_at, created_at) VALUES (?, 'had', ?, ?,"
                " 'live_capture', ?, '2026-11-05T17:30:00+00:00',"
                " '2026-11-05T17:30:00+00:00', 'x')",
                (fixture_id, selection, book, price),
            )
    db.commit()


def test_clv_dual_anchor_alert_fires_on_divergent_fixture(
    db: sqlite3.Connection,
) -> None:
    """硬验收：同注两锚差超界（主锚 0.50 vs 欧共识 ~0.62）→一致性告警。"""
    bet_id, fid = _seed_anchored_leg(db, tag="x", close_prob=0.50)
    _seed_consensus_snapshots(db, fid, odds=(1.55, 4.6, 6.2))
    section = monthly_read.clv_dual_anchor(db, report_month="2026-11")
    assert section["n_legs_main"] == 1
    assert section["n_legs_both"] == 1
    assert section["coverage"] == 1.0
    assert len(section["alerts"]) == 1
    alert = section["alerts"][0]
    assert alert["bet_id"] == bet_id
    assert alert["p_main"] == pytest.approx(0.50, abs=1e-6)
    assert alert["p_consensus"] > 0.55  # 共识显著偏主胜
    assert alert["divergence"] > monthly_read.DUAL_ANCHOR_ALERT_BOUND
    assert section["max_divergence"] == alert["divergence"]


def test_clv_dual_anchor_within_bound_no_alert(db: sqlite3.Connection) -> None:
    """对照面：两锚一致（共识 ~0.49）不告警。"""
    _, fid = _seed_anchored_leg(db, tag="y", close_prob=0.50)
    _seed_consensus_snapshots(db, fid, odds=(1.95, 3.5, 4.2))
    section = monthly_read.clv_dual_anchor(db, report_month="2026-11")
    assert section["alerts"] == []
    assert section["median_divergence"] is not None
    assert section["median_divergence"] < monthly_read.DUAL_ANCHOR_ALERT_BOUND


def test_clv_dual_anchor_coverage_honest_without_secondary(
    db: sqlite3.Connection,
) -> None:
    """副锚历史面不足：覆盖率如实 0、不伪造绿也不静默丢人口。"""
    _seed_anchored_leg(db, tag="z", close_prob=0.50)
    section = monthly_read.clv_dual_anchor(db, report_month="2026-11")
    assert section["n_legs_main"] == 1
    assert section["n_legs_both"] == 0
    assert section["coverage"] == 0.0
    assert section["status"] == "no_secondary_coverage"
    assert section["alerts"] == []


def test_clv_dual_anchor_post_kickoff_snapshot_not_admitted(
    db: sqlite3.Connection,
) -> None:
    """PIT 纪律：kickoff 后才观测的快照不作副锚（审计面同对账准入）。"""
    _, fid = _seed_anchored_leg(db, tag="w", close_prob=0.50)
    for book in ("odds_api:bet365",):
        for selection, price in zip(("h", "d", "a"), (1.55, 4.6, 6.2), strict=True):
            db.execute(
                "INSERT INTO odds_snapshots (fixture_id, market_code,"
                " selection_code, source, purpose, odds, captured_at,"
                " observed_at, created_at) VALUES (?, 'had', ?, ?,"
                " 'live_capture', ?, '2026-11-05T19:30:00+00:00',"
                " '2026-11-05T19:30:00+00:00', 'x')",
                (fid, selection, book, price),
            )
    db.commit()
    section = monthly_read.clv_dual_anchor(db, report_month="2026-11")
    assert section["n_legs_both"] == 0
    assert section["coverage"] == 0.0


def test_clv_dual_anchor_window_excludes_other_months(
    db: sqlite3.Connection,
) -> None:
    """窗语义：报送月外结算的主锚腿不进人口（月读只报刚结束的自然月）。"""
    _seed_anchored_leg(db, tag="m1", close_prob=0.50)  # 11 月结算
    _seed_anchored_leg(
        db, tag="m2", close_prob=0.50, settled_at="2026-10-15T21:00:00+00:00"
    )
    section = monthly_read.clv_dual_anchor(db, report_month="2026-11")
    assert section["n_legs_main"] == 1


def test_clv_dual_anchor_empty_population_honest(db: sqlite3.Connection) -> None:
    """窗口内无已结算主锚注：空人口如实报（不伪造绿也不告警）。"""
    section = monthly_read.clv_dual_anchor(db, report_month="2026-11")
    assert section["n_legs_main"] == 0
    assert section["alerts"] == []
    assert section["status"] == "no_anchored_legs"


# ---- ③ champion/challenger 影子对比窗（票 07 未建=占位+消费接缝） ----


def test_shadow_compare_placeholder_with_forecast_face(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """③ 占位：无影子数据如实报；forecasts 版本标签读口（票 07 消费缝）带出。"""
    fid = _seed_fixture(db, tag="s", kickoff="2026-11-05T18:00:00+00:00")
    db.execute(
        "INSERT INTO forecasts (fixture_id, track, model_version, issued_at,"
        " content_hash, payload) VALUES (?, 'ml', 'dc-aaaa',"
        " '2026-11-05T10:00:00+00:00', 'h1', '{}')",
        (fid,),
    )
    db.execute(
        "INSERT INTO forecasts (fixture_id, track, model_version, issued_at,"
        " content_hash, payload) VALUES (?, 'ml', 'dc-bbbb',"
        " '2026-11-20T10:00:00+00:00', 'h2', '{}')",
        (fid,),
    )
    section = monthly_read.shadow_compare(db, report_month="2026-11")
    # 仅 ml 两版本（无影子轨行）仍=无影子数据：对比面要 challenger 行在
    assert section["status"] == "no_shadow_data"
    assert section["forecast_versions"] == {"dc-aaaa": 1, "dc-bbbb": 1}
    assert "票 07" in section["detail"]


def test_shadow_compare_ok_when_shadow_track_present(
    db: sqlite3.Connection,
) -> None:
    """③ 票 07 落地后：窗内 champion(ml)+challenger(shadow) 双版本在→
    status=ok（⑧预算影子开关随之为 on）；版本读口两版本行数可数。"""
    fid = _seed_fixture(db, tag="s", kickoff="2026-11-05T18:00:00+00:00")
    for track, version, hash_ in (
        ("ml", "dc-aaaa", "h1"),
        ("shadow", "shadow-dc-bbbb", "h2"),
    ):
        db.execute(
            "INSERT INTO forecasts (fixture_id, track, model_version, issued_at,"
            f" content_hash, payload) VALUES (?, '{track}', '{version}',"
            " '2026-11-05T10:00:00+00:00', ?, '{}')",
            (fid, hash_),
        )
    section = monthly_read.shadow_compare(db, report_month="2026-11")
    assert section["status"] == "ok"
    assert section["forecast_versions"] == {"dc-aaaa": 1, "shadow-dc-bbbb": 1}
    budget = monthly_read.budget_suggestion(
        {
            "llm_costs": {"total_cny": 10.0},
            "rejections": {"share": 0.0},
        },
        shadow_available=section["status"] == "ok",
    )
    assert budget["shadow_retrain"] == "on"


# ---- ④ 版本状态机事件流 ----


def test_version_events_classification(db: sqlite3.Connection) -> None:
    """④ 事件流回放：晋升/降级/触发器命中 reason 分类计数。"""
    vid = versioning.register_version(
        db,
        estimator_version="e1",
        calibration_version="c1",
        params_version="p1",
        label="auto-v1",
    )
    for state in (
        versioning.StrategyState.SHADOW,
        versioning.StrategyState.PROBE,
        versioning.StrategyState.CANDIDATE,
        versioning.StrategyState.LIVE_PAPER,
    ):
        versioning.transition_version(
            db, vid, state, actor="human:cli", reason="人审晋升"
        )
    versioning.transition_version(
        db,
        vid,
        versioning.StrategyState.CANDIDATE,
        actor="trigger:A",
        reason="clv global-window mean=-0.05 n=50",
    )
    section = monthly_read.version_events(db, report_month="2026-11")
    assert section["counts"]["registration"] == 1
    assert section["counts"]["promotion"] == 4
    assert section["counts"]["trigger:a"] == 1
    assert section["total_all_time"] == 6
    event = section["events"][0]
    assert event["label"] == "auto-v1"
    assert event["kind"] == "trigger:a"
    assert "clv global-window" in event["reason"]


# ---- ⑤ 强制失败视图（不可省略，User Story 8） ----


def test_forced_failure_view_aggregates(db: sqlite3.Connection, tmp_path: Path) -> None:
    """⑤ 四面：refuted 占比/引擎门挂 zero_note 分布/周批拒收计数（语料树
    拍文件聚合=唯一可靠源）/LLM 花费汇总。"""
    from goalx_backend.evaluation import hypothesis_registry as hr

    # H1-H8 种子：1 refuted / 8 总
    hr.transition_hypothesis(
        db, "H4", hr.HypothesisState.REFUTED, actor="human:cli", reason="判读"
    )
    db.executemany(
        "INSERT INTO auto_paper_runs (business_date, anchor_label, strategy,"
        " ran_at, bets, slips, zero_note, detail_json) VALUES (?, '12:30',"
        " 'auto-v1', ?, 0, 0, ?, '{}')",
        [
            ("2026-11-02", "2026-11-02T04:31:00+00:00", "no_candidates"),
            ("2026-11-03", "2026-11-03T04:31:00+00:00", "no_candidates"),
            ("2026-11-04", "2026-11-04T04:31:00+00:00", "gate_rejected"),
            ("2026-11-05", "2026-11-05T04:31:00+00:00", None),  # 有注日
        ],
    )
    db.execute("UPDATE auto_paper_runs SET bets = 2 WHERE business_date = '2026-11-05'")
    proposals = tmp_path / "corpus" / "reports" / "proposals"
    proposals.mkdir(parents=True)
    (proposals / "weekly-proposals-2026-11-16.json").write_text(
        json.dumps(
            {
                "schema": "weekly-proposals/1",
                "run_date": "2026-11-16",
                "status": "generated",
                "proposals": [{"id": "P-20261116-01"}],
                "rejected": [
                    {
                        "raw": {},
                        "reasons": ["引用 id 缺失（refs 必带，票 11 裁定管线层拒收）"],
                    },
                    {
                        "raw": {},
                        "reasons": [
                            "撞 refuted 库：该台账回指已有 refuted 假设"
                            "（伪新颖，重开=人裁新 h_id）"
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (proposals / "weekly-proposals-2026-11-09.json").write_text(
        json.dumps(
            {
                "schema": "weekly-proposals/1",
                "run_date": "2026-11-09",
                "status": "generated",
                "proposals": [],
                "rejected": [
                    {
                        "raw": {},
                        "reasons": ["引用 id 缺失（refs 必带，票 11 裁定管线层拒收）"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db.execute(
        "INSERT INTO llm_proposal_costs (proposal_id, phase, model,"
        " prompt_tokens, completion_tokens, cost_cny, ts) VALUES"
        " ('B-20261116', 'generate', 'glm-5.3', 100, 50, 0.0022, ?),"
        " ('P-20261116-01', 'critique', 'glm-5.3', 80, 40, 0.0011, ?),"
        " ('B-20261109', 'generate', 'glm-5.3', 100, 50, 0.0022, ?)",
        (
            "2026-11-16T06:00:10+00:00",
            "2026-11-16T06:00:20+00:00",
            "2026-11-09T06:00:10+00:00",
        ),
    )
    section = monthly_read.forced_failure(
        db, store=_store(tmp_path), report_month="2026-11"
    )
    assert section["experiment_failure"]["refuted"] == 1
    assert section["experiment_failure"]["total"] == 8
    assert section["experiment_failure"]["refuted_share"] == pytest.approx(0.125)
    assert section["engine_gate_hangs"] == {"no_candidates": 2, "gate_rejected": 1}
    assert section["rejections"]["n_beats"] == 2
    assert section["rejections"]["n_rejected"] == 3
    assert section["rejections"]["n_validated"] == 1
    assert section["rejections"]["share"] == pytest.approx(3 / 4)
    costs = section["llm_costs"]
    assert costs["n_calls"] == 3
    assert costs["total_cny"] == pytest.approx(0.0055)
    assert costs["by_phase"] == {"generate": 0.0044, "critique": 0.0011}
    # 影子重训触发面（票 07 落账→⑤消费）：窗内按 reason×outcome 计数
    assert section["shadow_retrains"] == {"total": 0}  # 本测试未种触发账


def test_forced_failure_includes_shadow_retrains_face(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """⑤ 影子重训触发面：**按 period=报送月**计数（与事件落账 period 同源）。

    F5 口径钉死：不按 ts 计窗——月首北京 0-8 点拍的 ts 仍在上一 UTC 日
    （如北京 12-01 07:00=UTC 11-30 23:00），ts 窗会把该拍计进 11 月报表；
    period=北京业务日月键，与 beat 落账同源恒对齐。"""
    db.execute(
        "INSERT INTO shadow_retrain_events (period, reason, outcome, detail, ts)"
        " VALUES ('2026-11', 'drift', 'registered', 'd',"
        " '2026-11-05T02:35:00+00:00')"
    )
    db.execute(
        "INSERT INTO shadow_retrain_events (period, reason, outcome, detail, ts)"
        " VALUES ('2026-11', 'monthly', 'no_live_champion', 'd',"
        " '2026-11-01T02:35:00+00:00')"
    )
    db.execute(
        "INSERT INTO shadow_retrain_events (period, reason, outcome, detail, ts)"
        " VALUES ('2026-10', 'monthly', 'registered', 'd',"
        " '2026-10-01T02:35:00+00:00')"
    )
    # F5 月界样本：period=2026-12（北京 12-01 晨拍）而 ts 落 UTC 11-30——
    # 11 月报表不得计入（ts 窗口径会把 11-30 23:00 错收进 [11-01,12-01)）
    db.execute(
        "INSERT INTO shadow_retrain_events (period, reason, outcome, detail, ts)"
        " VALUES ('2026-12', 'monthly', 'registered', 'd',"
        " '2026-11-30T23:00:00+00:00')"
    )
    section = monthly_read.forced_failure(
        db, store=_store(tmp_path), report_month="2026-11"
    )
    assert section["shadow_retrains"] == {
        "drift|registered": 1,
        "monthly|no_live_champion": 1,
        "total": 2,
    }


# ---- ⑥ 双钟读数（含票 08 缝 B.3） ----


def test_clocks_section_includes_b3_and_signals(db: sqlite3.Connection) -> None:
    """⑥ 双钟读数+四信号面+B.3 到期读数（票 08 缝回填进本节）。"""
    db.execute(
        "INSERT INTO contraction_transitions (level, actor, reason, ts)"
        " VALUES ('L1', 'human:cli', 'r', '2026-04-01T00:00:00+00:00'),"
        " ('L2', 'human:cli', 'r', '2026-04-02T00:00:00+00:00')"
    )
    section = monthly_read.clocks_section(db, now=_RUN_AT, report_month="2026-11")
    assert section["clv_main"]["clock"] == "clv_main"
    assert section["exploration"]["clock"] == "exploration"
    assert len(section["rescan_signals"]) == 5  # 四信号 + B.3 timer
    assert section["b3_timer"]["status"] == "due_suggest_downgrade"
    assert section["b3_timer"]["suggest_to"] == "L3"
    # 双钟读数已落账（contraction-read 同款写口）
    assert (
        db.execute("SELECT COUNT(*) c FROM contraction_clock_readings").fetchone()["c"]
        == 2
    )


# ---- ⑦ 教训库视图（票 05 读口③） ----


def test_lessons_view_section(db: sqlite3.Connection, tmp_path: Path) -> None:
    """⑦ 月读视图：新入库（上个自然月 created）+ 复审到期两清单。"""

    def _lesson(lesson_id: str, created: str, review_due: str | None) -> None:
        due = review_due or "null"
        (tmp_path / f"{lesson_id}.md").write_text(
            f"""---
id: {lesson_id}
status: active
confidence: 高
created: {created}
review_due: {due}
window: null
cause: 一句话死因。
triggers:
  - 场景甲
evidence:
  - edge-ledger E-04
---

正文。
""",
            encoding="utf-8",
        )

    _lesson("L-01", "2026-11-09", "2027-11-09")  # 新入库（11 月）
    _lesson("L-02", "2025-10-01", "2026-11-15")  # 复审到期（≤ 12-01）
    _lesson("L-03", "2025-10-01", "2027-10-01")  # 都不是
    section = monthly_read.lessons_section(tmp_path, today=date(2026, 12, 1))
    assert [lesson["id"] for lesson in section["new_entries"]] == ["L-01"]
    assert [lesson["id"] for lesson in section["review_due_entries"]] == ["L-02"]


def test_lessons_view_absent_dir_honest(db: sqlite3.Connection, tmp_path: Path) -> None:
    """教训库目录缺席：如实降级（不炸月读、不伪造清单）。"""
    section = monthly_read.lessons_section(tmp_path / "nope", today=date(2026, 12, 1))
    assert section["status"] == "dir_absent"


# ---- ⑧ 下月预算定拍建议（规则写死可复算） ----


def test_budget_suggestion_rules_recomputable() -> None:
    """⑧ 纯建议性数字从⑤推导：上限=max(1.5×花费, 20)；拒收率>0.5→配额降 3；
    影子重训开关=③无数据即 off。"""
    failure = {
        "llm_costs": {"total_cny": 30.0},
        "rejections": {"share": 0.75},
    }
    section = monthly_read.budget_suggestion(failure, shadow_available=False)
    assert section["cost_cap_cny"] == 45.0
    assert section["quota_per_batch"] == 3
    assert section["batches_next_month"] == 4
    assert section["shadow_retrain"] == "off"
    assert section["rules"]  # 规则文本随报告（可复算说明）
    # 低拒收率 + 零花费：配额满额、下限兜底
    section2 = monthly_read.budget_suggestion(
        {"llm_costs": {"total_cny": 0.0}, "rejections": {"share": 0.0}},
        shadow_available=False,
    )
    assert section2["cost_cap_cny"] == 20.0
    assert section2["quota_per_batch"] == 5


# ---- 报告只读红线：不写任何决策状态 ----


def test_report_readonly_red_line(db: sqlite3.Connection, tmp_path: Path) -> None:
    """红线：月读不写 transitions/假设状态/级别（决策面零写入；信号账与
    双钟读数账=观测面允许）。"""
    monthly_read.run_monthly_read(
        db, store=_store(tmp_path), now=_RUN_AT, lessons_dir=tmp_path
    )
    assert db.execute("SELECT COUNT(*) c FROM version_transitions").fetchone()["c"] == 0
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM hypothesis_registry WHERE status NOT IN"
            " ('registered', 'running')"
        ).fetchone()["c"]
        == 0
    )
    assert (
        db.execute("SELECT COUNT(*) c FROM contraction_transitions").fetchone()["c"]
        == 1  # v52 genesis 行原样
    )


# ---- correctness-review 回归（F1 家族总尝试 / F2 limit 死参） ----


def test_gate_progress_family_attempts_from_ledger_count(
    db: sqlite3.Connection,
) -> None:
    """F1 回归：同族两非终态行各自 record_attempt 时，家族 m=账面
    hypothesis_attempts COUNT（票 02 总尝试口径），非 MAX(attempt_count)
    （同族双活行各自计数时 MAX 低估——DSR/BH 分母反保守失真）。"""
    from goalx_backend.evaluation import hypothesis_registry as hr

    for i in range(3):
        hr.record_attempt(db, "H1", window_key=f"s1:a{i}")  # H1 计数 3
    hr.register_hypothesis(  # 家族复试新行（继承族史快照 attempt_count=3）
        db,
        h_id="H1B",
        family="H1",
        statement="复试措辞",
        judgement="j",
        data_face="d",
        expected_n="n",
        ledger_ref="edge-ledger:E-02",
        actor="human:cli",
    )
    for i in range(2):
        hr.record_attempt(db, "H1", window_key=f"s1:b{i}")  # 旧行继续 → 5
        hr.record_attempt(db, "H1B", window_key=f"s1:c{i}")  # 新行 → 5
    section = monthly_read.gate_progress(db)
    family = next(f for f in section["families"] if f["family"] == "H1")
    assert family["m_attempts"] == 7  # 账面 COUNT=3+2+2；MAX(attempt_count)=5
    assert family["n_rows"] == 2


def test_transition_events_limit_honored_in_sql(db: sqlite3.Connection) -> None:
    """F2 回归：limit 走 SQL LIMIT（最新 N 行）；缺省 None=全表（月读的
    窗内计数与全史总数需要全量回放，显示清单在调用侧截断）。"""
    vid = versioning.register_version(
        db,
        estimator_version="e2",
        calibration_version="c2",
        params_version="p2",
        label="auto-v2",
    )
    versioning.transition_version(
        db, vid, versioning.StrategyState.SHADOW, actor="human:cli", reason="一步"
    )
    versioning.transition_version(
        db, vid, versioning.StrategyState.PROBE, actor="human:cli", reason="二步"
    )
    limited = versioning.transition_events(db, limit=1)
    assert len(limited) == 1
    assert limited[0].kind == "promotion"
    assert limited[0].reason == "二步"  # 时间倒序=最新
    assert len(versioning.transition_events(db)) == 3  # 缺省全量（genesis+2）
