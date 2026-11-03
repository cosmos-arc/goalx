"""CorpusStore 写入语义测试：跨进程 append 互斥（bronze 撕裂回归）。"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goalx_backend.data.corpus_store import CorpusStore

# 两写者并发 append 同一 bronze 文件——2026-10-03 前无锁会 gzip member
# 交错撕裂（真树四数据集尾部 BAD 实测）。flock 修复的回归锁。
_WORKER = """
import sys
from pathlib import Path
from goalx_backend.data.corpus_store import CorpusStore
store = CorpusStore(Path(sys.argv[1]))
for i in range(30):
    store.append_bronze("p", "d", [
        {"sid": f"{sys.argv[2]}-{i}-{j}", "dataset": "d", "provider": "p",
         "fetched_at": "2026-10-03T00:00:00+00:00", "parser_version": "v1",
         "raw_sha": "x", "payload": {}}
        for j in range(10)
    ])
store.close()
"""

# 两写者并发覆写同一 raw 键（boundary-hardening 票 03）：文件序与台账序
# 无互斥时会交叉——盘上 B 体、台账 A sha，verify_raw 误报腐化
_WORKER_RAW = """
import sys
from pathlib import Path
from goalx_backend.data.corpus_store import CorpusStore
store = CorpusStore(Path(sys.argv[1]))
for i in range(150):
    store.ingest_raw("p", "d", "same-key", f"{sys.argv[2]}-{i}".encode())
store.close()
"""


def test_append_bronze_concurrent_no_tear(tmp_path: Path) -> None:
    procs = [
        subprocess.Popen(  # noqa: S603 固定内联脚本+测试解释器
            [sys.executable, "-c", _WORKER, str(tmp_path), tag],
        )
        for tag in ("a", "b")
    ]
    for p in procs:
        assert p.wait() == 0

    bronze_file = tmp_path / "bronze" / "p" / "d.ndjson.gz"
    with gzip.open(bronze_file, "rt", encoding="utf-8") as h:
        lines = [line for line in h if line.strip()]
    assert len(lines) == 600  # 2 写者 × 30 批 × 10 行，零撕裂零丢失
    sids = {json.loads(line)["sid"] for line in lines}
    assert len(sids) == 600


def test_bronze_sid_cache_anchor_recheck_on_external_replace(tmp_path: Path) -> None:
    """外部替换 bronze 文件：缓存锚复核即弃缓存重扫（跨进程掩盖根治）。"""
    store = CorpusStore(tmp_path)
    try:
        store.append_bronze(
            "p",
            "d",
            [
                {
                    "sid": "old",
                    "dataset": "d",
                    "provider": "p",
                    "fetched_at": "2026-10-07T00:00:00+00:00",
                    "parser_version": "v1",
                    "raw_sha": "x",
                    "payload": {},
                }
            ],
        )
        assert store.bronze_sids("p", "d", "v1") == {"old"}  # 缓存就位
        # 外部重建：整文件替换不走 append——命中路径复核文件锚，即时弃缓存
        body = "\n".join(
            json.dumps(
                {
                    "sid": sid,
                    "dataset": "d",
                    "provider": "p",
                    "fetched_at": "2026-10-07T00:00:00+00:00",
                    "parser_version": "v2",
                    "raw_sha": "x",
                    "payload": {},
                }
            )
            for sid in ("new-a", "new-b")
        )
        store.bronze_path("p", "d").write_bytes(gzip.compress(body.encode()))
        assert store.bronze_sids("p", "d", "v1") == set()  # 不再被旧缓存掩盖
        assert store.bronze_sids("p", "d", "v2") == {"new-a", "new-b"}
        # reset 仍可显式强制（重建脚本声明式失效，不依赖缓存状态）
        store.reset_bronze_index("p", "d")
        assert store.bronze_sids("p", "d", "v2") == {"new-a", "new-b"}
        assert store.bronze_sids("p", "d", "v1") == set()
    finally:
        store.close()


def test_ingest_raw_overwrite_updates_ledger(tmp_path: Path) -> None:
    """correctness F1（2026-11-02）：每日槽键（@gap-*）以不同体覆写——
    台账行必须跟盘上最新体（sha/fetched_at），否则 verify_raw 误报腐化、
    取证时间错档；首写键行为与 INSERT OR IGNORE 时代逐位一致。"""
    import hashlib

    store = CorpusStore(tmp_path / "corpus")
    try:
        store.ingest_raw("srct", "bfdata", "@gap-20261102", b"body-one")
        store.ingest_raw("srct", "bfdata", "@gap-20261102", b"body-two")
        assert store.read_raw("srct", "bfdata", "@gap-20261102") == b"body-two"
        assert (
            store.raw_sha("srct", "bfdata", "@gap-20261102")
            == hashlib.sha256(b"body-two").hexdigest()
        )
        assert store.verify_raw("srct", "bfdata", "@gap-20261102")
    finally:
        store.close()


# ---- remat 锁原子性（boundary-hardening 票 02，P1-2） ----


def _contend(
    tag: str,
    store: CorpusStore,
    token: str,
    barrier: threading.Barrier,
    acquired: threading.Event,
    release: threading.Event,
    results: dict[str, object | None],
) -> None:
    """一个竞争者：barrier 同拍获锁，持锁至主线程放行（连接线程亲和：
    acquire/release/close 全在本线程）。"""
    barrier.wait()
    results[tag] = store.try_acquire_remat_lock(
        stale_after_seconds=3600.0, run_token=token
    )
    acquired.set()
    release.wait()
    store.release_remat_lock(token)
    store.close()


def test_remat_lock_concurrent_acquire_single_winner(tmp_path: Path) -> None:
    """票 02 验收①：两线程各持独立连接 barrier 同拍 acquire——恰一个
    成功，另一个拿到持锁行（含胜者 token）。连跑多轮钉死检查+写入
    原子性（SELECT/UPSERT 分离的旧实现下两连接可双双获锁）。"""
    root = tmp_path / "corpus"
    seed = CorpusStore(root)
    seed.ensure_tree()
    seed.close()
    for round_no in range(25):
        stores = {tag: CorpusStore(root) for tag in ("a", "b")}
        tokens = {tag: f"r{round_no}-{tag}" for tag in ("a", "b")}
        barrier = threading.Barrier(2)
        acquired = {tag: threading.Event() for tag in ("a", "b")}
        release = threading.Event()
        results: dict[str, object | None] = {}
        threads = [
            threading.Thread(
                target=_contend,
                args=(
                    tag,
                    stores[tag],
                    tokens[tag],
                    barrier,
                    acquired[tag],
                    release,
                    results,
                ),
            )
            for tag in ("a", "b")
        ]
        for t in threads:
            t.start()
        for evt in acquired.values():
            assert evt.wait(timeout=10)
        winners = [tag for tag in ("a", "b") if results[tag] is None]
        assert len(winners) == 1, f"第 {round_no} 轮获锁胜者数 {len(winners)}≠1"
        winner = winners[0]
        loser_tag = "b" if winner == "a" else "a"
        holder = results[loser_tag]
        assert holder is not None  # 败者拿到持锁行而非异常
        assert holder["run_token"] == tokens[winner]  # 指向胜者那轮
        assert holder["holder_pid"] > 0
        release.set()  # 胜者持锁已罩住败者尝试，现在放行释放
        for t in threads:
            t.join()


def test_remat_lock_stale_takeover_late_release_harmless(tmp_path: Path) -> None:
    """票 02 验收②：过期夺取后，迟到的旧持有者 release（token 不匹配）
    删 0 行——新持有者的锁不受影响；旧 token 再取锁拿到新持有行。"""
    root = tmp_path / "corpus"
    old = CorpusStore(root)
    new = CorpusStore(root)
    try:
        got = old.try_acquire_remat_lock(stale_after_seconds=60.0, run_token="old")
        assert got is None
        # 模拟持锁进程已死（started_at 超窗）→ 新一轮夺取
        stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
        old._checkpoint().execute("UPDATE srct_remat_lock SET started_at = ?", (stale,))
        old._checkpoint().commit()
        got = new.try_acquire_remat_lock(stale_after_seconds=3600.0, run_token="new")
        assert got is None
        old.release_remat_lock("old")  # 迟到的旧持有者 finally 到达
        row = (
            new._checkpoint()
            .execute("SELECT run_token FROM srct_remat_lock WHERE id = 1")
            .fetchone()
        )
        assert row["run_token"] == "new"  # 新锁原封不动
        holder = old.try_acquire_remat_lock(
            stale_after_seconds=3600.0, run_token="other"
        )
        assert holder is not None
        assert holder["run_token"] == "new"
        new.release_remat_lock("new")
        count = (
            new._checkpoint()
            .execute("SELECT COUNT(*) AS n FROM srct_remat_lock")
            .fetchone()
        )
        assert count["n"] == 0
    finally:
        old.close()
        new.close()


def test_remat_lock_legacy_row_without_token_still_occupies(tmp_path: Path) -> None:
    """老库锁行（run_token NULL，ALTER 补列前写入）仍按占用拒新轮。"""
    store = CorpusStore(tmp_path / "corpus")
    try:
        store._checkpoint().execute(
            "INSERT INTO srct_remat_lock (id, holder_pid, started_at)"
            " VALUES (1, 4242, ?)",
            (datetime.now(UTC).isoformat(timespec="seconds"),),
        )
        store._checkpoint().commit()
        holder = store.try_acquire_remat_lock(
            stale_after_seconds=3600.0, run_token="fresh"
        )
        assert holder is not None
        assert holder["holder_pid"] == 4242
    finally:
        store.close()


def test_ingest_raw_concurrent_same_key_ledger_matches_disk(tmp_path: Path) -> None:
    """票 03 验收：双进程并发写同 key 任意交叉——终态盘上 gunzip-sha ==
    台账 sha256（发布段按数据集目录 flock 同序互斥）。定位=soak/跨进程
    机制冒烟：旧实现竞态窗口小、本测试对旧代码也常绿——回归判据以
    test_ingest_raw_publish_crossing_prevented 的确定性判例为准
    （correctness F2）。"""
    import hashlib

    root = tmp_path / "corpus"
    procs = [
        subprocess.Popen(  # noqa: S603 固定内联脚本+测试解释器
            [sys.executable, "-c", _WORKER_RAW, str(root), tag],
        )
        for tag in ("a", "b")
    ]
    for p in procs:
        assert p.wait() == 0
    store = CorpusStore(root)
    try:
        body = store.read_raw("p", "d", "same-key")
        assert hashlib.sha256(body).hexdigest() == store.raw_sha("p", "d", "same-key")
        assert store.verify_raw("p", "d", "same-key")
        # 唯一 .part 后缀：发布完成零中间态残留
        assert list((root / "raw" / "p" / "d").glob("*.part")) == []
    finally:
        store.close()


def test_ingest_raw_failed_publish_cleans_own_tmp(tmp_path: Path) -> None:
    """票 03：发布失败（replace 段异常）自清本轮唯一 .part，不留可积累
    的中间态文件；上轮正式区与台账不受影响。"""
    store = CorpusStore(tmp_path / "corpus")
    try:
        store.ingest_raw("p", "d", "k1", b"good")
        real_replace = Path.replace

        def boom_replace(self: Path, target: str) -> None:
            raise OSError("disk full")

        Path.replace = boom_replace  # type: ignore[method-assign]
        try:
            with pytest.raises(OSError, match="disk full"):
                store.ingest_raw("p", "d", "k2", b"bad")
        finally:
            Path.replace = real_replace  # type: ignore[method-assign]
        raw_dir = tmp_path / "corpus" / "raw" / "p" / "d"
        assert list(raw_dir.glob("*.part")) == []
        assert store.verify_raw("p", "d", "k1")  # 上轮正式区不受失败影响
        assert store.raw_sha("p", "d", "k2") is None  # 台账零记账
    finally:
        store.close()


def test_ingest_raw_publish_crossing_prevented(tmp_path: Path) -> None:
    """票 03 判例复现（确定性编排）：A 落盘→（挂起）→B 落盘+B 记账→
    A 记账——旧实现终态=盘上 B 体、台账 A sha；数据集目录 flock 把
    发布段同序互斥后该交叉不可达，B 等 A 整轮收口再发布。"""
    import hashlib

    root = tmp_path / "corpus"
    real_checkpoint = CorpusStore._checkpoint
    a_past_replace = threading.Event()
    a_may_finish = threading.Event()
    fired = {"a": False}

    def hooked_checkpoint(self: CorpusStore) -> object:
        # 门控按实例不按线程 ident（ident 在 start 后赋值存在抢占窗口，
        # correctness F1）：补丁窗口内只有 store_a/store_b 会调，拦
        # store_a 首次到台账步（= replace 已完成、记账未开始）
        if self is store_a and not fired["a"]:
            fired["a"] = True
            a_past_replace.set()
            assert a_may_finish.wait(timeout=10)
        return real_checkpoint(self)

    CorpusStore._checkpoint = hooked_checkpoint  # type: ignore[method-assign]
    store_a = CorpusStore(root)
    try:

        def run_a() -> None:
            store_a.ingest_raw("p", "d", "k", b"AAA")

        thread_a = threading.Thread(target=run_a)
        thread_a.start()
        assert a_past_replace.wait(timeout=10)  # A 恰在 replace 与记账之间

        store_b = CorpusStore(root)

        def run_b() -> None:
            store_b.ingest_raw("p", "d", "k", b"BBB")

        thread_b = threading.Thread(target=run_b)
        thread_b.start()
        # 给 B 到达 flock 的机会（fix 下阻塞；旧实现下整轮完成）
        time.sleep(0.2)
        b_started_at = datetime.now(UTC).isoformat(timespec="seconds")
        a_may_finish.set()  # 放 A 收口（记账+释放锁）
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)
        assert not thread_a.is_alive()
        assert not thread_b.is_alive()
    finally:
        CorpusStore._checkpoint = real_checkpoint  # type: ignore[method-assign]

    assert fired["a"]  # 钩子必达（否则编排失效变假绿）
    check = CorpusStore(root)  # 主线程新连接做终态断言（线程亲和）
    try:
        body = check.read_raw("p", "d", "k")
        assert hashlib.sha256(body).hexdigest() == check.raw_sha("p", "d", "k")
        assert check.verify_raw("p", "d", "k")
        # 台账时间跟最新体（B）：B 的 stamp 晚于放行时刻（取证不回档）
        row = (
            check._checkpoint()
            .execute(
                "SELECT fetched_at FROM raw_artifacts"
                " WHERE provider='p' AND dataset='d' AND key='k'"
            )
            .fetchone()
        )
        assert str(row["fetched_at"]) >= b_started_at
        # fix 语义：B 在 A 之后整轮发布——终态是 B 的体（后写胜出），
        # 台账跟 B；盘/账不一致的旧交叉形态在此断言下必炸
    finally:
        check.close()
