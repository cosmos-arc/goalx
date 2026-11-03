"""CorpusStore 写入语义测试：跨进程 append 互斥（bronze 撕裂回归）。"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
