"""
源T 历史时点 as-of 青铜重放（报告审计 2026-11-04 票 04）。

silver odds_change_event 是**最新视图**：每 sid 只保留最新 bronze 信封，
行 observed_at=该信封 fetched_at——再物化（同载荷或变载荷重抓）会把整
段轨迹的观测时刻整体后移，早于最新观测的历史 as-of 查询在 silver 上
"消失"（报告一案例 6：06:00 再物化后查 05:31 从 6 行变 0 行）。

本模块提供读侧最小修复（人裁决 2026-11-04：读侧 as-of bronze 先行，
不引入决策时冻结快照、不做双时态全库改造）：as_of 早于 silver 最新
观测时，从 bronze 选 ``fetched_at <= as_of`` 的**可见信封**（重放语义
=本机当时可见的才进 as-of 决策），走物化同一转换
（srct_odds.kept_1x2_event_rows）产出伪快照行；bronze 追加不删，重放
跨再物化天然稳定。证据回查：伪行 observed_at=可见信封 fetched_at，
(provider, dataset, sid, fetched_at) 即信封指针。

分层（ADR-0008）：本模块在 ingest 层（转换属源T 语料域），产出经
``rows=`` 注入缝进入 data.quote_evidence 的共识/判定——data 层不依赖
ingest，铜/银两路径对消费方同形（SnapshotRow）。

shortcut: 逐 sid 全文件线性扫（GB 级 ~10s 量级）+ 进程内信封缓存
（32 sid FIFO）——检查点/批报告够用；页面热路径恒走 silver 快路径
（as_of ≥ 最新观测不触发本模块）。前瞻评分大批量（票 05）走
batch_asof_rows：wanted sid 集一次扫描摊销。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import duckdb
from loguru import logger

from goalx_backend.data import quote_evidence as qe
from goalx_backend.data import silver
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_odds
from goalx_backend.db import utc_now_iso

DuckCon = duckdb.DuckDBPyConnection

# 青铜信封进程内缓存：历史重放逐 sid 全文件扫，检查点多时点复用同 sid
# 信封集。键=(语料根, sid, bronze 文件锚 size:mtime)——同根重建/追加
# 都会换锚触发重扫（correctness F2：单调追加下旧缓存本可复用，但非单调
# 回填与同根重建会翻答案，锚判据一视同仁）。
_ENVELOPES_CACHE: dict[tuple[str, str, str], list[dict[str, object]]] = {}
_ENVELOPES_CACHE_MAX = 32


def _cached_envelopes(store: CorpusStore, sid: str) -> list[dict[str, object]]:
    """该 sid 的当前版本 bronze 信封集（文件序=抓取序，带锚键缓存）。"""
    root = str(store.root)
    anchor = store.bronze_anchor(srct.SRCT_PROVIDER, srct.ODDS_DATASET)
    key = (root, sid, anchor)
    cached = _ENVELOPES_CACHE.get(key)
    if cached is None:
        for stale in [k for k in _ENVELOPES_CACHE if k[0] == root and k[1] == sid]:
            _ENVELOPES_CACHE.pop(stale)  # 同根同 sid 旧锚条目不残留
        cached = silver.bronze_envelopes_for_sid(
            store,
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
            sid,
        )
        if len(_ENVELOPES_CACHE) >= _ENVELOPES_CACHE_MAX:
            _ENVELOPES_CACHE.pop(next(iter(_ENVELOPES_CACHE)))
        _ENVELOPES_CACHE[key] = cached
    return cached


def _ms_to_iso(ms: int | None) -> str | None:
    """毫秒 epoch → UTC iso 秒（与 silver 路径伪行时间格式一致）。"""
    return (
        datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")
        if ms is not None
        else None
    )


def _visible_envelope(
    envelopes: list[dict[str, object]], cutoff: datetime, exclusive: bool
) -> dict[str, object] | None:
    """文件序（=抓取序）里最后一个 fetched_at 满足 cutoff 的信封。"""
    visible: dict[str, object] | None = None
    for env in envelopes:
        try:
            fetched = qe.parse_ts(str(env["fetched_at"]))
        except ValueError:
            # correctness F1：坏 fetched_at 单信封跳过降级，不炸整份报告
            logger.warning(
                "srct asof bronze: 信封 fetched_at 不可解析, 跳过: {!r}",
                env.get("fetched_at"),
            )
            continue
        if (fetched < cutoff) if exclusive else (fetched <= cutoff):
            visible = env
    return visible


def _books_from_event_rows(
    rows: list[dict[str, object]], floor: datetime | None
) -> dict[str, tuple[float, float, float, str, str]]:
    """事件行 → 逐书 as-of 三向（ROW_NUMBER 的 python 镜像，与 qe 读桥同改）。"""

    def rank(row: dict[str, object]) -> tuple[bool, int, int, int]:
        # ROW_NUMBER 的 ORDER BY：published DESC NULLS LAST / observed / order
        pub = cast("int | None", row["published_at"])
        return (
            pub is not None,
            pub or 0,
            int(cast("int", row["observed_at"])),
            int(cast("int | None", row["source_order"]) or 0),
        )

    best: dict[str, dict[str, object]] = {}
    for row in rows:
        observed_ms = cast("int | None", row["observed_at"])
        if observed_ms is None:
            continue
        if (
            floor is not None
            and datetime.fromtimestamp(observed_ms / 1000, UTC) < floor
        ):
            continue
        book = str(row["bookmaker_id"])
        current = best.get(book)
        if current is None or rank(row) > rank(current):
            best[book] = row
    by_book: dict[str, tuple[float, float, float, str, str]] = {}
    for book, row in best.items():
        home, draw, away = row["odds_home"], row["odds_draw"], row["odds_away"]
        if home is None or draw is None or away is None:
            continue
        pub_iso = _ms_to_iso(cast("int | None", row["published_at"]))
        obs_iso = _ms_to_iso(int(cast("int", row["observed_at"])))
        if obs_iso is None:
            continue
        captured = pub_iso if pub_iso is not None else obs_iso
        by_book[book] = (
            float(cast("float", home)),
            float(cast("float", draw)),
            float(cast("float", away)),
            captured,
            obs_iso,
        )
    return by_book


def asof_rows(
    store: CorpusStore,
    duck_con: DuckCon,
    sid: str,
    as_of: str,
    *,
    exclusive: bool = False,
    max_age_seconds: float | None = None,
) -> list[qe.SnapshotRow] | None:
    """
    历史时点青铜重放；None=不适用（调用方走 silver 最新视图路径）。

    触发条件：silver 有该 sid 行且 as_of 严格早于其最新观测——此时
    silver 行 observed_at 全部晚于 as_of，最新视图无法回答"当时可见
    什么"。bronze 无信封（版本漂移等）同样返回 None（不劣于修复前）。
    as_of 早于首抓时返回 []（当时本机零可见，空真不冒充）。
    """
    cutoff = qe.parse_ts(as_of)
    try:
        top = duck_con.execute(
            "SELECT MAX(observed_at), MAX(kickoff) FROM odds_change_event WHERE sid=?",
            [sid],
        ).fetchone()
    except duckdb.Error as exc:
        logger.warning("srct asof bronze: silver max probe degraded: {}", exc)
        return None
    if top is None or top[0] is None:
        return None  # silver 无该 sid 行：不劣于现状，回退既有路径
    if cutoff >= cast("datetime", top[0]):
        return None  # as_of 不早于最新观测：silver 路径即真相（快路径）
    envelopes = _cached_envelopes(store, sid)
    if not envelopes:
        return None
    visible = _visible_envelope(envelopes, cutoff, exclusive)
    if visible is None:
        return []
    kickoff_ms = int(top[1].timestamp() * 1000) if top[1] is not None else None
    scratch = srct_odds.SilverOddsReport(built_at=utc_now_iso())
    rows = srct_odds.kept_1x2_event_rows(visible, kickoff_ms, scratch)
    floor = cutoff - timedelta(seconds=max_age_seconds) if max_age_seconds else None
    by_book = _books_from_event_rows(rows, floor)
    return qe.pseudo_rows_capped(duck_con, by_book)


def _silver_max_probe_batch(
    duck_con: DuckCon, sids: list[str]
) -> dict[str, tuple[datetime, datetime | None]]:
    """Silver 逐 sid (最新观测, 最新开球) 一次批查（分块 IN，探测降级空表）。"""
    probe: dict[str, tuple[datetime, datetime | None]] = {}
    try:
        for i in range(0, len(sids), 900):  # 同 mapping._ID_CHUNK 保守分块
            chunk = sids[i : i + 900]
            ph = ", ".join("?" for _ in chunk)
            sql = (
                "SELECT sid, MAX(observed_at), MAX(kickoff) FROM odds_change_event"  # noqa: S608 占位符=分块形状，值全参数绑定
                f" WHERE sid IN ({ph}) GROUP BY sid"
            )
            rows = duck_con.execute(sql, chunk).fetchall()
            probe.update(
                {
                    str(r[0]): (cast("datetime", r[1]), r[2])
                    for r in rows
                    if r[1] is not None
                }
            )
    except duckdb.Error as exc:
        logger.warning("srct asof bronze: silver max probe (batch) degraded: {}", exc)
        return {}
    return probe


def _scan_visible_envelopes(
    store: CorpusStore,
    pending_by_sid: dict[str, list[tuple[str, datetime]]],
) -> tuple[dict[tuple[str, str], dict[str, object]], set[str]]:
    """
    单遍 bronze 扫（票 05 摊销主体）：逐 (sid, as_of) 留最后可见信封。

    返回 ((sid, as_of) → 可见信封, 出现过信封的 sid 集)。重放内存 =
    每请求一对信封（文件序后者覆盖前者）。
    """
    visible: dict[tuple[str, str], dict[str, object]] = {}
    seen: set[str] = set()
    for env in silver.iter_bronze_envelopes_for_sids(
        store,
        srct.SRCT_PROVIDER,
        srct.ODDS_DATASET,
        srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
        set(pending_by_sid),
    ):
        sid = str(env["sid"])
        seen.add(sid)
        try:
            fetched = qe.parse_ts(str(env["fetched_at"]))
        except ValueError:
            # correctness F1 同款：坏 fetched_at 单信封跳过降级，不炸整批
            logger.warning(
                "srct asof bronze: 信封 fetched_at 不可解析, 跳过: {!r}",
                env.get("fetched_at"),
            )
            continue
        for as_of, cutoff in pending_by_sid[sid]:
            if fetched <= cutoff:
                visible[(sid, as_of)] = env  # 文件序后者胜=当时最新可见
    return visible, seen


def batch_asof_rows(
    store: CorpusStore,
    duck_con: DuckCon,
    requests: list[tuple[str, str]],
) -> list[list[qe.SnapshotRow] | None]:
    """
    批量历史时点青铜重放（票 05 前瞻评分接线）；返回与 requests 对齐。

    与逐条 ``asof_rows`` 同语义（None=不适用回退 silver 路径 / []=as_of
    早于首抓空真 / 行=可见信封重放），差异只在成本：silver 探针与 bronze
    全文件扫各**一次**摊销到整个请求集（前瞻面数百 sid，逐 sid 扫 GB 级
    文件不可行——srct_asof 模块头 shortcut 兑现）。请求对 (sid, as_of)
    去重。
    """
    pairs = list(dict.fromkeys(requests))
    if not pairs:
        return []
    probe = _silver_max_probe_batch(duck_con, sorted({sid for sid, _ in pairs}))
    cutoffs: dict[tuple[str, str], datetime] = {}
    pending_by_sid: dict[str, list[tuple[str, datetime]]] = {}
    for sid, as_of in pairs:
        top = probe.get(sid)
        if top is None:
            continue  # silver 无该 sid 行：不劣于现状
        cutoff = qe.parse_ts(as_of)
        if cutoff >= top[0]:
            continue  # as_of 不早于最新观测：silver 快路径
        cutoffs[(sid, as_of)] = cutoff
        pending_by_sid.setdefault(sid, []).append((as_of, cutoff))
    visible: dict[tuple[str, str], dict[str, object]] = {}
    seen: set[str] = set()
    if pending_by_sid:
        visible, seen = _scan_visible_envelopes(store, pending_by_sid)
    by_pair: dict[tuple[str, str], list[qe.SnapshotRow] | None] = {}
    for sid, as_of in pairs:
        if (sid, as_of) not in cutoffs:
            by_pair[(sid, as_of)] = None  # 不适用：silver 快路径/无行/探针降级
            continue
        if sid not in seen:
            by_pair[(sid, as_of)] = None  # bronze 无信封（版本漂移等）：不劣于修复前
            continue
        env = visible.get((sid, as_of))
        if env is None:
            by_pair[(sid, as_of)] = []  # as_of 早于首抓：空真
            continue
        top = probe[sid]
        kickoff_ms = int(top[1].timestamp() * 1000) if top[1] is not None else None
        scratch = srct_odds.SilverOddsReport(built_at=utc_now_iso())
        rows = srct_odds.kept_1x2_event_rows(env, kickoff_ms, scratch)
        by_pair[(sid, as_of)] = qe.pseudo_rows_capped(
            duck_con, _books_from_event_rows(rows, None)
        )
    return [by_pair[pair] for pair in requests]


def batch_asof_resolver(
    store: CorpusStore, duck_con: DuckCon
) -> Callable[[list[tuple[str, str]]], list[list[qe.SnapshotRow] | None]]:
    """
    请求域 memo 批解析器（票 05）：同 (sid, as_of) 只解析一次。

    m3 报告内部多次样本构建共享一实例——已解析对免扫，新 sid 集增量走
    batch_asof_rows。shortcut: 生命周期=单次报告生成（API 请求/CLI 一次
    调用）；跨请求进程级缓存不做（bronze 锚失效已有 _ENVELOPES_CACHE
    先例，前瞻面有延迟读数需求时再挂）。
    """
    cache: dict[tuple[str, str], list[qe.SnapshotRow] | None] = {}

    def resolve(requests: list[tuple[str, str]]) -> list[list[qe.SnapshotRow] | None]:
        pending = [p for p in dict.fromkeys(requests) if p not in cache]
        if pending:
            for pair, rows in zip(
                pending, batch_asof_rows(store, duck_con, pending), strict=True
            ):
                cache[pair] = rows
        return [cache[p] for p in requests]

    return resolve
