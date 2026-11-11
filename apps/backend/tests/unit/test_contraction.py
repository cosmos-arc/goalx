"""vision-build 票 08：收缩双钟与五级反向金丝雀。

prereg 附录（.scratch/edge-program-20261107/issues/
06-prereg-appendix-contraction-clocks.md）冻结口径的谓词级钉住：级别
转移全表（相邻一级，跳级/自环拒绝，agent 无路径）、双钟数值边界
（恰 3%/恰 2SE/恰 50 注/恰 365 日锚点）、PSI 数值面。账面（v52 两表
append-only）与 L0→L1→L0 演练 e2e 在文件尾。
"""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend.evaluation import contraction
from goalx_backend.evaluation.contraction import ContractionLevel


def test_transition_full_table() -> None:
    """级别转移全表：恰允许相邻一级双向（逐级降逐级可逆，重升必经 L2）。"""
    adjacent = {
        (ContractionLevel.L0, ContractionLevel.L1),
        (ContractionLevel.L1, ContractionLevel.L0),
        (ContractionLevel.L1, ContractionLevel.L2),
        (ContractionLevel.L2, ContractionLevel.L1),
        (ContractionLevel.L2, ContractionLevel.L3),
        (ContractionLevel.L3, ContractionLevel.L2),
        (ContractionLevel.L3, ContractionLevel.L4),
        (ContractionLevel.L4, ContractionLevel.L3),
    }
    for from_level in ContractionLevel:
        for to_level in ContractionLevel:
            pair = (from_level, to_level)
            assert contraction.transition_allowed(*pair) == (pair in adjacent), pair


def test_deployment_plan_monotone_shutdown() -> None:
    """级别→deployment 动作映射：逐级只关不开（除漂移监测恒开，常设不撤）。"""
    plan0 = contraction.deployment_plan(ContractionLevel.L0)
    plan1 = contraction.deployment_plan(ContractionLevel.L1)
    plan2 = contraction.deployment_plan(ContractionLevel.L2)
    plan3 = contraction.deployment_plan(ContractionLevel.L3)
    plan4 = contraction.deployment_plan(ContractionLevel.L4)
    # L1 停新注册，其余照常
    assert plan1.new_registrations is False
    assert plan0.new_registrations is True
    assert plan1.probes_and_auto_selection is True
    # L2 停探针+自动选票；周批暂停（不删）；月读改季读
    assert plan2.probes_and_auto_selection is False
    assert plan2.weekly_batch == "paused"
    assert plan2.monthly_read_cadence == "quarterly"
    # L3 采集缩监测最小集；L4 只留漂移监测
    assert plan3.ingest_scope == "monitoring_minimal"
    assert plan4.ingest_scope == "drift_only"
    # 漂移监测常设不撤（L4 仍在）
    for plan in (plan0, plan1, plan2, plan3, plan4):
        assert plan.drift_monitoring is True


def test_deployment_plan_is_recomputable_l0_l1_l0() -> None:
    """映射纯函数可复算：L0→L1 关注册，L1→L0 恢复——演练的动作面还原。"""
    assert contraction.deployment_plan(
        ContractionLevel.L1
    ) != contraction.deployment_plan(ContractionLevel.L0)
    restored = contraction.deployment_plan(ContractionLevel.L0)
    assert restored == contraction.deployment_plan(ContractionLevel.L0)
    assert restored.new_registrations is True


# ---- v52 账面：级别 transitions（append-only）+ 双钟读数账 ----


def test_migration_v52_idempotent(db: sqlite3.Connection) -> None:
    """v52 幂等：两表就位、genesis L0 落账、重跑零效果（同版本重跑先例）。"""
    from goalx_backend.db import migrate

    assert migrate(db) == 59
    assert migrate(db) == 59  # 同版本重跑零效果
    for table in ("contraction_transitions", "contraction_clock_readings"):
        assert (
            db.execute(
                "SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()["c"]
            == 1
        )
    # genesis 行=L0（prereg A 节：现态=最新行；账非空）
    assert contraction.current_level(db) is ContractionLevel.L0


def test_set_level_human_only_and_audited(db: sqlite3.Connection) -> None:
    """级别变更只能人 CLI：agent actor 拒绝（零写入）；human 落痕可审计。"""
    with pytest.raises(contraction.InvalidLevelTransition):
        contraction.set_level(db, ContractionLevel.L1, actor="agent:ops", reason="自动")
    assert contraction.current_level(db) is ContractionLevel.L0  # 零写入
    contraction.set_level(db, ContractionLevel.L1, actor="human:cli", reason="主钟触发")
    rows = db.execute(
        "SELECT level, actor, reason FROM contraction_transitions ORDER BY id"
    ).fetchall()
    assert [str(r["level"]) for r in rows] == ["L0", "L1"]  # genesis + 变更
    assert str(rows[-1]["actor"]) == "human:cli"


def test_set_level_rejects_jump_and_loop(db: sqlite3.Connection) -> None:
    """跳级/自环拒绝（谓词先验，零写入）——要快=连发相邻 CLI。"""
    with pytest.raises(contraction.InvalidLevelTransition):
        contraction.set_level(db, ContractionLevel.L2, actor="human:cli", reason="跳级")
    with pytest.raises(contraction.InvalidLevelTransition):
        contraction.set_level(db, ContractionLevel.L0, actor="human:cli", reason="自环")
    assert contraction.current_level(db) is ContractionLevel.L0


def test_level_ledger_append_only(db: sqlite3.Connection) -> None:
    """级别账 append-only：UPDATE/DELETE 库层拒绝（v50/v51 触发器先例）。"""
    contraction.set_level(db, ContractionLevel.L1, actor="human:cli", reason="演练")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE contraction_transitions SET level='L4'")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM contraction_transitions")


def test_drill_l0_l1_l0_e2e(db: sqlite3.Connection) -> None:
    """L0→L1→L0 演练 e2e：切换留痕 + 动作面恢复可复算（票 08 验收）。"""
    assert contraction.current_level(db) is ContractionLevel.L0
    before = contraction.current_plan(db)
    contraction.set_level(db, ContractionLevel.L1, actor="human:cli", reason="主钟触发")
    during = contraction.current_plan(db)
    assert during.new_registrations is False
    assert before.new_registrations is True
    contraction.set_level(db, ContractionLevel.L0, actor="human:cli", reason="恢复演练")
    after = contraction.current_plan(db)
    assert after == before  # 恢复可复算
    ledger = [
        str(r["level"])
        for r in db.execute("SELECT level FROM contraction_transitions ORDER BY id")
    ]
    assert ledger == ["L0", "L1", "L0"]


# ---- CLV 主钟（B.1：活动窗=最近 50 注 / 3% 阈 / 2SE 单侧；2026-11-11 人裁）----


def test_clv_clock_boundaries() -> None:
    """
    B.1 数值边界（严格不等，恰界不触发）。

    全等窗（se 恰 0.0）给精确浮点刀口：恰 3% 不触发；非退化窗用独立
    手算（与实现不同算路）验证显著门。
    """
    # 恰 3%：均值恰阈不触发（严格 <，零方差刀口）
    assert not contraction.clv_clock_fired([0.03] * 50)
    # 2% 但零方差=无限显著为正 → 「不显著」不成立，不触发
    assert not contraction.clv_clock_fired([0.02] * 50)
    # 2% 且不显著为正：25×(+0.0925)+25×(−0.0525) → 均值 0.02、pstdev 0.0725
    # → se=0.0725/√50≈0.010253 → 2se≈0.0205>0.02 → 触发
    assert contraction.clv_clock_fired([0.0925] * 25 + [-0.0525] * 25)
    # 2% 且显著为正：25×(+0.09)+25×(−0.05) → 均值 0.02、pstdev 0.07
    # → se≈0.009899 → 2se≈0.0198<0.02 → 不触发
    assert not contraction.clv_clock_fired([0.09] * 25 + [-0.05] * 25)
    # 均值为负（方向门自动不显著为正）→ 触发
    assert contraction.clv_clock_fired([-0.01] * 50)
    # n<50 恒不评估（恰 49 注不足窗；恰 50 注可判）
    assert not contraction.clv_clock_fired([-0.05] * 49)
    assert contraction.clv_clock_fired([-0.05] * 50)


def _seed_bet(
    db: sqlite3.Connection, *, clv: float, settled_at: str, strategy: str = "auto-v1"
) -> None:
    db.execute(
        """
        INSERT INTO bets (mode, market_kind, stake, created_at, status, payout,
                          profit, settled_at, strategy_version, clv)
        VALUES ('paper', 'fixed', 2.0, ?, 'won', 0.0, 0.0, ?, ?, ?)
        """,
        (settled_at, settled_at, strategy, clv),
    )


def test_clv_clock_reading_window_and_face(db: sqlite3.Connection) -> None:
    """主钟读数面：活动窗=最近 50 注（日期无关）+ 探针注准入（触发器 A 同源）。"""
    from datetime import UTC, datetime

    now = datetime(2026, 11, 10, tzinfo=UTC)
    # 51 注负 CLV：最早 1 注被活动窗挤出（LIMIT 50 语义，与日期新旧无关）
    _seed_bet(db, clv=0.9, settled_at="2025-01-01T00:00:00+00:00")  # 最旧，出局
    for i in range(50):
        _seed_bet(
            db, clv=-0.05, settled_at=f"2026-06-{(i % 28) + 1:02d}T00:00:00+00:00"
        )
    # 干扰行不进窗：非 auto / live 模式
    _seed_bet(db, clv=-0.9, settled_at="2026-07-01T00:00:00+00:00", strategy=None)
    db.execute(
        """
        INSERT INTO bets (mode, market_kind, stake, created_at, status, payout,
                          profit, settled_at, strategy_version, clv)
        VALUES ('live', 'fixed', 2.0, '2026-06-01', 'won', 0.0, 0.0,
                '2026-07-02T00:00:00+00:00', 'auto-v1', -0.9)
        """
    )
    reading = contraction.clv_clock_reading(db, now=now)
    assert reading.status == "ok"
    assert reading.n == 50
    assert reading.mean == -0.05
    assert reading.fired is True
    # 窗端点：start=窗内首注 settled_at（数据跨度）；end=读数时刻（读数账
    # 防重键口径——correctness F1：冻结窗下两次真实读各留一行）
    assert reading.window_start == "2026-06-01T00:00:00+00:00"
    assert reading.window_end == now.isoformat(timespec="seconds")


def test_clv_activity_window_covers_min_n() -> None:
    """correctness F3：常数耦合钉死——活动窗上限≥最小 n（防 TRIGGER_A_WINDOW

    被单独调低于 CLV_CLOCK_MIN_N 致主钟永久 insufficient 静默哑火）。
    """
    from goalx_backend.betting import versioning

    assert contraction.CLV_CLOCK_MIN_N <= versioning.TRIGGER_A_WINDOW


def test_clv_clock_reading_insufficient(db: sqlite3.Connection) -> None:
    """n<50=读数不足只报不判（窗如实返回短计数）。"""
    from datetime import UTC, datetime

    _seed_bet(db, clv=-0.05, settled_at="2026-11-01T00:00:00+00:00")
    reading = contraction.clv_clock_reading(db, now=datetime(2026, 11, 10, tzinfo=UTC))
    assert reading.status == "insufficient"
    assert reading.n == 1
    assert reading.fired is False


# ---- 探索钟（2026-11-11 人裁：52 轮止损窗 / 锚点=周批首跑日 / EM 全阴+零幸存者）----


def _wind_rounds(db: sqlite3.Connection, rounds: int | None = None) -> None:
    """上弦回路轮次（每轮一活跃日，日期互异）。"""
    total = contraction.EXPLORATION_WINDOW_ROUNDS if rounds is None else rounds
    for i in range(total):
        contraction.record_weekly_round(
            db, run_date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
        )


def test_record_weekly_round_daily_idempotent(db: sqlite3.Connection) -> None:
    """轮次记账：同日重跑不重复计轮（水位守卫的幂等语义同款）。"""
    assert contraction.weekly_rounds(db) == 0  # 无 face 行=0
    assert contraction.record_weekly_round(db, run_date="2026-11-16") == 1
    assert contraction.record_weekly_round(db, run_date="2026-11-16") == 1  # 同日
    assert contraction.record_weekly_round(db, run_date="2026-11-23") == 2
    assert contraction.weekly_rounds(db) == 2


def test_round_counter_bad_json_degrades(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """correctness F2：round_counter 行坏 JSON/坏类型读作 0 轮，不炸读数面。"""
    from datetime import UTC, datetime

    db.execute(
        """
        INSERT INTO weekly_agent_state (face, updated_at, stats)
        VALUES ('round_counter', '2026-11-10T06:00:00+00:00', 'not-json')
        """
    )
    assert contraction.weekly_rounds(db) == 0
    now = datetime(2026, 11, 10, tzinfo=UTC)
    monkeypatch.setattr(
        contraction, "EXPLORATION_CLOCK_ANCHOR", "2025-11-10T00:00:00+00:00"
    )
    reading = contraction.exploration_clock_reading(db, now=now)  # 不抛
    assert reading.rounds == 0
    assert reading.status == "insufficient"
    # bool 是 int 子类：{"rounds": true} 是坏账不是 1 轮
    db.execute(
        """
        UPDATE weekly_agent_state
        SET stats='{"rounds": true}' WHERE face='round_counter'
        """
    )
    assert contraction.weekly_rounds(db) == 0


def _seed_observation(db: sqlite3.Connection, scanner: str, created_at: str) -> None:
    db.execute(
        """
        INSERT OR REPLACE INTO edge_observations
            (scanner, window_key, payload, created_at)
        VALUES (?, ?, '{}', ?)
        """,
        (scanner, f"{scanner}:w-{created_at}", created_at),
    )


def _seed_finding(
    db: sqlite3.Connection,
    *,
    scanner: str = "s1",
    found_at: str,
    status: str = "new",
    segment: str = "seg-a",
) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO edge_findings
            (scanner, segment_key, metric_value, n, se, window, status, found_at)
        VALUES (?, ?, 3.5, 500, 0.5, 'w', ?, ?)
        """,
        (scanner, segment, status, found_at),
    )


def _fresh_sweeps(db: sqlite3.Connection, *, now_iso: str) -> None:
    for scanner in ("s1", "s2", "s4"):
        _seed_observation(db, scanner, now_iso)


def test_exploration_clock_unanchored_and_unwound(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """锚点未钉=unanchored；已钉但轮次未满 52=insufficient（只报不判）。"""
    from datetime import UTC, datetime

    now = datetime(2026, 11, 10, tzinfo=UTC)
    _fresh_sweeps(db, now_iso=now.isoformat(timespec="seconds"))
    # 锚点未钉（票 03 周批未首跑）
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.status == "unanchored"
    assert reading.rounds == 0
    assert reading.fired is False
    # 锚点已钉但零轮：未上弦
    monkeypatch.setattr(
        contraction, "EXPLORATION_CLOCK_ANCHOR", "2026-04-24T00:00:00+00:00"
    )
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.status == "insufficient"
    assert reading.fired is False


def test_exploration_clock_fires_on_quiet_full_rounds(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """满 52 轮 + 阵列新鲜 + 零发现零幸存者 → 触发建议 L2；51 轮不触发。"""
    from datetime import UTC, datetime

    now = datetime(2026, 11, 10, tzinfo=UTC)
    monkeypatch.setattr(
        contraction, "EXPLORATION_CLOCK_ANCHOR", "2025-11-10T00:00:00+00:00"
    )
    _fresh_sweeps(db, now_iso=now.isoformat(timespec="seconds"))
    _wind_rounds(db, rounds=contraction.EXPLORATION_WINDOW_ROUNDS - 1)
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.status == "insufficient"  # 恰 51 轮=未满
    assert reading.fired is False
    _wind_rounds(db, rounds=1)
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.status == "ok"
    assert reading.rounds == contraction.EXPLORATION_WINDOW_ROUNDS
    assert (reading.new_findings, reading.survivors) == (0, 0)
    assert reading.fired is True


def test_exploration_clock_blocked_by_survivor_or_new_finding(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """锚后幸存者（promoted 发现 / supported 假设）或越门发现阻断触发。"""
    from datetime import UTC, datetime

    from goalx_backend.evaluation import hypothesis_registry

    now = datetime(2026, 11, 10, tzinfo=UTC)
    monkeypatch.setattr(
        contraction, "EXPLORATION_CLOCK_ANCHOR", "2025-11-10T00:00:00+00:00"
    )
    _fresh_sweeps(db, now_iso=now.isoformat(timespec="seconds"))
    _wind_rounds(db)
    # promoted 发现（幸存者面①）
    _seed_finding(db, found_at="2026-06-01T00:00:00+00:00", status="promoted")
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.fired is False
    # supported 假设（幸存者面②）同样阻断（发现改 dead 后，幸存者归零）
    db.execute("UPDATE edge_findings SET status='dead'")
    hypothesis_registry.register_hypothesis(
        db,
        h_id="H9",
        statement="测试假设",
        judgement="n≥100",
        data_face="fixture",
        expected_n="n≥100",
        ledger_ref="edge-ledger:E-99",
    )
    hypothesis_registry.transition_hypothesis(
        db,
        "H9",
        hypothesis_registry.HypothesisState.SUPPORTED,
        actor="human:cli",
        reason="人裁支持",
    )
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.survivors == 1
    assert reading.fired is False
    # 窗内越门新发现（未人裁生死）也阻断：清扫非全阴——已 dead 的首条
    # 发现同计（「零越门发现」不论后人裁生死，prereg B.2）
    db.execute("UPDATE hypothesis_registry SET status='refuted' WHERE h_id='H9'")
    _seed_finding(
        db, found_at="2026-06-02T00:00:00+00:00", status="new", segment="seg-b"
    )
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.new_findings == 2  # dead 首条 + new 第二条都在窗内
    assert reading.fired is False


def test_exploration_clock_stale_sweep_is_insufficient(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """阵列停摆（新鲜度破）=insufficient：缺数据的「全阴」不是全阴。"""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 10, tzinfo=UTC)
    monkeypatch.setattr(
        contraction, "EXPLORATION_CLOCK_ANCHOR", "2025-11-10T00:00:00+00:00"
    )
    _wind_rounds(db)
    stale = (now - timedelta(hours=30)).isoformat(timespec="seconds")
    for scanner in ("s1", "s2"):
        _seed_observation(db, scanner, stale)
    _seed_observation(db, "s4", now.isoformat(timespec="seconds"))  # s1 缺席同理
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.status == "insufficient"
    assert reading.fired is False


def test_exploration_clock_window_excludes_old_survivor(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """锚端为凭（found_at 闭窗）：锚前旧发现/旧幸存者不计入。"""
    from datetime import UTC, datetime

    now = datetime(2026, 11, 10, tzinfo=UTC)
    monkeypatch.setattr(
        contraction, "EXPLORATION_CLOCK_ANCHOR", "2025-11-10T00:00:00+00:00"
    )
    _fresh_sweeps(db, now_iso=now.isoformat(timespec="seconds"))
    _wind_rounds(db)
    _seed_finding(db, found_at="2025-11-09T00:00:00+00:00", status="promoted")
    _seed_finding(
        db, found_at="2025-11-09T00:00:00+00:00", status="new", segment="seg-c"
    )
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.status == "ok"
    assert (reading.new_findings, reading.survivors) == (0, 0)
    assert reading.fired is True


# ---- 读数账（v52 contraction_clock_readings：显式读落账，同窗幂等）----


def test_record_clock_readings_append_per_read(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式读落账：每次读每钟追加一行（观测流水，票 04 F3 裁定口径）。

    UNIQUE(clock, window_end) 只吸收同窗（同秒）双写防重；不同时刻的
    真实重跑各留一行——append 无害，账面=读数流水非幂等面。
    """
    from datetime import UTC, datetime

    now = datetime(2026, 11, 10, tzinfo=UTC)
    monkeypatch.setattr(
        contraction, "EXPLORATION_CLOCK_ANCHOR", "2025-11-10T00:00:00+00:00"
    )
    _fresh_sweeps(db, now_iso=now.isoformat(timespec="seconds"))
    _wind_rounds(db)
    recorded = contraction.record_clock_readings(db, now=now)
    assert {r.clock for r in recorded} == {"clv_main", "exploration"}
    assert (
        db.execute("SELECT COUNT(*) FROM contraction_clock_readings").fetchone()[0] == 2
    )
    # 同秒双写防重（UNIQUE(clock, window_end) 的真实职责）
    again = contraction.record_clock_readings(db, now=now)
    assert {r.clock for r in again} == {"clv_main", "exploration"}
    assert (
        db.execute("SELECT COUNT(*) FROM contraction_clock_readings").fetchone()[0] == 2
    )
    # 不同时刻重跑=新读数，各钟再追加一行（每次月读每钟留痕一行）
    contraction.record_clock_readings(
        db, now=datetime(2026, 11, 10, 0, 0, 30, tzinfo=UTC)
    )
    assert (
        db.execute("SELECT COUNT(*) FROM contraction_clock_readings").fetchone()[0] == 4
    )
    row = db.execute(
        """
        SELECT clock, n, value, fired, detail FROM contraction_clock_readings
        WHERE clock='exploration'
        """
    ).fetchone()
    assert row["n"] == 0  # 静默满弦窗：零幸存者触发
    assert row["fired"] == 1
    assert "survivors" in str(row["detail"])  # detail JSON 带幸存者/发现/轮次面


def test_record_clock_readings_frozen_window_appends_per_read(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """correctness F1：冻结窗（末注 settled_at 不再前进）下两次真实读各留一行。

    clv_main 防重键=window_end=读数时刻（非末注 settled_at）——若误用数据
    跨度做键，暂停/稀疏期第二次月读被 INSERT OR IGNORE 吞行，主钟观测
    流水断流。
    """
    from datetime import UTC, datetime

    _seed_bet(db, clv=-0.05, settled_at="2026-06-01T00:00:00+00:00")  # 冻结窗
    monkeypatch.setattr(
        contraction, "EXPLORATION_CLOCK_ANCHOR", "2025-11-10T00:00:00+00:00"
    )
    contraction.record_clock_readings(db, now=datetime(2026, 11, 1, tzinfo=UTC))
    contraction.record_clock_readings(db, now=datetime(2026, 12, 1, tzinfo=UTC))
    rows = db.execute(
        """
        SELECT window_end FROM contraction_clock_readings WHERE clock='clv_main'
        """
    ).fetchall()
    assert len(rows) == 2
    assert {str(r["window_end"]) for r in rows} == {
        "2026-11-01T00:00:00+00:00",
        "2026-12-01T00:00:00+00:00",
    }


def test_clock_readings_ledger_append_only(db: sqlite3.Connection) -> None:
    """读数账 append-only：UPDATE/DELETE 库层拒绝。"""
    from datetime import UTC, datetime

    contraction.record_clock_readings(db, now=datetime(2026, 11, 10, tzinfo=UTC))
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE contraction_clock_readings SET fired=0")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM contraction_clock_readings")


# ---- PSI（附录 C：分箱分布比较纯函数；>0.25 越阈严格不等）----


def test_psi_identical_distributions_are_zero() -> None:
    """同分布 PSI=0（逐桶比例相等，Σ0·ln1=0）。"""
    data = [float(i) for i in range(1, 11)]
    assert contraction.psi(data, data) == 0.0


def test_psi_shifted_mass_hand_calculated() -> None:
    """质量全移末桶：独立手算（与实现不同算路）对数。"""
    import math

    reference = [float(i) for i in range(1, 11)]  # 十分位等频桶，各桶 1/10
    current = [10.0] * 10  # 全部落末桶
    p_ref_empty = 1.0 / (2 * 10)  # 空桶半样本代理
    expected = 9 * (p_ref_empty - 0.1) * math.log(p_ref_empty / 0.1) + (
        1.0 - 0.1
    ) * math.log(1.0 / 0.1)
    assert contraction.psi(reference, current) == pytest.approx(expected)


def test_psi_threshold_strict() -> None:
    """PSI>0.25 越阈（严格 >，恰 0.25 不触发——票 01 严格不等先例）。"""
    assert contraction.psi_exceeds(0.25) is False
    assert contraction.psi_exceeds(0.2500001) is True
    assert contraction.psi_exceeds(0.1) is False


def test_psi_rejects_empty_samples() -> None:
    """空样本 fail-loud（调用方判 insufficient，不伪造分布）。"""
    with pytest.raises(ValueError):
        contraction.psi([], [1.0, 2.0])
    with pytest.raises(ValueError):
        contraction.psi([1.0, 2.0], [])


def test_psi_categorical_hand_calculated() -> None:
    """类别 PSI：类别并集 + 空类半样本代理；独立手算对照。"""
    import math

    # 同构成 → 0
    assert contraction.psi_categorical(
        {"E0": 50, "E1": 50}, {"E0": 100, "E1": 100}
    ) == pytest.approx(0.0)
    # 热门集中：p=(.99,.01) vs (.5,.5)
    expected = (0.99 - 0.5) * math.log(0.99 / 0.5) + (0.01 - 0.5) * math.log(0.01 / 0.5)
    assert contraction.psi_categorical(
        {"E0": 50, "E1": 50}, {"E0": 99, "E1": 1}
    ) == pytest.approx(expected)
    # 类并集：cur 缺 E1（空类 1/(2n) 代理）
    p_cur_missing = 1.0 / (2 * 100)
    expected_union = (1.0 - 0.5) * math.log(1.0 / 0.5) + (
        p_cur_missing - 0.5
    ) * math.log(p_cur_missing / 0.5)
    assert contraction.psi_categorical(
        {"E0": 50, "E1": 50}, {"E0": 100}
    ) == pytest.approx(expected_union)


# ---- 四信号重扫唤醒（prereg C：信号面 + 接线状态）----


def _seed_fixtures(
    db: sqlite3.Connection,
    *,
    competition: str,
    kickoffs: list[str],
) -> None:
    from goalx_backend.data import fixtures as fx_store

    comp_id = fx_store.upsert_competition(db, competition)
    home = fx_store.upsert_team(db, f"{competition}-home")
    for i, kickoff in enumerate(kickoffs):
        away = fx_store.upsert_team(db, f"{competition}-away-{i}")
        fx_store.upsert_fixture(db, comp_id, kickoff, home, away)


def test_rescan_league_mix_fires_on_shift(db: sqlite3.Connection) -> None:
    """信号③已接线：联赛构成类别 PSI 越阈=唤醒告警（当前 30 日 vs 前 90 日）。"""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 10, tzinfo=UTC)
    iso = lambda d: d.isoformat(timespec="seconds")  # noqa: E731
    # 参考窗（前 90 日）：两联赛对半
    _seed_fixtures(
        db,
        competition="英超",
        kickoffs=[iso(now - timedelta(days=100 + i)) for i in range(50)],
    )
    _seed_fixtures(
        db,
        competition="英冠",
        kickoffs=[iso(now - timedelta(days=100 + i)) for i in range(50)],
    )
    # 当前窗（近 30 日）：全英超 → 构成显著变
    _seed_fixtures(
        db,
        competition="英超",
        kickoffs=[iso(now - timedelta(days=20 + i // 5)) for i in range(30)],
    )
    faces = {face.name: face for face in contraction.rescan_signals(db, now=now)}
    assert faces["league_mix"].status == "fired"
    assert "英超" in faces["league_mix"].detail
    # 未接线面如实报 unwired（不伪造绿）
    assert faces["odds_psi"].status == "unwired"
    assert faces["jc_rules"].status == "unwired"
    assert faces["quarterly_sweep"].status == "unwired"


def test_rescan_league_mix_stable_and_insufficient(db: sqlite3.Connection) -> None:
    """构成稳定=ok；窗空=insufficient（只报不判，不伪造分布）。"""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 10, tzinfo=UTC)
    iso = lambda d: d.isoformat(timespec="seconds")  # noqa: E731
    _seed_fixtures(
        db,
        competition="英超",
        kickoffs=[iso(now - timedelta(days=100 + i)) for i in range(30)]
        + [iso(now - timedelta(days=20 + i // 5)) for i in range(10)],
    )
    _seed_fixtures(
        db,
        competition="英冠",
        kickoffs=[iso(now - timedelta(days=100 + i)) for i in range(30)]
        + [iso(now - timedelta(days=20 + i // 5)) for i in range(10)],
    )
    faces = {face.name: face for face in contraction.rescan_signals(db, now=now)}
    assert faces["league_mix"].status == "ok"
    # 空库：当前窗无样本
    db.execute("DELETE FROM fixtures")
    faces = {face.name: face for face in contraction.rescan_signals(db, now=now)}
    assert faces["league_mix"].status == "insufficient"


def test_rescan_quarterly_reports_next_due(db: sqlite3.Connection) -> None:
    """信号④：下次季度边界进 detail（定拍 deployment 未建=unwired，不伪造）。"""
    from datetime import UTC, datetime

    faces = {
        face.name: face
        for face in contraction.rescan_signals(
            db, now=datetime(2026, 11, 10, tzinfo=UTC)
        )
    }
    assert faces["quarterly_sweep"].status == "unwired"
    assert "2027-01-01" in faces["quarterly_sweep"].detail  # 下次季度首月首日


# ---- 阈值历史基线核对报告缝（B.1：十年回放消费票喂窗读数，本票落纯函数缝）----


def test_clv_threshold_baseline_report() -> None:
    """基线核对报告：窗分布对 3% 阈的复算面（fixture 可复算，不跑回放）。"""
    readings = [
        contraction.ReplayWindowReading(
            window_end=f"2020-{i % 12 + 1:02d}-01", n=200, mean=0.05, se=0.01
        )
        for i in range(5)
    ] + [
        contraction.ReplayWindowReading(
            window_end=f"2021-{i % 12 + 1:02d}-01", n=200, mean=0.01, se=0.01
        )
        for i in range(5)
    ]
    report = contraction.clv_threshold_baseline(readings)
    assert report["n_windows"] == 10
    assert report["share_below_threshold"] == pytest.approx(0.5)
    # mean=0.01,se=0.01 → 0.01 ≤ 2·0.01 → 非显著为正 → 恰好触发口径
    assert report["share_fired"] == pytest.approx(0.5)
    assert report["median_mean"] == pytest.approx(0.03)
    # 独立手算分位（排序后 0.01×5+0.05×5 的中位=0.01 与 0.05 均值）
    assert report["min_mean"] == pytest.approx(0.01)
    assert report["max_mean"] == pytest.approx(0.05)


def test_clv_threshold_baseline_insufficient_windows() -> None:
    """不足窗不进判读面（n<50 如实报 evaluated，不冒充）。"""
    short = [
        contraction.ReplayWindowReading(
            window_end="2020-01-01", n=49, mean=-0.9, se=0.0
        )
    ]
    report = contraction.clv_threshold_baseline(short)
    assert report["n_windows"] == 1
    assert report["evaluated"] == 0
    assert report["share_fired"] == pytest.approx(0.0)


# ---- 人 CLI（级别切换留痕 + 双钟首读落账）----


def test_cli_contraction_set_and_read(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """contraction-set 人 CLI 留痕 + contraction-read 首读落账（票 08 验收）。"""
    import argparse
    import json

    from goalx_backend.cli import _cmd_contraction_read, _cmd_contraction_set
    from goalx_backend.config import Settings
    from goalx_backend.db import connect as db_connect
    from goalx_backend.db import migrate

    settings = Settings(db_path=tmp_path / "goalx.db")
    conn = db_connect(settings.db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    # 跳级拒绝 fail-loud（人 CLI 谓词同构）
    with pytest.raises(contraction.InvalidLevelTransition):
        _cmd_contraction_set(
            argparse.Namespace(level="L2", reason="跳级"),
            settings=settings,
        )
    # L0→L1→L0 演练（人 CLI 面）
    _cmd_contraction_set(
        argparse.Namespace(level="L1", reason="主钟触发演练"), settings=settings
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"from_level": "L0", "to_level": "L1", "actor": "human:cli"}
    _cmd_contraction_set(
        argparse.Namespace(level="L0", reason="恢复演练"), settings=settings
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["to_level"] == "L0"
    # 首读（真数据面口径：fixture 库上 insufficient/unanchored 如实报）+ 落账
    _cmd_contraction_read(settings=settings)
    reading = json.loads(capsys.readouterr().out)
    assert reading["level"] == "L0"
    assert reading["clv_main"]["status"] == "insufficient"
    assert reading["exploration"]["status"] == "unanchored"
    conn = db_connect(settings.db_path)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM contraction_clock_readings").fetchone()[
                0
            ]
            == 2
        )
        ledger = [
            str(r["level"])
            for r in conn.execute(
                "SELECT level FROM contraction_transitions ORDER BY id"
            )
        ]
    finally:
        conn.close()
    assert ledger == ["L0", "L1", "L0"]  # 切换留痕不可事后美化


def test_exploration_clock_bad_freshness_stamp_degrades(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """correctness：坏留痕时间戳读作不新鲜（钟 insufficient），不上抛炸晨检。"""
    from datetime import UTC, datetime

    now = datetime(2026, 11, 10, tzinfo=UTC)
    monkeypatch.setattr(
        contraction, "EXPLORATION_CLOCK_ANCHOR", "2025-11-10T00:00:00+00:00"
    )
    for scanner in ("s1", "s2"):
        _seed_observation(db, scanner, now.isoformat(timespec="seconds"))
    db.execute(
        """
        INSERT OR REPLACE INTO edge_observations
            (scanner, window_key, payload, created_at)
        VALUES ('s4', 's4:bad', '{}', 'not-a-timestamp')
        """
    )
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.status == "insufficient"
    assert reading.scanners_fresh is False
    assert reading.fired is False


# ---- correctness-review 批修：F1 naive 串 / F2 PSI 桶界 / F3 B.3 标注 ----


def test_exploration_clock_naive_freshness_stamp(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1：naive created_at 按 UTC 补 tzinfo（晨检同防御）——不抛 TypeError。"""
    from datetime import UTC, datetime

    now = datetime(2026, 11, 10, tzinfo=UTC)
    monkeypatch.setattr(
        contraction, "EXPLORATION_CLOCK_ANCHOR", "2025-11-10T00:00:00+00:00"
    )
    _wind_rounds(db)
    # s1/s2 aware 新鲜；s4 naive 陈旧（无 tz 后缀）→ 不新鲜 → 钟不误判全阴
    for scanner in ("s1", "s2"):
        _seed_observation(db, scanner, now.isoformat(timespec="seconds"))
    db.execute(
        """
        INSERT OR REPLACE INTO edge_observations
            (scanner, window_key, payload, created_at)
        VALUES ('s4', 's4:naive-old', '{}', '2025-06-01T00:00:00')
        """
    )
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.status == "insufficient"
    assert reading.scanners_fresh is False
    # naive 但按 UTC 补后新鲜 → 阵列算新鲜（naive≠坏串）
    db.execute(
        """
        INSERT OR REPLACE INTO edge_observations
            (scanner, window_key, payload, created_at)
        VALUES ('s4', 's4:naive-new', '{}', '2026-11-10T00:00:00')
        """
    )
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.scanners_fresh is True
    assert reading.fired is True  # 满弦+全阴+零幸存者（空台账）


def test_exploration_clock_naive_anchor(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1：锚点常量 naive 串同样按 UTC 补——aware-naive 比较不抛 TypeError。"""
    from datetime import UTC, datetime

    now = datetime(2026, 11, 10, tzinfo=UTC)
    monkeypatch.setattr(contraction, "EXPLORATION_CLOCK_ANCHOR", "2025-11-10T00:00:00")
    _fresh_sweeps(db, now_iso=now.isoformat(timespec="seconds"))
    _wind_rounds(db)
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.status == "ok"  # naive 2025-11-10T00:00 按 UTC 读=恰满弦
    assert reading.fired is True


def test_psi_bin_boundaries_per_prereg() -> None:
    """F2：桶界归属照 prereg C——<e_1 归首桶 / ==e_i 归 [e_i,e_{i+1}) / ≥e_9 归末桶。"""
    import math
    from statistics import quantiles

    reference = [0.0, 0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 6.0]
    edges = quantiles(reference, n=3, method="inclusive")
    assert edges == [1.0, 4.0]  # 手算前提（inclusive 分位恰落样本点）
    p_ref = (0.3, 0.3, 0.4)
    empty, full = 0.5, 1.0  # n_cur=1：空桶半样本代理 1/(2·1)，非空桶 1.0

    def expected(p_cur: tuple[float, float, float]) -> float:
        return sum((c - r) * math.log(c / r) for c, r in zip(p_cur, p_ref, strict=True))

    # ==e_1（1.0）归中桶 [e_1, e_2)：bisect_left 误归首桶（值不同可分辨）
    assert contraction.psi(reference, [1.0], bins=3) == pytest.approx(
        expected((empty, full, empty))
    )
    # <e_1（0.5）归首桶
    assert contraction.psi(reference, [0.5], bins=3) == pytest.approx(
        expected((full, empty, empty))
    )
    # ≥e_2（9.0）归末桶（闭端且外无界）
    assert contraction.psi(reference, [9.0], bins=3) == pytest.approx(
        expected((empty, empty, full))
    )


def test_rescan_signals_l3l4_timer_face_wired(db: sqlite3.Connection) -> None:
    """票 04 缝回填：B.3 计时面从 unwired 换成真读数（晨检只读消费）。"""
    from datetime import UTC, datetime

    faces = {
        face.name: face
        for face in contraction.rescan_signals(
            db, now=datetime(2026, 11, 10, tzinfo=UTC)
        )
    }
    # genesis L0（探针未全停）→ not_applicable；永不为 fired——B.3 建议的
    # 是再收缩非唤醒重扫，晨检 fired（唤醒重扫）语义不被污染
    assert faces["l3l4_timer"].status == "not_applicable"


# ---- 票 03 接线：探索钟锚点一次性写口（周批首跑钉值） ----


def test_exploration_anchor_one_time_write_port(db: sqlite3.Connection) -> None:
    """锚点写口：一次性（首写落账）、幂等重跑不覆盖（同值/异值都返回既有锚）。"""
    effective = contraction.ensure_exploration_anchor(
        db, "2026-11-16", actor="agent:weekly-batch"
    )
    assert effective == "2026-11-16"
    # 幂等重跑（同值）：不覆盖、不多行
    assert (
        contraction.ensure_exploration_anchor(
            db, "2026-11-16", actor="agent:weekly-batch"
        )
        == "2026-11-16"
    )
    # 后续周拍（异值）：仍返回首跑锚（一次性，不覆盖）
    assert (
        contraction.ensure_exploration_anchor(
            db, "2026-11-23", actor="agent:weekly-batch"
        )
        == "2026-11-16"
    )
    rows = db.execute("SELECT anchor_date FROM exploration_clock_anchor").fetchall()
    assert [str(r["anchor_date"]) for r in rows] == ["2026-11-16"]


def test_exploration_clock_reading_prefers_db_anchor(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读数锚点解析：DB 锚行优先，缺席回退模块常量（票 08 行为不变）。"""
    from datetime import UTC, datetime

    now = datetime(2026, 11, 16, tzinfo=UTC)
    # 无 DB 锚 + 常量 None → unanchored（票 08 原行为）
    assert contraction.exploration_clock_reading(db, now=now).status == "unanchored"
    # 无 DB 锚 + 常量有值 → 用常量（monkeypatch 只影响常量面，不改库）
    monkeypatch.setattr(contraction, "EXPLORATION_CLOCK_ANCHOR", "2025-11-16")
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.status in ("ok", "insufficient")  # 恰满弦=insufficient（无留痕）
    assert reading.window_start is not None
    # DB 锚优先于常量
    contraction.ensure_exploration_anchor(db, "2026-11-09", actor="agent:weekly-batch")
    reading = contraction.exploration_clock_reading(db, now=now)
    assert reading.status == "insufficient"  # 锚后 7 日，远未满弦
    assert reading.window_start == "2026-11-09T00:00:00+00:00"


# ---- v55（票 04 缝回填）：四信号触发史账 + B.3 到期读数 ----


def test_migration_v55_idempotent(db: sqlite3.Connection) -> None:
    """v55 幂等：rescan_signal_events 就位、append-only、重跑零效果。"""
    from goalx_backend.db import migrate

    assert migrate(db) == 59
    assert migrate(db) == 59  # 同版本重跑零效果
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name=?",
            ("rescan_signal_events",),
        ).fetchone()["c"]
        == 1
    )
    db.execute(
        """
        INSERT INTO rescan_signal_events
            (signal, period, status, detail, evaluated_at)
        VALUES ('league_mix', '2026-11', 'ok', 'PSI=0.0100',
                '2026-12-01T01:35:00+00:00')
        """
    )
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE rescan_signal_events SET status = 'fired'")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM rescan_signal_events")
    # 非四信号名拒绝（CHECK 面）
    with pytest.raises(sqlite3.DatabaseError):
        db.execute(
            """
            INSERT INTO rescan_signal_events
                (signal, period, status, detail, evaluated_at)
            VALUES ('l3l4_timer', '2026-11', 'ok', 'x', 't')
            """
        )


def test_record_rescan_signals_four_faces_same_period_absorbed(
    db: sqlite3.Connection,
) -> None:
    """四信号评估落账：恰四行（B.3 timer 是读数不落账）；同期重跑吸收。"""
    from datetime import UTC, datetime

    now = datetime(2026, 12, 1, 1, 35, tzinfo=UTC)
    faces = contraction.record_rescan_signals(db, now=now, period="2026-11")
    # 五面返回（四信号 + B.3 timer 读数），落账恰四行
    assert len(faces) == 5
    rows = db.execute(
        "SELECT signal, status FROM rescan_signal_events WHERE period = '2026-11'"
        " ORDER BY signal"
    ).fetchall()
    assert [str(r["signal"]) for r in rows] == [
        "jc_rules",
        "league_mix",
        "odds_psi",
        "quarterly_sweep",
    ]
    # 同期重跑吸收（UNIQUE(signal, period)，首读留痕）
    contraction.record_rescan_signals(db, now=now, period="2026-11")
    assert (
        db.execute("SELECT COUNT(*) c FROM rescan_signal_events").fetchone()["c"] == 4
    )
    # 异期新落
    contraction.record_rescan_signals(
        db, now=datetime(2027, 1, 1, 1, 35, tzinfo=UTC), period="2026-12"
    )
    assert (
        db.execute("SELECT COUNT(*) c FROM rescan_signal_events").fetchone()["c"] == 8
    )


def _ladder_to_l2(db: sqlite3.Connection, *, l2_ts: str) -> None:
    """手工落 L1→L2 台账（演练脚手架：set_level 会用当下时间戳，历史时点直接插）。"""
    db.execute(
        "INSERT INTO contraction_transitions (level, actor, reason, ts)"
        " VALUES ('L1', 'human:cli', '演练', ?)",
        (l2_ts,),
    )
    db.execute(
        "INSERT INTO contraction_transitions (level, actor, reason, ts)"
        " VALUES ('L2', 'human:cli', '演练', ?)",
        (l2_ts,),
    )


def test_l3l4_timer_not_applicable_below_l2(db: sqlite3.Connection) -> None:
    """B.3 只适用 L2+：L0（genesis）无锚无建议。"""
    from datetime import UTC, datetime

    reading = contraction.l3l4_timer_reading(db, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert reading.status == "not_applicable"
    assert reading.l2_entered_at is None
    assert reading.suggest_to is None


def test_l3l4_timer_due_zero_triggers_suggests_l3(db: sqlite3.Connection) -> None:
    """L2 满 183 日且四信号零触发→建议再降一级（L2→L3，⑥节读数）。"""
    from datetime import UTC, datetime

    _ladder_to_l2(db, l2_ts="2026-04-02T00:00:00+00:00")
    reading = contraction.l3l4_timer_reading(db, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert reading.status == "due_suggest_downgrade"
    assert reading.l2_entered_at == "2026-04-02T00:00:00+00:00"
    assert reading.days == 243  # 04-02 → 12-01
    assert reading.zero_triggers is True
    assert reading.fired_count == 0
    assert reading.suggest_to is ContractionLevel.L3


def test_l3l4_timer_timing_reports_remaining_days(db: sqlite3.Connection) -> None:
    """窗内计时：余天数如实报，不出建议。"""
    from datetime import UTC, datetime

    _ladder_to_l2(db, l2_ts="2026-08-23T00:00:00+00:00")
    reading = contraction.l3l4_timer_reading(db, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert reading.status == "timing"
    assert reading.days == 100
    assert reading.suggest_to is None


def test_l3l4_timer_fired_signal_blocks_suggestion(db: sqlite3.Connection) -> None:
    """锚后有 fired 信号→不出降级建议（重扫唤醒路径优先，prereg C）。"""
    from datetime import UTC, datetime

    _ladder_to_l2(db, l2_ts="2026-04-02T00:00:00+00:00")
    db.execute(
        """
        INSERT INTO rescan_signal_events
            (signal, period, status, detail, evaluated_at)
        VALUES ('league_mix', '2026-08', 'fired', 'PSI=0.3100',
                '2026-09-01T01:35:00+00:00')
        """
    )
    reading = contraction.l3l4_timer_reading(db, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert reading.status == "due_triggers_present"
    assert reading.zero_triggers is False
    assert reading.fired_count == 1
    assert reading.suggest_to is None


def test_l3l4_timer_anchor_before_window_not_counted(db: sqlite3.Connection) -> None:
    """锚前的 fired 旧信号不计入（探针全停期才算触发史）。"""
    from datetime import UTC, datetime

    _ladder_to_l2(db, l2_ts="2026-04-02T00:00:00+00:00")
    db.execute(
        """
        INSERT INTO rescan_signal_events
            (signal, period, status, detail, evaluated_at)
        VALUES ('league_mix', '2026-03', 'fired', 'PSI=0.3100',
                '2026-03-01T01:35:00+00:00')
        """
    )
    reading = contraction.l3l4_timer_reading(db, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert reading.zero_triggers is True
    assert reading.status == "due_suggest_downgrade"


def test_l3l4_timer_reentry_resets_anchor(db: sqlite3.Connection) -> None:
    """L2→L1→L2：锚=最新进入 L2（重升期探针曾恢复，计时重启）。"""
    from datetime import UTC, datetime

    _ladder_to_l2(db, l2_ts="2025-06-01T00:00:00+00:00")
    db.execute(
        "INSERT INTO contraction_transitions (level, actor, reason, ts)"
        " VALUES ('L1', 'human:cli', '重升', '2026-06-01T00:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO contraction_transitions (level, actor, reason, ts)"
        " VALUES ('L2', 'human:cli', '再降', '2026-08-23T00:00:00+00:00')"
    )
    reading = contraction.l3l4_timer_reading(db, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert reading.l2_entered_at == "2026-08-23T00:00:00+00:00"
    assert reading.status == "timing"  # 新锚 100 日，窗未满


def test_l3l4_timer_floor_l4(db: sqlite3.Connection) -> None:
    """L4 到期零触发：已到底，无更降（due_floor）。"""
    from datetime import UTC, datetime

    for level in ("L1", "L2", "L3", "L4"):
        db.execute(
            "INSERT INTO contraction_transitions (level, actor, reason, ts)"
            " VALUES (?, 'human:cli', '演练', '2026-04-02T00:00:00+00:00')",
            (level,),
        )
    reading = contraction.l3l4_timer_reading(db, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert reading.status == "due_floor"
    assert reading.suggest_to is None


def test_l3l4_timer_bad_anchor_ts_not_crashing(db: sqlite3.Connection) -> None:
    """坏锚串防御：ts 非 ISO 读数标 bad_anchor 不炸（_age_hours 同哲学——
    晨检消费本读数，账面完整性不得红掉整个晨检项）。"""
    from datetime import UTC, datetime

    db.execute(
        "INSERT INTO contraction_transitions (level, actor, reason, ts)"
        " VALUES ('L2', 'human:cli', '演练', 'not-a-timestamp')"
    )
    reading = contraction.l3l4_timer_reading(db, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert reading.status == "bad_anchor"
    assert reading.suggest_to is None
    assert "not-a-timestamp" in reading.detail
    # 晨检信号面同走不炸
    faces = contraction.rescan_signals(db, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert next(f for f in faces if f.name == "l3l4_timer").status == "bad_anchor"
