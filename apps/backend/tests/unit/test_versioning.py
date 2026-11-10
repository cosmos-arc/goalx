"""vision-build 票 01：策略版本状态机 + 回滚三触发器。

谓词级钉住三件事：状态转移全表（非法转移拒绝，晋升恒人审）、触发器
数值边界（2SE 恰为 0 / 回撤恰 10% / 恰 50 注窗）、transitions append-only。
引擎消费端 e2e（shadow 态拒单留痕）在文件尾。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from goalx_backend.betting import versioning
from goalx_backend.betting.versioning import StrategyState


def test_transition_full_table() -> None:
    """状态转移全表：恰允许（相邻升降 + 出梯 demoted/retired + 回梯注册）。"""
    allowed = {
        # 梯上相邻一步（升=人审 promote；降=触发器 A/C 或人）
        (StrategyState.REGISTERED, StrategyState.SHADOW),
        (StrategyState.SHADOW, StrategyState.PROBE),
        (StrategyState.PROBE, StrategyState.CANDIDATE),
        (StrategyState.CANDIDATE, StrategyState.LIVE_PAPER),
        (StrategyState.LIVE_PAPER, StrategyState.LIVE_REAL),
        # 梯下相邻一步（自动降级 A/C 的目标面）
        (StrategyState.SHADOW, StrategyState.REGISTERED),
        (StrategyState.PROBE, StrategyState.SHADOW),
        (StrategyState.CANDIDATE, StrategyState.PROBE),
        (StrategyState.LIVE_PAPER, StrategyState.CANDIDATE),
        (StrategyState.LIVE_REAL, StrategyState.LIVE_PAPER),
        # demoted 可再升：唯一回梯口=重新注册（走 L2 人审），不得跳梯
        (StrategyState.DEMOTED, StrategyState.REGISTERED),
    }
    # 出梯：任意梯态（含 registered）→demoted（人降/触发器 B 冻结 live-real）；
    # 任意非 retired 态 →retired（人归档，终态）
    for state in versioning.LADDER:
        allowed.add((state, StrategyState.DEMOTED))
    for state in (*versioning.LADDER, StrategyState.DEMOTED):
        allowed.add((state, StrategyState.RETIRED))
    seen: set[tuple[StrategyState, StrategyState]] = set()
    for from_state in StrategyState:
        for to_state in StrategyState:
            pair = (from_state, to_state)
            seen.add(pair)
            assert versioning.transition_allowed(*pair) == (pair in allowed), pair
    assert seen >= allowed  # 全表覆盖，allowed 里没有写错的状态名


def test_live_states_are_the_two_live_rungs() -> None:
    """消费端闸只放行 live-paper/live-real（票交付 5 的状态面）。"""
    assert (
        frozenset({StrategyState.LIVE_PAPER, StrategyState.LIVE_REAL})
        == versioning.LIVE_STATES
    )


# ---- 表与登记/转移（append-only，trial ledger 同构）----


def test_migration_v50_idempotent(db: sqlite3.Connection) -> None:
    """v50 幂等：两表就位、重跑零效果（同版本重跑=不重复施用）。"""
    from goalx_backend.db import migrate

    assert migrate(db) == 52
    assert migrate(db) == 52  # 同版本重跑零效果
    for table in ("strategy_versions", "version_transitions"):
        assert (
            db.execute(
                "SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()["c"]
            == 1
        )


def _register(db: sqlite3.Connection, suffix: str = "") -> int:
    return versioning.register_version(
        db,
        estimator_version=f"dc-test{suffix}",
        calibration_version=f"shin{suffix}",
        params_version=f"fp{suffix}",
        label=f"auto-v1{suffix}",
    )


def test_register_is_idempotent_on_triple(db: sqlite3.Connection) -> None:
    """三元组=身份：重复注册同三元组返回同 id，不落新行。"""
    first = _register(db)
    again = _register(db)
    assert first == again
    assert db.execute("SELECT COUNT(*) c FROM strategy_versions").fetchone()["c"] == 1
    assert versioning.current_state(db, first) is StrategyState.REGISTERED
    # 重复注册不重复落 genesis 行
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM version_transitions WHERE version_id=?",
            (first,),
        ).fetchone()["c"]
        == 1
    )
    # 初始态=注册行（from_state NULL → registered 的 genesis 转移）
    row = db.execute(
        "SELECT from_state, to_state, actor FROM version_transitions"
        " WHERE version_id=?",
        (first,),
    ).fetchone()
    assert row["from_state"] is None
    assert row["to_state"] == "registered"


def test_transition_writes_row_and_rejects_illegal(db: sqlite3.Connection) -> None:
    """合法转移落 transitions；非法转移抛 InvalidTransition 且零写入。"""
    version_id = _register(db)
    versioning.transition_version(
        db,
        version_id,
        StrategyState.SHADOW,
        actor="human:test",
        reason="shadow 起跑",
    )
    assert versioning.current_state(db, version_id) is StrategyState.SHADOW
    with pytest.raises(versioning.InvalidTransition):
        versioning.transition_version(
            db, version_id, StrategyState.LIVE_PAPER, actor="human:test", reason="跳级"
        )
    # 拒绝的转移不落行
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM version_transitions WHERE version_id=?",
            (version_id,),
        ).fetchone()["c"]
        == 2
    )


def test_unknown_version_transition_fails(db: sqlite3.Connection) -> None:
    with pytest.raises(versioning.UnknownVersion):
        versioning.transition_version(
            db, 999, StrategyState.SHADOW, actor="human:test", reason="无此版本"
        )


def test_transitions_and_versions_are_append_only(db: sqlite3.Connection) -> None:
    """历史不可改写：UPDATE/DELETE 被库层触发器拒绝（v29/v49 同构）。"""
    version_id = _register(db)
    for statement in (
        "DELETE FROM strategy_versions",
        "UPDATE strategy_versions SET label='x'",
        "DELETE FROM version_transitions",
        "UPDATE version_transitions SET reason='x'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(statement)
    assert versioning.current_state(db, version_id) is StrategyState.REGISTERED


# ---- 回滚触发器（确定性代码读指标；数字=配置常量；边界严格不等）----


def test_trigger_a_constants_are_config() -> None:
    """数字进配置常量（月读可调=改代码走 L2）：初始默认 50 注窗 / 2SE 门。"""
    assert versioning.TRIGGER_A_WINDOW == 50
    assert versioning.TRIGGER_A_SE_GATE == 2.0


def test_trigger_a_exactly_50_bet_window() -> None:
    """恰 50 注窗：49 注恒不评估；第 50 注（显著负）才触发。"""
    clvs = [-0.02] * 49
    assert not versioning.clv_window_fired(clvs)  # 不足窗（哪怕值面全负）
    assert versioning.clv_window_fired([*clvs, -0.02])  # 恰 50：零方差负均值触发


def test_trigger_a_two_se_margin_strict() -> None:
    """
    2SE 边界严格：均值低出 0 恰 2SE（mean+2SE 恰为 0）→ 不触发。

    全等窗（se 恰 0.0）给精确浮点刀口：0 均值恰界不触发；越过（任意负
    均值+零方差）触发。非退化窗用独立手算（与实现不同算路）验证显著门。
    """
    assert not versioning.clv_window_fired([0.0] * 50)  # 均值恰 0：严格 <
    assert versioning.clv_window_fired([-0.02] * 50)  # se=0，均值<0 即越 2SE
    # 非退化窗：25×(-0.09) + 25×(+0.01) → 均值 -0.04、总体 se=0.05/√50≈0.00707
    # → mean+2se ≈ -0.0259 < 0 显著为负 → 触发
    assert versioning.clv_window_fired([-0.09] * 25 + [0.01] * 25)
    # 均值为负但不显著：25×(-0.001)+25×(+0.049) → 均值 +0.024 为正不触发；
    # 反向构造 25×(-0.049)+25×(+0.001) → 均值 -0.024、se≈0.00707
    # → mean+2se ≈ -0.0099 < 0 → 触发（显著性门以 2SE 计，不以方向计）
    assert versioning.clv_window_fired([-0.049] * 25 + [0.001] * 25)
    # 均值为正的窗永不触发（方向门）
    assert not versioning.clv_window_fired([0.02] * 25 + [-0.01] * 25)


def _seed_settled_bet(
    db: sqlite3.Connection,
    *,
    clv: float,
    profit: float,
    settled_at: str,
    strategy: str = "auto-v1",
) -> None:
    db.execute(
        """
        INSERT INTO bets (mode, market_kind, stake, created_at, status, payout,
                          profit, settled_at, strategy_version, clv)
        VALUES ('paper', 'fixed', 2.0, ?, 'won', 0.0, ?, ?, ?, ?)
        """,
        (settled_at, profit, settled_at, strategy, clv),
    )


def test_rolling_clv_window_reads_last_settled_auto_bets(
    db: sqlite3.Connection,
) -> None:
    """A 的读数面：已结算 auto 纸面注 CLV，时间序取最近 50 注（升序返回）。"""
    for i in range(3):
        _seed_settled_bet(
            db,
            clv=-0.01 - i / 100,
            profit=1.0,
            settled_at=f"2026-11-0{i + 1}T00:00:00+00:00",
        )
    # 干扰行不进窗：非 auto（无 strategy_version）、未结算（clv NULL）、live 模式
    db.execute(
        """
        INSERT INTO bets (mode, market_kind, stake, created_at, status, profit,
                          settled_at, strategy_version, clv)
        VALUES ('paper', 'fixed', 2.0, '2026-11-04', 'won', 1.0,
                '2026-11-04T00:00:00+00:00', NULL, -0.5)
        """
    )
    db.execute(
        """
        INSERT INTO bets (mode, market_kind, stake, created_at, status, profit,
                          settled_at, strategy_version, clv)
        VALUES ('paper', 'fixed', 2.0, '2026-11-05', 'won', 1.0,
                '2026-11-05T00:00:00+00:00', 'manual', NULL)
        """
    )
    db.execute(
        """
        INSERT INTO bets (mode, market_kind, stake, created_at, status)
        VALUES ('live', 'fixed', 2.0, '2026-11-06', 'open')
        """
    )
    window = versioning.rolling_clv_window(db)
    assert window == pytest.approx([-0.01, -0.02, -0.03])  # 升序=时间序


def test_trigger_b_drawdown_exactly_ten_percent() -> None:
    """回撤恰 10% 不触发（严格 >）；越过触发；无高点回撤不触发。"""
    assert versioning.TRIGGER_B_DRAWDOWN == 0.10
    # 高点 1100 → 现值 990：恰 10% → 不触发
    assert not versioning.drawdown_fired([1000.0, 1100.0, 990.0])
    # 现值 989：回撤 111/1100 >10% → 触发
    assert versioning.drawdown_fired([1000.0, 1100.0, 989.0])
    # 现值即高点：零回撤
    assert not versioning.drawdown_fired([1000.0, 900.0, 1200.0])
    assert not versioning.drawdown_fired([])


def test_bankroll_history_tracks_high_water(db: sqlite3.Connection) -> None:
    """B 的读数面：初始资金 + 逐笔已结算 paper P&L 累计（settled_at 升序）。"""
    _seed_settled_bet(db, clv=0.0, profit=100.0, settled_at="2026-11-01T00:00:00+00:00")
    _seed_settled_bet(db, clv=0.0, profit=-50.0, settled_at="2026-11-02T00:00:00+00:00")
    # 未结算 profit 行不计入
    db.execute(
        "INSERT INTO bets (mode, market_kind, stake, created_at, status)"
        " VALUES ('paper', 'fixed', 2.0, '2026-11-03', 'open')"
    )
    history = versioning.bankroll_history(db, initial_bankroll=1000.0)
    assert history == pytest.approx([1000.0, 1100.0, 1050.0])


def test_trigger_c_exactly_two_se() -> None:
    """C=注册校准指标越 2SE→降级：恰 2SE 不触发（严格 >），双向越界都算。"""
    assert versioning.TRIGGER_C_SE_GATE == 2.0
    assert not versioning.calibration_drift_fired(0.02, 0.01)  # 恰 2SE
    assert not versioning.calibration_drift_fired(-0.02, 0.01)  # 恰 −2SE
    assert versioning.calibration_drift_fired(0.0201, 0.01)  # 越界
    assert versioning.calibration_drift_fired(-0.0201, 0.01)
    assert not versioning.calibration_drift_fired(0.01, 0.01)  # 1SE 界内
    assert not versioning.calibration_drift_fired(0.0, 0.01)  # 无漂移


# ---- 触发器编排：自动降级落 transitions（actor=trigger:* 可审计）----


def _promote_to(db: sqlite3.Connection, version_id: int, state: StrategyState) -> None:
    """人审逐级晋升到目标态（测试脚手架；生产只经 CLI）。"""
    for target in versioning.LADDER[1 : versioning.LADDER.index(state) + 1]:
        versioning.transition_version(
            db, version_id, target, actor="human:test", reason="逐级晋升"
        )


def test_apply_triggers_a_demotes_one_level_with_audit_row(
    db: sqlite3.Connection,
) -> None:
    """A 触发：live-paper 自动降一级→candidate，transitions 落 actor=trigger:A。"""
    version_id = _register(db)
    _promote_to(db, version_id, StrategyState.LIVE_PAPER)
    for i in range(versioning.TRIGGER_A_WINDOW):
        _seed_settled_bet(
            db,
            clv=-0.02,
            profit=1.0,
            settled_at=f"2026-11-01T00:{i % 60:02d}:00+00:00",
        )
    result = versioning.apply_rollback_triggers(db, initial_bankroll=1000.0)
    assert result.fired["A"] is True
    assert versioning.current_state(db, version_id) is StrategyState.CANDIDATE
    row = db.execute(
        "SELECT actor, reason, from_state, to_state FROM version_transitions"
        " WHERE version_id=? ORDER BY id DESC LIMIT 1",
        (version_id,),
    ).fetchone()
    assert row["actor"] == "trigger:A"
    assert row["from_state"] == "live-paper"
    assert row["to_state"] == "candidate"
    assert "clv" in row["reason"]


def test_apply_triggers_b_freezes_live_real_and_warns_paper(
    db: sqlite3.Connection,
) -> None:
    """B 触发：live-real 全冻结→demoted；live-paper 不动（纸面态仅告警）。"""
    real_id = _register(db, suffix="-real")
    paper_id = _register(db, suffix="-paper")
    _promote_to(db, real_id, StrategyState.LIVE_REAL)
    _promote_to(db, paper_id, StrategyState.LIVE_PAPER)
    # 水位 1000→1100（高点）→900：回撤 200/1100≈18% >10%
    _seed_settled_bet(db, clv=0.0, profit=100.0, settled_at="2026-11-01T00:00:00+00:00")
    _seed_settled_bet(
        db, clv=0.0, profit=-200.0, settled_at="2026-11-02T00:00:00+00:00"
    )
    result = versioning.apply_rollback_triggers(db, initial_bankroll=1000.0)
    assert result.fired["B"] is True
    assert versioning.current_state(db, real_id) is StrategyState.DEMOTED
    assert versioning.current_state(db, paper_id) is StrategyState.LIVE_PAPER
    row = db.execute(
        "SELECT actor, to_state FROM version_transitions"
        " WHERE version_id=? ORDER BY id DESC LIMIT 1",
        (real_id,),
    ).fetchone()
    assert row["actor"] == "trigger:B"
    assert row["to_state"] == "demoted"


def test_apply_triggers_c_demotes_via_calibration_reading(
    db: sqlite3.Connection,
) -> None:
    """C 触发：校准读数越 2SE→live 版本降一级（actor=trigger:C）。"""
    version_id = _register(db)
    _promote_to(db, version_id, StrategyState.LIVE_PAPER)
    result = versioning.apply_rollback_triggers(
        db, initial_bankroll=1000.0, calibration_reading=(0.05, 0.01)
    )
    assert result.fired["C"] is True
    assert versioning.current_state(db, version_id) is StrategyState.CANDIDATE
    row = db.execute(
        "SELECT actor, reason FROM version_transitions"
        " WHERE version_id=? ORDER BY id DESC LIMIT 1",
        (version_id,),
    ).fetchone()
    assert row["actor"] == "trigger:C"
    assert "global-reading" in row["reason"]


def test_apply_triggers_a_spares_non_live_versions(db: sqlite3.Connection) -> None:
    """
    F5 回归：全局 CLV 窗只降 live 版本——未下注的 shadow 不连坐。

    窗口由 live 版本的下注产生；shadow/probe 版本被全局读数降级是归因
    污染。reason 标 global-window 口径。
    """
    live_id = _register(db, suffix="-live")
    shadow_id = _register(db, suffix="-shadow")
    _promote_to(db, live_id, StrategyState.LIVE_PAPER)
    versioning.transition_version(
        db, shadow_id, StrategyState.SHADOW, actor="human:test", reason="影子"
    )
    for i in range(versioning.TRIGGER_A_WINDOW):
        _seed_settled_bet(
            db,
            clv=-0.05,
            profit=-1.0,
            settled_at=f"2026-11-01T00:{i % 60:02d}:00+00:00",
        )
    result = versioning.apply_rollback_triggers(db, initial_bankroll=1000.0)
    assert result.fired["A"] is True
    assert [demotion.version_id for demotion in result.demotions] == [live_id]
    assert versioning.current_state(db, live_id) is StrategyState.CANDIDATE
    assert versioning.current_state(db, shadow_id) is StrategyState.SHADOW
    reason_row = db.execute(
        "SELECT reason FROM version_transitions WHERE version_id=?"
        " ORDER BY id DESC LIMIT 1",
        (live_id,),
    ).fetchone()
    assert "global-window" in reason_row["reason"]


def test_apply_triggers_healthy_account_is_noop(db: sqlite3.Connection) -> None:
    """健康读数：零触发零写入（永无自动晋升——好指标也不升态）。"""
    version_id = _register(db)
    _promote_to(db, version_id, StrategyState.SHADOW)
    for i in range(versioning.TRIGGER_A_WINDOW):
        _seed_settled_bet(
            db,
            clv=0.01,
            profit=2.0,
            settled_at=f"2026-11-01T00:{i % 60:02d}:00+00:00",
        )
    before = db.execute("SELECT COUNT(*) c FROM version_transitions").fetchone()["c"]
    result = versioning.apply_rollback_triggers(db, initial_bankroll=1000.0)
    assert result.fired == {"A": False, "B": False, "C": False}
    assert result.demotions == []
    after = db.execute("SELECT COUNT(*) c FROM version_transitions").fetchone()["c"]
    assert before == after
    assert versioning.current_state(db, version_id) is StrategyState.SHADOW


def test_auto_actor_cannot_promote(db: sqlite3.Connection) -> None:
    """永无自动晋升（代码级哨）：trigger actor 的晋升转移被拒且零写入。"""
    version_id = _register(db)
    with pytest.raises(versioning.InvalidTransition, match="不得晋升"):
        versioning.transition_version(
            db,
            version_id,
            StrategyState.SHADOW,
            actor="trigger:A",
            reason="自动路径越权",
        )
    assert versioning.current_state(db, version_id) is StrategyState.REGISTERED
    # demoted 回梯注册同属晋升：自动 actor 也禁（回梯唯一口=人审 L2）
    versioning.transition_version(
        db, version_id, StrategyState.DEMOTED, actor="human:test", reason="出梯"
    )
    with pytest.raises(versioning.InvalidTransition, match="不得晋升"):
        versioning.transition_version(
            db,
            version_id,
            StrategyState.REGISTERED,
            actor="trigger:C",
            reason="自动回梯越权",
        )
    assert versioning.current_state(db, version_id) is StrategyState.DEMOTED


def test_apply_triggers_skip_off_ladder_and_bottom_rungs(
    db: sqlite3.Connection,
) -> None:
    """A 触发时：demoted/retired（出梯）与 registered（梯底）都不动。"""
    demoted_id = _register(db, suffix="-dm")
    versioning.transition_version(
        db, demoted_id, StrategyState.DEMOTED, actor="human:test", reason="出梯"
    )
    bottom_id = _register(db, suffix="-bottom")  # 停在 registered
    retired_id = _register(db, suffix="-rt")
    versioning.transition_version(
        db, retired_id, StrategyState.RETIRED, actor="human:test", reason="归档"
    )
    for i in range(versioning.TRIGGER_A_WINDOW):
        _seed_settled_bet(
            db,
            clv=-0.05,
            profit=-1.0,
            settled_at=f"2026-11-01T00:{i % 60:02d}:00+00:00",
        )
    result = versioning.apply_rollback_triggers(db, initial_bankroll=1000.0)
    assert result.fired["A"] is True
    assert [demotion.version_id for demotion in result.demotions] == []
    assert versioning.current_state(db, demoted_id) is StrategyState.DEMOTED
    assert versioning.current_state(db, bottom_id) is StrategyState.REGISTERED
    assert versioning.current_state(db, retired_id) is StrategyState.RETIRED
    assert versioning.current_state(db, 999) is None  # 未知版本=None 非 raise


def test_transition_check_then_act_race_keeps_chain_continuous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    F1 回归：检查与落账同事务——竞争写者不得造成 from_state 断链。

    竞争注入=谓词求值时另一连接抢写一步合法转移（check 与 INSERT 之间
    的窗口）。修复后（检查在 BEGIN IMMEDIATE 写锁内）另一连接的写被
    锁挡下（OperationalError）或转移被拒——无论哪条路，账本 from_state
    链恒连续；修复前会落出 live-real 之后再 from live-paper 的断链行。
    """
    from goalx_backend.db import connect as db_connect
    from goalx_backend.db import migrate, utc_now_iso

    path = tmp_path / "race.db"
    conn = db_connect(path)
    other = db_connect(path)
    try:
        migrate(conn)
        version_id = versioning.register_version(
            conn,
            estimator_version="dc-race",
            calibration_version="shin",
            params_version="fp-race",
            label="auto-race",
        )
        _promote_to(conn, version_id, StrategyState.LIVE_PAPER)
        other.execute("PRAGMA busy_timeout=200")  # 竞争写者快速失败
        real_allowed = versioning.transition_allowed
        armed: list[bool] = []

        def racing_allowed(from_state: StrategyState, to_state: StrategyState) -> bool:
            if not armed:  # 一次性注入：模拟检查与落账之间的并发写
                armed.append(True)
                other.execute(
                    """
                    INSERT INTO version_transitions
                        (version_id, from_state, to_state, reason, actor, ts)
                    VALUES (?, 'live-paper', 'live-real', '竞争转移',
                            'human:race', ?)
                    """,
                    (version_id, utc_now_iso()),
                )
                other.commit()
            return real_allowed(from_state, to_state)

        monkeypatch.setattr(versioning, "transition_allowed", racing_allowed)
        with pytest.raises((versioning.InvalidTransition, sqlite3.OperationalError)):
            versioning.transition_version(
                conn,
                version_id,
                StrategyState.CANDIDATE,
                actor="human:test",
                reason="降级",
            )
        monkeypatch.setattr(versioning, "transition_allowed", real_allowed)
        rows = conn.execute(
            "SELECT from_state, to_state FROM version_transitions"
            " WHERE version_id=? ORDER BY id",
            (version_id,),
        ).fetchall()
        assert all(
            rows[i]["from_state"] == rows[i - 1]["to_state"]
            for i in range(1, len(rows))
        ), "账本 from_state 链断——检查与落账间有未加锁窗口"
        # 竞争写者被锁挡下时主连接事务回滚：现态停在 live-paper
        assert versioning.current_state(conn, version_id) is StrategyState.LIVE_PAPER
    finally:
        conn.close()
        other.close()


def test_drawdown_degenerate_peak_not_evaluated() -> None:
    """退化水位（高点 ≤0，账户亏穿零）不评估除零面。"""
    assert not versioning.drawdown_fired([-100.0, -50.0])


# ---- 人审 CLI（register / promote；晋升恒人审的唯一写路径）----


def test_cli_version_register_and_promote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 三件套接线：注册幂等落账；晋升写 transitions；非法转移 fail-loud。"""
    from goalx_backend.cli import _cmd_version_promote, _cmd_version_register
    from goalx_backend.config import Settings
    from goalx_backend.db import connect as db_connect
    from goalx_backend.db import migrate

    settings = Settings(db_path=tmp_path / "goalx.db")
    conn = db_connect(settings.db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    _cmd_version_register(
        argparse.Namespace(
            estimator="dc-abc123",
            calibration="shin",
            params="fp-001",
            label="auto-v2",
        ),
        settings=settings,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == 1
    assert payload["state"] == "registered"
    assert payload["label"] == "auto-v2"

    # F4：幂等命中回显库内首注册 label（后到 --label 不改写既有行）
    _cmd_version_register(
        argparse.Namespace(
            estimator="dc-abc123",
            calibration="shin",
            params="fp-001",
            label="auto-v2-relabeled",
        ),
        settings=settings,
    )
    again = json.loads(capsys.readouterr().out)
    assert again["id"] == 1
    assert again["label"] == "auto-v2"

    _cmd_version_promote(
        argparse.Namespace(id=1, to="shadow", reason="影子起跑"),
        settings=settings,
    )
    promoted = json.loads(capsys.readouterr().out)
    assert promoted["to_state"] == "shadow"
    assert promoted["actor"].startswith("human")

    # 非法转移（跳级）fail-loud：人审 CLI 不静默吞谓词拒绝
    with pytest.raises(versioning.InvalidTransition):
        _cmd_version_promote(
            argparse.Namespace(id=1, to="live-real", reason="跳级"),
            settings=settings,
        )


# ---- 消费端闸与参数指纹（auto_strategy 最小接线）----


def test_params_fingerprint_is_content_addressed() -> None:
    """参数指纹=内容寻址：同参同印；改参换印（改参数=升版本）。"""
    from dataclasses import replace

    from goalx_backend.betting.auto_strategy import AUTO_V1_PARAMS

    base = versioning.params_fingerprint(AUTO_V1_PARAMS)
    assert base == versioning.params_fingerprint(AUTO_V1_PARAMS)
    changed = replace(AUTO_V1_PARAMS, top_singles=AUTO_V1_PARAMS.top_singles + 1)
    assert versioning.params_fingerprint(changed) != base


def test_engine_gate_semantics(db: sqlite3.Connection) -> None:
    """
    消费端闸（F2/F3 语义）：全三元组匹配；非 live 态拒；未注册同拒
    （fail-closed——未注册=非 live 态=拒单留痕）；live-paper 放行。
    """
    gate = versioning.engine_gate(
        db,
        estimator_version="dc-g",
        calibration_version="shin",
        params_version="fp-gate",
    )
    assert not gate.allowed  # F3：未注册不放行
    assert gate.reason == "version_unregistered"
    assert gate.state is None

    version_id = versioning.register_version(
        db,
        estimator_version="dc-g",
        calibration_version="shin",
        params_version="fp-gate",
        label="auto-g",
    )
    gate = versioning.engine_gate(
        db,
        estimator_version="dc-g",
        calibration_version="shin",
        params_version="fp-gate",
    )
    assert not gate.allowed
    assert gate.reason == "version_not_live"
    assert gate.state is StrategyState.REGISTERED

    ladder = list(versioning.LADDER)
    top = ladder.index(StrategyState.LIVE_PAPER)
    for target in ladder[ladder.index(StrategyState.SHADOW) : top + 1]:
        versioning.transition_version(
            db, version_id, target, actor="human:test", reason="逐级"
        )
    gate = versioning.engine_gate(
        db,
        estimator_version="dc-g",
        calibration_version="shin",
        params_version="fp-gate",
    )
    assert gate.allowed
    assert gate.state is StrategyState.LIVE_PAPER
    assert gate.version_id == version_id


def test_engine_gate_full_triple_no_cross_estimator_leak(
    db: sqlite3.Connection,
) -> None:
    """
    F2 回归：同参数指纹不同估计器的版本不互借 live 态。

    (E1,C,P) 升 live-paper 后，(E2,C,P)（registered）按 E2 查闸必须拒
    ——单维 params 匹配会让未过梯的 E2 借 E1 的 live 放行。
    """
    live_id = versioning.register_version(
        db,
        estimator_version="E1",
        calibration_version="C",
        params_version="P",
        label="auto-x",
    )
    versioning.register_version(
        db,
        estimator_version="E2",
        calibration_version="C",
        params_version="P",
        label="auto-y",
    )
    for target in (
        StrategyState.SHADOW,
        StrategyState.PROBE,
        StrategyState.CANDIDATE,
        StrategyState.LIVE_PAPER,
    ):
        versioning.transition_version(
            db, live_id, target, actor="human:test", reason="逐级"
        )
    leaked = versioning.engine_gate(
        db, estimator_version="E2", calibration_version="C", params_version="P"
    )
    assert not leaked.allowed
    assert leaked.reason == "version_not_live"
    assert leaked.state is StrategyState.REGISTERED
    # E1 自身照常放行
    assert versioning.engine_gate(
        db, estimator_version="E1", calibration_version="C", params_version="P"
    ).allowed
