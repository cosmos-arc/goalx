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

夜班台账（切片 13）：srct_day_status（日级 done/not_found——done 只由夜班
干净跑完一日报，日页 raw 在而状态缺 = 中断日，次夜仍 pending 续传）+
srct_night_summaries（每夜请求/新增/吸收/失败摘要，晨检一眼健康度）；
季深度判定（票 18）：srct_season_depth（season PK，老季浅深/全深跨夜
持久，夜班直读不重探）。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
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
# 切片 13 夜班台账：日级状态（done=该日 collect 干净跑完；not_found=日页
# 伪 200 留痕防夜夜重试）与每夜摘要（窗口外非跑不落行）
_SRCT_DAY_STATUS_SQL = """
CREATE TABLE IF NOT EXISTS srct_day_status (
    date TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""
_SRCT_NIGHT_SUMMARIES_SQL = """
CREATE TABLE IF NOT EXISTS srct_night_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    night_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    stop_reason TEXT NOT NULL,
    dates_attempted INTEGER NOT NULL,
    dates_done INTEGER NOT NULL,
    dates_not_found INTEGER NOT NULL,
    pending_before INTEGER NOT NULL,
    pending_after INTEGER NOT NULL,
    requests INTEGER NOT NULL,
    raw_new INTEGER NOT NULL,
    parsed_ok INTEGER NOT NULL,
    bronze_repaired INTEGER NOT NULL,
    xg_matches INTEGER NOT NULL,
    parse_failed_count INTEGER NOT NULL,
    failed_count INTEGER NOT NULL,
    failed_json TEXT NOT NULL,
    budget_cap INTEGER NOT NULL
)
"""
# 票 18 老季深度判定：跨夜持久不重探；shallow→full 升深=合法状态迁移
_SRCT_SEASON_DEPTH_SQL = """
CREATE TABLE IF NOT EXISTS srct_season_depth (
    season TEXT PRIMARY KEY,
    depth TEXT NOT NULL,
    probed_at TEXT NOT NULL
)
"""
# 票 65 当期班：sid 建档（联赛/开球/scope 资格——开售拍顺带取自 1x2d meta）；
# 拍去重不在此表（raw checkpoint 键 @open/@daily-*/@close 即台账，has() 即判）
_SRCT_SHIFT_MATCHES_SQL = """
CREATE TABLE IF NOT EXISTS srct_shift_matches (
    sid TEXT PRIMARY KEY,
    league TEXT NOT NULL DEFAULT '',
    home TEXT NOT NULL DEFAULT '',
    away TEXT NOT NULL DEFAULT '',
    kickoff_utc TEXT,
    in_scope INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    beats INTEGER NOT NULL DEFAULT 0,
    last_beat_at TEXT
)
"""
_NIGHT_SUMMARY_COLUMNS = (
    "night_date",
    "started_at",
    "ended_at",
    "stop_reason",
    "dates_attempted",
    "dates_done",
    "dates_not_found",
    "pending_before",
    "pending_after",
    "requests",
    "raw_new",
    "parsed_ok",
    "bronze_repaired",
    "xg_matches",
    "parse_failed_count",
    "failed_count",
    "failed_json",
    "budget_cap",
)
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
        conn = self._checkpoint()
        conn.execute(_RAW_ARTIFACTS_SQL)
        conn.execute(_SRCT_DAY_STATUS_SQL)
        conn.execute(_SRCT_NIGHT_SUMMARIES_SQL)
        conn.execute(_SRCT_SEASON_DEPTH_SQL)
        conn.execute(_SRCT_SHIFT_MATCHES_SQL)
        conn.commit()

    def _checkpoint(self) -> sqlite3.Connection:
        # ponytail: 进程内单连接串行用；夜班单进程采集足够，多进程并发再上锁
        if self._conn is None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.checkpoint_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(_RAW_ARTIFACTS_SQL)
            self._conn.execute(_SRCT_DAY_STATUS_SQL)
            self._conn.execute(_SRCT_NIGHT_SUMMARIES_SQL)
            self._conn.execute(_SRCT_SEASON_DEPTH_SQL)
            self._conn.execute(_SRCT_SHIFT_MATCHES_SQL)
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

    def iter_bronze_lines(self, provider: str, dataset: str) -> Iterator[str]:
        """
        逐行流式读 bronze NDJSON 原文（内存有界遍历；文件不存在静默空）。

        票 19 选轨/spill 遍用——只解 gzip 不落整表（多 member 追加对
        顺序读取透明）。调用方自行 json.loads 需要的行。
        """
        path = self.bronze_path(provider, dataset)
        if not path.exists():
            return
        with gzip.open(path, mode="rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield line.rstrip("\n")

    def set_day_status(self, date: str, status: str) -> None:
        """记/改一日状态（done / not_found；夜班台账，幂等覆盖）。"""
        self._checkpoint().execute(
            """
            INSERT OR REPLACE INTO srct_day_status (date, status, recorded_at)
            VALUES (?, ?, ?)
            """,
            (date, status, utc_now_iso()),
        )
        self._checkpoint().commit()

    def day_status_dates(self, status: str) -> set[str]:
        """该状态的全部日期（夜班 pending 计算排除集）。"""
        rows = self._checkpoint().execute(
            "SELECT date FROM srct_day_status WHERE status=?", (status,)
        )
        return {str(row["date"]) for row in rows}

    def season_depths(self) -> dict[str, str]:
        """季深度判定全表（票 18：夜班跨夜直读不重探）。"""
        rows = self._checkpoint().execute("SELECT season, depth FROM srct_season_depth")
        return {str(row["season"]): str(row["depth"]) for row in rows}

    def set_season_depth(self, season: str, depth: str) -> None:
        """记/升一季深度（full/shallow；同值重写无害）。"""
        self._checkpoint().execute(
            """
            INSERT OR REPLACE INTO srct_season_depth (season, depth, probed_at)
            VALUES (?, ?, ?)
            """,
            (season, depth, utc_now_iso()),
        )
        self._checkpoint().commit()

    def upsert_shift_match(self, row: Mapping[str, object]) -> None:
        """当期班 sid 建档/刷新（票 65；键=sid，开售拍后随拍更新计数）。"""
        columns = (
            "sid",
            "league",
            "home",
            "away",
            "kickoff_utc",
            "in_scope",
            "beats",
            "last_beat_at",
        )
        values = tuple(row.get(c) for c in columns)
        updates = ",".join(f"{c}=excluded.{c}" for c in columns if c != "sid")
        self._checkpoint().execute(
            # 列名来自模块常量元组，非用户输入；first_seen_at 插入盖戳、
            # 更新保留（不在 DO UPDATE 集）
            "INSERT INTO srct_shift_matches (first_seen_at,"  # noqa: S608
            + f"{','.join(columns)}) VALUES (?,{','.join('?' for _ in columns)})"
            + f" ON CONFLICT(sid) DO UPDATE SET {updates}",
            (utc_now_iso(), *values),
        )
        self._checkpoint().commit()

    def shift_matches(self) -> dict[str, dict[str, object]]:
        """当期班建档全表（sid → 行；拍决策的联赛/开球/scope 事实源）。"""
        rows = self._checkpoint().execute("SELECT * FROM srct_shift_matches")
        return {str(row["sid"]): dict(row) for row in rows}

    def record_night_summary(self, row: Mapping[str, object]) -> None:
        """落一夜摘要行（键 = _NIGHT_SUMMARY_COLUMNS；晨检口径）。"""
        columns = ",".join(_NIGHT_SUMMARY_COLUMNS)
        marks = ",".join("?" for _ in _NIGHT_SUMMARY_COLUMNS)
        self._checkpoint().execute(
            # 列名/占位来自模块常量元组，非用户输入
            f"INSERT INTO srct_night_summaries ({columns}) VALUES ({marks})",  # noqa: S608
            tuple(row[column] for column in _NIGHT_SUMMARY_COLUMNS),
        )
        self._checkpoint().commit()

    def night_summaries(self, limit: int = 20) -> list[dict[str, object]]:
        """最近的夜班摘要（新→旧；CLI --list / 晨检用）。"""
        rows = self._checkpoint().execute(
            """
            SELECT * FROM srct_night_summaries ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    def close(self) -> None:
        """关 checkpoint 连接（树文件保留）。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
