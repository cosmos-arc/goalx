"""CorpusStore 写入语义测试：跨进程 append 互斥（bronze 撕裂回归）。"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
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
