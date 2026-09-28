"""
Silver builder 内核（deepen-20260928 票 05）：语料 silver 层的共同实现面。

各 builder（srct_silver / srct_odds / srct_market / jc_silver / elo_silver /
archive_538）的共同件一处住所：分区与整文件 parquet 原子写、幂等重建清理、
事件流去重核 keep_value_changes（A→B→A 保留/同值心跳丢弃/不可解时间跳行
——jc sp_change_event 与 srct odds_change_event 两表一份语义）、bronze
选取迭代（账本/载荷遍）、数值与赛季工具、数据集 meta。

磁盘布局唯一落点是 CorpusStore.silver_path()/raw_dir()（本模块只做写与
选，不拼路径）。bronze 选取的 provider/版本由调用方注入——本模块在
data 层，不 import data.ingest（层序见 pyproject importlinter）。

pyarrow 无官方 stub（srct_silver/jc_silver/elo_silver 同先例）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Protocol

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
import pyarrow as pa
import pyarrow.parquet as pq

from goalx_backend.data.corpus_store import CorpusStore

# 不可解时间的排序垫底值（随后按 bad_time 跳行，序不影响结果）
UNSORTABLE_MS = 1 << 62

# 赛季 8 月界（srct_silver/elo_silver 同界）
SEASON_START_MONTH = 8


class KeepCounters(Protocol):
    """keep_value_changes 的记账面（bad_time_rows/heartbeat_dropped 两计数）。"""

    bad_time_rows: int
    heartbeat_dropped: int


def keep_value_changes[R](
    rows: list[R],
    report: KeepCounters,
    *,
    time_of: Callable[[R], int | None],
    order_of: Callable[[R], int],
    value_of: Callable[[R], tuple[object, ...]],
    account_row: Callable[[R], None] | None = None,
) -> list[R]:
    """
    事件流去重核（两表一份语义）：值组与上一保留行全等的行丢弃。

    按 (published_ms, source_order) 升序（不可解时间垫底），A→B→A 保留、
    首条自然保留、末条同值心跳丢弃；不可解时间行不落事件（bad_time 记账）。
    account_row 对每个可解时间行调用（含随后被心跳丢弃的行——行级坏值
    口径由调用方注入；jc 形态无此面则缺省不调）。
    """

    def _sort_key(row: R) -> tuple[int, int]:
        published = time_of(row)
        return (
            published if published is not None else UNSORTABLE_MS,
            order_of(row),
        )

    rows.sort(key=_sort_key)
    kept: list[R] = []
    last_values: tuple[object, ...] | None = None
    for row in rows:
        if time_of(row) is None:
            report.bad_time_rows += 1
            continue
        if account_row is not None:
            account_row(row)
        values = value_of(row)
        if values == last_values:
            report.heartbeat_dropped += 1
            continue
        last_values = values
        kept.append(row)
    return kept


def season_of(kickoff: datetime) -> str:
    """开球 → 赛季键（8 月界切，目录名安全形 "2025-26"）。"""
    year = kickoff.year if kickoff.month >= SEASON_START_MONTH else kickoff.year - 1
    return f"{year}-{(year + 1) % 100:02d}"


def to_float(value: object) -> float | None:
    """数值化容错（贴源串/数值均可；失败 None）。"""
    if value is None:
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def write_partition(
    part: Path, rows: list[dict[str, object]], schema: pa.Schema
) -> None:
    """一分区一 parquet 文件（tmp 原子替换；排序已在上游统一完成）。"""
    part.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    tmp = part / "data.parquet.tmp"
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(part / "data.parquet")


def write_dataset_file(
    root: Path, rows: list[dict[str, object]], schema: pa.Schema
) -> None:
    """整数据集一 parquet（tmp 原子替换；单文件数据集共用舞步）。"""
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "data.parquet.tmp"
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), tmp, compression="zstd")
    tmp.replace(root / "data.parquet")


def remove_stale(root: Path, target: set[Path]) -> int:
    """删除不在目标集里的旧分区（重建幂等的清理半边；silver 各数据集共用）。"""
    removed = 0
    if not root.exists():
        return removed
    for season_dir in sorted(root.iterdir()):
        if not season_dir.is_dir():
            continue  # _meta.json 等文件不动
        for part in sorted(season_dir.iterdir()):
            if part.is_dir() and part not in target:
                for file in part.iterdir():
                    file.unlink()
                part.rmdir()
                removed += 1
        if season_dir.is_dir() and not any(season_dir.iterdir()):
            season_dir.rmdir()
    return removed


def write_dataset_meta(root: Path, meta: dict[str, object]) -> None:
    """数据集 _meta.json（空语料也要落——建库即审计；silver 各数据集共用）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def latest_bronze_rows(
    store: CorpusStore, provider: str, dataset: str, version: str
) -> dict[str, dict[str, object]]:
    """
    指定解析器版本的 bronze 行，key→最新（append-only 后行胜出）。

    silver builder 共用的选取口径：重复抓取选一份轨迹、旧 parser_version
    行一律不可见。**全量物化**——仅限有界数据集（15.5K 行级）；odds/
    handicap 大数据集走 latest_bronze_ledger + 流式遍。
    """
    latest: dict[str, dict[str, object]] = {}
    for row in store.read_bronze(provider, dataset):
        if row.get("parser_version") != version:
            continue
        latest[str(row["sid"])] = row
    return latest


def latest_bronze_ledger(
    store: CorpusStore, provider: str, dataset: str, version: str
) -> dict[str, int]:
    """
    选轨信封账本：sid → 选中行号。

    选取口径与 latest_bronze_rows 全同（指定 parser_version、后行胜出）
    但零物化——流式过 bronze 只留每 sid 一个行号，载荷逐行即弃；内存=
    每 sid 一条 int，与语料总量无关。载荷遍按行号精确命中选中行。
    """
    ledger: dict[str, int] = {}
    for lineno, line in enumerate(store.iter_bronze_lines(provider, dataset)):
        row = json.loads(line)
        if row.get("parser_version") == version and row.get("sid") is not None:
            ledger[str(row["sid"])] = lineno
    return ledger


def iter_selected_rows(
    store: CorpusStore, provider: str, dataset: str, ledger: dict[str, int]
) -> Iterator[tuple[str, dict[str, object]]]:
    """
    信封账本的载荷遍：yield (sid, 选中 bronze 行)。

    命中行号才 json.loads、单行即弃（内存=单行）。需原文透传的 spill 遍
    不走这里（零解析写回保字节），见 srct_odds._spill_selected。
    """
    want = {lineno: sid for sid, lineno in ledger.items()}
    for lineno, line in enumerate(store.iter_bronze_lines(provider, dataset)):
        sid = want.get(lineno)
        if sid is not None:
            yield sid, json.loads(line)
