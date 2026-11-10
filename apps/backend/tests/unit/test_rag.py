"""vision-build 票 06：RAG 两点（语料检索 + 教训查重）。

管线级钉住：v56 迁移幂等、embedder 注入缝（假件确定性可复算）、
DuckDB 纯 SQL 余弦检索（确定性排序）、增量 embed 幂等（content_hash
相同跳过）、两索引源（卫报篇级/教训条目）、引用纪律拒收（缺 id 直接
拒）、语义近邻查重接缝（近邻复现死法被拦）。真 fastembed 路径一条
冒烟（模型缓存缺席 skip，_CORPUS_DUCK 先例）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import duckdb
import pytest

from goalx_backend.db import connect, migrate
from goalx_backend.evaluation import rag


class HashEmbedder:
    """确定性假 embedder：token 哈希进桶计数（词重合→高余弦，近邻可复算）。"""

    model_id = "fake-hash-v1"
    dim = 64

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in re.findall(r"\w+", text.lower()):
            digest = hashlib.sha256(tok.encode()).hexdigest()
            vec[int(digest[:8], 16) % self.dim] += 1.0
        return vec

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """批量 passage 向量。"""
        return [self._vec(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """单条 query 向量。"""
        return self._vec(text)


@pytest.fixture
def rag_env(
    tmp_path: Path,
) -> Iterator[tuple[sqlite3.Connection, duckdb.DuckDBPyConnection]]:
    """文件库 + duck 只读 ATTACH（:memory: 库 duck 挂不上，须落文件）。"""
    conn = connect(tmp_path / "goalx.db")
    migrate(conn)
    duck_con = rag.connect_rag(tmp_path / "goalx.db")
    try:
        yield conn, duck_con
    finally:
        duck_con.close()
        conn.close()


def test_embed_pending_incremental_idempotent(
    rag_env: tuple[sqlite3.Connection, duckdb.DuckDBPyConnection],
) -> None:
    """增量幂等：首跑全嵌、同 hash 重跑跳过、文本变更重嵌（upsert）。"""
    conn, _duck = rag_env
    embedder = HashEmbedder()
    items = [
        ("L-01", "口径纪律：未扣水的数字禁止进判读"),
        ("L-02", "PIT 纪律：xG 赛后值禁入特征"),
    ]
    first = rag.embed_pending(conn, namespace="lesson", items=items, embedder=embedder)
    assert first == {"seen": 2, "embedded": 2, "skipped": 0}
    # 同输入重跑：content_hash 相同 → 全跳过（幂等，不重烧 embed）
    second = rag.embed_pending(conn, namespace="lesson", items=items, embedder=embedder)
    assert second == {"seen": 2, "embedded": 0, "skipped": 2}
    # L-01 文本变更：hash 变 → 重嵌（upsert 覆盖，非新增行）
    changed = [("L-01", "口径纪律：池面要税后口径"), items[1]]
    third = rag.embed_pending(
        conn, namespace="lesson", items=changed, embedder=embedder
    )
    assert third == {"seen": 2, "embedded": 1, "skipped": 1}
    row = _row(conn, "lesson", "L-01", embedder.model_id)
    assert row is not None
    assert (
        str(row["content_hash"])
        == hashlib.sha256("口径纪律：池面要税后口径".encode()).hexdigest()
    )
    vec = json.loads(str(row["vec"]))
    assert len(vec) == embedder.dim
    assert int(row["dim"]) == embedder.dim
    # upsert 不增行：两命名空间各键仍单行
    total = conn.execute(
        "SELECT COUNT(*) c FROM rag_embeddings WHERE namespace = 'lesson'"
    ).fetchone()["c"]
    assert total == 2

    # 换模型=另一 model_id 全量重嵌（旧口径行原地保留）
    class OtherModelEmbedder(HashEmbedder):
        """同哈希面、另立 model_id（换模型=重嵌的形态自证）。"""

        model_id = "fake-hash-v2"

    other = rag.embed_pending(
        conn,
        namespace="lesson",
        items=items,
        embedder=OtherModelEmbedder(),
    )
    assert other == {"seen": 2, "embedded": 2, "skipped": 0}
    models = {
        str(r["model_id"])
        for r in conn.execute(
            "SELECT DISTINCT model_id FROM rag_embeddings WHERE namespace='lesson'"
        )
    }
    assert models == {"fake-hash-v1", "fake-hash-v2"}


def test_search_cosine_orders_and_scores(
    rag_env: tuple[sqlite3.Connection, duckdb.DuckDBPyConnection],
) -> None:
    """DuckDB 余弦检索：同文=1.0、排序确定、命名空间/模型隔离。"""
    conn, duck_con = rag_env
    embedder = HashEmbedder()
    rag.embed_pending(
        conn,
        namespace="lesson",
        items=[
            ("L-01", "early lock beats late lock on clv"),
            ("L-02", "pool share mispricing draw"),
        ],
        embedder=embedder,
    )
    hits = rag.search(
        duck_con,
        namespace="lesson",
        model_id=embedder.model_id,
        query_vec=embedder.embed_query("early lock beats late lock on clv"),
        top_k=2,
    )
    assert [hit.item_id for hit in hits] == ["L-01", "L-02"]
    # 同文查询=向量全同：余弦恰 1（float32 舍入内）
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)
    assert hits[0].namespace == "lesson"
    # 独立词面查询排到另一条之下（确定性排序：score DESC, item_id ASC）
    other = rag.search(
        duck_con,
        namespace="lesson",
        model_id=embedder.model_id,
        query_vec=embedder.embed_query("pool share mispricing draw"),
        top_k=1,
    )
    assert other[0].item_id == "L-02"
    # 命名空间隔离：guardian 命名空间无行 → 空
    empty = rag.search(
        duck_con,
        namespace="guardian_article",
        model_id=embedder.model_id,
        query_vec=[0.0] * embedder.dim,
        top_k=3,
    )
    assert empty == []
    # 模型隔离：另一 model_id 无行 → 空（换模型=重嵌前的空窗如实空）
    empty_model = rag.search(
        duck_con,
        namespace="lesson",
        model_id="fake-other-v1",
        query_vec=[0.0] * embedder.dim,
        top_k=3,
    )
    assert empty_model == []


def test_search_dim_mismatch_fails_loud(
    rag_env: tuple[sqlite3.Connection, duckdb.DuckDBPyConnection],
) -> None:
    """定长 CAST 面维：查询向量与库内 dim 不符 → DuckDB 显式报错（不静默空）。"""
    conn, duck_con = rag_env
    embedder = HashEmbedder()
    rag.embed_pending(
        conn, namespace="lesson", items=[("L-01", "dim check")], embedder=embedder
    )
    with pytest.raises(duckdb.Error):
        rag.search(
            duck_con,
            namespace="lesson",
            model_id=embedder.model_id,
            query_vec=[0.0] * (embedder.dim + 1),
            top_k=1,
        )


# ---- 索引源②：教训条目（lessons 解析面） ----


def _seed_lesson_file(directory: Path, lesson_id: str, *, cause: str) -> None:
    """种一条教训库条目（frontmatter 极简面，票 05 schema）。"""
    directory.mkdir(parents=True, exist_ok=True)
    directory.joinpath(f"{lesson_id}.md").write_text(
        f"""---
id: {lesson_id}
status: active
confidence: 高
created: 2026-11-09
review_due: 2027-11-09
window: null
cause: {cause}
triggers:
  - 场景甲
  - 场景乙
evidence:
  - edge-ledger E-04
---

正文：三段切分各只碰一次。
""",
        encoding="utf-8",
    )


def test_lesson_texts_composition(tmp_path: Path) -> None:
    """教训索引源：triggers+cause+body 合成嵌入文本，id 即引用 id。"""
    _seed_lesson_file(tmp_path, "L-01", cause="未扣水口径数字禁止进判读")
    items = rag.lesson_texts(tmp_path)
    assert [item_id for item_id, _ in items] == ["L-01"]
    text = items[0][1]
    assert "未扣水口径数字禁止进判读" in text
    assert "场景甲" in text
    assert "场景乙" in text
    assert "三段切分各只碰一次" in text


# ---- 索引源①：卫报语料（bronze 篇级） ----


def _guardian_envelope(sid: str, title: str, body: str) -> dict[str, Any]:
    """bronze 信封行（guardian/news_article 布局，票 79 采集面）。"""
    return {
        "sid": sid,
        "parser_version": "guardian_v1",
        "payload": {
            "id": sid,
            "webTitle": title,
            "bodyText": body,
            "webPublicationDate": "2026-11-01T09:00:00Z",
        },
    }


def test_guardian_article_texts(tmp_path: Path) -> None:
    """卫报索引源：篇级文本=title+body、sid 去重首见为准、残行跳过、limit 有界。"""
    from goalx_backend.data.corpus_store import CorpusStore

    store = CorpusStore(tmp_path / "corpus")
    store.append_bronze(
        "guardian",
        "news_article",
        [
            _guardian_envelope(
                "art-1", "Arsenal injury crisis", "Saka is out for weeks."
            ),
            _guardian_envelope(
                "art-2", "Transfer window", "Quiet deadline day overall."
            ),
            # 同 sid 重採行：append-only bronze 可有，首见为准（同
            # guardian_silver.candidate_articles 去重序）
            _guardian_envelope(
                "art-1", "Arsenal injury crisis UPDATED", "Saka is out."
            ),
            # 残行：无 body → 跳过
            {
                "sid": "art-3",
                "parser_version": "guardian_v1",
                "payload": {"id": "art-3", "webTitle": "No body"},
            },
        ],
    )
    items = list(rag.guardian_article_texts(store))
    assert items == [
        ("art-1", "Arsenal injury crisis\n\nSaka is out for weeks."),
        ("art-2", "Transfer window\n\nQuiet deadline day overall."),
    ]
    # limit 有界（首夜/试点增量跑批的护栏）
    limited = list(rag.guardian_article_texts(store, limit=1))
    assert limited == [("art-1", "Arsenal injury crisis\n\nSaka is out for weeks.")]
    # 空语料（数据集缺席）= 空清单，不炸
    assert list(rag.guardian_article_texts(CorpusStore(tmp_path / "corpus2"))) == []
    store.close()


# ---- 引用纪律：检索产出缺引用 id 直接拒（票 11 裁定延伸到检索面） ----


def test_citation_reasons_rejects_missing_ids() -> None:
    """拒收断言：缺 item_id/空串/非字符串 → 理由进清单；带 id 行放行。"""
    good = [
        {"item_id": "art-1", "quote": "Saka is out for weeks."},
        {"item_id": "L-01", "quote": "未扣水口径"},
    ]
    assert rag.citation_reasons(good) == []
    bad = [
        {"quote": "无 id 的检索产出"},
        {"item_id": "", "quote": "空 id"},
        {"item_id": None, "quote": "None id"},
        {"item_id": 42, "quote": "非字符串 id"},
        good[0],
    ]
    reasons = rag.citation_reasons(bad)
    assert len(reasons) == 4
    assert all("引用 id" in reason for reason in reasons)
    # 拒收后不带 id 行进下游：只留过审行是调用方纪律，本函数只产理由
    assert rag.citation_reasons([]) == []


# ---- 周批查重缝：空索引零成本降级 + 模型身份落账 ----


class _CountingEmbedder(HashEmbedder):
    """计数假件：embed_query 调用可观测（空索引不得烧嵌入）。"""

    def __init__(self) -> None:
        self.query_calls = 0

    def embed_query(self, text: str) -> list[float]:
        """计数的 query 向量。"""
        self.query_calls += 1
        return super().embed_query(text)


def test_lesson_deduper_skips_embed_when_index_empty(
    rag_env: tuple[sqlite3.Connection, duckdb.DuckDBPyConnection],
) -> None:
    """教训索引空（未回填）→ 无近邻恒 None 且零嵌入调用（降级不烧模型下载）。"""
    _conn, duck_con = rag_env
    embedder = _CountingEmbedder()
    deduper = rag.LessonDeduper(embedder=embedder, duck_con=duck_con)
    assert deduper.nearest("任何提案措辞") is None
    assert embedder.query_calls == 0  # 先查索引再嵌入：空索引零成本


def test_current_model_id_zero_download() -> None:
    """模型身份串（model_id）可零成本取用：晨检/运维只读口，不构造模型。"""
    identity = rag.current_model_id()
    assert identity.startswith(rag.FastembedEmbedder.MODEL_NAME + "@fastembed-")
    # 实例身份与模块口一致；dim=384 落账（MiniLM 多语线）
    embedder = rag.FastembedEmbedder()
    assert embedder.model_id == identity
    assert embedder.dim == 384


# ---- 真 fastembed 路径冒烟（模型缓存缺席 skip，_CORPUS_DUCK 先例） ----

_FASTEMBED_CACHE = Path.home() / ".cache" / "fastembed"
_MODEL_CACHED = _FASTEMBED_CACHE.is_dir() and any(
    _FASTEMBED_CACHE.glob("**/*paraphrase-multilingual-MiniLM*")
)


@pytest.mark.slow
@pytest.mark.skipif(
    not _MODEL_CACHED, reason="本机 fastembed 模型缓存缺席（CI 跳过，不真下模型）"
)
def test_fastembed_real_smoke() -> None:
    """真模型冒烟：384 维、同入同出（可复算）、passage/query 双面同口径。"""
    embedder = rag.FastembedEmbedder()
    first = embedder.embed_query("pool drift signal")
    second = embedder.embed_query("pool drift signal")
    assert len(first) == 384
    assert first == second  # 确定性（无前缀随机/归一漂移）
    passages = embedder.embed_passages(["早锁优于晚锁", "early lock beats late"])
    assert len(passages) == 2
    assert all(len(vec) == 384 for vec in passages)


def test_migration_v56_idempotent(db: sqlite3.Connection) -> None:
    """v56 幂等：rag_embeddings 就位、主键三键齐、重跑零效果。"""
    assert migrate(db) == 56
    assert migrate(db) == 56  # 同版本重跑零效果
    ddl = str(
        db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='rag_embeddings'"
        ).fetchone()["sql"]
    )
    for column in (
        "namespace",
        "item_id",
        "model_id",
        "dim",
        "vec",
        "content_hash",
        "embedded_at",
    ):
        assert column in ddl, f"缺列 {column}"
    assert "PRIMARY KEY (namespace, item_id, model_id)" in ddl


def _row(db: sqlite3.Connection, namespace: str, item_id: str, model_id: str) -> Any:
    """读一行 rag_embeddings（测试断言共用）。"""
    return db.execute(
        """
        SELECT namespace, item_id, model_id, dim, vec, content_hash, embedded_at
        FROM rag_embeddings
        WHERE namespace = ? AND item_id = ? AND model_id = ?
        """,
        (namespace, item_id, model_id),
    ).fetchone()
