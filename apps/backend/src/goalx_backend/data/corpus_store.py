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

import fcntl
import gzip
import hashlib
import json
import re
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
# 票 18 月度探针账（2026-10-02 用户裁决加密采样）：老季每月首个有场次日
# 全深探一日；空场月不记账（零证据≠无数据）。判定=任一月有证据即 full，
# 全部 12 月干净零证据才 shallow
_SRCT_SEASON_PROBE_MONTHS_SQL = """
CREATE TABLE IF NOT EXISTS srct_season_probe_months (
    season TEXT NOT NULL,
    month TEXT NOT NULL,
    PRIMARY KEY (season, month)
)
"""
# 票 71 JC 当期拍：matchId 建档（kickoff 取自 calculator 发现响应；收口
# 判据=开球已过而裸键 raw 缺——fixedBonus 存档永在，赛后任意时点可收）
_JC_SHIFT_MATCHES_SQL = """
CREATE TABLE IF NOT EXISTS jc_shift_matches (
    match_id TEXT PRIMARY KEY,
    league TEXT NOT NULL DEFAULT '',
    home TEXT NOT NULL DEFAULT '',
    away TEXT NOT NULL DEFAULT '',
    kickoff_utc TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT,
    beats INTEGER NOT NULL DEFAULT 0,
    finalized INTEGER NOT NULL DEFAULT 0
)
"""
# 票 67 JC 回填日账（done 日不重枚举——十年一轮后真零请求心跳的前提）
_JC_BACKFILL_DAYS_SQL = """
CREATE TABLE IF NOT EXISTS jc_backfill_days (
    day TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    recorded_at TEXT NOT NULL
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
# 票 79 卫报语料：游标断点（单行状态机：backfill 深翻页锚定 to-date
# 防新文插入漂移；daily 日增量）+ 免费层 500/日请求账本（404 也记 1）
_GUARDIAN_SYNC_STATE_SQL = """
CREATE TABLE IF NOT EXISTS guardian_sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    phase TEXT NOT NULL DEFAULT 'backfill',
    from_date TEXT NOT NULL DEFAULT '1999-01-01',
    to_date TEXT NOT NULL DEFAULT '',
    page INTEGER NOT NULL DEFAULT 1,
    last_completed_date TEXT,
    total_articles INTEGER NOT NULL DEFAULT 0,
    boundary TEXT,
    updated_at TEXT NOT NULL
)
"""
# 2026-09-27 追加列（跨进程断点续跑需要持久化末篇发布日；存量表 ALTER 补列）
_GUARDIAN_BOUNDARY_ALTER_SQL = (
    "ALTER TABLE guardian_sync_state ADD COLUMN boundary TEXT"
)


def _ensure_boundary_column(conn: sqlite3.Connection) -> None:
    """幂等补齐 boundary 列（PRAGMA 探测，老 checkpoint 库升级）。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(guardian_sync_state)")}
    if "boundary" not in cols:
        conn.execute(_GUARDIAN_BOUNDARY_ALTER_SQL)


_GUARDIAN_REQUEST_DAYS_SQL = """
CREATE TABLE IF NOT EXISTS guardian_request_days (
    day TEXT PRIMARY KEY,
    requests INTEGER NOT NULL DEFAULT 0
)
"""
# Bronze sid 索引：解析层防重（_bronze_sids 原全量解压 bronze GB 级 ~10 分钟/
# 每日一次）的 O(1) 载体——append_bronze 同步 upsert；anchor=文件 size:mtime，
# 外部改动（文件被删/旧代码进程直写）触发整体重扫，保住"文件即真相"自愈
_BRONZE_SIDS_SQL = """
CREATE TABLE IF NOT EXISTS bronze_sids (
    provider TEXT NOT NULL,
    dataset TEXT NOT NULL,
    sid TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    PRIMARY KEY (provider, dataset, sid)
)
"""
_BRONZE_SID_BACKFILL_SQL = """
CREATE TABLE IF NOT EXISTS bronze_sid_backfill (
    provider TEXT NOT NULL,
    dataset TEXT NOT NULL,
    sids INTEGER NOT NULL,
    anchor TEXT NOT NULL,
    backfilled_at TEXT NOT NULL,
    PRIMARY KEY (provider, dataset)
)
"""
# checkpoint 建表单处登记（票 02 前 ensure_tree 与 _checkpoint 双抄两处；
# 现两路共用本序列；新表 = 加一条 DDL 常量 + 入本元组）
_CHECKPOINT_TABLE_SQL = (
    _RAW_ARTIFACTS_SQL,
    _SRCT_DAY_STATUS_SQL,
    _SRCT_NIGHT_SUMMARIES_SQL,
    _SRCT_SEASON_DEPTH_SQL,
    _SRCT_SEASON_PROBE_MONTHS_SQL,
    _SRCT_SHIFT_MATCHES_SQL,
    _JC_SHIFT_MATCHES_SQL,
    _JC_BACKFILL_DAYS_SQL,
    _GUARDIAN_SYNC_STATE_SQL,
    _GUARDIAN_REQUEST_DAYS_SQL,
    _BRONZE_SIDS_SQL,
    _BRONZE_SID_BACKFILL_SQL,
)
_TABLE_NAME_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)")


def checkpoint_table_names() -> tuple[str, ...]:
    """已登记 checkpoint 表名（数据集注册表规格核对面；顺序=建表顺序）。"""
    names: list[str] = []
    for ddl in _CHECKPOINT_TABLE_SQL:
        match = _TABLE_NAME_RE.match(ddl.strip())
        if match is None:  # 建表格式漂移会让核对面无声缩水——失败即炸
            msg = f"checkpoint DDL 缺表名（格式漂移？）：{ddl.strip()[:60]}"
            raise AssertionError(msg)
        names.append(match.group(1))
    return tuple(names)


def _create_checkpoint_tables(conn: sqlite3.Connection) -> None:
    """建全部 checkpoint 表 + 老库 boundary 补列（幂等）。"""
    for ddl in _CHECKPOINT_TABLE_SQL:
        conn.execute(ddl)
    _ensure_boundary_column(conn)
    conn.commit()


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
        self._bronze_sid_cache: dict[tuple[str, str, str], set[str]] = {}

    @property
    def checkpoint_path(self) -> Path:
        """Checkpoint SQLite 路径（树根独立文件）。"""
        return self.root / "checkpoint.db"

    def ensure_tree(self) -> None:
        """建目录树与 checkpoint 表（幂等）。"""
        for sub in _TREE_SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        _create_checkpoint_tables(self._checkpoint())

    def _checkpoint(self) -> sqlite3.Connection:
        # ponytail: 进程内单连接串行用；夜班单进程采集足够，多进程并发再上锁
        if self._conn is None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.checkpoint_path)
            self._conn.row_factory = sqlite3.Row
            _create_checkpoint_tables(self._conn)
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

    def raw_dir(self, provider: str, dataset: str) -> Path:
        """Raw 数据集目录（布局唯一落点；raw_path 与计数走此）。"""
        return self.root / "raw" / provider / dataset

    def raw_path(self, provider: str, dataset: str, key: str, *, ext: str = "") -> Path:
        """Raw 工件约定路径（不保证已存在）。"""
        return self.raw_dir(provider, dataset) / f"{key}{ext}.gz"

    def silver_path(self, provider: str, dataset: str) -> Path:
        """Silver 数据集根（布局唯一落点：{provider}/{dataset} 分区树）。"""
        return self.root / "silver" / provider / dataset

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
        try:
            tmp.replace(path)
        except FileNotFoundError:
            # 双班竞态:对端先 replace 走了同名 .part——目标已达成,当成功
            if not (path.exists() and not tmp.exists()):
                raise
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
        # 跨进程互斥：day 班/夜班/马拉松多写者并发 append 同一 gzip 文件
        # 会使 member 字节交错撕裂（2026-10-03 实测四数据集尾部 BAD）；
        # flock 把"起 member→写行→close"整段串行化。checkpoint SQLite 自
        # 带锁不在此护。
        with path.open("ab") as raw_fh:
            fcntl.flock(raw_fh, fcntl.LOCK_EX)
            try:
                with gzip.GzipFile(fileobj=raw_fh, mode="ab", mtime=0) as fh:
                    for row in rows:
                        fh.write((json.dumps(row, ensure_ascii=False) + "\n").encode())
            finally:
                fcntl.flock(raw_fh, fcntl.LOCK_UN)
        index_rows = [
            (provider, dataset, str(row["sid"]), str(row["parser_version"]))
            for row in rows
            if row.get("sid") is not None and row.get("parser_version") is not None
        ]
        if index_rows:
            conn = self._checkpoint()
            conn.executemany(
                "INSERT OR REPLACE INTO bronze_sids VALUES (?, ?, ?, ?)",
                index_rows,
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO bronze_sid_backfill
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    dataset,
                    -1,  # 行数由重扫维护；自写路径 anchor 同步防误扫
                    self._bronze_anchor(provider, dataset),
                    utc_now_iso(),
                ),
            )
            conn.commit()
            for _, _, sid, version in index_rows:
                cached = self._bronze_sid_cache.get((provider, dataset, version))
                if cached is not None:
                    cached.add(sid)
        return len(rows)

    def _bronze_anchor(self, provider: str, dataset: str) -> str:
        """Bronze 文件身份锚（size:mtime；缺文件=空串）。"""
        path = self.bronze_path(provider, dataset)
        try:
            stat = path.stat()
        except OSError:
            return ""
        return f"{stat.st_size}:{stat.st_mtime_ns}"

    def bronze_sids(self, provider: str, dataset: str, parser_version: str) -> set[str]:
        """
        该数据集指定解析器版本已落 bronze 的 sid 集（防重/回补判断）。

        载体=checkpoint 表（append_bronze 同步 upsert）；台账 anchor 与文件
        stat 不符（首次访问/外部改动）→ 流式重扫重建，此后进程内缓存。
        """
        cache_key = (provider, dataset, parser_version)
        cached = self._bronze_sid_cache.get(cache_key)
        if cached is not None:
            return cached
        conn = self._checkpoint()
        anchor = self._bronze_anchor(provider, dataset)
        row = conn.execute(
            "SELECT anchor FROM bronze_sid_backfill WHERE provider=? AND dataset=?",
            (provider, dataset),
        ).fetchone()
        if row is None or row["anchor"] != anchor:
            self._backfill_bronze_sids(provider, dataset, anchor)
        sids = {
            str(r["sid"])
            for r in conn.execute(
                """
                SELECT sid FROM bronze_sids
                WHERE provider=? AND dataset=? AND parser_version=?
                """,
                (provider, dataset, parser_version),
            )
        }
        self._bronze_sid_cache[cache_key] = sids
        return sids

    def _backfill_bronze_sids(self, provider: str, dataset: str, anchor: str) -> None:
        """按当前文件全量重建索引（外部改动/首次访问；幂等）。"""
        for key in [k for k in self._bronze_sid_cache if k[:2] == (provider, dataset)]:
            del self._bronze_sid_cache[key]
        index_rows: list[tuple[str, str, str, str]] = []
        for line in self.iter_bronze_lines(provider, dataset):
            envelope = json.loads(line)
            sid = envelope.get("sid")
            version = envelope.get("parser_version")
            if sid is not None and version is not None:
                index_rows.append((provider, dataset, str(sid), str(version)))
        conn = self._checkpoint()
        conn.execute(
            "DELETE FROM bronze_sids WHERE provider=? AND dataset=?",
            (provider, dataset),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO bronze_sids VALUES (?, ?, ?, ?)",
            index_rows,
        )
        conn.execute(
            "INSERT OR REPLACE INTO bronze_sid_backfill VALUES (?, ?, ?, ?, ?)",
            (provider, dataset, len(index_rows), anchor, utc_now_iso()),
        )
        conn.commit()

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

    def season_probe_months(self, season: str) -> set[str]:
        """该季已探月集（票 18 月度采样：有场次的探针日才记账）。"""
        rows = self._checkpoint().execute(
            "SELECT month FROM srct_season_probe_months WHERE season=?", (season,)
        )
        return {str(row["month"]) for row in rows}

    def add_season_probe_month(self, season: str, month: str) -> None:
        """记一探针月（幂等）。"""
        self._checkpoint().execute(
            """
            INSERT OR IGNORE INTO srct_season_probe_months (season, month)
            VALUES (?, ?)
            """,
            (season, month),
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

    def upsert_jc_shift_match(self, row: Mapping[str, object]) -> None:
        """JC 当期拍 matchId 建档/刷新（票 71；同 srct_shift_matches 形态）。"""
        columns = (
            "match_id",
            "league",
            "home",
            "away",
            "kickoff_utc",
            "beats",
            "last_seen_at",
            "finalized",
        )
        values = tuple(row.get(c) for c in columns)
        updates = ",".join(f"{c}=excluded.{c}" for c in columns if c != "match_id")
        self._checkpoint().execute(
            # 列名来自模块常量元组，非用户输入；first_seen_at 插入盖戳、更新保留
            "INSERT INTO jc_shift_matches (first_seen_at,"  # noqa: S608
            + f"{','.join(columns)}) VALUES (?,{','.join('?' for _ in columns)})"
            + f" ON CONFLICT(match_id) DO UPDATE SET {updates}",
            (utc_now_iso(), *values),
        )
        self._checkpoint().commit()

    def jc_shift_matches(self) -> dict[str, dict[str, object]]:
        """JC 当期拍建档全表（matchId → 行；拍决策/收口判据事实源）。"""
        rows = self._checkpoint().execute("SELECT * FROM jc_shift_matches")
        return {str(row["match_id"]): dict(row) for row in rows}

    def set_jc_backfill_day(self, day: str, status: str = "done") -> None:
        """记 JC 回填一日状态（done=该日 mid 全采；幂等覆盖）。"""
        self._checkpoint().execute(
            """
            INSERT OR REPLACE INTO jc_backfill_days (day, status, recorded_at)
            VALUES (?, ?, ?)
            """,
            (day, status, utc_now_iso()),
        )
        self._checkpoint().commit()

    def jc_backfill_days(self, status: str = "done") -> set[str]:
        """JC 回填该状态的全部日期。"""
        rows = self._checkpoint().execute(
            "SELECT day FROM jc_backfill_days WHERE status=?", (status,)
        )
        return {str(row["day"]) for row in rows}

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

    def guardian_state(self) -> dict[str, object]:
        """卫报语料游标状态（无行=初生默认：backfill 自 1999-01-01 起）。"""
        row = (
            self._checkpoint()
            .execute("SELECT * FROM guardian_sync_state WHERE id = 1")
            .fetchone()
        )
        if row is None:
            return {
                "phase": "backfill",
                "from_date": "1999-01-01",
                "to_date": "",
                "page": 1,
                "last_completed_date": None,
                "total_articles": 0,
                "boundary": None,
            }
        return dict(row)

    def upsert_guardian_state(self, row: Mapping[str, object]) -> None:
        """整行覆写游标状态（票 79；键集与 guardian_state 默认行一致）。"""
        columns = (
            "phase",
            "from_date",
            "to_date",
            "page",
            "last_completed_date",
            "total_articles",
            "boundary",
        )
        self._checkpoint().execute(
            # 列名来自模块常量，非用户输入
            "INSERT INTO guardian_sync_state (id,"  # noqa: S608
            + f"{','.join(columns)},updated_at)"
            + f" VALUES (1,{','.join('?' for _ in columns)},?)"
            + " ON CONFLICT(id) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in columns)
            + ",updated_at=excluded.updated_at",
            (*tuple(row.get(c) for c in columns), utc_now_iso()),
        )
        self._checkpoint().commit()

    def guardian_requests(self, day: str) -> int:
        """该 UTC 日已用请求数（预算闸门查询）。"""
        row = (
            self._checkpoint()
            .execute("SELECT requests FROM guardian_request_days WHERE day = ?", (day,))
            .fetchone()
        )
        return int(row["requests"]) if row else 0

    def increment_guardian_requests(self, day: str) -> int:
        """记账 +1 并返回当日新计数（404 也记 1，propline 同型）。"""
        cur = self._checkpoint().execute(
            """
            INSERT INTO guardian_request_days (day, requests) VALUES (?, 1)
            ON CONFLICT(day) DO UPDATE SET requests = requests + 1
            """,
            (day,),
        )
        self._checkpoint().commit()
        del cur
        return self.guardian_requests(day)

    def close(self) -> None:
        """关 checkpoint 连接（树文件保留）。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
