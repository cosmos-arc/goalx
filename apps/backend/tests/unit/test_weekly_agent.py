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
    assert migrate(db) == 61
    assert migrate(db) == 61  # 同版本重跑零效果
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
    # 暂停拍不写水位（解除后新料仍算新）；不计轮（回路已停，止损钟不走）
    assert weekly_agent.read_watermark(db) is None
    assert "round" not in report
    assert contraction.weekly_rounds(db) == 0


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
    assert first["round"] == 1  # 轮次记账：首拍=轮 1（2026-11-11 人裁）
    assert weekly_agent.read_watermark(db) == {
        "edge_findings_max_id": 3,
        "lessons_count": 1,
    }
    # 第二拍：无新发现无新教训 → 心跳，零 LLM 调用（idle 也计轮——空转即回路运行）
    second = weekly_agent.run_weekly_batch(
        db, store=store, chat=chat, lessons_dir=tmp_path, run_date="2026-11-23"
    )
    assert second["status"] == "idle_heartbeat"
    assert len(chat.prompts) == 1
    assert second["round"] == 2
    # 台账来了新发现 → 又是真跑（同日重跑不重复计轮：按日幂等）
    _seed_finding(db, finding_id=4, found_at="2026-11-20T23:40:00+00:00")
    third = weekly_agent.run_weekly_batch(
        db, store=store, chat=chat, lessons_dir=tmp_path, run_date="2026-11-23"
    )
    assert third["status"] == "generated"
    assert len(chat.prompts) == 2
    assert third["round"] == 2


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
    assert "注册即排历史回放验证" in md_text  # 7① 升格（2026-11-11 人裁）
    assert "回放待排程假设" in md_text
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
    # 首跑钉锚+记轮+回放待排程面（种子 H1-H8 零尝试在册）
    assert (report["anchor"], report["round"]) == ("2026-11-16", 1)
    assert "H1" in report["replay_pending"]
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


# ---- 票 06 查重升级：duplicate_reason 语义近邻（注入 embedder/duck 缝） ----


class _HashEmbedder:
    """确定性假 embedder（test_rag.HashEmbedder 同款）：token 哈希进桶计数。

    dim=256：中文长 token 视作单桶（无分词），桶碰撞须压到零——近邻
    0.2236（=1/√16，词重合的理论值）与对照 0.0 才对阈值 0.2 稳定分侧。
    """

    model_id = "fake-hash-v1"
    dim = 256

    def _vec(self, text: str) -> list[float]:
        import hashlib
        import re as _re

        vec = [0.0] * self.dim
        for tok in _re.findall(r"\w+", text.lower()):
            digest = hashlib.sha256(tok.encode()).hexdigest()
            vec[int(digest[:8], 16) % self.dim] += 1.0
        return vec

    def embed_passages(self, texts: Any) -> list[list[float]]:
        """批量 passage 向量。"""
        return [self._vec(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """单条 query 向量。"""
        return self._vec(text)


_NEAR_DUP_STATEMENT = "未扣水口径数字禁止进判读 池面复现提案"
_CONTROL_STATEMENT = "english control statement about pool drift"


def test_semantic_dedup_rejects_near_duplicate_lesson(tmp_path: Any) -> None:
    """票 06 接缝演示：近邻复现死法被拦（lesson_similar:L-01），非近邻放行。"""
    import json as _json
    from pathlib import Path

    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.db import connect, migrate
    from goalx_backend.evaluation import rag, weekly_agent

    # 文件库（duck 只读 ATTACH 挂不上 :memory:）+ 教训索引先嵌入
    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    _seed_lesson(tmp_path, "L-01", trigger="未扣水口径数字禁止进判读")
    _seed_lesson(tmp_path, "L-02", trigger="无关触发面")
    embedder = _HashEmbedder()
    rag.embed_pending(
        conn,
        namespace=rag.NAMESPACE_LESSON,
        items=rag.lesson_texts(tmp_path),
        embedder=embedder,
    )
    duck_con = rag.connect_rag(db_path)
    # 假件向量分布≠真嵌入：阈值经缝注入（0.2 使 token 重合近邻过阈；
    # 生产阈=rag.LESSON_SIMILAR_THRESHOLD，真分布校准见
    # test_rag.py::test_fastembed_threshold_calibration）
    deduper = rag.LessonDeduper(
        embedder=embedder,
        duck_con=duck_con,
        active_ids=frozenset({"L-01", "L-02"}),
        threshold=0.2,
    )

    # duplicate_reason 直钉：近邻=lesson_similar:L-01；非近邻/缝缺席=v0 行为
    reason = weekly_agent.duplicate_reason(
        "edge:9",
        batch_refs=set(),
        refuted_refs=set(),
        statement=_NEAR_DUP_STATEMENT,
        lesson_deduper=deduper,
    )
    assert reason is not None
    assert reason.startswith("lesson_similar:L-01")
    assert (
        weekly_agent.duplicate_reason(
            "edge:9",
            batch_refs=set(),
            refuted_refs=set(),
            statement=_CONTROL_STATEMENT,
            lesson_deduper=deduper,
        )
        is None
    )
    assert (
        weekly_agent.duplicate_reason("edge:9", batch_refs=set(), refuted_refs=set())
        is None
    )

    # 管线级：近邻复现死法提案被拒收留痕，无关提案过审
    _seed_finding(conn, finding_id=42)
    _seed_finding(conn, finding_id=43)
    raw = [
        {
            "statement": _NEAR_DUP_STATEMENT,
            "judgement": "门",
            "data_face": "面",
            "expected_n": "n",
            "ledger_ref": "edge:42",
            "refs": ["42", "L-01"],
        },
        {
            "statement": _CONTROL_STATEMENT,
            "judgement": "门",
            "data_face": "面",
            "expected_n": "n",
            "ledger_ref": "edge:43",
            "refs": ["43"],
        },
    ]

    class _ReplyChat:
        """固定回复桩（本测试只有 generate 一次调用即可断言拒收面）。"""

        def __call__(self, prompt: str) -> weekly_agent.ChatOutcome:
            return weekly_agent.ChatOutcome(
                content=f"```json\n{_json.dumps(raw, ensure_ascii=False)}\n```",
                model="glm-5.3",
                prompt_tokens=100,
                completion_tokens=50,
                cost_cny=0.0022,
            )

    report = weekly_agent.run_weekly_batch(
        conn,
        store=CorpusStore(tmp_path / "corpus"),
        chat=_ReplyChat(),  # type: ignore[arg-type]
        lessons_dir=Path(tmp_path),
        run_date="2026-11-16",
        lesson_deduper=deduper,
    )
    assert report["status"] == "generated"
    proposals = report["proposals"]
    rejected = report["rejected"]
    assert len(proposals) == 1
    assert proposals[0]["statement"] == _CONTROL_STATEMENT
    assert len(rejected) == 1
    assert rejected[0]["reasons"][0].startswith("lesson_similar:L-01")
    duck_con.close()
    conn.close()


def test_semantic_dedupe_degrades_not_crashes_on_duck_query_error(
    tmp_path: Any,
) -> None:
    """correctness F6：查询期 duckdb.Error 不炸周拍——降级回精确面留痕。"""
    from typing import Any as _Any
    from typing import cast as _cast

    import duckdb as _duckdb

    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.db import connect, migrate
    from goalx_backend.evaluation import rag, weekly_agent

    class _ExplodingDuck:
        """查询期必炸的假 duck 连接（ATTACH 成功后的查询期故障形态）。"""

        def execute(self, *_args: _Any, **_kwargs: _Any) -> _Any:
            raise _duckdb.Error("query-time boom")

    db_path = tmp_path / "goalx.db"
    conn = connect(db_path)
    migrate(conn)
    exploding = rag.LessonDeduper(
        embedder=_HashEmbedder(),
        duck_con=_cast("_duckdb.DuckDBPyConnection", _ExplodingDuck()),
        active_ids=frozenset({"L-01"}),
    )
    # 语义缝查询期炸：duplicate_reason 降级回精确面（None），不向上抛
    assert (
        weekly_agent.duplicate_reason(
            "edge:9",
            batch_refs=set(),
            refuted_refs=set(),
            statement="任何措辞",
            lesson_deduper=exploding,
        )
        is None
    )
    # 周拍级：同故障缝下一拍仍 generated（不炸整拍）
    _seed_finding(conn, finding_id=42)
    _seed_lesson(tmp_path, "L-01")

    class _OneProposalChat:
        """一次 generate 回单条好提案（语义面本步被降级跳过）。"""

        def __call__(self, prompt: str) -> weekly_agent.ChatOutcome:
            import json as _json

            return weekly_agent.ChatOutcome(
                content=_json.dumps(
                    [
                        {
                            "statement": "阵型族条件面的独立新提案",
                            "judgement": "门",
                            "data_face": "面",
                            "expected_n": "n",
                            "ledger_ref": "edge:42",
                            "refs": ["42"],
                        }
                    ],
                    ensure_ascii=False,
                ),
                model="glm-5.3",
                prompt_tokens=100,
                completion_tokens=50,
                cost_cny=0.0022,
            )

    report = weekly_agent.run_weekly_batch(
        conn,
        store=CorpusStore(tmp_path / "corpus"),
        chat=_OneProposalChat(),  # type: ignore[arg-type]
        lessons_dir=Path(tmp_path),
        run_date="2026-11-16",
        lesson_deduper=exploding,
    )
    assert report["status"] == "generated"
    assert len(report["proposals"]) == 1
    conn.close()


def test_beat_files_append_only_on_same_date_rerun(
    db: sqlite3.Connection, tmp_path: Any
) -> None:
    """同日再真跑（周中新发现手动补拍）：新文件递增后缀，旧文件不改写。"""
    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import weekly_agent

    _seed_finding(db, finding_id=42)
    _seed_lesson(tmp_path, "L-01")
    store = CorpusStore(tmp_path / "corpus")
    first = weekly_agent.run_weekly_batch(
        db,
        store=store,
        chat=_ScriptedChat(),
        lessons_dir=tmp_path,
        run_date="2026-11-16",
    )
    _seed_finding(db, finding_id=43, found_at="2026-11-18T23:40:00+00:00")
    second = weekly_agent.run_weekly_batch(
        db,
        store=store,
        chat=_ScriptedChat(),
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
    # F4：拍次进提案/批次 id（首拍无后缀，第二拍 P-…-2-NN / B-…-2）
    assert first["beat"] == 1
    assert second["beat"] == 2
    assert first["proposals"][0]["id"] == "P-20261116-01"
    assert second["proposals"][0]["id"] == "P-20261116-2-01"
    rows = db.execute(
        "SELECT proposal_id FROM llm_proposal_costs WHERE phase='generate' ORDER BY id"
    ).fetchall()
    assert [str(r["proposal_id"]) for r in rows] == ["B-20261116", "B-20261116-2"]


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


# ---- correctness-review 批修回归（F1/F2/F3/F5/F6/F7） ----


def test_numeric_scalar_forms_are_coerced(
    db: sqlite3.Connection, tmp_path: Any
) -> None:
    """F1：refs 数字元素/expected_n 裸数字 → 管线层 coerce 过审不误拒。"""
    import json as _json

    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import weekly_agent

    _seed_finding(db, finding_id=42)
    _seed_lesson(tmp_path, "L-01")
    reply = _json.dumps(
        [
            {
                "statement": "数字形态提案（LLM 常见裸数字输出）",
                "judgement": "门",
                "data_face": "面",
                "expected_n": 300,
                "ledger_ref": "edge:42",
                "refs": [42, "L-01"],
            }
        ],
        ensure_ascii=False,
    )
    report = weekly_agent.run_weekly_batch(
        db,
        store=CorpusStore(tmp_path / "corpus"),
        chat=_StubChat(reply),
        lessons_dir=tmp_path,
        run_date="2026-11-16",
    )
    assert report["rejected"] == []
    proposal = report["proposals"][0]
    assert proposal["refs"] == ["42", "L-01"]  # 归一为字符串（落盘/注册同形态）
    assert proposal["expected_n"] == "300"


def test_unicode_digit_refs_rejected_without_crash(
    db: sqlite3.Connection, tmp_path: Any
) -> None:
    """F2：上标 "²" 不炸整批、全角 "４２" 不伪通过（finding 42 实存仍拒）。"""
    import json as _json

    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import weekly_agent

    _seed_finding(db, finding_id=42)
    _seed_lesson(tmp_path, "L-01")
    reply = _json.dumps(
        [
            {
                "statement": "上标与全角引用",
                "judgement": "门",
                "data_face": "面",
                "expected_n": "n",
                "ledger_ref": "edge:42",
                "refs": ["²", "４２"],
            },
            {
                "statement": "全角台账回指",
                "judgement": "门",
                "data_face": "面",
                "expected_n": "n",
                "ledger_ref": "edge:４２",
                "refs": ["42"],
            },
            {
                "statement": "上标台账回指",
                "judgement": "门",
                "data_face": "面",
                "expected_n": "n",
                "ledger_ref": "edge:²",
                "refs": ["42"],
            },
        ],
        ensure_ascii=False,
    )
    report = weekly_agent.run_weekly_batch(  # 旧码在此 ValueError 崩整批
        db,
        store=CorpusStore(tmp_path / "corpus"),
        chat=_StubChat(reply),
        lessons_dir=tmp_path,
        run_date="2026-11-16",
    )
    assert report["status"] == "generated"
    assert report["proposals"] == []  # 三条全拒，不伪通过
    reasons = [r["reasons"][0] for r in report["rejected"]]
    assert any("引用 id 不可解析" in r for r in reasons)
    assert sum("台账回指不可解析" in r for r in reasons) == 2


def test_generate_prompt_carries_lesson_ids(
    db: sqlite3.Connection, tmp_path: Any
) -> None:
    """F3：教训注入带 lesson_id（render_injection 行形态）——LLM 有实存 L-NN 可引。"""
    from goalx_backend.evaluation import weekly_agent

    _seed_lesson(tmp_path, "L-07")
    faces = weekly_agent.read_faces(db, lessons_dir=tmp_path)
    assert any("L-07" in str(line) for line in faces["lessons_injection"])
    prompt = weekly_agent.build_generate_prompt(faces)
    assert "[L-07]" in prompt  # 引用形态与 refs 校验口径一致
    assert "别再死因" in prompt  # render_injection 单行注入形态


def test_concurrent_beat_claims_get_distinct_files(tmp_path: Any) -> None:
    """F5：并发双跑（手动 CLI × 调度）O_EXCL 各抢拍次——两不同文件，不互覆。"""
    import threading

    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import weekly_agent

    store = CorpusStore(tmp_path / "corpus")
    proposals_dir = tmp_path / "corpus" / "reports" / "proposals"
    claimed: list[int] = []

    def worker() -> None:
        proposals_dir.mkdir(parents=True, exist_ok=True)
        beat = weekly_agent._claim_beat_suffix(proposals_dir, "2026-11-16")
        report: dict[str, Any] = {
            "schema": "weekly-proposals/1",
            "run_date": "2026-11-16",
            "status": "generated",
            "level": "L0",
            "proposals": [],
            "rejected": [],
        }
        weekly_agent.write_beat_files(store, report, beat_suffix=beat)
        claimed.append(beat)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(claimed) == [1, 2]  # 各抢各的拍次
    names = sorted(p.name for p in proposals_dir.glob("weekly-proposals-*.json"))
    assert names == [
        "weekly-proposals-2026-11-16-2.json",
        "weekly-proposals-2026-11-16.json",
    ]


def test_register_sentinels_share_write_transaction(
    db: sqlite3.Connection, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6：撞 refuted 哨/L1 哨/注册在同一写事务内（BEGIN IMMEDIATE 关 TOCTOU 窗）。"""
    from goalx_backend.evaluation import hypothesis_registry, weekly_agent

    beat_json, _ = _run_beat(db, tmp_path)
    seen: dict[str, bool] = {}
    original_refs = weekly_agent._registry_refs

    def spy_refs(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
        seen["sentinel_in_txn"] = conn.in_transaction
        return original_refs(conn)

    monkeypatch.setattr(weekly_agent, "_registry_refs", spy_refs)
    original_register = hypothesis_registry.register_hypothesis

    def spy_register(conn: sqlite3.Connection, **kwargs: object) -> int:
        seen["register_in_txn"] = conn.in_transaction
        return original_register(conn, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(hypothesis_registry, "register_hypothesis", spy_register)
    weekly_agent.register_from_proposal(
        db, beat_json, proposal_id="P-20261116-01", h_id="H9"
    )
    # 哨读与注册都在同一 BEGIN IMMEDIATE 写事务内：并发 hypothesis-mark/
    # contraction-set 被写锁串行化，哨后翻转要么先于本事务（哨内重读可见）
    # 要么后于提交——窗口不存在
    assert seen["sentinel_in_txn"] is True
    assert seen["register_in_txn"] is True


def test_partial_beat_residual_json_next_beat_completes(
    db: sqlite3.Connection, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7：双视图两 rename 非事务——json 落 md 未落留残拍（可解析），下拍 -2 补齐。"""
    import json as _json

    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.evaluation import weekly_agent

    _seed_finding(db, finding_id=42)
    _seed_lesson(tmp_path, "L-01")
    store = CorpusStore(tmp_path / "corpus")
    proposals_dir = tmp_path / "corpus" / "reports" / "proposals"

    real_replace = Path.replace
    md_renames = {"n": 0}

    def flaky_replace(self: Path, target: str) -> Path:
        if str(target).endswith(".md") and md_renames["n"] == 0:
            md_renames["n"] += 1
            raise OSError("注入崩溃：json 已 rename、md 未 rename")
        return real_replace(self, target)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(Path, "replace", flaky_replace)
    with pytest.raises(OSError, match="注入崩溃"):
        weekly_agent.run_weekly_batch(
            db,
            store=store,
            chat=_StubChat("[]"),
            lessons_dir=tmp_path,
            run_date="2026-11-16",
        )
    monkeypatch.undo()
    # 残拍：beat-1 JSON 在场且可解析（半拍证据保留），MD 缺席
    residual = proposals_dir / "weekly-proposals-2026-11-16.json"
    assert residual.is_file()
    loaded = _json.loads(residual.read_text(encoding="utf-8"))
    assert loaded["status"] == "generated"
    assert not (proposals_dir / "weekly-proposals-2026-11-16.md").exists()
    # 崩溃拍未写水位（非空转）；拍名被哨兵占用 → 下拍 -2 双视图补齐
    report = weekly_agent.run_weekly_batch(
        db,
        store=store,
        chat=_StubChat("[]"),
        lessons_dir=tmp_path,
        run_date="2026-11-16",
    )
    assert report["beat"] == 2
    assert (proposals_dir / "weekly-proposals-2026-11-16-2.json").is_file()
    assert (proposals_dir / "weekly-proposals-2026-11-16-2.md").is_file()
