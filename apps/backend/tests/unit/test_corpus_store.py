"""CorpusStore 写入语义测试：跨进程 append 互斥（bronze 撕裂回归）。"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

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
