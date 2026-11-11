# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
"""
RAG 两点（vision-build 票 06）：卫报语料检索 + 教训条目查重的最小向量面。

后端裁定（人裁 3，撤回 sqlite-vec 荐）：**DuckDB 纯 SQL 暴力余弦，零新
服务/零新框架/零新存储面**——embeddings 落主库（v56 ``rag_embeddings``
一张通用表，namespace 隔离两索引），检索时 DuckDB ATTACH 主库只读、
``CAST vec AS FLOAT[N]`` 物化 FLOAT[] 后 ``array_cosine_similarity``
list 函数暴力扫（确定性可复算，无近似索引）。查询全异步批处理：全库
扫 203K×1024 数秒级、过滤子集毫秒级，够用。
shortcut（照票面）：若未来出现交互式延迟需求或语料量级×10，再升
VSS/sidecar。

embedding 模型（2026-11-11 人裁终版：业界调研后弃票面 e5-small）：
fastembed 0.9.1 清单**没有** multilingual-e5-small（e5 线仅 -large 2.24GB，
无自定义加载缝）；过渡替身 MiniLM-L12（2020 年模型，跨语检索弱）亦被
替换。现选 ``Qwen/Qwen3-Embedding-0.6B-Q``（fastembed 0.9.1 原生支持，
ONNX int8 量化 1.12GB/1024 维/32k 上下文/Apache-2.0，2025 年发布，
MTEB 多语跨语检索同档最强——中文提案/教训 ↔ 英文卫报语料的跨语命中
正是本索引主用途）。Qwen3 前缀约定：query 侧 Instruct+Query 双行、文档侧无前缀——前缀在
FastembedEmbedder 内自理（见该类 docstring），调用方零改动。模型 id/版本落账
（``{模型名}@fastembed-{版本}`` 进主键），换模型=对新 model_id 重嵌
（增量管线幂等，旧口径行原地保留并行可查）。

纪律（票面）：
- RAG 只做检索与查重，不做概率引擎；
- 引用纪律：检索产出必带引用 id（文章 sid/条目 L-NN）——管线层缺 id
  直接拒（``citation_reasons`` 拒收断言）；
- 增量入口=CLI（``rag-embed --namespace``）+晨检只报落后条数（只读
  红线：晨检永不构造 embedder、零写入）；
- 教训索引 active-only（correctness F1）：archived/review_due 不进
  索引；检索按构造时 active id 快照过滤（归档教训不拦新提案）；
- 卫报 bronze 毒丸行/截断 gzip 逐行容错（correctness F2：残数据黄口
  可见，不炸晨检/嵌入流）。

测试不依赖真模型：embedder 全注入（假件=确定性哈希向量）；真
fastembed 路径一条冒烟（模型缓存缺席 skip，test_bucket_face 的
_CORPUS_DUCK 先例）。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sqlite3
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import duckdb
from loguru import logger

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.evaluation import lessons

#: 命名空间①：卫报语料（v0 篇级 chunk——item_id=文章 sid，全文一条向量；
#: 段级 chunk 后置留缝：届时 item_id 形如 ``{sid}#p{n}``，表结构不变）
NAMESPACE_GUARDIAN = "guardian_article"
#: 命名空间②：教训条目（item_id=L-NN）
NAMESPACE_LESSON = "lesson"
#: embed 批大小（增量管线逐批落库，中途崩溃已落批不回退）。
#: 2026-11-11 卫报全量回填实录：batch 64×长文（Qwen3 32k 不截断）在
#: 16GB 机上峰值超限被 jetsam SIGKILL（exit 137）——16 篇/批+512 token
#: 截断后模型常驻 ~2.6GB、批内峰值受控。
EMBED_BATCH = 16
#: 教训语义近邻查重阈（周批 duplicate_reason 缝消费；高精度取向——
#: 错拒一条提案的代价大于漏拒，阈取实测窗中偏下）。
#: 2026-11-11 真分布校准（Qwen3-0.6B-Q，test_fastembed_threshold_calibration
#: 实测）：near（复现死法重述）0.66–0.86 / far（跨域无关）0.26–0.37，
#: 分离窗 (0.37, 0.66]——0.85 先验值过高（4 对近邻仅 1 对过阈）改 0.55。
#: 换模型后本常量须随校准测重定。
LESSON_SIMILAR_THRESHOLD = 0.55


class Embedder(Protocol):
    """embedding 注入缝：模型 id/维度落账 + passage/query 双面。"""

    @property
    def model_id(self) -> str:
        """身份串（模型名@运行时版本）——进表主键，换模型=重嵌。"""
        ...

    @property
    def dim(self) -> int:
        """输出维度（定长；检索侧 CAST FLOAT[N] 的一致性基准）。"""
        ...

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """批量语料/条目向量（异步批处理面：一批一落库）。"""
        ...

    def embed_query(self, text: str) -> list[float]:
        """单条查询向量（与 passage 同口径；e5 类不对称模型在此加前缀）。"""
        ...


class _TextEmbeddingModel(Protocol):
    """fastembed TextEmbedding 的最小消费面（embed/query_embed 出向量迭代器）。"""

    def embed(self, documents: list[str]) -> Iterable[Iterable[float]]:
        """批量嵌入（迭代器面）。"""
        ...

    def query_embed(self, queries: list[str]) -> Iterable[Iterable[float]]:
        """查询嵌入（迭代器面）。"""
        ...


class FastembedEmbedder:
    """
    生产 embedder：fastembed（ONNX 运行时，不进 torch）+ Qwen3 多语 1024 维。

    模型惰性构建：构造零成本（晨检/管线装配不触发模型下载），首次
    embed 才拉模型（运行期下载，CI/测试不得依赖——测试注入假件）。
    Qwen3-Embedding 前缀约定（模型卡）：query 侧 Instruct 行+Query 行、
    文档侧无前缀——前缀在本类内自理（embed_query 拼 Instruct 后走
    model.embed，不经 fastembed query_embed 的内建前缀，口径自控可复算）。
    Instruct 文案是查询侧配方（改它不动文档索引，无需重嵌）。
    """

    MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B-Q"
    DIM = 1024
    # 查询侧任务指令（Qwen3 官方配方；中文提问跨语命中英文语料是本索引的主用途）
    QUERY_INSTRUCT = (
        "Given a betting research query (Chinese or English), retrieve "
        "relevant football betting lessons, hypotheses and news articles"
    )

    def __init__(self) -> None:
        self._model: _TextEmbeddingModel | None = None

    @property
    def model_id(self) -> str:
        """``{模型名}@fastembed-{版本}``（importlib.metadata 落账，可复算）。"""
        return current_model_id()

    @property
    def dim(self) -> int:
        """输出维度（模型档案钉死 1024）。"""
        return self.DIM

    def _ensure(self) -> _TextEmbeddingModel:
        """惰性构建（首次 embed 拉模型；CPU provider 钉死，无 GPU 面）。"""
        if self._model is None:
            from fastembed import TextEmbedding  # noqa: PLC0415 运行期下载面

            # fastembed 0.9 有标注但签名面宽（batch_size/parallel/**kwargs、
            # NumpyArray 出参）——cast 收窄到本模块消费面（运行期行为同构）
            model = cast(
                "_TextEmbeddingModel",
                TextEmbedding(
                    model_name=self.MODEL_NAME,
                    providers=["CPUExecutionProvider"],
                    # fastembed 缺省缓存落系统临时目录（macOS 定期清空→静默重下
                    # 1.1GB）；钉持久目录（与测试缓存探测同路径）
                    cache_dir=str(Path.home() / ".cache" / "fastembed"),
                    # 截断与批是嵌入配方的一部分（改动=换口径重嵌考量）：
                    # 512 token=v0 篇级检索取头部（标题+导语承载语义主体，
                    # 段级留缝）；batch_size 压内存（缺省 256 会在长文上
                    # 撑爆 16GB 机——见 EMBED_BATCH 注释）。
                    max_length=512,
                    batch_size=EMBED_BATCH,
                ),
            )
            self._model = model
            return model
        return self._model

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """批量 passage 向量（文档侧无前缀，Qwen3 模型卡约定）。"""
        model = self._ensure()
        return [[float(x) for x in vec] for vec in model.embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        """单条 query 向量（拼 Instruct+Query 前缀后走 embed，口径自控）。"""
        model = self._ensure()
        prefixed = f"Instruct: {self.QUERY_INSTRUCT}\nQuery: {text}"
        vec = next(iter(model.embed([prefixed])))
        return [float(x) for x in vec]


def current_model_id() -> str:
    """当前 RAG 模型身份串（晨检/运维只读口——零模型下载零构造）。"""
    version = importlib.metadata.version("fastembed")
    return f"{FastembedEmbedder.MODEL_NAME}@fastembed-{version}"


@dataclass(frozen=True)
class RagHit:
    """一条检索命中（引用纪律：item_id 必带——文章 sid/条目 L-NN）。"""

    namespace: str
    item_id: str
    score: float


def content_hash(text: str) -> str:
    """嵌入文本 sha256（增量幂等判据+回填可复算的输入侧落账）。"""
    return hashlib.sha256(text.encode()).hexdigest()


def _existing_hashes(
    conn: sqlite3.Connection, namespace: str, model_id: str
) -> dict[str, str]:
    """现拍已嵌条目的 {item_id: content_hash}（增量跳过判据面）。"""
    rows = conn.execute(
        """
        SELECT item_id, content_hash FROM rag_embeddings
        WHERE namespace = ? AND model_id = ?
        """,
        (namespace, model_id),
    ).fetchall()
    return {str(row["item_id"]): str(row["content_hash"]) for row in rows}


def _flush_batch(
    conn: sqlite3.Connection,
    namespace: str,
    embedder: Embedder,
    batch: list[tuple[str, str, str]],
    stats: dict[str, int],
) -> None:
    """
    一批落库：嵌入→校验（条数/维度 fail-loud）→upsert 单事务。

    upsert 覆盖文本变更行（hash 已变才进 batch）；stats["embedded"] 就地
    累加，batch 清空（embed_pending 的批处理单元）。
    """
    vectors = embedder.embed_passages([text for _, text, _ in batch])
    if len(vectors) != len(batch):
        raise ValueError(f"embedder 返回向量数 {len(vectors)} != 输入条数 {len(batch)}")
    with atomic(conn):
        for (item_id, _text, digest), vec in zip(batch, vectors, strict=True):
            if len(vec) != embedder.dim:
                raise ValueError(
                    f"{item_id}: 向量维度 {len(vec)} != embedder.dim {embedder.dim}"
                )
            conn.execute(
                """
                INSERT INTO rag_embeddings
                    (namespace, item_id, model_id, dim, vec,
                     content_hash, embedded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (namespace, item_id, model_id) DO UPDATE SET
                    dim = excluded.dim, vec = excluded.vec,
                    content_hash = excluded.content_hash,
                    embedded_at = excluded.embedded_at
                """,
                (
                    namespace,
                    item_id,
                    embedder.model_id,
                    embedder.dim,
                    json.dumps(vec, separators=(",", ":")),
                    digest,
                    utc_now_iso(),
                ),
            )
    stats["embedded"] += len(batch)
    batch.clear()


def embed_pending(
    conn: sqlite3.Connection,
    *,
    namespace: str,
    items: Iterable[tuple[str, str]],
    embedder: Embedder,
    batch_size: int = EMBED_BATCH,
    max_new: int | None = None,
) -> dict[str, int]:
    """
    增量 embed（幂等）：新条目嵌入落库，content_hash 相同跳过。

    items 流式（卫报 bronze 20 万篇不驻内存）；同 item_id 首见为准
    （bronze append-only 去重与 guardian_silver.candidate_articles 同序）；
    文本变更（hash 变）→ 重嵌 upsert 覆盖。逐批一个事务：中途崩溃已落
    批保留，重跑按 hash 续（幂等）。max_new=**最多嵌 N 条新条目**
    （correctness F5：过滤 known 后计数——同参数重跑能推进分段回填，
    不是前缀截断；None=全量）。返回 {seen, embedded, skipped}。
    """
    stats = {"seen": 0, "embedded": 0, "skipped": 0}
    known = _existing_hashes(conn, namespace, embedder.model_id)
    seen_ids: set[str] = set()
    batch: list[tuple[str, str, str]] = []

    def flush() -> None:
        if batch:
            _flush_batch(conn, namespace, embedder, batch, stats)

    new_count = 0
    for item_id, text in items:
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        stats["seen"] += 1
        digest = content_hash(text)
        if known.get(item_id) == digest:
            stats["skipped"] += 1
            continue
        if max_new is not None and new_count >= max_new:
            # 新条目预算用尽即止扫（F5：下段重跑从此处续）
            flush()
            break
        batch.append((item_id, text, digest))
        new_count += 1
        if len(batch) >= batch_size:
            flush()
    flush()
    return stats


def connect_rag(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    """
    RAG 检索侧 DuckDB 连接：内存 duck + 主库只读 ATTACH。

    别名 ``goalx`` 与 corpus_duckdb 桥同惯例（表名字面量钉死，ADR-0008
    静态审可见）。也可直接复用语料桥连接（corpus_duckdb.connect 已带
    同名 ATTACH）。调用方负责 close()。
    """
    con = duckdb.connect()
    face = Path(db_path).resolve().as_posix()
    try:
        con.execute(
            # 路径来自调用方 Settings/测试 tmp，非用户输入（corpus_duckdb 先例）
            f"ATTACH '{face}' AS goalx (TYPE sqlite, READ_ONLY)"
        )
    except BaseException:
        # ATTACH 失败不留孤儿连接（correctness F6）
        con.close()
        raise
    return con


def search(
    duck_con: duckdb.DuckDBPyConnection,
    *,
    namespace: str,
    model_id: str,
    query_vec: Sequence[float],
    top_k: int,
    only_ids: Collection[str] | None = None,
) -> list[RagHit]:
    """
    暴力余弦检索（人裁 3）：namespace×model_id 过滤子集内全扫打分。

    确定性：同向量集同查询必同结果（无近似索引）；排序 score DESC、
    item_id ASC（平分稳定）。查询向量维度与库内 dim 不符 = DuckDB
    定长 CAST 显式报错（不静默空）；零向量行得 -1（DuckDB 口径，
    排序自然沉底）。查询侧前缀（e5 类）已由 embedder.embed_query 承担。
    only_ids=引用/新鲜面子集过滤（correctness F1：教训检索只认
    active 面——SQL 内过滤非事后剔除，archived 高分行不得挤占 top-k）；
    空集=无行命中（教训库缺席降级面）。
    """
    dim = len(query_vec)
    if dim == 0:
        raise ValueError("查询向量为空（embedder 输出异常）")
    ids_filter = "AND item_id IN (SELECT unnest(?))\n" if only_ids is not None else ""
    rows = duck_con.execute(
        # dim 经 int 校验后内插（float32 定长数组类型字面量）；表名/过滤值
        # 全字面量与参数——ADR-0008 静态审可见
        f"""
        SELECT item_id, array_cosine_similarity(
            CAST(vec AS FLOAT[{dim}]), CAST(? AS FLOAT[{dim}])) AS score
        FROM goalx.rag_embeddings
        WHERE namespace = ? AND model_id = ?
        {ids_filter}ORDER BY score DESC, item_id ASC
        LIMIT ?
        """,  # noqa: S608
        [list(query_vec), namespace, model_id]
        + ([sorted(only_ids)] if only_ids is not None else [])
        + [top_k],
    ).fetchall()
    return [
        RagHit(namespace=namespace, item_id=str(row[0]), score=float(row[1]))
        for row in rows
    ]


# ---- 索引源②：教训条目（票 05 解析面 → 嵌入文本） ----


def lesson_texts(directory: Path | None = None) -> list[tuple[str, str]]:
    """
    教训索引源：**active 条目** → (lesson id, 合成嵌入文本)。

    active-only（correctness F1）：archived/review_due 不进索引——归档
    教训无删除面，进了就永久拦新提案。嵌入文本=摘要行+触发面+死因+
    正文：摘要行（triggers+cause 一行，F4）供查重匹配——周批 statement
    是单句措辞，库侧单句摘要面对称化查询形态，降低不对称对余弦系统性
    偏低；触发/死因/正文保留原语义面（证据检索同用）。evidence 是账本
    回指（id 串）非语义面，不进文本。目录缺席 fail-loud（lessons 同款，
    受控文档宁可红不可猜）。
    """
    loaded = lessons.load_lessons(directory)
    return [
        (
            lesson.id,
            "摘要："
            + "；".join(lesson.triggers)
            + "——"
            + lesson.cause
            + "\n触发："
            + "；".join(lesson.triggers)
            + "\n死因："
            + lesson.cause
            + "\n正文："
            + lesson.body,
        )
        for lesson in loaded
        if lesson.status == "active"
    ]


def active_lesson_ids(directory: Path | None = None) -> frozenset[str]:
    """
    当前 active 教训 id 集（检索面过滤判据；LessonDeduper 消费）。

    旧行留在 rag_embeddings（回填可重算），检索只认本集（correctness
    F1：归档教训不再拦新提案）。目录缺席/破损 fail-loud——调用方
    （tasks 适配件）自行降级为空集=查重退精确面。
    """
    return frozenset(
        lesson.id
        for lesson in lessons.load_lessons(directory)
        if lesson.status == "active"
    )


# ---- 索引源①：卫报语料（bronze 篇级 chunk，v0） ----

#: 卫报语料 bronze 数据集（票 79 采集面：provider/dataset 字面量与
#: data.ingest.guardian_silver 同源；evaluation 低于 data.ingest 层，
#: 不可上翻 import，故本地钉字面量+回指注释）
_GUARDIAN_PROVIDER = "guardian"
_GUARDIAN_DATASET = "news_article"


class GuardianArticleStream:
    """
    卫报索引源流：bronze 逐行 → (文章 sid, 篇级文本 title+body)。

    毒丸容错（correctness F2）：半行 JSON/坏编码行跳过计数
    （``bad_lines``），gzip 流损坏（截断 member）止读置 ``truncated``
    ——bronze 崩溃半写不得炸晨检整检（消费方把两计数带进黄口 detail）；
    嵌入侧下次修复后续跑（幂等由 content_hash 承担）。计数在 __iter__
    消费时累加，消费完毕后可读。
    """

    def __init__(self, store: CorpusStore, *, limit: int | None = None) -> None:
        self._store = store
        self._limit = limit
        self.bad_lines = 0
        self.truncated = False

    def __iter__(self) -> Iterator[tuple[str, str]]:
        """流式迭代（计数随消费累加，消费完毕后可读）。"""
        self.bad_lines = 0
        self.truncated = False
        seen: set[str] = set()
        produced = 0
        try:
            for line in self._store.iter_bronze_lines(
                _GUARDIAN_PROVIDER, _GUARDIAN_DATASET
            ):
                if self._limit is not None and produced >= self._limit:
                    break
                try:
                    row: dict[str, Any] = json.loads(line)
                except ValueError:  # json.JSONDecodeError/UnicodeDecodeError
                    self.bad_lines += 1
                    continue
                payload = row.get("payload") or {}
                item_id = str(row.get("sid") or payload.get("id") or "")
                title = str(payload.get("webTitle") or "")
                body = str(payload.get("bodyText") or "")
                if (
                    not item_id
                    or item_id in seen
                    or not title.strip()
                    or not body.strip()
                ):
                    continue
                seen.add(item_id)
                produced += 1
                yield item_id, f"{title}\n\n{body}"
        except (OSError, EOFError) as exc:
            # 截断/损坏 gzip member：止读留痕，已产行照常产出
            self.truncated = True
            logger.error(
                "guardian bronze 流损坏止读（{}）：已产 {} 行 / 残行 {}",
                exc,
                produced,
                self.bad_lines,
            )


def guardian_article_texts(
    store: CorpusStore, *, limit: int | None = None
) -> GuardianArticleStream:
    """
    卫报索引源入口（流对象：``list()``/for 消费同生成器）。

    v0 篇级 chunk（一篇一条向量）；段级留缝（item_id 形如 ``{sid}#p{n}``，
    表结构不变）。sid 去重首见为准（append-only bronze 重採行不重嵌，
    guardian_silver.candidate_articles 同序）；无 sid/缺标题或正文残行
    跳过。limit=有界护栏（首夜/试点）。流式——20 万篇全文不驻内存，
    embed_pending 同流式消费；毒丸容错与计数见 GuardianArticleStream。
    """
    return GuardianArticleStream(store, limit=limit)


# ---- 周批查重缝：教训索引语义近邻（票 03 精确回指 → 票 06 升级消费口） ----


@dataclass(frozen=True)
class LessonDeduper:
    """
    教训索引语义近邻查重缝：statement 嵌入 → 教训命名空间 top-1 余弦。

    周批 duplicate_reason 消费（weekly_agent 注入；票 03 留缝的升级口）：
    近邻（cos ≥ 阈值）=复现死法 → 拒收理由 ``lesson_similar:L-NN``。
    embedder 与 duck 连接全注入——生产=FastembedEmbedder+connect_rag
    （tasks 适配件），测试=确定性假件。教训索引空（未回填）=无近邻
    恒 None，查重退回票 03 精确面（如实降级不伪造）。
    """

    embedder: Embedder
    duck_con: duckdb.DuckDBPyConnection
    #: active 教训 id 集（correctness F1：检索只认当前 active 面——旧
    #: 向量行留库可重算，归档教训不得再拦新提案）。空集=教训库缺席
    #: 降级面（无近邻恒 None）；构造时快照（拍内一致）。
    active_ids: frozenset[str]
    threshold: float = LESSON_SIMILAR_THRESHOLD

    def _index_ready(self) -> bool:
        """教训索引非空才值得嵌入查询（空索引=未回填，先查后嵌零成本降级）。"""
        row = self.duck_con.execute(
            """
            SELECT 1 FROM goalx.rag_embeddings
            WHERE namespace = ? AND model_id = ? LIMIT 1
            """,
            [NAMESPACE_LESSON, self.embedder.model_id],
        ).fetchone()
        return row is not None

    def nearest(self, statement: str) -> RagHit | None:
        """top-1 active 教训近邻；余弦 ≥ 阈值才返回（否则 None=非近邻）。"""
        if not self.active_ids or not self._index_ready():
            return None
        hits = search(
            self.duck_con,
            namespace=NAMESPACE_LESSON,
            model_id=self.embedder.model_id,
            query_vec=self.embedder.embed_query(statement),
            top_k=1,
            only_ids=self.active_ids,
        )
        if hits and hits[0].score >= self.threshold:
            return hits[0]
        return None


def pending_count(
    conn: sqlite3.Connection,
    *,
    namespace: str,
    items: Iterable[tuple[str, str]],
    model_id: str,
) -> int:
    """
    只读落后计数（晨检挂项消费，correctness F3 判据带 hash 维）。

    落后=源条目在当前 model 口径下无行**或文本变更**（content_hash
    不符）——已嵌条目修订后晨检不得仍绿灯。只数不嵌（晨检只读
    红线）；items 单遍消费（卫报 20 万篇流式）。
    """
    known = _existing_hashes(conn, namespace, model_id)
    # F3：判据带 hash 维——已嵌条目文本修订（hash 不符）也算落后，
    # 否则晨检对「该重嵌」绿灯
    return sum(1 for item_id, text in items if known.get(item_id) != content_hash(text))


# ---- 引用纪律：检索产出必带引用 id（票 11 裁定延伸到检索面） ----


def citation_reasons(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """
    管线层拒收闸：检索产出（证据行）缺引用 id 直接拒（返回拒收理由）。

    检索消费方（EM/NewsShock/周批提案的证据面）把检索结果拼进 prompt/
    提案前过本闸：每行必须有非空字符串 item_id（文章 sid/条目 L-NN），
    缺 id 的行产出理由——带理由的行由调用方剔除留痕（weekly_agent
    拒收清单同构）。空清单=全部过审。
    """
    reasons: list[str] = []
    for position, row in enumerate(rows):
        item_id = row.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            reasons.append(
                f"检索产出缺引用 id（第 {position} 行 item_id={item_id!r}，"
                + "票 11 裁定管线层拒收）"
            )
    return reasons
