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
import math
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


_FASTEMBED_CACHE = Path.home() / ".cache" / "fastembed"
_MODEL_CACHED = _FASTEMBED_CACHE.is_dir() and any(
    _FASTEMBED_CACHE.glob("**/*paraphrase-multilingual-MiniLM*")
)


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


def _seed_lesson_file(
    directory: Path, lesson_id: str, *, cause: str, status: str = "active"
) -> None:
    """种一条教训库条目（frontmatter 极简面，票 05 schema）。"""
    directory.mkdir(parents=True, exist_ok=True)
    directory.joinpath(f"{lesson_id}.md").write_text(
        f"""---
id: {lesson_id}
status: {status}
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
    """教训索引源：active-only（correctness F1）+摘要行（F4 对称化）+合成文本。"""
    _seed_lesson_file(tmp_path, "L-01", cause="未扣水口径数字禁止进判读")
    # F1：archived/review_due 不进索引（归档教训不得拦新提案）
    _seed_lesson_file(tmp_path, "L-02", cause="已归档死法", status="archived")
    _seed_lesson_file(tmp_path, "L-03", cause="复审中死法", status="review_due")
    items = rag.lesson_texts(tmp_path)
    assert [item_id for item_id, _ in items] == ["L-01"]
    text = items[0][1]
    # F4：摘要行（triggers+cause 一行）——供查重匹配的对称查询面
    assert "摘要：" in text
    assert "场景甲" in text.splitlines()[0]
    assert "未扣水口径数字禁止进判读" in text
    assert "场景甲" in text
    assert "场景乙" in text
    assert "三段切分各只碰一次" in text


def test_nearest_filters_archived_lessons(
    rag_env: tuple[sqlite3.Connection, duckdb.DuckDBPyConnection],
    tmp_path: Path,
) -> None:
    """correctness F1：检索侧按 active id 集过滤——已嵌条目转 archived 后不再命中。"""
    conn, duck_con = rag_env
    embedder = HashEmbedder()
    _seed_lesson_file(tmp_path, "L-01", cause="未扣水口径数字禁止进判读")
    _seed_lesson_file(tmp_path, "L-02", cause="无关触发面")
    rag.embed_pending(
        conn,
        namespace=rag.NAMESPACE_LESSON,
        items=rag.lesson_texts(tmp_path),
        embedder=embedder,
    )
    near = "未扣水口径数字禁止进判读 池面复现提案"
    both_active = frozenset({"L-01", "L-02"})
    deduper = rag.LessonDeduper(
        embedder=embedder, duck_con=duck_con, active_ids=both_active, threshold=0.1
    )
    hit = deduper.nearest(near)
    assert hit is not None
    assert hit.item_id == "L-01"
    # L-01 归档（active 集只剩 L-02）：旧行仍在库（回填可重算）但检索不认
    only_l02 = frozenset({"L-02"})
    archived_view = rag.LessonDeduper(
        embedder=embedder, duck_con=duck_con, active_ids=only_l02, threshold=0.1
    )
    assert archived_view.nearest(near) is None
    # 空 active 集（教训库缺席降级面）：无近邻恒 None
    empty_view = rag.LessonDeduper(
        embedder=embedder, duck_con=duck_con, active_ids=frozenset(), threshold=0.1
    )
    assert empty_view.nearest(near) is None
    assert rag.active_lesson_ids(tmp_path) == both_active


def test_pending_count_catches_hash_drift(
    rag_env: tuple[sqlite3.Connection, duckdb.DuckDBPyConnection],
    tmp_path: Path,
) -> None:
    """correctness F3：落后判据带 hash 维——已嵌条目文本修订后 pending>0。"""
    conn, _duck = rag_env
    embedder = HashEmbedder()
    _seed_lesson_file(tmp_path, "L-01", cause="旧死因措辞")
    rag.embed_pending(
        conn,
        namespace=rag.NAMESPACE_LESSON,
        items=rag.lesson_texts(tmp_path),
        embedder=embedder,
    )
    assert (
        rag.pending_count(
            conn,
            namespace=rag.NAMESPACE_LESSON,
            items=rag.lesson_texts(tmp_path),
            model_id=embedder.model_id,
        )
        == 0
    )
    # 文本修订（cause 改写）：无新条目但 hash 不符 → 落后（该重嵌了）
    _seed_lesson_file(tmp_path, "L-01", cause="新死因措辞——修订版")
    assert (
        rag.pending_count(
            conn,
            namespace=rag.NAMESPACE_LESSON,
            items=rag.lesson_texts(tmp_path),
            model_id=embedder.model_id,
        )
        == 1
    )


def test_embed_pending_max_new_advances_backfill(
    rag_env: tuple[sqlite3.Connection, duckdb.DuckDBPyConnection],
) -> None:
    """correctness F5：limit=最多嵌 N 条新条目——同参数重跑能推进分段回填。"""
    conn, _duck = rag_env
    embedder = HashEmbedder()
    items = [(f"L-{i:02d}", f"死法措辞 {i}") for i in range(1, 4)]
    first = rag.embed_pending(
        conn, namespace="lesson", items=items, embedder=embedder, max_new=2
    )
    # seen=扫过计数（预算外新条目已读不嵌，止扫留余量给下段）
    assert first == {"seen": 3, "embedded": 2, "skipped": 0}
    # 同参数重跑：前 2 已嵌跳过，第 3 条是新条目 → embedded=1（推进而非 0）
    second = rag.embed_pending(
        conn, namespace="lesson", items=items, embedder=embedder, max_new=2
    )
    assert second == {"seen": 3, "embedded": 1, "skipped": 2}
    third = rag.embed_pending(
        conn, namespace="lesson", items=items, embedder=embedder, max_new=2
    )
    assert third == {"seen": 3, "embedded": 0, "skipped": 3}
    # max_new=0：零嵌入零扫描
    more = [("L-04", "死法措辞 4")]
    zero = rag.embed_pending(
        conn, namespace="lesson", items=more, embedder=embedder, max_new=0
    )
    assert zero == {"seen": 1, "embedded": 0, "skipped": 0}


def test_guardian_stream_tolerates_poison_lines(tmp_path: Path) -> None:
    """correctness F2：bronze 毒丸行/截断 gzip 逐行容错——跳过计数不炸流。"""
    import gzip as _gzip

    from goalx_backend.data.corpus_store import CorpusStore

    store = CorpusStore(tmp_path / "corpus")
    store.append_bronze(
        "guardian",
        "news_article",
        [_guardian_envelope("art-1", "Good title", "Good body.")],
    )
    # 毒丸①：半行 JSON（崩溃半写形态）——新 gzip member 里一行残文
    path = store.bronze_path("guardian", "news_article")
    with path.open("ab") as raw_fh:
        with _gzip.GzipFile(fileobj=raw_fh, mode="ab", mtime=0) as fh:
            fh.write(b'{"sid": "art-2", "parser_ver\n')
        # 毒丸②：尾随垃圾字节（截断 member 形态）
        raw_fh.write(b"\x00\x01garbage-not-gzip")
    stream = rag.guardian_article_texts(store)
    items = list(stream)
    assert items == [("art-1", "Good title\n\nGood body.")]
    assert stream.bad_lines == 1  # 残行计数可观测
    assert stream.truncated  # 截断止读可观测
    store.close()


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
    deduper = rag.LessonDeduper(
        embedder=embedder, duck_con=duck_con, active_ids=frozenset({"L-01"})
    )
    assert deduper.nearest("任何提案措辞") is None
    assert embedder.query_calls == 0  # 先查索引再嵌入：空索引零成本


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """测试本地余弦（独立参考式：模版公式，非复用被测实现）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


# F4 真分布校准语料：近邻对=教训摘要行 vs 措辞重述的提案 statement；
# 无关对=不同研究域的死法面（阵型/底蕴 vs 口径/池面）。真 MiniLM 上
# 两组应清晰分离；本测红了=LESSON_SIMILAR_THRESHOLD 该挪进实测窗。
_CALIBRATION_NEAR: tuple[tuple[str, str], ...] = (
    (
        "摘要：JC 平赔带；英冠中下游——未扣水口径的数字禁止进判读",
        "竞彩平赔带对英冠中下游的定价偏差读数不该用未扣水口径进判读面",
    ),
    (
        "摘要：池面终局份额；热门段——池人群份额低估热门的 O-S 偏差是结构性的",
        "足球池终局份额在热门段被人群系统性低估的假设值得复测",
    ),
    (
        "摘要：xG 赛后值；特征面——赛后实际值 xG 禁入特征（PIT 违例）",
        "用赛后实际 xG 值做特征面会重演 PIT 前视违例的老死法",
    ),
    (
        "摘要：搜索/验证/确认切分；多重检验——三段各只碰一次的纪律不可绕",
        "在已扫过的段上再要一次显著的提案违反三段切分纪律",
    ),
)
_CALIBRATION_FAR: tuple[tuple[str, str], ...] = (
    (
        "摘要：JC 平赔带；英冠中下游——未扣水口径的数字禁止进判读",
        "阵型族 4-2-3-1 与 3-5-2 对高空球争顶成功率的条件差异",
    ),
    (
        "摘要：池面终局份额；热门段——池人群份额低估热门的 O-S 偏差是结构性的",
        "顶级联赛连续年限底蕴因子对保级队开赛慢热的解释力",
    ),
    (
        "摘要：xG 赛后值；特征面——赛后实际值 xG 禁入特征（PIT 违例）",
        "换帅后首三场角球数与射门转化率的教练任期效应",
    ),
    (
        "摘要：搜索/验证/确认切分；多重检验——三段各只碰一次的纪律不可绕",
        "周六下午开球时段的市场流动性对亚盘水位斜率的影响",
    ),
)


@pytest.mark.slow
@pytest.mark.skipif(
    not _MODEL_CACHED, reason="本机 fastembed 模型缓存缺席（CI 跳过，不真下模型）"
)
def test_fastembed_threshold_calibration() -> None:
    """correctness F4 真分布校准：近邻/无关两组余弦分得开且阈在窗内。

    回填后机器上一跑即知：红在第一断言=两组交叠（0.85 线本身救不了，
    需换对称化文本或模型）；红在第二断言=分离但阈不在窗内——把
    LESSON_SIMILAR_THRESHOLD 挪进 (far_max, near_min] 实测窗（常量
    注释即本测指针）。不猜阈值：窗由实测分布给。
    """
    embedder = rag.FastembedEmbedder()
    lib_texts = [lib for lib, _ in _CALIBRATION_NEAR]
    lib_vecs = embedder.embed_passages(lib_texts)
    near_cos = [
        _cosine(vec, embedder.embed_query(statement))
        for vec, (_, statement) in zip(lib_vecs, _CALIBRATION_NEAR, strict=True)
    ]
    far_lib_vecs = embedder.embed_passages([lib for lib, _ in _CALIBRATION_FAR])
    far_cos = [
        _cosine(vec, embedder.embed_query(statement))
        for vec, (_, statement) in zip(far_lib_vecs, _CALIBRATION_FAR, strict=True)
    ]
    near_min, far_max = min(near_cos), max(far_cos)
    # 分布可见性：-s 直跑时 stdout 出两组余弦（阈值调整的实测依据）
    import sys as _sys

    _sys.stdout.write(f"near_cos={near_cos} far_cos={far_cos}\n")
    assert near_min > far_max  # 分离性：最弱近邻仍强于最强无关
    assert far_max < rag.LESSON_SIMILAR_THRESHOLD <= near_min  # 阈在实测窗内


def test_current_model_id_zero_download() -> None:
    """模型身份串（model_id）可零成本取用：晨检/运维只读口，不构造模型。"""
    identity = rag.current_model_id()
    assert identity.startswith(rag.FastembedEmbedder.MODEL_NAME + "@fastembed-")
    # 实例身份与模块口一致；dim=384 落账（MiniLM 多语线）
    embedder = rag.FastembedEmbedder()
    assert embedder.model_id == identity
    assert embedder.dim == 384


# ---- 真 fastembed 路径冒烟（模型缓存缺席 skip，_CORPUS_DUCK 先例） ----


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
    assert migrate(db) == 57
    assert migrate(db) == 57  # 同版本重跑零效果
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
