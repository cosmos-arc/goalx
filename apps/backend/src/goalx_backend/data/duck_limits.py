"""
duckdb 连接资源帽（gold-rebuild 内存收口，票 04 / 票 03 裁决 P3）。

建库（写）/消费（读）/silver 选轨（market 段）连接同口径：memory_limit +
语料树内 spill 目录 + threads 帽。threads 帽是瞬时超限的主杠杆——
duckdb 每线程 morsel 缓冲随核数放大且不全计入 limit 账面（2026-11-06
晨间 swap 30+GB 实录，定拍点连续三日增开 swapfile）；官方口径：限内存
先限线程（duckdb "Memory Management" 博客）。
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

# 上限 + 树内 spill=超出部分落盘换时间（2026-11-05 收口口径，票 03 裁决不动）
DUCKDB_MEMORY_LIMIT = "3GB"
# 并行线程帽（票 03：量级 min(4, ncpu)，实现自选）
DUCKDB_THREADS_CAP = 4


def duckdb_threads() -> int:
    """Threads 设置值（物理核更少时随核数，单核环境至少 1）。"""
    return max(1, min(DUCKDB_THREADS_CAP, os.cpu_count() or 1))


def apply_resource_limits(con: duckdb.DuckDBPyConnection, spill_dir: Path) -> None:
    """应用资源帽；spill 目录缺席即建（消费侧连接时树内 tmp 可能缺席）。"""
    spill_dir.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET threads={duckdb_threads()}")
    con.execute(f"SET temp_directory='{spill_dir.as_posix()}'")
