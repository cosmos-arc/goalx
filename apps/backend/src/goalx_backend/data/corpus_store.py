"""
CorpusStore 语料树（ADR-0011 决策 1/2）：raw 压缩件+sha256 与独立 checkpoint。

repo 外独立数据资产树（默认 ~/goalx-data/，路径经 config 覆盖），与运行面
SQLite 互不相扰：爬虫状态不进运行面 runs 表族，运行面也永不写语料树。
目录树 = raw/ + bronze/ + silver/ + gold/（feature 预留，schema 归模型图）+
duckdb/ + checkpoint.db（本模块自持表 SQL，ADR-0008）。

raw 落盘约定：每响应 gzip 原样字节 + 内容 sha256，路径
``raw/{provider}/{dataset}/{key}{ext}.gz``；checkpoint 行记 sha/path/bytes，
(provider, dataset, key) 唯一——断点续传与零重抓都查这一张表。写入次序
先文件后 checkpoint 行：中断落在这两步之间时重跑会重抓该条并幂等覆盖，
同内容重写无害。

bronze 落盘约定（切片 12）：每数据集一文件
``bronze/{provider}/{dataset}.ndjson.gz``，NDJSON(gzip) append-only，行 =
信封（provider/dataset/sid/fetched_at/parser_version/raw_sha）+ payload；
解析失败不写行（raw 已 100% 留档，缺口由采集统计量化）。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from goalx_backend.db import utc_now_iso

# checkpoint 表：本模块自持（语料树独立 SQLite，不进运行面 migrations）
_RAW_ARTIFACTS_SQL = """
CREATE TABLE IF NOT EXISTS raw_artifacts (
    provider TEXT NOT NULL,
    dataset TEXT NOT NULL,
    key TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (provider, dataset, key)
)
"""
_TREE_SUBDIRS = ("raw", "bronze", "silver", "gold", "duckdb")


@dataclass(frozen=True)
class RawRef:
    """一条 raw 工件的定位（sha 对 gunzip 后的原始响应字节）。"""

    sha256: str
    path: Path
    byte_size: int


class CorpusStore:
    """语料树句柄：目录树 + raw 落盘 + checkpoint。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._conn: sqlite3.Connection | None = None

    @property
    def checkpoint_path(self) -> Path:
        """Checkpoint SQLite 路径（树根独立文件）。"""
        return self.root / "checkpoint.db"

    def ensure_tree(self) -> None:
        """建目录树与 checkpoint 表（幂等）。"""
        for sub in _TREE_SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._checkpoint().execute(_RAW_ARTIFACTS_SQL)
        self._checkpoint().commit()

    def _checkpoint(self) -> sqlite3.Connection:
        # ponytail: 进程内单连接串行用；夜班单进程采集足够，多进程并发再上锁
        if self._conn is None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.checkpoint_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(_RAW_ARTIFACTS_SQL)
            self._conn.commit()
        return self._conn

    def has(self, provider: str, dataset: str, key: str) -> bool:
        """该 (provider, dataset, key) 是否已抓完（断点续传查询）。"""
        row = (
            self._checkpoint()
            .execute(
                "SELECT 1 FROM raw_artifacts WHERE provider=? AND dataset=? AND key=?",
                (provider, dataset, key),
            )
            .fetchone()
        )
        return row is not None

    def raw_path(self, provider: str, dataset: str, key: str, *, ext: str = "") -> Path:
        """Raw 工件约定路径（不保证已存在）。"""
        return self.root / "raw" / provider / dataset / f"{key}{ext}.gz"

    def ingest_raw(
        self, provider: str, dataset: str, key: str, body: bytes, *, ext: str = ""
    ) -> RawRef:
        """gzip+sha256 落盘并记 checkpoint（幂等：重写同内容无害）。"""
        sha = hashlib.sha256(body).hexdigest()
        path = self.raw_path(provider, dataset, key, ext=ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        with (
            tmp.open("wb") as raw_fh,
            gzip.GzipFile(fileobj=raw_fh, mode="wb", mtime=0) as fh,
        ):
            fh.write(body)
        tmp.replace(path)
        conn = self._checkpoint()
        conn.execute(
            """
            INSERT OR IGNORE INTO raw_artifacts
                (provider, dataset, key, sha256, path, byte_size, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (provider, dataset, key, sha, str(path), len(body), utc_now_iso()),
        )
        conn.commit()
        return RawRef(sha256=sha, path=path, byte_size=len(body))

    def read_raw(
        self, provider: str, dataset: str, key: str, *, ext: str = ""
    ) -> bytes:
        """读回 raw 原始字节（断点续传时本地重解析，零网络）。"""
        path = self.raw_path(provider, dataset, key, ext=ext)
        return gzip.decompress(path.read_bytes())

    def raw_sha(self, provider: str, dataset: str, key: str) -> str | None:
        """Checkpoint 中该工件的 sha256（无记录 None；信封回溯对账用）。"""
        row = (
            self._checkpoint()
            .execute(
                """
            SELECT sha256 FROM raw_artifacts
            WHERE provider=? AND dataset=? AND key=?
            """,
                (provider, dataset, key),
            )
            .fetchone()
        )
        return None if row is None else str(row["sha256"])

    def verify_raw(
        self, provider: str, dataset: str, key: str, *, ext: str = ""
    ) -> bool:
        """Gunzip 后 sha 与 checkpoint 对账（冒烟验收用）。"""
        row = (
            self._checkpoint()
            .execute(
                """
            SELECT sha256 FROM raw_artifacts
            WHERE provider=? AND dataset=? AND key=?
            """,
                (provider, dataset, key),
            )
            .fetchone()
        )
        if row is None:
            return False
        body = self.read_raw(provider, dataset, key, ext=ext)
        return hashlib.sha256(body).hexdigest() == row["sha256"]

    def bronze_path(self, provider: str, dataset: str) -> Path:
        """Bronze 工件约定路径（每数据集一文件，NDJSON(gzip) append-only）。"""
        return self.root / "bronze" / provider / f"{dataset}.ndjson.gz"

    def append_bronze(
        self, provider: str, dataset: str, rows: list[dict[str, object]]
    ) -> int:
        """
        Bronze NDJSON(gzip) 追加一批信封行；返回写入行数。

        append-only：不查重不改写（重跑由 checkpoint 上游拦截；解析器升版
        重物化时按 parser_version 取最新行，归 silver 层）。gzip 追加 =
        新起 member，读取侧透明拼接。
        """
        if not rows:
            return 0
        path = self.bronze_path(provider, dataset)
        path.parent.mkdir(parents=True, exist_ok=True)
        with (
            path.open("ab") as raw_fh,
            gzip.GzipFile(fileobj=raw_fh, mode="ab", mtime=0) as fh,
        ):
            for row in rows:
                fh.write((json.dumps(row, ensure_ascii=False) + "\n").encode())
        return len(rows)

    def read_bronze(self, provider: str, dataset: str) -> list[dict[str, object]]:
        """读回全部 bronze 行（测试/对账用；文件不存在返回空）。"""
        path = self.bronze_path(provider, dataset)
        if not path.exists():
            return []
        lines = gzip.decompress(path.read_bytes()).decode("utf-8").splitlines()
        return [json.loads(line) for line in lines if line]

    def close(self) -> None:
        """关 checkpoint 连接（树文件保留）。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
