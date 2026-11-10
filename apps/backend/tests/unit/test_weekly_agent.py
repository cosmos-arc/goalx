"""vision-build 票 03：周批 agent 循环 + 对抗质疑（L2 提案层）。

管线级钉住：空转守卫（水位对比）、引用 id 缺失管线层拒收、查重 v0
（台账精确回指 + refuted 库去伪新颖）、≤5 条/次、对抗质疑 fresh-context
（新会话无生成上下文）、提案文件受控目录 append-only、LLM 花费落账
（llm_proposal_costs，v54）、收缩 ≥L2 只留心跳、探索钟锚点首跑钉值。
人审注册 CLI 端到端在文件尾（读台账→提案→质疑→人注册全链）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from goalx_backend.db import migrate
from goalx_backend.evaluation import weekly_agent


def test_migration_v54_idempotent(db: sqlite3.Connection) -> None:
    """v54 幂等：三表就位、花费/锚账 append-only、重跑零效果。"""
    assert migrate(db) == 54
    assert migrate(db) == 54  # 同版本重跑零效果
    for table in (
        "llm_proposal_costs",
        "weekly_agent_state",
        "exploration_clock_anchor",
    ):
        assert (
            db.execute(
                "SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()["c"]
            == 1
        )
    # 花费账 append-only（UPDATE/DELETE 库层拒绝；v49/v52 触发器先例）
    db.execute(
        """
        INSERT INTO llm_proposal_costs
            (proposal_id, phase, model, prompt_tokens, completion_tokens, cost_cny, ts)
        VALUES ('B-20261116', 'generate', 'glm-5.3', 100, 50, 0.0022,
                '2026-11-16T06:00:00+00:00')
        """
    )
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE llm_proposal_costs SET cost_cny = 0")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM llm_proposal_costs")
    # 锚账 append-only 且单行（id=1 约束拒绝第二行）
    db.execute(
        """
        INSERT INTO exploration_clock_anchor (id, anchor_date, actor, ts)
        VALUES (1, '2026-11-16', 'agent:weekly-batch', '2026-11-16T06:00:00+00:00')
        """
    )
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE exploration_clock_anchor SET anchor_date = '2020-01-01'")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM exploration_clock_anchor")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute(
            """
            INSERT INTO exploration_clock_anchor (id, anchor_date, actor, ts)
            VALUES (2, '2026-11-23', 'agent:weekly-batch', 'x')
            """
        )


def test_weekly_agent_state_upsertable(db: sqlite3.Connection) -> None:
    """水位 state 表（face_derive_state 同款）：face 键 upsert 可改写。"""
    db.execute(
        """
        INSERT INTO weekly_agent_state (face, updated_at, stats)
        VALUES ('watermark', '2026-11-16T06:00:00+00:00', '{"edge_max_id": 3}')
        """
    )
    db.execute(
        """
        UPDATE weekly_agent_state
        SET updated_at = '2026-11-23T06:00:00+00:00', stats = '{"edge_max_id": 4}'
        WHERE face = 'watermark'
        """
    )
    row = db.execute(
        "SELECT stats FROM weekly_agent_state WHERE face = 'watermark'"
    ).fetchone()
    assert str(row["stats"]) == '{"edge_max_id": 4}'


def _seed_finding(
    db: sqlite3.Connection,
    *,
    finding_id: int,
    scanner: str = "s1",
    segment: str | None = None,
    status: str = "new",
    found_at: str = "2026-11-13T23:40:00+00:00",
) -> None:
    """种一条 edge_findings 台账行（管线读面测试共用；段键缺省按 id 唯一）。"""
    resolved = segment if segment is not None else f"E0|had|home|v{finding_id}"
    db.execute(
        """
        INSERT INTO edge_findings
            (id, scanner, segment_key, metric_value, n, se, window, status, found_at)
        VALUES (?, ?, ?, 0.031, 520, 0.011, '2026-07-06..2026-11-12', ?, ?)
        """,
        (finding_id, scanner, resolved, status, found_at),
    )


def _seed_lesson(directory: Any, lesson_id: str, *, trigger: str = "场景甲") -> None:
    """种一条教训库条目（frontmatter 极简面）。"""
    directory.joinpath(f"{lesson_id}.md").write_text(
        f"""---
id: {lesson_id}
status: active
confidence: 高
created: 2026-11-09
review_due: 2027-11-09
window: null
cause: 测试教训——别再死一次。
triggers:
  - {trigger}
evidence:
  - edge-ledger E-04
---

正文（测试）。
""",
        encoding="utf-8",
    )


# ---- 空转守卫 + 收缩暂停（run_weekly_batch 心跳面） ----


class _StubChat:
    """LLM 注入桩：记录每次 prompt（供 fresh-context 断言），返回固定文本。"""

    def __init__(self, reply: str) -> None:
        self.prompts: list[str] = []
        self._reply = reply

    def __call__(self, prompt: str) -> weekly_agent.ChatOutcome:
        from goalx_backend.evaluation import weekly_agent

        self.prompts.append(prompt)
        return weekly_agent.ChatOutcome(
            content=self._reply,
            model="glm-5.3",
            prompt_tokens=100,
            completion_tokens=50,
            cost_cny=0.0022,
        )


def test_paused_level_skips_generation(db: sqlite3.Connection, tmp_path: Any) -> None:
    """收缩 ≥L2（weekly_batch=paused）：只留心跳，不烧 LLM 不产提案。"""
    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import contraction, weekly_agent

    contraction.set_level(
        db, contraction.ContractionLevel.L1, actor="human:cli", reason="演练"
    )
    contraction.set_level(
        db, contraction.ContractionLevel.L2, actor="human:cli", reason="演练"
    )
    chat = _StubChat("[]")
    report = weekly_agent.run_weekly_batch(
        db, store=CorpusStore(tmp_path / "corpus"), chat=chat, lessons_dir=tmp_path
    )
    assert report["status"] == "paused_heartbeat"
    assert report["level"] == "L2"
    assert chat.prompts == []  # 未烧 LLM
    assert not (tmp_path / "corpus" / "reports" / "proposals").exists()
    # 暂停拍不写水位（解除后新料仍算新）
    assert weekly_agent.read_watermark(db) is None


def test_idle_guard_heartbeat_on_no_new_material(
    db: sqlite3.Connection, tmp_path: Any
) -> None:
    """空转守卫：台账无新发现且教训库无新条目 → 第二拍只报心跳不烧 LLM。"""
    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import weekly_agent

    _seed_finding(db, finding_id=3)
    _seed_lesson(tmp_path, "L-01")
    chat = _StubChat("[]")
    store = CorpusStore(tmp_path / "corpus")
    first = weekly_agent.run_weekly_batch(
        db, store=store, chat=chat, lessons_dir=tmp_path, run_date="2026-11-16"
    )
    assert first["status"] == "generated"
    assert len(chat.prompts) == 1  # 首跑真跑：一次 generate
    assert weekly_agent.read_watermark(db) == {
        "edge_findings_max_id": 3,
        "lessons_count": 1,
    }
    # 第二拍：无新发现无新教训 → 心跳，零 LLM 调用
    second = weekly_agent.run_weekly_batch(
        db, store=store, chat=chat, lessons_dir=tmp_path, run_date="2026-11-23"
    )
    assert second["status"] == "idle_heartbeat"
    assert len(chat.prompts) == 1
    # 台账来了新发现 → 又是真跑
    _seed_finding(db, finding_id=4, found_at="2026-11-20T23:40:00+00:00")
    third = weekly_agent.run_weekly_batch(
        db, store=store, chat=chat, lessons_dir=tmp_path, run_date="2026-11-23"
    )
    assert third["status"] == "generated"
    assert len(chat.prompts) == 2


# ---- 生成全链：提案→拒收→对抗质疑 fresh-context→落盘→花费账 ----

_GOOD_REPLY = """前置说明可有一段。
```json
[
  {
    "statement": "JC 平赔带 P_d∈[0.24,0.28] 的定价偏差方向可预测（周中拍显著）",
    "judgement": "滚动 90 日窗，二项 SE 越门 |z|>2（RW stepdown 校正后）",
    "data_face": "s1 平赔桶段 + hist_matches 结算",
    "expected_n": "n≥300/桶",
    "ledger_ref": "edge:42",
    "refs": ["42", "L-01", "H2"]
  },
  {
    "statement": "缺引用 id 的裸提案",
    "judgement": "x", "data_face": "y", "expected_n": "z", "ledger_ref": "edge:42"
  },
  {
    "statement": "撞 refuted 库的伪新颖提案",
    "judgement": "x", "data_face": "y", "expected_n": "z",
    "ledger_ref": "edge-ledger:E-17", "refs": ["42"]
  }
]
```
尾注也可以有。"""

_CRITIQUE_TEXT = "反驳：该判据未校正族错误率，且平赔桶已被反复扫描——多重检验风险高。"


class _ScriptedChat:
    """脚本桩：第一次（generate）回提案 JSON，其后每次（critique）回反驳文本。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> weekly_agent.ChatOutcome:
        from goalx_backend.evaluation import weekly_agent

        self.prompts.append(prompt)
        content = _GOOD_REPLY if len(self.prompts) == 1 else _CRITIQUE_TEXT
        return weekly_agent.ChatOutcome(
            content=content,
            model="glm-5.3",
            prompt_tokens=100,
            completion_tokens=50,
            cost_cny=0.0022,
        )


def test_full_pass_validate_critique_files_costs(
    db: sqlite3.Connection, tmp_path: Any
) -> None:
    """全链：过审 1 条（缺引用/refuted 撞库各拒 1）、质疑 fresh-context、
    提案文件 JSON+MD、花费账逐调用落行。"""
    import json as _json
    from pathlib import Path

    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import weekly_agent

    _seed_finding(db, finding_id=42)
    _seed_lesson(tmp_path, "L-01")
    # H5（edge-ledger:E-17）翻成 refuted：refuted 库去伪新颖面
    from goalx_backend.evaluation import hypothesis_registry

    hypothesis_registry.transition_hypothesis(
        db,
        "H5",
        hypothesis_registry.HypothesisState.REFUTED,
        actor="human:cli",
        reason="测试翻终态",
    )
    chat = _ScriptedChat()
    store = CorpusStore(tmp_path / "corpus")
    report = weekly_agent.run_weekly_batch(
        db, store=store, chat=chat, lessons_dir=tmp_path, run_date="2026-11-16"
    )
    assert report["status"] == "generated"
    assert report["parse_note"] == "ok"
    # 拒收：缺引用 id（票 11 裁定）+ 撞 refuted 库（查重 v0）
    reasons = {r["reasons"][0] for r in report["rejected"]}
    assert any("引用 id 缺失" in r for r in reasons)
    assert any("撞 refuted 库" in r for r in reasons)
    proposals = report["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["id"] == "P-20261116-01"
    # 对抗质疑：每条提案独立 fresh-context 调用（generate 1 + critique 1）
    assert len(chat.prompts) == 2
    critique_prompt = chat.prompts[1]
    assert proposals[0]["statement"] in critique_prompt
    # fresh-context：质疑 prompt 不含生成上下文（读面装配节标题只在 generate prompt）
    assert "== 探索台账" not in critique_prompt
    assert "== 假设注册表" not in critique_prompt
    assert "== 教训库注入" not in critique_prompt
    assert "== 周报 digest" not in critique_prompt
    assert proposals[0]["critique"] == _CRITIQUE_TEXT
    assert proposals[0]["status"] == "awaiting_human_review"
    # 提案文件：受控目录 JSON+MD 双视图
    proposals_dir = tmp_path / "corpus" / "reports" / "proposals"
    beat_json = proposals_dir / "weekly-proposals-2026-11-16.json"
    beat_md = proposals_dir / "weekly-proposals-2026-11-16.md"
    assert beat_json.is_file()
    assert beat_md.is_file()
    loaded = _json.loads(beat_json.read_text(encoding="utf-8"))
    assert loaded["proposals"][0]["id"] == "P-20261116-01"
    md_text = beat_md.read_text(encoding="utf-8")
    assert "人审 checklist" in md_text
    assert "数据面历史可算" in md_text
    assert _CRITIQUE_TEXT in md_text
    # 花费账：generate 批次行 + critique 逐条行（append-only）
    rows = db.execute(
        "SELECT proposal_id, phase, model, cost_cny FROM llm_proposal_costs ORDER BY id"
    ).fetchall()
    assert [(str(r["proposal_id"]), str(r["phase"])) for r in rows] == [
        ("B-20261116", "generate"),
        ("P-20261116-01", "critique"),
    ]
    assert all(str(r["model"]) == "glm-5.3" for r in rows)
    # 首跑钉锚（当天，一次性）
    assert report["anchor"] == "2026-11-16"
    row = db.execute("SELECT anchor_date FROM exploration_clock_anchor").fetchone()
    assert str(row["anchor_date"]) == "2026-11-16"
    assert Path(report["files"]["json"]).name == "weekly-proposals-2026-11-16.json"


def test_over_five_cap_truncates(db: sqlite3.Connection, tmp_path: Any) -> None:
    """≤5 条/次：LLM 回 7 条 → 前 5 过审面、余 2 拒收（截断留痕）。"""
    import json as _json

    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import weekly_agent

    for i in range(50, 57):  # 7 条台账行
        _seed_finding(db, finding_id=i)
    _seed_lesson(tmp_path, "L-01")
    raw = [
        {
            "statement": f"提案 {i}",
            "judgement": "门",
            "data_face": "面",
            "expected_n": "n",
            "ledger_ref": f"edge:{i}",
            "refs": [str(i)],
        }
        for i in range(50, 57)
    ]
    chat = _StubChat(f"```json\n{_json.dumps(raw, ensure_ascii=False)}\n```")
    report = weekly_agent.run_weekly_batch(
        db,
        store=CorpusStore(tmp_path / "corpus"),
        chat=chat,  # type: ignore[arg-type]
        lessons_dir=tmp_path,
        run_date="2026-11-16",
    )
    assert len(report["proposals"]) == 5  # type: ignore[arg-type]
    assert len(report["rejected"]) == 2  # type: ignore[arg-type]
    assert all(
        "上限" in r["reasons"][0]
        for r in report["rejected"]  # type: ignore[index]
    )
    # 7 条台账 → critique 只有前 5 条各一次 + generate 一次 = 6 次调用
    assert len(chat.prompts) == 6


def test_beat_files_append_only_on_same_date_rerun(
    db: sqlite3.Connection, tmp_path: Any
) -> None:
    """同日再真跑（周中新发现手动补拍）：新文件递增后缀，旧文件不改写。"""
    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import weekly_agent

    _seed_finding(db, finding_id=1)
    _seed_lesson(tmp_path, "L-01")
    store = CorpusStore(tmp_path / "corpus")
    weekly_agent.run_weekly_batch(
        db,
        store=store,
        chat=_StubChat("[]"),
        lessons_dir=tmp_path,
        run_date="2026-11-16",
    )
    _seed_finding(db, finding_id=2, found_at="2026-11-18T23:40:00+00:00")
    weekly_agent.run_weekly_batch(
        db,
        store=store,
        chat=_StubChat("[]"),
        lessons_dir=tmp_path,
        run_date="2026-11-16",
    )
    proposals_dir = tmp_path / "corpus" / "reports" / "proposals"
    names = sorted(
        p.name for p in proposals_dir.glob("weekly-proposals-2026-11-16*.json")
    )
    assert names == [
        "weekly-proposals-2026-11-16-2.json",
        "weekly-proposals-2026-11-16.json",
    ]


def test_parse_proposals_tolerates_wrapping_and_garbage() -> None:
    """解析容错：围栏/前后缀可抽；坏输出=空列表+注记（不炸管线）。"""
    from goalx_backend.evaluation import weekly_agent

    parsed, note = weekly_agent.parse_proposals(_GOOD_REPLY)
    assert note == "ok"
    assert len(parsed) == 3
    assert weekly_agent.parse_proposals("我认为没有值得提的") == (
        [],
        "LLM 回文无 JSON 数组",
    )
    assert (
        weekly_agent.parse_proposals("[{broken json}]")[1] == "LLM 回文 JSON 解析失败"
    )
    # 括号对跨字符串错位 → 解析失败注记（fail-soft：管线不炸）
    assert (
        weekly_agent.parse_proposals('文 "见 [注]" 尾 {')[1] == "LLM 回文 JSON 解析失败"
    )


# ---- 人审注册（proposal-register：过审提案 freeze 进 hypothesis_registry） ----


def _run_beat(db: sqlite3.Connection, tmp_path: Any) -> tuple[Path, dict[str, object]]:
    """跑一拍产提案文件（全链共用 fixture：1 条过审提案 P-20261116-01）。"""
    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import weekly_agent

    _seed_finding(db, finding_id=42)
    _seed_lesson(tmp_path, "L-01")
    chat = _ScriptedChat()
    report = weekly_agent.run_weekly_batch(
        db,
        store=CorpusStore(tmp_path / "corpus"),
        chat=chat,
        lessons_dir=tmp_path,
        run_date="2026-11-16",
    )
    return Path(str(report["files"]["json"])), report


def test_register_from_proposal_freezes_into_registry(
    db: sqlite3.Connection, tmp_path: Any
) -> None:
    """人过审后 CLI 注册：提案字段 freeze 进 hypothesis_registry（actor=human:cli）。"""
    from goalx_backend.evaluation import weekly_agent

    beat_json, _ = _run_beat(db, tmp_path)
    result = weekly_agent.register_from_proposal(
        db, beat_json, proposal_id="P-20261116-01", h_id="H9"
    )
    assert result["h_id"] == "H9"
    assert result["idempotent"] is False
    row = db.execute(
        "SELECT statement, ledger_ref, status, status_actor FROM hypothesis_registry"
        " WHERE h_id = 'H9'"
    ).fetchone()
    proposal = weekly_agent.parse_beat_file(beat_json)["proposals"][0]
    assert str(row["statement"]) == proposal["statement"]
    assert str(row["ledger_ref"]) == "edge:42"
    assert str(row["status"]) == "registered"
    assert str(row["status_actor"]) == "human:cli"
    # 幂等：同注册再跑返回既有行（register_hypothesis 同 h_id 同 family 幂等）
    again = weekly_agent.register_from_proposal(
        db, beat_json, proposal_id="P-20261116-01", h_id="H9"
    )
    assert again["idempotent"] is True
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM hypothesis_registry WHERE h_id = 'H9'"
        ).fetchone()["c"]
        == 1
    )


def test_register_refuses_uncritiqued_and_pseudo_novel(
    db: sqlite3.Connection, tmp_path: Any
) -> None:
    """拒收：无反驳意见的提案不得注册；撞 refuted 库（判读后置翻转）不得注册。"""
    import json as _json

    from goalx_backend.evaluation import hypothesis_registry, weekly_agent

    beat_json, _ = _run_beat(db, tmp_path)
    beat = _json.loads(beat_json.read_text(encoding="utf-8"))
    # 掐掉反驳意见 → 未过对抗质疑，不得进人审
    beat["proposals"][0].pop("critique")
    stripped = tmp_path / "stripped.json"
    stripped.write_text(_json.dumps(beat, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(weekly_agent.ProposalRegistrationError, match="对抗质疑"):
        weekly_agent.register_from_proposal(
            db, stripped, proposal_id="P-20261116-01", h_id="H9"
        )
    # 注册表后置把同台账回指的假设翻成 refuted → 伪新颖拒注册
    hypothesis_registry.register_hypothesis(
        db,
        h_id="H10",
        statement="旧预测（已死）",
        judgement="门",
        data_face="面",
        expected_n="n",
        ledger_ref="edge:42",
        actor="human:cli",
    )
    hypothesis_registry.transition_hypothesis(
        db,
        "H10",
        hypothesis_registry.HypothesisState.REFUTED,
        actor="human:cli",
        reason="判读翻终态",
    )
    with pytest.raises(weekly_agent.ProposalRegistrationError, match="refuted"):
        weekly_agent.register_from_proposal(
            db, beat_json, proposal_id="P-20261116-01", h_id="H9"
        )
    # 未知提案 id → fail-loud
    with pytest.raises(LookupError):
        weekly_agent.register_from_proposal(
            db, beat_json, proposal_id="P-不存在", h_id="H9"
        )


def test_cli_proposal_register_listing_and_register(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 两态：缺省=checklist+提案清单（只读）；--proposal-id/--h-id=注册。"""
    import argparse
    import json as _json
    from pathlib import Path

    from goalx_backend.cli import _cmd_proposal_register
    from goalx_backend.config import Settings
    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.db import connect as db_connect
    from goalx_backend.db import migrate
    from goalx_backend.evaluation import weekly_agent

    settings = Settings(db_path=tmp_path / "goalx.db")
    conn = db_connect(settings.db_path)
    try:
        migrate(conn)
        _seed_finding(conn, finding_id=42)
        _seed_lesson(tmp_path, "L-01")
        report = weekly_agent.run_weekly_batch(
            conn,
            store=CorpusStore(tmp_path / "corpus"),
            chat=_ScriptedChat(),
            lessons_dir=tmp_path,
            run_date="2026-11-16",
        )
        beat_json = Path(str(report["files"]["json"]))
    finally:
        conn.close()

    # 缺省=只读清单：checklist + 提案概要，零写入
    _cmd_proposal_register(
        argparse.Namespace(
            proposal_file=beat_json, proposal_id=None, h_id=None, family=None
        ),
        settings=settings,
    )
    listing = _json.loads(capsys.readouterr().out)
    assert listing["mode"] == "review"
    assert any("数据面历史可算" in item for item in listing["checklist"])
    # 无 refuted 判读的库面：两条过审（edge:42 + edge-ledger:E-17=H5 在册）
    assert [p["id"] for p in listing["proposals"]] == [
        "P-20261116-01",
        "P-20261116-02",
    ]
    assert all(p["critiqued"] is True for p in listing["proposals"])

    # 注册态：freeze 进 registry（actor=human:cli）
    _cmd_proposal_register(
        argparse.Namespace(
            proposal_file=beat_json,
            proposal_id="P-20261116-01",
            h_id="H9",
            family=None,
        ),
        settings=settings,
    )
    payload = _json.loads(capsys.readouterr().out)
    assert payload["h_id"] == "H9"
    assert payload["actor"] == "human:cli"
    conn = db_connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT statement, status_actor FROM hypothesis_registry WHERE h_id='H9'"
        ).fetchone()
        assert row is not None
        assert str(row["status_actor"]) == "human:cli"
    finally:
        conn.close()


# ---- 端到端（repo LLM mock 先例：monkeypatch llm.client._build_client） ----


def test_e2e_weekly_batch_via_real_client_seam(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """读台账→提案→对抗质疑附反驳→花费落账→人 CLI 注册全链（真 glm_chat 缝）。"""
    from pathlib import Path
    from typing import Any as _Any

    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice

    from goalx_backend import tasks
    from goalx_backend.config import Settings
    from goalx_backend.db import connect as db_connect
    from goalx_backend.db import migrate

    monkeypatch.setenv("GOALX_DB_PATH", str(tmp_path / "goalx.db"))
    monkeypatch.setenv("GOALX_CORPUS_ROOT", str(tmp_path / "corpus"))
    monkeypatch.setenv("GOALX_GLM_API_KEY", "test-key")

    calls: list[str] = []

    class _StubCompletions:
        def create(self, **payload: _Any) -> ChatCompletion:
            calls.append(str(payload["messages"][0]["content"]))
            content = _GOOD_REPLY if len(calls) == 1 else _CRITIQUE_TEXT
            return ChatCompletion(
                id="stub",
                model=str(payload["model"]),
                object="chat.completion",
                created=0,
                choices=[
                    Choice(
                        index=0,
                        finish_reason="stop",
                        message=ChatCompletionMessage(
                            role="assistant", content=content
                        ),
                    )
                ],
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },  # type: ignore[arg-type]
            )

    class _StubClient:
        def __init__(self) -> None:
            self.chat = type("Chat", (), {})()
            self.chat.completions = _StubCompletions()  # type: ignore[attr-defined]

    monkeypatch.setattr(
        "goalx_backend.llm.client._build_client", lambda s, base: _StubClient()
    )

    # 种台账（goalx weekly-agent 走 settings 缝的同一条路径）
    conn = db_connect(Settings().db_path)
    try:
        migrate(conn)
        _seed_finding(conn, finding_id=42)
        conn.commit()
    finally:
        conn.close()
    # 教训库读 repo 受控目录（git 内 docs/plans/lessons/，票 05 seed 29 条
    # ——确定性受控文档；L-01 实存，好提案的 refs 可解析）
    report = tasks.weekly_agent_batch()
    assert report["status"] == "generated"
    # 好提案（edge:42）+ E-17（H5 在册非 refuted，同过审）→ 1 generate + 2 critique
    assert len(calls) == 3
    assert len(report["proposals"]) == 2  # type: ignore[arg-type]
    beat_json = Path(str(report["files"]["json"]))
    assert beat_json.is_file()
    # 花费落账走真 glm_chat 记账缝（cost_ledger）+ 提案层账（llm_proposal_costs）
    conn = db_connect(Settings().db_path)
    try:
        cost_rows = conn.execute(
            "SELECT phase FROM llm_proposal_costs ORDER BY id"
        ).fetchall()
        assert [str(r["phase"]) for r in cost_rows] == [
            "generate",
            "critique",
            "critique",
        ]
        ledger = conn.execute(
            "SELECT COUNT(*) c FROM cost_ledger WHERE category='llm_call'"
        ).fetchone()["c"]
        assert int(ledger) == 3
    finally:
        conn.close()
    # 人审注册（CLI 缝同一条：注册表 freeze）
    from goalx_backend.evaluation import weekly_agent

    first_id = str(report["proposals"][0]["id"])  # type: ignore[index]
    conn = db_connect(Settings().db_path)
    try:
        result = weekly_agent.register_from_proposal(
            conn, beat_json, proposal_id=first_id, h_id="H9"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM hypothesis_registry WHERE h_id='H9'"
            ).fetchone()["c"]
            == 1
        )
    finally:
        conn.close()
    assert result["h_id"] == "H9"


def test_register_blocked_when_contraction_l1_plus(
    db: sqlite3.Connection, tmp_path: Any
) -> None:
    """prereg E 节：L1+ 停新注册——过审提案在收缩态不得 freeze（零写入）。"""
    from goalx_backend.evaluation import contraction, weekly_agent

    beat_json, _ = _run_beat(db, tmp_path)
    contraction.set_level(
        db, contraction.ContractionLevel.L1, actor="human:cli", reason="主钟演练"
    )
    with pytest.raises(weekly_agent.ProposalRegistrationError, match="停新注册"):
        weekly_agent.register_from_proposal(
            db, beat_json, proposal_id="P-20261116-01", h_id="H9"
        )
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM hypothesis_registry WHERE h_id='H9'"
        ).fetchone()["c"]
        == 0
    )
    # 回 L0 后注册放行（相邻步进人 CLI）
    contraction.set_level(
        db, contraction.ContractionLevel.L0, actor="human:cli", reason="恢复演练"
    )
    result = weekly_agent.register_from_proposal(
        db, beat_json, proposal_id="P-20261116-01", h_id="H9"
    )
    assert result["h_id"] == "H9"
