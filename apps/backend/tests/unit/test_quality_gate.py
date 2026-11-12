"""vision-build 票 11：challenger 质量门（历史回放窗）+ 连续失败告警。

管线级钉住：v58 迁移（冻结面+门账两表 append-only）、prereg 冻结纪律
（注册时冻结指标面，首写恒胜）、RPS 配对评估器（恰劣于/不劣于显著水平
两侧边界）、promote 硬门（shadow→probe 需最新门评估通过记录）、月读⑤
门挂分布消费缝、连续 N=3 门挂告警（晨检+月读）。
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from statistics import NormalDist

import pytest

from goalx_backend.betting import versioning
from goalx_backend.betting.versioning import StrategyState
from goalx_backend.data.leagues import ERA_TRAJECTORY
from goalx_backend.evaluation import quality_gate

_SPEC_FROZEN_AT = "2026-11-11T02:00:00+00:00"


# ---- v58 迁移：冻结面 + 门账（append-only 两表） ----


def test_migration_v58_idempotent(db: sqlite3.Connection) -> None:
    """v58 幂等：两表就位、重跑零效果。"""
    from goalx_backend.db import migrate

    assert migrate(db) == 61
    assert migrate(db) == 61  # 同版本重跑零效果
    for table in ("quality_gate_specs", "quality_gate_events"):
        assert (
            db.execute(
                "SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()["c"]
            == 1
        )


def test_migration_v59_adds_coverage_columns_idempotent(
    db: sqlite3.Connection,
) -> None:
    """F4/v59：门账覆盖率三列就位（可空）；重跑零效果。"""
    from goalx_backend.db import migrate

    assert migrate(db) == 61
    columns = {
        str(row["name"]) for row in db.execute("PRAGMA table_info(quality_gate_events)")
    }
    assert {"n_champion_window", "n_challenger_window", "paired_ratio"} <= columns
    assert migrate(db) == 61  # ALTER 幂等守卫重入零效果


def test_gate_tables_are_append_only(db: sqlite3.Connection) -> None:
    """冻结面与门账 append-only：UPDATE/DELETE 库层拒绝（v49/v57 触发器先例）。

    prereg 纪律的库层表达：冻结指标面事后不可改写。"""
    version_id = versioning.register_version(
        db,
        estimator_version="dc-gate",
        calibration_version="shin",
        params_version="fp-gate",
        label="challenger",
    )
    db.execute(
        """
        INSERT INTO quality_gate_specs
            (challenger_version_id, metric_name, alpha, min_paired, window_era,
             frozen_at)
        VALUES (?, 'rps', 0.05, 200, 'trajectory', '2026-11-11T00:00:00+00:00')
        """,
        (version_id,),
    )
    db.execute(
        """
        INSERT INTO quality_gate_events
            (challenger_version_id, metric_name, champion_run_id, challenger_run_id,
             window_era, n_paired, passed, hang_reason, detail, ts)
        VALUES (?, 'rps', 1, 2, 'trajectory', 0, 0, 'insufficient_pairs',
                '演练', '2026-11-11T00:00:00+00:00')
        """,
        (version_id,),
    )
    for table, column in (
        ("quality_gate_specs", "frozen_at"),
        ("quality_gate_events", "hang_reason"),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(
                f"UPDATE {table} SET {column} = 'x' WHERE challenger_version_id = ?",
                (version_id,),
            )


# ---- prereg 冻结：注册时冻结指标名+阈值（首写恒胜，不得事后改） ----


def test_freeze_gate_spec_is_prereg(db: sqlite3.Connection) -> None:
    """冻结幂等：同版本一行、常数面=代码常数；重复冻结（首写恒胜）零新行。"""
    version_id = versioning.register_version(
        db,
        estimator_version="dc-freeze",
        calibration_version="shin",
        params_version="fp",
        label="challenger",
    )
    quality_gate.freeze_gate_spec(
        db, version_id, newly_registered=True, frozen_at=_SPEC_FROZEN_AT
    )
    quality_gate.freeze_gate_spec(
        db, version_id, newly_registered=True, frozen_at="2026-12-01T00:00:00+00:00"
    )  # 重复冻结：首写恒胜
    spec = quality_gate.frozen_spec(db, version_id)
    assert spec is not None
    assert spec.metric_name == quality_gate.GATE_METRIC_NAME
    assert spec.alpha == quality_gate.GATE_ALPHA
    assert spec.min_paired == quality_gate.GATE_MIN_PAIRED
    assert spec.window_era == ERA_TRAJECTORY  # 评估窗=现代窗留出（时代真相源）
    assert spec.frozen_at == _SPEC_FROZEN_AT  # 后到冻结不改写首冻结时间
    assert (
        db.execute("SELECT COUNT(*) c FROM quality_gate_specs", ()).fetchone()["c"] == 1
    )
    assert quality_gate.frozen_spec(db, version_id + 999) is None


# ---- 门谓词：恰劣于/不劣于显著水平两侧（验收：边界数值单测） ----


def test_challenger_worse_significant_boundary() -> None:
    """恰在临界值（dm=z_{1−α}）不挂（严格 >，p=α 恰界不劣于）；越过即挂。"""
    critical = NormalDist().inv_cdf(1.0 - quality_gate.GATE_ALPHA)
    # 不劣于侧：dm=临界值 → 单侧 p 恰=α → 严格 < 不成立 → 不显著
    assert quality_gate.challenger_worse_significant(critical) is False
    # 恰劣于侧：临界值上一位最小浮点 → p<α → 显著劣于
    assert quality_gate.challenger_worse_significant(math.nextafter(critical, 1e9))
    # dm<0（challenger 更优方向）恒不挂
    assert quality_gate.challenger_worse_significant(-critical) is False


# ---- 回放窗配对面：研究库 backtest_predictions（十年回放先例，只读） ----

_OUTCOME_CYCLE = ("H", "D", "A")


def _research_conn() -> sqlite3.Connection:
    """研究库脚手架（:memory: + migrate_research，票 07② 分库先例）。"""
    from goalx_backend.db import migrate_research

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate_research(conn)
    return conn


def _seed_run(research: sqlite3.Connection, run_id: int, label: str) -> None:
    research.execute(
        "INSERT INTO backtest_runs (id, label, params, status, created_at)"
        " VALUES (?, ?, '{}', 'done', '2026-11-11T00:00:00+00:00')",
        (run_id, label),
    )


def _probs_for(ftr: str, *, favor_offset: int = 0) -> dict[str, float]:
    """确定性三向：实际结果向 0.5 集中，favor_offset≠0 转而错向相邻选向。"""
    order = _OUTCOME_CYCLE.index(ftr)
    favored = _OUTCOME_CYCLE[(order + favor_offset) % 3]
    return {sel.lower(): (0.5 if sel == favored else 0.25) for sel in _OUTCOME_CYCLE}


def _seed_predictions(
    research: sqlite3.Connection,
    *,
    run_id: int,
    n: int,
    favor_offset: int = 0,
    era: str = ERA_TRAJECTORY,
) -> None:
    """n 行 walk-forward 预测（确定性：ftr 循环 H/D/A，概率向结果集中）。"""
    for i in range(n):
        ftr = _OUTCOME_CYCLE[i % 3]
        research.execute(
            """
            INSERT INTO backtest_predictions
                (run_id, match_key, competition, season, match_date, home_team,
                 away_team, era, ftr, had_probs, fair_probs, fair_source,
                 model_fingerprint, train_window_end)
            VALUES (?, ?, 'E0', '2526', '2025-09-01', 'h', 'a', ?, ?, ?, ?,
                    'traj_anchor', 'fp', '2025-08-31')
            """,
            (
                run_id,
                f"sid-{i:04d}",
                era,
                ftr,
                json.dumps(_probs_for(ftr, favor_offset=favor_offset)),
                json.dumps({"h": 0.4, "d": 0.3, "a": 0.3}),
            ),
        )


def _challenger_with_spec(db: sqlite3.Connection) -> int:
    """注册 challenger 版本并冻结门指标面（评估前置）。"""
    version_id = versioning.register_version(
        db,
        estimator_version="dc-gate-challenger",
        calibration_version="shin",
        params_version="fp",
        label="shadow-challenger-2026-11",
        initial_state=StrategyState.SHADOW,
    )
    quality_gate.freeze_gate_spec(
        db, version_id, newly_registered=True, frozen_at=_SPEC_FROZEN_AT
    )
    return version_id


def test_paired_replay_faces_pairs_window_and_filters(
    db: sqlite3.Connection,
) -> None:
    """配对面读口：按 match_key 交集配对；era 过滤=评估窗（现代窗留出）；
    ftr NULL 行（v25 前旧 run）不进配对面。"""
    research = _research_conn()
    try:
        _seed_run(research, 1, "champion")
        _seed_run(research, 2, "challenger")
        _seed_predictions(research, run_id=1, n=3)  # sid-0000..0002
        _seed_predictions(research, run_id=2, n=2)  # 交集=sid-0000..0001
        # 窗外行（开发窗 era）与无赛果行不进配对面
        research.execute(
            """
            INSERT INTO backtest_predictions
                (run_id, match_key, competition, season, match_date, home_team,
                 away_team, era, ftr, had_probs, fair_probs, fair_source,
                 model_fingerprint, train_window_end)
            VALUES (1, 'sid-9999', 'E0', '2223', '2023-01-01', 'h', 'a',
                    'psc_proxy', 'H', '{}', '{}', 'psc', 'fp', '2022-12-31')
            """
        )
        # sid-0000 置 ftr=NULL（v25 前旧 run 形态）——不进配对面
        research.execute(
            "UPDATE backtest_predictions SET ftr = NULL WHERE run_id=1"
            " AND match_key='sid-0000'"
        )
        pairing = quality_gate.paired_replay_faces(
            research,
            champion_run_id=1,
            challenger_run_id=2,
            window_era=ERA_TRAJECTORY,
        )
        faces = pairing.faces
        assert [f.match_key for f in faces] == ["sid-0001"]
        assert faces[0].ftr == "D"
        assert faces[0].champion_probs == _probs_for("D")
        assert faces[0].challenger_probs == _probs_for("D")
        # F4 行数面：ftr NULL 行不进窗行数；覆盖率=配对/max 窗内行数
        assert pairing.n_champion_window == 2
        assert pairing.n_challenger_window == 2
        assert pairing.paired_ratio == pytest.approx(0.5)
    finally:
        research.close()


def test_evaluate_gate_passes_identical_challenger(
    db: sqlite3.Connection,
) -> None:
    """challenger 与 champion 逐场同概率（不劣于）：零差 → dm 退化 0 → 过门；
    门账落 passed=1 行，数字可复算（diff=0）。"""
    research = _research_conn()
    try:
        _seed_run(research, 1, "champion")
        _seed_run(research, 2, "challenger")
        _seed_predictions(research, run_id=1, n=quality_gate.GATE_MIN_PAIRED)
        _seed_predictions(research, run_id=2, n=quality_gate.GATE_MIN_PAIRED)
        challenger_id = _challenger_with_spec(db)
        evaluation = quality_gate.evaluate_quality_gate(
            research,
            db,
            challenger_version_id=challenger_id,
            champion_run_id=1,
            challenger_run_id=2,
        )
        assert evaluation.passed is True
        assert evaluation.hang_reason == ""
        assert evaluation.n_paired == quality_gate.GATE_MIN_PAIRED
        assert evaluation.diff == pytest.approx(0.0)
        assert evaluation.metric_name == "rps"
        row = db.execute(
            "SELECT passed, hang_reason, n_paired FROM quality_gate_events"
        ).fetchone()
        assert row["passed"] == 1
        assert row["n_paired"] == quality_gate.GATE_MIN_PAIRED
        # 同 run 对重跑：UNIQUE 吸收零新行
        quality_gate.evaluate_quality_gate(
            research,
            db,
            challenger_version_id=challenger_id,
            champion_run_id=1,
            challenger_run_id=2,
        )
        assert (
            db.execute("SELECT COUNT(*) c FROM quality_gate_events").fetchone()["c"]
            == 1
        )
    finally:
        research.close()


def test_evaluate_gate_hangs_on_systematically_worse_challenger(
    db: sqlite3.Connection,
) -> None:
    """challenger 系统性错向（相邻选向集中）：配对 RPS 差显著劣于 → 门挂，
    reason=指标名+significantly_worse，单侧 p<α。"""
    research = _research_conn()
    try:
        _seed_run(research, 1, "champion")
        _seed_run(research, 2, "challenger")
        _seed_predictions(research, run_id=1, n=quality_gate.GATE_MIN_PAIRED)
        _seed_predictions(
            research, run_id=2, n=quality_gate.GATE_MIN_PAIRED, favor_offset=1
        )
        challenger_id = _challenger_with_spec(db)
        evaluation = quality_gate.evaluate_quality_gate(
            research,
            db,
            challenger_version_id=challenger_id,
            champion_run_id=1,
            challenger_run_id=2,
        )
        assert evaluation.passed is False
        assert evaluation.hang_reason == "rps_significantly_worse"
        assert evaluation.metric_challenger > evaluation.metric_champion
        assert evaluation.p_one_sided is not None
        assert evaluation.p_one_sided < quality_gate.GATE_ALPHA
        assert evaluation.dm_stat is not None
        assert evaluation.dm_stat > 0
        row = db.execute(
            "SELECT passed, hang_reason FROM quality_gate_events"
        ).fetchone()
        assert row["passed"] == 0
        assert row["hang_reason"] == "rps_significantly_worse"
    finally:
        research.close()


def test_evaluate_gate_insufficient_pairs_records_hang(
    db: sqlite3.Connection,
) -> None:
    """配对样本不足冻结 min_paired：insufficient_pairs 门挂（数字面 NULL
    如实不伪造），不作显著性判定。"""
    research = _research_conn()
    try:
        _seed_run(research, 1, "champion")
        _seed_run(research, 2, "challenger")
        _seed_predictions(research, run_id=1, n=10)
        _seed_predictions(research, run_id=2, n=10)
        challenger_id = _challenger_with_spec(db)
        evaluation = quality_gate.evaluate_quality_gate(
            research,
            db,
            challenger_version_id=challenger_id,
            champion_run_id=1,
            challenger_run_id=2,
        )
        assert evaluation.passed is False
        assert evaluation.hang_reason == "insufficient_pairs"
        assert evaluation.n_paired == 10
        assert evaluation.metric_champion is None
        assert evaluation.dm_stat is None
    finally:
        research.close()


def test_evaluate_gate_requires_frozen_spec(db: sqlite3.Connection) -> None:
    """prereg 前置：无冻结面不评估（fail-loud），零门账行。"""
    research = _research_conn()
    try:
        _seed_run(research, 1, "champion")
        _seed_run(research, 2, "challenger")
        bare = versioning.register_version(
            db,
            estimator_version="dc-bare",
            calibration_version="shin",
            params_version="fp",
            label="bare",
        )
        with pytest.raises(LookupError, match="冻结"):
            quality_gate.evaluate_quality_gate(
                research,
                db,
                challenger_version_id=bare,
                champion_run_id=1,
                challenger_run_id=2,
            )
        assert (
            db.execute("SELECT COUNT(*) c FROM quality_gate_events").fetchone()["c"]
            == 0
        )
    finally:
        research.close()


# ---- promote 硬门：shadow→probe 需最新门评估通过记录（拒绝留痕） ----


def _seed_gate_event(
    db: sqlite3.Connection,
    challenger_id: int,
    *,
    passed: bool,
    hang_reason: str,
    ts: str,
    runs: tuple[int, int] = (1, 2),
) -> None:
    """直接落一行门账（append-only 表，INSERT 直写=评估器同产物形状）。"""
    db.execute(
        """
        INSERT INTO quality_gate_events
            (challenger_version_id, metric_name, champion_run_id, challenger_run_id,
             window_era, n_paired, passed, hang_reason, detail, ts)
        VALUES (?, 'rps', ?, ?, 'trajectory', 0, ?, ?, '演练', ?)
        """,
        (challenger_id, runs[0], runs[1], int(passed), hang_reason, ts),
    )
    db.commit()  # CLI 测试开独立连接——未提交行对另一连接不可见


def _establish_ladder(db: sqlite3.Connection) -> int:
    """梯级已建立标记：一个早先到过 probe 的版本（豁免关闭前提）。"""
    prior_id = versioning.register_version(
        db,
        estimator_version="dc-prior",
        calibration_version="shin",
        params_version="fp-prior",
        label="prior-live-version",
        initial_state=StrategyState.LIVE_PAPER,
    )
    db.commit()
    return prior_id


def test_require_gate_pass_latest_must_pass(db: sqlite3.Connection) -> None:
    """硬门语义：无记录/最新挂/先过后挂（最新非过）皆拒；最新过门放行。
    拒绝=raise（promote CLI fail-loud 留痕——门账即证据面）。"""
    challenger_id = _challenger_with_spec(db)
    _establish_ladder(db)  # 梯级已建立：豁免关闭，硬门全量生效
    with pytest.raises(quality_gate.GateNotPassed, match="无门评估记录"):
        quality_gate.require_gate_pass(db, challenger_id)
    _seed_gate_event(
        db,
        challenger_id,
        passed=False,
        hang_reason="rps_significantly_worse",
        ts="2026-11-01T00:00:00+00:00",
    )
    with pytest.raises(quality_gate.GateNotPassed, match="rps_significantly_worse"):
        quality_gate.require_gate_pass(db, challenger_id)
    # 先过（runs=3,4）后挂（runs=1,2）：最新一行非过门 → 拒（过门记录
    # 不因更新一次挂的复评而「粘滞」放行）
    _seed_gate_event(
        db,
        challenger_id,
        passed=True,
        hang_reason="",
        ts="2026-11-02T00:00:00+00:00",
        runs=(3, 4),
    )
    _seed_gate_event(
        db,
        challenger_id,
        passed=False,
        hang_reason="insufficient_pairs",
        ts="2026-11-03T00:00:00+00:00",
        runs=(5, 6),
    )
    with pytest.raises(quality_gate.GateNotPassed, match="insufficient_pairs"):
        quality_gate.require_gate_pass(db, challenger_id)
    # 最新过门 → 放行（幂等读，无返回面=零异常即过）
    _seed_gate_event(
        db,
        challenger_id,
        passed=True,
        hang_reason="",
        ts="2026-11-04T00:00:00+00:00",
        runs=(7, 8),
    )
    quality_gate.require_gate_pass(db, challenger_id)


def test_cli_promote_hard_gate_on_shadow_to_probe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """人 CLI 晋升路径接线：注册即冻结门指标面；shadow→probe 查门账——
    无过门记录=拒绝（fail-loud 留痕，零 transitions 写入）；过门后放行；
    registered→shadow 晋升不受门（门只作用于 shadow→probe 出梯口）。"""
    import argparse

    from goalx_backend.cli import _cmd_version_promote, _cmd_version_register
    from goalx_backend.config import Settings
    from goalx_backend.db import connect as db_connect
    from goalx_backend.db import migrate

    settings = Settings(db_path=tmp_path / "goalx.db")
    conn = db_connect(settings.db_path)
    try:
        migrate(conn)
        _cmd_version_register(
            argparse.Namespace(
                estimator="dc-gate-cli",
                calibration="shin",
                params="fp-cli",
                label="challenger",
            ),
            settings=settings,
        )
        capsys.readouterr()
        # 注册即冻结（prereg 纪律的 CLI 落点）
        assert quality_gate.frozen_spec(conn, 1) is not None
        # registered→shadow：不受门（非出梯口）
        _cmd_version_promote(
            argparse.Namespace(id=1, to="shadow", reason="影子起跑"),
            settings=settings,
        )
        capsys.readouterr()
        # 现役 live 席位在场（豁免关闭前提）：另立一个在位版本，
        # 不动被测版本自身梯位
        prior = versioning.register_version(
            conn,
            estimator_version="dc-prior",
            calibration_version="shin",
            params_version="fp-prior",
            label="prior-live-version",
            initial_state=StrategyState.LIVE_PAPER,
        )
        assert prior > 0
        conn.commit()
        # shadow→probe：无过门记录=拒绝留痕（零 transitions 写入）
        with pytest.raises(quality_gate.GateNotPassed, match="无门评估记录"):
            _cmd_version_promote(
                argparse.Namespace(id=1, to="probe", reason="硬门演练"),
                settings=settings,
            )
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM version_transitions WHERE version_id=1"
            ).fetchone()["c"]
            == 2  # genesis + shadow
        )
        # 过门记录就位 → 放行
        _seed_gate_event(
            conn, 1, passed=True, hang_reason="", ts="2026-11-11T00:00:00+00:00"
        )
        _cmd_version_promote(
            argparse.Namespace(id=1, to="probe", reason="过门晋升"),
            settings=settings,
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["to_state"] == "probe"
        assert versioning.current_state(conn, 1) is StrategyState.PROBE
    finally:
        conn.close()


# ---- 连续失败告警：N=3 触发（晨检挂项 + 月读失败视图） ----


def test_consecutive_gate_failures_counts_and_resets(db: sqlite3.Connection) -> None:
    """连续门挂计数（F5 版本迭代序口径）：同版本连续挂折叠 1 段；跨版本
    递增；过门一行即断。"""
    challenger_a = _challenger_with_spec(db)
    challenger_b = versioning.register_version(
        db,
        estimator_version="dc-gate-challenger-b",
        calibration_version="shin",
        params_version="fp",
        label="shadow-challenger-b",
    )
    assert quality_gate.consecutive_gate_failures(db) == 0  # 零事件
    _seed_gate_event(
        db,
        challenger_a,
        passed=True,
        hang_reason="",
        ts="2026-11-01T00:00:00+00:00",
    )
    assert quality_gate.consecutive_gate_failures(db) == 0  # 最新=过门
    for owner, reason, ts, runs in (
        (challenger_a, "rps_significantly_worse", "2026-11-02T00:00:00+00:00", (1, 10)),
        (challenger_a, "insufficient_pairs", "2026-11-03T00:00:00+00:00", (1, 11)),
    ):
        _seed_gate_event(db, owner, passed=False, hang_reason=reason, ts=ts, runs=runs)
    assert quality_gate.consecutive_gate_failures(db) == 1  # 同版本两挂=1 段
    _seed_gate_event(
        db,
        challenger_b,
        passed=False,
        hang_reason="rps_significantly_worse",
        ts="2026-11-04T00:00:00+00:00",
        runs=(1, 12),
    )
    assert quality_gate.consecutive_gate_failures(db) == 2  # 跨版本挂=2 迭代
    _seed_gate_event(
        db,
        challenger_b,
        passed=True,
        hang_reason="",
        ts="2026-11-05T00:00:00+00:00",
        runs=(1, 20),
    )
    assert quality_gate.consecutive_gate_failures(db) == 0  # 过门即断


def test_gate_failure_face_monthly_window(db: sqlite3.Connection) -> None:
    """月读⑤消费面：ts 窗内评估计数/过挂分布/连续失败与告警旗标。"""
    challenger_id = _challenger_with_spec(db)
    _seed_gate_event(
        db,
        challenger_id,
        passed=True,
        hang_reason="",
        ts="2026-10-31T00:00:00+00:00",
        runs=(1, 13),
    )  # 窗外（10 月）不进 11 月计数面；且先落=早 id（连续段读数从最新 id 倒数）
    # F5 口径：三挂来自三个 challenger 版本迭代（同版本只算 1 段）
    version_ids = [challenger_id]
    for tag in ("v2", "v3"):
        version_ids.append(
            versioning.register_version(
                db,
                estimator_version=f"dc-gate-face-{tag}",
                calibration_version="shin",
                params_version="fp",
                label=f"shadow-face-{tag}",
            )
        )
    for owner, reason, ts, runs in (
        (
            version_ids[0],
            "rps_significantly_worse",
            "2026-11-02T00:00:00+00:00",
            (1, 11),
        ),
        (version_ids[1], "insufficient_pairs", "2026-11-03T00:00:00+00:00", (1, 12)),
        (
            version_ids[2],
            "rps_significantly_worse",
            "2026-11-04T00:00:00+00:00",
            (1, 14),
        ),
    ):
        _seed_gate_event(db, owner, passed=False, hang_reason=reason, ts=ts, runs=runs)
    face = quality_gate.gate_failure_face(
        db, ts_from="2026-11-01T00:00:00+00:00", ts_to="2026-12-01T00:00:00+00:00"
    )
    assert face["n_evaluations"] == 3
    assert face["n_passed"] == 0
    assert face["hangs_by_reason"] == {
        "rps_significantly_worse": 2,
        "insufficient_pairs": 1,
    }
    assert face["consecutive_failures"] == 3  # 3 个版本迭代连续挂（F5 口径）
    assert face["alert"] is True  # 连续 ≥N=3 → 告警
    assert face["consecutive_fail_alert_n"] == quality_gate.GATE_CONSECUTIVE_FAIL_N


def test_monthly_forced_failure_includes_gate_face(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """⑤ 接线：forced_failure 带 quality_gate 面（票 04 ⑤消费缝）。"""
    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import monthly_read

    store = CorpusStore(tmp_path / "corpus")
    challenger_id = _challenger_with_spec(db)
    _seed_gate_event(
        db,
        challenger_id,
        passed=False,
        hang_reason="rps_significantly_worse",
        ts="2026-11-02T00:00:00+00:00",
    )
    section = monthly_read.forced_failure(db, store=store, report_month="2026-11")
    assert section["quality_gate"]["n_evaluations"] == 1
    assert section["quality_gate"]["hangs_by_reason"] == {"rps_significantly_worse": 1}
    assert section["quality_gate"]["consecutive_failures"] == 1
    assert section["quality_gate"]["alert"] is False  # 1 < N=3 不告警


def test_morning_check_quality_gate_item(db: sqlite3.Connection) -> None:
    """晨检 26 项：连续迭代 ≥N=3 → FAIL（挂项告警）；1..N−1 → DEGRADED；
    零事件/最新过门 → OK；F5 口径=同版本多挂仍 1 迭代不红。"""
    from goalx_backend import morning_check as mc

    ok = mc.check_quality_gate(db)
    assert ok.status == mc.OK  # 零事件=门未跑过（观察面，绿）
    challenger_id = _challenger_with_spec(db)
    for reason, ts, runs in (
        ("rps_significantly_worse", "2026-11-02T00:00:00+00:00", (1, 11)),
        ("insufficient_pairs", "2026-11-03T00:00:00+00:00", (1, 12)),
        ("rps_significantly_worse", "2026-11-04T00:00:00+00:00", (1, 14)),
    ):
        _seed_gate_event(
            db, challenger_id, passed=False, hang_reason=reason, ts=ts, runs=runs
        )
    # F5：同版本三 run 对挂=1 个失败迭代——黄不红
    same_version = mc.check_quality_gate(db)
    assert same_version.status == mc.DEGRADED
    # 跨版本连续挂到 3 个迭代 → 红（挂项告警）
    for tag, ts, runs in (
        ("b", "2026-11-05T00:00:00+00:00", (1, 15)),
        ("c", "2026-11-06T00:00:00+00:00", (1, 16)),
    ):
        version_id = versioning.register_version(
            db,
            estimator_version=f"dc-gate-mc-{tag}",
            calibration_version="shin",
            params_version="fp",
            label=f"shadow-mc-{tag}",
        )
        _seed_gate_event(
            db,
            version_id,
            passed=False,
            hang_reason="rps_significantly_worse",
            ts=ts,
            runs=runs,
        )
    failed = mc.check_quality_gate(db)
    assert failed.status == mc.FAIL  # 3 个 challenger 迭代连续门挂 → 红告警
    assert "3" in failed.detail


# ---- 报告落盘：champion vs challenger 校准对比（可复算） ----


def test_write_report_files_double_view(tmp_path: Path) -> None:
    """报告落语料树 reports/（JSON+MD 双视图，temp+rename 原子写）：JSON 载荷
    含复算要素（窗/run 对/指标双侧/dm/p/判定/ECE 观察面）。"""
    from goalx_backend.data.corpus_store import CorpusStore

    store = CorpusStore(tmp_path / "corpus")
    # 直接构造评估产物（报告面单测：数字来源=评估器，这里只钉落盘形状）
    spec = quality_gate.GateSpec(
        challenger_version_id=7,
        metric_name=quality_gate.GATE_METRIC_NAME,
        alpha=quality_gate.GATE_ALPHA,
        min_paired=quality_gate.GATE_MIN_PAIRED,
        window_era=quality_gate.GATE_WINDOW_ERA,
        frozen_at=_SPEC_FROZEN_AT,
    )
    research = _research_conn()
    try:
        _seed_run(research, 1, "champion")
        _seed_run(research, 2, "challenger")
        _seed_predictions(research, run_id=1, n=quality_gate.GATE_MIN_PAIRED)
        _seed_predictions(research, run_id=2, n=quality_gate.GATE_MIN_PAIRED)
        evaluation = quality_gate.compute_gate_evaluation(
            research,
            spec,
            champion_run_id=1,
            challenger_run_id=2,
            champion_version_id=3,
        )
    finally:
        research.close()
    json_path, md_path = quality_gate.write_report(store, evaluation)
    assert json_path.exists()
    assert md_path.exists()
    assert json_path.name == "quality-gate.json"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["challenger_version_id"] == 7
    assert payload["champion_run_id"] == 1
    assert payload["challenger_run_id"] == 2
    assert payload["window_era"] == quality_gate.GATE_WINDOW_ERA
    assert payload["metric_name"] == "rps"
    assert payload["passed"] is True
    assert payload["n_paired"] == quality_gate.GATE_MIN_PAIRED
    assert payload["ece_challenger"]["h"] >= 0.0  # ECE 观察面随行
    md = md_path.read_text(encoding="utf-8")
    assert "champion vs challenger" in md
    assert "过门" in md


def test_cli_quality_gate_evaluate_runs_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 主链：评估→落账→报告落语料树 reports/；stdout JSON 带文件路径。"""
    import argparse

    from goalx_backend.cli import _cmd_quality_gate_evaluate
    from goalx_backend.config import Settings
    from goalx_backend.db import connect as db_connect
    from goalx_backend.db import connect_research, migrate

    settings = Settings(
        db_path=tmp_path / "goalx.db",
        research_db_path=tmp_path / "research.db",
        corpus_root=tmp_path / "corpus",
    )
    conn = db_connect(settings.db_path)
    try:
        migrate(conn)
        challenger_id = _challenger_with_spec(conn)
        champion_id = versioning.register_version(
            conn,
            estimator_version="dc-gate-champion",
            calibration_version="shin",
            params_version="fp-c",
            label="champion",
        )
        # 研究库写进 CLI 要读的文件库（connect_research=建库+研究链迁移）
        file_research = connect_research(settings.research_db_path)
        try:
            for run_id, label in ((1, "champion"), (2, "challenger")):
                file_research.execute(
                    "INSERT INTO backtest_runs (id, label, params, status,"
                    " created_at) VALUES (?, ?, '{}', 'done', '2026-11-11')"
                    "",
                    (run_id, label),
                )
            for run_id in (1, 2):
                for i in range(quality_gate.GATE_MIN_PAIRED):
                    ftr = _OUTCOME_CYCLE[i % 3]
                    file_research.execute(
                        """
                        INSERT INTO backtest_predictions
                            (run_id, match_key, competition, season, match_date,
                             home_team, away_team, era, ftr, had_probs, fair_probs,
                             fair_source, model_fingerprint, train_window_end)
                        VALUES (?, ?, 'E0', '2526', '2025-09-01', 'h', 'a',
                                'trajectory', ?, ?, ?, 'traj_anchor', 'fp',
                                '2025-08-31')
                        """,
                        (
                            run_id,
                            f"sid-{i:04d}",
                            ftr,
                            json.dumps(_probs_for(ftr)),
                            json.dumps({"h": 0.4, "d": 0.3, "a": 0.3}),
                        ),
                    )
            file_research.commit()
        finally:
            file_research.close()
        _cmd_quality_gate_evaluate(
            argparse.Namespace(
                challenger_version=challenger_id,
                champion_run=1,
                challenger_run=2,
                champion_version=champion_id,
            ),
            settings=settings,
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is True
        assert payload["files"]["json"].endswith("quality-gate.json")
        assert (
            conn.execute("SELECT COUNT(*) c FROM quality_gate_events").fetchone()["c"]
            == 1
        )
    finally:
        conn.close()


def test_evaluate_gate_rejects_same_run_pair(db: sqlite3.Connection) -> None:
    """自对比守卫：champion/challenger 同一 run（逐场零差恒过门）fail-loud
    拒绝——不是质量门读数。"""
    research = _research_conn()
    try:
        _seed_run(research, 1, "only")
        challenger_id = _challenger_with_spec(db)
        with pytest.raises(ValueError, match="相同"):
            quality_gate.evaluate_quality_gate(
                research,
                db,
                challenger_version_id=challenger_id,
                champion_run_id=1,
                challenger_run_id=1,
            )
        assert (
            db.execute("SELECT COUNT(*) c FROM quality_gate_events").fetchone()["c"]
            == 0
        )
    finally:
        research.close()


# ---- correctness-review 批修 F1-F6 ----


def test_f1_evaluate_rejects_incomplete_runs(db: sqlite3.Connection) -> None:
    """F1：非 done run（running/failed，逐联赛 checkpoint 已提交局部行）的
    局部判定会被 UNIQUE 永久固化→stale-pass——评估前查 status 拒绝
    （backtest 生产者契约：读者判不完整）；run 缺席同拒；零落账。"""
    research = _research_conn()
    try:
        _seed_run(research, 1, "champion")
        # challenger run=2 置 running（≥200 行已提交=局部面足以假过门）
        research.execute(
            "INSERT INTO backtest_runs (id, label, params, status, created_at)"
            " VALUES (2, 'challenger', '{}', 'running', '2026-11-11')"
        )
        _seed_predictions(research, run_id=1, n=quality_gate.GATE_MIN_PAIRED)
        _seed_predictions(research, run_id=2, n=quality_gate.GATE_MIN_PAIRED)
        challenger_id = _challenger_with_spec(db)
        with pytest.raises(ValueError, match="running"):
            quality_gate.evaluate_quality_gate(
                research,
                db,
                challenger_version_id=challenger_id,
                champion_run_id=1,
                challenger_run_id=2,
            )
        # run 缺席同拒（清数据面后 champion run=99 不存在）
        with pytest.raises(ValueError, match="不存在"):
            quality_gate.evaluate_quality_gate(
                research,
                db,
                challenger_version_id=challenger_id,
                champion_run_id=99,
                challenger_run_id=1,
            )
        assert (
            db.execute("SELECT COUNT(*) c FROM quality_gate_events").fetchone()["c"]
            == 0
        )
    finally:
        research.close()


def test_f2_degenerate_variance_hangs(db: sqlite3.Connection) -> None:
    """F2：DM 方差退化（(0.0,1.0) 分支）不可读作不劣于——常数劣差与
    交替劣差两形态逐场恒劣（均值>0）都挂 degenerate_variance。"""

    def _seed_fixed(
        research: sqlite3.Connection,
        *,
        run_id: int,
        n: int,
        probs_by_parity: tuple[dict[str, float], ...] | dict[str, float],
    ) -> None:
        for i in range(n):
            pattern = (
                probs_by_parity[i % len(probs_by_parity)]
                if isinstance(probs_by_parity, tuple)
                else probs_by_parity
            )
            research.execute(
                """
                INSERT INTO backtest_predictions
                    (run_id, match_key, competition, season, match_date, home_team,
                     away_team, era, ftr, had_probs, fair_probs, fair_source,
                     model_fingerprint, train_window_end)
                VALUES (?, ?, 'E0', '2526', '2025-09-01', 'h', 'a',
                        'trajectory', 'H', ?, ?, 'traj_anchor', 'fp',
                        '2025-08-31')
                """,
                (
                    run_id,
                    f"sid-{i:04d}",
                    json.dumps(pattern),
                    json.dumps({"h": 0.4, "d": 0.3, "a": 0.3}),
                ),
            )

    n = quality_gate.GATE_MIN_PAIRED
    champion_probs = {"h": 0.5, "d": 0.25, "a": 0.25}
    for name, challenger in (
        ("常数劣差", {"h": 0.4, "d": 0.3, "a": 0.3}),
        (
            "交替劣差",
            ({"h": 0.45, "d": 0.275, "a": 0.275}, {"h": 0.35, "d": 0.325, "a": 0.325}),
        ),
    ):
        research = _research_conn()
        try:
            _seed_run(research, 1, "champion")
            _seed_run(research, 2, "challenger")
            _seed_fixed(research, run_id=1, n=n, probs_by_parity=champion_probs)
            _seed_fixed(research, run_id=2, n=n, probs_by_parity=challenger)
            challenger_id = _challenger_with_spec(db)
            evaluation = quality_gate.evaluate_quality_gate(
                research,
                db,
                challenger_version_id=challenger_id,
                champion_run_id=1,
                challenger_run_id=2,
            )
            assert evaluation.passed is False, name
            assert evaluation.hang_reason == "degenerate_variance", name
            assert evaluation.metric_challenger > evaluation.metric_champion, name
            assert evaluation.p_one_sided is None  # 退化面无有效 SE，p 不伪造
        finally:
            research.close()


def test_f3_freeze_rejects_backfill_on_idempotent_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F3：幂等命中旧版本不得事后补冻结（prereg 击穿口）——重注册同三元组
    且版本无冻结面时拒绝（文案与 evaluate 侧「不得事后补」一致）；新建
    版本注册照常冻结。"""
    import argparse

    from goalx_backend.cli import _cmd_version_register
    from goalx_backend.config import Settings
    from goalx_backend.db import connect as db_connect
    from goalx_backend.db import migrate

    settings = Settings(db_path=tmp_path / "goalx.db")
    conn = db_connect(settings.db_path)
    try:
        migrate(conn)
        # 旧版本（票 11 前形态）：直接 versioning 注册，无冻结面
        legacy = versioning.register_version(
            conn,
            estimator_version="dc-legacy",
            calibration_version="shin",
            params_version="fp-legacy",
            label="legacy",
        )
        assert quality_gate.frozen_spec(conn, legacy) is None
        # 常数漂移后的幂等重注册（同一三元组）→ 拒绝补冻结
        monkeypatch.setattr(quality_gate, "GATE_ALPHA", 0.01)
        with pytest.raises(LookupError, match="不得事后补"):
            _cmd_version_register(
                argparse.Namespace(
                    estimator="dc-legacy",
                    calibration="shin",
                    params="fp-legacy",
                    label="legacy-relabel",
                ),
                settings=settings,
            )
        assert quality_gate.frozen_spec(conn, legacy) is None  # 仍无冻结面
        # 新建版本 → 随注册冻结（当前代码常数面）
        _cmd_version_register(
            argparse.Namespace(
                estimator="dc-fresh",
                calibration="shin",
                params="fp-fresh",
                label="fresh",
            ),
            settings=settings,
        )
        capsys.readouterr()
        fresh = conn.execute(
            "SELECT id FROM strategy_versions WHERE estimator_version='dc-fresh'"
        ).fetchone()["id"]
        spec = quality_gate.frozen_spec(conn, int(fresh))
        assert spec is not None
        assert spec.alpha == 0.01  # monkeypatch 面如实冻结（代码常数=当时值）
    finally:
        conn.close()


def test_f4_coverage_asymmetry_hangs_and_records_counts(
    db: sqlite3.Connection,
) -> None:
    """F4：challenger 窗内覆盖 220/5000（4%）——门不得在缩水面上过：
    coverage_asymmetry 挂，两 run 窗内行数与配对覆盖率入账。"""
    research = _research_conn()
    try:
        _seed_run(research, 1, "champion")
        _seed_run(research, 2, "challenger")
        _seed_predictions(research, run_id=1, n=5000)
        _seed_predictions(research, run_id=2, n=220)  # 共享 sid 前缀
        challenger_id = _challenger_with_spec(db)
        evaluation = quality_gate.evaluate_quality_gate(
            research,
            db,
            challenger_version_id=challenger_id,
            champion_run_id=1,
            challenger_run_id=2,
        )
        assert evaluation.n_paired == 220
        assert evaluation.n_champion_window == 5000
        assert evaluation.n_challenger_window == 220
        assert evaluation.paired_ratio == pytest.approx(220 / 5000)
        assert evaluation.passed is False
        assert evaluation.hang_reason == "coverage_asymmetry"
        row = db.execute(
            """
            SELECT n_champion_window, n_challenger_window, paired_ratio
            FROM quality_gate_events
            """
        ).fetchone()
        assert row["n_champion_window"] == 5000
        assert row["n_challenger_window"] == 220
    finally:
        research.close()


def test_f5_consecutive_failures_dedup_by_version(db: sqlite3.Connection) -> None:
    """F5：连续门挂按 challenger 版本迭代序计数（票面「影子重训连续 N 次
    门挂」）——同版本多 run 对复评挂算 1 段；跨版本各挂才递增。"""
    v1 = _challenger_with_spec(db)

    def _new_version(tag: str) -> int:
        version_id = versioning.register_version(
            db,
            estimator_version=f"dc-gate-{tag}",
            calibration_version="shin",
            params_version="fp",
            label=f"shadow-{tag}",
        )
        return version_id

    # 同版本三 run 对全挂 → 1（一个失败迭代）
    for runs in ((1, 11), (1, 12), (1, 13)):
        _seed_gate_event(
            db,
            v1,
            passed=False,
            hang_reason="rps_significantly_worse",
            ts="2026-11-01T00:00:00+00:00",
            runs=runs,
        )
    assert quality_gate.consecutive_gate_failures(db) == 1
    # 新版本 v2、v3 各挂 → 递增到 3（告警触发面）
    v2 = _new_version("b")
    v3 = _new_version("c")
    _seed_gate_event(
        db,
        v2,
        passed=False,
        hang_reason="insufficient_pairs",
        ts="2026-11-02T00:00:00+00:00",
        runs=(1, 14),
    )
    _seed_gate_event(
        db,
        v3,
        passed=False,
        hang_reason="rps_significantly_worse",
        ts="2026-11-03T00:00:00+00:00",
        runs=(1, 15),
    )
    assert quality_gate.consecutive_gate_failures(db) == 3
    # 过门（任意版本）→ 断
    _seed_gate_event(
        db,
        v3,
        passed=True,
        hang_reason="",
        ts="2026-11-04T00:00:00+00:00",
        runs=(1, 16),
    )
    assert quality_gate.consecutive_gate_failures(db) == 0


def test_f6_promote_gate_and_transition_share_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6：硬门查账与转移落账同事务（check-then-act 跨事务窗口闭合）——
    spy 记录两者调用时的 conn.in_transaction，须同为 True（CLI 外层
    atomic；weekly_agent F6 同款断言先例）。"""
    import argparse

    from goalx_backend.cli import _cmd_version_promote
    from goalx_backend.config import Settings
    from goalx_backend.db import connect as db_connect
    from goalx_backend.db import migrate

    settings = Settings(db_path=tmp_path / "goalx.db")
    conn = db_connect(settings.db_path)
    try:
        migrate(conn)
        challenger_id = versioning.register_version(
            conn,
            estimator_version="dc-f6",
            calibration_version="shin",
            params_version="fp",
            label="f6",
            initial_state=StrategyState.SHADOW,
        )
        _seed_gate_event(
            conn,
            challenger_id,
            passed=True,
            hang_reason="",
            ts="2026-11-11T00:00:00+00:00",
        )
        seen: dict[str, bool] = {}
        real_gate = quality_gate.require_gate_pass

        def spy_gate(conn_arg: sqlite3.Connection, version_id: int) -> None:
            seen["gate_in_txn"] = conn_arg.in_transaction
            real_gate(conn_arg, version_id)

        def spy_transition(
            conn_arg: sqlite3.Connection,
            version_id: int,
            to_state: StrategyState,
            **kwargs: object,
        ) -> None:
            seen["transition_in_txn"] = conn_arg.in_transaction

        monkeypatch.setattr(quality_gate, "require_gate_pass", spy_gate)
        monkeypatch.setattr(versioning, "transition_version", spy_transition)
        _cmd_version_promote(
            argparse.Namespace(id=challenger_id, to="probe", reason="F6 同事务"),
            settings=settings,
        )
        assert seen == {"gate_in_txn": True, "transition_in_txn": True}
    finally:
        conn.close()


def test_require_gate_pass_bootstrap_exemption_once(db: sqlite3.Connection) -> None:
    """引导豁免（席位语义）：现役 live 席位空缺时 shadow→probe 免门
    （无在位 champion 可保护，人 CLI --reason 即审计面）；席位在场
    （含自身升到 live-paper）→ 豁免关闭、无记录照拒。"""
    challenger_id = _challenger_with_spec(db)
    # 豁免期：无任何门评估记录也不拒（席位空缺）
    quality_gate.require_gate_pass(db, challenger_id)
    # 自身升到 live-paper 占席（最新转移行）→ 豁免关闭
    db.execute(
        "INSERT INTO version_transitions (version_id, from_state, to_state,"
        " reason, actor, ts) VALUES (?, 'shadow', 'live-paper', '演练上岗',"
        " 'human:cli', '2026-11-01T00:00:00+00:00')",
        (challenger_id,),
    )
    db.commit()
    with pytest.raises(quality_gate.GateNotPassed, match="无门评估记录"):
        quality_gate.require_gate_pass(db, challenger_id)
