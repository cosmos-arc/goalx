"""票 04 corpus 历史时点可见性：as-of 读数走 bronze 可见信封重放。

silver 是最新视图（每 sid 只留最新信封，行 observed_at=该信封
fetched_at），再物化整体后移会让历史时点 as-of 查询"消失"（报告一
案例 6：06:00 同载荷再物化后查 05:31 从 6 行变 0 行）。修复后历史
时点经 ingest.srct_asof 从 bronze 选 fetched_at<=as_of 可见信封重放
（与物化同一转换），rows= 注入缝进共识；最新视图 silver 行为不变。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from goalx_backend.data import corpus_duckdb
from goalx_backend.data import quote_evidence as qe
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_asof, srct_odds
from goalx_backend.db import connect, migrate

SID = "95001"
KICKOFF = datetime(2026, 11, 5, 19, 0, tzinfo=UTC)
FETCHED_1 = "2026-11-04T05:30:00+00:00"
FETCHED_2 = "2026-11-04T06:00:00+00:00"
AS_OF_531 = "2026-11-04T05:31:00+00:00"
AS_OF_605 = "2026-11-04T06:05:00+00:00"
# 两书三向；published 北京墙钟 12:00 = 04:00 UTC（早于两次抓取）
_ROW_A = "2.10|3.30|3.40|11-04 12:00|0.9|0.9|0.9|2026"
_ROW_A2 = "2.02|3.40|3.55|11-04 13:50|0.9|0.9|0.9|2026"  # 05:50 UTC 变价
_ROW_B = "3.50|3.60|1.90|11-04 11:30|0.9|0.9|0.9|2026"


def _envelope(
    fetched_at: str, book_a_rows: list[str] | None = None, *, sid: str = SID
) -> dict:
    """一个 odds_1x2d bronze 信封（书 A 轨迹可多行=变价历史）。"""
    a_rows = ";".join(book_a_rows or [_ROW_A])
    return {
        "provider": srct.SRCT_PROVIDER,
        "dataset": srct.ODDS_DATASET,
        "sid": sid,
        "fetched_at": fetched_at,
        "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
        "raw_sha": "0" * 64,
        "payload": {
            "meta": {},
            "game": [
                "9501|800001|TestSharpA|2.10|3.30|3.40|",
                "9502|800002|TestSharpB|3.50|3.60|1.90|",
            ],
            "game_detail": [
                f"800001^{a_rows};",
                f"800002^{_ROW_B};",
            ],
        },
    }


def _build(
    tmp_path: Path, envelopes: list[dict], *, sids: tuple[str, ...] = (SID,)
) -> tuple[CorpusStore, str]:
    """攒 bronze → silver → duck 库；返回 (store, duckdb 路径)。"""
    store = CorpusStore(tmp_path / "corpus")
    store.append_bronze(srct.SRCT_PROVIDER, srct.ODDS_DATASET, envelopes)
    srct_odds.build_odds_change_events(
        store,
        meta={s: {"sid": s, "kickoff": KICKOFF, "league": "英超"} for s in sids},
    )
    path = corpus_duckdb.build_corpus_duckdb(store)
    return store, str(path)


def _rows(duck_path: str, as_of: str, store: CorpusStore) -> list:
    """price_edge 同款读法：青铜重放先行，不适用回退 silver 读桥。"""
    con = duckdb.connect(duck_path, read_only=True)
    try:
        replayed = srct_asof.asof_rows(store, con, SID, as_of)
        if replayed is not None:
            return replayed
        return qe.srct_snapshot_rows_asof(con, SID, as_of)
    finally:
        con.close()


def test_same_payload_remat_keeps_historical_read(tmp_path: Path) -> None:
    """报告一案例 6：同载荷再抓+再物化后，查 05:31 仍 6 行且逐值一致。"""
    # 第一次：只有 05:30 信封——最新视图即可回答（青铜路径不触发）
    store, duck_path = _build(tmp_path, [_envelope(FETCHED_1)])
    try:
        con = duckdb.connect(duck_path, read_only=True)
        assert srct_asof.asof_rows(store, con, SID, AS_OF_531) is None
        con.close()
        before = _rows(duck_path, AS_OF_531, store)
        assert len(before) == 6
        assert {r["source"] for r in before} == {"srct:1x2:9501", "srct:1x2:9502"}
    finally:
        store.close()

    # 第二次：06:00 同载荷再抓、追加 bronze、再物化
    store2, duck_path2 = _build(tmp_path, [_envelope(FETCHED_1), _envelope(FETCHED_2)])
    try:
        after = _rows(duck_path2, AS_OF_531, store2)
        assert len(after) == 6  # 修复前：0 行（最新视图整体后移）
        assert after == before  # 与再物化前 silver 读数逐值一致（含时刻）
        h = [
            r
            for r in after
            if r["source"].endswith("9501") and r["selection_code"] == "h"
        ]
        assert h[0]["odds"] == 2.10
        # 证据身份可回查：伪行 observed_at=可见信封 fetched_at
        assert h[0]["observed_at"] == FETCHED_1
        # as_of ≥ 最新观测：silver 快路径（最新视图行为不变）
        latest = _rows(duck_path2, AS_OF_605, store2)
        assert len(latest) == 6
    finally:
        store2.close()


def test_value_change_replays_pre_change_world(tmp_path: Path) -> None:
    """重放语义：06:00 信封里书 A 已变价（05:50 发布 2.02）——查 05:31
    仍读 05:30 信封的 2.10；查 06:05 走 silver 读到 2.02。"""
    store, duck_path = _build(
        tmp_path,
        [_envelope(FETCHED_1), _envelope(FETCHED_2, book_a_rows=[_ROW_A, _ROW_A2])],
    )
    try:
        old = _rows(duck_path, AS_OF_531, store)
        h_old = [
            r
            for r in old
            if r["source"].endswith("9501") and r["selection_code"] == "h"
        ]
        assert h_old[0]["odds"] == 2.10  # 当时可见=变价前世界
        assert h_old[0]["observed_at"] == FETCHED_1

        new = _rows(duck_path, AS_OF_605, store)
        h_new = [
            r
            for r in new
            if r["source"].endswith("9501") and r["selection_code"] == "h"
        ]
        assert h_new[0]["odds"] == 2.02
    finally:
        store.close()


def test_eu_consensus_asof_corpus_branch_pinned(tmp_path: Path) -> None:
    """验收 3：eu_consensus_asof 的 corpus 分支 as-of 语义钉死——历史时点
    经 rows= 注入读可见信封共识（变价前），当前时点读最新（变价后）。"""
    store, duck_path = _build(
        tmp_path,
        [_envelope(FETCHED_1), _envelope(FETCHED_2, book_a_rows=[_ROW_A, _ROW_A2])],
    )
    conn = connect(":memory:")
    migrate(conn)

    def consensus(as_of: str) -> dict[str, float] | None:
        con = duckdb.connect(duck_path, read_only=True)
        try:
            replayed = srct_asof.asof_rows(store, con, SID, as_of)
            return qe.eu_consensus_asof(
                conn, 1, as_of, duck_con=con, sid=SID, rows=replayed
            )
        finally:
            con.close()

    try:
        old = consensus(AS_OF_531)
        new = consensus(AS_OF_605)
        assert old is not None
        assert new is not None
        assert set(old) == {"h", "d", "a"}
        assert old != new  # 变价前后两个世界
        assert old["h"] < new["h"]  # 2.10 → 2.02 赔率降=共识隐含概率升
    finally:
        conn.close()
        store.close()


def test_asof_before_first_fetch_returns_empty_truth(tmp_path: Path) -> None:
    """as_of 早于首抓：当时本机零可见——空真（不冒充、不回退最新视图）。"""
    store, duck_path = _build(tmp_path, [_envelope(FETCHED_1), _envelope(FETCHED_2)])
    try:
        con = duckdb.connect(duck_path, read_only=True)
        assert srct_asof.asof_rows(store, con, SID, "2026-11-04T05:00:00+00:00") == []
        con.close()
        rows = _rows(duck_path, "2026-11-04T05:00:00+00:00", store)
        assert rows == []
    finally:
        store.close()


def test_malformed_fetched_at_degrades_not_raises(tmp_path: Path) -> None:
    """票 04 correctness F1：坏 fetched_at 信封跳过降级，不炸读数。"""
    envelopes = [
        _envelope(FETCHED_1),
        _envelope("bogus-timestamp"),  # 中途坏信封（物化侧 fetched_ms→None）
        _envelope(FETCHED_2),
    ]
    store, duck_path = _build(tmp_path, envelopes)
    try:
        rows = _rows(duck_path, AS_OF_531, store)
        assert len(rows) == 6  # 坏信封跳过，05:30 可见信封照读
        assert (
            srct_asof._visible_envelope(
                [{"fetched_at": "bogus"}, {"fetched_at": FETCHED_1}],
                qe.parse_ts(AS_OF_531),
                False,
            )
            is not None
        )
    finally:
        store.close()


def test_same_root_rebuild_invalidates_envelope_cache(tmp_path: Path) -> None:
    """票 04 correctness F2：同根重建（同 fetched 时间、不同价）后，
    缓存按 bronze 文件锚失效——历史读数跟随新语料，不吐旧价。"""
    import shutil

    root = tmp_path / "corpus"
    store1, duck1 = _build(tmp_path, [_envelope(FETCHED_1), _envelope(FETCHED_2)])
    try:
        first = _rows(duck1, AS_OF_531, store1)
        h1 = [
            r
            for r in first
            if r["source"].endswith("9501") and r["selection_code"] == "h"
        ]
        assert h1[0]["odds"] == 2.10
    finally:
        store1.close()

    shutil.rmtree(root)  # 同根整体重建：同 fetched 时刻、书 A 价改 1.90
    cheap = "1.90|3.60|3.80|11-04 12:00|0.9|0.9|0.9|2026"
    store2, duck2 = _build(
        tmp_path,
        [
            _envelope(FETCHED_1, book_a_rows=[cheap]),
            _envelope(FETCHED_2, book_a_rows=[cheap]),
        ],
    )
    try:
        second = _rows(duck2, AS_OF_531, store2)
        h2 = [
            r
            for r in second
            if r["source"].endswith("9501") and r["selection_code"] == "h"
        ]
        assert h2[0]["odds"] == 1.90  # 修复前：旧缓存吐 2.10
    finally:
        store2.close()


def test_batch_asof_rows_matches_single(tmp_path: Path) -> None:
    """票 05 批扫等价：多 sid 多时点请求与逐条 asof_rows 逐值一致
    （None 快路径 / [] 空真 / 重放行三种形态全覆盖）。"""
    sid2 = "95002"
    envelopes = [
        _envelope(FETCHED_1),
        _envelope(FETCHED_2, book_a_rows=[_ROW_A, _ROW_A2]),
        _envelope(FETCHED_1, sid=sid2),
    ]
    store, duck_path = _build(tmp_path, envelopes, sids=(SID, sid2))
    con = duckdb.connect(duck_path, read_only=True)
    try:
        requests = [
            (SID, AS_OF_531),  # 重放（silver 最新=06:00 > 05:31）
            (SID, AS_OF_605),  # silver 快路径 → None
            (SID, "2026-11-04T05:00:00+00:00"),  # 早于首抓 → []
            (sid2, AS_OF_531),  # 第二 sid 重放
            ("95009", AS_OF_531),  # silver 无行 → None
            (SID, AS_OF_531),  # 重复请求（去重）
        ]
        batch = srct_asof.batch_asof_rows(store, con, requests)
        single = [srct_asof.asof_rows(store, con, s, a) for s, a in requests]
        assert batch == single
        assert len(batch) == len(requests)
        assert batch[0] is not None  # 重放行非空
        assert len(batch[0]) == 6
        assert batch[1] is None
        assert batch[2] == []
        # 空请求不炸
        assert srct_asof.batch_asof_rows(store, con, []) == []
    finally:
        con.close()
        store.close()


def test_batch_asof_resolver_memos_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """票 05 memo 解析器：已解析 (sid, as_of) 免再扫，新对增量批查。"""
    store, duck_path = _build(tmp_path, [_envelope(FETCHED_1), _envelope(FETCHED_2)])
    con = duckdb.connect(duck_path, read_only=True)
    real_batch = srct_asof.batch_asof_rows
    calls: list[list[tuple[str, str]]] = []

    def counting(
        s: CorpusStore, c: duckdb.DuckDBPyConnection, requests: list[tuple[str, str]]
    ) -> list[list[qe.SnapshotRow] | None]:
        calls.append(list(requests))
        return real_batch(s, c, requests)

    monkeypatch.setattr(srct_asof, "batch_asof_rows", counting)
    resolve = srct_asof.batch_asof_resolver(store, con)
    try:
        first = resolve([(SID, AS_OF_531)])
        second = resolve([(SID, AS_OF_531), (SID, AS_OF_605)])
        assert first[0] is second[0]  # memo：同对象复用，不再扫
        assert calls == [[(SID, AS_OF_531)], [(SID, AS_OF_605)]]  # 只查新对
        assert srct_asof.batch_asof_rows is counting  # sanity：断言替换生效
    finally:
        con.close()
        store.close()
