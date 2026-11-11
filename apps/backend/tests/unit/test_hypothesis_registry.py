"""vision-build 票 02：假设注册表（prereg 纪律的库表表达）。

谓词级钉住五件事：H1-H8 首批冻结入表（措辞=research/61 §二原文，可复算；
台账回指=edge-ledger 判决条目）、append-only 触发器（冻结字段不可改写、
refuted 留痕不删）、状态机（registered→running→supported|refuted，终态
翻转恒人审）、attempt 计数（同窗幂等、同族递增、终态停计）、S1→H1 扫描器
挂钩与周报注册表视图。人 CLI 终态 e2e 在文件尾。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from goalx_backend.evaluation import hypothesis_registry as hreg
from goalx_backend.evaluation.hypothesis_registry import HypothesisState

# research/61 §二（L39-46）原文冻结：h_id → (可证伪措辞, 数据面, 门)。
# 台账回指=edge-ledger 判决条目（E-xx）；预期样本量=门中的样本量子句。
_FROZEN_H1_H8: dict[str, dict[str, str]] = {
    "H1": {
        "statement": (
            "JC 收盘价保留 FLB：JC 长赔向实现 ROI 显著低于欧收盘同向（侵蚀差>2pp）"
        ),
        "data_face": "srct 收盘轨迹×JC SP×结果",
        "judgement": "n≥500/向，差>2SE",
        "expected_n": "n≥500/向",
        "ledger_ref": "edge-ledger:E-02",
    },
    "H2": {
        "statement": "JC 池终局份额低估热门（O-S 理论）：份额校准偏差方向=热门被低估",
        "data_face": "源B 849 行份额×结果",
        "judgement": "n≥200",
        "expected_n": "n≥200",
        "ledger_ref": "edge-ledger:E-23",
    },
    "H3": {
        "statement": "平局低买：池人群平局份额 < 模型/市场公允，平局命中派彩超比例",
        "data_face": "同上",
        "judgement": "n≥200，偏差>2SE",
        "expected_n": "n≥200",
        "ledger_ref": "edge-ledger:E-24",
    },
    "H4": {
        "statement": "JC 调盘滞后窗存在：欧盘动后 JC 跟随延迟>15min 的案例率稳定>10%",
        "data_face": "srct 轨迹×JC 快照",
        "judgement": "滚动 30 日",
        "expected_n": "滚动 30 日",
        "ledger_ref": "edge-ledger:E-08",
    },
    "H5": {
        "statement": "早锁优于晚锁（我方 drift 结论形式化）：早期信号 CLV>收盘锚 CLV",
        "data_face": "纸面注×收盘锚",
        "judgement": "n≥200/组",
        "expected_n": "n≥200/组",
        "ledger_ref": "edge-ledger:E-17",
    },
    "H6": {
        "statement": "LLM 情报先行：情报事件时点先于 JC 调盘案例率>0 且方向一致>55%",
        "data_face": "情报层×调盘账",
        "judgement": "n≥100",
        "expected_n": "n≥100",
        "ledger_ref": "edge-ledger:E-12",
    },
    "H7": {
        "statement": "滚存期 EV 结构：滚存期期望返奖率高于非滚存期（死钱注入）",
        "data_face": "pool_periods×draw_results",
        "judgement": "全量回放",
        "expected_n": "全量回放",
        "ledger_ref": "edge-ledger:E-25",
    },
    "H8": {
        "statement": "市场桶结构当代漂移：十年桶偏差（FLB）在 2026 样本仍成立",
        "data_face": "运行面积累",
        "judgement": "滚动 90 日",
        "expected_n": "滚动 90 日",
        "ledger_ref": "edge-ledger:E-01",
    },
}

# 首批状态：关联扫描器已在日扫（s1/s4/s5 ∈ ACTIVE_SCANNERS）→ running；
# S3/S6 未建成（H5/H6）→ registered。
_RUNNING = ("H1", "H2", "H3", "H4", "H7", "H8")
_REGISTERED = ("H5", "H6")


def test_migration_v51_idempotent(db: sqlite3.Connection) -> None:
    """v51 幂等：两表就位、H1-H8 种子恰八行、重跑零效果。"""
    from goalx_backend.db import migrate

    assert migrate(db) == 59
    assert migrate(db) == 59  # 同版本重跑零效果
    assert db.execute("SELECT COUNT(*) FROM hypothesis_registry").fetchone()[0] == 8
    assert db.execute("SELECT COUNT(*) FROM hypothesis_attempts").fetchone()[0] == 0


def test_h1_h8_frozen_wording_recomputable(db: sqlite3.Connection) -> None:
    """H1-H8 措辞冻结可复算：五字段逐一=research/61 §二原文（验收 1）。"""
    rows = {
        str(r["h_id"]): r
        for r in db.execute(
            """
            SELECT h_id, family, statement, data_face, judgement, expected_n,
                   ledger_ref, frozen_at, first_registered_at
            FROM hypothesis_registry
            """
        )
    }
    assert set(rows) == set(_FROZEN_H1_H8)
    for h_id, frozen in _FROZEN_H1_H8.items():
        row = rows[h_id]
        for column, expected in frozen.items():
            assert row[column] == expected, (h_id, column)
        assert row["family"] == h_id  # 首族注册：family=h_id、attempt 继承基线
        assert row["frozen_at"] == "2026-11-07"  # research/61 落稿日=措辞冻结日
        assert row["first_registered_at"]  # 入库时间戳非空（seed 落库时刻）


def test_seed_statuses_by_scanner_liveness(db: sqlite3.Connection) -> None:
    """首批状态：s1/s4/s5 关联假设 running；S3/S6 未建（H5/H6）registered。"""
    statuses = {
        str(r["h_id"]): str(r["status"])
        for r in db.execute("SELECT h_id, status FROM hypothesis_registry")
    }
    assert {h: statuses[h] for h in _RUNNING} == dict.fromkeys(_RUNNING, "running")
    assert {h: statuses[h] for h in _REGISTERED} == dict.fromkeys(
        _REGISTERED, "registered"
    )
    # 注册即未尝试：seed attempt_count=0（统计评估落账才计数）
    assert (
        db.execute(
            "SELECT COUNT(*) FROM hypothesis_registry WHERE attempt_count != 0"
        ).fetchone()[0]
        == 0
    )
    assert hreg.current_status(db, "H1") is HypothesisState.RUNNING
    assert hreg.current_status(db, "H5") is HypothesisState.REGISTERED


def test_registry_append_only_trigger_contract(db: sqlite3.Connection) -> None:
    """注册即冻结（不变量 1）：DELETE 全禁、冻结字段不可改写、refuted 留痕。"""
    for statement in (
        "DELETE FROM hypothesis_registry",
        "UPDATE hypothesis_registry SET statement='换措辞'",
        "UPDATE hypothesis_registry SET judgement='挪门'",
        "UPDATE hypothesis_registry SET h_id='H1x'",
        "UPDATE hypothesis_registry SET frozen_at='2020-01-01'",
        "UPDATE hypothesis_registry SET attempt_count = attempt_count - 1",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statement)
    # 生命周期列是唯一可变面：status 翻转与 attempt 递增直达 UPDATE 面
    db.execute("UPDATE hypothesis_registry SET status='refuted' WHERE h_id='H1'")
    db.execute(
        "UPDATE hypothesis_registry SET attempt_count = attempt_count + 1"
        " WHERE h_id='H1'"
    )
    db.commit()
    # refuted 留痕不删：终态行仍拒绝 DELETE（不变量 2 前半）
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM hypothesis_registry WHERE h_id='H1'")
    row = db.execute(
        "SELECT status, attempt_count FROM hypothesis_registry WHERE h_id='H1'"
    ).fetchone()
    assert (row["status"], row["attempt_count"]) == ("refuted", 1)


def test_attempts_table_is_append_only(db: sqlite3.Connection) -> None:
    """评估留痕 append-only（v50 同款全禁）：attempts 是账不是状态。"""
    db.execute(
        """
        INSERT INTO hypothesis_attempts (h_id, window_key, scanner, note, ts)
        VALUES ('H1', 's1:2026-11-09', 's1', '{}', '2026-11-09T16:00:00+00:00')
        """
    )
    db.commit()
    for statement in (
        "UPDATE hypothesis_attempts SET note='x'",
        "DELETE FROM hypothesis_attempts",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statement)


# ---- 状态机（registered→running→supported|refuted；终态恒人审）----


def test_transition_full_table() -> None:
    """转移全表：恰允许点亮/进终态；终态不翻案（复出/互翻皆拒）。"""
    allowed = {
        (HypothesisState.REGISTERED, HypothesisState.RUNNING),
        (HypothesisState.REGISTERED, HypothesisState.SUPPORTED),
        (HypothesisState.REGISTERED, HypothesisState.REFUTED),
        (HypothesisState.RUNNING, HypothesisState.SUPPORTED),
        (HypothesisState.RUNNING, HypothesisState.REFUTED),
    }
    for from_state in HypothesisState:
        for to_state in HypothesisState:
            pair = (from_state, to_state)
            assert hreg.transition_allowed(*pair) == (pair in allowed), pair


def test_terminal_flip_requires_human_actor(db: sqlite3.Connection) -> None:
    """交付 3：程序只可点亮 registered→running；终态翻转非 human actor 拒。"""
    # H5=registered：程序（扫描器数据面点亮）可翻 running
    hreg.transition_hypothesis(
        db, "H5", HypothesisState.RUNNING, actor="scanner:s3", reason="S3 首窗留痕"
    )
    assert hreg.current_status(db, "H5") is HypothesisState.RUNNING
    # 同族复盘语义：回 registered 再试程序终态翻转（agent 不自动定生死）
    db.execute("UPDATE hypothesis_registry SET status='registered' WHERE h_id='H5'")
    db.commit()
    with pytest.raises(hreg.InvalidTransition, match="人审"):
        hreg.transition_hypothesis(
            db, "H5", HypothesisState.REFUTED, actor="scanner:s3", reason="越门"
        )
    with pytest.raises(hreg.InvalidTransition, match="人审"):
        hreg.transition_hypothesis(
            db, "H1", HypothesisState.SUPPORTED, actor="agent:loop", reason="建议"
        )
    assert hreg.current_status(db, "H5") is HypothesisState.REGISTERED
    assert hreg.current_status(db, "H1") is HypothesisState.RUNNING


def test_terminal_is_terminal_and_human_can_flip(db: sqlite3.Connection) -> None:
    """终态不翻案（重开=新 h_id）；人 CLI 终态落账带审计三件套。"""
    hreg.transition_hypothesis(
        db, "H7", HypothesisState.REFUTED, actor="human", reason="门内 refuted"
    )
    assert hreg.current_status(db, "H7") is HypothesisState.REFUTED
    for back in (HypothesisState.RUNNING, HypothesisState.SUPPORTED):
        with pytest.raises(hreg.InvalidTransition):
            hreg.transition_hypothesis(db, "H7", back, actor="human", reason="翻案尝试")
    row = db.execute(
        """
        SELECT status_actor, status_reason, status_changed_at
        FROM hypothesis_registry WHERE h_id='H7'
        """
    ).fetchone()
    assert row["status_actor"] == "human"
    assert row["status_reason"] == "门内 refuted"
    assert row["status_changed_at"]


def test_unknown_hypothesis_fails_loud(db: sqlite3.Connection) -> None:
    """未注册 h_id 的翻转/计数 fail-loud（不自静默造行）。"""
    with pytest.raises(hreg.UnknownHypothesis):
        hreg.transition_hypothesis(
            db, "H99", HypothesisState.REFUTED, actor="human", reason="无此假设"
        )
    with pytest.raises(hreg.UnknownHypothesis):
        hreg.record_attempt(db, "H99", window_key="s1:2026-11-09")


# ---- attempt 计数（交付 5：同窗幂等、同族递增、终态停计）----


def test_record_attempt_idempotent_per_window(db: sqlite3.Connection) -> None:
    """同窗重扫不重复计数（UNIQUE 吸收）；新窗递增；attempts 落留痕。"""
    assert hreg.attempt_count(db, "H1") == 0
    assert hreg.record_attempt(db, "H1", window_key="s1:2026-11-09") is True
    assert hreg.record_attempt(db, "H1", window_key="s1:2026-11-09") is False  # 同窗
    assert hreg.attempt_count(db, "H1") == 1
    assert hreg.record_attempt(db, "H1", window_key="s1:2026-11-10") is True
    assert hreg.attempt_count(db, "H1") == 2
    rows = db.execute(
        "SELECT window_key FROM hypothesis_attempts WHERE h_id='H1' ORDER BY id"
    ).fetchall()
    assert [str(r["window_key"]) for r in rows] == ["s1:2026-11-09", "s1:2026-11-10"]


def test_record_attempt_stops_at_terminal(db: sqlite3.Connection) -> None:
    """终态停计：refuted 后扫描器照跑但不再给死假设记尝试（重开=新 h_id）。"""
    hreg.transition_hypothesis(
        db, "H8", HypothesisState.REFUTED, actor="human", reason="当代样本推翻"
    )
    assert (
        hreg.record_attempt(db, "H8", window_key="s1:2026-11-09", scanner="s1") is False
    )
    assert hreg.attempt_count(db, "H8") == 0
    assert (
        db.execute(
            "SELECT COUNT(*) FROM hypothesis_attempts WHERE h_id='H8'"
        ).fetchone()[0]
        == 0
    )


def test_register_hypothesis_family_inherits_attempts(db: sqlite3.Connection) -> None:
    """改预测=新 h_id：同族新行继承家族计数（DSR/BH 分母=家族总尝试）。"""
    for day in ("09", "10", "11"):
        hreg.record_attempt(db, "H1", window_key=f"s1:2026-11-{day}")
    hreg.transition_hypothesis(
        db, "H1", HypothesisState.REFUTED, actor="human", reason="反转翻案后收窄重注"
    )
    new_id = hreg.register_hypothesis(
        db,
        h_id="H1b",
        statement="H1 收窄版：侵蚀差只在 p<0.20 带成立（冻结措辞）",
        judgement="n≥500/向，差>2SE",
        data_face="srct 收盘轨迹×JC SP×结果",
        expected_n="n≥500/向",
        ledger_ref="edge-ledger:E-02",
        family="H1",
    )
    assert new_id > 0
    assert hreg.attempt_count(db, "H1b") == 3  # 家族继承，非归零
    assert hreg.current_status(db, "H1b") is HypothesisState.REGISTERED
    # 新行首个尝试=家族第 4 次
    assert hreg.record_attempt(db, "H1b", window_key="s1:2026-11-12") is True
    assert hreg.attempt_count(db, "H1b") == 4


def test_register_hypothesis_idempotent_on_h_id(db: sqlite3.Connection) -> None:
    """同 h_id 重复注册=既有行回显，不落新行不归零计数（幂等注册）。"""
    for day in ("09", "10"):
        hreg.record_attempt(db, "H2", window_key=f"s5:2026-11-{day}")
    again = hreg.register_hypothesis(
        db,
        h_id="H2",
        statement="换皮措辞不得改写冻结行",
        judgement="n≥200",
        data_face="源B 849 行份额×结果",
        expected_n="n≥200",
        ledger_ref="edge-ledger:E-23",
    )
    assert (
        again
        == db.execute("SELECT id FROM hypothesis_registry WHERE h_id='H2'").fetchone()[
            "id"
        ]
    )
    assert hreg.attempt_count(db, "H2") == 2  # 计数不被注册触碰
    assert db.execute("SELECT COUNT(*) FROM hypothesis_registry").fetchone()[0] == 8


def test_register_new_family_starts_at_zero(db: sqlite3.Connection) -> None:
    """新家族首注册：无先前行→attempt_count=0（无继承面）。"""
    fresh_id = hreg.register_hypothesis(
        db,
        h_id="H10",
        statement="新族假设（冻结措辞）",
        judgement="n≥100",
        data_face="新数据面",
        expected_n="n≥100",
        ledger_ref="edge-ledger:E-01",
    )
    assert fresh_id > 0
    assert hreg.attempt_count(db, "H10") == 0
    assert hreg.current_status(db, "H10") is HypothesisState.REGISTERED


def test_register_same_h_id_different_family_is_conflict(
    db: sqlite3.Connection,
) -> None:
    """W1 回归：同 h_id 异 family=参数冲突（语义明确报既有 family）。

    不再落「注册后读不到假设行」式误导错误；同 h_id 同 family 幂等回显
    既有行不变。
    """
    first = hreg.register_hypothesis(
        db,
        h_id="H20",
        statement="原措辞",
        judgement="n≥10",
        data_face="原数据面",
        expected_n="n≥10",
        ledger_ref="edge-ledger:E-01",
        family="F20",
    )
    with pytest.raises(hreg.HIdFamilyConflict, match="F20"):
        hreg.register_hypothesis(
            db,
            h_id="H20",
            statement="异族换皮",
            judgement="n≥10",
            data_face="另一数据面",
            expected_n="n≥10",
            ledger_ref="edge-ledger:E-23",
            family="OTHER",
        )
    # 缺省 family=h_id 与显式异族同理：冲突面=族归属不可改
    with pytest.raises(hreg.HIdFamilyConflict):
        hreg.register_hypothesis(
            db,
            h_id="H20",
            statement="缺省族冲突",
            judgement="n≥10",
            data_face="面",
            expected_n="n≥10",
            ledger_ref="edge-ledger:E-01",
        )
    # 同 h_id 同 family：幂等回显既有行（措辞/计数均不被后到调用改写）
    again = hreg.register_hypothesis(
        db,
        h_id="H20",
        statement="后到措辞不得改写",
        judgement="n≥10",
        data_face="原数据面",
        expected_n="n≥10",
        ledger_ref="edge-ledger:E-01",
        family="F20",
    )
    assert again == first
    row = db.execute(
        "SELECT statement, family FROM hypothesis_registry WHERE h_id='H20'"
    ).fetchone()
    assert (row["statement"], row["family"]) == ("原措辞", "F20")


def test_register_requires_human_actor(db: sqlite3.Connection) -> None:
    """I1 回归：注册面人审哨——非 human 前缀 actor 拒绝且零写入。

    agent 不自注册（人审在环不变量）：默认 actor='human' 兼容人 CLI；
    落账 status_actor 如实记录调用方。
    """
    with pytest.raises(hreg.InvalidRegistration, match="人审"):
        hreg.register_hypothesis(
            db,
            h_id="H21",
            statement="程序试图自注册",
            judgement="n≥10",
            data_face="面",
            expected_n="n≥10",
            ledger_ref="edge-ledger:E-01",
            actor="agent:loop",
        )
    assert hreg.current_status(db, "H21") is None  # 零写入
    registered = hreg.register_hypothesis(
        db,
        h_id="H21",
        statement="人审注册（冻结措辞）",
        judgement="n≥10",
        data_face="面",
        expected_n="n≥10",
        ledger_ref="edge-ledger:E-01",
        actor="human:cli",
    )
    assert registered > 0
    assert hreg.current_status(db, "H21") is HypothesisState.REGISTERED
    assert (
        db.execute(
            "SELECT status_actor FROM hypothesis_registry WHERE h_id='H21'"
        ).fetchone()["status_actor"]
        == "human:cli"
    )


# ---- 扫描器挂钩（交付 4：S1→H1 示范；其余按数据面点亮留缝）----


def test_scanner_links_pin_the_wired_face() -> None:
    """挂钩钉死：s1→(H1,H8)、s5→(H7)；未点亮面不硬造。"""
    assert hreg.SCANNER_LINKS["s1"] == ("H1", "H8")
    assert hreg.SCANNER_LINKS["s5"] == ("H7",)
    assert set(hreg.SCANNER_LINKS) == {"s1", "s5"}  # s4/s3/s6=TODO 缝不入表


def test_link_scanner_run_counts_each_active_hypothesis(db: sqlite3.Connection) -> None:
    """一次扫描窗=每个挂钩活假设各记一尝试（窗键=scanner 前缀域）。"""
    landed = hreg.link_scanner_run(db, scanner="s1", window_key="2026-11-09")
    assert landed == {"H1": True, "H8": True}
    assert hreg.attempt_count(db, "H1") == 1
    assert hreg.attempt_count(db, "H8") == 1
    assert hreg.attempt_count(db, "H7") == 0  # s5 不在本次窗
    # 同窗重扫全幂等
    assert hreg.link_scanner_run(db, scanner="s1", window_key="2026-11-09") == {
        "H1": False,
        "H8": False,
    }
    assert hreg.attempt_count(db, "H1") == 1
    # H8 终态后同扫描器照跑：只记活假设
    hreg.transition_hypothesis(
        db, "H8", HypothesisState.REFUTED, actor="human", reason="裁决"
    )
    assert hreg.link_scanner_run(db, scanner="s1", window_key="2026-11-10") == {
        "H1": True,
        "H8": False,
    }


# ---- 周报注册表视图（交付 3/验收 2：状态+建议态+尝试计数）----


def test_registry_view_reports_status_suggestion_attempts(
    db: sqlite3.Connection,
) -> None:
    """周报视图行：状态/建议态/尝试计数三列齐；建议态不出终态。"""
    hreg.record_attempt(db, "H1", window_key="s1:2026-11-09")
    hreg.transition_hypothesis(
        db, "H7", HypothesisState.REFUTED, actor="human", reason="门内"
    )
    rows = {r["h_id"]: r for r in hreg.registry_view(db)}
    assert set(rows) == set(_FROZEN_H1_H8)
    # running 持稳；registered 无点亮面=维持 registered 建议
    assert (rows["H1"]["status"], rows["H1"]["suggested"]) == ("running", "running")
    assert (rows["H5"]["status"], rows["H5"]["suggested"]) == (
        "registered",
        "registered",
    )
    assert rows["H1"]["attempt_count"] == 1
    # 终态：程序不出翻案建议（人裁面），状态如实
    assert (rows["H7"]["status"], rows["H7"]["suggested"]) == ("refuted", "")
    assert rows["H7"]["status_reason"] == "门内"
    # registered + 关联扫描器已留痕 → 建议点亮 running（程序算的建议态）
    hreg.record_attempt(db, "H5", window_key="s3:2026-11-09", scanner="s3")
    rows5 = {r["h_id"]: r for r in hreg.registry_view(db)}["H5"]
    assert (rows5["status"], rows5["suggested"]) == ("registered", "running")


# ---- 人 CLI 终态翻转 e2e（验收 3）----


def test_cli_hypothesis_mark_terminal_e2e(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """e2e：真库迁移→人 CLI 翻 H4→supported→账面/输出/留痕三面对齐。"""
    from goalx_backend.cli import _cmd_hypothesis_mark
    from goalx_backend.config import Settings
    from goalx_backend.db import connect as db_connect
    from goalx_backend.db import migrate

    settings = Settings(db_path=tmp_path / "goalx.db")
    conn = db_connect(settings.db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    _cmd_hypothesis_mark(
        argparse.Namespace(
            h_id="H4", to="supported", reason="滞后案例率 63.6% 远超门（人裁）"
        ),
        settings=settings,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "h_id": "H4",
        "from_state": "running",
        "to_state": "supported",
        "actor": "human:cli",
    }
    conn = db_connect(settings.db_path)
    try:
        row = conn.execute(
            """
            SELECT status, status_actor, status_reason
            FROM hypothesis_registry WHERE h_id='H4'
            """
        ).fetchone()
        assert (row["status"], row["status_actor"]) == ("supported", "human:cli")
        assert "63.6%" in str(row["status_reason"])
        # 留痕不删：终态行仍在，措辞冻结不动
        assert (
            conn.execute("SELECT COUNT(*) FROM hypothesis_registry").fetchone()[0] == 8
        )
    finally:
        conn.close()

    # 终态翻案（supported→running）fail-loud：人 CLI 不静默吞谓词拒绝
    with pytest.raises(hreg.InvalidTransition):
        _cmd_hypothesis_mark(
            argparse.Namespace(h_id="H4", to="running", reason="翻案尝试"),
            settings=settings,
        )


def test_replay_pending_faces(db: sqlite3.Connection) -> None:
    """回放待排程面（2026-11-11 人裁 7① 升格）：非终态零尝试在册，评估即出册。"""
    # 种子 H1-H8（v51）：无 attempt 行 → 全部待排程
    pending = hreg.replay_pending(db)
    assert pending == ["H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8"]
    # 首次评估留痕 → 出册
    assert hreg.record_attempt(db, "H1", window_key="s1:w-1", scanner="s1") is True
    assert "H1" not in hreg.replay_pending(db)
    # 终态假设（supported/refuted）恒不在册（生死已判，无「待排程」语义）
    hreg.transition_hypothesis(
        db,
        "H2",
        hreg.HypothesisState.SUPPORTED,
        actor="human:cli",
        reason="人裁支持",
    )
    assert "H2" not in hreg.replay_pending(db)
